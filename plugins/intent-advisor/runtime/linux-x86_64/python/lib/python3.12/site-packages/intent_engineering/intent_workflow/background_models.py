"""Bounded interpretation drafts for conversational intent maintenance."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from intent_engineering.core.models import NodeType, RelationType, SourceMode
from intent_engineering.core.models._base import StrictModel

Text = Annotated[str, Field(min_length=1, max_length=4096)]
Identity = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[^\s\x00-\x1f]+$")]


class BackgroundModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BackgroundAssertion(BackgroundModel):
    id: Identity
    type: NodeType
    label: Text
    source_mode: Literal[SourceMode.EXPLICIT, SourceMode.INFERRED]
    confidence: Annotated[float, Field(ge=0, le=0.95)]
    basis: Text
    evidence_refs: Annotated[tuple[Identity, ...], Field(max_length=64)] = ()
    revision_quote: Text | None = None


class BackgroundLink(BackgroundModel):
    id: Identity
    from_id: Identity
    relation: RelationType
    to_id: Identity


class BackgroundAnswer(BackgroundModel):
    question_ref: Identity
    quote: Text
    resolution: Literal["clarify", "retain_existing", "revise_intent", "preserve_disagreement"] = (
        "clarify"
    )


class BackgroundSubmission(BackgroundModel):
    """Agent interpretation; reported user words never constitute human approval."""

    session_id: Identity
    turn_id: Identity
    graph_version: Annotated[int, Field(ge=0)]
    user_statement: Annotated[str, Field(min_length=1, max_length=16384)]
    classification: Literal["unrelated", "aligned", "needs_clarification", "conflicting", "clear"]
    understanding: Text
    relevant_node_ids: Annotated[tuple[Identity, ...], Field(max_length=64)] = ()
    evidence_refs: Annotated[tuple[Identity, ...], Field(max_length=64)] = ()
    question: Text | None = None
    conflict: Text | None = None
    answers: Annotated[tuple[BackgroundAnswer, ...], Field(max_length=16)] = ()
    nodes: Annotated[tuple[BackgroundAssertion, ...], Field(max_length=128)] = ()
    edges: Annotated[tuple[BackgroundLink, ...], Field(max_length=256)] = ()

    @model_validator(mode="after")
    def check_shape(self) -> "BackgroundSubmission":
        questioning = self.classification in {"needs_clarification", "conflicting"}
        if questioning != bool(self.question):
            raise ValueError("only consequential clarification may ask a question")
        if (self.classification == "conflicting") != bool(self.conflict):
            raise ValueError("conflict requires a specific conflicting claim")
        if self.classification != "clear" and (self.nodes or self.edges):
            raise ValueError("clarify meaning before maintaining the graph")
        if self.classification in {"aligned", "conflicting"} and not self.relevant_node_ids:
            raise ValueError("relevant intent references required")
        if self.classification == "unrelated" and (
            self.relevant_node_ids or self.evidence_refs or self.answers
        ):
            raise ValueError("unrelated task cannot carry semantic effects")
        for items in (
            self.relevant_node_ids,
            self.evidence_refs,
            tuple(n.id for n in self.nodes),
            tuple(e.id for e in self.edges),
            tuple(a.question_ref for a in self.answers),
        ):
            if len(items) != len(set(items)):
                raise ValueError("duplicate background identity")
        return self
