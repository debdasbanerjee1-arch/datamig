"""LangGraph nodes — the binding layer.

Each node is a thin adapter: it reads what its agent needs from state, calls the
agent (a plain, graph-agnostic function), and writes the artifact back. No agent
logic lives here. `stage_source` sets up the shared DuckDB warehouse and loads
the code/target inputs once.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..agents.analyst import analyze
from ..agents.legacy_expert import build_evidence, enrich
from ..agents.mapping_agent import map_to_target
from ..agents.reviewer import apply_decisions, build_review_queue
from ..agents.validator import validate_spec
from ..config import llm_ready
from ..kgstore import KGStore, collect_inputs, fingerprint_inputs
from ..staging import Warehouse
from .state import PipelineState


def stage_source(state: PipelineState) -> PipelineState:
    table = state.get("table") or Path(state["source_csv"]).stem
    wh = Warehouse(state.get("warehouse_path", ":memory:"))
    wh.stage_csv(state["source_csv"], table, all_varchar=True)
    code = Path(state.get("code_dir", "data/legacy"))
    cobol_ext = ("*.cbl", "*.cob", "*.cpy", "*.cobol", "*.cobc")
    screen_ext = ("*.php", "*.inc", "*.phtml", "*.jsp", "*.asp", "*.html")
    def _read(globs):
        files = sorted({p for g in globs for p in code.glob(g)})
        return "\n\n".join(f"* ---- {p.name} ----\n{p.read_text(encoding='utf-8', errors='replace')}"
                           for p in files)
    cobol = _read(cobol_ext)
    php = _read(screen_ext)
    out = {"table": table, "warehouse": wh, "cobol_text": cobol, "php_text": php}
    if state.get("target_dict_path"):        # Flow A runs without a target
        out["target_dict"] = json.loads(
            Path(state["target_dict_path"]).read_text(encoding="utf-8"))
    return out


def analyst_node(state: PipelineState) -> PipelineState:
    return {"insight": analyze(state["source_csv"], state["table"], warehouse=state["warehouse"])}


def legacy_expert_node(state: PipelineState) -> PipelineState:
    return {"enriched": enrich(state["insight"], state["cobol_text"], state["php_text"])}


def mapping_node(state: PipelineState) -> PipelineState:
    spec = map_to_target(state["enriched"], state["target_dict"],
                         state["warehouse"], state["table"])
    kg = state.get("kg_result")
    if kg:                       # Flow B: stamp the knowledge provenance
        spec.kg_version = kg.get("version")
        spec.kg_fingerprint = kg.get("fingerprint")
        spec.kg_status = kg.get("status")
    if state.get("sources"):     # multi-source: which files + how joined
        spec.source_tables = [s["table"] for s in state["sources"]]
        spec.join_plan = state.get("join_plan") or []
        prim = (kg or {}).get("primary") or (spec.source_tables[0]
                                             if spec.source_tables else "")
        spec.stats["cross_file_mappings"] = sum(
            1 for m in spec.mappings
            if m.source_files and (len(m.source_files) > 1
                                   or m.source_files != [prim]))
        if state.get("excluded_sources"):
            spec.stats["excluded_sources"] = state["excluded_sources"]
    return {"spec": spec}


def validation_node(state: PipelineState) -> PipelineState:
    return {"report": validate_spec(state["spec"], state["target_dict"],
                                    state["insight"], state["warehouse"], state["table"])}


def review_node(state: PipelineState) -> PipelineState:
    """The single human-in-the-loop gate: assemble the exception-driven queue."""
    return {"review_queue": build_review_queue(
        state["spec"], state["report"], state["enriched"],
        state["insight"], state["target_dict"])}


def apply_decisions_node(state: PipelineState) -> PipelineState:
    """Apply human decisions to the spec, then clear them so the loop terminates.
    Tolerates an absent/empty decisions map (a certify with everything already
    auto-accepted): it simply re-validates the spec unchanged."""
    spec = apply_decisions(state["spec"], state.get("decisions") or {},
                           state.get("target_dict"))
    return {"spec": spec, "decisions": {}}


def seed_spec_node(state: PipelineState) -> PipelineState:
    """Certify-only entry: adopt the spec the reviewer already saw instead of
    re-deriving it with the mapping agent. The client sends back the exact spec
    it reviewed (spec_in); we rehydrate it so apply_decisions works on THAT spec,
    not a freshly regenerated one. Keeps the reviewer's decisions honest — they
    apply to what was on screen."""
    from ..agents.contracts import MappingSpec

    spec_in = state.get("spec_in")
    spec = spec_in if isinstance(spec_in, MappingSpec) else MappingSpec(**spec_in)
    kg = state.get("kg_result")
    if kg:                              # keep provenance consistent with load_kg
        spec.kg_version = spec.kg_version or kg.get("version")
        spec.kg_fingerprint = spec.kg_fingerprint or kg.get("fingerprint")
        spec.kg_status = spec.kg_status or kg.get("status")
    return {"spec": spec}


# --------------------------------------------------------- two-flow split
# Flow A ends by PERSISTING the knowledge (versioned, fingerprinted); Flow B
# begins by LOADING a chosen version instead of re-running comprehension.

def _is_degraded(enriched) -> bool:
    """LLM was configured but enrichment is incomplete — either the meanings
    call failed outright, or any calculated field is missing its narrative
    (a per-rule narration call failed). Degraded versions never replay."""
    if not llm_ready():
        return False
    if not enriched.generated_by.endswith("llm"):
        return True
    return any(c.derivation_cobol and not c.derivation_narrative
               for c in enriched.columns)


def persist_kg_node(state: PipelineState) -> PipelineState:
    """Flow A terminal node: write the versioned knowledge graph + artifacts.

    Fingerprint short-circuit: if a version already exists for these exact
    inputs and force is not set, reuse it instead of writing a duplicate.
    """
    inputs = collect_inputs(state["source_csv"], state.get("code_dir", "data/legacy"))
    store = KGStore(state.get("kg_path", "data/knowledge.duckdb"))
    try:
        fp = fingerprint_inputs(inputs)
        existing = store.find_by_fingerprint(fp)
        if existing is not None and not state.get("force"):
            return {"kg_result": {"version": existing, "fingerprint": fp,
                                  "reused": True,
                                  "status": store.meta(existing)["status"]}}
        evidence = build_evidence(state["insight"], state["cobol_text"],
                                  state["php_text"])
        v = store.new_version(inputs, state["table"],
                              state["insight"].row_count)
        degraded = _is_degraded(state["enriched"])
        llm_used = state["enriched"].generated_by.endswith("llm")
        counts = store.save(v, state["insight"], state["enriched"], evidence,
                            default_provenance="llm" if llm_used else "parser")
        if degraded:
            # LLM was configured but enrichment failed: keep the version for
            # history, but it must NOT satisfy future fingerprint replays —
            # the next run retries the LLM instead of freezing the failure
            store.con.execute(
                "UPDATE kg_version SET notes = ? WHERE version = ?",
                ["degraded: LLM enrichment failed — excluded from replay", v])
        return {"kg_result": {"version": v, "fingerprint": fp, "reused": False,
                              "status": "draft", "degraded": degraded, **counts}}
    finally:
        store.close()


def load_kg_node(state: PipelineState) -> PipelineState:
    """Flow B entry node: load insight + dictionary from the knowledge store
    (preferring certified versions), staging sources as needed.

    Single source: exactly the original behaviour. Multiple sources: each
    file's own knowledge version is loaded, the discovered relationships are
    fetched (or computed), and a composite workset view + combined dictionary
    is built so the downstream agents run unchanged.
    """
    store = KGStore(state.get("kg_path", "data/knowledge.duckdb"))
    try:
        sources = state.get("sources") or []
        if len(sources) > 1:
            return _load_kg_multi(state, store, sources)
        v = state.get("kg_version") or store.latest(certified_only=True) \
            or store.latest()
        if v is None:
            raise RuntimeError("knowledge store is empty — run Flow A first")
        meta = store.meta(v)
        insight = store.load_insight(v)
        enriched = store.load_dictionary(v)
        table = meta["source_table"]
        wh = state.get("warehouse") or Warehouse(
            state.get("warehouse_path", "data/warehouse.duckdb"))
        _ensure_staged(wh, table, state.get("source_csv"))
        return {"insight": insight, "enriched": enriched, "table": table,
                "warehouse": wh,
                "kg_result": {"version": v, "status": meta["status"],
                              "fingerprint": meta["fingerprint"], "reused": True}}
    finally:
        store.close()


def _ensure_staged(wh: Warehouse, table: str, csv_path: str | None) -> None:
    staged = wh.con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name=?",
        [table]).fetchone()[0]
    if not staged:
        if not csv_path:
            raise RuntimeError(
                f"table {table} not staged and no source path given to restage")
        wh.stage_csv(csv_path, table, all_varchar=True)


def _load_kg_multi(state: PipelineState, store: KGStore,
                   sources: list[dict]) -> PipelineState:
    from ..agents.contracts import SourceRelationships
    from ..agents.relationship import discover_relationships
    from ..composite import build_workset, choose_primary

    wh = state.get("warehouse") or Warehouse(
        state.get("warehouse_path", "data/warehouse.duckdb"))
    tables, insights, dicts, versions = [], {}, {}, {}
    for s in sources:
        t = s["table"]
        v = store.latest_for_table(t, certified_only=True) \
            or store.latest_for_table(t)
        if v is None:
            raise RuntimeError(
                f"no knowledge for source {t} — run Flow A on it first")
        _ensure_staged(wh, t, s.get("path"))
        tables.append(t)
        insights[t] = store.load_insight(v)
        dicts[t] = store.load_dictionary(v)
        versions[t] = {"version": v, "status": store.meta(v)["status"]}

    fp = relset_fingerprint(sources, state.get("code_dir", "data/legacy"))
    rel_doc = store.load_relationships(fp)
    if rel_doc:
        rels = SourceRelationships.model_validate(rel_doc)
    else:
        rels = discover_relationships(wh, tables, insights)
        store.save_relationships(fp, tables, rels.model_dump())

    # primary is chosen automatically from the join grain — the finest-grain
    # file that nothing fans out — never taken from the user
    row_counts = {t: wh.row_count(t) for t in tables}
    primary, components = choose_primary(tables, rels, row_counts)
    workset, combined, plan, excluded = build_workset(
        wh, primary, tables, dicts, rels)
    pv = versions[primary]
    return {"insight": insights[primary], "enriched": combined,
            "table": workset, "warehouse": wh,
            "relationships": rels.model_dump(), "join_plan": plan,
            "excluded_sources": excluded,
            "kg_result": {"version": pv["version"], "status": pv["status"],
                          "fingerprint": fp, "reused": True,
                          "versions": versions, "primary": primary,
                          "components": components}}


def relset_fingerprint(sources: list[dict], code_dir: str) -> str:
    """Deterministic key for a SET of sources: the sorted per-file input
    fingerprints, hashed. Any file (or the legacy code) changing changes it."""
    import hashlib
    fps = sorted(fingerprint_inputs(collect_inputs(s["path"], code_dir))
                 for s in sources if s.get("path"))
    return hashlib.sha256(json.dumps(fps).encode()).hexdigest()[:16]


def load_target_node(state: PipelineState) -> PipelineState:
    """Flow B: the target dictionary is Flow B's own input, not knowledge."""
    target_dict = json.loads(
        Path(state["target_dict_path"]).read_text(encoding="utf-8"))
    return {"target_dict": target_dict}


# --------------------------------------------------- manual mapping workspace
# The Mapping Workspace can also run WITHOUT Flow A / the knowledge store: the
# enriched source dictionary (Agent 2's artefact) is supplied directly by the
# user instead of being computed. `target_dict` is passed straight into the
# initial state by the caller (same as always), so no load_target step is
# needed here either.

def _minimal_insight(table: str, wh: Warehouse) -> "TableInsight":
    """Last-resort fallback: an empty-but-valid TableInsight, so validation
    skips its key-integrity / crossfield checks cleanly instead of the pipeline
    crashing on a missing artefact. Only reached if derivation itself fails."""
    from ..agents.contracts import TableInsight
    return TableInsight(table=table, row_count=wh.row_count(table),
                        column_count=len(wh.column_names(table)), columns=[])


def _resolve_insight(state: PipelineState, table: str, wh: Warehouse) -> "TableInsight":
    """The insight validation needs (candidate_keys + dependencies), DERIVED
    from the source CSV and cached in the staging warehouse.

    This used to be a user upload, which meant anyone who didn't supply it
    silently lost the key-integrity and crossfield checks. The source data
    alone is enough to recover what those checks consume, so it is no longer
    asked for. Derivation is best-effort: if it fails, fall back to the empty
    stub so those checks are skipped cleanly rather than failing the run.
    """
    src = state.get("source_csv")
    if src:
        try:
            from ..insight_cache import get_or_derive
            return get_or_derive(wh, src, table)
        except Exception:      # noqa: BLE001 — derivation is best-effort
            pass
    return _minimal_insight(table, wh)


def _stage_all(state: PipelineState, wh: Warehouse) -> list[str]:
    """Stage every uploaded source file. `sources` is a list of
    {path, table}; `source_csv` remains supported for the single-file case."""
    srcs = state.get("sources") or []
    if not srcs and state.get("source_csv"):
        srcs = [{"path": state["source_csv"],
                 "table": state.get("table") or Path(state["source_csv"]).stem}]
    tables = []
    for s in srcs:
        t = s.get("table") or Path(s["path"]).stem
        _ensure_staged(wh, t, s["path"])
        tables.append(t)
    return tables


def _combine_sources(state: PipelineState, wh: Warehouse, tables: list[str]):
    """Turn N staged files + N dictionaries into ONE mappable source.

    Relationships are discovered from the DATA (relationship.discover_
    relationships), never guessed from names: a name match only nominates a
    column pair for a value-containment test. The finest-grain file becomes the
    primary and every file with a safe, grain-preserving edge is LEFT JOINed in
    (composite.build_workset), so the mapping agent sees a single wide table and
    needs no multi-file logic of its own. Every combined column keeps
    origin_table / origin_name, which is what lets each mapping report the file
    it actually came from.

    Returns (table, enriched, insight, join_plan, excluded, relationships).
    """
    from ..agents.contracts import EnrichedColumn, EnrichedDictionary
    from ..agents.relationship import discover_relationships
    from ..composite import build_workset, choose_primary

    dicts = {}
    for d in (state.get("enriched_dicts") or []):
        ed = EnrichedDictionary.model_validate(d)
        dicts[ed.table] = ed
    if not dicts and state.get("enriched_json"):
        ed = EnrichedDictionary.model_validate(state["enriched_json"])
        dicts[ed.table if ed.table in tables else tables[0]] = ed

    # a file with no dictionary still joins — it just contributes columns with
    # only their raw names, which the matcher will rarely pick. Better than
    # dropping the file silently.
    for t in tables:
        dicts.setdefault(t, EnrichedDictionary(
            table=t, columns=[EnrichedColumn(name=c, business_name=c)
                              for c in wh.column_names(t)]))

    insights = {t: _resolve_insight(state, t, wh) for t in tables}
    if len(tables) == 1:
        return tables[0], dicts[tables[0]], insights[tables[0]], [], [], None

    rels = discover_relationships(wh, tables, insights)
    row_counts = {t: wh.row_count(t) for t in tables}
    primary, _cands = choose_primary(tables, rels, row_counts)
    workset, combined, plan, excluded = build_workset(wh, primary, tables, dicts, rels)
    # the insight validation uses must describe the WORKSET, not one input file
    insight = _resolve_insight(state, workset, wh)
    return workset, combined, insight, plan, excluded, rels


def manual_inputs_node(state: PipelineState) -> PipelineState:
    """Manual-mode entry point: stage every uploaded source file, load the
    per-file dictionaries, discover how the files relate, and combine them into
    one workset the mapping agent can treat as a single source."""
    wh = state.get("warehouse") or Warehouse(state.get("warehouse_path", ":memory:"))
    tables = _stage_all(state, wh)
    table, enriched, insight, plan, excluded, rels = _combine_sources(state, wh, tables)
    return {"table": table, "warehouse": wh, "enriched": enriched, "insight": insight,
            "source_tables": tables, "join_plan": plan, "excluded_sources": excluded,
            "relationships": rels}


def manual_seed_spec_node(state: PipelineState) -> PipelineState:
    """Manual-mode certify entry point: adopt the spec the reviewer already
    saw (spec_in) instead of re-deriving it — mirrors seed_spec_node, but
    without a knowledge version to carry provenance from. review_node still
    needs `enriched` (business names / decodes / evidence per source
    attribute for the queue's drill-down cards), so it's reloaded here too —
    the mapping agent itself is NOT re-run, only its dictionary input is
    made available again for display."""
    from ..agents.contracts import EnrichedDictionary, MappingSpec

    wh = state.get("warehouse") or Warehouse(state.get("warehouse_path", ":memory:"))
    tables = _stage_all(state, wh)
    # rebuild the SAME workset the mapping run used — the certified SQL refers
    # to workset column names, so the view has to exist before revalidation
    table, enriched, insight, plan, excluded, rels = _combine_sources(state, wh, tables)
    spec_in = state.get("spec_in")
    spec = spec_in if isinstance(spec_in, MappingSpec) else MappingSpec(**spec_in)
    return {"table": table, "warehouse": wh, "spec": spec,
           "enriched": enriched, "insight": insight,
           "source_tables": tables, "join_plan": plan, "excluded_sources": excluded,
           "relationships": rels}
