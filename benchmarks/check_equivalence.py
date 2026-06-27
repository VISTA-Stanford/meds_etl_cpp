"""Check that two ETL output directories are semantically equivalent.

The C++ and Polars backends are not expected to produce byte-identical shards:
they use different hash functions (so a given subject may land in a different
shard file) and different tie-breaking for rows that share a sort key. This
checker therefore compares the *content* independent of shard placement:

1. For each backend, every subject must appear in exactly one shard file.
2. Each shard file must be ordered by ``(subject_id, time)`` -- the only
   ordering MEDS actually requires. (The two backends legitimately break ties
   among equal ``(subject_id, time)`` rows differently: the C++ backend sorts by
   the raw encoded record bytes, the Polars backend by the remaining columns.)
3. Concatenating all shards and sorting by the full row gives identical frames,
   i.e. the two backends emit the same multiset of rows.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List

import polars as pl

_LEADING = ["subject_id", "time", "code", "numeric_value"]


def _shard_files(target: Path) -> List[Path]:
    return sorted((target / "data").glob("*.parquet"))


def _sort_key(df: pl.DataFrame) -> List[str]:
    leading = [c for c in _LEADING if c in df.columns]
    rest = sorted(c for c in df.columns if c not in leading)
    return leading + rest


def _check_single_backend(target: Path) -> pl.DataFrame:
    files = _shard_files(target)
    if not files:
        raise AssertionError(f"No output parquet files found in {target}/data")

    subject_to_shard: dict = {}
    frames = []
    primary_key = ["subject_id", "time"]
    for idx, f in enumerate(files):
        df = pl.read_parquet(f)
        # Project to the key columns so that rows sharing a key are identical;
        # a re-sort then equals the original iff the keys are non-decreasing.
        keys = df.select(primary_key)
        if not keys.equals(keys.sort(primary_key)):
            raise AssertionError(f"Shard {f} is not ordered by {primary_key}")
        for subject in df["subject_id"].unique().to_list():
            if subject in subject_to_shard:
                raise AssertionError(
                    f"Subject {subject} appears in multiple shards "
                    f"({subject_to_shard[subject]} and {f})"
                )
            subject_to_shard[subject] = idx
        frames.append(df)

    return pl.concat(frames, how="vertical")


def check(target_a: Path, target_b: Path) -> None:
    frame_a = _check_single_backend(target_a)
    frame_b = _check_single_backend(target_b)

    # Align columns (both should have the same schema/column order already).
    cols = _sort_key(frame_a)
    if set(frame_a.columns) != set(frame_b.columns):
        raise AssertionError(
            f"Column mismatch: {sorted(frame_a.columns)} vs {sorted(frame_b.columns)}"
        )

    canon_a = frame_a.select(cols).sort(cols)
    canon_b = frame_b.select(cols).sort(cols)

    if canon_a.height != canon_b.height:
        raise AssertionError(
            f"Row count mismatch: {canon_a.height} vs {canon_b.height}"
        )
    if not canon_a.equals(canon_b):
        raise AssertionError("Output content differs between the two backends")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target_a", type=Path)
    parser.add_argument("target_b", type=Path)
    args = parser.parse_args()
    check(args.target_a, args.target_b)
    print(f"EQUIVALENT: {args.target_a} == {args.target_b}")


if __name__ == "__main__":
    main()
