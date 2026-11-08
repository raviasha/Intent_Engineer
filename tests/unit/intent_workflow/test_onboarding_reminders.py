from pathlib import Path

import pytest

from intent_engineering.intent_workflow.onboarding_reminders import (
    complete_turn,
    defer_onboarding,
    offer_on_prompt,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    return project


def test_defer_reminds_after_three_completed_turns_and_survives_sessions(project: Path) -> None:
    assert offer_on_prompt(project, "one", "first")
    assert not (project / ".intent").exists()
    defer_onboarding(project)
    # The response to the deferral itself and unrelated stops do not count.
    complete_turn(project, "one", "first")
    for index in range(3):
        session, turn = f"chat-{index}", f"task-{index}"
        assert not offer_on_prompt(project, session, turn)
        complete_turn(project, session, turn)
        complete_turn(project, session, turn)
    assert offer_on_prompt(project, "four", "next")
    assert not (project / ".intent").exists()


def test_prompts_without_completions_do_not_consume_deferral(project: Path) -> None:
    defer_onboarding(project)
    for index in range(10):
        assert not offer_on_prompt(project, "chat", str(index))
    assert not offer_on_prompt(project, "chat", "next")


def test_replayed_prompt_and_stop_count_only_once(project: Path) -> None:
    defer_onboarding(project, after_tasks=2)
    assert not offer_on_prompt(project, "s", "t")
    complete_turn(project, "s", "t")
    assert not offer_on_prompt(project, "s", "t")
    complete_turn(project, "s", "t")
    assert not offer_on_prompt(project, "s", "another")
    complete_turn(project, "s", "another")
    assert offer_on_prompt(project, "s", "last")


def test_repository_isolation_and_repeated_deferral(project: Path, tmp_path: Path) -> None:
    defer_onboarding(project, after_tasks=1)
    other = tmp_path / "other"
    other.mkdir()
    assert offer_on_prompt(other, "s", "t")
    assert not offer_on_prompt(project, "s", "t")
    complete_turn(project, "s", "t")
    assert offer_on_prompt(project, "s", "next")
    defer_onboarding(project)
    assert not offer_on_prompt(project, "s", "next")


def test_invalid_counter_and_unsafe_cache_are_rejected(project: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        defer_onboarding(project, after_tasks=0)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "state").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        defer_onboarding(project)
    assert list(outside.iterdir()) == []
