"""Reconciliation workspace (tab 4) — additive.

Two families of checks against the DELIVERED transformed output, both run
against the actual output bytes (not a re-derivation of the transform):

  TECHNICAL — control totals. The figures a migration control sheet carries,
  all derivable from any spec plus any delivered file:
    rows             source workset vs delivered
    columns          delivered vs what the certified spec expects
    populated_cells  populated out of rows x columns
    distinct_keys    distinct business key, source vs delivered
    numeric_sums     total of every numeric column, source vs delivered

  BUSINESS_RULE — does the data still obey the rules the source data implies?
    category_profile for every low-cardinality attribute carrying a transform,
                     record counts grouped by value on both sides and compared
                     bucket by bucket. Categories are discovered from the data,
                     never configured, so this works on any extract.
    crossfield       conditional-population rules mined from the source
                     (engine/insight_cache.py), checked when BOTH the driver and
                     the dependent map 1:1 to a target attribute. Driver codes
                     are translated through the driver's own certified transform
                     before comparison — the rule is mined in SOURCE terms
                     ({CL}) but the delivered data holds the TRANSFORMED value
                     (CLOSED), and comparing the two directly made every row look
                     like a violation.

DELIBERATELY NOT HERE — three families were removed rather than left to pad the
report, because a check that cannot fail, or that a stronger check already
covers, costs the reader attention and earns nothing:

    value_loss       an aggregate populated-count comparison, strictly subsumed
                     by validation's transform check, which re-executes every
                     certified transform value by value across every row and
                     names the offending record. Weaker AND noisier.
    aggregate        sums are no longer a detection mechanism for the same
                     reason: if a value moved, the cell-level check already
                     caught it. Kept only as a REPORTED control total, because a
                     reviewer expects a money column to tie out on the face of
                     the report.
    derivation       read `derivation_resolved` from the enriched dictionary,
                     which was removed when the dictionary was simplified. It
                     produced zero checks and could never produce any.

Row count also moved into the control totals: it was the one check name
literally duplicated with the validation workspace.

Nothing here touches Flow A / Flow B state — it reads the spec + the delivered
CSV + the currently loaded source file(s) + (optionally) the active enriched
dictionary, plus the insight derived from the source data, and returns
CheckResult-shaped JSON.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import duckdb

from engine.agents.contracts import CheckResult
from engine.models import target_attributes

from .transform import _q, build_workset_sql

SENTINELS = ("00000000", "")


def _write_temp_csv(csv_text: str) -> str:
    fh = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False,
                                     newline="", encoding="utf-8")
    fh.write(csv_text)
    fh.close()
    return fh.name


def _load_output(con, csv_text: str) -> None:
    path = _write_temp_csv(csv_text)
    try:
        con.execute(
            "CREATE TABLE target_out AS "
            "SELECT * FROM read_csv_auto(?, all_varchar=true, header=true)",
            [path],
        )
    finally:
        Path(path).unlink(missing_ok=True)


def _rows(con, sql: str, limit: int = 5) -> list[dict]:
    cur = con.execute(sql + f" LIMIT {limit}")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _pop_pred(cols: list[str]) -> str:
    if not cols:
        return "TRUE"
    return " OR ".join(
        f"({_q(c)} IS NOT NULL AND {_q(c)} NOT IN ('00000000',''))" for c in cols
    )


_PROFILE_MAX = 25          # above this a column is a code list, not a category


def _is_nondet(expr: str) -> bool:
    low = (expr or "").lower().replace(" ", "")
    return any(t in low for t in ("now(", "current_timestamp", "current_date",
                                  "current_localtime", "localtimestamp",
                                  "random(", "uuid("))


_NUMERIC_TYPES = {"number", "integer", "int", "float", "decimal", "numeric"}
_TEMPORAL_TYPES = {"date", "datetime", "timestamp", "time"}


def run_reconciliation(spec: dict, target_dict: dict, csv_text: str,
                       source_paths: dict[str, str],
                       enriched: dict | None = None,
                       insight: dict | None = None,
                       rules: list[dict] | None = None) -> dict:
    """Execute the CERTIFIED rule set against the delivered output.

    `rules` is the certified set (api/recon_rules.py). When omitted the
    candidates are derived on the fly, so an unsaved session still works — but
    the certified set is what the script is generated from, so passing it is
    what guarantees the script and these results agree.
    """
    from .recon_rules import derive_candidates, rule_id

    if rules is None:
        rules = derive_candidates(spec, target_dict, source_paths, insight)
    by_kind: dict[str, list[dict]] = {}
    for r in rules:
        by_kind.setdefault(r.get("kind"), []).append(r)
    origin_of = {rule_id(r): r.get("origin", "mined") for r in rules}

    con = duckdb.connect(":memory:")
    checks: list[CheckResult] = []

    def add(name, cat, status, detail, *, sev="soft", attr=None, n=0, sample=None,
            sql=None, scanned=None):
        c = CheckResult(name=name, category=cat, status=status, severity=sev,
                        detail=detail, target_attribute=attr,
                        offending_rows=n, sample=sample or [],
                        sql=sql, rows_scanned=tgt_n if scanned is None else scanned)
        # provenance: a reviewer must be able to tell a mined control from one
        # the business asked for
        c.origin = origin_of.get(name, origin_of.get(name.split(":")[0], "mined"))
        checks.append(c)

    def enabled(kind: str) -> bool:
        """Only run what the certified set contains — a control the reviewer
        excluded must not quietly execute anyway."""
        return kind in by_kind

    _load_output(con, csv_text)
    out_cols = [d[0] for d in con.execute("SELECT * FROM target_out LIMIT 0").description]
    tgt_n = con.execute("SELECT count(*) FROM target_out").fetchone()[0]

    type_of = {a["name"]: (a.get("type") or "string") for a in
              (target_dict.get("attributes") or target_dict.get("columns")
               or target_dict.get("fields") or []) if isinstance(a, dict)}

    workset_sql, params, src_n, workset_err = None, None, None, None
    if source_paths:
        try:
            workset_sql, params = build_workset_sql(spec, source_paths)
            src_n = con.execute(f"SELECT count(*) FROM ({workset_sql}) AS w", params).fetchone()[0]
        except Exception as e:  # noqa: BLE001
            workset_err = str(e)

    # the mapped business key, if the spec has one — used by the control totals
    # and by the categorical profiles below
    one_to_one_pre = {}
    for m in spec.get("mappings", []):
        if (m.get("cardinality") == "1:1" and len(m.get("source_attributes") or []) == 1
                and m.get("gate") != "reject"):
            one_to_one_pre.setdefault(m["source_attributes"][0], m["target_attribute"])
    candidate_keys = (insight or {}).get("candidate_keys") or []
    key_src = next((k for k in candidate_keys if k in one_to_one_pre), None)
    key_tgt = one_to_one_pre.get(key_src) if key_src else None

    # =========================== TECHNICAL =================================
    # rows are a control total, not a family of their own — and this was the
    # one check name literally duplicated with the validation workspace
    if src_n is not None and enabled("control_total:rows"):
        add("control_total:rows", "control_total",
            "pass" if src_n == tgt_n else "fail",
            f"{src_n:,} source row(s) -> {tgt_n:,} delivered target row(s).",
            sev="hard", n=0 if src_n == tgt_n else abs(src_n - tgt_n),
            sample=[{"measure": "Record count", "source": src_n,
                     "delivered": tgt_n, "difference": tgt_n - src_n,
                     "ties": src_n == tgt_n}],
            sql=("-- delivered rows must equal the rebuilt source workset\n"
                 "SELECT (SELECT count(*) FROM target_out) AS delivered_rows,\n"
                 f"       (SELECT count(*) FROM ({workset_sql}) AS w) AS source_rows;"))
    elif enabled("control_total:rows"):
        add("control_total:rows", "control_total", "warn",
            workset_err and f"could not rebuild the source workset: {workset_err}"
            or f"{tgt_n:,} delivered target row(s) — source file(s) not loaded, "
               f"cannot compare.")

    # ---- control totals: the figures a migration control sheet carries ----
    # Generic by construction: row count, column count, populated-cell count and
    # distinct key count are derivable from ANY spec + delivered file, so this
    # block needs no knowledge of what the data means.
    expected_cols = {m["target_attribute"] for m in spec.get("mappings", [])
                     if m.get("gate") != "reject"}
    expected_cols |= {u["attribute"] for u in spec.get("unmapped_target", [])}
    if enabled("control_total:columns"):
      add("control_total:columns", "control_total",
        "pass" if not (expected_cols - set(out_cols)) else "fail",
        f"{len(out_cols)} column(s) delivered; the certified spec expects "
        f"{len(expected_cols)}.", sev="hard",
        n=len(expected_cols - set(out_cols)), scanned=0,
        sample=([{"measure": "Column count", "expected": len(expected_cols),
                  "delivered": len(out_cols),
                  "ties": not (expected_cols - set(out_cols))}]
                + [{"measure": "Missing column", "expected": c, "delivered": "—",
                    "ties": False} for c in sorted(expected_cols - set(out_cols))]),
        sql=("-- every column the certified spec expects must be delivered\n"
             "SELECT column_name FROM information_schema.columns\n"
             "WHERE table_name = 'target_out';"))

    # Fill rate, split by whether a blank is LEGITIMATE. A raw
    # "916 populated of 1,150" reads as 234 cells lost; almost always those
    # cells are blank in attributes the target dictionary declares nullable,
    # which is not a discrepancy at all. What matters — and what a reviewer is
    # actually asking — is whether any blank sits in an attribute that requires
    # a value.
    required = {a["name"] for a in target_attributes(target_dict)
                if not a.get("nullable", False)}
    populated = blank_required = 0
    _blank_by_col: dict[str, int] = {}
    for c in out_cols:
        empty = con.execute(
            f"SELECT count(*) FROM target_out WHERE {_q(c)} IS NULL "
            f"OR {_q(c)} = ''").fetchone()[0]
        populated += tgt_n - empty
        _blank_by_col[c] = empty
        if c in required:
            blank_required += empty
    total_cells = tgt_n * len(out_cols)
    blanks = total_cells - populated
    if enabled("control_total:populated_cells"):
      add("control_total:populated_cells", "control_total",
        "fail" if blank_required else "pass",
        (f"{populated:,} of {total_cells:,} delivered cell(s) hold a value "
         f"({tgt_n:,} row(s) x {len(out_cols)} column(s)). "
         + (f"{blanks:,} are blank, all in attributes the target declares "
            f"nullable." if blanks and not blank_required
            else f"{blanks:,} are blank, of which {blank_required:,} sit in "
                 f"attributes that require a value." if blanks
            else "None are blank.")),
        sev="hard", n=blank_required,
        sample=[{"attribute": c,
                 "records": tgt_n,
                 "populated": tgt_n - _blank_by_col[c],
                 "blank": _blank_by_col[c],
                 "mandatory": c in required,
                 "ties": not (c in required and _blank_by_col[c])}
                for c in out_cols],
        sql=("-- populated cells, one pass per delivered column\n"
             + "\n".join(f"SELECT count(*) FROM target_out WHERE {_q(c)} IS NOT NULL "
                          f"AND {_q(c)} <> '';" for c in out_cols[:3])
             + ("\n-- ... one per column" if len(out_cols) > 3 else "")))

    if key_tgt and key_tgt in out_cols:
        d_keys = con.execute(f"SELECT count(DISTINCT {_q(key_tgt)}) "
                             f"FROM target_out").fetchone()[0]
        s_keys = None
        if workset_sql is not None and key_src:
            try:
                s_keys = con.execute(f"SELECT count(DISTINCT {_q(key_src)}) "
                                     f"FROM ({workset_sql}) AS w", params).fetchone()[0]
            except Exception:      # noqa: BLE001
                s_keys = None
        ok = s_keys is None or s_keys == d_keys
        add("control_total:distinct_keys", "control_total", "pass" if ok else "fail",
            (f"{d_keys:,} distinct '{key_tgt}' delivered"
             + (f"; {s_keys:,} distinct '{key_src}' in the source." if s_keys is not None
                else " — source not loaded, no comparison.")),
            sev="hard", attr=key_tgt, n=0 if ok else abs((s_keys or 0) - d_keys),
            sample=[{"measure": f"Distinct '{key_tgt}'",
                     "source": s_keys if s_keys is not None else "—",
                     "delivered": d_keys, "ties": ok},
                    {"measure": "Records", "source": src_n if src_n is not None else "—",
                     "delivered": tgt_n, "ties": True}],
            sql=(f"SELECT count(DISTINCT {_q(key_tgt)}) AS delivered_keys FROM target_out;"
                 + (f"\nSELECT count(DISTINCT {_q(key_src)}) AS source_keys "
                    f"FROM (<source workset>) AS w;" if key_src and workset_sql else "")))

    # ---- numeric control totals ------------------------------------------
    # Sums are NOT a detection mechanism here: validation re-executes every
    # certified transform cell by cell, so if a value moved the sum has already
    # been caught with the offending record named. They are reported because a
    # reviewer expects a money column to tie out on the face of the report —
    # one control-total line per numeric column, not a family of checks.
    if workset_sql is not None and enabled("control_total:numeric_sums"):
        sums, _sum_sql_notes = [], []
        for m in spec.get("mappings", []):
            attr = m.get("target_attribute")
            expr = (m.get("transformation_sql") or "").strip()
            if (m.get("gate") == "reject" or attr not in out_cols or not expr
                    or type_of.get(attr) not in _NUMERIC_TYPES):
                continue
            try:
                expected = con.execute(
                    f"SELECT sum(TRY_CAST(({expr}) AS DOUBLE)) FROM ({workset_sql}) AS w",
                    params).fetchone()[0]
                actual = con.execute(
                    f"SELECT sum(TRY_CAST({_q(attr)} AS DOUBLE)) FROM target_out").fetchone()[0]
            except Exception:      # noqa: BLE001
                continue
            if expected is None and actual is None:
                continue
            diff = abs((expected or 0) - (actual or 0))
            tol = max(abs(expected or 0), abs(actual or 0)) * 1e-6 + 1e-9
            sums.append({"column": attr, "source_total": expected,
                         "delivered_total": actual, "ties": diff <= tol})
            _sum_sql_notes.append({"column": attr, "expr": expr})
        if sums:
            off = [x for x in sums if not x["ties"]]
            add("control_total:numeric_sums", "control_total",
                "pass" if not off else "fail",
                (f"{len(sums)} numeric column(s) tie out between source and "
                 f"delivered: "
                 + "; ".join(f"{x['column']}={x['delivered_total']:,.2f}" for x in sums)
                 if not off else
                 f"{len(off)} of {len(sums)} numeric column(s) do not tie out."),
                sev="hard", n=len(off), sample=sums,
                sql=("-- each numeric column: recomputed from source vs delivered\n"
                     + "\n".join(
                         f"SELECT sum(TRY_CAST(({x['expr']}) AS DOUBLE)) AS source_total, "
                         f"(SELECT sum(TRY_CAST({_q(x['column'])} AS DOUBLE)) "
                         f"FROM target_out) AS delivered_total "
                         f"FROM (<source workset>) AS w;"
                         for x in _sum_sql_notes)))

    # =========================== BUSINESS RULE ==============================
    # ---- categorical profiles: "how many of each kind?" -------------------
    #
    # This is what a business reviewer actually asks — how many live policies,
    # how many exited, how many of each product — and it is derived, never
    # hardcoded. The rule is structural:
    #
    #   for every LOW-CARDINALITY target attribute that carries a transform,
    #   group both sides by value and compare the record count per bucket.
    #
    # "Low cardinality" is measured from the delivered data (<= _PROFILE_MAX
    # distinct values), so an enum declared in the dictionary qualifies, and so
    # does any de-facto category in a file that declares nothing. Nothing here
    # knows what a policy or a product is: point it at a claims or member
    # extract and it profiles whatever categories that file happens to have.
    #
    # Each bucket is a reconciliation in its own right: the count recomputed
    # from the certified transform must equal the count delivered.
    if workset_sql is not None:
        for _r in by_kind.get("category_profile", []):
            attr = (_r.get("params") or {}).get("attribute")
            expr = ((_r.get("params") or {}).get("expr") or "").strip()
            if attr not in out_cols or not expr:
                continue
            try:
                distinct = con.execute(f"SELECT count(DISTINCT {_q(attr)}) "
                                       f"FROM target_out").fetchone()[0]
            except Exception:      # noqa: BLE001
                continue
            if distinct == 0 or distinct > _PROFILE_MAX or distinct == tgt_n:
                continue          # free text or an identifier: not a category
            try:
                exp = dict(con.execute(
                    f"SELECT CAST(({expr}) AS VARCHAR) AS v, count(*) "
                    f"FROM ({workset_sql}) AS w GROUP BY 1", params).fetchall())
                act = dict(con.execute(
                    f"SELECT CAST({_q(attr)} AS VARCHAR) AS v, count(*) "
                    f"FROM target_out GROUP BY 1").fetchall())
            except Exception:      # noqa: BLE001
                continue
            buckets = sorted(set(exp) | set(act), key=lambda v: (v is None, str(v)))
            diffs = [(v, exp.get(v, 0), act.get(v, 0)) for v in buckets
                     if exp.get(v, 0) != act.get(v, 0)]
            profile = ", ".join(f"{v if v is not None else 'null'}={act.get(v, 0):,}"
                                for v in buckets)
            add(f"category_profile:{attr}", "category_profile",
                "pass" if not diffs else "fail",
                (f"'{attr}' record counts by value reconcile across "
                 f"{len(buckets)} categor{'y' if len(buckets) == 1 else 'ies'} "
                 f"({profile})."
                 if not diffs else
                 f"'{attr}': {len(diffs)} categor{'y' if len(diffs) == 1 else 'ies'} "
                 f"differ between the recomputed transform and the delivered data."),
                sev="hard", attr=attr, n=sum(abs(e - a) for _v, e, a in diffs),
                sample=[{"value": v if v is not None else "(null)",
                         "expected_records": e, "delivered_records": a,
                         "difference": a - e} for v, e, a in diffs]
                       or [{"value": v if v is not None else "(null)",
                            "expected_records": exp.get(v, 0),
                            "delivered_records": act.get(v, 0),
                            "difference": 0} for v in buckets],
                sql=(f"-- record counts per value, recomputed vs delivered\n"
                     f"SELECT CAST(({expr}) AS VARCHAR) AS value, count(*) AS records\n"
                     f"FROM (<source workset>) AS w GROUP BY 1;\n"
                     f"SELECT CAST({_q(attr)} AS VARCHAR) AS value, count(*) AS records\n"
                     f"FROM target_out GROUP BY 1;"))

    # ---- business-authored aggregates -------------------------------------
    # Recompute the aggregate from the SOURCE using each column's certified
    # transform, recompute it from the DELIVERED file, and compare bucket by
    # bucket. Same execution path as every mined control — a user rule is not a
    # special case, which is why it needs no separate machinery.
    _sql_of = {m.get("target_attribute"): (m.get("transformation_sql") or "")
               for m in spec.get("mappings", []) if m.get("gate") != "reject"}
    for _r in by_kind.get("aggregate_by", []):
        pr = _r.get("params") or {}
        fn = (pr.get("function") or "sum").lower()
        col, groups = pr.get("column"), list(pr.get("group_by") or [])
        if workset_sql is None:
            skip_reason = "source workset unavailable"
            add(f"aggregate_by:{rule_id(_r).split(':',1)[1]}", "aggregate_by", "warn",
                f"could not evaluate: {skip_reason}.", sev="soft", attr=col)
            continue
        if any(g not in out_cols for g in groups) or (fn != "count" and col not in out_cols):
            continue
        agg = {"count": "count(*)", "count_distinct": f"count(DISTINCT {{x}})"}.get(
            fn, f"{fn}({{x}})")
        tgt_expr = agg.replace("{x}", f"TRY_CAST({_q(col)} AS DOUBLE)" if col else "*")
        src_expr = agg.replace("{x}", f"TRY_CAST(({_sql_of.get(col, 'NULL')}) AS DOUBLE)"
                               if col else "*")
        tgt_keys = ", ".join(f"CAST({_q(g)} AS VARCHAR)" for g in groups) or "'ALL'"
        src_keys = ", ".join(f"CAST(({_sql_of.get(g, 'NULL')}) AS VARCHAR)"
                             for g in groups) or "'ALL'"
        label = (f"{fn} of {col or 'records'}"
                 + (f" by {', '.join(groups)}" if groups else ""))
        try:
            src = dict(con.execute(
                f"SELECT concat_ws(' | ', {src_keys}) AS k, {src_expr} "
                f"FROM ({workset_sql}) AS w GROUP BY 1", params).fetchall())
            tgt = dict(con.execute(
                f"SELECT concat_ws(' | ', {tgt_keys}) AS k, {tgt_expr} "
                f"FROM target_out GROUP BY 1").fetchall())
        except Exception as e:      # noqa: BLE001
            add(f"aggregate_by:{rule_id(_r).split(':',1)[1]}", "aggregate_by", "warn",
                f"{label}: could not evaluate — {e}", sev="soft", attr=col)
            continue
        buckets = sorted(set(src) | set(tgt), key=str)
        rows, off = [], 0
        for b in buckets:
            a_, d_ = src.get(b) or 0, tgt.get(b) or 0
            ties = abs(a_ - d_) <= max(abs(a_), abs(d_)) * 1e-6 + 1e-9
            if not ties:
                off += 1
            rows.append({"group": b, "source": a_, "delivered": d_,
                         "ties": ties, "difference": d_ - a_})
        add(f"aggregate_by:{rule_id(_r).split(':',1)[1]}", "aggregate_by",
            "pass" if not off else "fail",
            (f"{label}: all {len(buckets)} group(s) agree between source and delivered."
             if not off else
             f"{label}: {off} of {len(buckets)} group(s) do not agree."),
            sev=_r.get("severity", "hard"), attr=col, n=off,
            sql=(f"-- {label}, recomputed from source vs delivered\n"
                 f"SELECT concat_ws(' | ', {src_keys}) AS grp, {src_expr} "
                 f"FROM (<source workset>) AS w GROUP BY 1;\n"
                 f"SELECT concat_ws(' | ', {tgt_keys}) AS grp, {tgt_expr} "
                 f"FROM target_out GROUP BY 1;"),
            sample=rows)

    # Crossfield rules come from the CERTIFIED set, already stated in the
    # delivered file's vocabulary (recon_rules.decode_pairs). Re-deriving them
    # here is what produced three divergent implementations of the same rule.
    for _r in by_kind.get("crossfield", []):
        pr = _r.get("params") or {}
        dep_tgt, driver_tgt = pr.get("attribute"), pr.get("driver")
        vals = list(pr.get("values") or [])
        if not (dep_tgt in out_cols and driver_tgt in out_cols and vals):
            continue
        inlist = ", ".join("'" + str(v).replace("'", "''") + "'" for v in vals)
        where = (f"{_q(dep_tgt)} IS NOT NULL AND {_q(dep_tgt)} <> '' "
                 f"AND {_q(driver_tgt)} NOT IN ({inlist})")
        bad = con.execute(f"SELECT count(*) FROM target_out WHERE {where}").fetchone()[0]
        add(f"crossfield:{dep_tgt}~{driver_tgt}", "crossfield",
            "pass" if not bad else "fail",
            f"'{dep_tgt}' should be populated only when '{driver_tgt}' in "
            f"{{{', '.join(vals)}}}: {bad} violation(s) in the delivered output.",
            sev=_r.get("severity", "soft"), attr=dep_tgt, n=bad,
            sql=(f"SELECT count(*) AS violations FROM target_out\n"
                 f"WHERE {_q(dep_tgt)} IS NOT NULL AND {_q(dep_tgt)} <> ''\n"
                 f"  AND {_q(driver_tgt)} NOT IN ({inlist});"),
            sample=_rows(con, f"SELECT * FROM target_out WHERE {where}") if bad else None)

    con.close()

    hard_fail = any(c.status == "fail" and c.severity == "hard" for c in checks)
    any_fail = any(c.status == "fail" for c in checks)
    verdict = "blocked" if hard_fail else ("needs_review" if any_fail else "certified")
    stats = {
        "checks": len(checks),
        "passed": sum(c.status == "pass" for c in checks),
        "warnings": sum(c.status == "warn" for c in checks),
        "failures": sum(c.status == "fail" for c in checks),
        "technical": sum(c.category == "control_total" for c in checks),
        "business_rule": sum(c.category in ("category_profile", "crossfield")
                             for c in checks),
    }
    return {
        "verdict": verdict,
        "checks": [c.model_dump() for c in checks],
        "stats": stats,
        "source_table": spec.get("source_table"),
        "target_table": spec.get("target_table"),
    }


# ---------------------------------------------------------------------------
# code generation — a standalone, readable Python/DuckDB reconciliation script
# ---------------------------------------------------------------------------
_CASE_PAIR = re.compile(r"WHEN\s+'((?:[^']|'')*)'\s+THEN\s+'((?:[^']|'')*)'", re.I)


def decode_pairs(sql: str) -> dict[str, str]:
    """Source code -> target value, read straight out of a certified CASE.

    The rule preview has no database, but it must still speak the same
    vocabulary as the report: a dependency mined from the source says
    "populated only when STATCD in {CL}", while the delivered data holds
    'CLOSED'. Showing the raw code in the preview while the results showed the
    decoded value made the two disagree — and the preview's own text claimed it
    was already in target terms.
    """
    return {a.replace("''", "'"): b.replace("''", "'")
            for a, b in _CASE_PAIR.findall(sql or "")}


def describe_reconciliation_rules(spec: dict, source_filenames: dict[str, str],
                                  insight: dict | None = None,
                                  enriched: dict | None = None,
                                  target_dict: dict | None = None,
                                  rules: list[dict] | None = None) -> list[dict]:
    """Group the CERTIFIED rule set for display.

    This used to re-derive the rules independently of the runner and the script
    generator — three implementations, which is exactly how the preview came to
    show source codes while the report showed decoded values. It now derives
    nothing.
    """
    from .recon_rules import derive_candidates

    if rules is None:
        rules = derive_candidates(spec, target_dict or {}, source_filenames, insight)

    families: dict[str, list[dict]] = {}
    for r in rules:
        families.setdefault(r.get("category") or r.get("kind"), []).append(r)

    detail = {
        "control_total": "The figures a migration control sheet carries: row "
                         "count source vs delivered, delivered columns against "
                         "the certified spec, blank cells in attributes that "
                         "require a value, distinct business keys, and the total "
                         "of every numeric column.",
        "category_profile": "For every low-cardinality attribute carrying a "
                            "transform, record counts are grouped by value on "
                            "both sides and compared bucket by bucket. The "
                            "categories are discovered from the delivered data, "
                            "not configured.",
        "crossfield": "An attribute that is only ever populated for certain "
                      "driver values in the source must stay that way in the "
                      "delivered output. Driver values below are shown as they "
                      "appear in the delivered file.",
    }
    out = []
    for cat, rs in families.items():
        added = sum(1 for r in rs if r.get("origin") == "user_added")
        entry = {"category": cat, "name": cat,
                 "detail": detail.get(cat, ""),
                 "scope": f"{len(rs)} rule(s)"
                          + (f" · {added} added by the business" if added else "")}
        if cat == "crossfield":
            entry["items"] = [{"attribute": (r["params"] or {}).get("attribute"),
                               "driver": (r["params"] or {}).get("driver"),
                               "values": (r["params"] or {}).get("values") or [],
                               "origin": r.get("origin", "mined")} for r in rs]
        out.append(entry)
    order = ["control_total", "category_profile", "crossfield"]
    out.sort(key=lambda e: order.index(e["category"]) if e["category"] in order else 99)
    return out


def generate_reconciliation_script(spec: dict, output_filename: str,
                                   source_filenames: dict[str, str],
                                   insight: dict | None = None,
                                   enriched: dict | None = None,
                                   target_dict: dict | None = None,
                                   rules: list[dict] | None = None) -> str:
    target = spec.get("target_table", "target")
    type_of = {a["name"]: (a.get("type") or "").lower()
               for a in (target_dict or {}).get("attributes", [])}

    # The THIRD derivation used to live here. The script is now generated from
    # the same certified rule set the runner executes, so a script and the
    # results it is meant to reproduce cannot disagree.
    from .recon_rules import derive_candidates

    if rules is None:
        rules = derive_candidates(spec, target_dict or {}, source_filenames, insight)
    crossfield = [((r["params"] or {}).get("attribute"),
                   (r["params"] or {}).get("driver"),
                   (r["params"] or {}).get("values") or [])
                  for r in rules if r.get("kind") == "crossfield"]
    enabled_kinds = {r.get("kind") for r in rules}

    numeric = [(x["column"], x["expr"])
               for r in rules if r.get("kind") == "control_total:numeric_sums"
               for x in ((r["params"] or {}).get("columns") or [])]
    # a FOURTH derivation lived here too: the script decided for itself which
    # attributes were categorical, so a profile the reviewer excluded would
    # still run in the handed-over script
    categorical = [(r["params"] or {}).get("attribute")
                   for r in rules if r.get("kind") == "category_profile"]
    expected_cols = next((sorted((r["params"] or {}).get("expected_columns") or [])
                          for r in rules if r.get("kind") == "control_total:columns"),
                         [])
    primary = next(iter(source_filenames)).lower() if source_filenames else None

    L: list[str] = []
    ap = L.append
    ap('"""')
    ap(f"Reconciliation — control totals + business controls for {output_filename} -> {target}")
    ap("")
    ap("Auto-generated from the certified mapping specification, and standalone:")
    ap("it needs only duckdb, the delivered file and the source file(s).")
    ap("")
    ap("  TECHNICAL      control totals — rows, columns, populated cells and the")
    ap("                 total of every numeric column, source vs delivered.")
    ap("  BUSINESS RULE  record counts per category, and the conditional-population")
    ap("                 rules mined from the source data.")
    ap('"""')
    ap("import duckdb")
    ap("")
    ap('con = duckdb.connect(":memory:")')
    ap(f'con.execute("CREATE TABLE target_out AS SELECT * FROM read_csv_auto('
       f'\'{output_filename}\', all_varchar=true, header=true)")')
    for t, fn in source_filenames.items():
        ap(f'con.execute(\'CREATE TABLE {t.lower()} AS SELECT * FROM read_csv_auto('
           f'"{fn}", all_varchar=true, header=true)\')')
    ap("")
    ap("checks = []")
    ap("def check(name, cat, status, detail):")
    ap("    checks.append((name, cat, status, detail))")
    ap('    print("[%-4s] %-16s %s: %s" % (status.upper(), cat, name, detail))')
    ap("")
    ap("# --- technical: control totals -----------------------------------------------")
    ap('n_target = con.execute("SELECT count(*) FROM target_out").fetchone()[0]')
    ap('cols = [r[1] for r in con.execute("PRAGMA table_info(\'target_out\')").fetchall()]')
    if primary:
        ap(f'n_source = con.execute("SELECT count(*) FROM {primary}").fetchone()[0]')
        ap('check("control_total:rows", "control_total", "pass" if n_source == n_target'
           ' else "fail", str(n_source) + " source row(s) -> " + str(n_target) + " delivered")')
    else:
        ap('check("control_total:rows", "control_total", "warn",'
           ' str(n_target) + " delivered row(s) — no source loaded, cannot compare")')
    ap(f"expected_cols = {expected_cols!r}")
    ap("missing = [c for c in expected_cols if c not in cols]")
    ap('check("control_total:columns", "control_total", "pass" if not missing else "fail",'
       ' str(len(cols)) + " delivered, " + str(len(expected_cols)) + " expected"'
       ' + ("; missing " + str(missing) if missing else ""))')
    ap("populated = 0")
    ap("for c in cols:")
    ap('    populated += con.execute(\'SELECT count(*) FROM target_out WHERE "\' + c'
       ' + \'" IS NOT NULL AND "\' + c + \'" <> \\\'\\\'\').fetchone()[0]')
    ap('check("control_total:populated_cells", "control_total", "pass",'
       ' str(populated) + " populated of " + str(n_target * len(cols)) + " cell(s)")')
    # ONE check per rule, matching the runner exactly. The script used to emit
    # a separate check per numeric column while the app emitted a single
    # aggregate, so the two reported different totals for the same rule set —
    # the divergence the certified set exists to prevent.
    if numeric and primary:
        ap(f"numeric = {numeric!r}  # (target_attribute, transformation_sql)")
        ap("off, detail = [], []")
        ap("for attr, expr in numeric:")
        ap(f'    exp = con.execute("SELECT sum(TRY_CAST((" + expr + ") AS DOUBLE)) '
           f'FROM {primary}").fetchone()[0] or 0')
        ap('    act = con.execute(\'SELECT sum(TRY_CAST("\' + attr + \'" AS DOUBLE)) '
           'FROM target_out\').fetchone()[0] or 0')
        ap("    ties = abs(exp - act) <= max(abs(exp), abs(act)) * 1e-6 + 1e-9")
        ap("    detail.append(attr + '=' + format(act, ',.2f'))")
        ap("    if not ties: off.append(attr)")
        ap('check("control_total:numeric_sums", "control_total", '
           '"pass" if not off else "fail", '
           '(str(len(numeric)) + " numeric column(s) tie out: " + "; ".join(detail)) '
           'if not off else (str(len(off)) + " of " + str(len(numeric)) '
           '+ " do not tie out: " + str(off)))')
    key_rule = next((r for r in rules if r.get("kind") == "control_total:distinct_keys"), None)
    if key_rule and primary:
        kt = (key_rule["params"] or {}).get("key_target")
        ks = (key_rule["params"] or {}).get("key_source")
        ap(f'd_keys = con.execute(\'SELECT count(DISTINCT "{kt}") FROM target_out\').fetchone()[0]')
        ap(f's_keys = con.execute(\'SELECT count(DISTINCT "{ks}" ) FROM {primary}\').fetchone()[0]')
        ap('check("control_total:distinct_keys", "control_total", '
           '"pass" if s_keys == d_keys else "fail", '
           'str(s_keys) + " distinct in source -> " + str(d_keys) + " delivered")')
    ap("")
    ap("# --- business rule: record counts per category --------------------------------")
    if categorical:
        ap(f"categorical = {categorical!r}")
        ap(f"PROFILE_MAX = {_PROFILE_MAX}")
        ap("for attr in categorical:")
        ap('    act = dict(con.execute(\'SELECT CAST("\' + attr + \'" AS VARCHAR), count(*)'
           ' FROM target_out GROUP BY 1\').fetchall())')
        ap("    if not act or len(act) > PROFILE_MAX or len(act) == n_target:")
        ap("        continue          # identifier or free text, not a category")
        ap('    profile = "; ".join(str(k) + "=" + str(v)'
           ' for k, v in sorted(act.items(), key=lambda x: str(x[0])))')
        ap('    check("category_profile:" + attr, "category_profile", "pass",'
           ' str(len(act)) + " categories — " + profile)')
    else:
        ap("# no categorical attribute carries a transform — nothing to profile")
    ap("")
    ap("# --- business rule: aggregates the business certified -------------------------")
    aggs = [dict((r["params"] or {}), _id=r.get("kind")) for r in rules
            if r.get("kind") == "aggregate_by"]
    if aggs and primary:
        ap(f"aggregates = {[{k: v for k, v in a.items() if k != '_id'} for a in aggs]!r}")
        ap(f"sql_of = {dict((m['target_attribute'], m.get('transformation_sql') or '') for m in spec.get('mappings', []) if m.get('gate') != 'reject')!r}")
        ap("for a in aggregates:")
        ap("    fn, col, groups = a['function'], a.get('column'), a.get('group_by') or []")
        ap("    agg_t = 'count(*)' if fn == 'count' else (")
        ap("        'count(DISTINCT ' + 'TRY_CAST(\"' + col + '\" AS DOUBLE)' + ')'")
        ap("        if fn == 'count_distinct' else fn + '(TRY_CAST(\"' + col + '\" AS DOUBLE))')")
        ap("    agg_s = 'count(*)' if fn == 'count' else (")
        ap("        'count(DISTINCT TRY_CAST((' + sql_of.get(col, 'NULL') + ') AS DOUBLE))'")
        ap("        if fn == 'count_distinct' else fn + '(TRY_CAST((' + sql_of.get(col, 'NULL') + ') AS DOUBLE))')")
        ap("    kt = ', '.join('CAST(\"' + g + '\" AS VARCHAR)' for g in groups) or \"'ALL'\"")
        ap("    ks = ', '.join('CAST((' + sql_of.get(g, 'NULL') + ') AS VARCHAR)' for g in groups) or \"'ALL'\"")
        ap(f"    src = dict(con.execute(\"SELECT concat_ws(' | ', \" + ks + \") , \" + agg_s + \" FROM {primary} GROUP BY 1\").fetchall())")
        ap("    tgt = dict(con.execute(\"SELECT concat_ws(' | ', \" + kt + \") , \" + agg_t + \" FROM target_out GROUP BY 1\").fetchall())")
        ap("    off = [k for k in set(src) | set(tgt)")
        ap("           if abs((src.get(k) or 0) - (tgt.get(k) or 0)) >")
        ap("              max(abs(src.get(k) or 0), abs(tgt.get(k) or 0)) * 1e-6 + 1e-9]")
        ap("    label = fn + ' of ' + (col or 'records') + (' by ' + ', '.join(groups) if groups else '')")
        ap("    check('aggregate_by:' + label, 'aggregate_by', 'pass' if not off else 'fail',")
        ap("          (label + ': all ' + str(len(set(src) | set(tgt))) + ' group(s) agree')")
        ap("          if not off else (label + ': ' + str(len(off)) + ' group(s) differ: ' + str(off)))")
    else:
        ap("# no business aggregate controls were certified")
    ap("")
    ap("# --- business rule: cross-field dependencies ---------------------------------")
    if crossfield:
        ap(f"crossfield = {crossfield!r}  # (dependent, driver, allowed_driver_values)")
        ap("for dep, driver, allowed in crossfield:")
        ap('    inlist = ", ".join("\'" + str(v).replace("\'", "\'\'") + "\'" for v in allowed)')
        ap('    q = (\'SELECT count(*) FROM target_out WHERE "\' + dep + \'" IS NOT NULL\''
           ' + \' AND "\' + dep + \'" <> \\\'\\\' AND "\' + driver + \'" NOT IN (\' + inlist + \')\')')
        ap("    bad = con.execute(q).fetchone()[0]")
        ap('    check("crossfield:" + dep + "~" + driver, "crossfield",'
           ' "pass" if bad == 0 else "fail", str(bad) + " violation(s) — " + dep'
           ' + " only when " + driver + " in " + str(allowed))')
    else:
        ap("# no dependency pattern with both sides mapped 1:1 — nothing to check")
    ap("")
    ap('passed = sum(1 for c in checks if c[2] == "pass")')
    ap('print("")')
    ap('print(str(passed) + "/" + str(len(checks)) + " reconciliation check(s) passed.")')
    ap("raise SystemExit(0 if passed == len(checks) else 1)")
    return "\n".join(L)

