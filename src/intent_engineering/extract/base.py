"""Provider-neutral boundary between durable evidence and graph mutations."""

from collections.abc import Sequence
from typing import Protocol

from intent_engineering.core.models import CandidateAssertion, ChangeSet, EvidenceDelta, Graph


class SemanticReasoner(Protocol):
    """Extract evidence-backed assertions and map them to a graph ChangeSet."""

    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        """Return typed semantic assertions for newly durable evidence."""
        raise NotImplementedError

    def map_to_graph(
        self, assertions: Sequence[CandidateAssertion], graph: Graph
    ) -> ChangeSet:
        """Return a validated change proposal against the supplied graph version."""
        raise NotImplementedError
