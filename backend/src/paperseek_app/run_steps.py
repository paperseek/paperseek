"""Retry boundaries of a research run.

The research workflow is a strict sequential pipeline: retrieval consumes the
query plan, citation expansion consumes retrieval, candidate selection consumes
citation, LLM ranking consumes selection, and the final assembly consumes the
ranking. Re-running one boundary therefore has to re-run every boundary that
consumes its output.

Each boundary below maps to the checkpoint step keys the pipeline writes, so a
retry can drop exactly the durable state of the chosen boundary and everything
downstream of it while keeping the work already completed upstream.
"""

from __future__ import annotations

from paperseek_app.errors import UnknownRunStep

# Ordered pipeline boundaries; a retry drops its target and every later boundary.
RETRY_STEPS: tuple[str, ...] = (
    "query",
    "retrieval",
    "citation",
    "selection",
    "ranking",
    "results",
)

# Checkpoint step keys written by each boundary, matched by exact key or prefix.
_CHECKPOINT_KEYS: dict[str, tuple[str, ...]] = {
    "query": ("query.",),
    "retrieval": ("retrieve.",),
    "citation": ("expand_citations",),
    "selection": ("select_candidates", "selection."),
    "ranking": ("rank_results.",),
    "results": ("assemble_final_result",),
}


def invalidated_checkpoints(step: str) -> tuple[str, ...]:
    """Checkpoint key patterns a retry starting at ``step`` has to drop.

    ``assemble_final_result`` short-circuits the whole workflow when it is
    checkpointed, so it is listed as the last boundary and is therefore always
    part of the returned patterns.
    """
    if step not in RETRY_STEPS:
        raise UnknownRunStep(f"Unknown run step {step!r}.")
    patterns: list[str] = []
    for boundary in RETRY_STEPS[RETRY_STEPS.index(step):]:
        patterns.extend(_CHECKPOINT_KEYS[boundary])
    return tuple(dict.fromkeys(patterns))


__all__ = ["RETRY_STEPS", "invalidated_checkpoints"]
