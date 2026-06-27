# meds_etl_cpp

Backend for the MEDS Unsorted -> MEDS sort/shard stage used by
[meds_etl](https://github.com/Medical-Event-Data-Standard/meds_etl).

> **Note:** Despite the name, this package is now implemented in **pure
> Python/Polars**. The original C++/pybind11 extension has been removed. The
> public API and output are unchanged, so it remains a drop-in dependency for
> `meds_etl[cpp]` while no longer requiring a native build toolchain (no Bazel,
> no Arrow C++, no compiler). This repository is slated for deprecation in favor
> of folding this logic directly into `meds_etl`.

## What it does

`perform_etl` converts a *MEDS Unsorted* dataset into a sorted, sharded *MEDS*
dataset:

- Reads `<source>/unsorted_data/*.parquet` (columns: `subject_id`, `time`,
  `code`, `numeric_value`, plus arbitrary "property" columns; schemas may differ
  across files and are unioned).
- Distributes subjects across `num_shards` shards via `hash(subject_id)` so each
  subject lands in exactly one shard.
- Sorts each shard by `(subject_id, time, ...)` and writes
  `<target>/data/<shard>.parquet` with ZSTD compression.
- Copies `<source>/metadata` to `<target>/metadata` if present.

Memory is bounded by the largest single shard (each shard is sorted
independently), so the usual guidance still applies: use roughly as many shards
as you have CPUs to keep peak memory in check.

## Installation

```bash
pip install meds_etl_cpp
# or, for local development:
pip install -e ".[test]"
```

## Requirements

- Python >= 3.10
- Polars >= 1.10

## Usage

```python
import meds_etl_cpp

meds_etl_cpp.perform_etl(
    source_directory="meds_unsorted",
    target_directory="meds",
    num_shards=16,
    num_threads=16,
)
```

## Compatibility notes vs. the old C++ backend

Output is *semantically* equivalent to the former C++ implementation, not
byte-identical:

- The shard a given subject lands in may differ (a different hash function is
  used), but every subject still lives in exactly one shard.
- Rows that share the same `(subject_id, time)` may be ordered differently (the
  C++ backend tie-broke on raw encoded bytes; this one tie-breaks on the
  remaining columns). MEDS only requires ordering by `(subject_id, time)`.

The benchmark harness verifies that both backends emit the same multiset of rows
for every configuration.

## Benchmarks

`benchmarks/` contains a harness that compares this implementation against the
legacy C++ extension (when a built `_native` extension is available) on
synthetic data, measuring wall-clock time and peak RSS and verifying output
equivalence.

```bash
python benchmarks/bench.py --quick            # fast smoke run
python benchmarks/bench.py --repeats 3 --sweep # fuller sweep, writes benchmarks/results/report.md
```

On a 14-core Apple Silicon machine (Polars 1.35, pyarrow 22), the Polars
implementation was **faster than the C++ backend in every configuration** while
keeping peak memory in the same ballpark:

| scale | rows | shards/threads | Polars wall | C++ wall | speedup | Polars RSS | C++ RSS |
|---|---|---|---|---|---|---|---|
| small | 100K | 14/14 | 0.03s | 0.04s | ~1.2x | 159 MiB | 119 MiB |
| medium | 1M | 14/14 | 0.09s | 0.31s | ~3.4x | 464 MiB | 332 MiB |
| large | 10M | 14/14 | 0.58s | 1.80s | ~3.1x | 2.2 GiB | 1.8 GiB |
| wide (16 props) | 1M | 14/14 | 0.17s | 0.52s | ~3.1x | 1.2 GiB | 601 MiB |

All equivalence checks pass. See `benchmarks/results/report.md` for the full
sweep (including `num_shards`/`num_threads` scaling) and plots.

## Documentation

See the [meds_etl repository](https://github.com/Medical-Event-Data-Standard/meds_etl).
