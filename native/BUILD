load(
    "@rules_foreign_cc//foreign_cc:defs.bzl", "cmake", "configure_make", "make"
)

load("@pybind11_bazel//:build_defs.bzl", "pybind_library", "pybind_extension")

cc_binary(
    name="simple_test",
    srcs=["simple_test.cc"],
)

cc_library(
    name="perform_etl",
    srcs=["perform_etl.cc", "pdqsort.h", "mmap_file.hh"],
    hdrs=["perform_etl.hh"],
    deps=[
        ":arrow", 
        "@abseil-cpp//absl/types:optional",
        "@abseil-cpp//absl/container:flat_hash_map",
        "@abseil-cpp//absl/container:flat_hash_set",
        "@readerwriterqueue",
        "@concurrentqueue",
    ],
)

cc_binary(
    name="main",
    srcs=["main.cc"],
    deps=[
        ":perform_etl", 
    ],
)

pybind_extension(
    name="meds_etl_cpp",
    srcs=[
        "meds_etl_cpp.cc",
    ],
    deps=[":perform_etl", ":arrow" ],
    linkopts = [],
)

cmake(
    name = "arrow",
    cache_entries = {
        "CMAKE_INSTALL_LIBDIR": "lib",
        "CMAKE_TOOLCHAIN_FILE": "",
        "CMAKE_C_FLAGS": "-fPIC",
        "CMAKE_CXX_FLAGS": "-fPIC",
        "EP_COMMON_CMAKE_ARGS": "-DWITH_OPENSSL=OFF",
        "ARROW_BUILD_SHARED": "OFF",
        "ARROW_BUILD_STATIC": "ON",
        "ARROW_BUILD_TESTS": "OFF",
        "ARROW_PARQUET": "ON",
        "ARROW_JEMALLOC": "OFF",
        "ARROW_IPC": "OFF",
        "ARROW_DEPENDENCY_SOURCE": "BUNDLED",
        "ARROW_WITH_SNAPPY": "ON",
        "ARROW_WITH_ZSTD": "ON",
        "CMAKE_OSX_DEPLOYMENT_TARGET": "12",
    },
    tags = ["requires-network"],
    generate_args = ["-GNinja", "-DCMAKE_RANLIB=/usr/bin/ranlib"],
    working_directory="cpp",
    lib_source = "@arrow//:all",
    out_static_libs = ["libparquet.a", "libarrow.a", "libarrow_bundled_dependencies.a"],
    linkopts = ["-pthread"],
)
