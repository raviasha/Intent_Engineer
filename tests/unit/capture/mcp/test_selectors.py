"""Behavioral contracts for declarative MCP selectors and argument binding."""

from __future__ import annotations

import traceback
from collections.abc import Iterator, Mapping
from math import inf
from types import MappingProxyType
from typing import NoReturn

import pytest

from intent_engineering.capture.mcp.profile_models import ArgumentBinding, Selector
from intent_engineering.capture.mcp.selectors import (
    SelectorError,
    TransformError,
    apply_transform,
    bind_arguments,
    select_value,
)
from intent_engineering.core.models import JsonValue


class _HostileMapping(Mapping[str, JsonValue]):
    """A mapping whose protocol access would disclose its secret if invoked."""

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self.invoked = False

    def _raise(self) -> NoReturn:
        self.invoked = True
        raise RuntimeError(self._secret)

    def __getitem__(self, key: str) -> JsonValue:
        self._raise()

    def __iter__(self) -> Iterator[str]:
        self._raise()

    def __len__(self) -> int:
        return self._raise()


def _captured_traceback(error: BaseException) -> str:
    traceback_exception = traceback.TracebackException.from_exception(error, capture_locals=True)
    return "\n".join(
        str(frame.locals)
        for frame in traceback_exception.stack or ()
        if "intent_engineering/capture/mcp/selectors.py" in frame.filename
    )


def test_selector_returns_json_null_but_only_returns_none_for_a_missing_optional_path() -> None:
    """Catches conflating an explicit provider null with a path that does not exist."""
    payload: dict[str, JsonValue] = {"record": {"nullable": None, "items": [{"id": "one"}]}}

    assert select_value(payload, Selector(path="$.record.nullable", required=False)) is None
    assert select_value(payload, Selector(path="$.record.absent", required=False)) is None
    assert select_value(payload, Selector(path="$.record.items.0.id")) == "one"
    with pytest.raises(SelectorError, match="required selector path is missing"):
        select_value(payload, Selector(path="$.record.absent"))


@pytest.mark.parametrize(
    "path",
    [
        "",
        "records",
        "$.records..id",
        "$.records.*.id",
        "$..id",
        "$.records[0]",
        "$.records.01",
        "$.records.-1",
        "$.records.__class__",
        "$.records.\x00id",
    ],
)
def test_selector_rejects_every_non_declarative_or_ambiguous_path(path: str) -> None:
    """Catches grammar expansion that could select unbounded or unsafe provider data."""
    with pytest.raises(ValueError, match="invalid selector path"):
        Selector(path=path)


def test_selector_type_and_index_failures_are_redacted_and_stable() -> None:
    """Catches raw provider values leaking through mapping/index error paths."""
    secret = "provider-secret"
    payload: dict[str, JsonValue] = {"record": secret, "items": []}

    for selector in (Selector(path="$.record.id"), Selector(path="$.items.0")):
        with pytest.raises(SelectorError) as caught:
            select_value(payload, selector)
        assert str(caught.value) == "invalid selector access"
        assert secret not in repr(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


def test_transforms_normalize_only_their_declared_json_shapes() -> None:
    """Catches noncanonical output and accidental transform behavior changes."""
    assert apply_transform("string", 42) == "42"
    assert apply_transform("integer", "42") == 42
    assert apply_transform("iso_datetime", "2026-08-26T08:15:00+05:30") == "2026-08-26T02:45:00Z"
    assert apply_transform("string_list", ["a", "b"]) == ["a", "b"]
    assert apply_transform("canonical_json", {"b": True, "a": [2, 1]}) == '{"a":[2,1],"b":true}'
    assert (
        apply_transform("sha256", "content")
        == "sha256:ed7002b439e9ac845f22357d822bac1444730fbdb6016d3ec9432297b9ec9f73"
    )


@pytest.mark.parametrize(
    ("transform", "value"),
    [
        ("integer", True),
        ("integer", inf),
        ("iso_datetime", "2026-08-26T08:15:00"),
        ("string_list", ["a", 2]),
        ("missing", "value"),
    ],
)
def test_transforms_reject_invalid_or_ambiguous_values(transform: str, value: object) -> None:
    """Catches boolean coercion, nonfinite numbers, ambiguous time, and unknown transforms."""
    with pytest.raises(TransformError, match="invalid selector transform"):
        apply_transform(transform, value)


def test_selector_rejects_incompatible_transform_chains() -> None:
    """Catches chains that attempt to reinterpret a normalized scalar as a list."""
    with pytest.raises(ValueError, match="incompatible selector transforms"):
        Selector(path="$.items", transforms=["canonical_json", "string_list"])


def test_bind_arguments_uses_only_declared_sources_and_detaches_json_values() -> None:
    """Catches accepting arbitrary context sources or returning aliases to caller-owned values."""
    constant: dict[str, JsonValue] = {"nested": ["fixed"]}
    fields: dict[str, JsonValue] = {"text": {"parts": ["one"]}}
    context: dict[str, JsonValue] = {
        "scope": {"workspace": "alpha"},
        "cursor": "cursor-1",
        "object_id": "object-1",
        "object_version": "version-1",
        "target_id": "target-1",
        "before_version": "before-1",
        "fields": fields,
    }
    bindings = {
        "fixed": ArgumentBinding(source="constant", value=constant),
        "scope": ArgumentBinding(source="scope"),
        "text": ArgumentBinding(source="field", field="text"),
    }

    result = bind_arguments(bindings, context)
    constant["nested"] = ["changed"]
    fields["text"] = "changed"

    assert result == {
        "fixed": {"nested": ["fixed"]},
        "scope": {"workspace": "alpha"},
        "text": {"parts": ["one"]},
    }
    assert not isinstance(result, MappingProxyType)


@pytest.mark.parametrize(
    ("binding", "context"),
    [
        (ArgumentBinding.model_construct(source="field", field=None, value=None), {"fields": {}}),
        (ArgumentBinding(source="field", field="text"), {"fields": {}}),
        (ArgumentBinding(source="cursor"), {}),
    ],
)
def test_bind_arguments_rejects_missing_required_context_and_field(
    binding: ArgumentBinding,
    context: dict[str, JsonValue],
) -> None:
    """Catches defaulting a required argument to null or accepting malformed bindings."""
    with pytest.raises(SelectorError, match="invalid argument binding"):
        bind_arguments({"argument": binding}, context)


def test_selector_transform_and_binding_errors_clear_secret_bearing_traceback_locals() -> None:
    """Catches fixed errors whose traceback frames retain provider or write secrets."""
    secret = "selector-write-secret"
    failures: list[BaseException] = []

    with pytest.raises(SelectorError) as selected:
        select_value({"record": secret}, Selector(path="$.record.id"))
    failures.append(selected.value)

    with pytest.raises(TransformError) as transformed:
        apply_transform("integer", secret)
    failures.append(transformed.value)

    with pytest.raises(SelectorError) as bound:
        bind_arguments(
            {
                "fixed": ArgumentBinding(source="constant", value="safe"),
                "cursor": ArgumentBinding(source="cursor"),
            },
            {"cursor": (secret,)},  # type: ignore[dict-item]
        )
    failures.append(bound.value)

    for failure in failures:
        assert failure.args in {
            ("invalid selector access",),
            ("invalid selector transform",),
            ("invalid argument binding",),
        }
        assert failure.__cause__ is None
        assert failure.__context__ is None
        assert secret not in _captured_traceback(failure)


def test_selector_rejects_nonexact_external_json_without_invoking_mapping_protocol() -> None:
    """Catches arbitrary Mapping coercion at the selector input boundary."""
    payload = _HostileMapping("mapping-secret")
    with pytest.raises(SelectorError) as caught:
        select_value(payload, Selector(path="$.value"))

    assert str(caught.value) == "invalid selector access"
    assert payload.invoked is False


def test_selector_rejects_tuple_external_json() -> None:
    """Catches tuple coercion at the selector input boundary."""
    with pytest.raises(SelectorError, match="invalid selector access"):
        select_value(("secret",), Selector(path="$.value"))


def test_bind_arguments_rejects_nonexact_external_json_context_without_protocol_invocation() -> None:
    """Catches arbitrary Mapping coercion at the argument-context boundary."""
    context = _HostileMapping("mapping-secret")
    with pytest.raises(SelectorError) as caught:
        bind_arguments({"cursor": ArgumentBinding(source="cursor")}, context)  # type: ignore[arg-type]

    assert str(caught.value) == "invalid argument binding"
    assert context.invoked is False


def test_bind_arguments_rejects_tuple_external_json_context() -> None:
    """Catches tuple coercion at the argument-context boundary."""
    with pytest.raises(SelectorError, match="invalid argument binding"):
        bind_arguments({"cursor": ArgumentBinding(source="cursor")}, {"cursor": ("secret",)})  # type: ignore[dict-item]


def test_bind_arguments_thaws_trusted_constant_without_aliasing_it() -> None:
    """Catches returning a model-frozen constant or treating it as untrusted external mapping input."""
    binding = ArgumentBinding(source="constant", value={"nested": ["fixed"]})

    result = bind_arguments({"constant": binding}, {})
    result["constant"]["nested"].append("changed")  # type: ignore[index,union-attr]

    assert binding.value == {"nested": ("fixed",)}
    assert result == {"constant": {"nested": ["fixed", "changed"]}}


def test_select_value_preserves_transform_error_type_and_redacts_provider_locals() -> None:
    """Catches transform errors accidentally crossing the selector boundary as SelectorError."""
    secret = "invalid-transform-secret"
    selector = Selector(path="$.amount", transforms=("integer",))

    with pytest.raises(TransformError) as caught:
        select_value({"amount": secret}, selector)

    assert str(caught.value) == "invalid selector transform"
    assert caught.value.args == ("invalid selector transform",)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in _captured_traceback(caught.value)
