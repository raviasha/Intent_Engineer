"""Exercise the real subprocess boundary with an offline CLI double."""

import json

import pytest

from intent_engineering.intent_workflow.codex_review import CodexReviewRunner


def test_runner_uses_read_only_isolated_snapshots_and_disables_recursive_tools(tmp_path):
    script = tmp_path / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        + r"""
import json, pathlib, sys
if "--help" in sys.argv:
    print("--ignore-user-config --output-schema --sandbox --ephemeral")
    raise SystemExit()
args = sys.argv[1:]
assert args[args.index("--sandbox") + 1] == "read-only"
assert "--ignore-user-config" in args
assert "--ephemeral" in args
for feature in ("hooks", "plugins", "apps", "multi_agent", "computer_use", "browser_use"):
    assert any(args[i:i+2] == ["--disable", feature] for i in range(len(args)))
assert "approval_policy=\"never\"" in args
assert "web_search=\"disabled\"" in args
cwd = pathlib.Path.cwd()
assert cwd.name.startswith("intent-review-")
snapshot = json.loads((cwd / "assessment.json").read_text())
assert snapshot["inputs"]["marker"] == "actual captured evidence"
prompt = sys.stdin.read()
assert "source intent" in prompt.lower()
assert "Do not modify" in prompt
assert "unknown" in prompt
pathlib.Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps({"comparisons": []}))
"""
    )
    script.chmod(0o700)
    result = CodexReviewRunner(executable=str(script)).review(
        {"marker": "actual captured evidence"}, [{"axis": "intent_code", "subject_ref": "i"}]
    )
    assert result == ()
    assert not list(tmp_path.glob("intent-review-*"))


@pytest.mark.parametrize(
    "content", ["not json", json.dumps({"comparisons": ["fake"]}), "x" * 4194305]
)
def test_malformed_and_oversized_model_output_is_rejected(tmp_path, content):
    script = tmp_path / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        + (
            "import pathlib, sys\n"
            "if '--help' in sys.argv:\n"
            " print('--ignore-user-config --output-schema --sandbox --ephemeral'); raise SystemExit()\n"
            f"pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text({content!r})\n"
        )
    )
    script.chmod(0o700)
    with pytest.raises(ValueError, match="review unavailable"):
        CodexReviewRunner(executable=str(script)).review({}, [])


def test_missing_cli_is_not_reported_as_a_completed_review(tmp_path):
    with pytest.raises(ValueError, match="review unavailable"):
        CodexReviewRunner(executable=str(tmp_path / "missing")).review({}, [])
