"""
meds_etl_cpp - High-performance C++ backend for meds_etl

This module provides optimized implementations of MEDS ETL algorithms.
"""

# Load pyarrow first to ensure Arrow dylibs are available
# This is required on macOS where the native extension links against PyArrow's libraries
import pyarrow as _pyarrow  # noqa: F401

# Import everything from the native extension
from ._native import *  # noqa: F401, F403

__version__ = "0.1.0"
__all__ = ["perform_etl"]

