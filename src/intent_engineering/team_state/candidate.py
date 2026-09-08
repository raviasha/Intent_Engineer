"""Read-only validation of an exact proposed state release, never candidate code."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from intent_engineering.team_state.restore import (
    SharedStateTrust,
    TrustProvider,
    _decrypt_release_payload,
    _GitRefReader,
    _origin_repository,
    _release_name,
    _run_git,
    _validate_authenticated_state,
    _verify_release_lineage,
)


def validate_candidate(
    root: Path, provider: TrustProvider, *, base: str, head: str, at: datetime
) -> None:
    """Require exact live base, sole-parent lineage, signatures and canonical plaintext."""
    reader = _GitRefReader(root)
    trust = None
    files: dict[str, bytes] = {}
    failure: BaseException | None = None
    try:
        if any(
            re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value) is None for value in (base, head)
        ):
            raise ValueError("invalid state candidate")
        trust = provider.load()
        if type(trust) is not SharedStateTrust or _origin_repository(root) != trust.repository_id:
            raise ValueError("invalid state trust")
        if reader.commit() != base or reader.parents(head) != (base,):
            raise ValueError("state candidate changed")
        lineage = _verify_release_lineage(reader, head, trust, at, None)
        suffix = _release_name(lineage.tip.manifest)
        expected = {"manifest.json", f"bundles/{suffix}.intent", f"signatures/{suffix}.json"}
        listing = _run_git(root, ("ls-tree", "-rz", "--full-tree", head), maximum=65536)
        paths = set()
        for item in listing.rstrip(b"\0").split(b"\0"):
            metadata, path = item.split(b"\t", 1)
            if not metadata.startswith(b"100644 blob "):
                raise ValueError("unsafe state candidate")
            paths.add(path.decode("utf-8"))
        if paths != expected:
            raise ValueError("state candidate inventory changed")
        files = _decrypt_release_payload(reader, lineage.tip, trust)
        _validate_authenticated_state(files, lineage.tip.manifest, trust)
        if reader.commit() != base:
            raise ValueError("state base changed")
    except BaseException as caught:  # noqa: BLE001 - erase secret-bearing verifier frames
        caught.__traceback__ = None
        caught.__cause__ = None
        caught.__context__ = None
        failure = (
            caught
            if not isinstance(caught, Exception)
            else ValueError("state candidate unavailable")
        )
    finally:
        files.clear()
        trust = None
        provider = None  # type: ignore[assignment]
        reader.close()
    if failure is not None:
        raise failure.with_traceback(None) from None
