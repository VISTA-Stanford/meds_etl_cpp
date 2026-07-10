"""Generate synthetic MEDS Unsorted datasets for benchmarking.

The output is written to ``<out>/unsorted_data/*.parquet`` with a schema that
both the C++ and Polars backends accept verbatim:

    subject_id     Int64
    time           Datetime("us")  (no timezone, a fraction may be null)
    code           String          (drawn from a vocabulary)
    numeric_value  Float32         (a fraction may be null)
    prop_0..k      String          (optional extra "property" columns)

Rows are shuffled and split across files so that a given subject's events are
spread across multiple input files (exercising the shard/join logic).

Memory behaviour
----------------
Data is generated **file by file, chunk by chunk** and streamed to disk with a
PyArrow ``ParquetWriter`` (one row group per chunk). Nothing larger than a
single ``chunk_rows`` batch is ever held in memory, so this can generate
datasets far larger than RAM (hundreds of GB / TB) as long as there is disk.

Two ways to size the dataset:

* ``num_rows`` / ``--num-rows``: an explicit total row count, or
* ``target_uncompressed_bytes`` / ``--target-uncompressed-bytes``: a target
  *uncompressed* (in-memory Arrow) footprint. The generator probes the real
  per-row byte cost and derives the row count. This is the knob to reproduce a
  "~4 TB uncompressed" snapshot.

Subject skew
------------
``subject_skew`` (>0) draws per-subject event weights from a lognormal so a few
subjects (and therefore a few shards) are much larger than average. Skewed shard
sizes are the real worst case for peak memory, since peak RSS is bounded by the
*largest* shard, not the average.
"""

from __future__ import annotations

import argparse
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# Fixed-width portion of each row (subject_id int64 + time us int64 + value f32).
_FIXED_BYTES_PER_ROW = 8 + 8 + 4


@dataclass
class GenStats:
    """Summary of a generated dataset."""

    num_rows: int
    num_files: int
    on_disk_bytes: int
    uncompressed_bytes: int

    @property
    def ratio(self) -> float:
        if self.on_disk_bytes == 0:
            return float("nan")
        return self.uncompressed_bytes / self.on_disk_bytes


def _make_vocab(prefix: str, cardinality: int) -> np.ndarray:
    """Build a vocabulary of python-string objects (fast to index into)."""
    return np.array(
        [f"{prefix}{i}" for i in range(max(1, cardinality))], dtype=object
    )


def _subject_weights(num_subjects: int, skew: float, rng: np.random.Generator):
    """Return a probability vector over subjects, or ``None`` for uniform."""
    if skew <= 0:
        return None
    # Lognormal weights: sigma == skew. Larger skew -> heavier tail -> more
    # lopsided shard sizes.
    weights = rng.lognormal(mean=0.0, sigma=skew, size=num_subjects)
    total = weights.sum()
    if total <= 0:
        return None
    return weights / total


def _make_chunk(
    rng: np.random.Generator,
    size: int,
    num_subjects: int,
    subject_p: Optional[np.ndarray],
    codes: np.ndarray,
    prop_vocab: np.ndarray,
    num_extra_property_columns: int,
    span_us: int,
    null_time_frac: float,
    null_value_frac: float,
) -> pa.Table:
    """Build one Arrow table of ``size`` rows (no data kept beyond this call)."""
    if subject_p is None:
        subject_id = rng.integers(0, num_subjects, size=size, dtype=np.int64)
    else:
        subject_id = rng.choice(num_subjects, size=size, p=subject_p).astype(
            np.int64
        )

    time_us = (rng.random(size) * float(span_us)).astype(np.int64)
    time_mask = rng.random(size) < null_time_frac

    numeric_value = rng.normal(0.0, 100.0, size=size).astype(np.float32)
    value_mask = rng.random(size) < null_value_frac

    code = codes[rng.integers(0, len(codes), size=size)]

    arrays = {
        "subject_id": pa.array(subject_id, type=pa.int64()),
        "time": pa.array(time_us, mask=time_mask, type=pa.int64()).cast(
            pa.timestamp("us")
        ),
        "code": pa.array(code, type=pa.string()),
        "numeric_value": pa.array(
            numeric_value, mask=value_mask, type=pa.float32()
        ),
    }
    for k in range(num_extra_property_columns):
        vals = prop_vocab[rng.integers(0, len(prop_vocab), size=size)]
        arrays[f"prop_{k}"] = pa.array(vals, type=pa.string())

    return pa.table(arrays)


def _probe_bytes_per_row(
    rng: np.random.Generator,
    num_subjects: int,
    subject_p: Optional[np.ndarray],
    codes: np.ndarray,
    prop_vocab: np.ndarray,
    num_extra_property_columns: int,
    span_us: int,
    null_time_frac: float,
    null_value_frac: float,
) -> float:
    """Measure real uncompressed (Arrow) bytes/row with a small probe chunk."""
    probe = min(50_000, max(1_000, num_subjects))
    table = _make_chunk(
        rng,
        probe,
        num_subjects,
        subject_p,
        codes,
        prop_vocab,
        num_extra_property_columns,
        span_us,
        null_time_frac,
        null_value_frac,
    )
    return table.nbytes / table.num_rows


def generate(
    out: Path,
    num_subjects: int,
    events_per_subject: int,
    num_files: int,
    num_extra_property_columns: int,
    string_cardinality: int,
    null_time_frac: float,
    null_value_frac: float,
    seed: int,
    *,
    num_rows: Optional[int] = None,
    target_uncompressed_bytes: Optional[int] = None,
    subject_skew: float = 0.0,
    chunk_rows: int = 2_000_000,
    compression: str = "zstd",
) -> GenStats:
    """Generate a synthetic MEDS Unsorted dataset, streaming to disk.

    ``num_rows`` and ``target_uncompressed_bytes`` (if provided) override the
    default ``num_subjects * events_per_subject`` row count, in that order of
    precedence.
    """
    out = Path(out)
    rng = np.random.default_rng(seed)
    span_us = int(np.int64(10 * 365 * 24 * 3600) * 1_000_000)

    codes = _make_vocab("CODE_", string_cardinality)
    prop_vocab = _make_vocab("v", string_cardinality // 4 + 1)
    subject_p = _subject_weights(num_subjects, subject_skew, rng)

    if num_rows is not None:
        n = int(num_rows)
    elif target_uncompressed_bytes is not None:
        bytes_per_row = _probe_bytes_per_row(
            rng,
            num_subjects,
            subject_p,
            codes,
            prop_vocab,
            num_extra_property_columns,
            span_us,
            null_time_frac,
            null_value_frac,
        )
        n = max(1, int(round(target_uncompressed_bytes / bytes_per_row)))
    else:
        n = num_subjects * events_per_subject

    num_files = max(1, int(num_files))
    chunk_rows = max(1, int(chunk_rows))

    unsorted = out / "unsorted_data"
    unsorted.mkdir(parents=True, exist_ok=True)
    for old in unsorted.glob("*.parquet"):
        old.unlink()

    # Split rows across files as evenly as possible.
    base = n // num_files
    remainder = n % num_files
    file_row_counts = [
        base + (1 if i < remainder else 0) for i in range(num_files)
    ]

    on_disk_bytes = 0
    uncompressed_bytes = 0
    rows_written = 0

    for file_idx, file_rows in enumerate(file_row_counts):
        if file_rows == 0:
            continue
        path = unsorted / f"{file_idx}.parquet"
        writer: Optional[pq.ParquetWriter] = None
        remaining = file_rows
        try:
            while remaining > 0:
                size = min(chunk_rows, remaining)
                table = _make_chunk(
                    rng,
                    size,
                    num_subjects,
                    subject_p,
                    codes,
                    prop_vocab,
                    num_extra_property_columns,
                    span_us,
                    null_time_frac,
                    null_value_frac,
                )
                if writer is None:
                    writer = pq.ParquetWriter(
                        path, table.schema, compression=compression
                    )
                writer.write_table(table)
                uncompressed_bytes += table.nbytes
                rows_written += size
                remaining -= size
                del table
        finally:
            if writer is not None:
                writer.close()
        on_disk_bytes += path.stat().st_size

    return GenStats(
        num_rows=rows_written,
        num_files=sum(1 for c in file_row_counts if c > 0),
        on_disk_bytes=on_disk_bytes,
        uncompressed_bytes=uncompressed_bytes,
    )


def _human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def _parse_bytes(text: str) -> int:
    """Parse a human byte size like ``4TB``, ``512GiB``, ``1_000_000``."""
    text = text.strip().replace("_", "")
    units = {
        "B": 1,
        "KB": 10**3,
        "MB": 10**6,
        "GB": 10**9,
        "TB": 10**12,
        "KIB": 2**10,
        "MIB": 2**20,
        "GIB": 2**30,
        "TIB": 2**40,
    }
    upper = text.upper()
    for suffix in sorted(units, key=len, reverse=True):
        if upper.endswith(suffix):
            num = upper[: -len(suffix)].strip()
            return int(float(num) * units[suffix])
    return int(float(text))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--num-subjects", type=int, default=10_000)
    parser.add_argument("--events-per-subject", type=int, default=100)
    parser.add_argument("--num-files", type=int, default=8)
    parser.add_argument("--num-extra-property-columns", type=int, default=2)
    parser.add_argument("--string-cardinality", type=int, default=2000)
    parser.add_argument("--null-time-frac", type=float, default=0.02)
    parser.add_argument("--null-value-frac", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--num-rows",
        type=int,
        default=None,
        help="Explicit total row count (overrides subjects*events).",
    )
    parser.add_argument(
        "--target-uncompressed-bytes",
        type=_parse_bytes,
        default=None,
        help="Target uncompressed footprint, e.g. 4TB, 512GiB (overrides rows).",
    )
    parser.add_argument(
        "--subject-skew",
        type=float,
        default=0.0,
        help="Lognormal sigma for per-subject event weights (0 = uniform).",
    )
    parser.add_argument(
        "--chunk-rows",
        type=int,
        default=2_000_000,
        help="Rows generated/written per row group (bounds generator memory).",
    )
    parser.add_argument("--compression", type=str, default="zstd")
    args = parser.parse_args()

    start = _time.perf_counter()
    stats = generate(
        out=args.out,
        num_subjects=args.num_subjects,
        events_per_subject=args.events_per_subject,
        num_files=args.num_files,
        num_extra_property_columns=args.num_extra_property_columns,
        string_cardinality=args.string_cardinality,
        null_time_frac=args.null_time_frac,
        null_value_frac=args.null_value_frac,
        seed=args.seed,
        num_rows=args.num_rows,
        target_uncompressed_bytes=args.target_uncompressed_bytes,
        subject_skew=args.subject_skew,
        chunk_rows=args.chunk_rows,
        compression=args.compression,
    )
    elapsed = _time.perf_counter() - start
    print(
        f"Generated {stats.num_rows:,} rows across {stats.num_files} files into "
        f"{args.out}/unsorted_data in {elapsed:.2f}s"
    )
    print(
        f"  on-disk (compressed): {_human_bytes(stats.on_disk_bytes)}  |  "
        f"uncompressed (Arrow): {_human_bytes(stats.uncompressed_bytes)}  |  "
        f"ratio: {stats.ratio:.1f}x"
    )


if __name__ == "__main__":
    main()
