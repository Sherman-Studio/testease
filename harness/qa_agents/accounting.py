"""Token accounting for a persona run.

The Agent SDK's ``ResultMessage`` carries a ``usage`` dict. The harness
records one ``PhaseUsage`` per ``query()`` call (explore + report) and sums
them into run-level token totals.

#1822 — the per-run dollar conversion was retired. Every run bills the
operator's flat-rate Claude Code Max subscription (the org API key was
removed in #1788), so a USD figure was a vanity number computed from a
price table that had to be kept current by hand. The accounting now stops
at raw token counts; old run documents that carry ``cost_usd`` /
``real_cost_usd`` keys are simply passed through by their readers.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PhaseUsage:
    """Token tally for one ``query()`` phase."""

    phase: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    num_turns: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_tokens": self.total_tokens,
            "num_turns": self.num_turns,
        }


def _coerce_usage(usage: object) -> dict:
    """Normalise a ResultMessage.usage value into a plain dict of ints."""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    # Some SDK versions hand back an object with attributes rather than a dict.
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    return {k: getattr(usage, k, 0) for k in keys}


@dataclass
class RunAccounting:
    """Accumulates per-phase usage across an entire persona run."""

    phases: list[PhaseUsage] = field(default_factory=list)
    # Which LLM backend ran this persona. Always ``claude-code``: the
    # spawned ``claude`` subprocess falls through to OAuth (the operator's
    # Claude Code Max subscription) and no API charge is incurred. The
    # Atlas sink and review-UI key off this field to render the Max pill —
    # it is retained (not hard-coded away) so those downstream readers
    # keep a stable shape.
    backend: str = "claude-code"

    def record(
        self,
        phase: str,
        model: str,
        usage: object,
        num_turns: int = 0,
    ) -> PhaseUsage:
        """Record one ``query()`` phase from its ResultMessage usage."""
        u = _coerce_usage(usage)
        pu = PhaseUsage(
            phase=phase,
            model=model,
            input_tokens=int(u.get("input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cache_creation_input_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
            cache_read_input_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
            num_turns=int(num_turns or 0),
        )
        self.phases.append(pu)
        return pu

    @property
    def total_input_tokens(self) -> int:
        return sum(p.input_tokens for p in self.phases)

    @property
    def total_output_tokens(self) -> int:
        return sum(p.output_tokens for p in self.phases)

    @property
    def total_cache_tokens(self) -> int:
        return sum(
            p.cache_creation_input_tokens + p.cache_read_input_tokens for p in self.phases
        )

    @property
    def total_tokens(self) -> int:
        return sum(p.total_tokens for p in self.phases)

    @property
    def total_turns(self) -> int:
        return sum(p.num_turns for p in self.phases)

    def as_dict(self) -> dict:
        return {
            "backend": self.backend,
            "phases": [p.as_dict() for p in self.phases],
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cache_tokens": self.total_cache_tokens,
            "total_tokens": self.total_tokens,
            "total_turns": self.total_turns,
        }
