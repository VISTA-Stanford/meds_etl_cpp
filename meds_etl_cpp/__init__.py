"""Deprecated compatibility shim for :mod:`meds_sort`.

This package was renamed to ``meds_sort`` and reimplemented in pure
Python/Polars (it no longer ships a C++ extension). Importing ``meds_etl_cpp``
still works and re-exports :func:`meds_sort.perform_etl`, but new code should
import ``meds_sort`` directly.
"""

import warnings

from meds_sort import __version__, perform_etl

warnings.warn(
    "meds_etl_cpp has been renamed to meds_sort; import meds_sort instead. "
    "The meds_etl_cpp name is a deprecated compatibility shim.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["perform_etl", "__version__"]
