"""Correctness tests for the pure Python/Polars ``perform_etl`` implementation."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import polars as pl
import pytest

import meds_sort


def _write_unsorted(source: Path) -> None:
    """Create a small, heterogeneous MEDS Unsorted dataset under ``source``."""
    unsorted = source / "unsorted_data"
    unsorted.mkdir(parents=True, exist_ok=True)

    # File 0: code, numeric_value (float64), an extra "unit" property, a null time.
    pl.DataFrame(
        {
            "subject_id": [3, 1, 2, 1],
            "time": [
                datetime.datetime(2020, 1, 5),
                datetime.datetime(2020, 1, 1),
                None,
                datetime.datetime(2020, 1, 3),
            ],
            "code": ["B", "A", "Z", "A"],
            "numeric_value": pl.Series([1.5, 2.5, None, 0.0], dtype=pl.Float64),
            "unit": ["mg", "kg", None, "kg"],
        }
    ).write_parquet(unsorted / "0.parquet")

    # File 1: no numeric_value/unit columns, a different extra property "table".
    pl.DataFrame(
        {
            "subject_id": [2, 1, 3],
            "time": [
                datetime.datetime(2020, 1, 2),
                datetime.datetime(2020, 1, 1),
                datetime.datetime(2020, 1, 4),
            ],
            "code": ["Y", "A", "C"],
            "table": ["t1", "t2", "t1"],
        }
    ).write_parquet(unsorted / "1.parquet")


def _read_all_outputs(target: Path) -> list[pl.DataFrame]:
    return [pl.read_parquet(p) for p in sorted((target / "data").glob("*.parquet"))]


def test_end_to_end(tmp_path: Path) -> None:
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    _write_unsorted(source)
    (source / "metadata").mkdir(parents=True)
    (source / "metadata" / "dataset.json").write_text(json.dumps({"dataset_name": "x"}))

    meds_sort.perform_etl(str(source), str(target), num_shards=4, num_threads=4)

    # Metadata is copied.
    assert (target / "metadata" / "dataset.json").exists()
    assert json.loads((target / "metadata" / "dataset.json").read_text())["dataset_name"] == "x"

    # The intermediate shard directory is cleaned up.
    assert not (target / "_shard_tmp").exists()

    dfs = _read_all_outputs(target)
    assert len(dfs) >= 1

    expected_cols = ["subject_id", "time", "code", "numeric_value", "table", "unit"]
    expected_dtypes = [pl.Int64, pl.Datetime("us"), pl.String, pl.Float32, pl.String, pl.String]

    subject_to_file: dict[int, int] = {}
    total_rows = 0
    for idx, df in enumerate(dfs):
        # Schema: exact column order and dtypes.
        assert df.columns == expected_cols
        assert df.dtypes == expected_dtypes

        # Each shard is sorted by (subject_id, time, code, numeric_value, *props).
        assert df.equals(df.sort(expected_cols))

        # Every subject appears in exactly one shard file.
        for subject in df["subject_id"].unique().to_list():
            assert subject not in subject_to_file, "subject split across shards"
            subject_to_file[subject] = idx

        total_rows += df.height

    # Row count is conserved (4 + 3 input rows).
    assert total_rows == 7
    # File count equals the number of non-empty shards.
    assert len(dfs) == len({subject_to_file[s] for s in subject_to_file})


def test_global_sort_invariant(tmp_path: Path) -> None:
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    _write_unsorted(source)

    meds_sort.perform_etl(str(source), str(target), num_shards=8, num_threads=2)

    full = pl.concat(_read_all_outputs(target))
    # Within each subject, rows are sorted by time (nulls first).
    for _, group in full.sort(["subject_id", "time"]).group_by("subject_id", maintain_order=True):
        times = group["time"].to_list()
        non_null = [t for t in times if t is not None]
        assert non_null == sorted(non_null)


def test_code_and_numeric_value_always_present(tmp_path: Path) -> None:
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    unsorted = source / "unsorted_data"
    unsorted.mkdir(parents=True)
    # No code, no numeric_value at all.
    pl.DataFrame(
        {
            "subject_id": [1, 2],
            "time": [datetime.datetime(2020, 1, 1), datetime.datetime(2020, 1, 2)],
            "extra": ["a", "b"],
        }
    ).write_parquet(unsorted / "0.parquet")

    meds_sort.perform_etl(str(source), str(target), num_shards=2, num_threads=1)

    full = pl.concat(_read_all_outputs(target))
    assert full.columns == ["subject_id", "time", "code", "numeric_value", "extra"]
    assert full["code"].null_count() == full.height
    assert full["numeric_value"].null_count() == full.height
    assert full.schema["numeric_value"] == pl.Float32


def test_conflicting_dtypes_raise(tmp_path: Path) -> None:
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    unsorted = source / "unsorted_data"
    unsorted.mkdir(parents=True)
    pl.DataFrame(
        {"subject_id": [1], "time": [datetime.datetime(2020, 1, 1)], "prop": pl.Series([1], dtype=pl.Int32)}
    ).write_parquet(unsorted / "0.parquet")
    pl.DataFrame(
        {"subject_id": [2], "time": [datetime.datetime(2020, 1, 1)], "prop": ["x"]}
    ).write_parquet(unsorted / "1.parquet")

    with pytest.raises(ValueError, match="conflicting types"):
        meds_sort.perform_etl(str(source), str(target), num_shards=2, num_threads=1)


def test_timezone_time_raises(tmp_path: Path) -> None:
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    unsorted = source / "unsorted_data"
    unsorted.mkdir(parents=True)
    df = pl.DataFrame({"subject_id": [1], "time": [datetime.datetime(2020, 1, 1)], "code": ["A"]})
    df = df.with_columns(pl.col("time").dt.replace_time_zone("UTC"))
    df.write_parquet(unsorted / "0.parquet")

    with pytest.raises(ValueError, match="timezone"):
        meds_sort.perform_etl(str(source), str(target), num_shards=2, num_threads=1)


def test_value_column_raises(tmp_path: Path) -> None:
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    unsorted = source / "unsorted_data"
    unsorted.mkdir(parents=True)
    pl.DataFrame(
        {"subject_id": [1], "time": [datetime.datetime(2020, 1, 1)], "code": ["A"], "value": ["x"]}
    ).write_parquet(unsorted / "0.parquet")

    with pytest.raises(ValueError, match="value"):
        meds_sort.perform_etl(str(source), str(target), num_shards=2, num_threads=1)


def test_empty_input(tmp_path: Path) -> None:
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    (source / "unsorted_data").mkdir(parents=True)

    # Should not raise, and should produce an (empty) data directory.
    meds_sort.perform_etl(str(source), str(target), num_shards=2, num_threads=1)
    assert (target / "data").exists()
    assert list((target / "data").glob("*.parquet")) == []


def test_invalid_num_shards(tmp_path: Path) -> None:
    source = tmp_path / "src"
    target = tmp_path / "tgt"
    (source / "unsorted_data").mkdir(parents=True)
    with pytest.raises(ValueError, match="num_shards"):
        meds_sort.perform_etl(str(source), str(target), num_shards=0, num_threads=1)


def test_meds_etl_cpp_shim_reexports_and_warns() -> None:
    import importlib
    import sys

    # Ensure a fresh import so the deprecation warning fires.
    sys.modules.pop("meds_etl_cpp", None)
    with pytest.warns(DeprecationWarning, match="renamed to meds_sort"):
        meds_etl_cpp = importlib.import_module("meds_etl_cpp")

    assert meds_etl_cpp.perform_etl is meds_sort.perform_etl
