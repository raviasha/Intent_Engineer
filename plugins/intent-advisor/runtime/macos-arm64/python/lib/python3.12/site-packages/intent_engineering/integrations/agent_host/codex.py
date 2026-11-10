"""Capability detection for mandatory Codex intent preflight."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

from pydantic import ConfigDict, Field, computed_field

from intent_engineering.core.models._base import StrictModel
from intent_engineering.integrations.agent_host.base import MandatoryHookUnavailable

_SUPPORTED_CONTRACT = "0.148.0-alpha.9"
_MANDATORY_SUPPORTED_CONTRACTS: frozenset[str] = frozenset()
_UNSUPPORTED_MUTATION_PATHS = (
    "specialized_tool_hook_opt_out",
    "write_stdin_continuation",
)


class CodexHostContract(StrictModel):
    """Locally detected Codex hook capabilities and completeness limits."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    schema_version: int = Field(default=1, frozen=True, ge=1, le=1)
    contract_version: Annotated[str, Field(min_length=1, max_length=128)]
    hooks_enabled: bool
    plugins_enabled: bool
    pre_tool_use: bool
    synchronous: bool
    command_hook_can_deny: bool
    covers_bash: bool
    covers_unified_exec: bool
    covers_apply_patch: bool
    specialized_paths_may_bypass_hooks: bool
    continuation_pretool_hook_complete: bool
    complete_mutation_coverage: bool
    unsupported_mutation_paths: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=128)], ...],
        Field(max_length=64),
    ] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def can_deny_every_repository_mutation_before_effect(self) -> bool:
        return all(
            (
                self.contract_version in _MANDATORY_SUPPORTED_CONTRACTS,
                self.hooks_enabled,
                self.plugins_enabled,
                self.pre_tool_use,
                self.synchronous,
                self.command_hook_can_deny,
                self.covers_bash,
                self.covers_unified_exec,
                self.covers_apply_patch,
                not self.specialized_paths_may_bypass_hooks,
                self.continuation_pretool_hook_complete,
                self.complete_mutation_coverage,
                not self.unsupported_mutation_paths,
            )
        )


def detect_codex_contract(executable: Path) -> CodexHostContract:
    """Probe local version/features and apply the documented completeness ruling."""
    if not isinstance(executable, Path):
        raise TypeError("invalid Codex capability probe")
    version = "unavailable"
    features: dict[str, tuple[str, bool]] = {}
    try:
        resolved = executable.resolve(strict=True)
        version_result = subprocess.run(
            [str(resolved), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        feature_result = subprocess.run(
            [str(resolved), "features", "list"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        prefix = "codex-cli "
        output = version_result.stdout.strip()
        if output.startswith(prefix):
            version = output.removeprefix(prefix)
        for line in feature_result.stdout.splitlines():
            columns = line.split()
            if len(columns) >= 3 and columns[-1] in {"true", "false"}:
                features[columns[0]] = (" ".join(columns[1:-1]), columns[-1] == "true")
    except Exception:  # noqa: BLE001 - unavailable probe is one incapable contract
        version = "unavailable"
        features = {}
    exact = version == _SUPPORTED_CONTRACT
    hooks = features.get("hooks") == ("stable", True)
    plugins = features.get("plugins") == ("stable", True)
    unified_exec = features.get("unified_exec") == ("stable", True)
    return CodexHostContract(
        contract_version=version,
        hooks_enabled=hooks,
        plugins_enabled=plugins,
        pre_tool_use=exact and hooks,
        synchronous=exact and hooks,
        command_hook_can_deny=exact and hooks,
        covers_bash=exact and hooks,
        covers_unified_exec=exact and unified_exec,
        covers_apply_patch=exact and hooks,
        specialized_paths_may_bypass_hooks=True,
        continuation_pretool_hook_complete=False,
        complete_mutation_coverage=False,
        unsupported_mutation_paths=_UNSUPPORTED_MUTATION_PATHS,
    )


class CodexIntentAdapter:
    """Binary mandatory-mode gate over an audited Codex host contract."""

    def __init__(self, contract: CodexHostContract, *, mandatory: bool) -> None:
        self.contract = contract
        self.mandatory = mandatory

    @classmethod
    def from_contract(
        cls,
        contract: CodexHostContract,
        *,
        mandatory: bool,
    ) -> CodexIntentAdapter:
        if type(contract) is not CodexHostContract or type(mandatory) is not bool:
            raise ValueError("invalid Codex adapter configuration")
        detached = CodexHostContract.model_validate_json(
            contract.model_dump_json(exclude_computed_fields=True)
        )
        if mandatory and not detached.can_deny_every_repository_mutation_before_effect:
            raise MandatoryHookUnavailable()
        return cls(detached, mandatory=mandatory)


__all__ = ["CodexHostContract", "CodexIntentAdapter", "detect_codex_contract"]
