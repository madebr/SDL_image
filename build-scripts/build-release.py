#!/usr/bin/env python

import argparse
import collections
import contextlib
import datetime
import fnmatch
import glob
import io
import json
import logging
import multiprocessing
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import typing
import zipfile

logger = logging.getLogger(__name__)


GIT_HASH_FILENAME = ".git-hash"


class VsArchPlatformConfig:
    def __init__(self, arch: str, platform: str, configuration: str):
        self.arch = arch
        self.platform = platform
        self.configuration = configuration

    def configure(self, s: str) -> str:
        return s.replace("@ARCH@", self.arch).replace("@PLATFORM@", self.platform).replace("@CONFIGURATION@", self.configuration)


@contextlib.contextmanager
def chdir(path):
    original_cwd = os.getcwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(original_cwd)


class Executer:
    def __init__(self, root: Path, dry: bool=False):
        self.root = root
        self.dry = dry

    def run(self, cmd, cwd=None, env=None):
        logger.info("Executing args=%r", cmd)
        sys.stdout.flush()
        if not self.dry:
            subprocess.run(cmd, check=True, cwd=cwd or self.root, env=env, text=True)

    def check_output(self, cmd, cwd=None, dry_out=None, env=None, text=True):
        logger.info("Executing args=%r", cmd)
        sys.stdout.flush()
        if self.dry:
            return dry_out
        return subprocess.check_output(cmd, cwd=cwd or self.root, env=env, text=text)


class SectionPrinter:
    @contextlib.contextmanager
    def group(self, title: str):
        print(f"{title}:")
        yield


class GitHubSectionPrinter(SectionPrinter):
    def __init__(self):
        super().__init__()
        self.in_group = False

    @contextlib.contextmanager
    def group(self, title: str):
        print(f"::group::{title}")
        assert not self.in_group, "Can enter a group only once"
        self.in_group = True
        yield
        self.in_group = False
        print("::endgroup::")


class VisualStudio:
    def __init__(self, executer: Executer, year: typing.Optional[str]=None):
        self.executer = executer
        self.vsdevcmd = self.find_vsdevcmd(year)
        self.msbuild = self.find_msbuild()

    @property
    def dry(self) -> bool:
        return self.executer.dry

    VS_YEAR_TO_VERSION = {
        "2022": 17,
        "2019": 16,
        "2017": 15,
        "2015": 14,
        "2013": 12,
    }

    def find_vsdevcmd(self, year: typing.Optional[str]=None) -> typing.Optional[Path]:
        vswhere_spec = ["-latest"]
        if year is not None:
            try:
                version = self.VS_YEAR_TO_VERSION[year]
            except KeyError:
                logger.error("Invalid Visual Studio year")
                return None
            vswhere_spec.extend(["-version", f"[{version},{version+1})"])
        vswhere_cmd = ["vswhere"] + vswhere_spec + ["-property", "installationPath"]
        vs_install_path = Path(self.executer.check_output(vswhere_cmd, dry_out="/tmp").strip())
        logger.info("VS install_path = %s", vs_install_path)
        assert vs_install_path.is_dir(), "VS installation path does not exist"
        vsdevcmd_path = vs_install_path / "Common7/Tools/vsdevcmd.bat"
        logger.info("vsdevcmd path = %s", vsdevcmd_path)
        if self.dry:
            vsdevcmd_path.parent.mkdir(parents=True, exist_ok=True)
            vsdevcmd_path.touch(exist_ok=True)
        assert vsdevcmd_path.is_file(), "vsdevcmd.bat batch file does not exist"
        return vsdevcmd_path

    def find_msbuild(self) -> typing.Optional[Path]:
        vswhere_cmd = ["vswhere", "-latest", "-requires", "Microsoft.Component.MSBuild", "-find", r"MSBuild\**\Bin\MSBuild.exe"]
        msbuild_path = Path(self.executer.check_output(vswhere_cmd, dry_out="/tmp/MSBuild.exe").strip())
        logger.info("MSBuild path = %s", msbuild_path)
        if self.dry:
            msbuild_path.parent.mkdir(parents=True, exist_ok=True)
            msbuild_path.touch(exist_ok=True)
        assert msbuild_path.is_file(), "MSBuild.exe does not exist"
        return msbuild_path

    def build(self, arch_platform: VsArchPlatformConfig, projects: list[Path]):
        assert projects, "Need at least one project to build"

        vsdev_cmd_str = f"\"{self.vsdevcmd}\" -arch={arch_platform.arch}"
        msbuild_cmd_str = " && ".join([f"\"{self.msbuild}\" \"{project}\" /m /p:BuildInParallel=true /p:Platform={arch_platform.platform} /p:Configuration={arch_platform.configuration}" for project in projects])
        bat_contents = f"{vsdev_cmd_str} && {msbuild_cmd_str}\n"
        bat_path = Path(tempfile.gettempdir()) / "cmd.bat"
        with bat_path.open("w") as f:
            f.write(bat_contents)

        logger.info("Running cmd.exe script (%s): %s", bat_path, bat_contents)
        cmd = ["cmd.exe", "/D", "/E:ON", "/V:OFF", "/S", "/C", f"CALL {str(bat_path)}"]
        self.executer.run(cmd)


class Releaser:
    def __init__(self, release_info: dict, commit: str, root: Path, dist_path: Path, section_printer: SectionPrinter, executer: Executer, cmake_generator: str, deps_path: Path, overwrite: bool):
        self.release_info = release_info
        self.project = release_info["name"]
        self.version = self.extract_sdl_version(root=root, release_info=release_info)
        self.root = root
        self.commit = commit
        self.dist_path = dist_path
        self.section_printer = section_printer
        self.executer = executer
        self.cmake_generator = cmake_generator
        self.cpu_count = multiprocessing.cpu_count()
        self.deps_path = deps_path
        self.overwrite = overwrite

        self.artifacts: dict[str, Path] = {}

    @property
    def dry(self) -> bool:
        return self.executer.dry

    def prepare(self):
        logger.debug("Creating dist folder")
        self.dist_path.mkdir(parents=True, exist_ok=True)

    TreeItem = collections.namedtuple("TreeItem", ("path", "mode", "data", "time"))
    def _get_file_times(self, paths: tuple[str, ...]) -> dict[str, datetime.datetime]:
        dry_out = textwrap.dedent("""\
            time=2024-03-14T15:40:25-07:00

            M\tCMakeLists.txt
        """)
        git_log_out = self.executer.check_output(["git", "log", "--name-status", '--pretty=time=%cI', self.commit], dry_out=dry_out).splitlines(keepends=False)
        current_time = None
        set_paths = set(paths)
        path_times: dict[str, datetime.datetime] = {}
        for line in git_log_out:
            if not line:
                continue
            if line.startswith("time="):
                current_time = datetime.datetime.fromisoformat(line.removeprefix("time="))
                continue
            mod_type, file_paths = line.split(maxsplit=1)
            assert current_time is not None
            for file_path in file_paths.split("\t"):
                if file_path in set_paths and file_path not in path_times:
                    path_times[file_path] = current_time
        assert set(path_times.keys()) == set_paths
        return path_times

    @staticmethod
    def _path_filter(path: str):
        if path.startswith(".git"):
            return False
        return True

    def _get_git_contents(self) -> dict[str, TreeItem]:
        contents_tgz = self.executer.check_output(["git", "archive", "--format=tar.gz", self.commit, "-o", "/dev/stdout"], text=False)
        contents = tarfile.open(fileobj=io.BytesIO(contents_tgz), mode="r:gz")
        filenames = tuple(m.name for m in contents if m.isfile())
        for file in self.release_info["source"]["checks"]:
            assert file in filenames, f"'{file}' must exist"
        file_times = self._get_file_times(filenames)
        git_contents = {}
        for ti in contents:
            if not ti.isfile():
                continue
            if not self._path_filter(ti.name):
                continue
            contents_file = contents.extractfile(ti.name)
            assert contents_file, f"{ti.name} is not a file"
            git_contents[ti.name] = self.TreeItem(path=ti.name, mode=ti.mode, data=contents_file.read(), time=file_times[ti.name])
        return git_contents

    def create_source_archives(self) -> None:
        archive_base = f"{self.project}-{self.version}"

        git_contents = self._get_git_contents()
        git_files = list(git_contents.values())
        assert len(git_contents) == len(git_files)

        latest_mod_time = max(item.time for item in git_files)

        git_files.append(self.TreeItem(path="VERSION.txt", data=f"{self.version}\n".encode(), mode=0o100644, time=latest_mod_time))
        git_files.append(self.TreeItem(path=GIT_HASH_FILENAME, data=f"{self.commit}\n".encode(), mode=0o100644, time=latest_mod_time))

        git_files.sort(key=lambda v: v.time)

        zip_path = self.dist_path / f"{archive_base}.zip"
        logger.info("Creating .zip source archive (%s)...", zip_path)
        if self.dry:
            zip_path.touch()
        else:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_object:
                for git_file in git_files:
                    file_data_time = (git_file.time.year, git_file.time.month, git_file.time.day, git_file.time.hour, git_file.time.minute, git_file.time.second)
                    zip_info = zipfile.ZipInfo(filename=f"{archive_base}/{git_file.path}", date_time=file_data_time)
                    zip_info.external_attr = git_file.mode << 16
                    zip_info.compress_type = zipfile.ZIP_DEFLATED
                    zip_object.writestr(zip_info, data=git_file.data)
        self.artifacts["src-zip"] = zip_path

        tar_types = (
            (".tar.gz", "gz"),
            (".tar.xz", "xz"),
        )
        for ext, comp in tar_types:
            tar_path = self.dist_path / f"{archive_base}{ext}"
            logger.info("Creating %s source archive (%s)...", ext, tar_path)
            if self.dry:
                tar_path.touch()
            else:
                with tarfile.open(tar_path, f"w:{comp}") as tar_object:
                    for git_file in git_files:
                        tar_info = tarfile.TarInfo(f"{archive_base}/{git_file.path}")
                        tar_info.mode = git_file.mode
                        tar_info.size = len(git_file.data)
                        tar_info.mtime = git_file.time.timestamp()
                        tar_object.addfile(tar_info, fileobj=io.BytesIO(git_file.data))

            if tar_path.suffix == ".gz":
                # Zero the embedded timestamp in the gzip'ed tarball
                with open(tar_path, "r+b") as f:
                    f.seek(4, 0)
                    f.write(b"\x00\x00\x00\x00")

            self.artifacts[f"src-tar-{comp}"] = tar_path

    def create_dmg(self, configuration: str="Release") -> None:
        dmg_in = self.root / self.release_info["dmg"]["path"]
        xcode_project = self.root / self.release_info["dmg"]["project"]
        assert xcode_project.is_dir(), f"{xcode_projet} must be a directory"
        assert (xcode_project / "project.pbxproj").is_file, f"{xcode_project} must contain project.pbxproj"
        dmg_in.unlink(missing_ok=True)
        self.executer.run(["xcodebuild", "-project", xcode_project, "-target", self.release_info["dmg"]["target"], "-configuration", configuration])
        if self.dry:
            dmg_in.parent.mkdir(parents=True, exist_ok=True)
            dmg_in.touch()

        assert dmg_in.is_file(), f"{self.project}.dmg was not created by xcodebuild"

        dmg_out = self.dist_path / f"{self.project}-{self.version}.dmg"
        shutil.copy(dmg_in, dmg_out)
        self.artifacts["dmg"] = dmg_out

    @property
    def git_hash_data(self) -> bytes:
        return f"{self.commit}\n".encode()

    def _tar_add_git_hash(self, tar_object: tarfile.TarFile, root: typing.Optional[str]=None, time: typing.Optional[datetime.datetime]=None):
        if not time:
            time = datetime.datetime(year=2024, month=4, day=1)
        path = GIT_HASH_FILENAME
        if root:
            path = f"{root}/{path}"

        tar_info = tarfile.TarInfo(path)
        tar_info.mode = 0o100644
        tar_info.size = len(self.git_hash_data)
        tar_info.mtime = int(time.timestamp())
        tar_object.addfile(tar_info, fileobj=io.BytesIO(self.git_hash_data))

    def _zip_add_git_hash(self, zip_file: zipfile.ZipFile, root: typing.Optional[str]=None, time: typing.Optional[datetime.datetime]=None):
        if not time:
            time = datetime.datetime(year=2024, month=4, day=1)
        path = GIT_HASH_FILENAME
        if root:
            path = f"{root}/{path}"

        file_data_time = (time.year, time.month, time.day, time.hour, time.minute, time.second)
        zip_info = zipfile.ZipInfo(filename=path, date_time=file_data_time)
        zip_info.external_attr = 0o100644 << 16
        zip_info.compress_type = zipfile.ZIP_DEFLATED
        zip_file.writestr(zip_info, data=self.git_hash_data)

    def create_mingw_archives(self) -> None:
        build_type = "Release"
        build_parent_dir = self.root / "build-mingw"
        assert "autotools" in self.release_info["mingw"]
        assert "cmake" not in self.release_info["mingw"]
        mingw_archs = self.release_info["mingw"]["autotools"]["archs"]
        ARCH_TO_TRIPLET = {
            "x86": "i686-w64-mingw32",
            "x64": "x86_64-w64-mingw32",
        }
        mingw_deps_path = self.deps_path / "mingw-deps"
        shutil.rmtree(mingw_deps_path, ignore_errors=True)
        mingw_deps_path.mkdir()

        for triplet in ARCH_TO_TRIPLET.values():
            (mingw_deps_path / triplet).mkdir()

        def extract_filter(member: tarfile.TarInfo, path: str, /):
            if member.name.startswith("SDL"):
                member.name = "/".join(Path(member.name).parts[1:])
            return member
        for dep in self.release_info["dependencies"].keys():
            extract_dir = mingw_deps_path / f"extract-{dep}"
            extract_dir.mkdir()
            with chdir(extract_dir):
                tar_path = glob.glob(self.release_info["mingw"]["dependencies"][dep]["artifact"], root_dir=self.deps_path)[0]
                logger.info("Extracting %s to %s", tar_path, mingw_deps_path)
                with tarfile.open(self.deps_path / tar_path, mode="r:gz") as tarf:
                    tarf.extractall(filter=extract_filter)
                for triplet in ARCH_TO_TRIPLET.values():
                    self.executer.run(["make", "-C", str(extract_dir), "install-package", f"arch={triplet}", f"prefix={str(mingw_deps_path / triplet)}"])

        dep_binpath = mingw_deps_path / triplet / "bin"
        assert dep_binpath.is_dir(), f"{dep_binpath} for PATH should exist"
        dep_pkgconfig = mingw_deps_path / triplet / "lib/pkgconfig"
        assert dep_pkgconfig.is_dir(), f"{dep_pkgconfig} for PKG_CONFIG_PATH should exist"

        new_env = dict(os.environ)
        new_env["PATH"] = os.pathsep.join([str(dep_binpath), new_env["PATH"]])
        new_env["PKG_CONFIG_PATH"] = str(dep_pkgconfig)
        new_env["CFLAGS"] = f"-O2 -ffile-prefix-map={self.root}=/src/{self.project}"
        new_env["CXXFLAGS"] = f"-O2 -ffile-prefix-map={self.root}=/src/{self.project}"

        zip_path = self.dist_path / f"{self.project}-devel-{self.version}-mingw.zip"
        tar_exts = ("gz", "xz")
        tar_paths = { ext: self.dist_path / f"{self.project}-devel-{self.version}-mingw.tar.{ext}" for ext in tar_exts }

        arch_install_paths = {}
        arch_files = {}
        for arch in mingw_archs:
            triplet = ARCH_TO_TRIPLET[arch]
            new_env["CC"] = f"{triplet}-gcc"
            new_env["CXX"] = f"{triplet}-g++"
            new_env["RC"] = f"{triplet}-windres"

            build_path = build_parent_dir / f"build-{triplet}"
            install_path = build_parent_dir / f"install-{triplet}"
            arch_install_paths[arch] = install_path
            shutil.rmtree(install_path, ignore_errors=True)
            build_path.mkdir(parents=True, exist_ok=True)
            with self.section_printer.group(f"Configuring MinGW {triplet}"):
                extra_args = [arg.replace("@DEP_PREFIX@", str(mingw_deps_path / triplet)) for arg in self.release_info["mingw"]["autotools"]["args"]]
                assert "@" not in " ".join(extra_args), f"@ should not be present in extra arguments ({extra_args})"
                self.executer.run([
                    self.root / "configure",
                    f"--prefix={install_path}",
                    f"--includedir={install_path}/include",
                    f"--libdir={install_path}/lib",
                    f"--bindir={install_path}/bin",
                    f"--host={triplet}",
                    f"--build=x86_64-none-linux-gnu",
                ] + extra_args, cwd=build_path, env=new_env)
            with self.section_printer.group(f"Build MinGW {triplet}"):
                self.executer.run(["make", f"-j{self.cpu_count}"], cwd=build_path, env=new_env)
            with self.section_printer.group(f"Install MinGW {triplet}"):
                self.executer.run(["make", "install"], cwd=build_path, env=new_env)
            arch_files[arch] = list(Path(r) / f for r, _, files in os.walk(install_path) for f in files)

        # FIXME: split SDL2.dll debug information into debug library
        # objcopy --only-keep-debug SDL2.dll SDL2.debug.dll
        # objcopy --add-gnu-debuglink=SDL2.debug.dll SDL2.dll
        # objcopy --strip-debug SDL2.dll

        for comp in tar_exts:
            logger.info("Creating %s...", tar_paths[comp])
            with tarfile.open(tar_paths[comp], f"w:{comp}") as tar_object:
                arc_root = f"{self.project}-{self.version}"
                for arch in mingw_archs:
                    triplet = ARCH_TO_TRIPLET[arch]
                    install_path = arch_install_paths[arch]
                    arcname_parent = f"{arc_root}/{triplet}"
                    for file in arch_files[arch]:
                        arcname = os.path.join(arcname_parent, file.relative_to(install_path))
                        logger.debug("Adding %s as %s", file, arcname)
                        tar_object.add(file, arcname=arcname)
                for destdir, files in self.release_info["mingw"]["files"].items():
                    assert destdir[0] == "/" and destdir[-1] == "/", f"'{destir}' must begin and end with '/'"
                    if isinstance(files, str):
                        parent_dir = Path(self.root) / files
                        assert parent_dir.is_dir(), f"{parent_dir} must be a directory"
                        files = list(Path(r) / f for r, _, files in os.walk(parent_dir) for f in files)

                    for file in files:
                        filepath = self.root / file
                        arcname = f"{arc_root}{destdir}{filepath.name}"
                        logger.debug("Adding %s as %s", file, arcname)
                        tar_object.add(filepath, arcname=arcname)

                self._tar_add_git_hash(tar_object=tar_object, root=arc_root)

                self.artifacts[f"mingw-devel-tar-{comp}"] = tar_paths[comp]

    def download_dependencies(self):
        shutil.rmtree(self.deps_path, ignore_errors=True)
        self.deps_path.mkdir(parents=True)

        for dep, depinfo in self.release_info["dependencies"].items():
            startswith = depinfo["startswith"]
            dep_repo = depinfo["repo"]
            dep_tag = self.executer.check_output(["gh", "-R", dep_repo, "release", "list", "--exclude-drafts", "--exclude-pre-releases", "--json", "name,createdAt,tagName", "--jq", f'[.[]|select(.name|startswith("{startswith}"))]|max_by(.createdAt)|.tagName']).strip()
            logger.info("Download %s dependency with tag '%s'", dep, dep_tag)
            self.executer.run(["gh", "-R", dep_repo, "release", "download", dep_tag], cwd=self.deps_path)

    def verify_dependencies(self):
        for dep, dpeinfo in self.release_info["dependencies"].items():
            mingw_matches = glob.glob(self.release_info["mingw"]["dependencies"][dep]["artifact"], root_dir=self.deps_path)
            assert len(mingw_matches) == 1, f"Exactly one archive matches mingw {dep} dependency: {mingw_matches}"
            dmg_matches = glob.glob(self.release_info["dmg"]["dependencies"][dep]["artifact"], root_dir=self.deps_path)
            assert len(dmg_matches) == 1, f"Exactly one archive matches dmg {dep} dependency: {dmg_matches}"
            msvc_matches = glob.glob(self.release_info["msvc"]["dependencies"][dep]["artifact"], root_dir=self.deps_path)
            assert len(msvc_matches) == 1, f"Exactly one archive matches msvc {dep} dependency: {msvc_matches}"


    def build_vs(self, arch_platform: VsArchPlatformConfig, vs: VisualStudio):
        msvc_deps_path = self.deps_path / "msvc-deps"
        shutil.rmtree(msvc_deps_path, ignore_errors=True)
        for dep, depinfo in self.release_info["msvc"]["dependencies"].items():
            msvc_zip = self.deps_path / glob.glob(depinfo["artifact"], root_dir=self.deps_path)[0]

            src_globs = [arch_platform.configure(instr["src"]) for instr in depinfo["copy"]]

            with zipfile.ZipFile(msvc_zip, "r") as zf:
                for member in zf.namelist():
                    member_path = "/".join(Path(member).parts[1:])
                    for src_i, src_glob in enumerate(src_globs):
                        if fnmatch.fnmatch(member_path, src_glob):
                            dst = (self.root / arch_platform.configure(depinfo["copy"][src_i]["dst"])).resolve() /Path(member_path).name
                            if dst.exists():
                                logger.warn("Extracting dependency %s, will cause %s to be overwritten", dep, dst)
                                if not self.overwrite:
                                    raise RuntimeError("Run with --overwrite to allow overwriting")
                            logger.debug("Extracting %s -> %s", member, dst)

                            data = zf.read(member)
                            dst.parent.mkdir(exist_ok=True, parents=True)
                            dst.write_bytes(data)

        assert "msbuild" in self.release_info["msvc"]
        assert "cmake" not in self.release_info["msvc"]
        built_paths = [
            Path(arch_platform.configure(f)) for msbuild_files in self.release_info["msvc"]["msbuild"]["files"] for f in msbuild_files["paths"]
        ]

        for b in built_paths:
            b.unlink(missing_ok=True)

        projects = self.release_info["msvc"]["msbuild"]["projects"]

        with self.section_printer.group(f"Build {arch_platform.arch} VS binary"):
            vs.build(arch_platform=arch_platform, projects=projects)

        if self.dry:
            for b in built_paths:
                b.parent.mkdir(parents=True, exist_ok=True)
                b.touch()

        for b in built_paths:
            assert b.is_file(), f"{b} has not been created"
            b.parent.mkdir(parents=True, exist_ok=True)
            b.touch()

        zip_path = self.dist_path / f"{self.project}-{self.version}-win32-{arch_platform.arch}.zip"
        zip_path.unlink(missing_ok=True)
        logger.info("Creating %s", zip_path)
        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for msbuild_files in self.release_info["msvc"]["msbuild"]["files"]:
                if "lib" in msbuild_files:
                    arcdir = arch_platform.configure(msbuild_files["lib"])
                    for p in msbuild_files["paths"]:
                        p = arch_platform.configure(p)
                        zf.write(self.root / p, arcname=Path(arcdir) / Path(p).name)
            for extra_files in self.release_info["msvc"]["files"]:
                if "lib" in extra_files:
                    arcdir = arch_platform.configure(extra_files["lib"])
                    for p in extra_files["paths"]:
                        p = arch_platform.configure(p)
                        zf.write(self.root / p, arcname=Path(arcdir) / Path(p).name)

            self._zip_add_git_hash(zip_file=zf)
        self.artifacts[f"VC-{arch_platform.arch}"] = zip_path

        for p in built_paths:
            assert p.is_file(), f"{p} should exist"

    def build_vs_devel(self, arch_platforms: list["arch"]) -> None:
        zip_path = self.dist_path / f"{self.project}-devel-{self.version}-VC.zip"
        archive_prefix = f"{self.project}-{self.version}"

        def zip_file(zf: zipfile.ZipFile, path: Path, arcrelpath: str):
            arcname = f"{archive_prefix}/{arcrelpath}"
            logger.debug("Adding %s to %s", path, arcname)
            zf.write(path, arcname=arcname)

        def zip_directory(zf: zipfile.ZipFile, directory: Path, arcrelpath: str):
            for f in directory.iterdir():
                if f.is_file():
                    arcname = f"{archive_prefix}/{arcrelpath}/{f.name}"
                    logger.debug("Adding %s to %s", f, arcname)
                    zf.write(f, arcname=arcname)

        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for msbuild_files in self.release_info["msvc"]["msbuild"]["files"]:
                if "devel" in msbuild_files:
                    for path in msbuild_files["paths"]:
                        if "@" in path or "@" in msbuild_files["devel"]:
                            for arch_platform in arch_platforms:
                                p = arch_platform.configure(path)
                                arcdir = Path(archive_prefix) / arch_platform.configure(msbuild_files["devel"])
                                zf.write(self.root / p, arcname=arcdir / Path(p).name)
                        else:
                            zf.write(self.root / p, arcname=Path(archive_prefix) / msbuild_files["devel"] / Path(p).name)
            for extra_files in self.release_info["msvc"]["files"]:
                if "devel" in extra_files:
                    for path in extra_files["paths"]:
                        if "@" in path or "@" in extra_files["devel"]:
                            for arch_platform in arch_platforms:
                                arcdir = Path(archive_prefix) / arch_platform.configure(extra_files["devel"])
                                p = arch_platform.configure(path)
                                zf.write(self.root / p, arcname=arcdir / Path(p).name)
                        else:
                            zf.write(self.root / path, arcname=Path(archive_prefix) / extra_files["devel"] / Path(path).name)

            self._zip_add_git_hash(zip_file=zf, root=archive_prefix)
        self.artifacts["VC-devel"] = zip_path

    @classmethod
    def extract_sdl_version(cls, root: Path, release_info: dict) -> str:
        with open(root / release_info["version"]["file"], "r") as f:
            text = f.read()
        major = next(re.finditer(release_info["version"]["re_major"], text, flags=re.M)).group(1)
        minor = next(re.finditer(release_info["version"]["re_minor"], text, flags=re.M)).group(1)
        micro = next(re.finditer(release_info["version"]["re_micro"], text, flags=re.M)).group(1)
        return f"{major}.{minor}.{micro}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False, description="Create SDL release artifacts")
    parser.add_argument("--root", metavar="DIR", type=Path, default=Path(__file__).absolute().parents[1], help="Root of project")
    parser.add_argument("--release-info", metavar="JSON", dest="path_release_info", type=Path, default=Path(__file__).absolute().parent / "release-info.json", help="Path of release-info.json")
    parser.add_argument("--dependency-folder", metavar="FOLDER", dest="deps_path", type=Path, default="deps", help="Directory containing pre-built archives of dependencies (will be removed when downloading archives)")
    parser.add_argument("--out", "-o", metavar="DIR", dest="dist_path", type=Path, default="dist", help="Output directory")
    parser.add_argument("--github", action="store_true", help="Script is running on a GitHub runner")
    parser.add_argument("--commit", default="HEAD", help="Git commit/tag of which a release should be created")
    parser.add_argument("--actions", choices=["download", "source", "mingw", "msvc", "dmg"], required=True, action="append", dest="actions", help="What to do?")
    parser.set_defaults(loglevel=logging.INFO)
    parser.add_argument('--vs-year', dest="vs_year", help="Visual Studio year")
    parser.add_argument('--cmake-generator', dest="cmake_generator", default="Ninja", help="CMake Generator")
    parser.add_argument('--debug', action='store_const', const=logging.DEBUG, dest="loglevel", help="Print script debug information")
    parser.add_argument('--dry-run', action='store_true', dest="dry", help="Don't execute anything")
    parser.add_argument('--force', action='store_true', dest="force", help="Ignore a non-clean git tree")
    parser.add_argument('--overwrite', action='store_true', dest="overwrite", help="Allow potentially overwriting other projects")

    args = parser.parse_args(argv)
    logging.basicConfig(level=args.loglevel, format='[%(levelname)s] %(message)s')
    args.deps_path = args.deps_path.absolute()
    args.dist_path = args.dist_path.absolute()
    args.root = args.root.absolute()
    args.dist_path = args.dist_path.absolute()
    if args.dry:
        args.dist_path = args.dist_path / "dry"

    if args.github:
        section_printer: SectionPrinter = GitHubSectionPrinter()
    else:
        section_printer = SectionPrinter()

    executer = Executer(root=args.root, dry=args.dry)

    root_git_hash_path = args.root / GIT_HASH_FILENAME
    root_is_maybe_archive = root_git_hash_path.is_file()
    if root_is_maybe_archive:
        logger.warning("%s detected: Building from archive", GIT_HASH_FILENAME)
        archive_commit = root_git_hash_path.read_text().strip()
        if args.commit != archive_commit:
            logger.warning("Commit argument is %s, but archive commit is %s. Using %s.", args.commit, archive_commit, archive_commit)
        args.commit = archive_commit
    else:
        args.commit = executer.check_output(["git", "rev-parse", args.commit], dry_out="e5812a9fd2cda317b503325a702ba3c1c37861d9").strip()
        logger.info("Using commit %s", args.commit)

    try:
        with args.path_release_info.open() as f:
            release_info = json.load(f)
    except FileNotFoundError:
        log.error(f"Could not find {args.path_release_info}")

    releaser = Releaser(
        release_info=release_info,
        commit=args.commit,
        root=args.root,
        dist_path=args.dist_path,
        executer=executer,
        section_printer=section_printer,
        cmake_generator=args.cmake_generator,
        deps_path=args.deps_path,
        overwrite=args.overwrite,
    )

    if root_is_maybe_archive:
        logger.warning("Building from archive. Skipping clean git tree check.")
    else:
        porcelain_status = executer.check_output(["git", "status", "--ignored", "--porcelain"], dry_out="\n").strip()
        if porcelain_status:
            print(porcelain_status)
            logger.warning("The tree is dirty! Do not publish any generated artifacts!")
            if not args.force:
                raise Exception("The git repo contains modified and/or non-committed files. Run with --force to ignore.")

    with section_printer.group("Arguments"):
        print(f"project          = {releaser.project}")
        print(f"version          = {releaser.version}")
        print(f"commit           = {args.commit}")
        print(f"out              = {args.dist_path}")
        print(f"actions          = {args.actions}")
        print(f"dry              = {args.dry}")
        print(f"force            = {args.force}")
        print(f"overwrite        = {args.overwrite}")
        print(f"cmake_generator  = {args.cmake_generator}")

    releaser.prepare()

    if "download" in args.actions:
        releaser.download_dependencies()

    releaser.verify_dependencies()

    if "source" in args.actions:
        if root_is_maybe_archive:
            raise Exception("Cannot build source archive from source archive")
        with section_printer.group("Create source archives"):
            releaser.create_source_archives()

    if "dmg" in args.actions:
        if platform.system() != "Darwin" and not args.dry:
            parser.error("framework artifact(s) can only be built on Darwin")

        releaser.create_dmg()

    if "msvc" in args.actions:
        if platform.system() != "Windows" and not args.dry:
            parser.error("msvc artifact(s) can only be built on Windows")
        with section_printer.group("Find Visual Studio"):
            vs = VisualStudio(executer=executer)

        arch_platforms = [
            VsArchPlatformConfig(arch="x86", platform="Win32", configuration="Release"),
            VsArchPlatformConfig(arch="x64", platform="x64", configuration="Release"),
        ]
        for arch_platform in arch_platforms:
            releaser.build_vs(arch_platform=arch_platform, vs=vs)
        with section_printer.group("Create SDL VC development zip"):
            releaser.build_vs_devel(arch_platforms)

    if "mingw" in args.actions:
        releaser.create_mingw_archives()

    with section_printer.group("Summary"):
        print(f"artifacts = {releaser.artifacts}")

    if args.github:
        if args.dry:
            os.environ["GITHUB_OUTPUT"] = "/tmp/github_output.txt"
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"project={releaser.project}\n")
            f.write(f"version={releaser.version}\n")
            for k, v in releaser.artifacts.items():
                f.write(f"{k}={v.name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
