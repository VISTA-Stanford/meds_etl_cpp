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
        
        # Get the runtime PyArrow location BEFORE building
        # (build environment might have different location)
        import pyarrow
        runtime_arrow_dir = pyarrow.get_library_dirs()[0]
        
        subprocess.run(
            args=["make", "-f", ext.makefile],
            cwd=ext.sourcedir,
            check=True,
        )
        
        # Find the built .so file
        built_files = list(pathlib.Path(ext.sourcedir).glob("*.so"))
        if not built_files:
            raise RuntimeError(f"Could not find built extension for {ext.name}")
        
        # Copy to the build directory
        parent_directory = os.path.abspath(
            os.path.join(self.get_ext_fullpath(ext.name), os.pardir)
        )
        os.makedirs(parent_directory, exist_ok=True)
        
        shutil.copy(str(built_files[0]), self.get_ext_fullpath(ext.name))
        os.chmod(self.get_ext_fullpath(ext.name), 0o755)
        
        # Fix rpath to point to runtime PyArrow location (not build-time)
        # This is critical for pip installs which use isolated build environments
        try:
            # Get all existing rpaths
            result = subprocess.run(
                ["otool", "-l", self.get_ext_fullpath(ext.name)],
                capture_output=True,
                text=True,
            )
            
            # Extract all rpath entries
            existing_rpaths = []
            lines = result.stdout.split('\n')
            for i, line in enumerate(lines):
                if 'cmd LC_RPATH' in line:
                    # The path is 2 lines down
                    if i + 2 < len(lines) and 'path ' in lines[i + 2]:
                        path = lines[i + 2].strip().split('path ')[1].split(' (')[0]
                        existing_rpaths.append(path)
            
            print(f"📋 Existing rpaths: {existing_rpaths}")
            
            # Remove any rpath that's from a temp build env
            for rpath in existing_rpaths:
                if 'pip-build-env' in rpath or '/tmp/' in rpath or '/var/folders' in rpath:
                    if '/site-packages/pyarrow' in rpath:  # Only remove temp pyarrow paths
                        try:
                            subprocess.run(
                                ["install_name_tool", "-delete_rpath", rpath,
                                 self.get_ext_fullpath(ext.name)],
                                check=True,
                            )
                            print(f"🗑️  Removed temporary rpath: {rpath}")
                        except subprocess.CalledProcessError:
                            pass  # Already removed or doesn't exist
            
            # Add the correct runtime rpath if not already there
            if runtime_arrow_dir not in existing_rpaths:
                try:
                    subprocess.run(
                        ["install_name_tool", "-add_rpath", runtime_arrow_dir,
                         self.get_ext_fullpath(ext.name)],
                        check=True,
                    )
                    print(f"✅ Added runtime rpath: {runtime_arrow_dir}")
                except subprocess.CalledProcessError as e:
                    print(f"⚠️  Could not add rpath (may already exist): {e}")
            else:
                print(f"✅ Runtime rpath already correct: {runtime_arrow_dir}")
                
        except Exception as e:
            print(f"⚠️  Warning: Could not fix rpath: {e}")
            print(f"   Manual fix: bash fix_rpath.sh")
        
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
        # macOS: Use Makefile approach
        print("🍎 Detected macOS - using Makefile build")
        return MakefileExtension("meds_etl_cpp", "Makefile.simple", ".")
    else:
        # Linux and others: Use Bazel
        print("🐧 Detected Linux/other - using Bazel build")
        return BazelExtension("meds_etl_cpp", "meds_etl_cpp.so", "native")


setuptools.setup(
    ext_modules=[get_extension_for_platform()],
    cmdclass={"build_ext": HybridBuildExt},
    zip_safe=False,
)

