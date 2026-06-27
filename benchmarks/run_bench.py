"""Run a single ETL with one backend and report timing + peak memory.

This script performs exactly one ``perform_etl`` invocation and prints a single
JSON line of metrics to stdout. It is meant to be launched as a fresh subprocess
(see ``bench.py``) so that peak RSS, measured via ``getrusage``, reflects only
this one run.

Backends:
  polars  -> meds_etl_cpp.perform_etl (the pure Python/Polars implementation)
  cpp     -> the legacy compiled extension at meds_etl_cpp/_native.*.so
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import resource
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_polars_backend():
    sys.path.insert(0, str(REPO_ROOT))
    from meds_etl_cpp import perform_etl  # noqa: WPS433

    return perform_etl


def _load_cpp_backend():
    # The compiled extension links against pyarrow's Arrow dylibs, so importing
    # pyarrow first ensures they are available for the dynamic loader.
    import pyarrow  # noqa: F401

    candidates = sorted((REPO_ROOT / "meds_etl_cpp").glob("_native*.so"))
    if not candidates:
        raise FileNotFoundError(
            "Could not find the compiled _native extension; the cpp baseline is "
            "unavailable."
        )
    # The pybind11 module's init symbol is PyInit__native, so the import name
    # must be exactly "_native".
    spec = importlib.util.spec_from_file_location("_native", candidates[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.perform_etl


def _peak_rss_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports kilobytes.
    if sys.platform == "darwin":
        return int(rss)
    return int(rss) * 1024


def _output_stats(target: Path) -> tuple[int, int]:
    data_dir = target / "data"
    files = list(data_dir.glob("*.parquet"))
    total = sum(f.stat().st_size for f in files)
    return total, len(files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["polars", "cpp"], required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--num-shards", type=int, required=True)
    parser.add_argument("--num-threads", type=int, required=True)
    args = parser.parse_args()

    if args.backend == "polars":
        perform_etl = _load_polars_backend()
    else:
        perform_etl = _load_cpp_backend()

    if args.target.exists():
        shutil.rmtree(args.target)

    start = time.perf_counter()
    perform_etl(str(args.source), str(args.target), args.num_shards, args.num_threads)
    wall = time.perf_counter() - start

    usage = resource.getrusage(resource.RUSAGE_SELF)
    out_bytes, out_files = _output_stats(args.target)

    print(
        json.dumps(
            {
                "backend": args.backend,
                "num_shards": args.num_shards,
                "num_threads": args.num_threads,
                "wall_s": wall,
                "peak_rss_bytes": _peak_rss_bytes(),
                "cpu_user_s": usage.ru_utime,
                "cpu_sys_s": usage.ru_stime,
                "out_bytes": out_bytes,
                "out_files": out_files,
            }
        )
    )


if __name__ == "__main__":
    main()
