"""Memory-focused stress benchmarks: Polars vs C++ toward TB scale.

Where ``bench.py`` is a correctness+speed sweep at small scales, ``stress.py``
is built to answer one question: *how does peak memory behave as the dataset
grows toward a ~4 TB uncompressed / ~0.5 TB compressed snapshot, and which knobs
keep it under a RAM budget?*

It drives the ETL through the instrumented single-run harness (``run_bench.py``)
in isolated subprocesses and supports four things (combine freely):

* ``--scaling``   : a geometric ladder of dataset sizes at a fixed config.
* ``--knob-sweep``: cross num_shards x MEDS_SORT_SHARD_CONCURRENCY x
  POLARS_MAX_THREADS at a fixed mid-size, to map the memory/throughput frontier.
* ``--budget-bytes B``: find the smallest num_shards (with concurrency=1) whose
  measured peak RSS stays under B, then verify it under a hard RLIMIT_AS cap.
* extrapolation: fit peak RSS vs (concurrency x largest-shard bytes) from the
  runs above and predict the num_shards needed for ``--target-uncompressed-bytes``
  (default 4 TB) under a RAM budget. ``--full-scale`` actually generates and runs
  the full target instead of extrapolating.

Run with full permissions so numpy/pyarrow/the C++ extension load:

    python benchmarks/stress.py --scaling --backends polars
    python benchmarks/stress.py --scaling --knob-sweep --budget-bytes 32GiB
    python benchmarks/stress.py --full-scale --target-uncompressed-bytes 4TB
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import polars as pl

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from gen_data import _human_bytes, _parse_bytes, generate  # noqa: E402
import report as report_mod  # noqa: E402

RESULTS_DIR = HERE / "results"
WORK_DIR = Path(os.environ.get("BENCH_WORK_DIR", "/tmp/meds_etl_stress"))

# Default extrapolation target: ~4 TB uncompressed.
_DEFAULT_TARGET_BYTES = 4 * 10**12


@dataclass
class RunResult:
    label: str
    uncompressed_bytes: int
    on_disk_bytes: int
    backend: str
    num_shards: int
    num_threads: int
    concurrency: int
    status: str
    wall_best_s: float
    wall_mean_s: float
    peak_rss_bytes: int
    peak_rss_stage_a_bytes: int
    peak_rss_stage_b_bytes: int
    out_bytes: int
    out_files: int
    out_max_file_bytes: int
    mem_limit_bytes: int
    error: Optional[str] = None
    rss_series: List[Tuple[float, int]] = field(default_factory=list)

    @property
    def largest_shard_uncompressed_bytes(self) -> float:
        """Skew-aware estimate of the largest shard's uncompressed size.

        The largest *output* file's share of total compressed bytes is used as a
        proxy for that shard's share of the (uncompressed) data, assuming roughly
        uniform compressibility across shards.
        """
        if self.out_bytes <= 0 or self.out_max_file_bytes <= 0:
            if self.num_shards > 0:
                return self.uncompressed_bytes / self.num_shards
            return float(self.uncompressed_bytes)
        fraction = self.out_max_file_bytes / self.out_bytes
        return fraction * self.uncompressed_bytes


# --------------------------------------------------------------------------- #
# Running backends
# --------------------------------------------------------------------------- #


def _run_subprocess(
    backend: str,
    source: Path,
    target: Path,
    num_shards: int,
    num_threads: int,
    concurrency: int,
    mem_limit_bytes: int,
) -> Dict:
    env = dict(os.environ)
    if backend == "polars":
        env["POLARS_MAX_THREADS"] = str(num_threads)
        env["MEDS_SORT_SHARD_CONCURRENCY"] = str(concurrency)
    cmd = [
        sys.executable,
        str(HERE / "run_bench.py"),
        "--backend",
        backend,
        "--source",
        str(source),
        "--target",
        str(target),
        "--num-shards",
        str(num_shards),
        "--num-threads",
        str(num_threads),
        "--mem-limit-bytes",
        str(mem_limit_bytes),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        # A non-zero exit under a memory cap is almost certainly an OOM kill
        # (native abort / SIGKILL) that couldn't emit clean JSON.
        status = "oom" if mem_limit_bytes > 0 else "error"
        tail = proc.stderr.strip().splitlines()[-5:]
        return {
            "status": status,
            "error": f"exit {proc.returncode}: " + " | ".join(tail),
        }
    lines = proc.stdout.strip().splitlines()
    if not lines:
        return {"status": "error", "error": "no output from run_bench"}
    return json.loads(lines[-1])


def run_backend(
    backend: str,
    label: str,
    uncompressed_bytes: int,
    on_disk_bytes: int,
    source: Path,
    target: Path,
    num_shards: int,
    num_threads: int,
    concurrency: int,
    repeats: int,
    warmup: int,
    mem_limit_bytes: int,
) -> RunResult:
    walls: List[float] = []
    peak = 0
    peak_a = 0
    peak_b = 0
    out_bytes = out_files = out_max = 0
    series: List[Tuple[float, int]] = []
    status = "ok"
    error: Optional[str] = None

    total_runs = warmup + max(1, repeats)
    for i in range(total_runs):
        metrics = _run_subprocess(
            backend,
            source,
            target,
            num_shards,
            num_threads,
            concurrency if backend == "polars" else num_threads,
            mem_limit_bytes,
        )
        status = metrics.get("status", "ok")
        if status != "ok":
            error = metrics.get("error")
            break
        if i < warmup:
            continue
        walls.append(metrics["wall_s"])
        if metrics["peak_rss_bytes"] >= peak:
            peak = metrics["peak_rss_bytes"]
            series = metrics.get("rss_series", [])
        peak_a = max(peak_a, metrics.get("peak_rss_stage_a_bytes") or 0)
        peak_b = max(peak_b, metrics.get("peak_rss_stage_b_bytes") or 0)
        out_bytes = metrics["out_bytes"]
        out_files = metrics["out_files"]
        out_max = metrics.get("out_max_file_bytes", 0)

    return RunResult(
        label=label,
        uncompressed_bytes=uncompressed_bytes,
        on_disk_bytes=on_disk_bytes,
        backend=backend,
        num_shards=num_shards,
        num_threads=num_threads,
        concurrency=concurrency if backend == "polars" else num_threads,
        status=status,
        wall_best_s=min(walls) if walls else float("nan"),
        wall_mean_s=statistics.mean(walls) if walls else float("nan"),
        peak_rss_bytes=peak,
        peak_rss_stage_a_bytes=peak_a,
        peak_rss_stage_b_bytes=peak_b,
        out_bytes=out_bytes,
        out_files=out_files,
        out_max_file_bytes=out_max,
        mem_limit_bytes=mem_limit_bytes,
        error=error,
        rss_series=series,
    )


# --------------------------------------------------------------------------- #
# Data generation (cached per size label)
# --------------------------------------------------------------------------- #


def ensure_dataset(
    label: str,
    uncompressed_bytes: int,
    num_files: int,
    num_extra_property_columns: int,
    string_cardinality: int,
    subject_skew: float,
    num_subjects: int,
    seed: int,
) -> Tuple[Path, int, int]:
    source = WORK_DIR / f"src_{label}"
    marker = source / ".genstats.json"
    if marker.exists():
        stats = json.loads(marker.read_text())
        return source, stats["uncompressed_bytes"], stats["on_disk_bytes"]

    print(f"[gen] {label}: target ~{_human_bytes(uncompressed_bytes)} ...", flush=True)
    t0 = time.perf_counter()
    stats = generate(
        out=source,
        num_subjects=num_subjects,
        events_per_subject=0,
        num_files=num_files,
        num_extra_property_columns=num_extra_property_columns,
        string_cardinality=string_cardinality,
        null_time_frac=0.02,
        null_value_frac=0.5,
        seed=seed,
        target_uncompressed_bytes=uncompressed_bytes,
        subject_skew=subject_skew,
    )
    marker.write_text(
        json.dumps(
            {
                "uncompressed_bytes": stats.uncompressed_bytes,
                "on_disk_bytes": stats.on_disk_bytes,
                "num_rows": stats.num_rows,
            }
        )
    )
    print(
        f"      {stats.num_rows:,} rows, on-disk {_human_bytes(stats.on_disk_bytes)}, "
        f"uncompressed {_human_bytes(stats.uncompressed_bytes)} "
        f"in {time.perf_counter() - t0:.1f}s",
        flush=True,
    )
    return source, stats.uncompressed_bytes, stats.on_disk_bytes


# --------------------------------------------------------------------------- #
# Extrapolation model
# --------------------------------------------------------------------------- #


@dataclass
class MemoryModel:
    """Two-term peak-RSS model capturing both stages of the pipeline.

    ``peak_rss ~= c0 + c1 * (concurrency * largest_shard_bytes) + c2 * num_shards``

    * The ``c1`` term is Stage B: each concurrent sort holds a full shard, so
      peak grows with concurrency x largest-shard size (which *falls* as shards
      rise).
    * The ``c2`` term is Stage A: the partitioned ``sink_parquet`` keeps one open
      writer per shard, so peak *grows* with num_shards.

    These pull in opposite directions, so there is a memory-optimal shard count.
    """

    intercept_bytes: float
    coef_shard_mem: float  # c1
    coef_num_shards: float  # c2
    skew_factor: float  # largest_shard ~ skew_factor * total / num_shards
    n_points: int
    r2: float

    def _largest_shard(self, target_uncompressed: float, num_shards: int) -> float:
        return self.skew_factor * target_uncompressed / max(1, num_shards)

    def predict_peak(
        self, concurrency: int, num_shards: int, target_uncompressed: float
    ) -> float:
        largest = self._largest_shard(target_uncompressed, num_shards)
        return (
            self.intercept_bytes
            + self.coef_shard_mem * concurrency * largest
            + self.coef_num_shards * num_shards
        )

    def recommend(
        self, target_uncompressed: int, budget_bytes: int, concurrency: int
    ) -> Dict:
        """Recommend a memory-optimal num_shards and whether it fits the budget."""
        c1, c2 = self.coef_shard_mem, self.coef_num_shards
        a = c1 * concurrency * self.skew_factor * target_uncompressed  # /N term
        # peak(N) = c0 + a/N + c2*N ; minimized at N* = sqrt(a/c2).
        if a > 0 and c2 > 0:
            n_opt = max(1, round(math.sqrt(a / c2)))
        elif a > 0 and c2 <= 0:
            # Stage A doesn't grow: more shards always help; push under budget.
            headroom = budget_bytes - self.intercept_bytes
            n_opt = max(1, math.ceil(a / headroom)) if headroom > 0 else None
        else:
            n_opt = max(1, round(target_uncompressed / (2 * 2**30)))  # fallback ~2GiB/shard
        if n_opt is None:
            return {"recommended_num_shards": None, "predicted_peak_bytes": None,
                    "feasible": False}
        predicted = self.predict_peak(concurrency, n_opt, target_uncompressed)
        return {
            "recommended_num_shards": n_opt,
            "predicted_peak_bytes": predicted,
            "feasible": predicted <= budget_bytes,
        }


def fit_model(results: List[RunResult]) -> Optional[MemoryModel]:
    import numpy as np

    pts = [
        r
        for r in results
        if r.backend == "polars" and r.status == "ok" and r.peak_rss_bytes > 0
    ]
    if len(pts) < 3:
        return None

    skews = [
        (r.out_max_file_bytes / r.out_bytes) * r.num_shards
        for r in pts
        if r.out_bytes > 0 and r.out_max_file_bytes > 0
    ]
    skew_factor = statistics.mean(skews) if skews else 1.0

    # Design matrix: [1, concurrency*largest_shard, num_shards].
    feature_rows = [
        [1.0, r.concurrency * r.largest_shard_uncompressed_bytes, float(r.num_shards)]
        for r in pts
    ]
    x = np.array(feature_rows, dtype=float)
    y = np.array([float(r.peak_rss_bytes) for r in pts], dtype=float)

    # Need variation in both non-constant features to identify the model.
    if np.ptp(x[:, 1]) == 0 or np.ptp(x[:, 2]) == 0:
        return None

    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    pred = x @ coef
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    return MemoryModel(
        intercept_bytes=float(coef[0]),
        coef_shard_mem=float(coef[1]),
        coef_num_shards=float(coef[2]),
        skew_factor=skew_factor,
        n_points=len(pts),
        r2=r2,
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def _size_ladder(spec: Optional[str]) -> List[Tuple[str, int]]:
    if spec:
        sizes = [_parse_bytes(s) for s in spec.split(",")]
    else:
        sizes = [1 * 2**30, 4 * 2**30, 16 * 2**30]  # 1, 4, 16 GiB
    return [(_size_label(b), b) for b in sizes]


def _size_label(nbytes: int) -> str:
    return _human_bytes(nbytes).replace(" ", "").replace(".0", "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backends", nargs="+", default=["polars"], choices=["polars", "cpp"])
    parser.add_argument("--scaling", action="store_true", help="Run the size ladder.")
    parser.add_argument("--sizes", type=str, default=None, help="Comma sizes, e.g. 1GiB,4GiB,16GiB")
    parser.add_argument("--knob-sweep", action="store_true")
    parser.add_argument("--knob-size", type=_parse_bytes, default=4 * 2**30)
    parser.add_argument(
        "--shard-list",
        type=str,
        default=None,
        help="Comma-separated num_shards to sweep, e.g. 56,128,256,512 (overrides default).",
    )
    parser.add_argument(
        "--thread-list",
        type=str,
        default=None,
        help="Comma-separated num_threads to sweep (overrides default).",
    )
    parser.add_argument(
        "--conc-list",
        type=str,
        default=None,
        help="Comma-separated MEDS_SORT_SHARD_CONCURRENCY values to sweep (overrides default).",
    )
    parser.add_argument("--budget-bytes", type=_parse_bytes, default=0)
    parser.add_argument("--target-uncompressed-bytes", type=_parse_bytes, default=_DEFAULT_TARGET_BYTES)
    parser.add_argument("--full-scale", action="store_true", help="Actually run the full target size.")
    parser.add_argument("--num-shards", type=int, default=None, help="Fixed shards for scaling runs.")
    parser.add_argument("--num-threads", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=1, help="MEDS_SORT_SHARD_CONCURRENCY for fixed runs.")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--num-files", type=int, default=16)
    parser.add_argument("--num-extra-property-columns", type=int, default=4)
    parser.add_argument("--string-cardinality", type=int, default=512)
    parser.add_argument("--subject-skew", type=float, default=1.0)
    parser.add_argument("--num-subjects", type=int, default=1_000_000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mem-limit-bytes", type=_parse_bytes, default=0)
    parser.add_argument(
        "--verify-cap",
        action="store_true",
        help="Advisory: re-run the budget winner under an RLIMIT_AS cap.",
    )
    args = parser.parse_args()

    ncpu = os.cpu_count() or 8
    num_threads = args.num_threads or ncpu
    fixed_shards = args.num_shards or ncpu
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    if not (args.scaling or args.knob_sweep or args.budget_bytes or args.full_scale):
        args.scaling = True  # sensible default

    results: List[RunResult] = []
    equivalence: List[str] = []

    def _run_config(label, unc, disk, source, num_shards, threads, concurrency, mem_limit):
        targets: Dict[str, Path] = {}
        for backend in args.backends:
            target = WORK_DIR / f"out_{label}_{num_shards}_{threads}_{concurrency}_{backend}"
            targets[backend] = target
            print(
                f"[run] {label} backend={backend} shards={num_shards} threads={threads} "
                f"conc={concurrency} limit={_human_bytes(mem_limit) if mem_limit else 'none'}",
                flush=True,
            )
            res = run_backend(
                backend, label, unc, disk, source, target, num_shards, threads,
                concurrency, args.repeats, args.warmup, mem_limit,
            )
            results.append(res)
            if res.status != "ok":
                print(f"      {res.status.upper()}: {res.error}", flush=True)
            else:
                print(
                    f"      wall={res.wall_best_s:.2f}s peak={_human_bytes(res.peak_rss_bytes)} "
                    f"(A={_human_bytes(res.peak_rss_stage_a_bytes)} "
                    f"B={_human_bytes(res.peak_rss_stage_b_bytes)}) "
                    f"out={_human_bytes(res.out_bytes)} files={res.out_files}",
                    flush=True,
                )
        _maybe_check_equivalence(targets, label, equivalence)
        return targets

    # ---- Scaling ladder --------------------------------------------------- #
    if args.scaling:
        for label, nbytes in _size_ladder(args.sizes):
            source, unc, disk = ensure_dataset(
                label, nbytes, args.num_files, args.num_extra_property_columns,
                args.string_cardinality, args.subject_skew, args.num_subjects, args.seed,
            )
            _run_config(label, unc, disk, source, fixed_shards, num_threads,
                        args.concurrency, args.mem_limit_bytes)

    # ---- Knob sweep ------------------------------------------------------- #
    if args.knob_sweep:
        label = "knob_" + _size_label(args.knob_size)
        source, unc, disk = ensure_dataset(
            label, args.knob_size, args.num_files, args.num_extra_property_columns,
            args.string_cardinality, args.subject_skew, args.num_subjects, args.seed,
        )
        if args.shard_list:
            shard_options = sorted({int(s) for s in args.shard_list.split(",")})
        else:
            shard_options = sorted({max(1, ncpu // 2), ncpu, 4 * ncpu})
        if args.conc_list:
            conc_options = sorted({int(c) for c in args.conc_list.split(",")})
        else:
            conc_options = sorted({1, min(4, num_threads)})
        if args.thread_list:
            thread_options = sorted({int(t) for t in args.thread_list.split(",")})
        else:
            thread_options = sorted({max(1, ncpu // 2), ncpu})
        for shards in shard_options:
            for conc in conc_options:
                for threads in thread_options:
                    _run_config(label, unc, disk, source, shards, threads, conc,
                                args.mem_limit_bytes)

    # ---- Budget search ---------------------------------------------------- #
    budget_recommendation: Optional[Dict] = None
    if args.budget_bytes:
        label = "budget_" + _size_label(args.knob_size)
        source, unc, disk = ensure_dataset(
            label, args.knob_size, args.num_files, args.num_extra_property_columns,
            args.string_cardinality, args.subject_skew, args.num_subjects, args.seed,
        )
        budget_recommendation = _budget_search(
            args, label, unc, disk, source, ncpu, num_threads, results, equivalence
        )

    # ---- Full-scale ------------------------------------------------------- #
    if args.full_scale:
        label = "full_" + _size_label(args.target_uncompressed_bytes)
        source, unc, disk = ensure_dataset(
            label, args.target_uncompressed_bytes, args.num_files,
            args.num_extra_property_columns, args.string_cardinality,
            args.subject_skew, args.num_subjects, args.seed,
        )
        _run_config(label, unc, disk, source, fixed_shards, num_threads,
                    args.concurrency, args.mem_limit_bytes)

    # ---- Fit + extrapolate ------------------------------------------------ #
    model = fit_model(results)
    extrapolation = None
    if model is not None:
        budget = args.budget_bytes or (32 * 2**30)
        rec = model.recommend(args.target_uncompressed_bytes, budget, 1)
        extrapolation = {
            "target_uncompressed_bytes": args.target_uncompressed_bytes,
            "budget_bytes": budget,
            "recommended_num_shards": rec["recommended_num_shards"],
            "predicted_peak_bytes": rec["predicted_peak_bytes"],
            "feasible": rec["feasible"],
            "recommended_concurrency": 1,
            "model": asdict(model),
        }

    report_path = report_mod.write_memory_report(
        RESULTS_DIR,
        [asdict(r) for r in results],
        equivalence=equivalence,
        extrapolation=extrapolation,
        budget_recommendation=budget_recommendation,
    )
    print(f"\nReport written to {report_path}")


def _budget_search(args, label, unc, disk, source, ncpu, num_threads, results, equivalence):
    """Find the smallest num_shards (concurrency=1) whose peak RSS fits the budget.

    The decision is based on *measured peak RSS*, which is the quantity that
    matters. An optional ``--verify-cap`` re-runs the winner under an
    ``RLIMIT_AS`` cap, but that caps virtual address space (which Arrow/Polars
    over-reserve well beyond RSS) and is unreliable -- especially on macOS -- so
    it is only advisory. For a faithful hard cap, use a Linux cgroup (see the
    report's budget section).
    """
    budget = args.budget_bytes
    print(f"[budget] searching num_shards to keep peak RSS < {_human_bytes(budget)}", flush=True)
    candidate_shards = sorted({ncpu, 2 * ncpu, 4 * ncpu, 8 * ncpu, 16 * ncpu, 32 * ncpu})
    chosen = None
    measured_peak = 0
    for shards in candidate_shards:
        target = WORK_DIR / f"out_{label}_budget_{shards}"
        res = run_backend(
            "polars", label, unc, disk, source, target, shards, num_threads,
            1, args.repeats, args.warmup, 0,
        )
        results.append(res)
        fits = res.status == "ok" and res.peak_rss_bytes <= budget
        print(
            f"      shards={shards}: peak={_human_bytes(res.peak_rss_bytes)} "
            f"{'OK' if fits else 'over'}",
            flush=True,
        )
        if fits:
            chosen = shards
            measured_peak = res.peak_rss_bytes
            break
    if chosen is None:
        return {"budget_bytes": budget, "chosen_num_shards": None,
                "measured_peak_bytes": 0, "cap_probe": None,
                "note": "no tested shard count fit the budget; raise budget or shards"}

    cap_probe = None
    if args.verify_cap:
        target = WORK_DIR / f"out_{label}_budget_verify"
        verify = run_backend(
            "polars", label, unc, disk, source, target, chosen, num_threads,
            1, args.repeats, args.warmup, budget,
        )
        results.append(verify)
        cap_probe = {"mechanism": "RLIMIT_AS (virtual, advisory)", "status": verify.status}
        print(f"[budget] shards={chosen} RLIMIT_AS probe: {verify.status} (advisory)", flush=True)

    return {"budget_bytes": budget, "chosen_num_shards": chosen,
            "measured_peak_bytes": measured_peak, "cap_probe": cap_probe}


def _maybe_check_equivalence(targets: Dict[str, Path], label: str, equivalence: List[str]) -> None:
    if "polars" not in targets or "cpp" not in targets:
        return
    try:
        from check_equivalence import check_scalable

        check_scalable(targets["polars"], targets["cpp"])
        msg = f"OK: {label} (polars == cpp)"
    except AssertionError as exc:
        msg = f"MISMATCH: {label}: {exc}"
    except Exception as exc:  # noqa: BLE001
        msg = f"SKIPPED: {label}: {exc}"
    print(f"      equivalence: {msg}", flush=True)
    equivalence.append(msg)


if __name__ == "__main__":
    main()
