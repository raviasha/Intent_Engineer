"""Recover encrypted publication drafts without manufacturing human authority."""

from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from intent_engineering.team_state.publication import PublicationAuthority
from tests.helpers.shared_state import ready_project
from tests.integration.team_state.test_publication import (
    NOW,
    RecordingPublisher,
    _recipient,
    _service,
)


def test_recovered_encrypted_draft_keeps_exact_artifacts_but_requires_a_fresh_decision(tmp_path):
    """Catches restarting a pending publication creating a second encrypted branch or reusing authority."""
    root = tmp_path / "project"
    root.mkdir()
    ready_project(root)
    recipient_key = X25519PrivateKey.generate()
    authority = PublicationAuthority(
        recipients=(_recipient(recipient_key),),
        signing_private_keys={"signer:release": Ed25519PrivateKey.generate().private_bytes_raw()},
        remote_state=None,
    )
    runtime, service = _service(root, authority, RecordingPublisher())
    try:
        first = service.preview(now=NOW)
        draft = service.pending_publication()
        recovered = service.recover_preview(
            draft,
            recipient_private_key=recipient_key.private_bytes_raw(),
            now=NOW + timedelta(seconds=1),
        )
        assert recovered.manifest == first.manifest
        assert recovered.payload.result_digest == first.payload.result_digest
        assert recovered.payload != first.payload
        assert service.pending_publication() == draft
        with pytest.raises(ValueError):
            service.recover_preview(
                draft, recipient_private_key=b"x" * 32, now=NOW + timedelta(seconds=2)
            )
    finally:
        runtime.close()
