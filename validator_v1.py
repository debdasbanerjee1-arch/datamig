"""Agent 4 — the validation agent.

Validates a MappingSpec *as a whole* (Agent 3 already validates each rule in
isolation). It materialises the proposed target by applying every transformation
in the DuckDB staging layer, then runs check families that no single rule can
reveal:

  * wellformed     — no duplicate targets, source refs exist, every transform compiles
  * grain          — row count preserved
  * key_integrity  — target key is unique and non-null
  * completeness   — required (non-nullable) targets are populated
  * crossfield     — decoded business rules still hold in the target shape
  * reconciliation — no unexpected value loss vs source

Failures carry the actual offending rows, and the agent demotes gates
accordingly. Almost entirely deterministic — validation is the last place you
want a model guessing. The LLM (optional) only summarises the report.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Optional

from ..models import Gate, target_attributes
from ..staging import Warehouse
from .contracts import (CheckResult, GateAdjustment, MappingSpec,
                        TableInsight, ValidationReport)

SENTINELS = ("00000000", "")
_MAT = "_val_target"   # materialised target table name


def _pop_pred(cols: list[str]) -> str:
    """Rows in which at least one source column carries a real value.

    A mapping may legitimately have NO source column — a load-time default
    promoted from an unmapped target ('SYS1', false, a load timestamp). For
    those, every row qualifies, so the predicate is TRUE. Returning "" here
    would splice an empty WHERE clause into the SQL and raise a parser error.
    """
    if not cols:
        return "TRUE"
    return " OR ".join(
        f"(\"{c}\" IS NOT NULL AND \"{c}\" NOT IN ('00000000',''))" for c in cols
    )


def _rows(wh: Warehouse, sql: str, limit: int = 5) -> list[dict]:
    cur = wh.con.execute(sql + f" LIMIT {limit}")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def validate_spec(spec: MappingSpec, target_dict: dict, insight: TableInsight,
                  warehouse: Warehouse, source_table: str) -> ValidationReport:
    wh = warehouse
    src_cols = set(wh.column_names(source_table))
    checks: list[CheckResult] = []
    # target_attr -> worst severity seen, for gate demotion
    failed: dict[str, str] = {}

    def add(name, cat, status, detail, *, sev="soft", attr=None, n=0, sample=None):
        checks.append(CheckResult(name=name, category=cat, status=status, severity=sev,
                                  detail=detail, target_attribute=attr,
                                  offending_rows=n, sample=sample or []))
        if status == "fail" and attr:
            failed[attr] = "hard" if sev == "hard" else failed.get(attr, "soft")

    # ---------------- wellformed (before materialising) ----------------
    dups = [t for t, c in Counter(m.target_attribute for m in spec.mappings).items() if c > 1]
    add("no_duplicate_targets", "wellformed", "fail" if dups else "pass",
        f"target(s) written more than once: {dups}" if dups else "each target written once.",
        sev="hard")

    # A REJECTED mapping is not accepted, so it is never materialised. Executing
    # SQL we have already rejected can only produce a spurious hard failure: a
    # coded column matched to a numeric target compiled, threw on the real rows,
    # and blocked the entire run on a mapping nobody intended to use. The
    # attribute is instead treated as having no source, so completeness reports
    # the actionable fact ("this required attribute has no accepted source")
    # rather than a cast error nobody asked about.
    #
    # Rejects are NOT ignored: they stay in the review queue, and the verdict
    # below still blocks until a human has ruled on them. Once a reviewer
    # accepts or edits one it stops being a reject, and the next validation pass
    # (review -> apply_decisions -> validation) executes it normally.
    live = [m for m in spec.mappings if m.gate != "reject"]

    compilable, bad_ref, no_compile = [], [], []
    for m in live:
        if not m.transformation_sql:
            continue
        missing = [c for c in m.source_attributes if c not in src_cols]
        if missing:
            bad_ref.append((m.target_attribute, missing)); continue
        try:
            wh.con.execute(f'SELECT {m.transformation_sql} FROM "{source_table}" LIMIT 0')
            compilable.append(m)
        except Exception as e:
            no_compile.append((m.target_attribute, str(e).splitlines()[0]))
    add("source_refs_exist", "wellformed", "fail" if bad_ref else "pass",
        f"accepted mappings referencing unknown columns: {bad_ref}" if bad_ref
        else "all source refs in accepted mappings are valid.", sev="hard")
    add("transforms_compile", "wellformed", "fail" if no_compile else "pass",
        f"accepted transforms failing to compile: {no_compile}" if no_compile
        else "all accepted transforms compile.", sev="hard")

    # a transform can compile yet still throw on the REAL rows (bad cast on a
    # wrongly-guessed source, etc.) — probe each one and turn failures into
    # findings against that mapping instead of letting them kill the run
    executable, no_execute = [], []
    for m in compilable:
        try:
            wh.con.execute(f'SELECT count({m.transformation_sql}) '
                           f'FROM "{source_table}"')
            executable.append(m)
        except Exception as e:
            no_execute.append((m.target_attribute, str(e).splitlines()[0]))
    add("transforms_execute", "wellformed", "fail" if no_execute else "pass",
        (f"accepted transforms failing on the actual data (wrong source?): {no_execute}"
         if no_execute else "all accepted transforms execute on the staged data."),
        sev="hard")
    for attr, err in no_execute:
        add(f"transform_runtime:{attr}", "wellformed", "fail",
            f"transformation throws on real rows: {err}", sev="hard", attr=attr)

    # ---------------- materialise the proposed target ------------------
    # `executable` can legitimately be EMPTY — every mapping rejected, or none
    # survived compilation. An empty column list produced "SELECT , * FROM" and
    # a raw ParserException, crashing the run instead of reporting it. Excluding
    # rejects from materialisation makes that state reachable, so it must be a
    # finding rather than a stack trace.
    cols_sql = ", ".join(f'{m.transformation_sql} AS "{m.target_attribute}"'
                         for m in executable)
    projection = f"{cols_sql}, *" if cols_sql else "*"
    wh.con.execute(f'CREATE OR REPLACE TABLE {_MAT} AS '
                   f'SELECT {projection} FROM "{source_table}"')
    if not executable:
        add("no_accepted_transforms", "wellformed", "fail",
            "no accepted mapping produced a usable transform — every mapping was "
            "rejected or failed to compile, so there is nothing to validate.",
            sev="hard")
    src_n = wh.row_count(source_table)
    tgt_n = wh.con.execute(f"SELECT count(*) FROM {_MAT}").fetchone()[0]

    # ---------------- grain --------------------------------------------
    add("row_count_preserved", "grain", "pass" if src_n == tgt_n else "fail",
        f"{src_n} source rows -> {tgt_n} target rows.", sev="hard")

    # ---------------- key integrity ------------------------------------
    # only trusted 1:1 mappings can stand in for the key — a rejected/weak mapping
    # that happens to borrow the key column as a throwaway source must not count.
    one_to_one: dict[str, str] = {}
    for m in spec.mappings:
        if m.cardinality == "1:1" and len(m.source_attributes) == 1 and m.gate != "reject":
            one_to_one.setdefault(m.source_attributes[0], m.target_attribute)
    key_tgt = one_to_one.get(insight.candidate_keys[0]) if insight.candidate_keys else None
    if key_tgt:
        nulls = wh.con.execute(f'SELECT count(*) FROM {_MAT} WHERE "{key_tgt}" IS NULL').fetchone()[0]
        distinct = wh.con.execute(f'SELECT count(DISTINCT "{key_tgt}") FROM {_MAT}').fetchone()[0]
        ok = (nulls == 0 and distinct == tgt_n)
        add("key_unique_not_null", "key_integrity", "pass" if ok else "fail",
            f"key '{key_tgt}': {nulls} null(s), {distinct}/{tgt_n} distinct.",
            sev="hard", attr=key_tgt, n=nulls)
    else:
        add("key_identified", "key_integrity", "warn", "no target key identified from source key.")

    # ---------------- completeness (required targets) ------------------
    materialised = {m.target_attribute for m in executable}
    mapped = {m.target_attribute for m in spec.mappings} & materialised
    unmapped_t = {u["attribute"] for u in spec.unmapped_target}
    for attr in target_attributes(target_dict):
        name, required = attr["name"], not attr.get("nullable", False)
        if not required:
            continue
        if name in unmapped_t:
            add(f"required_present:{name}", "completeness", "warn",
                f"No source field maps to required attribute '{name}'. A default value must be set at load time.", attr=name)
        elif name in mapped:
            nulls = wh.con.execute(f'SELECT count(*) FROM {_MAT} WHERE "{name}" IS NULL').fetchone()[0]
            add(f"required_present:{name}", "completeness", "fail" if nulls else "pass",
                f"'{name}' required; {nulls} null(s) after transform." if nulls
                else f"'{name}' fully populated.", sev="hard", attr=name, n=nulls,
                sample=_rows(wh, f'SELECT "{name}" FROM {_MAT} WHERE "{name}" IS NULL') if nulls else None)

    # ---------------- crossfield (decoded business rules) --------------
    for d in insight.dependencies:
        dep_tgt = one_to_one.get(d.dependent)
        if dep_tgt and dep_tgt not in materialised:
            continue
        mobj = re.search(r"(\w+)\s+in\s+\{([^}]*)\}", d.condition or "")
        if not (dep_tgt and mobj):
            continue
        driver, vals = mobj.group(1), [v.strip() for v in mobj.group(2).split(",")]
        inlist = ", ".join("'" + str(v).replace("'", "''") + "'" for v in vals)
        where = f'"{dep_tgt}" IS NOT NULL AND "{driver}" NOT IN ({inlist})'
        bad = wh.con.execute(f"SELECT count(*) FROM {_MAT} WHERE {where}").fetchone()[0]
        add(f"crossfield:{dep_tgt}~{driver}", "crossfield", "pass" if not bad else "fail",
            f"'{dep_tgt}' populated only when {driver} in {{{', '.join(vals)}}}: "
            f"{bad} violation(s)." , sev="soft", attr=dep_tgt, n=bad,
            sample=_rows(wh, f'SELECT "{dep_tgt}", "{driver}" FROM {_MAT} WHERE {where}') if bad else None)

    # ---------------- reconciliation (value loss) ----------------------
    for m in executable:
        # a mapping with no source column is a constant / load-time default —
        # there is no source value that could have been lost, so reconciling it
        # is meaningless (and the sample query would have no columns to select)
        if not m.source_attributes:
            add(f"reconciliation:{m.target_attribute}", "reconciliation", "pass",
                f"'{m.target_attribute}': load-time default, no source value to reconcile.",
                attr=m.target_attribute)
            continue
        pred = _pop_pred(m.source_attributes)
        src_pop = wh.con.execute(f'SELECT count(*) FROM {_MAT} WHERE {pred}').fetchone()[0]
        tgt_nn = wh.con.execute(
            f'SELECT count(*) FROM {_MAT} WHERE "{m.target_attribute}" IS NOT NULL AND ({pred})'
        ).fetchone()[0]
        loss = src_pop - tgt_nn
        if loss > 0:
            # show the distinct source values that were populated but produced NULL
            samp = _rows(wh, f'SELECT DISTINCT {", ".join(chr(34)+c+chr(34) for c in m.source_attributes)} '
                             f'FROM {_MAT} WHERE "{m.target_attribute}" IS NULL AND ({pred})')
            add(f"reconciliation:{m.target_attribute}", "reconciliation", "warn",
                f"{loss} source row(s) had a value but became NULL in '{m.target_attribute}' "
                f"after the transform — likely an unmapped code.", attr=m.target_attribute, n=loss, sample=samp)
        else:
            add(f"reconciliation:{m.target_attribute}", "reconciliation", "pass",
                f"'{m.target_attribute}': no value loss ({tgt_nn}/{src_pop}).", attr=m.target_attribute)

    # ---------------- gate demotion ------------------------------------
    adjustments: list[GateAdjustment] = []
    for m in spec.mappings:
        sev = failed.get(m.target_attribute)
        if not sev:
            continue
        to = Gate.REJECT.value if sev == "hard" else Gate.REVIEW.value
        order = {"reject": 0, "review": 1, "auto_accept": 2}
        if order[to] < order.get(m.gate, 2):
            adjustments.append(GateAdjustment(
                target_attribute=m.target_attribute, from_gate=m.gate, to_gate=to,
                reason=f"failed validation ({sev})"))
            m.gate = to  # apply to the spec in place

    # ---------------- verdict ------------------------------------------
    hard_fail = any(c.status == "fail" and c.severity == "hard" for c in checks)
    # an unresolved review always blocks; a reject blocks until a human has ruled on
    # it — once the queue has been reviewed, a confirmed reject is a deliberate
    # exclusion (handle separately / don't migrate), not an outstanding data problem.
    reviewed = "reviewed" in spec.generated_by
    # WARNINGS ARE ADVISORY, NOT BLOCKING. A warn is an observation about the
    # data (e.g. "3 rows carried an exit reason with no target enum value") —
    # it is surfaced on the report and on the item's card, but NO reviewer
    # decision can clear it, because accepting a mapping does not change the
    # underlying rows. Treating warns as blocking pinned the verdict at
    # needs_review forever: the reviewer resolved every item, certified, and
    # got the same queue back. Only real failures and undecided gates block.
    blocking = (
        any(c.status == "fail" for c in checks)
        or any(m.gate == "review" for m in spec.mappings)
        or (not reviewed and any(m.gate == "reject" for m in spec.mappings))
    )
    verdict = "blocked" if hard_fail else ("needs_review" if blocking else "certified")

    stats = {
        "checks": len(checks),
        "passed": sum(c.status == "pass" for c in checks),
        "warnings": sum(c.status == "warn" for c in checks),
        "failures": sum(c.status == "fail" for c in checks),
        "gate_demotions": len(adjustments),
    }
    report = ValidationReport(
        source_table=spec.source_table, target_table=spec.target_table,
        verdict=verdict, checks=checks, gate_adjustments=adjustments, stats=stats,
    )
    return _narrate(report)


# ----------------------------------------------------- LLM summary (optional)
def _narrate(report: ValidationReport) -> ValidationReport:
    from .. import config
    client, model = config.llm_client()
    if client is None:
        report.generated_by = "deterministic+offline_stub"
        return report

    sys = ("You are a data-migration QA lead. Summarise this validation report into a "
           "short, prioritised list of what a reviewer must act on. Do NOT change any "
           "pass/fail verdicts — they are computed. Return ONLY JSON: {summary: [strings]}.")
    try:
        resp = client.chat.completions.create(
            model=model, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": json.dumps(report.model_dump(), default=str)}],
        )
        report.stats["llm_summary"] = json.loads(resp.choices[0].message.content).get("summary", [])
        report.generated_by = "deterministic+llm"
    except Exception as e:
        report.stats["llm_note"] = f"summary skipped: {e}"
    return report
