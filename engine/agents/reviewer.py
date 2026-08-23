"""Agent 5 — the exception-driven reviewer (the human-in-the-loop gate).

Sits once, after validation. It does NOT re-review everything — it assembles only
the items that need a human: mappings gated review/reject, plus validator
exceptions and required-but-unmapped targets. Each item carries drill-down
provenance back through the layers (the enriched meaning it rests on, the
analyst's data pattern, the validator's exception and offending rows) and a
suggested resolution, so a reviewer can decide without spelunking.

`apply_decisions` feeds human choices back into the spec; the graph then
re-validates, so a resolved exception flips the verdict to certified.

Fully deterministic — it organises evidence, it doesn't invent judgements.
"""
from __future__ import annotations

import re

from ..models import target_attributes
from .contracts import (EnrichedDictionary, MappingEntry, MappingSpec, ReviewItem,
                        ReviewQueue, TableInsight, ValidationReport)


def _norm(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(s).lower()))


def _source_label(spec: MappingSpec) -> str:
    """A human-meaningful name for the originating system. On a composite run
    the spec's source_table is the internal '__workset' view, so prefer the
    real file names the workset was built from."""
    if spec.source_tables:
        return spec.source_tables[0]
    st = spec.source_table or ""
    return "" if st.startswith("__") else st


def _suggest_code_fix(mapping, enriched_by_name, target_attr, target_dict) -> tuple[str, str]:
    """For an unmapped categorical code, propose the nearest target value and the
    repaired CASE expression."""
    tgt = next((a for a in target_attributes(target_dict) if a["name"] == target_attr), None)
    allowed = (tgt or {}).get("allowed_values")
    if not (allowed and mapping.unmapped_codes):
        return "", ""
    src = mapping.source_attributes[0]
    decode = enriched_by_name.get(src).value_decode if src in enriched_by_name else {}
    fixes, sql = [], mapping.transformation_sql
    for code in mapping.unmapped_codes:
        label = decode.get(code, code)
        best = max(allowed, key=lambda v: len(_norm(label) & _norm(v)), default=None)
        if best and (_norm(label) & _norm(best)):
            fixes.append(f"{code} ('{label}') → {best}")
            sql = sql.replace(" ELSE NULL END", f" WHEN '{code}' THEN '{best}' ELSE NULL END", 1)
    text = "; ".join(fixes)
    return (f"map {text}" if text else ""), (sql if fixes else "")


def _lit(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


# load-time system columns: the platform, not the reviewer, owns their value.
# transform.py already materialises these; mirroring them here means the review
# queue proposes the SAME value the ETL will actually write.
_LOAD_TS_SQL = "strftime(current_localtimestamp(), '%Y-%m-%d %H:%M:%S')"
_SYSTEM_TS_NAMES = ("migrated_at", "loaded_at", "created_at", "updated_at",
                    "ingested_at")


def _suggest_default(target: dict, source_table: str | None = None
                     ) -> tuple[str | None, str]:
    """Propose a load-time default for an unmapped target and its SQL literal.

    Returns (display_value, transformation_sql). The display value is what the
    UI pre-fills into an editable field; the SQL is what actually loads.

    The default MUST satisfy the target's own contract. Previously every
    non-nullable string / date fell through to NULL, which then hard-failed the
    completeness check the moment the reviewer accepted it — the default was
    guaranteed to be invalid. Now a REQUIRED target always gets a value that
    loads: system columns get their real system value, and anything else gets
    an explicit 'UNKNOWN' marker the reviewer can overwrite.
    """
    if not target:
        # no target definition available — we cannot know the contract, so we
        # must not invent a marker value. NULL is the honest fallback.
        return None, "NULL"
    ttype = target.get("type", "string")
    nullable = target.get("nullable", False)
    name = str(target.get("name", "")).lower()

    # ---- system-owned load columns (concrete, not reviewer-supplied) ----
    if name == "source_system" and source_table:
        return source_table, _lit(source_table)
    if name in _SYSTEM_TS_NAMES and ttype in ("date", "timestamp"):
        return "load timestamp", _LOAD_TS_SQL

    if nullable:
        return None, "NULL"
    if ttype == "boolean":
        return "false", "false"
    if ttype in ("decimal", "number", "numeric", "integer"):
        return "0", "0"
    if ttype == "enum":
        allowed = target.get("allowed_values") or []
        if allowed:
            return allowed[0], _lit(allowed[0])
        return "UNKNOWN", _lit("UNKNOWN")
    if ttype in ("date", "timestamp"):
        return "load timestamp", _LOAD_TS_SQL
    # required string / identifier — an explicit marker beats a NULL that
    # cannot satisfy the target's own not-null contract.
    return "UNKNOWN", _lit("UNKNOWN")


def build_review_queue(spec: MappingSpec, report: ValidationReport,
                       enriched: EnrichedDictionary, insight: TableInsight,
                       target_dict: dict) -> ReviewQueue:
    by_name = {c.name: c for c in enriched.columns}
    # Two buckets, deliberately different:
    #   checks_by_attr   — every non-pass check, shown on the card (context)
    #   blocking_by_attr — FAILURES only; these are what re-open an item
    # An accepted mapping that carries a soft warn (e.g. a reconciliation
    # observation about unmapped source codes) must NOT be dragged back into
    # the queue on every certify pass: no reviewer decision can clear a warn,
    # so it would never converge. The warn still travels on the report.
    checks_by_attr: dict[str, list] = {}
    blocking_by_attr: dict[str, list] = {}
    for c in report.checks:
        if c.target_attribute and c.status != "pass":
            checks_by_attr.setdefault(c.target_attribute, []).append(c)
            if c.status == "fail":
                blocking_by_attr.setdefault(c.target_attribute, []).append(c)

    items: list[ReviewItem] = []
    auto: list[str] = []
    excluded: list[str] = []

    for m in spec.mappings:
        if m.gate == "auto_accept" and m.target_attribute not in blocking_by_attr:
            auto.append(m.target_attribute)
            continue
        # a reject the reviewer has already confirmed is a DECISION, not an
        # outstanding question — it stays visible in the final mapping table
        # (marked "excluded"), but re-queueing it would ask the same question
        # on every certify pass and the queue would never empty.
        if m.gate == "reject" and m.match_source == "human" \
                and m.target_attribute not in blocking_by_attr:
            excluded.append(m.target_attribute)
            continue
        # provenance
        bnames = [by_name[s].business_name for s in m.source_attributes if s in by_name]
        decode: dict[str, str] = {}
        evidence: list[str] = []
        for s in m.source_attributes:
            if s in by_name:
                decode.update(by_name[s].value_decode)
                evidence += (getattr(by_name[s], "evidence", None) or [])
        patterns = [d.statement for d in insight.dependencies
                    if d.dependent in m.source_attributes or any(dr in m.source_attributes for dr in d.drivers)]
        exc = checks_by_attr.get(m.target_attribute, [])
        offending = [r for c in exc for r in c.sample]
        sugg, sugg_sql = _suggest_code_fix(m, by_name, m.target_attribute, target_dict)
        # for an ambiguous source, the most useful suggestion is the competing
        # candidate the reviewer should weigh against the chosen one.
        if m.ambiguous and m.alternatives and not sugg:
            alt = m.alternatives[0]
            sugg = (f"confirm the source: chose {m.source_attributes[0]} "
                    f"({bnames[0] if bnames else '?'}); "
                    f"{alt['source']} ({alt.get('business_name','?')}) is an equally plausible match.")

        items.append(ReviewItem(
            target_attribute=m.target_attribute, kind="mapping_review", gate=m.gate,
            confidence=m.confidence,
            deterministic_confidence=m.deterministic_confidence,
            llm_confidence=m.llm_confidence, llm_recovered=m.llm_recovered,
            reason=(m.rationale or "needs review") + (
                " | " + "; ".join(c.detail for c in exc) if exc else ""),
            transformation_sql=m.transformation_sql,
            source_attributes=m.source_attributes, source_business_names=bnames,
            source_decode=decode, alternatives=m.alternatives, ambiguous=m.ambiguous,
            upstream_evidence=evidence,
            data_patterns=patterns,
            validator_exceptions=[c.detail for c in exc], offending_rows=offending,
            suggested_resolution=sugg, suggested_sql=sugg_sql,
        ))

    tgt_by_name = {a["name"]: a for a in target_attributes(target_dict)}
    src_label = _source_label(spec)
    for u in spec.unmapped_target:
        tgt = tgt_by_name.get(u["attribute"], {})
        dflt_display, dflt_sql = _suggest_default(tgt, src_label)
        items.append(ReviewItem(
            target_attribute=u["attribute"], kind="unmapped_target", gate="review",
            reason=u["reason"], actions=["set_default", "leave_null"],
            suggested_resolution="provide a load-time default value.",
            target_type=tgt.get("type", ""),
            suggested_default=dflt_display,
            suggested_sql=dflt_sql,
        ))

    stats = {"to_review": len(items), "auto_accepted": len(auto),
             "excluded_by_reviewer": len(excluded),
             "exceptions": sum(len(v) for v in checks_by_attr.values())}
    return ReviewQueue(source_table=spec.source_table, target_table=spec.target_table,
                       verdict=report.verdict, items=items, auto_accepted=auto, stats=stats)


def apply_decisions(spec: MappingSpec, decisions: dict,
                    target_dict: dict | None = None) -> MappingSpec:
    """Apply human decisions back onto the spec.

    decisions = { target_attribute: {
        "action": "accept" | "edit" | "reject",
        "transformation_sql": "<new sql>"   # for edit
    }}
    accept -> gate auto_accept; edit -> swap SQL + clear unmapped codes + auto_accept;
    reject -> gate reject.
    """
    def _note(m, text: str) -> None:
        # a certified mapping can be amended and re-certified, so the same
        # decision may be applied more than once — record it only once
        if text not in (m.rationale or ""):
            m.rationale = (m.rationale or "") + text

    for m in spec.mappings:
        d = decisions.get(m.target_attribute)
        if not d:
            continue
        action = d.get("action")
        if action == "accept":
            m.gate = "auto_accept"
            _note(m, " | accepted by reviewer.")
        elif action == "edit":
            if d.get("source_attributes"):
                m.source_attributes = d["source_attributes"]
            if d.get("transformation_sql"):
                m.transformation_sql = d["transformation_sql"]
            m.unmapped_codes = []
            m.ambiguous = False
            m.alternatives = []
            m.gate = "auto_accept"
            m.match_source = "human"
            _note(m, " | edited by reviewer.")
        elif action == "reject":
            m.gate = "reject"
            m.match_source = "human"      # settled: a deliberate exclusion
            _note(m, " | rejected by reviewer.")

    # ---- UNMAPPED targets are made TERMINAL here -------------------------
    # Previously an untouched unmapped target stayed in spec.unmapped_target
    # forever, so build_review_queue re-emitted it on every certify pass and
    # the queue never emptied. The UI already tells the reviewer these are
    # non-blocking and pre-fills a suggested default — so leaving one alone IS
    # a decision: accept the suggestion. Resolve all three cases now:
    #   explicit edit   -> the reviewer's own SQL
    #   explicit reject -> deliberate exclusion, dropped entirely
    #   no decision     -> the suggested load-time default, promoted as-is
    # Whatever the path, the target leaves the outstanding list.
    tgt_by_name = {a["name"]: a for a in target_attributes(target_dict or {})}
    src_label = _source_label(spec)
    mapped_names = {m.target_attribute for m in spec.mappings}
    still_unmapped = []
    for u in spec.unmapped_target:
        attr = u["attribute"]
        if attr in mapped_names:          # already mapped elsewhere: nothing to do
            continue
        d = decisions.get(attr) or {}
        action = d.get("action")
        srcs: list[str] = []
        if action == "reject":
            continue                      # deliberate exclusion, not migrated
        if action == "edit" and d.get("transformation_sql"):
            sql = d["transformation_sql"]
            srcs = list(d.get("source_attributes") or [])
            note = "Manually mapped by reviewer."
            why = "Previously unmapped; manually mapped by reviewer."
        else:
            tgt = tgt_by_name.get(attr)
            if not tgt:
                # No dictionary entry for this target, so its contract (type,
                # nullability, allowed values) is unknown. Inventing a default
                # could violate it — leave the target outstanding instead of
                # guessing. In the graph the dictionary is always supplied, so
                # this only guards direct/library callers.
                still_unmapped.append(u)
                continue
            display, sql = _suggest_default(tgt, src_label)
            if not sql or sql == "NULL":
                # nullable with no value to invent — genuinely nothing to load.
                # Record it as an explicit NULL default rather than leaving it
                # outstanding, so the queue converges and the target shape is
                # still complete.
                sql = "NULL"
                display = "NULL"
            note = f"Load-time default ({display}); no source attribute."
            why = (f"No source attribute feeds '{attr}'. Defaulted at load to "
                   f"{display} — {u.get('reason', '')}".strip())
        spec.mappings.append(MappingEntry(
            target_attribute=attr, source_attributes=srcs,
            cardinality="derived", transformation_sql=sql,
            transformation_note=note, confidence=1.0, gate="auto_accept",
            match_source="human", rationale=why))
        mapped_names.add(attr)
    spec.unmapped_target = still_unmapped
    # idempotent: certify can be pressed repeatedly, the marker is set once
    if "reviewed" not in spec.generated_by:
        spec.generated_by += "+reviewed"
    return spec
