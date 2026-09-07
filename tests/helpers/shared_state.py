"""Deterministic Git-ref fixtures for approved shared-state restore tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from intent_engineering.capture.base import RawSourceObject, normalize_raw_source
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import (
    ChangeSet,
    Edge,
    Node,
    NodeType,
    RelationType,
    SourceMode,
)
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.team_state.restore import (
    CANONICAL_STATE_PATHS,
    SharedStateArtifacts,
    SharedStateTrust,
    TrustedSigningKey,
    build_state_payload,
    seal_state_payload,
)

NOW = datetime(2026, 9, 7, 10, tzinfo=UTC)
REPOSITORY_ID = "github.com/acme/project"
RECIPIENT_ID = "recipient:ci"
SIGNER_ID = "signer:release"


def git(repo: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Shared State Fixture",
        "GIT_AUTHOR_EMAIL": "state@example.test",
        "GIT_COMMITTER_NAME": "Shared State Fixture",
        "GIT_COMMITTER_EMAIL": "state@example.test",
        "GIT_AUTHOR_DATE": "2026-09-07T10:00:00Z",
        "GIT_COMMITTER_DATE": "2026-09-07T10:00:00Z",
    }
    return subprocess.run(
        ("git", *arguments),
        cwd=repo,
        env=environment,
        input=input_bytes,
        check=True,
        capture_output=True,
    ).stdout


def init_repository(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "--quiet")
    git(path, "remote", "add", "origin", "https://github.com/acme/project.git")
    (path / "README.md").write_text("code branch\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "--quiet", "-m", "initial code")
    return path


def canonical_files(source: Path) -> dict[str, bytes]:
    workspace = source / ".intent"
    return {
        path: (workspace / path).read_bytes() if (workspace / path).exists() else b""
        for path in CANONICAL_STATE_PATHS
    }


def ready_project(root: Path) -> None:
    """Create the smallest valid graph/evidence/history baseline for restore tests."""
    initialize_project(root)
    runtime = load_runtime(root)
    try:
        content = "Approved shared-state baseline"
        content_hash = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        evidence = normalize_raw_source(
            RawSourceObject(
                connector_type="markdown",
                external_object_id="path:approved-intent.md",
                external_version=content_hash,
                author="local:owner",
                observed_at=NOW,
                source_locator="approved-intent.md",
                content_hash=content_hash,
                payload={"content": content},
            )
        )
        runtime.evidence_store.associate("markdown", evidence)
        intent = Node(
            id="intent:approved",
            type=NodeType.PRODUCT_INTENT,
            label=content,
            status="active",
            created_by="local:owner",
            created_at=NOW,
            last_modified_by="local:owner",
            last_modified_at=NOW,
            source_mode=SourceMode.EXPLICIT,
            intent_fidelity_confidence=1.0,
            confidence_basis="approved test fixture",
            evidence_refs=(evidence.id,),
        )
        requirement = Node(
            id="requirement:approved",
            type=NodeType.REQUIREMENT,
            label="Restore the approved baseline",
            status="active",
            created_by="local:owner",
            created_at=NOW,
            last_modified_by="local:owner",
            last_modified_at=NOW,
            source_mode=SourceMode.EXPLICIT,
            intent_fidelity_confidence=1.0,
            confidence_basis="approved test fixture",
            evidence_refs=(evidence.id,),
        )
        runtime.graph_store.apply(
            ChangeSet(
                id="changeset:approved-shared-state",
                actor="local:owner",
                timestamp=NOW,
                baseline_graph_version=0,
                evidence_refs=(evidence.id,),
                nodes_added=(intent, requirement),
                nodes_updated=(),
                nodes_superseded=(),
                edges_added=(
                    Edge(
                        id="edge:approved-requirement",
                        from_id=intent.id,
                        relation=RelationType.REFINES,
                        to_id=requirement.id,
                        status="active",
                        created_by="local:owner",
                        created_at=NOW,
                        last_modified_by="local:owner",
                        last_modified_at=NOW,
                    ),
                ),
                edges_updated=(),
                edges_superseded=(),
                confidence_changes=(),
                implementation_status_changes=(),
                reconciliation_cases_created=(),
                reconciliation_cases_resolved=(),
                validation_status="validated",
            )
        )
    finally:
        runtime.close()


def keys(
    *, project_id: str = "project"
) -> tuple[X25519PrivateKey, Ed25519PrivateKey, SharedStateTrust]:
    recipient = X25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    trust = SharedStateTrust(
        project_id=project_id,
        repository_id=REPOSITORY_ID,
        recipient_key_id=RECIPIENT_ID,
        recipient_private_key=recipient.private_bytes_raw(),
        signing_keys=(
            TrustedSigningKey(
                signature_id=SIGNER_ID,
                public_key=signer.public_key().public_bytes_raw(),
            ),
        ),
    )
    return recipient, signer, trust


def trust_environment(trust: SharedStateTrust) -> str:
    """Encode the fixed, strict production CI trust-provider input."""
    return json.dumps(
        {
            "schema_version": 1,
            "project_id": trust.project_id,
            "repository_id": trust.repository_id,
            "recipient_key_id": trust.recipient_key_id,
            "recipient_private_key_base64": _fixture_b64(trust.recipient_private_key),
            "signing_keys": [
                {
                    "signature_id": item.signature_id,
                    "public_key_base64": _fixture_b64(item.public_key),
                }
                for item in trust.signing_keys
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _fixture_b64(content: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def artifacts(
    files: Mapping[str, bytes],
    recipient: X25519PrivateKey,
    signer: Ed25519PrivateKey,
    *,
    project_id: str = "project",
    repository_id: str = REPOSITORY_ID,
    schema_version: int = 1,
    parent_bundle_digest: str | None = None,
    payload: bytes | None = None,
) -> SharedStateArtifacts:
    plaintext = build_state_payload(files) if payload is None else payload
    return seal_state_payload(
        plaintext,
        project_id=project_id,
        repository_id=repository_id,
        graph_version=1,
        parent_bundle_digest=parent_bundle_digest,
        created_at=NOW,
        recipient_public_keys={RECIPIENT_ID: recipient.public_key().public_bytes_raw()},
        signing_private_keys={SIGNER_ID: signer.private_bytes_raw()},
        schema_version=schema_version,
    )


def install_state_ref(
    repo: Path,
    release: SharedStateArtifacts,
    *,
    parent: str | None = None,
    modes: Mapping[str, str] | None = None,
) -> str:
    blobs = {
        "manifest.json": release.manifest,
        release.bundle_path: release.bundle,
        release.signature_path: release.signatures,
    }
    index = repo / ".git" / "intent-state-test-index"
    environment = {**os.environ, "GIT_INDEX_FILE": str(index)}
    subprocess.run(
        ("git", "read-tree", "--empty"),
        cwd=repo,
        env=environment,
        check=True,
        capture_output=True,
    )
    for path, content in blobs.items():
        blob = git(repo, "hash-object", "-w", "--stdin", input_bytes=content).decode().strip()
        subprocess.run(
            (
                "git",
                "update-index",
                "--add",
                "--cacheinfo",
                (modes or {}).get(path, "100644"),
                blob,
                path,
            ),
            cwd=repo,
            env=environment,
            check=True,
            capture_output=True,
        )
    tree = subprocess.run(
        ("git", "write-tree"),
        cwd=repo,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    arguments = ["commit-tree", tree]
    if parent is not None:
        arguments.extend(("-p", parent))
    commit = git(repo, *arguments, input_bytes=b"approved state\n").decode().strip()
    git(repo, "update-ref", "refs/remotes/origin/intent-state", commit)
    index.unlink(missing_ok=True)
    return commit
