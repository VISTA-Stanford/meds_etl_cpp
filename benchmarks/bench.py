"""Benchmark orchestrator: compares the Polars and C++ backends.

For each data scale and each (num_shards, num_threads) configuration, this runs
both backends in isolated subprocesses, records wall-clock time and peak RSS,
verifies the two backends produce semantically equivalent output, and writes a
markdown report (plus plots if matplotlib is installed) under
``benchmarks/results/``.

Run it with full permissions so numpy/pyarrow/the C++ extension work:

    python benchmarks/bench.py --quick
    python benchmarks/bench.py            # fuller sweep
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import polars as pl

from check_equivalence import check as check_equivalence
from gen_data import generate

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
RESULTS_DIR = HERE / "results"
WORK_DIR = Path(os.environ.get("BENCH_WORK_DIR", "/tmp/meds_etl_bench"))


@dataclass
class Scale:
    name: str
    num_subjects: int
    events_per_subject: int
    num_files: int
    num_extra_property_columns: int = 2
    string_cardinality: int = 2000

    @property
    def num_rows(self) -> int:
        return self.num_subjects * self.events_per_subject


@dataclass
class RunResult:
    scale: str
    backend: str
    num_shards: int
    num_threads: int
    num_rows: int
    wall_best_s: float
    wall_mean_s: float
    peak_rss_bytes: int
    out_bytes: int
    out_files: int
    cpu_total_s: float
    error: Optional[str] = None


def _human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def _run_subprocess(
    backend: str,
    source: Path,
    target: Path,
    num_shards: int,
    num_threads: int,
) -> Dict:
    env = dict(os.environ)
    if backend == "polars":
        # Pin Polars' internal thread pool so num_threads is apples-to-apples.
        env["POLARS_MAX_THREADS"] = str(num_threads)
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
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{backend} run failed (exit {proc.returncode}):\n{proc.stderr.strip()}"
        )
    last_line = proc.stdout.strip().splitlines()[-1]
    return json.loads(last_line)


def run_backend(
    backend: str,
    scale: Scale,
    source: Path,
    target: Path,
    num_shards: int,
    num_threads: int,
    repeats: int,
) -> RunResult:
    walls: List[float] = []
    peak = 0
    cpu_total = 0.0
    out_bytes = 0
    out_files = 0
    # One warm-up run (discarded) plus `repeats` timed runs.
    for i in range(repeats + 1):
        try:
            metrics = _run_subprocess(backend, source, target, num_shards, num_threads)
        except RuntimeError as exc:
            return RunResult(
                scale=scale.name,
                backend=backend,
                num_shards=num_shards,
                num_threads=num_threads,
                num_rows=scale.num_rows,
                wall_best_s=float("nan"),
                wall_mean_s=float("nan"),
                peak_rss_bytes=0,
                out_bytes=0,
                out_files=0,
                cpu_total_s=0.0,
                error=str(exc),
            )
        if i == 0:
            continue  # warm-up
        walls.append(metrics["wall_s"])
        peak = max(peak, metrics["peak_rss_bytes"])
        cpu_total = max(cpu_total, metrics["cpu_user_s"] + metrics["cpu_sys_s"])
        out_bytes = metrics["out_bytes"]
        out_files = metrics["out_files"]

    return RunResult(
        scale=scale.name,
        backend=backend,
        num_shards=num_shards,
        num_threads=num_threads,
        num_rows=scale.num_rows,
        wall_best_s=min(walls),
        wall_mean_s=statistics.mean(walls),
        peak_rss_bytes=peak,
        out_bytes=out_bytes,
        out_files=out_files,
        cpu_total_s=cpu_total,
    )


def _make_scales(quick: bool) -> List[Scale]:
    ncpu = os.cpu_count() or 4
    if quick:
        return [
            Scale("small", num_subjects=2_000, events_per_subject=20, num_files=4),
            Scale("wide", num_subjects=2_000, events_per_subject=20, num_files=4,
                  num_extra_property_columns=12),
        ]
    return [
        Scale("small", num_subjects=5_000, events_per_subject=20, num_files=8),
        Scale("medium", num_subjects=50_000, events_per_subject=20, num_files=16),
        Scale("large", num_subjects=200_000, events_per_subject=50, num_files=32),
        Scale("wide", num_subjects=50_000, events_per_subject=20, num_files=16,
              num_extra_property_columns=16),
    ]


def _env_info() -> Dict[str, str]:
    info = {
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": str(os.cpu_count()),
        "python": sys.version.split()[0],
        "polars": pl.__version__,
    }
    try:
        import pyarrow

        info["pyarrow"] = pyarrow.__version__
    except Exception:  # noqa: BLE001
        info["pyarrow"] = "unavailable"
    return info


def _write_report(
    results: List[RunResult],
    env: Dict[str, str],
    equivalence: List[str],
) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame([r.__dict__ for r in results])
    df.write_parquet(RESULTS_DIR / "results.parquet")
    df.write_csv(RESULTS_DIR / "results.csv")

    lines: List[str] = []
    lines.append("# meds_etl_cpp benchmark: Polars vs C++\n")
    lines.append("## Environment\n")
    for key, value in env.items():
        lines.append(f"- **{key}**: {value}")
    lines.append("")

    lines.append("## Results\n")
    lines.append(
        "| scale | rows | shards | threads | backend | wall best (s) | "
        "wall mean (s) | peak RSS | out size | out files |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")

    # Pair backends per config for an easy speed/memory ratio readout.
    for r in sorted(results, key=lambda x: (x.scale, x.num_shards, x.num_threads, x.backend)):
        if r.error:
            lines.append(
                f"| {r.scale} | {r.num_rows:,} | {r.num_shards} | {r.num_threads} | "
                f"{r.backend} | ERROR | ERROR | - | - | - |"
            )
            continue
        lines.append(
            f"| {r.scale} | {r.num_rows:,} | {r.num_shards} | {r.num_threads} | "
            f"{r.backend} | {r.wall_best_s:.3f} | {r.wall_mean_s:.3f} | "
            f"{_human_bytes(r.peak_rss_bytes)} | {_human_bytes(r.out_bytes)} | "
            f"{r.out_files} |"
        )
    lines.append("")

    # Ratio summary (polars / cpp) per matched config, when both succeeded.
    lines.append("## Polars / C++ ratios (lower is better for Polars)\n")
    lines.append("| scale | shards | threads | speed ratio | peak-RSS ratio |")
    lines.append("|---|---|---|---|---|")
    by_key: Dict[tuple, Dict[str, RunResult]] = {}
    for r in results:
        by_key.setdefault((r.scale, r.num_shards, r.num_threads), {})[r.backend] = r
    for key, pair in sorted(by_key.items()):
        cpp = pair.get("cpp")
        pol = pair.get("polars")
        if not cpp or not pol or cpp.error or pol.error:
            continue
        speed = pol.wall_best_s / cpp.wall_best_s if cpp.wall_best_s else float("nan")
        mem = (
            pol.peak_rss_bytes / cpp.peak_rss_bytes if cpp.peak_rss_bytes else float("nan")
        )
        lines.append(
            f"| {key[0]} | {key[1]} | {key[2]} | {speed:.2f}x | {mem:.2f}x |"
        )
    lines.append("")

    if equivalence:
        lines.append("## Equivalence checks\n")
        for line in equivalence:
            lines.append(f"- {line}")
        lines.append("")

    _maybe_plot(df)
    if (RESULTS_DIR / "speed.png").exists():
        lines.append("## Plots\n")
        lines.append("![wall-clock](speed.png)")
        lines.append("![peak RSS](memory.png)")
        lines.append("")

    report_path = RESULTS_DIR / "report.md"
    report_path.write_text("\n".join(lines))
    return report_path


def _maybe_plot(df: pl.DataFrame) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001
        return

    # Use the "default config" rows (max shards == num_threads heuristic not
    # reliable); instead plot the per-scale config with the largest thread count.
    ok = df.filter(pl.col("error").is_null())
    if ok.height == 0:
        return

    # Pick, per scale, the config with the most threads (the "headline" config).
    headline = (
        ok.sort(["scale", "num_threads", "num_shards"])
        .group_by("scale", maintain_order=True)
        .last()
        .select("scale", "num_shards", "num_threads")
    )
    keys = {(r["scale"], r["num_shards"], r["num_threads"]) for r in headline.to_dicts()}
    sub = ok.filter(
        pl.struct("scale", "num_shards", "num_threads").map_elements(
            lambda s: (s["scale"], s["num_shards"], s["num_threads"]) in keys,
            return_dtype=pl.Boolean,
        )
    )

    scales = sub.select("scale").unique().to_series().to_list()
    backends = sorted(sub.select("backend").unique().to_series().to_list())

    def _plot(metric: str, ylabel: str, filename: str, transform=lambda x: x) -> None:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(7, 4))
        x = range(len(scales))
        width = 0.35
        for i, backend in enumerate(backends):
            vals = []
            for scale in scales:
                row = sub.filter(
                    (pl.col("scale") == scale) & (pl.col("backend") == backend)
                )
                vals.append(transform(row[metric][0]) if row.height else 0)
            ax.bar([xi + i * width for xi in x], vals, width, label=backend)
        ax.set_xticks([xi + width / 2 for xi in x])
        ax.set_xticklabels(scales)
        ax.set_ylabel(ylabel)
        ax.set_title(ylabel + " by scale (headline config)")
        ax.legend()
        fig.tight_layout()
        fig.savefig(RESULTS_DIR / filename, dpi=120)
        plt.close(fig)

    _plot("wall_best_s", "Wall-clock (s)", "speed.png")
    _plot("peak_rss_bytes", "Peak RSS (MiB)", "memory.png", lambda b: b / (1024 * 1024))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="Small scales, fast run.")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--backends",
        nargs="+",
        default=["polars", "cpp"],
        choices=["polars", "cpp"],
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Also sweep num_shards/num_threads on the medium scale.",
    )
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    ncpu = os.cpu_count() or 4
    scales = _make_scales(args.quick)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    # Build the list of (scale, num_shards, num_threads) configs.
    configs: List[tuple] = []
    for scale in scales:
        configs.append((scale, ncpu, ncpu))  # headline config
    if args.sweep:
        medium = next((s for s in scales if s.name == "medium"), scales[len(scales) // 2])
        for shards in sorted({1, ncpu, 4 * ncpu}):
            for threads in sorted({1, ncpu}):
                if (medium, shards, threads) not in configs:
                    configs.append((medium, shards, threads))

    # Generate input data once per scale.
    sources: Dict[str, Path] = {}
    for scale in scales:
        source = WORK_DIR / f"src_{scale.name}"
        print(f"[gen] {scale.name}: {scale.num_rows:,} rows ...", flush=True)
        t0 = time.perf_counter()
        generate(
            out=source,
            num_subjects=scale.num_subjects,
            events_per_subject=scale.events_per_subject,
            num_files=scale.num_files,
            num_extra_property_columns=scale.num_extra_property_columns,
            string_cardinality=scale.string_cardinality,
            null_time_frac=0.02,
            null_value_frac=0.5,
            seed=args.seed,
        )
        sources[scale.name] = source
        print(f"      done in {time.perf_counter() - t0:.1f}s", flush=True)

    results: List[RunResult] = []
    equivalence: List[str] = []
    for scale, num_shards, num_threads in configs:
        source = sources[scale.name]
        targets: Dict[str, Path] = {}
        for backend in args.backends:
            target = WORK_DIR / f"out_{scale.name}_{num_shards}_{num_threads}_{backend}"
            targets[backend] = target
            print(
                f"[run] {scale.name} shards={num_shards} threads={num_threads} "
                f"backend={backend} ...",
                flush=True,
            )
            res = run_backend(
                backend, scale, source, target, num_shards, num_threads, args.repeats
            )
            results.append(res)
            if res.error:
                print(f"      ERROR: {res.error}", flush=True)
            else:
                print(
                    f"      wall_best={res.wall_best_s:.3f}s "
                    f"peak_rss={_human_bytes(res.peak_rss_bytes)} "
                    f"out={_human_bytes(res.out_bytes)} files={res.out_files}",
                    flush=True,
                )

        if "polars" in targets and "cpp" in targets:
            try:
                check_equivalence(targets["polars"], targets["cpp"])
                msg = (
                    f"OK: {scale.name} shards={num_shards} threads={num_threads} "
                    "(polars == cpp)"
                )
            except AssertionError as exc:
                msg = (
                    f"MISMATCH: {scale.name} shards={num_shards} "
                    f"threads={num_threads}: {exc}"
                )
            print(f"      equivalence: {msg}", flush=True)
            equivalence.append(msg)

    env = _env_info()
    report = _write_report(results, env, equivalence)
    print(f"\nReport written to {report}")


if __name__ == "__main__":
    main()
