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
"""

from __future__ import annotations

import argparse
import time as _time
from pathlib import Path

import numpy as np
import polars as pl


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
) -> int:
    rng = np.random.default_rng(seed)
    n = num_subjects * events_per_subject

    # subject_id: each subject appears events_per_subject times, then shuffled.
    subject_id = np.repeat(np.arange(num_subjects, dtype=np.int64), events_per_subject)
    rng.shuffle(subject_id)

    # time: microseconds since epoch within a ~10-year window.
    base = np.int64(0)
    span_us = np.int64(10 * 365 * 24 * 3600) * 1_000_000
    time_us = base + (rng.random(n) * float(span_us)).astype(np.int64)
    time_null = rng.random(n) < null_time_frac

    # numeric_value
    numeric_value = rng.normal(0.0, 100.0, size=n).astype(np.float32)
    value_null = rng.random(n) < null_value_frac

    # code vocabulary
    codes = np.array([f"CODE_{i}" for i in range(max(1, string_cardinality))])
    code = codes[rng.integers(0, len(codes), size=n)]

    columns: dict[str, pl.Series] = {
        "subject_id": pl.Series("subject_id", subject_id, dtype=pl.Int64),
        "time": pl.Series("time", time_us, dtype=pl.Int64).cast(pl.Datetime("us")),
        "code": pl.Series("code", code, dtype=pl.String),
        "numeric_value": pl.Series("numeric_value", numeric_value, dtype=pl.Float32),
        "__time_null": pl.Series("__time_null", time_null, dtype=pl.Boolean),
        "__value_null": pl.Series("__value_null", value_null, dtype=pl.Boolean),
    }

    prop_vocab = np.array([f"v{i}" for i in range(max(1, string_cardinality // 4 + 1))])
    for k in range(num_extra_property_columns):
        vals = prop_vocab[rng.integers(0, len(prop_vocab), size=n)]
        columns[f"prop_{k}"] = pl.Series(f"prop_{k}", vals, dtype=pl.String)

    df = pl.DataFrame(columns).with_columns(
        pl.when(pl.col("__time_null")).then(None).otherwise(pl.col("time")).alias("time"),
        pl.when(pl.col("__value_null"))
        .then(None)
        .otherwise(pl.col("numeric_value"))
        .alias("numeric_value"),
    ).drop("__time_null", "__value_null")

    unsorted = out / "unsorted_data"
    unsorted.mkdir(parents=True, exist_ok=True)
    # Clear any previous files.
    for old in unsorted.glob("*.parquet"):
        old.unlink()

    # Split row-wise into num_files chunks.
    chunk = (n + num_files - 1) // num_files
    for i in range(num_files):
        part = df.slice(i * chunk, chunk)
        if part.height == 0:
            continue
        part.write_parquet(unsorted / f"{i}.parquet")

    return n


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
    args = parser.parse_args()

    start = _time.perf_counter()
    n = generate(
        out=args.out,
        num_subjects=args.num_subjects,
        events_per_subject=args.events_per_subject,
        num_files=args.num_files,
        num_extra_property_columns=args.num_extra_property_columns,
        string_cardinality=args.string_cardinality,
        null_time_frac=args.null_time_frac,
        null_value_frac=args.null_value_frac,
        seed=args.seed,
    )
    elapsed = _time.perf_counter() - start
    print(f"Generated {n:,} rows into {args.out}/unsorted_data in {elapsed:.2f}s")


if __name__ == "__main__":
    main()
