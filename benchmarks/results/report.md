# meds_sort benchmark: Polars vs C++

## Environment

- **platform**: macOS-26.5.1-arm64-arm-64bit
- **processor**: arm
- **cpu_count**: 14
- **python**: 3.12.7
- **polars**: 1.35.2
- **pyarrow**: 22.0.0

## Results

| scale | rows | shards | threads | backend | wall best (s) | wall mean (s) | peak RSS | out size | out files |
|---|---|---|---|---|---|---|---|---|---|
| small | 40,000 | 14 | 14 | polars | 0.025 | 0.025 | 125.4 MiB | 602.1 KiB | 14 |
| wide | 40,000 | 14 | 14 | polars | 0.034 | 0.034 | 155.1 MiB | 1.2 MiB | 14 |

## Polars / C++ ratios (lower is better for Polars)

| scale | shards | threads | speed ratio | peak-RSS ratio |
|---|---|---|---|---|

## Plots

![wall-clock](speed.png)
![peak RSS](memory.png)
