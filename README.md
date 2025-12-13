# meds_etl_cpp

High-performance C++ backend for [meds_etl](https://github.com/Medical-Event-Data-Standard/meds_etl).

## Features

- Optimized parallel processing using C++17
- Arrow-based columnar data handling
- Python bindings via pybind11
- Native support for macOS (arm64) and Linux (x86_64)

## Installation

**macOS:**
```bash
pip install -e .
```

**Linux:**
```bash
cd native && bazel build //:meds_etl_cpp
```

See [BUILD_MAC.md](BUILD_MAC.md) for detailed macOS build instructions.

## Requirements

- Python ≥3.10
- PyArrow ≥18.0.0
- macOS: Xcode Command Line Tools
- Linux: Bazel, GCC ≥9

## Usage

```python
import meds_etl_cpp
# Provides optimized implementations for meds_etl
```

Documentation: [meds_etl repository](https://github.com/Medical-Event-Data-Standard/meds_etl)
