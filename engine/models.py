"""Core shared primitives used across the agent pipeline.

Kept deliberately small: the gating tiers and helper that the mapping and
validation agents share. Agent-specific I/O shapes live in agents/contracts.py.
"""
from __future__ import annotations

from enum import Enum

# three-tier gating thresholds (confidence is on a 0–1 scale)
#   >= 0.85         -> auto-accept   (strong, unambiguous, type- & value-confirmed)
#   0.60 – 0.85     -> review        (plausible but needs a human: ambiguous source,
#                                      unresolved codes, derived/composed values)
#   < 0.60          -> reject        (too weak to trust as-is)
# The mapping agent additionally *caps* gates on semantic grounds — an ambiguous or
# unconfirmable match can't auto-accept however cleanly its SQL runs — so these
# numeric bands and those caps together decide the final gate.
AUTO_ACCEPT_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.60


class Gate(str, Enum):
    AUTO_ACCEPT = "auto_accept"   # >= 0.85
    REVIEW = "review"             # 0.60 - 0.85
    REJECT = "reject"             # < 0.60


def decide_gate(confidence: float) -> Gate:
    if confidence >= AUTO_ACCEPT_THRESHOLD:
        return Gate.AUTO_ACCEPT
    if confidence >= REVIEW_THRESHOLD:
        return Gate.REVIEW
    return Gate.REJECT


def target_table_name(td: dict) -> str:
    """Target table name — tolerate key variants in a user-supplied dictionary."""
    return td.get("table") or td.get("target_table") or td.get("name") or "target"


def target_attributes(td: dict) -> list[dict]:
    """Target attributes as a list of dicts — accept 'attributes'/'columns'/'fields',
    and allow bare strings (treated as string-typed attributes)."""
    attrs = td.get("attributes") or td.get("columns") or td.get("fields") or []
    out = []
    for a in attrs:
        if isinstance(a, str):
            a = {"name": a, "type": "string"}
        a.setdefault("type", "string")
        out.append(a)
    return out
