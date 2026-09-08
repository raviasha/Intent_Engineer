"""Canonical contracts for encrypted, Git-transported team intent state."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from intent_engineering import team_state
from intent_engineering.team_state.models import (
    CANONICAL_STATE_PATHS,
    MAX_FILE_BYTES,
    BundleInventory,
    BundleInventoryEntry,
    CanonicalStateFile,
    CanonicalStateSnapshot,
    PreparedPublication,
    PublicationLineage,
    RecipientRecord,
    RemoteStateSnapshot,
    TeamStateManifest,
    TeamStateManifestV2,
    V1MigrationBinding,
    canonical_manifest_bytes,
)
from intent_engineering.team_state.restore import SharedStateManifest

NOW = datetime(2026, 9, 7, 10, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REPOSITORY = "github.com/acme/project"
COMMIT = "c" * 40
X25519_PUBLIC_KEY = "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXowMTIzNDU"
WEBAUTHN_CREDENTIAL_ID = "Y3JlZGVudGlhbC1pZC0x"
WEBAUTHN_PUBLIC_KEY = "Y3JlZGVudGlhbC1wdWJsaWMta2V5LTE"
V2_CI_RECIPIENT_ID = "recipient:ci:sha256:" + "1" * 64
V2_HUMAN_RECIPIENT_ID = "recipient:sha256:" + "2" * 64


def manifest_values(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": 1,
        "project_id": "project",
        "repository_id": REPOSITORY,
        "graph_version": 3,
        "parent_bundle_digest": DIGEST_A,
        "bundle_digest": DIGEST_B,
        "bundle_size": 17,
        "encryption_algorithm": "x25519-hkdf-sha256-aes256gcm-v1",
        "recipient_key_ids": ("recipient:alice", "recipient:bob"),
        "required_signature_ids": ("signer:alice",),
        "created_at": NOW,
    }
    values.update(changes)
    return values


def manifest(**changes: object) -> TeamStateManifest:
    return TeamStateManifest(**manifest_values(**changes))


def canonical_files() -> tuple[CanonicalStateFile, ...]:
    return tuple(
        CanonicalStateFile(path=path, content=path.encode()) for path in CANONICAL_STATE_PATHS
    )


def recipient_values(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": 1,
        "key_id": "recipient:alice",
        "project_id": "project",
        "repository_id": REPOSITORY,
        "actor": "github:1234",
        "github_account_id": "1234",
        "github_login": "alice-dev",
        "public_key": X25519_PUBLIC_KEY,
        "webauthn_credential_id": WEBAUTHN_CREDENTIAL_ID,
        "webauthn_credential_public_key": WEBAUTHN_PUBLIC_KEY,
        "encryption_algorithm": "x25519-hkdf-sha256-aes256gcm-v1",
        "decision_algorithm": "webauthn-decision-v1",
        "enrolled_at": NOW,
    }
    values.update(changes)
    return values


def recipient(**changes: object) -> RecipientRecord:
    return RecipientRecord(**recipient_values(**changes))


def test_manifest_emits_one_bounded_canonical_wire_representation() -> None:
    """Catches locale/order/default serialization changing the signed manifest bytes."""
    value = manifest()

    encoded = canonical_manifest_bytes(value)

    assert encoded == (
        b'{"bundle_digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"bundle_size":17,"created_at":"2026-09-07T10:00:00Z",'
        b'"encryption_algorithm":"x25519-hkdf-sha256-aes256gcm-v1","graph_version":3,'
        b'"parent_bundle_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"project_id":"project","recipient_key_ids":["recipient:alice","recipient:bob"],'
        b'"repository_id":"github.com/acme/project",'
        b'"required_signature_ids":["signer:alice"],"schema_version":1}'
    )
    assert TeamStateManifest.model_validate_json(encoded) == value


def test_v2_manifest_is_strict_canonical_and_migration_bound() -> None:
    """Catches a v2 release omitting its stable-root and legacy bridge authority."""
    value = TeamStateManifestV2(
        project_id="project",
        repository_id=REPOSITORY,
        graph_version=4,
        parent_bundle_digest=DIGEST_A,
        bundle_digest=DIGEST_B,
        bundle_size=17,
        recipient_key_ids=(V2_CI_RECIPIENT_ID, V2_HUMAN_RECIPIENT_ID),
        authority_digest="sha256:" + "d" * 64,
        authority_epoch=1,
        root_key_id="root:sha256:" + "e" * 64,
        created_at=NOW,
        migration=V1MigrationBinding(
            prior_manifest_digest="sha256:" + "f" * 64,
            legacy_signature_ids=("signer:alice",),
        ),
    )

    encoded = canonical_manifest_bytes(value)

    assert b'"archive_version":2' in encoded
    assert TeamStateManifestV2.model_validate_json(encoded) == value
    with pytest.raises(ValidationError):
        TeamStateManifestV2(**{**value.model_dump(), "recipient_key_ids": ["a", "b"]})
    with pytest.raises(ValidationError):
        TeamStateManifestV2(
            **{
                **value.model_dump(),
                "recipient_key_ids": tuple(f"recipient:{index:02d}" for index in range(65)),
            }
        )


@pytest.mark.parametrize(
    "recipient_ids",
    [
        ("recipient:ci", "recipient:human"),
        ("recipient:sha256:" + "1" * 64, "recipient:sha256:" + "2" * 64),
        (
            "recipient:ci:sha256:" + "1" * 64,
            "recipient:ci:sha256:" + "2" * 64,
            "recipient:sha256:" + "3" * 64,
        ),
        ("recipient:ci:sha256:" + "1" * 64, "recipient:other:" + "2" * 64),
    ],
)
def test_v2_manifest_requires_exactly_one_ci_and_at_least_one_human_recipient(
    recipient_ids: tuple[str, ...],
) -> None:
    """Catches malformed or authority-free recipient sets entering signed v2 metadata."""
    value = TeamStateManifestV2(
        project_id="project",
        repository_id=REPOSITORY,
        graph_version=4,
        parent_bundle_digest=DIGEST_A,
        bundle_digest=DIGEST_B,
        bundle_size=17,
        recipient_key_ids=(V2_CI_RECIPIENT_ID, V2_HUMAN_RECIPIENT_ID),
        authority_digest="sha256:" + "d" * 64,
        authority_epoch=1,
        root_key_id="root:sha256:" + "e" * 64,
        created_at=NOW,
    )

    with pytest.raises(ValidationError):
        TeamStateManifestV2(**{**value.model_dump(), "recipient_key_ids": recipient_ids})


def test_restore_uses_the_same_manifest_type_and_canonical_bytes() -> None:
    """Catches publication and restore evolving competing schema-version-1 wire types."""
    value = manifest()
    encoded = canonical_manifest_bytes(value)

    assert SharedStateManifest is TeamStateManifest
    assert canonical_manifest_bytes(SharedStateManifest.model_validate_json(encoded)) == encoded


def test_team_state_package_exports_the_provider_neutral_contracts() -> None:
    """Catches callers being forced back through the Git restore adapter for domain types."""
    assert team_state.TeamStateManifest is TeamStateManifest
    assert team_state.CanonicalStateSnapshot is CanonicalStateSnapshot
    assert team_state.PreparedPublication is PreparedPublication
    assert team_state.canonical_manifest_bytes is canonical_manifest_bytes


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("project_id", "../project"),
        ("repository_id", "https://github.com/acme/project"),
        ("repository_id", "github.com/acme/project.git/extra"),
        ("repository_id", "github.com/../project"),
        ("repository_id", "github.com/acme/.."),
        ("repository_id", "github.com/acme/project.git"),
        ("bundle_digest", "SHA256:" + "b" * 64),
        ("bundle_digest", "sha256:" + "B" * 64),
        ("bundle_digest", "b" * 64),
        ("parent_bundle_digest", "sha256:short"),
        ("encryption_algorithm", "aes-gcm"),
        ("graph_version", 0),
        ("bundle_size", 0),
    ],
)
def test_manifest_rejects_inexact_identity_digest_algorithm_and_bounds(
    field: str, bad: object
) -> None:
    """Catches accepting ambiguous identities or metadata that cannot bind exact bytes."""
    with pytest.raises(ValidationError):
        manifest(**{field: bad})


@pytest.mark.parametrize("field", ["recipient_key_ids", "required_signature_ids"])
@pytest.mark.parametrize(
    "bad",
    [
        ("recipient:bob", "recipient:alice"),
        ("recipient:alice", "recipient:alice"),
        (),
        ["recipient:alice"],
    ],
)
def test_manifest_requires_nonempty_sorted_unique_identifier_tuples(
    field: str, bad: object
) -> None:
    """Catches alternate recipient/signer orderings changing authority over one release."""
    with pytest.raises((ValidationError, ValueError)):
        manifest(**{field: bad})


def test_manifest_rejects_a_bundle_as_its_own_parent() -> None:
    """Catches a self-cycle entering authenticated publication lineage."""
    with pytest.raises(ValidationError):
        manifest(bundle_digest=DIGEST_A, parent_bundle_digest=DIGEST_A)


def test_manifest_requires_utc_and_canonical_z_json() -> None:
    """Catches equivalent offset timestamps acquiring multiple signed byte forms."""
    with pytest.raises(ValidationError):
        manifest(created_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        manifest(created_at=datetime(2026, 9, 7, 11, tzinfo=timezone(timedelta(hours=1))))

    canonical = canonical_manifest_bytes(manifest())
    with pytest.raises(ValueError, match="noncanonical"):
        TeamStateManifest.model_validate_json(canonical.replace(b'Z"', b'+00:00"'))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw.replace(b'"bundle_size":17', b'"bundle_size":"17"'),
        lambda raw: raw.replace(b'{"bundle_digest"', b'{ "bundle_digest"'),
        lambda raw: raw.replace(
            b'{"bundle_digest":',
            b'{"bundle_size":17,"bundle_digest":',
        ),
    ],
)
def test_manifest_parser_rejects_typed_noncanonical_and_duplicate_json(mutation: object) -> None:
    """Catches accepting a second byte representation for the signed manifest object."""
    raw = canonical_manifest_bytes(manifest())
    changed = mutation(raw)  # type: ignore[operator]

    with pytest.raises((TypeError, ValueError, ValidationError)):
        TeamStateManifest.model_validate_json(changed)


@pytest.mark.parametrize(
    "changes",
    [
        {"recipient_key_ids": ()},
        {"repository_id": "github.com/../project"},
        {"created_at": NOW.replace(tzinfo=None)},
    ],
)
def test_canonical_manifest_bytes_revalidates_model_copy_updates(
    changes: dict[str, object],
) -> None:
    """Catches unvalidated Pydantic copies crossing the signed canonical-byte boundary."""
    forged = manifest().model_copy(update=changes)

    with pytest.raises(ValidationError):
        canonical_manifest_bytes(forged)


@pytest.mark.parametrize("path", ["/graph.yaml", "../graph.yaml", "a\\graph.yaml", "graph//x"])
def test_snapshot_rejects_unsafe_or_noncanonical_paths(path: str) -> None:
    """Catches archive construction receiving a path that could escape the restore root."""
    with pytest.raises(ValidationError):
        CanonicalStateFile(path=path, content=b"x")


def test_snapshot_is_complete_sorted_unique_bounded_and_frozen() -> None:
    """Catches omitted, reordered, duplicate, oversized, or mutable canonical state."""
    files = canonical_files()
    value = CanonicalStateSnapshot(
        project_id="project",
        repository_id=REPOSITORY,
        graph_version=3,
        files=files,
    )

    assert value.files == files
    with pytest.raises(ValidationError):
        CanonicalStateSnapshot(
            project_id="project",
            repository_id=REPOSITORY,
            graph_version=3,
            files=files[:-1],
        )
    with pytest.raises(ValidationError):
        CanonicalStateSnapshot(
            project_id="project",
            repository_id=REPOSITORY,
            graph_version=3,
            files=tuple(reversed(files)),
        )
    with pytest.raises(ValidationError):
        CanonicalStateFile(path="graph.yaml", content=b"x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(ValidationError):
        value.graph_version = 4


def test_snapshot_inventory_binds_every_path_size_digest_and_total() -> None:
    """Catches an inventory that can describe bytes other than the snapshot it accompanies."""
    files = canonical_files()
    snapshot = CanonicalStateSnapshot(
        project_id="project",
        repository_id=REPOSITORY,
        graph_version=3,
        files=files,
    )

    inventory = snapshot.inventory()

    expected = tuple(
        BundleInventoryEntry(
            path=item.path,
            size=len(item.content),
            sha256="sha256:" + hashlib.sha256(item.content).hexdigest(),
        )
        for item in files
    )
    assert inventory == BundleInventory(
        entries=expected,
        total_size=sum(item.size for item in expected),
    )
    with pytest.raises(ValidationError):
        BundleInventory(entries=expected, total_size=1)


def test_recipient_is_project_actor_and_webauthn_bound_without_private_material() -> None:
    """Catches a public recipient key being reusable across a project or decision policy."""
    value = recipient()

    assert value.repository_id == REPOSITORY
    assert value.github_account_id == "1234"
    assert value.github_login == "alice-dev"
    assert value.webauthn_credential_id == WEBAUTHN_CREDENTIAL_ID
    assert "private" not in json.dumps(value.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        recipient(public_key="not-base64")
    with pytest.raises(ValidationError):
        recipient(decision_algorithm="agent-approval-v1")


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("repository_id", "GitHub.com/acme/project"),
        ("actor", "GitHub:1234"),
        ("actor", "github:álîce"),
        ("github_account_id", "01234"),
        ("github_account_id", "12a34"),
        ("github_account_id", 1234),
        ("github_login", "Alice-Dev"),
        ("github_login", "álîce"),
        ("github_login", "alice--dev"),
        ("public_key", X25519_PUBLIC_KEY + "="),
        ("public_key", "YQ"),
        ("webauthn_credential_id", ""),
        ("webauthn_credential_id", WEBAUTHN_CREDENTIAL_ID + "="),
        ("webauthn_credential_id", "💥"),
        ("webauthn_credential_id", "YQ" * 700),
        ("webauthn_credential_public_key", ""),
        ("webauthn_credential_public_key", WEBAUTHN_PUBLIC_KEY + "="),
        ("webauthn_credential_public_key", "YQ" * 2800),
    ],
)
def test_recipient_rejects_noncanonical_or_unbounded_identity_and_key_material(
    field: str, bad: object
) -> None:
    """Catches ambiguous GitHub/WebAuthn/X25519 identities entering reviewed team state."""
    with pytest.raises(ValidationError):
        recipient(**{field: bad})


def test_recipient_requires_every_team_identity_binding() -> None:
    """Catches a key-store result that cannot prove repository, GitHub, or WebAuthn ownership."""
    for field in (
        "repository_id",
        "github_account_id",
        "github_login",
        "webauthn_credential_id",
        "webauthn_credential_public_key",
    ):
        values = recipient_values()
        del values[field]
        with pytest.raises(ValidationError):
            RecipientRecord(**values)


def test_recipient_preserves_the_exact_case_sensitive_repository_path() -> None:
    """Catches canonicalization corrupting the repository name returned by GitHub and Git."""
    value = recipient(repository_id="github.com/raviasha/Intent_Engineer")

    assert value.repository_id == "github.com/raviasha/Intent_Engineer"


def test_publication_lineage_distinguishes_genesis_from_descendants() -> None:
    """Catches a release claiming genesis while carrying ancestry, or self-parenting."""
    assert (
        PublicationLineage(
            bundle_digest=DIGEST_A,
            parent_bundle_digest=None,
            ancestor_bundle_digests=(),
        ).parent_bundle_digest
        is None
    )
    descendant = PublicationLineage(
        bundle_digest=DIGEST_B,
        parent_bundle_digest=DIGEST_A,
        ancestor_bundle_digests=(DIGEST_A,),
    )
    assert descendant.ancestor_bundle_digests == (DIGEST_A,)
    for values in (
        {
            "bundle_digest": DIGEST_B,
            "parent_bundle_digest": None,
            "ancestor_bundle_digests": (DIGEST_A,),
        },
        {
            "bundle_digest": DIGEST_A,
            "parent_bundle_digest": DIGEST_A,
            "ancestor_bundle_digests": (DIGEST_A,),
        },
        {
            "bundle_digest": DIGEST_B,
            "parent_bundle_digest": DIGEST_A,
            "ancestor_bundle_digests": (),
        },
    ):
        with pytest.raises(ValidationError):
            PublicationLineage(**values)


def test_remote_snapshot_and_prepared_publication_bind_exact_manifest_and_artifacts() -> None:
    """Catches a ref or publication swapping bytes after the typed manifest was validated."""
    bundle = b"encrypted-bundle"
    bound_manifest = manifest(
        bundle_size=len(bundle),
        bundle_digest="sha256:" + hashlib.sha256(bundle).hexdigest(),
    )
    manifest_bytes = canonical_manifest_bytes(bound_manifest)
    remote = RemoteStateSnapshot(
        repository_id=REPOSITORY,
        ref="refs/remotes/origin/intent-state",
        commit=COMMIT,
        manifest=bound_manifest,
        manifest_bytes=manifest_bytes,
    )
    digest_hex = bound_manifest.bundle_digest.removeprefix("sha256:")
    prepared = PreparedPublication(
        repository_id=REPOSITORY,
        branch=f"intent-publication/{digest_hex}",
        manifest=bound_manifest,
        manifest_bytes=manifest_bytes,
        bundle=bundle,
        signatures=b'{"signatures":[]}',
        bundle_path=f"bundles/3-{digest_hex}.intent",
        signature_path=f"signatures/3-{digest_hex}.json",
        decision_algorithm="webauthn-decision-v1",
    )

    assert remote.manifest == prepared.manifest
    with pytest.raises(ValidationError):
        RemoteStateSnapshot(**{**remote.model_dump(), "manifest_bytes": manifest_bytes + b"\n"})
    with pytest.raises(ValidationError):
        PreparedPublication(**{**prepared.model_dump(), "bundle": bundle + b"x"})
    with pytest.raises(ValidationError):
        PreparedPublication(**{**prepared.model_dump(), "bundle_path": "../bundle.intent"})


def test_containers_revalidate_nested_model_copy_updates() -> None:
    """Catches trusted containers accepting nested model instances that bypassed validation."""
    files = canonical_files()
    valid_inventory = CanonicalStateSnapshot(
        project_id="project",
        repository_id=REPOSITORY,
        graph_version=3,
        files=files,
    ).inventory()
    invalid_entry = valid_inventory.entries[0].model_copy(update={"sha256": "not-a-digest"})
    with pytest.raises(ValidationError):
        BundleInventory(
            entries=(invalid_entry, *valid_inventory.entries[1:]),
            total_size=valid_inventory.total_size,
        )

    invalid_file = files[0].model_copy(update={"path": "../config.yaml"})
    with pytest.raises(ValidationError):
        CanonicalStateSnapshot(
            project_id="project",
            repository_id=REPOSITORY,
            graph_version=3,
            files=(invalid_file, *files[1:]),
        )

    bundle = b"encrypted-bundle"
    valid_manifest = manifest(
        bundle_size=len(bundle),
        bundle_digest="sha256:" + hashlib.sha256(bundle).hexdigest(),
    )
    valid_bytes = canonical_manifest_bytes(valid_manifest)
    invalid_manifest = valid_manifest.model_copy(update={"recipient_key_ids": ()})
    with pytest.raises(ValidationError):
        RemoteStateSnapshot(
            repository_id=REPOSITORY,
            ref="refs/remotes/origin/intent-state",
            commit=COMMIT,
            manifest=invalid_manifest,
            manifest_bytes=valid_bytes,
        )
    digest_hex = valid_manifest.bundle_digest.removeprefix("sha256:")
    with pytest.raises(ValidationError):
        PreparedPublication(
            repository_id=REPOSITORY,
            branch=f"intent-publication/{digest_hex}",
            manifest=invalid_manifest,
            manifest_bytes=valid_bytes,
            bundle=bundle,
            signatures=b'{"signatures":[]}',
            bundle_path=f"bundles/3-{digest_hex}.intent",
            signature_path=f"signatures/3-{digest_hex}.json",
            decision_algorithm="webauthn-decision-v1",
        )
