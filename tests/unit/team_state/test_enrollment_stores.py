"""Durable replay and challenge stores survive independent process composition."""

from __future__ import annotations

import pytest

from tests.unit.team_state.test_enrollment import NOW, _Backend, _fixture


def test_invite_and_challenge_survive_restart_and_consumption_is_monotonic(tmp_path):
    """Catches default in-memory replay/challenge state breaking the three-command exchange."""
    from intent_engineering.team_state.enrollment import (
        FileEnrollmentReplayStateStore,
        KeyringEnrollmentChallengeKeyStore,
    )

    backend = _Backend()
    replay = FileEnrollmentReplayStateStore(
        tmp_path / "public", "project", "github.com/acme/project"
    )
    challenge = KeyringEnrollmentChallengeKeyStore(
        "project", "github.com/acme/project", backend=backend, lock_root=tmp_path / "locks"
    )
    sponsor, _, state, identity, _ = _fixture(tmp_path)
    sponsor._replay_store = replay
    sponsor._challenge_store = challenge
    invite = sponsor.create_invite(state=state, intended_identity=identity, now=NOW)
    restarted = FileEnrollmentReplayStateStore(
        tmp_path / "public", "project", "github.com/acme/project"
    )
    restarted_keys = KeyringEnrollmentChallengeKeyStore(
        "project", "github.com/acme/project", backend=backend, lock_root=tmp_path / "locks"
    )
    assert restarted.get_invite(invite.invite_id) == invite
    assert restarted_keys.has(invite.invite_id)
    with pytest.raises(ValueError):
        restarted_keys.create(invite.invite_id, b"x" * 32)
    restarted.consume(invite.invite_id)
    assert replay.is_consumed(invite.invite_id)
    with pytest.raises(ValueError):
        replay.consume(invite.invite_id)
    restarted_keys.delete(invite.invite_id)
    assert not challenge.has(invite.invite_id)
    assert all(
        b"eeeeeeeeeeeeeeee" not in path.read_bytes()
        for path in (tmp_path / "public").rglob("*")
        if path.is_file()
    )
