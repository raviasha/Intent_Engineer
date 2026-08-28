"""Codex capability-gate contracts for mandatory intent preflight."""

from __future__ import annotations

from pathlib import Path

import pytest

from intent_engineering.integrations.agent_host import MandatoryHookUnavailable
from intent_engineering.integrations.agent_host.codex import (
    CodexHostContract,
    CodexIntentAdapter,
    detect_codex_contract,
)


def _installed_contract() -> CodexHostContract:
    return CodexHostContract(
        contract_version="0.148.0-alpha.9",
        hooks_enabled=True,
        plugins_enabled=True,
        pre_tool_use=True,
        synchronous=True,
        command_hook_can_deny=True,
        covers_bash=True,
        covers_unified_exec=True,
        covers_apply_patch=True,
        specialized_paths_may_bypass_hooks=True,
        continuation_pretool_hook_complete=False,
        complete_mutation_coverage=False,
        unsupported_mutation_paths=(
            "specialized_tool_hook_opt_out",
            "write_stdin_continuation",
        ),
    )


def test_installed_codex_contract_refuses_incomplete_mandatory_coverage() -> None:
    contract = detect_codex_contract(
        Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    )

    assert contract.contract_version == "0.148.0-alpha.9"
    assert contract.hooks_enabled is True
    assert contract.plugins_enabled is True
    assert contract.specialized_paths_may_bypass_hooks is True
    assert contract.continuation_pretool_hook_complete is False
    assert contract.complete_mutation_coverage is False
    assert contract.can_deny_every_repository_mutation_before_effect is False
    with pytest.raises(
        MandatoryHookUnavailable,
        match="^Codex mandatory mutation hook is unavailable$",
    ):
        CodexIntentAdapter.from_contract(contract, mandatory=True)


def test_mandatory_refusal_is_fixed_and_happens_before_side_effects(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_bytes(b"unchanged\n")

    with pytest.raises(MandatoryHookUnavailable) as caught:
        CodexIntentAdapter.from_contract(_installed_contract(), mandatory=True)

    assert str(caught.value) == "Codex mandatory mutation hook is unavailable"
    assert sentinel.read_bytes() == b"unchanged\n"


def test_nonmandatory_codex_gate_is_transparent_and_deeply_detached() -> None:
    contract = _installed_contract()

    adapter = CodexIntentAdapter.from_contract(contract, mandatory=False)

    assert adapter.mandatory is False
    assert adapter.contract == contract
    assert adapter.contract is not contract
    assert (
        CodexHostContract.model_validate_json(
            adapter.contract.model_dump_json(exclude_computed_fields=True)
        )
        == contract
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"contract_version": "unknown"},
        {"hooks_enabled": False},
        {"plugins_enabled": False},
        {"pre_tool_use": False},
        {"synchronous": False},
        {"command_hook_can_deny": False},
        {"covers_bash": False},
        {"covers_unified_exec": False},
        {"covers_apply_patch": False},
        {"specialized_paths_may_bypass_hooks": True},
        {"continuation_pretool_hook_complete": False},
        {"complete_mutation_coverage": False},
        {"unsupported_mutation_paths": ("unknown_mutation_path",)},
    ],
)
def test_any_incomplete_codex_contract_raises_the_same_fixed_error(
    updates: dict[str, object],
) -> None:
    values = _installed_contract().model_dump(exclude_computed_fields=True)
    values.update(
        {
            "specialized_paths_may_bypass_hooks": False,
            "continuation_pretool_hook_complete": True,
            "complete_mutation_coverage": True,
            "unsupported_mutation_paths": (),
        }
    )
    values.update(updates)

    with pytest.raises(
        MandatoryHookUnavailable,
        match="^Codex mandatory mutation hook is unavailable$",
    ):
        CodexIntentAdapter.from_contract(
            CodexHostContract.model_validate(values),
            mandatory=True,
        )


def test_caller_cannot_override_the_audited_installed_contract_ruling() -> None:
    values = _installed_contract().model_dump(exclude_computed_fields=True)
    values.update(
        {
            "specialized_paths_may_bypass_hooks": False,
            "continuation_pretool_hook_complete": True,
            "complete_mutation_coverage": True,
            "unsupported_mutation_paths": (),
        }
    )
    fabricated = CodexHostContract.model_validate(values)
    assert fabricated.can_deny_every_repository_mutation_before_effect is False

    with pytest.raises(
        MandatoryHookUnavailable,
        match="^Codex mandatory mutation hook is unavailable$",
    ):
        CodexIntentAdapter.from_contract(fabricated, mandatory=True)


def test_unsupported_outcome_does_not_ship_a_codex_plugin() -> None:
    plugin = Path(__file__).parents[3] / "plugins" / "intent-preflight"

    assert not plugin.exists()


def test_codex_probe_rejects_non_path_input_without_coercion() -> None:
    with pytest.raises(TypeError, match="invalid Codex capability probe"):
        detect_codex_contract("/Applications/ChatGPT.app/Contents/Resources/codex")  # type: ignore[arg-type]
