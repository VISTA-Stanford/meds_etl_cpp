"""Run a single ETL with one backend and report timing + memory.

This script performs exactly one ``perform_etl`` invocation and prints a single
JSON line of metrics to stdout. It is meant to be launched as a fresh subprocess
(see ``bench.py`` / ``stress.py``) so that peak RSS reflects only this one run.

Metrics collected
------------------
* wall time,
* peak RSS (authoritative, via ``getrusage``),
* an RSS time-series sampled by a background thread (Linux ``/proc`` or psutil),
* the wall-time at which Stage B started (first file in ``<target>/data``), so
  peak RSS can be attributed to Stage A vs Stage B,
* CPU user/sys time and output size.

Memory-cap stress mode
----------------------
``--mem-limit-bytes`` installs an ``RLIMIT_AS`` (address-space) cap before the
run. If the ETL exceeds it, the run reports ``status="oom"`` (either because a
Python ``MemoryError`` was caught, or because the process was killed and the
orchestrator observed a non-zero exit). ``RLIMIT_AS`` is portable but blunt
(it caps virtual, not resident, memory); on Linux a cgroup v2 ``MemoryMax``
wrapper (e.g. ``systemd-run --scope -p MemoryMax=...``) is a more faithful cap.

Backends
--------
  polars  -> meds_sort.perform_etl (the pure Python/Polars implementation)
  cpp     -> the legacy compiled extension at meds_etl_cpp/_native.*.so
            (only available if a built extension is present; it was removed
            when this package was reimplemented in pure Python -- build it from
            the ``main`` branch's ``native/`` and drop the .so in place)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import resource
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# Keep the emitted RSS series small regardless of run length.
_MAX_SERIES_POINTS = 400


def _load_polars_backend():
    sys.path.insert(0, str(REPO_ROOT))
    from meds_sort import perform_etl  # noqa: WPS433

    return perform_etl


def _load_cpp_backend():
    # The compiled extension links against pyarrow's Arrow dylibs, so importing
    # pyarrow first ensures they are available for the dynamic loader.
    import pyarrow  # noqa: F401

    # Accept either historical name: the `main` branch's pybind module is
    # `meds_etl_cpp` (PyInit_meds_etl_cpp), a later revision renamed it to
    # `_native` (PyInit__native). build_cpp.sh installs `meds_etl_cpp.so`.
    candidates = sorted(
        list((REPO_ROOT / "meds_etl_cpp").glob("_native*.so"))
        + list((REPO_ROOT / "meds_etl_cpp").glob("meds_etl_cpp*.so"))
    )
    if not candidates:
        raise FileNotFoundError(
            "Could not find a compiled C++ extension; the cpp baseline is "
            "unavailable. Build it with benchmarks/build_cpp.sh (which compiles "
            "the 'main' branch's native/ tree) and place the .so in meds_etl_cpp/."
        )
    so_path = candidates[0]
    # The import name must match the module's PyInit_<name> symbol, which the
    # pybind_extension names after the file stem (before the first dot).
    module_name = so_path.name.split(".")[0]
    spec = importlib.util.spec_from_file_location(module_name, so_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.perform_etl


def _getrusage_peak_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports kilobytes.
    if sys.platform == "darwin":
        return int(rss)
    return int(rss) * 1024


def _rss_now() -> Optional[int]:
    """Best-effort instantaneous resident set size in bytes."""
    if sys.platform == "linux":
        try:
            with open("/proc/self/statm") as handle:
                resident_pages = int(handle.read().split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE")
        except Exception:  # noqa: BLE001
            return None
    try:
        import psutil  # noqa: WPS433

        return int(psutil.Process().memory_info().rss)
    except Exception:  # noqa: BLE001
        return None


class MemorySampler:
    """Background thread sampling RSS and the Stage A -> Stage B transition."""

    def __init__(self, data_dir: Path, interval_s: float = 0.05) -> None:
        self._data_dir = data_dir
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._t0 = 0.0
        self.samples: List[Tuple[float, int]] = []
        self.stage_b_start_s: Optional[float] = None

    def _stage_b_started(self) -> bool:
        try:
            return any(self._data_dir.glob("*.parquet"))
        except Exception:  # noqa: BLE001
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.perf_counter() - self._t0
            rss = _rss_now()
            if rss is not None:
                self.samples.append((now, rss))
            if self.stage_b_start_s is None and self._stage_b_started():
                self.stage_b_start_s = now
            self._stop.wait(self._interval)

    def __enter__(self) -> "MemorySampler":
        self._t0 = time.perf_counter()
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def sampled_peak(self) -> Optional[int]:
        if not self.samples:
            return None
        return max(rss for _, rss in self.samples)

    def downsampled(self) -> List[Tuple[float, int]]:
        if len(self.samples) <= _MAX_SERIES_POINTS:
            return self.samples
        step = len(self.samples) / _MAX_SERIES_POINTS
        picked = [self.samples[int(i * step)] for i in range(_MAX_SERIES_POINTS)]
        # Always include the actual peak sample so plots show the spike.
        peak_sample = max(self.samples, key=lambda s: s[1])
        if peak_sample not in picked:
            picked.append(peak_sample)
            picked.sort(key=lambda s: s[0])
        return picked

    def peak_before(self, marker_s: Optional[float]) -> Optional[int]:
        if marker_s is None:
            return None
        vals = [rss for t, rss in self.samples if t <= marker_s]
        return max(vals) if vals else None

    def peak_after(self, marker_s: Optional[float]) -> Optional[int]:
        if marker_s is None:
            return None
        vals = [rss for t, rss in self.samples if t > marker_s]
        return max(vals) if vals else None


def _output_stats(target: Path) -> Tuple[int, int, int]:
    data_dir = target / "data"
    files = list(data_dir.glob("*.parquet"))
    sizes = [f.stat().st_size for f in files]
    total = sum(sizes)
    largest = max(sizes) if sizes else 0
    return total, len(files), largest


def _apply_mem_limit(limit_bytes: int) -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    new_hard = limit_bytes if hard == resource.RLIM_INFINITY else min(limit_bytes, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, new_hard))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["polars", "cpp"], required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--num-shards", type=int, required=True)
    parser.add_argument("--num-threads", type=int, required=True)
    parser.add_argument(
        "--mem-limit-bytes",
        type=int,
        default=0,
        help="If >0, install an RLIMIT_AS cap; report status=oom if exceeded.",
    )
    parser.add_argument(
        "--sample-interval-s",
        type=float,
        default=0.05,
        help="RSS sampling interval for the background sampler.",
    )
    args = parser.parse_args()

    if args.backend == "polars":
        perform_etl = _load_polars_backend()
    else:
        perform_etl = _load_cpp_backend()

    if args.target.exists():
        shutil.rmtree(args.target)
    data_dir = args.target / "data"

    # Install the memory cap only around the actual ETL work (after imports, so
    # loading libraries doesn't spuriously trip the limit).
    if args.mem_limit_bytes > 0:
        _apply_mem_limit(args.mem_limit_bytes)

    status = "ok"
    error: Optional[str] = None
    start = time.perf_counter()
    with MemorySampler(data_dir, interval_s=args.sample_interval_s) as sampler:
        try:
            perform_etl(
                str(args.source),
                str(args.target),
                args.num_shards,
                args.num_threads,
            )
        except MemoryError as exc:
            status = "oom"
            error = f"MemoryError: {exc}"
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
    wall = time.perf_counter() - start

    usage = resource.getrusage(resource.RUSAGE_SELF)
    peak_rss = _getrusage_peak_bytes()
    sampled_peak = sampler.sampled_peak()
    if sampled_peak is not None:
        peak_rss = max(peak_rss, sampled_peak)

    out_bytes, out_files, out_max_file_bytes = (
        (0, 0, 0) if status != "ok" else _output_stats(args.target)
    )

    print(
        json.dumps(
            {
                "backend": args.backend,
                "num_shards": args.num_shards,
                "num_threads": args.num_threads,
                "status": status,
                "error": error,
                "mem_limit_bytes": args.mem_limit_bytes,
                "wall_s": wall,
                "peak_rss_bytes": peak_rss,
                "peak_rss_stage_a_bytes": sampler.peak_before(sampler.stage_b_start_s),
                "peak_rss_stage_b_bytes": sampler.peak_after(sampler.stage_b_start_s),
                "stage_b_start_s": sampler.stage_b_start_s,
                "cpu_user_s": usage.ru_utime,
                "cpu_sys_s": usage.ru_stime,
                "out_bytes": out_bytes,
                "out_files": out_files,
                "out_max_file_bytes": out_max_file_bytes,
                "rss_series": sampler.downsampled(),
            }
        )
    )


if __name__ == "__main__":
    main()
