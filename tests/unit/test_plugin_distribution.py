"""Distribution integrity and completeness are checked before publication."""

import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "plugin_distribution.py"


def distribution():
    assert SCRIPT.exists(), "distribution builder missing"
    spec = importlib.util.spec_from_file_location("plugin_distribution", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_download_hash_checked_before_extraction(tmp_path):
    archive = tmp_path / "runtime.tar.gz"
    archive.write_bytes(b"not the expected runtime")
    with pytest.raises(ValueError, match="checksum"):
        distribution().extract_verified(archive, "0" * 64, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_archive_cannot_write_outside_runtime(tmp_path):
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("../escaped")
        member.size = 4
        tar.addfile(member, io.BytesIO(b"evil"))
    with pytest.raises((ValueError, tarfile.TarError)):
        distribution().extract_verified(
            archive, hashlib.sha256(archive.read_bytes()).hexdigest(), tmp_path / "output"
        )
    assert not (tmp_path / "escaped").exists()


def test_manifest_detects_modified_missing_and_extra_files(tmp_path):
    mod = distribution()
    (tmp_path / "app").write_text("original")
    manifest = mod.file_manifest(tmp_path)
    mod.verify_files(tmp_path, manifest)
    (tmp_path / "app").write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        mod.verify_files(tmp_path, manifest)
    (tmp_path / "app").unlink()
    with pytest.raises(ValueError, match="integrity"):
        mod.verify_files(tmp_path, manifest)
    (tmp_path / "app").write_text("original")
    (tmp_path / "extra").write_text("unexpected")
    with pytest.raises(ValueError, match="integrity"):
        mod.verify_files(tmp_path, manifest)


def test_manifest_rejects_external_symlink(tmp_path):
    (tmp_path / "escape").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        distribution().file_manifest(tmp_path)


def test_assembly_rejects_missing_platforms(tmp_path):
    with pytest.raises(ValueError, match="platform"):
        distribution().assemble(tmp_path, tmp_path / "output", "a" * 40)


def test_assembly_combines_only_matching_verified_platforms(tmp_path):
    import json

    mod = distribution()
    inputs = tmp_path / "inputs"
    for target in ("macos-arm64", "macos-x86_64", "linux-x86_64"):
        artifact = inputs / target
        package = artifact / "plugin"
        runtime = package / "runtime" / target
        runtime.mkdir(parents=True)
        (runtime / "executable").write_text(target)
        (package / "shared").write_text("same plugin")
        (package / "distribution.json").write_text(
            json.dumps({"target": target, "source_revision": "a" * 40, "plugin_version": "0.1.8"})
        )
        (artifact / "manifest.json").write_text(json.dumps(mod.file_manifest(package)))
    mod.assemble(inputs, tmp_path / "release", "a" * 40)
    package = tmp_path / "release/plugins/intent-advisor"
    assert {p.name for p in (package / "runtime").iterdir()} == {
        "macos-arm64",
        "macos-x86_64",
        "linux-x86_64",
    }
    assert (tmp_path / "release/.agents/plugins/marketplace.json").is_file()
    mod.verify_files(package, json.loads((tmp_path / "release/manifest.json").read_text()))
    with pytest.raises(ValueError, match="revision"):
        mod.assemble(inputs, tmp_path / "wrong-source", "b" * 40)


def test_runtime_symlinks_become_regular_files_for_codex_installation(tmp_path):
    mod = distribution()
    real = tmp_path / "python3.12"
    real.write_text("executable content")
    real.chmod(0o755)
    alias = tmp_path / "python3"
    alias.symlink_to("python3.12")
    convert = getattr(mod, "materialize_symlinks", None)
    assert convert is not None, "Codex installer drops symlinks; release must materialize them"
    convert(tmp_path)
    assert alias.is_file() and not alias.is_symlink()
    assert alias.read_text() == "executable content"
    assert alias.stat().st_mode & 0o111


def test_publication_requires_version_bump_for_changed_source(tmp_path):
    import json

    mod = distribution()
    old = tmp_path / "old"
    new = tmp_path / "new"
    for root, revision in ((old, "a" * 40), (new, "b" * 40)):
        plugin = root / "plugins/intent-advisor"
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({"version": "0.1.8"}))
        (plugin / "distribution.json").write_text(json.dumps({"source_revision": revision}))
    check = getattr(mod, "check_upgrade", None)
    assert check is not None, "changed source must not reuse an installed plugin version"
    with pytest.raises(ValueError, match="version"):
        check(old, new)
    (new / "plugins/intent-advisor/.codex-plugin/plugin.json").write_text(
        json.dumps({"version": "0.1.9"})
    )
    check(old, new)
    check(new, new)
    with pytest.raises(ValueError, match="version"):
        check(new, old)


@pytest.mark.parametrize("target", ("macos-arm64", "macos-x86_64", "linux-x86_64"))
def test_only_intel_cbor_may_build_from_hash_locked_source(target):
    mod = distribution()
    argv = mod.dependency_install_argv(Path("/bundled/python"), target)
    assert "--require-hashes" in argv
    assert "--only-binary=:all:" in argv
    assert ("--no-binary=cbor2" in argv) == (target == "macos-x86_64")
    if target == "macos-x86_64":
        assert "--build-constraint" in argv
        constraints = Path(argv[argv.index("--build-constraint") + 1]).read_text()
        assert "setuptools-rust==" in constraints
        assert "setuptools_scm==" in constraints
