#!/bin/bash
# Setup script to ensure dependencies are available
# This is only needed for macOS Makefile build

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo "🔍 Checking build dependencies..."

# Check if we're on macOS
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo "🍎 macOS detected - checking Makefile dependencies..."
    
    # Check for PyArrow
    if ! python -c "import pyarrow" 2>/dev/null; then
        echo "📦 Installing PyArrow..."
        pip install pyarrow
    else
        echo "✅ PyArrow found"
    fi
    
    # Check for pybind11
    if ! python -c "import pybind11" 2>/dev/null; then
        echo "📦 Installing pybind11..."
        pip install pybind11
    else
        echo "✅ pybind11 found"
    fi
    
    # Check for Abseil
    if [ ! -d "third_party/abseil-cpp/build/absl" ]; then
        echo "📦 Building Abseil-cpp..."
        
        # Clone if needed
        if [ ! -d "third_party/abseil-cpp" ]; then
            echo "  📥 Cloning Abseil..."
            mkdir -p third_party
            git clone --depth 1 --branch 20250127.1 https://github.com/abseil/abseil-cpp.git third_party/abseil-cpp
        fi
        
        # Build
        echo "  🔨 Compiling Abseil for arm64..."
        mkdir -p third_party/abseil-cpp/build
        cd third_party/abseil-cpp/build
        cmake -DCMAKE_CXX_STANDARD=17 \
              -DCMAKE_BUILD_TYPE=Release \
              -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
              -DABSL_PROPAGATE_CXX_STD=ON \
              -DCMAKE_OSX_ARCHITECTURES=arm64 \
              ..
        cmake --build . --config Release -j 4
        cd "$SCRIPT_DIR"
        echo "  ✅ Abseil built successfully"
    else
        echo "✅ Abseil found"
    fi
    
    # Download header-only queue libraries
    echo "📦 Checking queue libraries..."
    cd native
    for file in concurrentqueue.h blockingconcurrentqueue.h lightweightsemaphore.h readerwriterqueue.h atomicops.h; do
        if [ ! -f "$file" ]; then
            echo "  📥 Downloading $file..."
            case $file in
                concurrentqueue.h|blockingconcurrentqueue.h|lightweightsemaphore.h)
                    curl -sL "https://raw.githubusercontent.com/cameron314/concurrentqueue/master/$file" -o "$file"
                    ;;
                readerwriterqueue.h|atomicops.h)
                    curl -sL "https://raw.githubusercontent.com/cameron314/readerwriterqueue/master/$file" -o "$file"
                    ;;
            esac
        fi
    done
    cd "$SCRIPT_DIR"
    echo "✅ Queue libraries ready"
    
    echo ""
    echo "✅ All macOS dependencies ready!"
else
    echo "🐧 Linux detected - Bazel will manage dependencies"
fi

echo ""
echo "✨ Ready to build!"

