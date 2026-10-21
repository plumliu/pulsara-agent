"""Prepare native desktop payloads; build tools are never runtime dependencies.

Upstream PostgreSQL owns its relocatable layout, PGXS builds pgvector, pkgutil /
dpkg-deb extract the exact ParadeDB release without running installer scripts,
and dylibbundler / the native Linux loader / patchelf own dependency relocation.
Run natively on macOS ARM64 or Ubuntu 24.04 x86_64/ARM64; no cross compilation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
RESOURCES = ROOT / "resources"
PG_VERSION = "17.9"
PG_SHA256 = "3b9a62538a8da151e807a3ddb1198e8605f2032544d78f403ae883d27ecf1ee4"
VECTOR_VERSION = "0.8.1"
VECTOR_SHA256 = "a9094dfb85ccdde3cbb295f1086d4c71a20db1d26bf1d6c39f07a7d164033eb4"
SEARCH_VERSION = "0.26.1"
# Official release assets for PG17, not the similarly named PG18 packages.
# https://github.com/paradedb/paradedb/releases/tag/v0.26.1
SEARCH_ASSETS = {
    ("darwin", "arm64"): (
        "pg_search%4017--0.26.1.arm64_sequoia.pkg",
        "f37bd43fd4929fba53b29776921d054d015a76dbcf4ea536bd11a5075890cc5d",
    ),
    ("linux", "x86_64"): (
        "postgresql-17-pg-search_0.26.1-1PARADEDB-noble_amd64.deb",
        "66927e09a7112b9f103c4f98a31a3b82824278e906214d752d687964a2100f76",
    ),
    ("linux", "aarch64"): (
        "postgresql-17-pg-search_0.26.1-1PARADEDB-noble_arm64.deb",
        "e75bc7472eed871cccc5a6d8719af0accb1d116057877b187ffe21c21fb82e4e",
    ),
}
SEARCH_SOURCE_SHA256 = (
    "de5ec85d2193e9fd665db44729135059dab9527ab982c0470e29cceac3f6643e"
)
SEARCH_LICENSE_SHA256 = (
    "8486a10c4393cee1c25392769ddd3b2d6c242d6ec7928e1414efff7dfb2f07ef"
)


def run(args, **kwargs):
    subprocess.run([str(v) for v in args], check=True, **kwargs)


def output(args, **kwargs):
    return subprocess.check_output([str(v) for v in args], text=True, **kwargs).strip()


def target_platform():
    target = (sys.platform, platform.machine())
    if target not in SEARCH_ASSETS:
        raise RuntimeError("Build natively on macOS ARM64 or Ubuntu 24.04 x86_64/ARM64")
    if sys.platform == "linux":
        release = platform.freedesktop_os_release()
        if (release.get("ID"), release.get("VERSION_ID")) != ("ubuntu", "24.04"):
            raise RuntimeError("Linux desktop payloads must be built on Ubuntu 24.04")
    elif int(platform.mac_ver()[0].split(".")[0]) < 15:
        raise RuntimeError(
            "The pinned pg_search Sequoia payload requires macOS 15 or newer"
        )
    return target


def download(url: str, destination: Path, sha256: str):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        pending = destination.with_name(destination.name + ".part")
        urllib.request.urlretrieve(url, pending)
        with pending.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != sha256:
                pending.unlink()
                raise RuntimeError(f"Upstream archive checksum mismatch: {url}")
        pending.replace(destination)
    with destination.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != sha256:
            raise RuntimeError(
                f"Cached upstream archive checksum mismatch: {destination}"
            )
    return destination


def source_tree(url: str, archive: Path, sha256: str, folder: Path):
    download(url, archive, sha256)
    if not folder.exists():
        with tarfile.open(archive) as bundle:
            bundle.extractall(folder.parent, filter="data")
    return folder


def native_files(root: Path):
    """Inspect all programs/modules, including initdb, psql and nested extension dirs."""
    magic = {b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(4) in magic:
                    yield path


def validate_architecture(path: Path):
    if sys.platform == "darwin":
        arches = output(["lipo", "-archs", path]).split()
        if "arm64" not in arches:
            raise RuntimeError(f"Expected ARM64 Mach-O: {path}: {arches}")
        # Apple's otool treats a trailing "(Alerts)" helper filename as an
        # archive member. vtool owns deployment metadata and accepts these paths.
        load_commands = output(["xcrun", "vtool", "-show-build", path])
        minimums = re.findall(r"\bminos\s+(\d+\.\d+(?:\.\d+)?)", load_commands)
        minimums += re.findall(
            r"LC_VERSION_MIN_MACOSX\s+cmdsize\s+\d+\s+version\s+(\d+\.\d+(?:\.\d+)?)",
            load_commands,
        )
        if any(
            tuple(map(int, version.split("."))) > (15, 0, 0) for version in minimums
        ):
            raise RuntimeError(
                f"Mach-O requires newer than supported macOS 15: {path}: {minimums}"
            )
    else:
        header = output(["readelf", "-h", path])
        machine = (
            "Advanced Micro Devices X86-64"
            if platform.machine() == "x86_64"
            else "AArch64"
        )
        if "ELF64" not in header or not re.search(
            r"Machine:\s*" + re.escape(machine) + r"\s*$", header, re.M
        ):
            raise RuntimeError(f"Wrong ELF architecture: {path}")


def pg_layout(target: Path):
    config = target / "bin" / "pg_config"
    if output([config, "--version"]) != f"PostgreSQL {PG_VERSION}":
        raise RuntimeError("PostgreSQL build version does not match the desktop pin")
    lib = Path(output([config, "--pkglibdir"]))
    share = Path(output([config, "--sharedir"])) / "extension"
    if not lib.is_relative_to(target) or not share.is_relative_to(target):
        raise RuntimeError("pg_config points outside the bundled PostgreSQL layout")
    return lib, share


def install_extension(
    name: str,
    version: str,
    source_lib: Path,
    source_share: Path,
    lib: Path,
    share: Path,
):
    control = source_share / f"{name}.control"
    if not re.search(
        r"^default_version\s*=\s*'" + re.escape(version) + r"'\s*$",
        control.read_text(),
        re.M,
    ):
        raise RuntimeError(f"Expected {name} {version}: {control}")
    sql = source_share / f"{name}--{version}.sql"
    if not sql.is_file():
        raise RuntimeError(f"Missing clean installation SQL: {sql}")
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    binary = source_lib / f"{name}{suffix}"
    validate_architecture(binary)
    if binary != lib / binary.name:
        shutil.copy2(binary, lib)
    if control != share / control.name:
        shutil.copy2(control, share)
        for script in source_share.glob(f"{name}--*.sql"):
            shutil.copy2(script, share)


def upstream_extensions(build: Path, target: Path, lib: Path, share: Path):
    name, digest = SEARCH_ASSETS[target_platform()]
    archive = download(
        f"https://github.com/paradedb/paradedb/releases/download/v{SEARCH_VERSION}/{name}",
        build / name.replace("%40", "@"),
        digest,
    )
    extracted = build / f"pg-search-{sys.platform}-{platform.machine()}"
    if extracted.exists():
        shutil.rmtree(extracted)
    if sys.platform == "darwin":
        run(["pkgutil", "--expand-full", archive, extracted])
        payload = extracted / "Payload"
        search_lib = payload / "lib" / "postgresql"
        search_share = payload / "share" / "postgresql@17" / "extension"
    else:
        expected_arch = "amd64" if platform.machine() == "x86_64" else "arm64"
        for field, expected in (
            ("Package", "postgresql-17-pg-search"),
            ("Architecture", expected_arch),
            ("Version", SEARCH_VERSION),
        ):
            actual = output(["dpkg-deb", "--field", archive, field])
            if actual != expected:
                raise RuntimeError(f"Unexpected ParadeDB Debian {field}: {actual}")
        run(["dpkg-deb", "--extract", archive, extracted])
        search_lib = extracted / "usr" / "lib" / "postgresql" / "17" / "lib"
        search_share = extracted / "usr" / "share" / "postgresql" / "17" / "extension"
    install_extension("pg_search", SEARCH_VERSION, search_lib, search_share, lib, share)
    vector = source_tree(
        f"https://codeload.github.com/pgvector/pgvector/tar.gz/refs/tags/v{VECTOR_VERSION}",
        build / f"pgvector-{VECTOR_VERSION}.tar.gz",
        VECTOR_SHA256,
        build / f"pgvector-{VECTOR_VERSION}",
    )
    # PGXS owns compilation and installation. Upstream documents OPTFLAGS=""
    # for portable binaries rather than the build CPU's -march=native tuning.
    run(["make", "clean", f"PG_CONFIG={target / 'bin' / 'pg_config'}"], cwd=vector)
    run(
        [
            "make",
            f"-j{os.cpu_count() or 2}",
            f"PG_CONFIG={target / 'bin' / 'pg_config'}",
            "OPTFLAGS=",
        ],
        cwd=vector,
    )
    run(
        ["make", "install", f"PG_CONFIG={target / 'bin' / 'pg_config'}", "OPTFLAGS="],
        cwd=vector,
    )


def system_elf(path: Path):
    # Ubuntu's libc + ELF loader remain OS-owned; copying another private libc
    # would violate its ABI contract. Everything else in the dependency tree ships.
    return bool(
        re.match(
            r"^(ld-linux[^/]*|lib(?:c|m|pthread|dl|rt|resolv|util|anl)\.so(?:\..*)?)$",
            path.name,
        )
    )


def linux_dependencies(binary: Path, *, search_siblings: bool = False) -> list[Path]:
    """Let Ubuntu's loader resolve the complete dependency tree of build payloads.

    python-build-standalone uses $ORIGIN in DT_NEEDED, which Ubuntu 24.04's
    lddtree 1.3.7 does not resolve. ldd delegates to the native glibc loader,
    preserving that binding without rewriting ELF dependencies or losing their
    transitive libraries. Only our native, prepared build payloads reach here.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("LD_")}
    env["LC_ALL"] = "C"
    if search_siblings:
        # Wheel-internal libraries can inherit the importing extension's RPATH.
        # Inspect them independently without modifying the upstream ELF first.
        env["LD_LIBRARY_PATH"] = str(binary.parent)
    try:
        result = output(["/usr/bin/ldd", binary], env=env)
    except UnicodeDecodeError as error:
        raise RuntimeError(
            f"Invalid loader output for {binary}: {error.object!r}"
        ) from error
    dependencies = []
    for line in result.splitlines():
        entry = line.strip()
        if re.fullmatch(r"linux-(?:vdso|gate)\.so\.\d+\s+\(0x[0-9a-fA-F]+\)", entry):
            continue  # Kernel-provided virtual library, not a redistributable file.
        location = entry.split(" => ", 1)[-1]
        match = re.fullmatch(r"(/.+)\s+\(0x[0-9a-fA-F]+\)", location)
        if not match:
            raise RuntimeError(f"Unresolved native dependency for {binary}: {line}")
        dependency = Path(os.path.normpath(match[1].rstrip()))
        if not dependency.is_file():
            raise RuntimeError(f"Unresolved native dependency for {binary}: {line}")
        dependencies.append(dependency)
    return dependencies


def bundle_linux_dependencies(target: Path):
    originals = list(native_files(target))
    # Collect first, then patch each ELF once. Ubuntu's patchelf 0.18 corrupts
    # Pillow x64 libXau version data when a second RPATH edit grows .dynstr.
    shared = {}
    local_directories = {}
    for binary in originals:
        # Statically linked tools (including ripgrep) have no loader dependencies.
        if "(NEEDED)" not in output(["readelf", "-d", binary]):
            continue
        local_directories[binary] = {binary.parent}
        validate_architecture(binary)
        for candidate in linux_dependencies(binary, search_siblings=True):
            if candidate.is_relative_to(target):
                local_directories[binary].add(candidate.parent)
                continue
            if system_elf(candidate):
                continue
            prior = shared.get(candidate.name)
            if prior and prior.resolve() != candidate.resolve():
                raise RuntimeError(
                    f"Conflicting dependency SONAME {candidate.name}: {prior}, {candidate}"
                )
            shared[candidate.name] = candidate
    (target / "lib").mkdir(exist_ok=True)
    for dependency in shared.values():
        shutil.copy2(dependency, target / "lib" / dependency.name)
        # Debian packages own third-party license notices for copied system libs.
        # Ubuntu's merged-/usr filesystem can resolve /lib paths differently
        # from the package database. Ask for the shipped path before its realpath.
        package = None
        for name in dict.fromkeys(
            (
                str(dependency),
                str(dependency.resolve()),
                str(dependency).removeprefix("/usr"),
            )
        ):
            query = subprocess.run(
                ["dpkg-query", "--search", name], capture_output=True, text=True
            )
            if query.returncode == 0:
                package = query.stdout.split(": ", 1)[0].split(":", 1)[0]
                break
        if package is None:
            raise RuntimeError(
                f"No Debian package owns bundled dependency: {dependency}"
            )
        copyright_file = Path("/usr/share/doc") / package / "copyright"
        if not copyright_file.is_file():
            raise RuntimeError(
                f"Missing redistribution notice for {dependency}: {copyright_file}"
            )
        shutil.copy2(copyright_file, RESOURCES / "licenses" / f"{package}-copyright")
    for binary in native_files(target):
        validate_architecture(binary)
        if "(NEEDED)" not in output(["readelf", "-d", binary]):
            continue
        directories = {target / "lib", *local_directories.get(binary, ())}
        paths = [
            "$ORIGIN/" + os.path.relpath(directory, binary.parent)
            for directory in sorted(directories)
        ]
        run(["patchelf", "--set-rpath", ":".join(paths), binary])
    # Validate after relocation too: no accidental dependency on build-host libraries.
    for binary in native_files(target):
        if "(NEEDED)" not in output(["readelf", "-d", binary]):
            continue
        for dependency in linux_dependencies(binary):
            if not dependency.is_file() or not (
                system_elf(dependency) or dependency.is_relative_to(target)
            ):
                raise RuntimeError(
                    f"Non-bundled ELF dependency after relocation: {binary}: {dependency}"
                )


def mac_dependencies(binary: Path):
    # LC_ID_DYLIB is not a load dependency. MH_BUNDLE modules may have a .dylib
    # suffix without an ID, so filename-based skipping loses a real dependency.
    identities = set(output(["otool", "-D", binary]).splitlines()[1:])
    return [
        line.strip().split(" (", 1)[0]
        for line in output(["otool", "-L", binary]).splitlines()
        if line.startswith("\t") and line.strip().split(" (", 1)[0] not in identities
    ]


def bundle_mac_dependencies(target: Path, build: Path):
    bundled = build / "bundled-libs"
    if bundled.exists():
        shutil.rmtree(bundled)
    bundled.mkdir()
    binaries = list(native_files(target))
    command = [
        "dylibbundler",
        "-b",
        "-d",
        bundled,
        "-p",
        "@executable_path/../lib/",
        "-of",
    ]
    for binary in binaries:
        validate_architecture(binary)
        for dependency in mac_dependencies(binary):
            if not dependency.startswith(
                (str(target) + "/", "@", "/usr/lib/", "/System/Library/")
            ):
                raise RuntimeError(
                    f"Unexpected build-host library; rebuild with the supported dependency-free configure flags: {binary}: {dependency}"
                )
        command.extend(["-x", binary])
    run(command)
    for dependency in bundled.iterdir():
        shutil.copy2(dependency, target / "lib" / dependency.name)
    for binary in native_files(target):
        validate_architecture(binary)
        for dependency in mac_dependencies(binary):
            if not dependency.startswith(("@", "/usr/lib/", "/System/Library/")):
                raise RuntimeError(
                    f"Non-relocatable Mach-O dependency: {binary}: {dependency}"
                )
        if len(output(["otool", "-D", binary]).splitlines()) > 1:
            run(["install_name_tool", "-id", f"@rpath/{binary.name}", binary])
        # install_name_tool changes invalidate Mach-O signatures. ARM64 macOS
        # requires a fresh local signature before the pre-package smoke can run.
        run(["codesign", "--force", "--sign", "-", binary])


def prepare_postgres(
    extension_lib: Path | None = None, extension_share: Path | None = None
):
    build = ROOT / "build" / "postgres"
    build.mkdir(parents=True, exist_ok=True)
    source = source_tree(
        f"https://ftp.postgresql.org/pub/source/v{PG_VERSION}/postgresql-{PG_VERSION}.tar.bz2",
        build / f"postgresql-{PG_VERSION}.tar.bz2",
        PG_SHA256,
        build / f"postgresql-{PG_VERSION}",
    )
    target = RESOURCES / "postgres"
    # A compiler/SDK deployment target change must rebuild every object.
    if (source / "GNUmakefile").exists():
        run(["make", "distclean"], cwd=source)
    run(
        [
            source / "configure",
            f"--prefix={target}",
            "--without-icu",
            "--without-readline",
            "--without-zlib",
        ],
        cwd=source,
    )
    run(["make", f"-j{os.cpu_count() or 2}"], cwd=source)
    # Replace the generated runtime, including stale files from prior layouts.
    if target.exists():
        shutil.rmtree(target)
    run(["make", "install"], cwd=source)
    lib, share = pg_layout(target)
    if extension_lib is None:
        upstream_extensions(build, target, lib, share)
    else:
        for name, version in (
            ("pg_search", SEARCH_VERSION),
            ("vector", VECTOR_VERSION),
        ):
            install_extension(name, version, extension_lib, extension_share, lib, share)
    notices = RESOURCES / "licenses"
    notices.mkdir(exist_ok=True)
    shutil.copy2(source / "COPYRIGHT", notices / "PostgreSQL-COPYRIGHT")
    download(
        f"https://raw.githubusercontent.com/paradedb/paradedb/v{SEARCH_VERSION}/LICENSE",
        notices / "ParadeDB-AGPL-3.0",
        SEARCH_LICENSE_SHA256,
    )
    vector = source_tree(
        f"https://codeload.github.com/pgvector/pgvector/tar.gz/refs/tags/v{VECTOR_VERSION}",
        build / f"pgvector-{VECTOR_VERSION}.tar.gz",
        VECTOR_SHA256,
        build / f"pgvector-{VECTOR_VERSION}",
    )
    shutil.copy2(vector / "LICENSE", notices / "pgvector-LICENSE")
    # Ship the exact corresponding pg_search source with its upstream build
    # instructions, alongside the AGPL notice, rather than only a moving URL.
    download(
        f"https://codeload.github.com/paradedb/paradedb/tar.gz/refs/tags/v{SEARCH_VERSION}",
        notices / f"paradedb-{SEARCH_VERSION}-source.tar.gz",
        SEARCH_SOURCE_SHA256,
    )
    shutil.copy2(Path(__file__), notices / "prepare_resources.py")
    (notices / "database-sources.txt").write_text(
        f"PostgreSQL {PG_VERSION}: https://ftp.postgresql.org/pub/source/v{PG_VERSION}/\nParadeDB pg_search {SEARCH_VERSION} (AGPL-3.0): https://github.com/paradedb/paradedb/tree/v{SEARCH_VERSION}\nCorresponding source archive: https://github.com/paradedb/paradedb/archive/refs/tags/v{SEARCH_VERSION}.tar.gz\npgvector {VECTOR_VERSION}: https://github.com/pgvector/pgvector/tree/v{VECTOR_VERSION}\nBuild and relocation instructions: desktop/scripts/prepare_resources.py in the Pulsara source distribution.\n",
        encoding="utf-8",
    )
    if sys.platform == "darwin":
        bundle_mac_dependencies(target, build)
    else:
        bundle_linux_dependencies(target)
    for name, version in (("pg_search", SEARCH_VERSION), ("vector", VECTOR_VERSION)):
        install_extension(name, version, lib, share, lib, share)


def export_requirements():
    """uv owns resolution; only runtime requirements become a native-cache input."""
    requirements = ROOT / "build" / "requirements.txt"
    requirements.parent.mkdir(parents=True, exist_ok=True)
    text = output(
        ["uv", "export", "--locked", "--default-index", "https://pypi.org/simple", "--no-dev", "--no-emit-project",
         "--no-header", "--no-annotate"], cwd=REPO,
    ) + "\n"
    if not requirements.exists() or requirements.read_text() != text:
        requirements.write_text(text)


def compile_python_stdlib(python: Path, stdlib: Path):
    # Resource copies can change source mtimes. CPython's checked-hash importer
    # keeps these caches valid while still checking source contents. Force is
    # required to replace even valid timestamp caches; uv owns site-packages.
    run([
        python, "-m", "compileall", "-q", "-f",
        "--invalidation-mode", "checked-hash",
        "-x", "^" + re.escape(str(stdlib / "site-packages")) + r"([/\\]|$)", stdlib,
    ])


def prepare_python_dependencies():
    target = RESOURCES / "python"
    managed = output(
        ["uv", "python", "find", "--managed-python", "--no-project",
         (REPO / ".python-version").read_text().strip()]
    )
    base = Path(output([managed, "-c", "import sys; print(sys.base_prefix)"]))
    # Rebuild a dependency layer from pristine SDK files. Re-patching already
    # relocated Linux wheels can corrupt their ELF version metadata.
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(base, target, symlinks=True)
    python = target / "bin" / "python3"
    validate_architecture(python.resolve())
    notices = RESOURCES / "licenses"
    notices.mkdir(exist_ok=True)
    shutil.copy2(
        target / "lib" / "python3.12" / "LICENSE.txt", notices / "Python-LICENSE.txt"
    )
    run(
        [
            "uv",
            "pip",
            "sync",
            "--python",
            python,
            "--system",
            "--break-system-packages",
            "--link-mode=copy",
            # uv owns compilation with the target interpreter. Hash validation
            # survives electron-builder's resource copies, which change mtimes.
            "--compile-bytecode",
            "--require-hashes",
            ROOT / "build" / "requirements.txt",
        ],
        cwd=REPO,
        env={**os.environ, "PYC_INVALIDATION_MODE": "CHECKED_HASH"},
    )
    compile_python_stdlib(python, target / "lib" / "python3.12")
    if sys.platform == "linux":
        bundle_linux_dependencies(target)


def prepare_application():
    # Always rebuild local application code, including prompts, bundled Skills
    # and the renderer. Dependency cache hits never authorize an old app wheel.
    with tempfile.TemporaryDirectory(prefix="pulsara-wheel-") as temporary:
        wheels = Path(temporary)
        run(["uv", "build", "--wheel", "--no-build-isolation", "--python",
             REPO / ".venv/bin/python", "--out-dir", wheels], cwd=REPO)
        wheel, = wheels.glob("pulsara_agent-*.whl")
        run(
            ["uv", "pip", "install", "--python", RESOURCES / "python/bin/python3",
             "--system", "--break-system-packages", "--link-mode=copy",
             "--compile-bytecode", "--no-deps", "--reinstall", wheel], cwd=REPO,
            env={**os.environ, "PYC_INVALIDATION_MODE": "CHECKED_HASH"},
        )


def prepare_browsers():
    # Playwright owns revision selection and download caching. Relocate a copy;
    # never patchelf the SDK's cached browser binaries.
    downloads = ROOT / "build" / "playwright-downloads"
    run(
        [RESOURCES / "python/bin/python3", "-m", "playwright", "install", "chromium"],
        env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(downloads)},
    )
    browsers = RESOURCES / "playwright"
    if browsers.exists():
        shutil.rmtree(browsers)
    shutil.copytree(downloads, browsers, symlinks=True)
    if sys.platform == "linux":
        bundle_linux_dependencies(browsers)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "component", nargs="?",
        choices=("requirements", "postgres", "python-dependencies", "browsers", "application", "validate"),
        help="Internal resource task; omit to run the mise dependency graph",
    )
    parser.add_argument(
        "--extension-lib",
        type=Path,
        help="Optional build input containing both pinned native extension libraries",
    )
    parser.add_argument(
        "--extension-share",
        type=Path,
        help="Matching extension controls and SQL; required with --extension-lib",
    )
    options = parser.parse_args()
    if options.component is None:
        if options.extension_lib or options.extension_share:
            parser.error("Custom extensions require the explicit postgres component")
        run(["mise", "run", "desktop:resources"], cwd=REPO)
        return
    target_platform()
    if sys.platform == "darwin":
        os.environ["MACOSX_DEPLOYMENT_TARGET"] = "15.0"
    if bool(options.extension_lib) != bool(options.extension_share):
        parser.error("--extension-lib and --extension-share must be supplied together")
    if options.extension_lib and options.component != "postgres":
        parser.error("Custom extensions belong to the postgres component")
    RESOURCES.mkdir(exist_ok=True)
    if options.component == "requirements":
        export_requirements()
    elif options.component == "postgres":
        prepare_postgres(
            options.extension_lib.resolve() if options.extension_lib else None,
            options.extension_share.resolve() if options.extension_share else None,
        )
    elif options.component == "python-dependencies":
        prepare_python_dependencies()
    elif options.component == "browsers":
        prepare_browsers()
    elif options.component == "application":
        prepare_application()
    else:
        validate_resources()


def validate_resources():
    target = RESOURCES / "postgres"
    lib, share = pg_layout(target)
    for name, version in (("pg_search", SEARCH_VERSION), ("vector", VECTOR_VERSION)):
        install_extension(name, version, lib, share, lib, share)
    for payload in (target, RESOURCES / "python", RESOURCES / "playwright"):
        for binary in native_files(payload):
            validate_architecture(binary)
    python = RESOURCES / "python" / "bin" / "python3"
    validate_architecture(python.resolve())
    python_version = output(
        [python, "-c", "import platform; print(platform.python_version())"]
    )
    expected_python = (REPO / ".python-version").read_text().strip()
    if python_version != expected_python:
        raise RuntimeError(f"Bundled Python must be {expected_python}, got {python_version}")
    (RESOURCES / "desktop-launch.json").write_text(
        json.dumps(
            {
                "executable": "../MacOS/pulsara"
                if sys.platform == "darwin"
                else "../pulsara-desktop"
            }
        )
        + "\n"
    )
    (RESOURCES / "versions.json").write_text(
        json.dumps(
            {
                "postgresql": PG_VERSION,
                "pg_search": SEARCH_VERSION,
                "vector": VECTOR_VERSION,
                "platform": sys.platform,
                "architecture": platform.machine(),
                "python": python_version,
                "minimum_os": "macOS 15"
                if sys.platform == "darwin"
                else "Ubuntu 24.04",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
