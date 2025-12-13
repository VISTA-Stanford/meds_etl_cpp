from __future__ import annotations

import os
import pathlib
import platform
import shutil
import subprocess
import sys
from typing import List

import setuptools
from setuptools.command.build_ext import build_ext


class BazelExtension(setuptools.Extension):
    def __init__(self, name: str, target: str, sourcedir: str):
        super().__init__(name, sources=[])
        self.target = target
        self.sourcedir = str(pathlib.Path(sourcedir).resolve())


class MakefileExtension(setuptools.Extension):
    def __init__(self, name: str, makefile: str, sourcedir: str):
        super().__init__(name, sources=[])
        self.makefile = makefile
        self.sourcedir = str(pathlib.Path(sourcedir).resolve())


def can_build_simple(sourcedir, env, bazel_extra_args):
    try:
        subprocess.run(
            args=["bazel"] + bazel_extra_args + ["build", "-c", "opt", "simple_test"],
            cwd=sourcedir,
            env=env,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


class HybridBuildExt(build_ext):
    """Build extension that chooses between Bazel (Linux) and Makefile (macOS)"""
    
    def build_extensions(self) -> None:
        for ext in self.extensions:
            if isinstance(ext, BazelExtension):
                self.build_bazel_extension(ext)
            elif isinstance(ext, MakefileExtension):
                self.build_makefile_extension(ext)
            else:
                super().build_extension(ext)
    
    def build_makefile_extension(self, ext: MakefileExtension) -> None:
        """Build using simple Makefile (for macOS)"""
        print(f"Building {ext.name} using Makefile...")
        
        # Auto-setup dependencies if needed
        setup_script = pathlib.Path(ext.sourcedir) / "setup_dependencies.sh"
        abseil_path = pathlib.Path(ext.sourcedir) / "third_party" / "abseil-cpp" / "build"
        
        # Check if we need to run setup
        needs_setup = False
        try:
            import pyarrow
            import pybind11
        except ImportError:
            print("📦 PyArrow or pybind11 not found - will install...")
            needs_setup = True
        
        if not (abseil_path / "absl").exists():
            print("📦 Abseil not found - will build...")
            needs_setup = True
        
        # Run setup script if needed
        if needs_setup and setup_script.exists():
            print("🔧 Running dependency setup (this may take a few minutes)...")
            try:
                subprocess.run(
                    args=["bash", str(setup_script)],
                    cwd=ext.sourcedir,
                    check=True,
                )
                print("✅ Dependencies ready!")
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    f"Dependency setup failed. Please run manually: bash setup_dependencies.sh"
                ) from e
        else:
            print("✅ All dependencies found")
        
        # Build with make
        subprocess.run(
            args=["make", "-f", ext.makefile, "clean"],
            cwd=ext.sourcedir,
            check=True,
        )
        
        subprocess.run(
            args=["make", "-f", ext.makefile],
            cwd=ext.sourcedir,
            check=True,
        )
        
        # Find the built .so file in the package directory
        # Makefile builds to meds_etl_cpp/_native.*.so
        pkg_dir = pathlib.Path(ext.sourcedir) / "meds_etl_cpp"
        built_files = list(pkg_dir.glob("_native*.so"))
        if not built_files:
            # Fallback: check root directory
            built_files = list(pathlib.Path(ext.sourcedir).glob("meds_etl_cpp*.so"))
        if not built_files:
            raise RuntimeError(f"Could not find built extension for {ext.name}")
        
        # Copy to the build directory
        parent_directory = os.path.abspath(
            os.path.join(self.get_ext_fullpath(ext.name), os.pardir)
        )
        os.makedirs(parent_directory, exist_ok=True)
        
        # Copy built extension to the target location
        ext_fullpath = self.get_ext_fullpath(ext.name)
        shutil.copy(str(built_files[0]), ext_fullpath)
        os.chmod(ext_fullpath, 0o755)
        
        # The Makefile sets relative rpath: @loader_path/../pyarrow
        # This works when installed in site-packages (meds_etl_cpp/_native.so -> ../pyarrow)
        # The meds_etl_cpp/__init__.py imports pyarrow first as a fallback
        
        # Verify rpath is set correctly
        try:
            result = subprocess.run(
                ["otool", "-l", ext_fullpath],
                capture_output=True,
                text=True,
            )
            if "@loader_path/../pyarrow" in result.stdout:
                print(f"✅ Relative rpath configured correctly")
            else:
                print(f"⚠️  Adding relative rpath for pyarrow...")
                subprocess.run(
                    ["install_name_tool", "-add_rpath", "@loader_path/../pyarrow", ext_fullpath],
                    check=False,  # May already exist
                )
        except Exception as e:
            print(f"⚠️  Could not verify rpath: {e}")
        
        print(f"✅ Built {ext.name} successfully using Makefile")
    
    def build_bazel_extension(self, ext: BazelExtension) -> None:
        """Build using Bazel (for Linux)"""
        print(f"Building {ext.name} using Bazel...")
        
        try:
            subprocess.check_output(["bazel", "version"]).decode("utf8")
        except OSError:
            raise RuntimeError("Cannot find bazel executable")
        
        source_env = dict(os.environ)
        env = {**source_env}
        
        bazel_extra_args: List[str] = []
        extra_args: List[str] = []
        
        if source_env.get("DISTDIR"):
            extra_args.extend(["--distdir", source_env["DISTDIR"]])
        
        if source_env.get("MACOSX_DEPLOYMENT_TARGET"):
            extra_args.extend(["--macos_minimum_os", source_env["MACOSX_DEPLOYMENT_TARGET"]])
        
        if source_env.get("DISABLE_CPU_ARCH") or not can_build_simple(
            sourcedir=ext.sourcedir, env=env, bazel_extra_args=bazel_extra_args
        ):
            bazel_extra_args.extend(["--noworkspace_rc", "--bazelrc=backupbazelrc"])
            assert can_build_simple(
                sourcedir=ext.sourcedir, env=env, bazel_extra_args=bazel_extra_args
            ), "Cannot build C++ extension"
        
        subprocess.run(
            args=["bazel", "clean", "--expunge"],
            cwd=ext.sourcedir,
            env=env,
            check=True,
        )
        
        if source_env.get("DEBUG", False):
            compile_mode = "dbg"
        else:
            compile_mode = "opt"
        
        subprocess.run(
            args=["bazel"] + bazel_extra_args + ["build", "-c", compile_mode, ext.target] + extra_args,
            cwd=ext.sourcedir,
            env=env,
            check=True,
        )
        
        parent_directory = os.path.abspath(
            os.path.join(self.get_ext_fullpath(ext.name), os.pardir)
        )
        os.makedirs(parent_directory, exist_ok=True)
        
        shutil.copy(
            os.path.join(ext.sourcedir, "bazel-bin", ext.target),
            self.get_ext_fullpath(ext.name),
        )
        
        os.chmod(self.get_ext_fullpath(ext.name), 0o700)
        
        print(f"✅ Built {ext.name} successfully using Bazel")


def get_extension_for_platform():
    """Choose the appropriate extension type based on platform"""
    system = platform.system()
    
    if system == "Darwin":
        # macOS: Use Makefile approach - builds into meds_etl_cpp/_native
        print("🍎 Detected macOS - using Makefile build")
        return MakefileExtension("meds_etl_cpp._native", "Makefile.simple", ".")
    else:
        # Linux and others: Use Bazel - builds into meds_etl_cpp/_native
        print("🐧 Detected Linux/other - using Bazel build")
        return BazelExtension("meds_etl_cpp._native", "meds_etl_cpp.so", "native")


setuptools.setup(
    ext_modules=[get_extension_for_platform()],
    cmdclass={"build_ext": HybridBuildExt},
    zip_safe=False,
)

