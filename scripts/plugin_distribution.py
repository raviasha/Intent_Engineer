"""Build and verify self-contained Git marketplace packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("macos-arm64", "macos-x86_64", "linux-x86_64")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def extract_verified(archive: Path, expected: str, destination: Path) -> None:
    if sha256(archive) != expected:
        raise ValueError("runtime checksum mismatch")
    if destination.exists():
        raise ValueError("runtime destination already exists")
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(destination, filter="data")


def file_manifest(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            if Path(target).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"unsafe symlink: {relative}")
            result[relative] = {"symlink": target}
        elif path.is_file():
            if path.stat().st_size >= 100 * 1024 * 1024:
                raise ValueError(f"file exceeds GitHub size limit: {relative}")
            result[relative] = {
                "sha256": sha256(path),
                "executable": int(bool(path.stat().st_mode & 0o111)),
            }
    return result


def verify_files(root: Path, expected: dict) -> None:
    if file_manifest(root) != expected:
        raise ValueError("package integrity mismatch")


def materialize_symlinks(root: Path) -> None:
    """Codex's plugin cache copier omits symlinks; distribute actual runtime files."""
    file_manifest(root)  # Reject escaping links before dereferencing anything.
    for path in list(root.rglob("*")):
        if path.is_symlink():
            target = path.resolve(strict=True)
            path.unlink()
            if target.is_dir():
                shutil.copytree(target, path)
            else:
                shutil.copy2(target, path)


def native_target() -> str:
    system = {"Darwin": "macos", "Linux": "linux"}.get(platform.system(), "unsupported")
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x86_64"}.get(
        platform.machine(), "unsupported"
    )
    return f"{system}-{arch}"


def check_upgrade(current: Path, incoming: Path) -> None:
    old = current / "plugins/intent-advisor"
    new = incoming / "plugins/intent-advisor"
    if not (old / "distribution.json").exists():
        return
    old_version = json.loads((old / ".codex-plugin/plugin.json").read_text())["version"]
    new_version = json.loads((new / ".codex-plugin/plugin.json").read_text())["version"]
    old_source = json.loads((old / "distribution.json").read_text())["source_revision"]
    new_source = json.loads((new / "distribution.json").read_text())["source_revision"]
    if old_version == new_version and old_source == new_source:
        return
    if tuple(map(int, new_version.split("."))) <= tuple(map(int, old_version.split("."))):
        raise ValueError("changed release requires a newer plugin version")


def dependency_install_argv(python: Path, target: str) -> list[str]:
    argv = [
        str(python),
        "-I",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--require-hashes",
        "--only-binary=:all:",
    ]
    if target == "macos-x86_64":
        # Upstream publishes no Intel macOS wheel for these pinned releases.
        # Compile only this hash-verified source on the release runner.
        argv += [
            "--no-binary=cbor2,cryptography",
            "--build-constraint",
            str(ROOT / "packaging/build-constraints.txt"),
        ]
    return argv + ["-r", str(ROOT / "packaging/runtime.lock")]


def prepare_static_openssl(temp_root: Path) -> dict[str, str]:
    source = json.loads((ROOT / "packaging/openssl-source.json").read_text())
    archive = temp_root / "openssl.tar.gz"
    with (
        urllib.request.urlopen(source["url"], timeout=120) as response,
        archive.open("wb") as stream,
    ):
        shutil.copyfileobj(response, stream)
    extracted = temp_root / "openssl-source"
    extract_verified(archive, source["sha256"], extracted)
    work = extracted / f"openssl-{source['version']}"
    prefix = temp_root / "openssl-static"
    subprocess.run(
        [
            "./Configure",
            "darwin64-x86_64-cc",
            "no-shared",
            "no-module",
            "no-tests",
            f"--prefix={prefix}",
            "--libdir=lib",
        ],
        cwd=work,
        check=True,
    )
    subprocess.run(["make", "-j4"], cwd=work, check=True)
    subprocess.run(["make", "install_sw"], cwd=work, check=True)
    return {**os.environ, "OPENSSL_DIR": str(prefix), "OPENSSL_STATIC": "1"}


def verify_macos_links(runtime: Path) -> None:
    for extension in runtime.rglob("*.so"):
        linked = subprocess.check_output(["otool", "-L", str(extension)], text=True)
        for line in linked.splitlines()[1:]:
            dependency = line.strip().split(" (", 1)[0]
            if not dependency.startswith(("@", "/usr/lib/", "/System/Library/")):
                raise ValueError(f"external native dependency: {extension.name}: {dependency}")


def build(target: str, output: Path, revision: str) -> None:
    if target != native_target() or target not in TARGETS:
        raise ValueError("build must run on its native supported platform")
    sources = json.loads((ROOT / "packaging/runtime-sources.json").read_text())
    source = sources["targets"][target]
    filename = f"cpython-{sources['python_version']}+{sources['release']}-{source['triple']}-install_only_stripped.tar.gz"
    url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{sources['release']}/{filename}"
    if output.exists():
        raise ValueError("output already exists")
    output.mkdir(parents=True)
    package = output / "plugin"
    shutil.copytree(
        ROOT / "plugins/intent-advisor",
        package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "runtime"),
    )
    runtime = package / "runtime" / target
    with tempfile.TemporaryDirectory(prefix="intent-package-") as temp:
        temp_root = Path(temp)
        archive = temp_root / "python.tar.gz"
        with urllib.request.urlopen(url, timeout=120) as response, archive.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        extract_verified(archive, source["sha256"], runtime)
        python = runtime / "python/bin/python3"
        build_env = prepare_static_openssl(temp_root) if target == "macos-x86_64" else None
        subprocess.run(dependency_install_argv(python, target), check=True, env=build_env)
        if target == "macos-x86_64":
            verify_macos_links(runtime)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(temp_root / "wheels"),
                str(ROOT),
            ],
            check=True,
        )
        wheel = next((temp_root / "wheels").glob("intent_engineering-*.whl"))
        subprocess.run(
            [str(python), "-I", "-m", "pip", "install", "--no-deps", str(wheel)], check=True
        )
        subprocess.run([str(python), "-I", "-m", "pip", "check"], check=True)
        installed = subprocess.check_output(
            [str(python), "-I", "-m", "pip", "list", "--format=json"]
        )
    # Remove console scripts with build-host shebangs; the plugin launcher owns dispatch.
    for path in (runtime / "python/bin").iterdir():
        if not path.name.startswith("python"):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    for path in list(runtime.rglob("__pycache__")):
        shutil.rmtree(path)
    for path in runtime.rglob("direct_url.json"):
        path.unlink()
    shutil.copy2(ROOT / "LICENSE", package / "LICENSE")
    shutil.copytree(ROOT / "packaging/python-licenses", package / "python-licenses")
    shutil.copy2(
        ROOT / "packaging/openssl-LICENSE.txt",
        package / "python-licenses/openssl-static-LICENSE.txt",
    )
    shutil.copy2(ROOT / "packaging/openssl-source.json", package / "openssl-source.json")
    materialize_symlinks(runtime)
    shutil.copy2(ROOT / "packaging/runtime.lock", package / "runtime.lock")
    shutil.copy2(ROOT / "packaging/build-constraints.txt", package / "build-constraints.txt")
    info = {
        "source_revision": revision,
        "target": target,
        "python_version": sources["python_version"],
        "runtime_url": url,
        "runtime_sha256": source["sha256"],
        "dependencies": json.loads(installed),
        "plugin_version": json.loads((package / ".codex-plugin/plugin.json").read_text())[
            "version"
        ],
    }
    (package / "distribution.json").write_text(json.dumps(info, indent=2) + "\n")
    (output / "manifest.json").write_text(json.dumps(file_manifest(package), indent=2) + "\n")
    print(json.dumps(info, indent=2))


def assemble(inputs: Path, output: Path, revision: str) -> None:
    for target in TARGETS:
        artifact = inputs / target
        if not (artifact / "plugin/distribution.json").is_file():
            raise ValueError(f"missing platform: {target}")
        verify_files(artifact / "plugin", json.loads((artifact / "manifest.json").read_text()))
        info = json.loads((artifact / "plugin/distribution.json").read_text())
        if info["target"] != target or info["source_revision"] != revision:
            raise ValueError("platform source revision mismatch")
    if output.exists():
        raise ValueError("assembly output already exists")
    package = output / "plugins/intent-advisor"
    first = inputs / TARGETS[0] / "plugin"
    shutil.copytree(first, package, symlinks=True)
    common = {
        k: v
        for k, v in file_manifest(first).items()
        if not k.startswith("runtime/") and k != "distribution.json"
    }
    platforms = {}
    for target in TARGETS:
        source = inputs / target / "plugin"
        other = {
            k: v
            for k, v in file_manifest(source).items()
            if not k.startswith("runtime/") and k != "distribution.json"
        }
        if common != other:
            raise ValueError("platform plugin content differs")
        if target != TARGETS[0]:
            shutil.copytree(
                source / "runtime" / target, package / "runtime" / target, symlinks=True
            )
        platforms[target] = json.loads((source / "distribution.json").read_text())
    (package / "distribution.json").write_text(
        json.dumps({"source_revision": revision, "platforms": platforms}, indent=2) + "\n"
    )
    marketplace = output / ".agents/plugins/marketplace.json"
    marketplace.parent.mkdir(parents=True)
    shutil.copy2(ROOT / ".agents/plugins/marketplace.json", marketplace)
    (output / "README.md").write_text(
        "# Intent Advisor marketplace\n\nGenerated release; source: "
        f"https://github.com/raviasha/Intent_Engineer/commit/{revision}\n\n"
        "Install: `codex plugin marketplace add raviasha/Intent_Engineer --ref marketplace`, "
        "then `codex plugin add intent-advisor@intent-engineering-local`. Review and trust hooks.\n"
        "Includes Python; macOS arm64/x86_64 and Linux x86_64 (glibc).\n"
    )
    (output / "manifest.json").write_text(json.dumps(file_manifest(package), indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "assemble", "verify", "check-upgrade"])
    parser.add_argument("--target", choices=TARGETS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--revision", default="")
    args = parser.parse_args()
    if args.action == "build":
        build(args.target, args.output.resolve(), args.revision)
    elif args.action == "assemble":
        assemble(args.inputs.resolve(), args.output.resolve(), args.revision)
    elif args.action == "check-upgrade":
        check_upgrade(args.inputs.resolve(), args.output.resolve())
    else:
        root = args.output
        package = root / "plugin" if (root / "plugin").exists() else root / "plugins/intent-advisor"
        verify_files(package, json.loads((root / "manifest.json").read_text()))


if __name__ == "__main__":
    main()
