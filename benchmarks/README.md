# meds_sort benchmarks

Two harnesses live here:

- **`bench.py`** — the original correctness + speed sweep at small scales.
- **`stress.py`** — a memory-focused stress suite that drives the ETL toward a
  TB-scale snapshot (e.g. ~4 TB uncompressed / ~0.5 TB compressed), measures
  peak and time-series RSS, sweeps the memory knobs, optionally enforces a
  memory budget, and extrapolates to the target scale.

All of these load native libraries (numpy, pyarrow, and optionally the C++
extension), so run them with full permissions / outside any syscall sandbox.

## The memory model

Peak RSS is driven by two competing terms:

- **Stage B** (per-shard sort): each concurrently-sorted shard holds a full,
  decompressed shard in memory, so peak grows with
  `MEDS_SORT_SHARD_CONCURRENCY x largest_shard_size`. Larger `num_shards` ->
  smaller shards -> lower Stage B peak.
- **Stage A** (partitioned `sink_parquet`): one open writer per shard, so peak
  *grows* with `num_shards`.

Because they pull in opposite directions there is a memory-optimal shard count.
`stress.py` fits a two-term model to your measurements and reports it.

### Knobs

| Knob | Effect |
|---|---|
| `num_shards` | Primary lever: more shards shrink Stage B, but grow Stage A. |
| `MEDS_SORT_SHARD_CONCURRENCY` | Concurrent shard sorts; set `1` to minimize peak. |
| `POLARS_MAX_THREADS` | Polars thread pool. |
| `POLARS_TEMP_DIR` | Where Polars spills, if it does. |

## Generating data (`gen_data.py`)

Streams data file-by-file, chunk-by-chunk, so it can create datasets far larger
than RAM. Size it by target uncompressed footprint or explicit rows, and add
subject skew (uneven shard sizes are the real worst case for peak memory):

```bash
python benchmarks/gen_data.py /data/snap \
  --target-uncompressed-bytes 4TB --subject-skew 1.5 \
  --num-subjects 20000000 --num-files 256 --string-cardinality 512
```

To reproduce a ~8:1 (4 TB : 0.5 TB) compression ratio, lower
`--string-cardinality` (more repetition compresses better) and check the printed
`ratio` line, adjusting until it matches your real snapshot.

## Single instrumented run (`run_bench.py`)

Runs one ETL in-process and prints one JSON line with wall time, authoritative
peak RSS (`getrusage`), an RSS time series, and Stage A vs Stage B peak
attribution. Supports `--mem-limit-bytes` (an `RLIMIT_AS` cap; see caveat below).

```bash
python benchmarks/run_bench.py --backend polars \
  --source /data/snap --target /data/out --num-shards 128 --num-threads 32
```

## Stress suite (`stress.py`)

```bash
# Scaling ladder (peak RSS vs dataset size):
python benchmarks/stress.py --scaling --sizes 4GiB,16GiB,64GiB

# Knob sweep (peak RSS vs shards x concurrency x threads):
python benchmarks/stress.py --knob-sweep --knob-size 32GiB

# Find the smallest num_shards that fits a RAM budget, then extrapolate to 4 TB:
python benchmarks/stress.py --scaling --knob-sweep \
  --budget-bytes 64GiB --target-uncompressed-bytes 4TB

# Actually run the full target (needs the disk + RAM):
python benchmarks/stress.py --full-scale --target-uncompressed-bytes 4TB --num-shards 256
```

Outputs land in `benchmarks/results/`: `stress_report.md`, `results.csv`,
`results.parquet`, and (if matplotlib is installed) `memory_scaling.png`,
`memory_vs_shards.png`, `rss_timeseries.png`.

### Memory caps: RLIMIT_AS vs cgroups

`--mem-limit-bytes` / `--verify-cap` install an `RLIMIT_AS` cap. This limits
*virtual* address space, which Arrow/Polars over-reserve well beyond actual RSS
(especially on macOS), so it is only an advisory probe. For a **faithful RSS
cap on Linux**, run inside a cgroup:

```bash
systemd-run --user --scope -p MemoryMax=64G \
  python benchmarks/run_bench.py --backend polars \
  --source /data/snap --target /data/out --num-shards 256 --num-threads 32
```

## Head-to-head with the C++ backend

The C++ backend was removed from this branch; build it from `main` first:

```bash
benchmarks/build_cpp.sh          # compiles main's native/ into meds_etl_cpp/meds_etl_cpp.so
python benchmarks/stress.py --scaling --backends polars cpp
```

Note: the C++ backend has no shard-sort concurrency cap (it sorts up to
`num_threads` shards at once), so at matched configs Polars with
`MEDS_SORT_SHARD_CONCURRENCY=1` can show a lower peak.

## Equivalence

`check_equivalence.py check(...)` fully compares two outputs (small scales).
`check_scalable(...)` is a memory-safe sampled check for TB-scale outputs (row
counts, single-shard-per-subject invariant, and content/ordering on a subject
subset). `stress.py` uses the scalable check automatically when both backends
run.
