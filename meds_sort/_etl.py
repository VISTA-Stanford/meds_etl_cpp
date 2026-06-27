"""Pure Python/Polars implementation of the MEDS Unsorted -> MEDS sort/shard ETL.

This is the implementation behind ``meds_sort`` (formerly distributed as the
C++/pybind11 package ``meds_etl_cpp``). It exposes a single function,
:func:`perform_etl`, but relies only on Polars (a multithreaded,
larger-than-memory Rust engine) instead of a compiled extension.

Pipeline (mirrors the original two-stage C++ design so peak memory stays bounded
by the largest single shard rather than the whole dataset):

1. Discover ``<source>/unsorted_data/*.parquet`` and unify their schemas.
2. Stage A: stream every row into per-shard intermediate parquet files, sharded
   by ``hash(subject_id) % num_shards`` so each subject lands in exactly one
   shard. No sorting happens here, so memory use is just streaming write buffers.
3. Stage B: sort each shard independently by
   ``(subject_id, time, code, numeric_value, *other_properties)`` and write the
   final ``<target>/data/<shard>.parquet`` files with ZSTD compression.
4. Copy ``<source>/metadata`` to ``<target>/metadata`` if present.
"""

from __future__ import annotations

import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Tuple

import polars as pl

__all__ = ["perform_etl"]

# How many shards Stage B sorts concurrently. Each concurrent shard sort holds a
# full (decompressed) shard in memory, so peak RSS scales roughly with this
# value. Because each individual Polars sort already uses the whole thread pool,
# pushing concurrency up to num_threads oversubscribes the CPU and inflates
# memory for no speed benefit; a small cap is both faster and far leaner. Set the
# MEDS_SORT_SHARD_CONCURRENCY env var to override (e.g. 1 to minimize memory).
_SHARD_CONCURRENCY_ENV = "MEDS_SORT_SHARD_CONCURRENCY"
_DEFAULT_MAX_SHARD_CONCURRENCY = 4

# Columns that are not treated as generic "properties".
_KNOWN_FIELDS = ("subject_id", "time")

# Temporary column used to route rows to shards. Never written to the output.
_SHARD_COL = "__shard"

# Forced output dtypes, matching the original C++ schema.
_SUBJECT_ID_DTYPE = pl.Int64
_TIME_DTYPE = pl.Datetime("us")
_CODE_DTYPE = pl.String
_NUMERIC_VALUE_DTYPE = pl.Float32

# Columns whose dtype is forced/cast (so cross-file dtype differences are fine).
_FORCED_COLUMNS = {"code", "numeric_value"}

_SHARD_DIR_RE = re.compile(r"__shard=(-?\d+)")


def _list_unsorted_files(unsorted_dir: Path) -> List[Path]:
    """Return the parquet files under ``unsorted_data`` in a deterministic order."""
    if not unsorted_dir.exists():
        return []
    files = [
        p
        for p in unsorted_dir.iterdir()
        if p.is_file() and not p.name.startswith(".")
    ]
    return sorted(files)


def _collect_schemas(files: List[Path]) -> Dict[Path, "pl.Schema"]:
    return {f: pl.scan_parquet(f).collect_schema() for f in files}


def _unify_schema(
    schemas: Dict[Path, "pl.Schema"],
) -> List[Tuple[str, pl.DataType]]:
    """Compute the union output schema and column order across all input files.

    The output column order matches the C++ backend:
    ``subject_id, time, code, numeric_value, <other properties, alphabetical>``.

    ``code`` and ``numeric_value`` always appear in the output (filled with nulls
    if absent from every input) and have forced dtypes. Any other property column
    that appears with conflicting dtypes across files is an error, mirroring the
    C++ ``get_properties_fields`` conflict check.
    """
    # name -> {dtype-repr: dtype} so we can detect conflicting dtypes per column.
    seen: Dict[str, Dict[str, pl.DataType]] = {}

    for schema in schemas.values():
        for name, dtype in schema.items():
            if name in _KNOWN_FIELDS:
                continue
            if name == "value":
                raise ValueError(
                    "meds_sort does not support generic 'value' fields; "
                    "MEDS Unsorted data should use 'code'/'numeric_value'."
                )
            seen.setdefault(name, {})[str(dtype)] = dtype

    other_props: Dict[str, pl.DataType] = {}
    conflicts: List[str] = []
    for name, variants in seen.items():
        if name in _FORCED_COLUMNS:
            continue
        if len(variants) > 1:
            conflicts.append(f"{name}: {sorted(variants)}")
        else:
            other_props[name] = next(iter(variants.values()))

    if conflicts:
        raise ValueError(
            "Got conflicting types across input files for column(s): "
            + "; ".join(conflicts)
        )

    ordered: List[Tuple[str, pl.DataType]] = [
        ("subject_id", _SUBJECT_ID_DTYPE),
        ("time", _TIME_DTYPE),
        ("code", _CODE_DTYPE),
        ("numeric_value", _NUMERIC_VALUE_DTYPE),
    ]
    for name in sorted(other_props):
        ordered.append((name, other_props[name]))
    return ordered


def _build_combined_lf(
    schemas: Dict[Path, "pl.Schema"],
    ordered_cols: List[Tuple[str, pl.DataType]],
) -> pl.LazyFrame:
    """Build one lazy frame that unions all files, normalized to ``ordered_cols``.

    Missing columns are inserted as typed nulls and present columns are cast to
    the unified dtype, so the per-file frames are schema-compatible for concat.
    """
    lfs: List[pl.LazyFrame] = []
    for file, schema in schemas.items():
        lf = pl.scan_parquet(file)
        exprs = []
        for name, dtype in ordered_cols:
            if name in schema:
                if name == "time":
                    src_dtype = schema[name]
                    if (
                        isinstance(src_dtype, pl.Datetime)
                        and src_dtype.time_zone is not None
                    ):
                        raise ValueError(
                            "The 'time' column must not have a timezone, but "
                            f"file {file} has {src_dtype}."
                        )
                exprs.append(pl.col(name).cast(dtype).alias(name))
            else:
                exprs.append(pl.lit(None, dtype=dtype).alias(name))
        lfs.append(lf.select(exprs))

    if len(lfs) == 1:
        return lfs[0]
    return pl.concat(lfs, how="vertical")


def perform_etl(
    source_directory: str,
    target_directory: str,
    num_shards: int,
    num_threads: int,
) -> None:
    """Convert a MEDS Unsorted dataset into a sorted, sharded MEDS dataset.

    Parameters
    ----------
    source_directory:
        Directory containing ``unsorted_data/*.parquet`` and optionally a
        ``metadata`` folder.
    target_directory:
        Output directory. ``data/<shard>.parquet`` files are written here, along
        with a copy of ``metadata`` if present in the source.
    num_shards:
        Number of shards. Subjects are distributed across shards by
        ``hash(subject_id) % num_shards`` so each subject lives in exactly one
        shard. Controls peak memory (memory is bounded by the largest shard).
    num_threads:
        Upper bound on the number of shards sorted concurrently in Stage B
        (each Polars sort already uses the whole thread pool internally). The
        effective concurrency is capped at a small default to avoid CPU
        oversubscription and excessive peak memory; override with the
        ``MEDS_SORT_SHARD_CONCURRENCY`` environment variable (set it to ``1`` to
        minimize memory).
    """
    num_shards = int(num_shards)
    num_threads = int(num_threads)
    if num_shards < 1:
        raise ValueError(f"num_shards must be >= 1, got {num_shards}")
    if num_threads < 1:
        num_threads = 1

    source = Path(source_directory)
    target = Path(target_directory)
    target.mkdir(parents=True, exist_ok=True)

    # Copy metadata (recursively) if present.
    src_metadata = source / "metadata"
    if src_metadata.exists():
        dst_metadata = target / "metadata"
        shutil.copytree(src_metadata, dst_metadata, dirs_exist_ok=True)

    data_dir = target / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    files = _list_unsorted_files(source / "unsorted_data")
    if not files:
        return

    schemas = _collect_schemas(files)
    ordered_cols = _unify_schema(schemas)
    sort_cols = [name for name, _ in ordered_cols]

    combined = _build_combined_lf(schemas, ordered_cols).with_columns(
        (pl.col("subject_id").hash() % num_shards).alias(_SHARD_COL)
    )

    shard_tmp = target / "_shard_tmp"
    if shard_tmp.exists():
        shutil.rmtree(shard_tmp)

    # Stage A: stream rows into per-shard intermediate files (no sorting).
    combined.sink_parquet(
        pl.PartitionByKey(str(shard_tmp), by=_SHARD_COL, include_key=False),
        mkdir=True,
    )

    # Stage B: sort each shard independently and write the final parquet files.
    shard_dirs = sorted(shard_tmp.glob("__shard=*"))

    def _sort_shard(shard_dir: Path) -> None:
        match = _SHARD_DIR_RE.search(shard_dir.name)
        shard_id = match.group(1) if match else shard_dir.name
        out_path = data_dir / f"{shard_id}.parquet"
        (
            pl.scan_parquet(str(shard_dir / "*.parquet"))
            .sort(sort_cols)
            .sink_parquet(out_path, compression="zstd")
        )

    if shard_dirs:
        env_concurrency = os.environ.get(_SHARD_CONCURRENCY_ENV)
        if env_concurrency:
            concurrency = max(1, int(env_concurrency))
        else:
            concurrency = min(num_threads, _DEFAULT_MAX_SHARD_CONCURRENCY)
        workers = max(1, min(concurrency, len(shard_dirs)))
        if workers == 1:
            for shard_dir in shard_dirs:
                _sort_shard(shard_dir)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                list(executor.map(_sort_shard, shard_dirs))

    shutil.rmtree(shard_tmp, ignore_errors=True)
