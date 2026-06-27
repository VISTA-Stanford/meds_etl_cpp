"""meds_etl_cpp - pure Python/Polars backend for meds_etl.

Historically this package shipped a compiled C++/pybind11 extension. It is now a
pure Python implementation built on top of Polars, with the same public API
(:func:`perform_etl`) and output layout, so it remains a drop-in dependency for
``meds_etl[cpp]`` while no longer requiring a native build toolchain.
"""

from ._etl import perform_etl

__version__ = "0.4.0"
__all__ = ["perform_etl"]
