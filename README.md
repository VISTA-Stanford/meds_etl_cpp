# meds_etl_cpp

C++ backend for [meds_etl](https://github.com/Medical-Event-Data-Standard/meds_etl) with optimized algorithms.

## Installation

### macOS
```bash
pip install -e . --no-build-isolation
```

See [BUILD_MAC.md](BUILD_MAC.md) for details.

### Linux
```bash
cd native
bazel build //:meds_etl_cpp
```

## Requirements

- Python ≥3.10
- PyArrow ≥18.0.0

## Usage

```python
import meds_etl_cpp
# Use optimized algorithms from meds_etl
```

For full documentation, see the [meds_etl repository](https://github.com/Medical-Event-Data-Standard/meds_etl).
