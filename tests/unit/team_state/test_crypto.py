"""Cryptographic envelope behavior for canonical team-state archives."""

from __future__ import annotations

import asyncio
import base64
import json
import random
import traceback
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from intent_engineering.team_state import crypto, restore
from intent_engineering.team_state.crypto import (
    EncryptedBundle,
    canonical_encrypted_bundle_bytes,
    decrypt_bundle,
    encrypt_bundle,
)
from intent_engineering.team_state.models import RecipientRecord

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
REPOSITORY = "github.com/acme/project"
AAD = b'{"manifest":"exact"}'


def _b64(content: bytes) -> str:
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def _recipient(key_id: str, private: X25519PrivateKey) -> RecipientRecord:
    suffix = key_id.rsplit(":", 1)[-1]
    account_id = {"alice": "101", "bob": "202"}.get(suffix, "303")
    return RecipientRecord(
        key_id=key_id,
        project_id="project",
        repository_id=REPOSITORY,
        actor=f"github:{account_id}",
        github_account_id=account_id,
        github_login=f"{suffix}-dev",
        public_key=_b64(private.public_key().public_bytes_raw()),
        webauthn_credential_id=_b64(f"credential-{suffix}".encode()),
        webauthn_credential_public_key=_b64(b"webauthn-public-key-material"),
        enrolled_at=NOW,
    )


def _keys() -> tuple[X25519PrivateKey, X25519PrivateKey]:
    return (
        X25519PrivateKey.from_private_bytes(b"a" * 32),
        X25519PrivateKey.from_private_bytes(b"b" * 32),
    )


def _recipients() -> tuple[RecipientRecord, ...]:
    alice, bob = _keys()
    return (_recipient("recipient:alice", alice), _recipient("recipient:bob", bob))


def _mutate(value: str) -> str:
    replacement = "A" if value[-1] != "A" else "B"
    return value[:-1] + replacement


def _traceback_contains_bytes(error: BaseException, secret: bytes) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for frame, _lineno in traceback.walk_tb(current.__traceback__):
            if frame.f_globals.get("__name__") != "intent_engineering.team_state.crypto":
                continue
            if any(value is secret or value == secret for value in frame.f_locals.values()):
                return True
        current = current.__cause__ or current.__context__
    return False


def test_each_recipient_decrypts_randomized_envelope_and_encryptions_are_fresh() -> None:
    """Catches missing recipient wrapping, deterministic nonces, or payload corruption."""
    alice, bob = _keys()
    recipients = _recipients()
    plaintext = b"approved canonical state\x00\xff"

    first = encrypt_bundle(plaintext, recipients, AAD)
    second = encrypt_bundle(plaintext, recipients, AAD)

    assert first != second
    assert decrypt_bundle(first, alice.private_bytes_raw(), AAD) == plaintext
    assert decrypt_bundle(first, bob.private_bytes_raw(), AAD) == plaintext
    nonces = (first.nonce, *(item.nonce for item in first.wrapped_keys))
    assert len(set(nonces)) == len(nonces)
    assert tuple(item.recipient_key_id for item in first.wrapped_keys) == (
        "recipient:alice",
        "recipient:bob",
    )


def test_one_hundred_randomized_crypto_round_trips() -> None:
    """Catches AEAD/framing boundary failures across varied binary plaintexts."""
    source = random.Random(20260908)
    alice, _bob = _keys()
    recipients = _recipients()
    for _ in range(100):
        plaintext = source.randbytes(source.randrange(0, 2049))
        bundle = encrypt_bundle(plaintext, recipients, AAD)
        assert decrypt_bundle(bundle, alice.private_bytes_raw(), AAD) == plaintext


def test_encrypted_bundle_has_one_canonical_bounded_wire_form() -> None:
    """Catches alternate JSON encodings weakening manifest digest and restore binding."""
    bundle = encrypt_bundle(b"state", _recipients(), AAD)
    encoded = canonical_encrypted_bundle_bytes(bundle)

    assert EncryptedBundle.model_validate_json(encoded) == bundle
    with pytest.raises(ValueError, match="noncanonical"):
        EncryptedBundle.model_validate_json(encoded + b"\n")


def test_hardened_restore_and_public_crypto_share_one_byte_compatible_envelope() -> None:
    """Catches publication and restore silently evolving competing encryption formats."""
    alice, _bob = _keys()
    created_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    release = restore.seal_state_payload(
        b"legacy-compatible-payload",
        project_id="project",
        repository_id=REPOSITORY,
        graph_version=7,
        parent_bundle_digest=None,
        created_at=created_at,
        recipient_public_keys={"recipient:alice": alice.public_key().public_bytes_raw()},
        signing_private_keys={
            "signer:release": Ed25519PrivateKey.from_private_bytes(b"s" * 32).private_bytes_raw()
        },
    )
    aad = json.dumps(
        {
            "schema_version": 1,
            "project_id": "project",
            "repository_id": REPOSITORY,
            "graph_version": 7,
            "parent_bundle_digest": None,
            "encryption_algorithm": "x25519-hkdf-sha256-aes256gcm-v1",
            "recipient_key_ids": ["recipient:alice"],
            "required_signature_ids": ["signer:release"],
            "created_at": "2026-09-08T12:00:00Z",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    parsed = EncryptedBundle.model_validate_json(release.bundle)

    assert restore.EncryptedStateBundle is EncryptedBundle
    assert canonical_encrypted_bundle_bytes(parsed) == release.bundle
    assert decrypt_bundle(parsed, alice.private_bytes_raw(), aad) == b"legacy-compatible-payload"


def test_legacy_seal_helper_keeps_canonicalizing_mapping_order() -> None:
    """Catches extraction making historical mapping callers insertion-order dependent."""
    alice, bob = _keys()

    release = restore.seal_state_payload(
        b"payload",
        project_id="project",
        repository_id=REPOSITORY,
        graph_version=7,
        parent_bundle_digest=None,
        created_at=NOW,
        recipient_public_keys={
            "recipient:bob": bob.public_key().public_bytes_raw(),
            "recipient:alice": alice.public_key().public_bytes_raw(),
        },
        signing_private_keys={
            "signer:release": Ed25519PrivateKey.from_private_bytes(b"s" * 32).private_bytes_raw()
        },
    )

    parsed = EncryptedBundle.model_validate_json(release.bundle)
    assert tuple(item.recipient_key_id for item in parsed.wrapped_keys) == (
        "recipient:alice",
        "recipient:bob",
    )


@pytest.mark.parametrize("target", ["ciphertext", "nonce", "ephemeral_public_key"])
def test_payload_tamper_truncation_and_substitution_fail_closed(target: str) -> None:
    """Catches accepting substituted payload ciphertext, nonce, or ephemeral identity."""
    alice, _bob = _keys()
    bundle = encrypt_bundle(b"state", _recipients(), AAD)
    value = getattr(bundle, target)
    forged = bundle.model_copy(update={target: _mutate(value)})

    with pytest.raises(ValueError, match="unable to decrypt"):
        decrypt_bundle(forged, alice.private_bytes_raw(), AAD)


def test_recipient_substitution_wrong_key_and_wrong_aad_fail_closed() -> None:
    """Catches recipient-wrap swapping or context/key confusion revealing plaintext."""
    alice, _bob = _keys()
    outsider = X25519PrivateKey.from_private_bytes(b"z" * 32)
    bundle = encrypt_bundle(b"state", _recipients(), AAD)
    first, second = bundle.wrapped_keys
    substituted = bundle.model_copy(
        update={
            "wrapped_keys": (
                first.model_copy(update={"ciphertext": second.ciphertext}),
                second.model_copy(update={"ciphertext": first.ciphertext}),
            )
        }
    )

    for candidate, key, aad in (
        (substituted, alice.private_bytes_raw(), AAD),
        (bundle, outsider.private_bytes_raw(), AAD),
        (bundle, alice.private_bytes_raw(), AAD + b"x"),
    ):
        with pytest.raises(ValueError, match="unable to decrypt"):
            decrypt_bundle(candidate, key, aad)


@pytest.mark.parametrize(
    "recipients",
    [(), tuple(reversed(_recipients())), (_recipients()[0], _recipients()[0])],
)
def test_encryption_requires_nonempty_sorted_unique_reviewed_recipients(
    recipients: tuple[RecipientRecord, ...],
) -> None:
    """Catches ambiguous recipient authority or envelope ordering."""
    with pytest.raises(ValueError, match="recipient"):
        encrypt_bundle(b"state", recipients, AAD)


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "other-project"},
        {"repository_id": "github.com/acme/other"},
    ],
)
def test_encryption_rejects_mixed_recipient_authority(change: dict[str, str]) -> None:
    """Catches one envelope mixing recipients reviewed for different project authority."""
    alice, bob = _recipients()
    mixed = (alice, bob.model_copy(update=change))

    with pytest.raises(ValueError, match="recipient"):
        encrypt_bundle(b"state", mixed, AAD)


def test_cancellation_propagates_without_plaintext_retained_in_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches cancellation conversion or traceback retention of plaintext bytes."""
    plaintext = b"plain-secret-" + bytes(range(64))

    def cancel(_size: int) -> bytes:
        raise asyncio.CancelledError

    monkeypatch.setattr(crypto, "_random_bytes", cancel)
    with pytest.raises(asyncio.CancelledError) as caught:
        encrypt_bundle(plaintext, _recipients(), AAD)

    assert not _traceback_contains_bytes(caught.value, plaintext)


def test_decryption_error_does_not_retain_private_key_in_traceback() -> None:
    """Catches wrong-key failures retaining caller private bytes in exception frames."""
    private_key = bytes(bytearray(b"z" * 32))
    bundle = encrypt_bundle(b"state", _recipients(), AAD)

    with pytest.raises(ValueError, match="unable to decrypt") as caught:
        decrypt_bundle(bundle, private_key, AAD)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not _traceback_contains_bytes(caught.value, private_key)
