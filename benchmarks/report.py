"""Reporting for the memory stress benchmarks (``stress.py``).

Writes ``results.csv`` / ``results.parquet`` plus a markdown report with:

* a results table (peak RSS, Stage A vs Stage B peaks, wall time, output size),
* a peak-RSS-vs-dataset-size curve (scaling ladder),
* a peak-RSS-vs-num_shards curve (knob sweep),
* RSS time-series plots for representative runs,
* the extrapolation to the target (e.g. 4 TB) and a recommended production
  ``(num_shards, MEDS_SORT_SHARD_CONCURRENCY)``,
* the budget-search outcome and equivalence results.

Plots are only produced if ``matplotlib`` is installed; everything else works
without it.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Dict, List, Optional

import polars as pl

# Columns dropped from the flat tables (the RSS series is kept only for plots).
_NON_TABULAR = {"rss_series"}


def _human_bytes(n: float) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


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


def write_memory_report(
    results_dir: Path,
    results: List[Dict],
    equivalence: Optional[List[str]] = None,
    extrapolation: Optional[Dict] = None,
    budget_recommendation: Optional[Dict] = None,
) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    equivalence = equivalence or []

    tabular = [{k: v for k, v in r.items() if k not in _NON_TABULAR} for r in results]
    if tabular:
        df = pl.DataFrame(tabular)
        df.write_parquet(results_dir / "results.parquet")
        df.write_csv(results_dir / "results.csv")

    lines: List[str] = []
    lines.append("# meds_sort memory stress benchmark\n")

    lines.append("## Environment\n")
    for key, value in _env_info().items():
        lines.append(f"- **{key}**: {value}")
    lines.append("")

    lines.append("## Runs\n")
    lines.append(
        "| label | backend | uncompressed | shards | threads | conc | status | "
        "wall best (s) | peak RSS | peak A | peak B | out size | files |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(results, key=lambda x: (x["label"], x["backend"], x["num_shards"], x["concurrency"])):
        wall = f"{r['wall_best_s']:.2f}" if r["status"] == "ok" else "-"
        lines.append(
            f"| {r['label']} | {r['backend']} | {_human_bytes(r['uncompressed_bytes'])} | "
            f"{r['num_shards']} | {r['num_threads']} | {r['concurrency']} | {r['status']} | "
            f"{wall} | {_human_bytes(r['peak_rss_bytes'])} | "
            f"{_human_bytes(r['peak_rss_stage_a_bytes'])} | "
            f"{_human_bytes(r['peak_rss_stage_b_bytes'])} | "
            f"{_human_bytes(r['out_bytes'])} | {r['out_files']} |"
        )
    lines.append("")

    # Polars / C++ ratios where both ran on the same config.
    ratio_lines = _ratio_section(results)
    if ratio_lines:
        lines.extend(ratio_lines)

    if extrapolation:
        lines.extend(_extrapolation_section(extrapolation))

    if budget_recommendation:
        lines.extend(_budget_section(budget_recommendation))

    if equivalence:
        lines.append("## Equivalence checks\n")
        for line in equivalence:
            lines.append(f"- {line}")
        lines.append("")

    plots = _maybe_plot(results_dir, results)
    if plots:
        lines.append("## Plots\n")
        for caption, filename in plots:
            lines.append(f"### {caption}\n")
            lines.append(f"![{caption}]({filename})\n")

    report_path = results_dir / "stress_report.md"
    report_path.write_text("\n".join(lines))
    return report_path


def _ratio_section(results: List[Dict]) -> List[str]:
    by_key: Dict[tuple, Dict[str, Dict]] = {}
    for r in results:
        key = (r["label"], r["num_shards"], r["num_threads"])
        by_key.setdefault(key, {})[r["backend"]] = r
    rows = []
    for key, pair in sorted(by_key.items()):
        cpp = pair.get("cpp")
        pol = pair.get("polars")
        if not cpp or not pol or cpp["status"] != "ok" or pol["status"] != "ok":
            continue
        speed = pol["wall_best_s"] / cpp["wall_best_s"] if cpp["wall_best_s"] else float("nan")
        mem = pol["peak_rss_bytes"] / cpp["peak_rss_bytes"] if cpp["peak_rss_bytes"] else float("nan")
        rows.append(f"| {key[0]} | {key[1]} | {key[2]} | {speed:.2f}x | {mem:.2f}x |")
    if not rows:
        return []
    out = ["## Polars / C++ ratios (lower is better for Polars)\n"]
    out.append("| label | shards | threads | speed ratio | peak-RSS ratio |")
    out.append("|---|---|---|---|---|")
    out.extend(rows)
    out.append("")
    out.append(
        "> Note: the C++ backend has no shard-sort concurrency cap (it sorts up "
        "to num_threads shards at once), whereas Polars here uses "
        "MEDS_SORT_SHARD_CONCURRENCY. Matched-config peak RSS can therefore favor "
        "Polars when concurrency is capped.\n"
    )
    return out


def _extrapolation_section(extrapolation: Dict) -> List[str]:
    model = extrapolation["model"]
    out = ["## Extrapolation to target scale\n"]
    out.append(
        f"Fitted two-term model (n={model['n_points']}, R^2={model['r2']:.3f}):\n"
    )
    out.append(
        "```\n"
        f"peak_rss ~= {_human_bytes(model['intercept_bytes'])}\n"
        f"           + {model['coef_shard_mem']:.3f} * concurrency * largest_shard_bytes   (Stage B)\n"
        f"           + {_human_bytes(model['coef_num_shards'])} * num_shards               (Stage A open writers)\n"
        "```\n"
    )
    out.append(
        f"Observed skew factor {model['skew_factor']:.2f} (largest shard vs uniform). "
        "The two terms trade off, so there is a memory-optimal shard count.\n"
    )
    rec = extrapolation["recommended_num_shards"]
    predicted = extrapolation.get("predicted_peak_bytes")
    feasible = extrapolation.get("feasible")
    out.append(
        "| target uncompressed | RAM budget | recommended shards | predicted peak | fits budget | concurrency |"
    )
    out.append("|---|---|---|---|---|---|")
    out.append(
        f"| {_human_bytes(extrapolation['target_uncompressed_bytes'])} | "
        f"{_human_bytes(extrapolation['budget_bytes'])} | "
        f"{rec if rec is not None else 'n/a'} | "
        f"{_human_bytes(predicted) if predicted is not None else 'n/a'} | "
        f"{'yes' if feasible else 'NO (raise budget or RAM)'} | "
        f"{extrapolation['recommended_concurrency']} |"
    )
    out.append("")
    if rec is not None:
        verdict = (
            "should fit"
            if feasible
            else "does NOT fit -- the memory-optimal config still exceeds the budget"
        )
        out.append(
            f"> Memory-optimal config for ~{_human_bytes(extrapolation['target_uncompressed_bytes'])}: "
            f"`num_shards={rec}`, `MEDS_SORT_SHARD_CONCURRENCY=1` -> predicted peak "
            f"{_human_bytes(predicted) if predicted is not None else 'n/a'} ({verdict}).\n"
        )
    out.append(
        "> Caveat: extrapolation assumes the fitted linear terms hold at the target "
        "scale. Validate with `--full-scale` (or a larger `--sizes` ladder) before "
        "relying on it in production.\n"
    )
    return out


def _budget_section(budget: Dict) -> List[str]:
    out = ["## Budget search\n"]
    chosen = budget.get("chosen_num_shards")
    if chosen is None:
        out.append(
            f"- No tested shard count kept peak RSS under "
            f"{_human_bytes(budget['budget_bytes'])}. {budget.get('note', '')}\n"
        )
        return out
    out.append(
        f"- Smallest num_shards keeping measured peak RSS under "
        f"{_human_bytes(budget['budget_bytes'])}: **{chosen}** (concurrency=1), "
        f"measured peak {_human_bytes(budget.get('measured_peak_bytes', 0))}.\n"
    )
    probe = budget.get("cap_probe")
    if probe:
        out.append(
            f"- RLIMIT_AS probe ({probe['mechanism']}): `{probe['status']}`. "
            "This caps *virtual* address space, which Arrow/Polars over-reserve "
            "beyond RSS, so a failure here is not a true RSS-budget breach.\n"
        )
    out.append(
        "> For a faithful RSS hard cap on Linux, run the ETL inside a cgroup, e.g.:\n"
    )
    out.append(
        "> ```bash\n"
        f"> systemd-run --user --scope -p MemoryMax={_human_bytes(budget['budget_bytes']).replace(' ', '')} \\\n"
        ">   python benchmarks/run_bench.py --backend polars --source SRC --target OUT \\\n"
        f">   --num-shards {chosen} --num-threads $(nproc)\n"
        "> ```\n"
    )
    return out


def _maybe_plot(results_dir: Path, results: List[Dict]) -> List[tuple]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001
        return []

    produced: List[tuple] = []
    ok = [r for r in results if r["status"] == "ok"]
    if not ok:
        return produced

    backends = sorted({r["backend"] for r in ok})

    def _frontier(rows: List[Dict], key: str):
        """Leanest (min peak RSS) achievable per value of ``key``."""
        best: Dict = {}
        for r in rows:
            k = r[key]
            if k not in best or r["peak_rss_bytes"] < best[k]["peak_rss_bytes"]:
                best[k] = r
        return [best[k] for k in sorted(best)]

    # 1) Peak RSS vs dataset size (scaling ladder rows), leanest config per size.
    scaling_all = [
        r for r in ok if not r["label"].startswith(("knob_", "budget_", "full_"))
    ]
    if len({r["uncompressed_bytes"] for r in scaling_all}) >= 2:
        fig, ax = plt.subplots(figsize=(7, 4))
        for backend in backends:
            rows = _frontier(
                [r for r in scaling_all if r["backend"] == backend],
                "uncompressed_bytes",
            )
            if rows:
                ax.plot(
                    [r["uncompressed_bytes"] / 2**30 for r in rows],
                    [r["peak_rss_bytes"] / 2**30 for r in rows],
                    "o-",
                    label=backend,
                )
        ax.set_xlabel("uncompressed input (GiB)")
        ax.set_ylabel("peak RSS (GiB)")
        ax.set_title("Peak RSS vs dataset size (leanest config)")
        ax.legend()
        fig.tight_layout()
        fig.savefig(results_dir / "memory_scaling.png", dpi=120)
        plt.close(fig)
        produced.append(("Peak RSS vs dataset size", "memory_scaling.png"))

    # 2) Peak RSS vs num_shards (knob-sweep rows), leanest config per shard count.
    knob = [r for r in ok if r["label"].startswith("knob_")]
    if len({r["num_shards"] for r in knob}) >= 2:
        fig, ax = plt.subplots(figsize=(7, 4))
        for backend in backends:
            rows = _frontier(
                [r for r in knob if r["backend"] == backend], "num_shards"
            )
            if rows:
                ax.plot(
                    [r["num_shards"] for r in rows],
                    [r["peak_rss_bytes"] / 2**30 for r in rows],
                    "o-",
                    label=backend,
                )
        ax.set_xlabel("num_shards")
        ax.set_ylabel("peak RSS (GiB)")
        ax.set_title("Peak RSS vs num_shards (leanest config per shard count)")
        ax.legend()
        fig.tight_layout()
        fig.savefig(results_dir / "memory_vs_shards.png", dpi=120)
        plt.close(fig)
        produced.append(("Peak RSS vs num_shards", "memory_vs_shards.png"))

    # 3) RSS time-series for a few representative runs.
    with_series = [r for r in results if r.get("rss_series")]
    if with_series:
        with_series = sorted(with_series, key=lambda r: -r["peak_rss_bytes"])[:4]
        fig, ax = plt.subplots(figsize=(7, 4))
        for r in with_series:
            series = r["rss_series"]
            ax.plot(
                [t for t, _ in series],
                [b / 2**30 for _, b in series],
                label=f"{r['label']}/{r['backend']} s={r['num_shards']} c={r['concurrency']}",
            )
        ax.set_xlabel("time (s)")
        ax.set_ylabel("RSS (GiB)")
        ax.set_title("RSS over time")
        ax.legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(results_dir / "rss_timeseries.png", dpi=120)
        plt.close(fig)
        produced.append(("RSS over time", "rss_timeseries.png"))

    return produced
