"""meds_sort - sort and shard a MEDS Unsorted dataset into MEDS.

A pure Python/Polars implementation of the MEDS Unsorted -> MEDS finalization
step: it reads ``unsorted_data/*.parquet``, distributes subjects across shards,
sorts each shard by ``(subject_id, time, ...)``, and writes the resulting
``data/<shard>.parquet`` files.

This package was previously distributed as ``meds_etl_cpp`` (a compiled C++
extension); that name is kept as a thin, deprecated compatibility shim.
"""

from ._etl import perform_etl

__version__ = "0.4.0"
__all__ = ["perform_etl"]
