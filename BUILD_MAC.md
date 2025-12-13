# Building meds_etl_cpp on macOS arm64

## Quick Start

```bash
# Activate your Python environment
conda activate your_env  # or: source .venv/bin/activate

# Install
pip install -e .

# Test
python -c "import meds_etl_cpp; print('✅ Success!')"
```

---

## How It Works

The package uses a **relative rpath** (`@loader_path/../pyarrow`) so the extension finds PyArrow's libraries at runtime regardless of where your environment is located.

The `meds_etl_cpp/__init__.py` wrapper imports PyArrow first, ensuring its dylibs are loaded before the native extension.

---

## Supported Python Versions

**Python 3.10, 3.11, 3.12** (and future 3.x versions)

Each Python version gets its own compiled extension:
- Python 3.10 → `meds_etl_cpp/_native.cpython-310-darwin.so`
- Python 3.11 → `meds_etl_cpp/_native.cpython-311-darwin.so`
- Python 3.12 → `meds_etl_cpp/_native.cpython-312-darwin.so`

If you switch Python versions, just reinstall:
```bash
conda activate different_env
pip install -e .
```

---

## Alternative: Build with Makefile

For development or if you prefer more control:

```bash
# One-time setup
bash setup_dependencies.sh

# Build (5 seconds)
make -f Makefile.simple

# Test
python -c "import meds_etl_cpp"
```

The Makefile approach:
- ✅ Auto-detects your Python version
- ✅ Auto-detects PyArrow version (18.x, 20.x, 22.x, etc.)
- ✅ No pip complexity
- ✅ Great for rapid C++ development iteration

---

## Dependencies

### Runtime Dependencies (installed automatically)
- `pyarrow>=18.0.0` - Provides Arrow/Parquet libraries

### Build Dependencies
These are set up once by `setup_dependencies.sh`:
- **Abseil-cpp** - Built locally in `third_party/` (arm64)
- **Queue headers** - Downloaded to `native/` (header-only)
- **pybind11** - Installed via pip

---

## Key Files

### For macOS Build
- `Makefile.simple` - Fast, simple build for macOS
- `setup_dependencies.sh` - One-time dependency setup
- `setup.py` - Handles both Linux (Bazel) and macOS (Makefile)
- `meds_etl_cpp/__init__.py` - Python wrapper that loads PyArrow first

### For Linux Build (unchanged)
- `native/BUILD` - Bazel build configuration
- `native/MODULE.bazel` - Bazel dependencies

Linux users continue using: `cd native && bazel build //:meds_etl_cpp`

---

## Troubleshooting

### "ImportError: Library not loaded: libarrow.*.dylib"

PyArrow isn't installed:
```bash
pip install pyarrow
pip install -e .
```

### "Could not find libarrow dylib"

PyArrow isn't installed in your environment:
```bash
pip install pyarrow pybind11
```

### "No such file or directory: libabsl_*.a"

Dependencies not built. Run once:
```bash
bash setup_dependencies.sh
```

---

## Using with uv

```bash
uv pip install -e .
```

Or in your project's `pyproject.toml`:

```toml
[project]
dependencies = [
    "meds-etl-cpp @ file:///absolute/path/to/meds_etl_cpp",
]
```

---

## Build Times

- **First build:** 2-3 minutes (compiles Abseil once)
- **Subsequent builds:** ~5 seconds
- **After C++ changes:** ~5 seconds (with Makefile)

---

## Distribution

For building wheels for distribution:

```bash
pip install cibuildwheel
cibuildwheel --platform macos
```

This builds separate wheels for Python 3.10, 3.11, 3.12 automatically.
