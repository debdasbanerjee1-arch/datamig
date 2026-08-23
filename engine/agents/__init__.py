"""Reasoning units of the pipeline. Each agent owns its logic and prompt and
stays LangGraph-agnostic — nodes.py wires them into the graph. All data shapes
that flow between agents live in contracts.py.

  analyst        — Agent 1: data-analyst, infers a dictionary from data alone
  legacy_expert  — Agent 2: reads COBOL + PHP screen, enriches with business meaning
  mapping_agent  — Agent 3: aligns enriched source to target, validates transforms
  validator      — Agent 4: materialises target, validates the spec holistically
  reviewer       — Agent 5: exception-driven human-in-the-loop gate + drill-down
"""
from . import contracts
from .analyst import analyze
from .contracts import (
    ColumnInsight, DependencyFinding, TableInsight,
    EnrichedColumn, EnrichedDictionary,
    MappingEntry, MappingSpec,
    CheckResult, GateAdjustment, ValidationReport,
    ReviewItem, ReviewQueue,
)
from .legacy_expert import enrich
from .mapping_agent import map_to_target
from .reviewer import apply_decisions, build_review_queue
from .validator import validate_spec

__all__ = [
    "contracts", "analyze", "enrich", "map_to_target", "validate_spec",
    "build_review_queue", "apply_decisions",
    "ColumnInsight", "DependencyFinding", "TableInsight",
    "EnrichedColumn", "EnrichedDictionary", "MappingEntry", "MappingSpec",
    "CheckResult", "GateAdjustment", "ValidationReport",
    "ReviewItem", "ReviewQueue",
]
