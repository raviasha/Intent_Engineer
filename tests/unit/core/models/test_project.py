from __future__ import annotations

import pytest
from pydantic import ValidationError

from intent_engineering.core.models import ProjectConfig


def _config(**updates: object) -> ProjectConfig:
    return ProjectConfig(project_id="demo", local_actor="local:asha", **updates)


def test_project_config_accepts_only_reviewed_argv_commands() -> None:
    configured = _config(
        test_commands=(("tools/test-runner", "--suite", "unit"),),
        test_result_paths=(".intent/test-results/unit.json",),
    )

    assert configured.test_commands == (("tools/test-runner", "--suite", "unit"),)
    assert configured.test_result_paths == (".intent/test-results/unit.json",)

    with pytest.raises(ValidationError):
        _config(test_commands=("tools/test-runner --suite unit",))


@pytest.mark.parametrize(
    "command",
    [
        (("tools/test-runner", ";", "touch", "owned"),),
        (("tools/test-runner", "$(touch owned)"),),
        (("tools/test-runner", "*.py"),),
        (("/usr/bin/pytest",),),
        (("../outside/pytest",),),
        (("/dev/null",),),
    ],
)
def test_project_config_rejects_shell_metacharacters_and_unsafe_executables(
    command: tuple[tuple[str, ...], ...],
) -> None:
    with pytest.raises(ValidationError):
        _config(test_commands=command)


@pytest.mark.parametrize(
    "path",
    ["/tmp/result.json", "../result.json", ".intent/../result.json", "/dev/null"],
)
def test_project_config_rejects_absolute_parent_and_device_result_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        _config(test_result_paths=(path,))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("test_commands", (("./tools/test-runner",),)),
        ("test_commands", (("tools/./test-runner",),)),
        ("test_result_paths", ("./results.json",)),
        ("test_result_paths", ("reports/./results.json",)),
    ],
)
def test_project_config_rejects_noncanonical_path_aliases(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _config(**{field: value})


def test_project_config_rejects_duplicate_and_oversized_test_configuration() -> None:
    with pytest.raises(ValidationError):
        _config(test_commands=(("tools/test-runner",), ("tools/test-runner",)))
    with pytest.raises(ValidationError):
        _config(test_result_paths=("results.json", "results.json"))
    with pytest.raises(ValidationError):
        _config(test_commands=(("tools/test-runner", "x" * 4097),))
    with pytest.raises(ValidationError):
        _config(test_result_paths=("x" * 4097,))


def test_project_config_test_collections_are_deeply_immutable() -> None:
    configured = _config(test_commands=(("tools/test-runner",),))

    with pytest.raises(TypeError):
        configured.test_commands[0][0] = "other"  # type: ignore[index]
