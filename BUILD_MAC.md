# Building meds_etl_cpp on macOS arm64

## Quick Start

```bash
# Activate your Python environment
conda activate your_env  # or: source .venv/bin/activate

# Install with pip (recommended)
pip install -e . --no-build-isolation

# Test
python -c "import meds_etl_cpp; print('✅ Success!')"
```

**That's it!** The `--no-build-isolation` flag is the key.

---

## What We Learned

### The Problem with Regular `pip install -e .`

When you run `pip install -e .` without flags, pip creates an **isolated build environment**:

1. pip installs PyArrow in a **temporary** directory
2. Your extension builds against that temporary PyArrow
3. pip deletes the temporary directory
4. At runtime, your extension can't find PyArrow's libraries → **ImportError**

### The Solution: `--no-build-isolation`

The `--no-build-isolation` flag tells pip to use your **actual environment**:

1. pip uses PyArrow from **your environment**
2. Your extension builds against your environment's PyArrow
3. At runtime, it finds the libraries in the same place → **Works!** ✅

---

## Supported Python Versions

**Python 3.10, 3.11, 3.12** (and future 3.x versions)

Each Python version gets its own compiled extension:
- Python 3.10 → `meds_etl_cpp.cpython-310-darwin.so`
- Python 3.11 → `meds_etl_cpp.cpython-311-darwin.so`
- Python 3.12 → `meds_etl_cpp.cpython-312-darwin.so`

If you switch Python versions, just reinstall:
```bash
conda activate different_env
pip install -e . --no-build-isolation
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

### For Linux Build (unchanged)
- `native/BUILD` - Bazel build configuration
- `native/MODULE.bazel` - Bazel dependencies

Linux users continue using: `cd native && bazel build //:meds_etl_cpp`

---

## macOS-Specific Fixes

The `native/BUILD` file was updated to remove Linux-specific paths:

**Removed:**
- `-I/usr/include` (doesn't exist on modern macOS)
- `-fvisibility=hidden` (caused symbol issues)

**Added:**
- Auto-detection of Arrow library versions
- Explicit arm64 architecture targeting for Abseil

---

## Troubleshooting

### "ImportError: Library not loaded: libarrow.2200.dylib"

You either:
1. Forgot `--no-build-isolation`, or
2. PyArrow isn't installed

**Fix:**
```bash
pip install pyarrow
pip install -e . --no-build-isolation
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

### Build works but import fails

Use the correct installation method:
```bash
pip install -e . --no-build-isolation  # NOT: pip install -e .
```

---

## Using with uv

In your project's `pyproject.toml`:

```toml
[project]
dependencies = [
    "meds-etl-cpp @ file:///absolute/path/to/meds_etl_cpp",
]
```

Then:
```bash
uv sync
```

uv will build it automatically using your local version.

---

## Summary

### For Regular Use:
```bash
pip install -e . --no-build-isolation
```

### For C++ Development:
```bash
bash setup_dependencies.sh  # Once
make -f Makefile.simple     # Every change
```

### Key Lesson:
The `--no-build-isolation` flag is essential on macOS to avoid rpath issues with PyArrow's dynamic libraries.

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

