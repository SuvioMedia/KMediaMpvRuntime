#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Build the public LGPL libmpv/libplacebo runtime against a KMediaFfmpegRuntime SDK."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = (
    "fast_float", "glslang", "jinja2", "libplacebo", "markupsafe",
    "moltenvk", "mpv", "vulkan_headers",
)
EXTRACTED_COMPONENTS = tuple(component for component in COMPONENTS if component != "moltenvk")
TARGETS = (
    "android-arm64-v8a", "android-armeabi-v7a", "linux-x86_64", "linux-aarch64",
    "macos-aarch64", "windows-x86_64", "ios-arm64", "ios-simulator-arm64",
)
ANDROID = {
    "android-arm64-v8a": ("arm64-v8a", "aarch64-linux-android", "aarch64", "armv8-a"),
    "android-armeabi-v7a": ("armeabi-v7a", "armv7a-linux-androideabi", "arm", "armv7-a"),
}
APPLE = {
    "ios-arm64": ("iphoneos", "arm64-apple-ios16.2"),
    "ios-simulator-arm64": ("iphonesimulator", "arm64-apple-ios16.2-simulator"),
}
APPLE_TARGETS = frozenset({"macos-aarch64", *APPLE})
RUNTIME_ID = re.compile(r"kmediaffmpeg-9\.0\.1-ass-0\.17\.5-[0-9a-f]{16}")


def run(*command: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    effective_command = command
    if env is not None:
        effective_command = ("env", *(f"{name}={value}" for name, value in sorted(env.items())), *command)
    print("+", " ".join(effective_command), flush=True)
    result = subprocess.run(
        effective_command, cwd=cwd, check=False, text=True,
        stdout=subprocess.PIPE, stderr=None,
    )
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, file=sys.stderr, end="")
        result.check_returncode()
    return result.stdout


def build_environment(prefix: Path, target: str) -> dict[str, str]:
    runtime_pkgconfig = os.pathsep.join(
        (str(prefix / "pkgconfig"), str(prefix / "lib/pkgconfig"))
    )
    environment = {
        "LC_ALL": "C",
        "TZ": "UTC",
        "SOURCE_DATE_EPOCH": "1767225600",
        "ZERO_AR_DATE": "1",
        "PKG_CONFIG_PATH": runtime_pkgconfig,
    }
    if not target.startswith("linux-"):
        environment["PKG_CONFIG_LIBDIR"] = runtime_pkgconfig
    if target.startswith("windows-"):
        environment["LIBRARY_PATH"] = str(prefix / "lib")
    if target in APPLE_TARGETS or target.startswith("android-"):
        environment["CPATH"] = str(prefix / "include")
        environment["LIBRARY_PATH"] = str(prefix / "lib")
    return environment


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise ValueError(f"cannot load build helper: {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def load_properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="ISO-8859-1").splitlines():
        if not line or line.startswith(("#", "!")):
            continue
        if "=" not in line:
            raise ValueError("runtime SDK manifest contains a malformed line")
        key, value = line.split("=", 1)
        if not key or key in values or not value:
            raise ValueError("runtime SDK manifest contains duplicate or empty fields")
        values[key] = value
    return values


def safe_extract(archive: Path, output: Path) -> None:
    temporary = output.with_name(output.name + ".extract")
    temporary.mkdir(parents=True)
    with tarfile.open(archive, "r:*") as source:
        for member in source.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise ValueError(f"unsafe source member: {member.name}")
        source.extractall(temporary)
    roots = [path for path in temporary.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise ValueError("source archive must contain one root")
    roots[0].replace(output)
    temporary.rmdir()


def download_input(
    archive: str,
    url: str,
    expected_sha256: str,
    downloads: Path,
    source_archives: Path | None,
) -> Path:
    destination = downloads / archive
    cached = source_archives / archive if source_archives is not None else None
    if cached is not None:
        if not cached.is_file() or cached.is_symlink():
            raise ValueError(f"source/build archive is missing: {cached}")
        shutil.copyfile(cached, destination)
    else:
        request = urllib.request.Request(url, headers={"User-Agent": "KMediaMpv/0.3"})
        with urllib.request.urlopen(request, timeout=120) as response, destination.open("xb") as output:
            shutil.copyfileobj(response, output)
    if sha256(destination) != expected_sha256:
        raise ValueError(f"archive SHA-256 differs from policy: {archive}")
    return destination


def prepare_sources(work: Path, source_archives: Path | None) -> Path:
    downloads = work / "downloads"
    sources = work / "sources"
    downloads.mkdir()
    sources.mkdir()
    for component in COMPONENTS:
        manifest = load_json(ROOT / f"compliance/components/{component}.json")
        archive = download_input(
            manifest["sourceArchive"],
            manifest["sourceUrl"],
            manifest["sourceSha256"],
            downloads,
            source_archives,
        )
        if component in EXTRACTED_COMPONENTS:
            safe_extract(archive, sources / component)
    for name, destination in (
        ("fast_float", "fast_float"), ("jinja2", "jinja"),
        ("markupsafe", "markupsafe"), ("vulkan_headers", "Vulkan-Headers"),
    ):
        target = sources / "libplacebo/3rdparty" / destination
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(sources / name, target, dirs_exist_ok=True)
    module = load_module(
        "kmediampv_patches", ROOT / "native/android/apply_source_patches.py"
    )
    patches = module.apply_patches(sources)
    (work / "source-patches.json").write_text(json.dumps(patches, indent=2) + "\n")
    return sources


def prepare_apple_moltenvk(
    work: Path,
    prefix: Path,
    target: str,
    source_archives: Path | None,
    minimum_os: str,
    sdk_version: str,
) -> None:
    dependency_tool = load_module(
        "kmediampv_apple_dependencies", ROOT / "native/apple/dependency_manifest.py"
    )
    dependency_path = ROOT / "native/apple/dependencies.json"
    dependency_manifest = dependency_tool.load_manifest(dependency_path)
    for name in ("glslang", "moltenvk"):
        component = load_json(ROOT / f"compliance/components/{name}.json")
        dependency = dependency_manifest["components"][name]
        for field in ("version", "sourceUrl", "sourceSha256", "sourceArchive"):
            if component[field] != dependency[field]:
                raise ValueError(f"{name} Apple dependency pin differs from compliance policy")
    binary = dependency_manifest["components"]["moltenvk"]["binaryDistribution"]
    archive = download_input(
        binary["archive"], binary["url"], binary["sha256"],
        work / "downloads", source_archives,
    )
    preparation = load_module(
        "kmediampv_prepare_moltenvk", ROOT / "native/apple/prepare_moltenvk.py"
    )
    preparation.prepare(
        archive,
        prefix,
        target,
        minimum_os,
        sdk_version,
        work / "moltenvk-inventory.json",
    )


def install_vulkan_headers(sources: Path, prefix: Path, target: str) -> None:
    if target not in APPLE_TARGETS and not target.startswith("android-"):
        raise ValueError("Vulkan headers are installed only for Vulkan clients")
    include = sources / "vulkan_headers/include"
    for required in ("vulkan/vulkan_core.h", "vk_video/vulkan_video_codecs_common.h"):
        path = include / required
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Vulkan-Headers source omits {required}")
    shutil.copytree(include, prefix / "include", dirs_exist_ok=True)


def quote(value: str) -> str:
    return "'" + value.replace("'", "\\'") + "'"


def meson_array(values: list[str]) -> str:
    return "[" + ", ".join(quote(item) for item in values) + "]"


def android_cross(path: Path, ndk: Path, target: str, prefix: Path, work: Path) -> dict[str, str]:
    abi, triple, arch, cpu = ANDROID[target]
    host = "darwin-x86_64" if platform.system() == "Darwin" else "linux-x86_64"
    tools = ndk / "toolchains/llvm/prebuilt" / host / "bin"
    api = 28
    values = {
        "c": str(tools / f"{triple}{api}-clang"),
        "cpp": str(tools / f"{triple}{api}-clang++"),
        "ar": str(tools / "llvm-ar"), "nm": str(tools / "llvm-nm"),
        "ranlib": str(tools / "llvm-ranlib"), "strip": str(tools / "llvm-strip"),
        "readelf": str(tools / "llvm-readelf"), "abi": abi,
    }
    common = ["-fPIC", f"-ffile-prefix-map={work}=.", f"-fdebug-prefix-map={work}=."]
    # Meson's generic glslang probe does not pass its explicit library directory. The Android
    # armv7 clang driver also does not honor LIBRARY_PATH for that probe, so keep the audited
    # per-ABI prefix explicit for every cross link.
    link = [
        f"-L{prefix / 'lib'}",
        "-Wl,--build-id=sha1",
        "-Wl,-z,relro",
        "-Wl,-z,now",
        "-Wl,-z,max-page-size=16384",
    ]
    family = "aarch64" if arch == "aarch64" else "arm"
    path.write_text("\n".join([
        "[binaries]", f"c = {quote(values['c'])}", f"cpp = {quote(values['cpp'])}",
        f"ar = {quote(values['ar'])}", f"nm = {quote(values['nm'])}",
        f"ranlib = {quote(values['ranlib'])}", f"strip = {quote(values['strip'])}",
        "pkg-config = 'pkg-config'", "", "[properties]", "needs_exe_wrapper = true",
        f"pkg_config_libdir = {quote(os.pathsep.join((str(prefix / 'pkgconfig'), str(prefix / 'lib/pkgconfig'))))}", "", "[host_machine]",
        "system = 'android'", f"cpu_family = {quote(family)}", f"cpu = {quote(cpu)}",
        "endian = 'little'", "", "[built-in options]", f"c_args = {meson_array(common)}",
        f"cpp_args = {meson_array(common)}", f"c_link_args = {meson_array(link)}",
        f"cpp_link_args = {meson_array(link + ['-static-libstdc++'])}",
    ]) + "\n")
    return values


def xcrun(sdk: str, *arguments: str) -> str:
    return run("xcrun", "--sdk", sdk, *arguments).strip()


def apple_cross(path: Path, target: str, prefix: Path, work: Path) -> dict[str, object]:
    sdk, triple = APPLE[target]
    sysroot = xcrun(sdk, "--show-sdk-path")
    tools = {name: xcrun(sdk, "--find", executable) for name, executable in {
        "c": "clang", "cpp": "clang++", "objc": "clang", "objcpp": "clang++",
        "ar": "ar", "nm": "nm", "ranlib": "ranlib", "strip": "strip",
    }.items()}
    common = ["-target", triple, "-isysroot", sysroot, "-fPIC", f"-ffile-prefix-map={work}=."]
    link = ["-target", triple, "-isysroot", sysroot, "-Wl,-headerpad_max_install_names"]
    path.write_text("\n".join([
        "[binaries]", *(f"{name} = {quote(value)}" for name, value in tools.items()),
        "pkg-config = 'pkg-config'", "", "[properties]", "needs_exe_wrapper = true",
        f"pkg_config_libdir = {quote(os.pathsep.join((str(prefix / 'pkgconfig'), str(prefix / 'lib/pkgconfig'))))}", "", "[host_machine]",
        "system = 'darwin'", "cpu_family = 'aarch64'", "cpu = 'arm64'", "endian = 'little'", "",
        "[built-in options]", f"c_args = {meson_array(common)}", f"cpp_args = {meson_array(common)}",
        f"objc_args = {meson_array(common)}", f"objcpp_args = {meson_array(common)}",
        f"c_link_args = {meson_array(link)}", f"cpp_link_args = {meson_array(link)}",
        f"objc_link_args = {meson_array(link)}", f"objcpp_link_args = {meson_array(link)}",
    ]) + "\n")
    return {
        "sdk": sdk,
        "sysroot": sysroot,
        "tools": tools,
        "minimumOs": "16.2",
        "sdkVersion": xcrun(sdk, "--show-sdk-version"),
    }


def component_arguments(component: str, target: str) -> list[str]:
    arguments = list(load_json(ROOT / f"compliance/components/{component}.json")["buildArguments"])
    if target.startswith("android-"):
        override = load_json(ROOT / "native/android/build-policy.json")["componentOverrides"].get(component, {})
    elif target.startswith("ios-"):
        override = load_json(ROOT / "native/apple/build-policy.json")["componentOverrides"].get(component, {})
    else:
        os_name = target.split("-", 1)[0]
        override = load_json(ROOT / "native/desktop/build-policy.json")["componentOverrides"].get(os_name, {}).get(component, {})
    replacements = override.get("replace", {})
    result = [replacements.get(argument, argument) for argument in arguments]
    result.extend(override.get("append", []))
    return result


def copy_runtime_sdk(sdk: Path, prefix: Path, expected_version: str) -> dict[str, str]:
    manifest = load_properties(sdk / "runtime.properties")
    if not RUNTIME_ID.fullmatch(manifest.get("runtimeId", "")):
        raise ValueError("runtime SDK has an invalid runtime ID")
    if manifest.get("version.ffmpeg") != "9.0.1" or manifest.get("version.libass") != "0.17.5":
        raise ValueError("runtime SDK component versions differ from the client contract")
    if manifest.get("distributionVersion") != expected_version:
        raise ValueError("runtime SDK distribution version differs from the client contract")
    for directory in ("include", "lib"):
        source = sdk / directory
        if not source.is_dir() or source.is_symlink():
            raise ValueError(f"runtime SDK omits {directory}")
        shutil.copytree(source, prefix / directory, dirs_exist_ok=True)
    if (sdk / "pkgconfig").is_dir():
        shutil.copytree(sdk / "pkgconfig", prefix / "pkgconfig", dirs_exist_ok=True)
    if manifest.get("platform") == "windows":
        for component in (
            "avcodec", "avfilter", "avformat", "avutil", "swresample", "swscale",
        ):
            canonical = prefix / "lib" / f"libkmediaffmpeg_{component}.dll.a"
            if canonical.is_file():
                continue
            generated = prefix / "lib" / f"liblibkmediaffmpeg_{component}.dll.a"
            if not generated.is_file():
                raise ValueError(f"runtime SDK omits the {component} import library")
            shutil.copyfile(generated, canonical)
    return manifest


def build_meson(component: str, sources: Path, work: Path, prefix: Path, target: str, cross: Path | None, env: dict[str, str]) -> None:
    command = [
        "meson", "setup", str(work / f"build-{component}"), str(sources / component),
        "--prefix", str(prefix), *component_arguments(component, target),
    ]
    if cross is not None:
        command.extend(["--cross-file", str(cross)])
    if component == "mpv" and target.startswith("windows-"):
        # Header-only ANGLE dispatch: the embedding player supplies its EGL DLL.
        headers = ROOT / "native/windows/include"
        for relative, expected in load_json(ROOT / "native/windows/headers.json")["sha256"].items():
            if sha256(headers / relative) != expected:
                raise ValueError(f"ANGLE header checksum differs: {relative}")
        command.append(f"-Dc_args=-I{headers.as_posix()}")
    run(*command, env=env)
    run("meson", "compile", "-C", str(work / f"build-{component}"), "-j", str(os.cpu_count() or 4), env=env)
    run("meson", "install", "-C", str(work / f"build-{component}"), env=env)
    if component == "libplacebo" and (
        target in APPLE_TARGETS or target.startswith("android-")
    ):
        config = work / "build-libplacebo/src/include/libplacebo/config.h"
        text = config.read_text(encoding="utf-8")
        required_capabilities = ["#define PL_HAVE_GLSLANG 1", "#define PL_HAVE_VULKAN 1"]
        if target in APPLE_TARGETS:
            required_capabilities.append("#define PL_HAVE_VK_PROC_ADDR 1")
        for required in required_capabilities:
            if required not in text.splitlines():
                raise ValueError(f"Vulkan libplacebo capability missing: {required}")


def build_glslang(
    sources: Path,
    work: Path,
    prefix: Path,
    target: str,
    env: dict[str, str],
    apple_metadata: dict[str, object] | None,
    ndk: Path | None,
    android_values: dict[str, str] | None,
    configured_cmake: Path | None,
) -> None:
    if target not in APPLE_TARGETS and not target.startswith("android-"):
        raise ValueError("glslang is built only for Vulkan clients")
    cmake = (
        os.fspath(configured_cmake.resolve())
        if configured_cmake is not None
        else shutil.which("cmake")
    )
    if cmake is None:
        raise ValueError("cmake is required for the glslang build")
    if target.startswith("android-"):
        if ndk is None or android_values is None:
            raise ValueError("Android glslang build requires NDK metadata")
        system_arguments = [
            f"-DCMAKE_TOOLCHAIN_FILE={ndk / 'build/cmake/android.toolchain.cmake'}",
            f"-DANDROID_ABI={android_values['abi']}",
            "-DANDROID_PLATFORM=android-28",
            "-DANDROID_STL=c++_static",
        ]
        tool_arguments: list[str] = []
    elif target == "macos-aarch64":
        sdk = "macosx"
        sysroot = xcrun(sdk, "--show-sdk-path")
        minimum_os = load_json(ROOT / "native/desktop/build-policy.json")["toolchain"]["macosDeploymentTarget"]
        tools = {
            name: xcrun(sdk, "--find", executable)
            for name, executable in {
                "c": "clang", "cpp": "clang++", "ar": "ar",
                "ranlib": "ranlib", "strip": "strip",
            }.items()
        }
        system_arguments: list[str] = []
        tool_arguments = [
            f"-DCMAKE_OSX_SYSROOT={sysroot}",
            "-DCMAKE_OSX_ARCHITECTURES=arm64",
            f"-DCMAKE_OSX_DEPLOYMENT_TARGET={minimum_os}",
            f"-DCMAKE_C_COMPILER={tools['c']}",
            f"-DCMAKE_CXX_COMPILER={tools['cpp']}",
            f"-DCMAKE_AR={tools['ar']}",
            f"-DCMAKE_RANLIB={tools['ranlib']}",
            f"-DCMAKE_STRIP={tools['strip']}",
        ]
    else:
        if apple_metadata is None:
            raise ValueError("iOS glslang build requires Apple cross metadata")
        sysroot = str(apple_metadata["sysroot"])
        minimum_os = str(apple_metadata["minimumOs"])
        tools = apple_metadata["tools"]
        if not isinstance(tools, dict):
            raise ValueError("Apple tool metadata is invalid")
        system_arguments = ["-DCMAKE_SYSTEM_NAME=iOS"]
        tool_arguments = [
            f"-DCMAKE_OSX_SYSROOT={sysroot}",
            "-DCMAKE_OSX_ARCHITECTURES=arm64",
            f"-DCMAKE_OSX_DEPLOYMENT_TARGET={minimum_os}",
            f"-DCMAKE_C_COMPILER={tools['c']}",
            f"-DCMAKE_CXX_COMPILER={tools['cpp']}",
            f"-DCMAKE_AR={tools['ar']}",
            f"-DCMAKE_RANLIB={tools['ranlib']}",
            f"-DCMAKE_STRIP={tools['strip']}",
        ]
    flags = f"-O3 -DNDEBUG -ffile-prefix-map={work}=. -fdebug-prefix-map={work}=."
    arguments = [
        cmake, "-S", str(sources / "glslang"),
        "-B", str(work / "build-glslang"), "-G", "Ninja",
        *component_arguments("glslang", target),
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_INSTALL_PREFIX={prefix}",
        "-DCMAKE_INSTALL_LIBDIR=lib",
        "-DCMAKE_INSTALL_INCLUDEDIR=include",
        *tool_arguments,
        "-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
        f"-DCMAKE_C_FLAGS_RELEASE={flags}",
        f"-DCMAKE_CXX_FLAGS_RELEASE={flags}",
        *system_arguments,
    ]
    run(*arguments, env=env)
    run(cmake, "--build", str(work / "build-glslang"), "--parallel", str(os.cpu_count() or 4), env=env)
    run(cmake, "--install", str(work / "build-glslang"), env=env)
    for required in ("libglslang.a", "libSPIRV.a"):
        if not (prefix / "lib" / required).is_file():
            raise ValueError(f"glslang install omitted {required}")


def find_client_library(prefix: Path, logical: str, target: str) -> Path:
    roots = (prefix / "bin", prefix / "lib") if target.startswith("windows-") else (prefix / "lib",)
    candidates = [
        path for root in roots if root.is_dir() for path in root.glob(f"*kmediampv_{logical}*")
        if path.is_file() and not path.is_symlink() and not path.name.endswith((".a", ".dll.a", ".lib"))
    ]
    if len(candidates) != 1:
        raise ValueError(f"{logical}: expected one client shared library, got {[p.name for p in candidates]}")
    return candidates[0]


def client_name(logical: str, target: str, source: Path) -> str:
    if target.startswith("windows-"):
        return source.name
    if target.startswith(("macos-", "ios-")):
        return f"libkmediampv_{logical}.dylib"
    return f"libkmediampv_{logical}.so"


def collect_clients(prefix: Path, output: Path, target: str) -> list[str]:
    runtime = output / "runtime"
    runtime.mkdir()
    names: list[str] = []
    originals: dict[str, str] = {}
    logical_clients = ["placebo", "mpv"]
    if target in APPLE_TARGETS:
        logical_clients.append("moltenvk")
    for logical in logical_clients:
        source = find_client_library(prefix, logical, target)
        name = client_name(logical, target, source)
        shutil.copyfile(source, runtime / name)
        originals[source.name] = name
        names.append(name)
    if target.startswith(("linux-", "android-")) and shutil.which("patchelf"):
        for name in names:
            run("patchelf", "--set-soname", name, str(runtime / name))
            for dependency in run("patchelf", "--print-needed", str(runtime / name)).splitlines():
                if dependency in originals and originals[dependency] != dependency:
                    run("patchelf", "--replace-needed", dependency, originals[dependency], str(runtime / name))
            if target.startswith("linux-"):
                run("patchelf", "--set-rpath", "$ORIGIN", str(runtime / name))
    elif target.startswith(("macos-", "ios-")):
        for name in names:
            path = runtime / name
            run("install_name_tool", "-id", f"@rpath/{name}", str(path))
            if target.startswith("macos-"):
                run("install_name_tool", "-add_rpath", "@loader_path", str(path))
            dependencies = [line.strip().split(" (", 1)[0] for line in run("otool", "-L", str(path)).splitlines()[1:]]
            for dependency in dependencies:
                replacement = originals.get(Path(dependency).name)
                if replacement:
                    run("install_name_tool", "-change", dependency, f"@rpath/{replacement}", str(path))
        if target.startswith("macos-"):
            for name in names:
                run(
                    "codesign", "--force", "--sign", "-", "--timestamp=none",
                    str(runtime / name),
                )
                run("codesign", "--verify", "--strict", str(runtime / name))
    return names


def write_manifest(
    output: Path,
    target: str,
    runtime: dict[str, str],
    libraries: list[str],
    revision: str,
    source_offer: str,
) -> None:
    components = {
        "kmediampv_placebo": "libplacebo",
        "kmediampv_mpv": "mpv",
        "kmediampv_moltenvk": "moltenvk",
    }
    libmpv = next(name for name in libraries if name.startswith("libkmediampv_mpv"))
    lines = [
        "schemaVersion=1", f"platform={target}", "licenseSpdx=LGPL-2.1-or-later",
        "releaseEligible=true", "audioOutputs=true", f"recipeRevision={revision}",
        f"sourceOffer={source_offer}",
        f"sharedRuntimeId={runtime['runtimeId']}",
        f"sharedRuntimeVersion={runtime['distributionVersion']}", "mpvVersion=0.41.0",
        "libplaceboVersion=7.360.1",
        f"libmpv.name={libmpv}", f"libmpv.sha256={sha256(output / 'runtime' / libmpv)}",
        f"library.count={len(libraries)}",
    ]
    if target in APPLE_TARGETS:
        lines.append("moltenVkVersion=1.4.2")
    for index, library in enumerate(libraries):
        logical = library.removeprefix("lib").split(".", 1)[0]
        component = components[logical]
        path = output / "runtime" / library
        lines.extend([
            f"library.{index}.name={library}",
            f"library.{index}.component={component}",
            f"library.{index}.size={path.stat().st_size}",
            f"library.{index}.sha256={sha256(path)}",
        ])
    manifest = "\n".join(lines) + "\n"
    (output / "manifest.properties").write_bytes(manifest.encode("utf-8"))


def package_apple(output: Path, target: str, version: str) -> None:
    frameworks = output / "Frameworks"
    frameworks.mkdir()
    mapping = {
        "placebo": "KMediaMpvPlacebo",
        "mpv": "KMediaMpv",
        "moltenvk": "KMediaMpvMoltenVK",
    }
    binaries: dict[str, tuple[Path, str]] = {}
    shared_runtime_frameworks = {
        "libkmediaffmpeg_avcodec.dylib": "KMediaFfmpegAvcodec",
        "libkmediaffmpeg_avfilter.dylib": "KMediaFfmpegAvfilter",
        "libkmediaffmpeg_avformat.dylib": "KMediaFfmpegAvformat",
        "libkmediaffmpeg_avutil.dylib": "KMediaFfmpegAvutil",
        "libkmediaffmpeg_swresample.dylib": "KMediaFfmpegSwresample",
        "libkmediaffmpeg_swscale.dylib": "KMediaFfmpegSwscale",
        "libkmediaffmpeg_freetype.dylib": "KMediaFfmpegFreetype",
        "libkmediaffmpeg_fribidi.dylib": "KMediaFfmpegFribidi",
        "libkmediaffmpeg_harfbuzz.dylib": "KMediaFfmpegHarfbuzz",
        "libkmediaffmpeg_ass.dylib": "KMediaFfmpegAss",
    }
    for logical, name in mapping.items():
        source_name = f"libkmediampv_{logical}.dylib"
        framework = frameworks / f"{name}.framework"
        (framework / "Headers").mkdir(parents=True)
        (framework / "Modules").mkdir()
        binary = framework / name
        shutil.copyfile(output / "runtime" / source_name, binary)
        binary.chmod(0o755)
        header = f"{name}.h"
        if logical == "mpv":
            for source in sorted((output / "sdk" / target / "include/mpv").glob("*.h")):
                shutil.copyfile(source, framework / "Headers" / source.name)
            (framework / "Headers" / header).write_text("#pragma once\n#include <mpv/client.h>\n")
        else:
            (framework / "Headers" / header).write_text("#pragma once\n")
        (framework / "Modules/module.modulemap").write_text(
            f"framework module {name} {{\n  umbrella header \"{header}\"\n  export *\n}}\n")
        plist = {
            "CFBundleDevelopmentRegion": "en", "CFBundleExecutable": name,
            "CFBundleIdentifier": f"cc.suviomedia.kmediampv.{name.lower()}",
            "CFBundleInfoDictionaryVersion": "6.0", "CFBundleName": name,
            "CFBundlePackageType": "FMWK", "CFBundleShortVersionString": version,
            "CFBundleVersion": "1", "MinimumOSVersion": "16.2",
        }
        with (framework / "Info.plist").open("wb") as destination:
            plistlib.dump(plist, destination, sort_keys=True)
        binaries[source_name] = (binary, name)
    for source_name, (binary, name) in binaries.items():
        run("install_name_tool", "-id", f"@rpath/{name}.framework/{name}", str(binary))
        dependencies = [line.strip().split(" (", 1)[0] for line in run("otool", "-L", str(binary)).splitlines()[1:]]
        for dependency in dependencies:
            match = binaries.get(Path(dependency).name)
            if match:
                run("install_name_tool", "-change", dependency, f"@rpath/{match[1]}.framework/{match[1]}", str(binary))
                continue
            shared_framework = shared_runtime_frameworks.get(Path(dependency).name)
            if shared_framework:
                run(
                    "install_name_tool", "-change", dependency,
                    f"@rpath/{shared_framework}.framework/{shared_framework}", str(binary),
                )


def validate_host(target: str) -> None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if target == "linux-x86_64" and not (system == "linux" and machine in {"x86_64", "amd64"}):
        raise ValueError("linux-x86_64 requires a matching host")
    if target == "linux-aarch64" and not (system == "linux" and machine in {"aarch64", "arm64"}):
        raise ValueError("linux-aarch64 requires a matching host")
    if target == "macos-aarch64" and not (system == "darwin" and machine in {"aarch64", "arm64"}):
        raise ValueError("macOS client requires Apple Silicon")
    if target == "windows-x86_64" and not (
        system.startswith(("windows", "mingw", "msys")) and machine in {"x86_64", "amd64"}
    ):
        raise ValueError("Windows x86_64 client requires a matching MSYS2 UCRT64 host")
    if target.startswith("ios-") and system != "darwin":
        raise ValueError("iOS client requires macOS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--runtime-sdk", type=Path, required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-offer")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ndk", type=Path)
    parser.add_argument("--cmake", type=Path)
    parser.add_argument(
        "--source-archives", type=Path,
        help="offline directory containing the exact source archives named by compliance manifests",
    )
    arguments = parser.parse_args()
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?", arguments.version):
        raise ValueError("version must be immutable SemVer")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?", arguments.runtime_version):
        raise ValueError("runtime version must be immutable SemVer")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", arguments.revision):
        raise ValueError("revision must be a full lowercase Git object ID")
    source_offer = arguments.source_offer or (
        "https://github.com/SuvioMedia/KMediaMpvRuntime/releases/download/"
        f"v{arguments.version}/kmedia-mpv-runtime-{arguments.version}-corresponding-source.tar.gz"
    )
    if not source_offer.startswith("https://") or any(character.isspace() for character in source_offer):
        raise ValueError("source offer must be an HTTPS URL without whitespace")
    validate_host(arguments.target)
    work = arguments.work.resolve()
    output = arguments.output.resolve()
    if work.exists() or output.exists():
        raise ValueError("work and output must not already exist")
    work.mkdir(parents=True)
    output.mkdir(parents=True)
    prefix = work / "prefix"
    prefix.mkdir()
    runtime = copy_runtime_sdk(arguments.runtime_sdk.resolve(), prefix, arguments.runtime_version)
    sources = prepare_sources(
        work,
        arguments.source_archives.resolve() if arguments.source_archives is not None else None,
    )
    cross: Path | None = None
    android_values: dict[str, str] | None = None
    apple_metadata: dict[str, object] | None = None
    if arguments.target.startswith("android-"):
        if arguments.ndk is None:
            raise ValueError("--ndk is required for Android")
        cross = work / "android.ini"
        android_values = android_cross(cross, arguments.ndk.resolve(), arguments.target, prefix, work)
    elif arguments.target.startswith("ios-"):
        cross = work / "apple.ini"
        apple_metadata = apple_cross(cross, arguments.target, prefix, work)
    env = build_environment(prefix, arguments.target)
    if arguments.target in APPLE_TARGETS or arguments.target.startswith("android-"):
        install_vulkan_headers(sources, prefix, arguments.target)
    if arguments.target in APPLE_TARGETS:
        if arguments.target == "macos-aarch64":
            minimum_os = str(
                load_json(ROOT / "native/desktop/build-policy.json")["toolchain"]["macosDeploymentTarget"]
            )
            sdk_version = xcrun("macosx", "--show-sdk-version")
        else:
            if apple_metadata is None:
                raise ValueError("iOS target omitted Apple cross metadata")
            minimum_os = str(apple_metadata["minimumOs"])
            sdk_version = str(apple_metadata["sdkVersion"])
        prepare_apple_moltenvk(
            work,
            prefix,
            arguments.target,
            arguments.source_archives.resolve() if arguments.source_archives is not None else None,
            minimum_os,
            sdk_version,
        )
    if arguments.target in APPLE_TARGETS or arguments.target.startswith("android-"):
        build_glslang(
            sources,
            work,
            prefix,
            arguments.target,
            env,
            apple_metadata,
            arguments.ndk.resolve() if arguments.ndk is not None else None,
            android_values,
            arguments.cmake,
        )
    build_meson("libplacebo", sources, work, prefix, arguments.target, cross, env)
    build_meson("mpv", sources, work, prefix, arguments.target, cross, env)
    libraries = collect_clients(prefix, output, arguments.target)
    write_manifest(output, arguments.target, runtime, libraries, arguments.revision, source_offer)
    sdk = output / "sdk" / arguments.target
    shutil.copytree(prefix / "include/mpv", sdk / "include/mpv", dirs_exist_ok=True)
    shutil.copytree(output / "runtime", sdk / "lib", dirs_exist_ok=True)
    shutil.copyfile(output / "manifest.properties", sdk / "manifest.properties")
    if arguments.target.startswith("ios-"):
        package_apple(output, arguments.target, arguments.version)
    evidence = output / "compliance"
    (evidence / "sources").mkdir(parents=True)
    for component in COMPONENTS:
        manifest = load_json(ROOT / f"compliance/components/{component}.json")
        shutil.copyfile(work / "downloads" / manifest["sourceArchive"], evidence / "sources" / manifest["sourceArchive"])
    shutil.copyfile(work / "source-patches.json", evidence / "source-patches.json")
    shutil.copyfile(arguments.runtime_sdk.resolve() / "runtime.properties", evidence / "kmediaffmpeg-runtime.properties")
    if arguments.target in APPLE_TARGETS:
        shutil.copyfile(
            ROOT / "native/apple/dependencies.json",
            evidence / "apple-dependencies.json",
        )
        shutil.copyfile(
            work / "moltenvk-inventory.json",
            evidence / "moltenvk-inventory.json",
        )
    (evidence / "target.json").write_text(json.dumps({"schemaVersion": 1, "target": arguments.target}, indent=2) + "\n")
    verification = [
        os.fspath(Path(os.sys.executable)), "-B", os.fspath(ROOT / "scripts/verify_client_output.py"),
        "--output", os.fspath(output), "--target", arguments.target,
    ]
    if arguments.target.startswith("android-"):
        assert android_values is not None
        verification.extend(["--readelf", android_values["readelf"]])
    run(*verification)
    print(runtime["runtimeId"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
