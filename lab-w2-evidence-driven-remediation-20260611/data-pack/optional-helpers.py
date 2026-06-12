"""Optional parsing helpers for the schema gotchas in HANDOUT §2.6.

These are pure mechanical conversions — no algorithmic choices.
Skip this file and write your own parsers if you prefer.
"""
from __future__ import annotations


def parse_history_action(s: str) -> dict:
    """Parse historical-corpus action format into structured form.

    >>> parse_history_action("rollback_service:payment-svc:v3.1")
    {'name': 'rollback_service', 'params': ['payment-svc', 'v3.1']}

    Mapping positional params to named params per actions.yaml is your
    responsibility (action 'rollback_service' takes params [service, target_version]).
    """
    parts = s.split(":")
    if not parts:
        return {"name": "page_oncall", "params": []}
    return {"name": parts[0], "params": parts[1:]}


def parse_metric_delta(s: str) -> tuple[float, float]:
    """Parse metric_signatures[*].delta string into (before, after) floats.

    >>> parse_metric_delta("30 -> 99")
    (30.0, 99.0)
    >>> parse_metric_delta("0.001 -> 1.0")
    (0.001, 1.0)
    >>> parse_metric_delta("50->100")
    (50.0, 100.0)
    """
    parts = s.replace("->", "|").split("|")
    if len(parts) != 2:
        raise ValueError(f"unexpected delta format: {s!r}")
    return float(parts[0].strip()), float(parts[1].strip())
