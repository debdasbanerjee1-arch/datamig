"""Shared state threaded through the pipeline graph.

Inputs come in at the top; each node writes its artifact back. The DuckDB
warehouse (the staging layer) is created once and shared by every node.
"""
from __future__ import annotations

from typing import TypedDict

from ..agents.contracts import (EnrichedDictionary, MappingSpec, ReviewQueue,
                               TableInsight, ValidationReport)
from ..staging import Warehouse


class PipelineState(TypedDict, total=False):
    # inputs
    source_csv: str
    table: str
    sources: list                    # multi-source: [{"table": str, "path": str}]
    primary_table: str               # the driving table for the composite join
    code_dir: str
    target_dict_path: str
    warehouse_path: str
    kg_path: str                     # knowledge store (default data/knowledge.duckdb)
    kg_version: int                  # Flow B: which knowledge version to consume
    force: bool                      # Flow A: re-run even if fingerprint unchanged
    decisions: dict                  # optional human decisions {target_attr: {...}}
    spec_in: dict                    # certify-only: the reviewed spec sent back by the client
    # manual mapping workspace: the artefact the client uploaded directly
    # instead of Flow A computing it / the knowledge store persisting it.
    # (The source insight is NOT here — it is derived from source_csv by
    # engine/insight_cache.py.)
    enriched_json: dict              # single-source: one raw EnrichedDictionary
    sources: list                    # multi-source: [{path, table}, ...]
    enriched_dicts: list             # multi-source: one raw dictionary per file
    source_tables: list              # the staged tables that fed the workset
    join_plan: list                  # how they were joined (composite.plan_joins)
    excluded_sources: list           # files with no safe, grain-preserving edge
    relationships: object            # SourceRelationships — discovered, for the UI
    # shared infra + loaded inputs (set by stage_source)
    warehouse: Warehouse
    cobol_text: str
    php_text: str
    target_dict: dict
    # artifacts, one per agent node
    insight: TableInsight            # Agent 1
    enriched: EnrichedDictionary     # Agent 2
    spec: MappingSpec                # Agent 3
    report: ValidationReport         # Agent 4
    review_queue: ReviewQueue        # Agent 5 (the gate output)
    # knowledge persistence (Flow A output / Flow B provenance)
    kg_result: dict                  # {version, fingerprint, reused, counts}
    # multi-source (Flow B composite)
    relationships: dict              # SourceRelationships dump
    join_plan: list                  # how the workset was assembled
    excluded_sources: list           # files left out of the workset + why
