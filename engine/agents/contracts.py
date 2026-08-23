"""Shared data contracts for the agent pipeline.

Every shape that flows *between* agents (and out to the API / review UI) lives
here, so consumers import the data type without pulling in an agent's logic, and
no agent depends on another agent's module. The grouping mirrors the pipeline:

  Agent 1 (analyst)        -> TableInsight  (+ ColumnInsight, DependencyFinding)
  Agent 2 (legacy expert)  -> EnrichedDictionary (+ EnrichedColumn)
  Agent 3 (mapping agent)  -> MappingSpec (+ MappingEntry)
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ..models import Gate


# ============================ Agent 1: analyst ============================
class ColumnInsight(BaseModel):
    name: str
    inferred_type: str                       # DATE_YYYYMMDD, IDENTIFIER, CATEGORICAL_CODE, ...
    role: str                                # candidate key / code / date / measure / dead / constant
    row_count: int
    populated_fraction: float                # excludes blanks AND detected sentinels
    distinct_count: int
    distinct_ratio: float
    top_values: dict[str, float] = Field(default_factory=dict)   # value -> freq (low-card only)
    sentinels: list[str] = Field(default_factory=list)           # e.g. ["00000000"]
    observations: list[str] = Field(default_factory=list)        # evidence, data-derived
    hypotheses: list[str] = Field(default_factory=list)          # what the column probably is


class DependencyFinding(BaseModel):
    statement: str                           # human-readable
    dependent: str                           # the conditionally-populated column
    drivers: list[str] = Field(default_factory=list)             # columns it depends on
    condition: Optional[str] = None          # e.g. "XA03 in {GPEN}"
    support_rows: int = 0                     # rows backing the pattern
    confidence: float = 0.0                   # how cleanly the condition predicts population


class DqFinding(BaseModel):
    column: str
    completeness: float                       # populated fraction (excl. blanks/sentinels)
    validity: float                           # fraction conforming to inferred type
    distinct_ratio: float
    issues: list[str] = Field(default_factory=list)
    severity: str = "ok"                      # ok | minor | major


class DQRule(BaseModel):
    """One EXECUTABLE data-quality rule: the finding for the sample extract,
    plus the exact SQL that reruns it at full volume during cleansing."""
    id: str
    name: str
    category: str                              # format | validity | consistency | uniqueness | completeness
    columns: list[str] = Field(default_factory=list)
    severity: str = "minor"                    # info | minor | major
    description: str = ""
    sql: str = ""                              # violation-count SQL against the staged table
    total: int = 0                             # rows the rule applies to
    failed: int = 0
    pass_rate: float = 1.0
    samples: list[str] = Field(default_factory=list)
    suppressed: bool = False                   # SME judged the rule wrong/noise
    suppress_note: str = ""                    # who + why (curation evidence)


class PiiFinding(BaseModel):
    column: str
    is_pii: bool
    category: str = "None"                     # Name / Date of Birth / National Insurance Number / Postcode / ...
    sensitivity: str = "none"                  # high | medium | low | none
    confidence: float = 0.0
    method: str = "pattern"                    # pattern | llm | llm+pattern
    rationale: str = ""
    sample_evidence: list[str] = Field(default_factory=list)
    recommended_action: str = "retain"        # mask | tokenize | pseudonymize | retain | drop


class TableInsight(BaseModel):
    table: str
    row_count: int
    column_count: int
    candidate_keys: list[str] = Field(default_factory=list)
    dead_columns: list[str] = Field(default_factory=list)
    linked_groups: list[list[str]] = Field(default_factory=list)   # co-populated column sets
    columns: list[ColumnInsight]
    dependencies: list[DependencyFinding] = Field(default_factory=list)
    dq: list[DqFinding] = Field(default_factory=list)
    dq_rules: list[DQRule] = Field(default_factory=list)   # the executable rule library
    pii: list[PiiFinding] = Field(default_factory=list)
    dq_summary: dict = Field(default_factory=dict)
    pii_summary: dict = Field(default_factory=dict)
    summary: list[str] = Field(default_factory=list)
    generated_by: str = "deterministic+offline_stub"


# ======================== Agent 2: legacy expert =========================
class EnrichedColumn(BaseModel):
    """A source column as a business analyst would describe it.

    SIMPLIFIED (v9). The COBOL/screen provenance fields — cobol_name, cobol_pic,
    screen_label, evidence, sources, confidence — and the seven derivation_*
    fields were artefacts of the retired comprehension pipeline. Asking a human
    to author them produced noise, and outside legacy_expert.py only one of them
    had a live reader. They are gone from the declared schema.

    `value_decode` STAYS, and is the one field people mistake for COBOL
    metadata. It is the code -> meaning dictionary, and it is the single most
    load-bearing field here: enum targets are only allowed to match on coded
    evidence (_value_overlap), the auto-accept gate depends on every code having
    a target equivalent (_unmapped_codes), and the decode CASE WHEN SQL is
    generated straight from it (_synth). Without it, every coded target becomes
    unmappable. It is business knowledge, not legacy metadata: an analyst knows
    'CL' means Claimed.

    extra='allow' so a dictionary produced by the legacy expert (which still
    carries its COBOL provenance) round-trips through this model unharmed.
    """
    model_config = ConfigDict(extra="allow")

    name: str                                   # the column as it appears in the file
    business_name: str                          # what a person calls it
    description: str = ""                       # one line of plain English
    inferred_type: str = ""                     # IDENTIFIER / CODE / DATE / AMOUNT / FREE_TEXT
    value_decode: dict[str, str] = Field(default_factory=dict)   # code -> meaning
    # Alternative business terms for this column. This is where domain knowledge
    # that no string-similarity metric can recover belongs: NINO carries
    # ["National Insurance number", "tax identifier"] so it can win
    # tax_file_number, which it never would on tokens alone. Authored, auditable,
    # and visible in the dictionary rather than buried in the matcher.
    aliases: list[str] = Field(default_factory=list)
    # Marks a column that links files. Relationships are discovered from the
    # data; this is the override for when that inference is wrong or absent.
    join_key: bool = False
    # multi-source: which staged file this column came from, and its name there
    # (columns are renamed on collision when sources are combined). Set by
    # composite.build_workset — not authored by hand.
    origin_table: str = ""
    origin_name: str = ""


class EnrichedDictionary(BaseModel):
    table: str
    columns: list[EnrichedColumn]
    rules: list[str] = Field(default_factory=list)   # decoded business rules
    generated_by: str = "deterministic+offline_stub"


class Relationship(BaseModel):
    """One discovered link between two source files, backed by value evidence.
    containment_left = fraction of the left column's distinct values found in
    the right column (and vice versa). cardinality reads left:right."""
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    containment_left: float = 0.0      # |L ∩ R| / |distinct L|
    containment_right: float = 0.0     # |L ∩ R| / |distinct R|
    cardinality: str = "M:N"           # 1:1 | N:1 | 1:N | M:N
    kind: str = "value_overlap"        # join_key | shared_domain | value_overlap
    confidence: float = 0.0
    evidence: str = ""


class SourceRelationships(BaseModel):
    """The cross-file knowledge: how the loaded source files relate. Joinable
    edges (join_key with a unique side) are what the composite workset uses;
    shared_domain edges are code-set overlaps (same vocabulary, not a join)."""
    tables: list[str] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    generated_by: str = "deterministic"


# ======================== Agent 3: mapping agent =========================
class MappingEntry(BaseModel):
    target_attribute: str
    source_attributes: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)    # originating source file(s)
    cardinality: str                      # 1:1 | many:1 | derived | unmapped
    transformation_sql: str = ""
    transformation_note: str = ""
    confidence: float = 0.0
    gate: str = Gate.REJECT.value
    validation_coverage: Optional[float] = None
    unmapped_codes: list[str] = Field(default_factory=list)
    alternatives: list[dict] = Field(default_factory=list)   # competing source candidates
    ambiguous: bool = False
    rationale: str = ""
    # LLM re-rank provenance: when the model recovers or overrides a match,
    # the earned deterministic score is preserved alongside the proposed one
    # so the review queue can show both (audit: earned vs proposed confidence)
    deterministic_confidence: Optional[float] = None
    llm_confidence: Optional[float] = None
    llm_recovered: bool = False
    # Why the deterministic transform is insufficient for this target: the
    # target describes work the synthesised SQL demonstrably does not do —
    # composition across columns, a unit conversion, an explicit format. Set by
    # _derivation_gap, read by the LLM synthesis tier and shown on the review
    # card. Empty means the transform matches what the target asks for.
    derivation_gap: list[str] = Field(default_factory=list)
    match_source: str = "deterministic"      # deterministic | llm | human


class MappingSpec(BaseModel):
    source_table: str
    target_table: str
    mappings: list[MappingEntry]
    unmapped_source: list[dict] = Field(default_factory=list)
    unmapped_target: list[dict] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
    generated_by: str = "deterministic+offline_stub"
    # multi-source: every file that fed this spec, and how they were joined
    source_tables: list[str] = Field(default_factory=list)
    join_plan: list[dict] = Field(default_factory=list)
    # provenance of the knowledge this spec was derived from (two-flow split)
    kg_version: Optional[int] = None
    kg_fingerprint: Optional[str] = None
    kg_status: Optional[str] = None          # draft / certified at mapping time


# ======================== Agent 4: validation agent ======================
class CheckResult(BaseModel):
    name: str                              # e.g. "crossfield: exit_date~XA05"
    category: str                          # completeness | key_integrity | grain | crossfield | reconciliation | wellformed
    status: str                            # pass | warn | fail
    severity: str = "soft"                 # soft | hard
    detail: str = ""
    target_attribute: Optional[str] = None
    offending_rows: int = 0
    sample: list[dict] = Field(default_factory=list)   # sample offending rows
    # The SQL this check actually executed, and the population it ran over.
    # A green tick is an assertion; a green tick with its SQL and row count is
    # evidence. Also makes the results reconcilable against the generated
    # script — same SQL in both.
    sql: Optional[str] = None
    rows_scanned: int = 0
    # Which rule asked for this check: "mined" (derived from the source data),
    # "llm_proposed", or "user_added" (a control the business specified). An
    # assurance reviewer must be able to tell them apart.
    origin: str = "mined"


class GateAdjustment(BaseModel):
    target_attribute: str
    from_gate: str
    to_gate: str
    reason: str


class ValidationReport(BaseModel):
    source_table: str
    target_table: str
    verdict: str                           # certified | needs_review | blocked
    checks: list[CheckResult]
    gate_adjustments: list[GateAdjustment] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
    generated_by: str = "deterministic+offline_stub"
    # provenance of the knowledge this spec was derived from (two-flow split)
    kg_version: Optional[int] = None
    kg_fingerprint: Optional[str] = None
    kg_status: Optional[str] = None          # draft / certified at mapping time


# ======================== Agent 5: reviewer ==============================
class ReviewItem(BaseModel):
    target_attribute: str
    kind: str                              # mapping_review | unmapped_target
    gate: str
    confidence: float = 0.0
    deterministic_confidence: Optional[float] = None
    llm_confidence: Optional[float] = None
    llm_recovered: bool = False
    reason: str = ""
    transformation_sql: str = ""
    # drill-down provenance, back through the layers
    source_attributes: list[str] = Field(default_factory=list)
    source_business_names: list[str] = Field(default_factory=list)
    source_decode: dict[str, str] = Field(default_factory=dict)
    alternatives: list[dict] = Field(default_factory=list)       # competing candidates
    ambiguous: bool = False
    upstream_evidence: list[str] = Field(default_factory=list)   # Agent 2
    data_patterns: list[str] = Field(default_factory=list)       # Agent 1
    validator_exceptions: list[str] = Field(default_factory=list)  # Agent 4
    offending_rows: list[dict] = Field(default_factory=list)
    # what the human can do
    actions: list[str] = Field(default_factory=lambda: ["accept", "edit", "reject"])
    suggested_resolution: str = ""
    suggested_sql: str = ""
    # unmapped-target defaulting: the target's declared type and a proposed
    # load-time default (as a plain display value) so the UI can pre-fill an
    # editable field the reviewer accepts or changes.
    target_type: str = ""
    suggested_default: Optional[str] = None


class ReviewQueue(BaseModel):
    source_table: str
    target_table: str
    verdict: str                           # carried from validation
    items: list[ReviewItem]                # only what needs a human
    auto_accepted: list[str] = Field(default_factory=list)   # flowed through untouched
    stats: dict = Field(default_factory=dict)
    generated_by: str = "deterministic"
