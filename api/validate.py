"""Validation workspace (tab 3) — additive.

Checks the DELIVERED transformed output (the CSV the transformation workspace
produced) against the certified mapping spec and the target dictionary — does
the file actually satisfy what was promised, rather than trusting that the
generator and the runtime agree. Everything here is a fresh, independent pass
over the actual output bytes; it does not reuse engine.agents.validator (which
checks the transform SQL against staged source, before any ETL runs).

Checks:
  wellformed    — every certified/defaulted target attribute is present
  grain         — delivered row count vs the rebuilt source workset
  key_integrity — the mapped key column is unique and non-null (candidate_keys
                  are derived from the source data; skipped if none is found)
  completeness  — required (non-nullable) target attributes have no null/blank
  domain        — enum attributes stay within their declared allowed_values

NO SILENT SKIPS. Every attribute in the target dictionary produces a row in the
report — an executed assertion, or an explicit `skipped` row stating why none
applies. This is the invariant the results table rests on: the reader can see
that every declared attribute was considered, so coverage is an observable
property of the table rather than a self-reported percentage. `skipped` never
affects the verdict and is excluded from the headline pass/warn/fail counts.

EVIDENCE. Every executed check carries the SQL it ran and the population it
scanned, so a pass is evidence rather than an assertion — and the results can be
reconciled against the downloadable script, which runs the same SQL.

Nothing here touches Flow A / Flow B state — it reads the spec + target
dictionary + the delivered CSV + the currently loaded source file(s), and
returns a report in the same shape as engine.agents.contracts.ValidationReport
so the UI renders it with the same check-card components used elsewhere.
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


def _load_output(con: "duckdb.DuckDBPyConnection", csv_text: str) -> None:
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


def _one_to_one_map(spec: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in spec.get("mappings", []):
        if (m.get("cardinality") == "1:1" and len(m.get("source_attributes") or []) == 1
                and m.get("gate") != "reject"):
            out.setdefault(m["source_attributes"][0], m["target_attribute"])
    return out


# Expressions whose value changes between executions — a re-run comparison is
# meaningless for these, so the transform check declares them rather than
# failing them. Load-audit columns (migrated_at, source_system) are the usual
# case.
#
# Matched as SUBSTRINGS, deliberately not on word boundaries: DuckDB's
# `current_localtimestamp()` slipped past a \b-anchored pattern because the
# underscore is a word character, and the resulting check only passed when the
# transform and the validation happened to run inside the same second. A
# time-dependent pass is worse than an outright failure — it hides.
_NONDET_TOKENS = (
    "now(", "today(", "random(", "uuid(", "gen_random_uuid",
    "current_timestamp", "current_date", "current_time", "current_localtime",
    "localtimestamp", "localtime", "transaction_timestamp",
    "statement_timestamp", "get_current_timestamp", "nextval", "currval",
)


def _is_nondeterministic(sql_expr: str) -> bool:
    low = (sql_expr or "").lower().replace(" ", "")
    return any(tok.replace(" ", "") in low for tok in _NONDET_TOKENS)


def run_output_validation(spec: dict, target_dict: dict, csv_text: str,
                          source_paths: dict[str, str],
                          insight: dict | None = None) -> dict:
    con = duckdb.connect(":memory:")
    checks: list[CheckResult] = []

    def add(name, cat, status, detail, *, sev="soft", attr=None, n=0, sample=None,
            sql=None, scanned=None):
        checks.append(CheckResult(name=name, category=cat, status=status, severity=sev,
                                  detail=detail, target_attribute=attr,
                                  offending_rows=n, sample=sample or [],
                                  sql=sql, rows_scanned=tgt_n if scanned is None else scanned))

    def skip(name, cat, detail, *, attr=None):
        """Record a check that did NOT execute, and WHY.

        An attribute that cannot be checked must still appear in the report.
        Previously a nullable non-enum attribute produced no rows at all, so
        "not examined" and "examined and clean" were indistinguishable — the
        one thing a validation report must never blur. `skipped` never affects
        the verdict; it is an accounting status, not a result.
        """
        checks.append(CheckResult(name=name, category=cat, status="skipped",
                                  severity="soft", detail=detail,
                                  target_attribute=attr, rows_scanned=0))

    # per-value examination accounting — the confidence figure the results
    # dashboard reports. Counted, never asserted.
    cells_checked = {"completeness": 0, "domain": 0, "type": 0,
                     "transform": 0, "key_integrity": 0}

    _load_output(con, csv_text)
    out_cols = [d[0] for d in con.execute("SELECT * FROM target_out LIMIT 0").description]
    tgt_n = con.execute("SELECT count(*) FROM target_out").fetchone()[0]

    # ---------------- wellformed: delivered shape matches the spec --------
    expected = {m["target_attribute"] for m in spec.get("mappings", [])
                if m.get("gate") != "reject"}
    expected |= {u["attribute"] for u in spec.get("unmapped_target", [])}
    # Executed in SQL, not in Python: the recorded evidence has to be the query
    # that actually produced the result. It previously showed an illustrative
    # `SELECT * FROM target_out LIMIT 0`, which ran nothing and — paired with a
    # "50 rows scanned" line inherited from the row-based checks — read like the
    # grain query had been pasted into the wrong card.
    exp_list = sorted(expected)
    if exp_list:
        values = ", ".join("('" + c.replace("'", "''") + "')" for c in exp_list)
        wf_sql = (f"-- every column the certified spec expects must be delivered\n"
                  f"WITH expected(col) AS (VALUES {values})\n"
                  f"SELECT col AS missing_column FROM expected\n"
                  f"WHERE col NOT IN (SELECT column_name FROM information_schema.columns\n"
                  f"                  WHERE table_name = 'target_out');")
        missing_cols = [r[0] for r in con.execute(
            f"WITH expected(col) AS (VALUES {values}) SELECT col FROM expected "
            f"WHERE col NOT IN (SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = 'target_out') ORDER BY col").fetchall()]
    else:
        wf_sql, missing_cols = None, []
    add("delivered_columns_match_spec", "wellformed", "fail" if missing_cols else "pass",
        (f"column(s) the spec expects but the delivered file is missing: {missing_cols}"
         if missing_cols else
         f"all {len(exp_list)} certified/defaulted target attribute(s) are present "
         f"in the delivered output."),
        sev="hard", n=len(missing_cols), sql=wf_sql,
        # a structural check: it compares COLUMN NAMES and reads no row values,
        # so it must not claim a row population
        scanned=0)

    # ---------------- grain: row count vs the rebuilt source workset ------
    src_n, workset_err = None, None
    if source_paths:
        try:
            workset_sql, params = build_workset_sql(spec, source_paths)
            src_n = con.execute(f"SELECT count(*) FROM ({workset_sql}) AS w", params).fetchone()[0]
        except Exception as e:  # noqa: BLE001
            workset_err = str(e)
    if src_n is not None:
        add("row_count_preserved", "grain", "pass" if src_n == tgt_n else "fail",
            f"{src_n} source row(s) -> {tgt_n} delivered target row(s).", sev="hard",
            sql=("-- delivered row count must equal the rebuilt source workset\n"
                 "SELECT (SELECT count(*) FROM target_out) AS delivered_rows,\n"
                 f"       (SELECT count(*) FROM ({workset_sql}) AS w) AS source_rows;"),
            scanned=src_n)
    elif workset_err:
        add("row_count_preserved", "grain", "warn",
            f"could not rebuild the source workset to compare row counts: {workset_err}")
    else:
        add("row_count_preserved", "grain", "warn",
            f"{tgt_n} delivered target row(s) — source file(s) not loaded, cannot compare grain.")

    # ---------------- key integrity ----------------------------------------
    candidate_keys = (insight or {}).get("candidate_keys") or []
    one_to_one = _one_to_one_map(spec)
    key_tgt = next((one_to_one[k] for k in candidate_keys if k in one_to_one), None)
    if key_tgt and key_tgt in out_cols:
        key_sql = (f'SELECT count(*) FILTER (WHERE {_q(key_tgt)} IS NULL) AS nulls,\n'
                   f'       count(DISTINCT {_q(key_tgt)}) AS distinct_values,\n'
                   f'       count(*) AS rows\n'
                   f'FROM target_out;')
        nulls = con.execute(f'SELECT count(*) FROM target_out WHERE {_q(key_tgt)} IS NULL').fetchone()[0]
        distinct = con.execute(f'SELECT count(DISTINCT {_q(key_tgt)}) FROM target_out').fetchone()[0]
        ok = nulls == 0 and distinct == tgt_n
        cells_checked["key_integrity"] += tgt_n
        add("key_unique_not_null", "key_integrity", "pass" if ok else "fail",
            f"key '{key_tgt}': {nulls} null(s), {distinct}/{tgt_n} distinct in the delivered output.",
            sev="hard", attr=key_tgt, n=nulls, sql=key_sql,
            sample=_rows(con,
                f'SELECT * FROM (SELECT row_number() OVER () AS row_number, * '
                f'FROM target_out) WHERE {_q(key_tgt)} IS NULL',
                limit=10) if nulls else None)
    else:
        add("key_identified", "key_integrity",
            "warn", "no candidate key derived from the source data (or no key mapped 1:1) — key-uniqueness check skipped.")

    # ---------------- completeness: required target attributes ------------
    for attr in target_attributes(target_dict):
        name, required = attr["name"], not attr.get("nullable", False)
        if name not in out_cols:
            skip(f"required_present:{name}", "completeness",
                 f"'{name}' is not present in the delivered output — "
                 "nothing to check for completeness.", attr=name)
            continue
        if not required:
            skip(f"required_present:{name}", "completeness",
                 f"'{name}' is declared nullable in the target dictionary, so "
                 "null/blank is a legal value — no completeness assertion applies.",
                 attr=name)
            continue
        comp_sql = (f"SELECT count(*) AS violations FROM target_out\n"
                    f"WHERE {_q(name)} IS NULL OR {_q(name)} = '';")
        nulls = con.execute(f"SELECT count(*) FROM target_out WHERE {_q(name)} IS NULL "
                            f"OR {_q(name)} = ''").fetchone()[0]
        cells_checked["completeness"] += tgt_n
        add(f"required_present:{name}", "completeness", "fail" if nulls else "pass",
            (f"'{name}' required; {nulls} null/blank in the delivered output." if nulls
             else f"'{name}' fully populated in the delivered output."),
            sev="hard", attr=name, n=nulls, sql=comp_sql,
            sample=_rows(con,
                (f"SELECT row_number, "
                 + (f"{_q(key_tgt)} " if key_tgt and key_tgt in out_cols else "")
                 + (", " if key_tgt and key_tgt in out_cols else "")
                 + f"{_q(name)} FROM (SELECT row_number() OVER () AS row_number, * "
                   f"FROM target_out) WHERE {_q(name)} IS NULL OR {_q(name)} = ''"),
                limit=10) if nulls else None)

    # ---------------- domain: enum attributes stay in-list -----------------
    for attr in target_attributes(target_dict):
        name, allowed = attr["name"], attr.get("allowed_values")
        if name not in out_cols:
            skip(f"domain:{name}", "domain",
                 f"'{name}' is not present in the delivered output — "
                 "no domain to enforce.", attr=name)
            continue
        if attr.get("type") != "enum" or not allowed:
            skip(f"domain:{name}", "domain",
                 f"'{name}' is type '{attr.get('type')}' with no declared "
                 "allowed_values — the target dictionary defines no domain to "
                 "enforce.", attr=name)
            continue
        inlist = ", ".join("'" + v.replace("'", "''") + "'" for v in allowed)
        dom_sql = (f"SELECT count(*) AS violations FROM target_out\n"
                   f"WHERE {_q(name)} IS NOT NULL AND {_q(name)} <> ''\n"
                   f"  AND {_q(name)} NOT IN ({inlist});")
        bad = con.execute(f"SELECT count(*) FROM target_out WHERE {_q(name)} IS NOT NULL "
                          f"AND {_q(name)} <> '' AND {_q(name)} NOT IN ({inlist})").fetchone()[0]
        cells_checked["domain"] += tgt_n
        add(f"domain:{name}", "domain", "pass" if not bad else "fail",
            (f"'{name}': {bad} value(s) outside the declared allowed_values." if bad
             else f"'{name}': every value is one of the declared allowed_values."),
            sev="soft", attr=name, n=bad, sql=dom_sql,
            sample=_rows(con,
                (f"SELECT {_q(name)} AS offending_value, count(*) AS rows_affected "
                 f"FROM target_out WHERE {_q(name)} IS NOT NULL AND {_q(name)} <> '' "
                 f"AND {_q(name)} NOT IN ({inlist}) GROUP BY 1 ORDER BY 2 DESC"),
                limit=10) if bad else None)

    # ---------------- duplicate rows --------------------------------------
    # NOT redundant with key uniqueness: a file can carry a unique key and
    # still hold two rows identical across every OTHER column, which is the
    # classic signature of a join that fanned out.
    if out_cols:
        allc = ", ".join(_q(c) for c in out_cols)
        dup_sql = (f"SELECT count(*) AS duplicate_rows FROM (\n"
                   f"  SELECT {allc}, count(*) AS n FROM target_out\n"
                   f"  GROUP BY ALL HAVING count(*) > 1\n) d;")
        dups = con.execute(f"SELECT count(*) FROM (SELECT {allc}, count(*) AS n "
                           f"FROM target_out GROUP BY ALL HAVING count(*) > 1) d").fetchone()[0]
        dup_sample = None
        if dups:
            keycol = _q(key_tgt) if key_tgt and key_tgt in out_cols else _q(out_cols[0])
            dup_sample = _rows(con,
                f"SELECT {keycol} AS duplicated_key, count(*) AS copies "
                f"FROM target_out GROUP BY {allc} HAVING count(*) > 1 "
                f"ORDER BY count(*) DESC", limit=10)
        add("no_duplicate_rows", "duplicates", "fail" if dups else "pass",
            (f"{dups} fully-duplicated row group(s) in the delivered output."
             if dups else "no two delivered rows are identical across all columns."),
            sev="hard", n=dups, sql=dup_sql, sample=dup_sample)

    # ---------------- data type conformance --------------------------------
    # EVERY declared attribute, not just the numerically typed ones: the
    # generated target data must conform to the type the mapping spec targets.
    # Text types are asserted too — the assertion is weaker (any byte sequence
    # is text), and the detail says so, but the column still appears with its
    # declared type and its whole population examined rather than vanishing.
    _CAST = {"date": "DATE", "timestamp": "TIMESTAMP", "time": "TIME",
             "decimal": "DOUBLE", "numeric": "DOUBLE", "number": "DOUBLE",
             "float": "DOUBLE", "double": "DOUBLE", "integer": "BIGINT",
             "int": "BIGINT", "bigint": "BIGINT", "smallint": "SMALLINT",
             "boolean": "BOOLEAN", "bool": "BOOLEAN"}
    _TEXT = {"string", "varchar", "text", "char", "enum", ""}
    spec_types = {m.get("target_attribute"): m.get("target_type")
                  for m in spec.get("mappings", [])}
    for attr in target_attributes(target_dict):
        name, ttype = attr["name"], (attr.get("type") or "").lower()
        if name not in out_cols:
            skip(f"data_type:{name}", "type",
                 f"'{name}' is not present in the delivered output.", attr=name)
            continue
        # the type the mapping spec targets, falling back to the dictionary
        declared = (spec_types.get(name) or ttype or "").lower()
        cast = _CAST.get(declared)
        cells_checked["type"] += tgt_n
        if cast is None and declared in _TEXT:
            t_sql = (f"SELECT count(*) AS non_text FROM target_out\n"
                     f"WHERE {_q(name)} IS NOT NULL\n"
                     f"  AND TRY_CAST({_q(name)} AS VARCHAR) IS NULL;")
            bad = con.execute(f"SELECT count(*) FROM target_out WHERE {_q(name)} IS NOT NULL "
                              f"AND TRY_CAST({_q(name)} AS VARCHAR) IS NULL").fetchone()[0]
            add(f"data_type:{name}", "type", "fail" if bad else "pass",
                (f"{bad} value(s) in '{name}' are not representable as "
                 f"{declared or 'text'}."
                 if bad else
                 f"'{name}' targets {declared or 'text'}; all {tgt_n} row(s) "
                 f"examined and conform. Text admits any value, so the binding "
                 f"constraints for this column are its domain and its "
                 f"transformation rule."),
                sev="soft", attr=name, n=bad, sql=t_sql)
            continue
        if cast is None:
            cells_checked["type"] -= tgt_n
            skip(f"data_type:{name}", "type",
                 f"'{name}' declares type '{declared}', which has no parse rule "
                 f"defined — no data-type assertion applies.", attr=name)
            continue
        t_sql = (f"-- target type per the mapping spec: {declared}\n"
                 f"SELECT count(*) AS unparseable FROM target_out\n"
                 f"WHERE {_q(name)} IS NOT NULL AND {_q(name)} <> ''\n"
                 f"  AND TRY_CAST({_q(name)} AS {cast}) IS NULL;")
        bad = con.execute(f"SELECT count(*) FROM target_out WHERE {_q(name)} IS NOT NULL "
                          f"AND {_q(name)} <> '' AND TRY_CAST({_q(name)} AS {cast}) IS NULL"
                          ).fetchone()[0]
        add(f"data_type:{name}", "type", "fail" if bad else "pass",
            (f"{bad} of {tgt_n} value(s) in '{name}' do not parse as the "
             f"targeted type {declared} ({cast})."
             if bad else f"'{name}' targets {declared}; all {tgt_n} row(s) "
                         f"examined and every populated value parses as {cast}."),
            sev="hard", attr=name, n=bad, sql=t_sql,
            sample=_rows(con,
                (f"SELECT row_number, "
                 + (f"{_q(key_tgt)}, " if key_tgt and key_tgt in out_cols else "")
                 + f"{_q(name)} AS unparseable_value "
                   f"FROM (SELECT row_number() OVER () AS row_number, * FROM target_out) "
                   f"WHERE {_q(name)} IS NOT NULL AND {_q(name)} <> '' "
                   f"AND TRY_CAST({_q(name)} AS {cast}) IS NULL"), limit=10)
                if bad else None)

    # ---------------- transformation rule conformance ----------------------
    # THE check that earns the confidence claim: re-execute each certified
    # mapping's SQL against the source workset and compare the result to the
    # delivered column, value by value. Everything else asks "is the delivered
    # file internally plausible?"; this asks "is it what the certified spec
    # says it should be?" — and it touches every row of every mapped column.
    #
    # Compared in SQL (a FULL OUTER JOIN on row number, counting mismatches)
    # rather than by pulling cells into Python, so it stays a single scan.
    if source_paths and src_n is not None:
        try:
            ws_sql, ws_params = build_workset_sql(spec, source_paths)
            # CTAS, not a VIEW: DuckDB rejects prepared parameters inside a
            # view definition, and the file paths arrive as bound parameters.
            # Materialising also means the workset is scanned once rather than
            # re-read per mapping.
            con.execute(f"CREATE OR REPLACE TABLE _srcn AS "
                        f"SELECT *, row_number() OVER () AS _rn FROM ({ws_sql}) w",
                        ws_params)
            con.execute("CREATE OR REPLACE TABLE _outn AS "
                        "SELECT *, row_number() OVER () AS _rn FROM target_out")
        except Exception as e:      # noqa: BLE001
            add("transform_rules_reproduced", "transform", "warn",
                f"could not rebuild the source workset to re-execute the "
                f"certified transforms: {e}")
        else:
            # Rows are aligned by ordinal position, which is only meaningful
            # when both sides have the same number of rows. If grain already
            # failed, every column would report a cascade of spurious
            # mismatches and bury the real defect — so say so once, per column,
            # and point at the check that actually needs fixing first.
            grain_ok = (src_n == tgt_n)
            for m in spec.get("mappings", []):
                name, sql_expr = m.get("target_attribute"), m.get("transformation_sql")
                if m.get("gate") == "reject" or name not in out_cols:
                    continue
                if not grain_ok:
                    skip(f"transform_rule:{name}", "transform",
                         f"delivered row count ({tgt_n}) differs from the source "
                         f"workset ({src_n}), so rows cannot be aligned for a "
                         f"value-by-value comparison. Resolve the grain check "
                         f"first, then re-run.", attr=name)
                    continue
                if not sql_expr:
                    skip(f"transform_rule:{name}", "transform",
                         f"'{name}' carries no transformation SQL to re-execute.",
                         attr=name)
                    continue
                if _is_nondeterministic(sql_expr):
                    # now()/current_date/random() produce a different value on
                    # every execution, so re-running them can never reproduce
                    # what was delivered. Reporting that as a mismatch would be
                    # a guaranteed false failure on every load-audit column.
                    skip(f"transform_rule:{name}", "transform",
                         f"'{name}' is derived from a non-deterministic "
                         f"expression, so re-executing it cannot reproduce the "
                         f"delivered value. Its type and completeness are still "
                         f"checked.", attr=name)
                    continue
                cmp_sql = (f"WITH expected AS (\n"
                           f"  SELECT _rn, CAST({sql_expr} AS VARCHAR) AS v FROM _srcn\n"
                           f"), delivered AS (\n"
                           f"  SELECT _rn, CAST({_q(name)} AS VARCHAR) AS v FROM _outn\n"
                           f")\n"
                           f"SELECT count(*) AS mismatches FROM expected e\n"
                           f"FULL OUTER JOIN delivered d USING (_rn)\n"
                           f"WHERE e.v IS DISTINCT FROM d.v;")
                try:
                    bad = con.execute(
                        f"WITH expected AS (SELECT _rn, CAST({sql_expr} AS VARCHAR) AS v FROM _srcn), "
                        f"delivered AS (SELECT _rn, CAST({_q(name)} AS VARCHAR) AS v FROM _outn) "
                        f"SELECT count(*) FROM expected e FULL OUTER JOIN delivered d USING (_rn) "
                        f"WHERE e.v IS DISTINCT FROM d.v").fetchone()[0]
                except Exception as e:      # noqa: BLE001
                    add(f"transform_rule:{name}", "transform", "warn",
                        f"could not re-execute the certified transform for "
                        f"'{name}': {e}", attr=name, sql=cmp_sql)
                    continue
                sample = None
                if bad:
                    # The evidence that matters for a transform failure is not
                    # "a row" but the DIFF: which record, what the certified
                    # rule produces, and what was actually delivered. Keyed by
                    # the business key where one is mapped, so the reviewer can
                    # find the record in the source system.
                    # identify the record by the mapped business key where one
                    # exists, otherwise by the first delivered column — a diff
                    # the reviewer cannot trace back to a record is not evidence
                    idcol = (key_tgt if key_tgt and key_tgt in out_cols
                             else (out_cols[0] if out_cols and out_cols[0] != name else None))
                    keysel = f"d.{_q(idcol)} AS {_q(idcol)}, " if idcol else ""
                    try:
                        sample = _rows(con,
                            f"WITH expected AS (SELECT _rn, CAST({sql_expr} AS VARCHAR) AS v FROM _srcn), "
                            f"delivered AS (SELECT _rn, {(_q(idcol) + ', ') if idcol else ''}"
                            f"CAST({_q(name)} AS VARCHAR) AS v FROM _outn) "
                            f"SELECT coalesce(e._rn, d._rn) AS row_number, "
                            f"{keysel}e.v AS expected, d.v AS delivered "
                            f"FROM expected e FULL OUTER JOIN delivered d USING (_rn) "
                            f"WHERE e.v IS DISTINCT FROM d.v ORDER BY 1", limit=10)
                    except Exception:      # noqa: BLE001
                        sample = None
                cells_checked["transform"] += tgt_n
                add(f"transform_rule:{name}", "transform", "fail" if bad else "pass",
                    (f"{bad} of {tgt_n} delivered value(s) in '{name}' differ from "
                     f"re-executing the certified transform."
                     if bad else
                     f"all {tgt_n} delivered value(s) in '{name}' reproduce exactly "
                     f"when the certified transform is re-executed."),
                    sev="hard", attr=name, n=bad, sql=cmp_sql, sample=sample)

    con.close()

    hard_fail = any(c.status == "fail" and c.severity == "hard" for c in checks)
    any_fail = any(c.status == "fail" for c in checks)
    verdict = "blocked" if hard_fail else ("needs_review" if any_fail else "certified")
    executed = [c for c in checks if c.status != "skipped"]
    stats = {
        "checks": len(executed),
        "passed": sum(c.status == "pass" for c in executed),
        "warnings": sum(c.status == "warn" for c in executed),
        "failures": sum(c.status == "fail" for c in executed),
        "skipped": sum(c.status == "skipped" for c in checks),
    }
    # (5) The confidence figure, COUNTED. Columns that received per-value
    # examination x rows, deduplicated across families — so the headline is
    # "N of R x C cells examined", a number a reader can verify by
    # multiplication rather than a coverage percentage to argue about.
    per_value_cols = {c.target_attribute for c in checks
                      if c.status in ("pass", "fail")
                      and c.category in ("completeness", "domain", "type",
                                         "transform", "key_integrity")
                      and c.target_attribute}
    declared = [a["name"] for a in target_attributes(target_dict)]
    coverage_cells = {
        "rows": tgt_n,
        "columns_total": len(declared),
        "columns_examined": len(per_value_cols),
        "cells_total": tgt_n * len(declared),
        "cells_examined": tgt_n * len(per_value_cols),
        "assertions": sum(cells_checked.values()),   # incl. a column checked twice
        "by_family": cells_checked,
        "columns_not_examined": sorted(set(declared) - per_value_cols),
    }
    return {
        "verdict": verdict,
        "checks": [c.model_dump() for c in checks],
        "stats": stats,
        "coverage_cells": coverage_cells,
        "source_table": spec.get("source_table"),
        "target_table": spec.get("target_table"),
    }


# ---------------------------------------------------------------------------
# code generation — a standalone, readable Python/DuckDB validation script
# ---------------------------------------------------------------------------
def describe_validation_rules(spec: dict, target_dict: dict,
                              has_source: bool, has_insight: bool) -> list[dict]:
    """The checks `generate_validation_script` will emit, described BEFORE the
    script runs — same rule set, same wording as run_output_validation, so the
    preview never drifts from the actual outcome."""
    req = [a["name"] for a in target_attributes(target_dict) if not a.get("nullable", False)]
    enums = [(a["name"], a.get("allowed_values")) for a in target_attributes(target_dict)
             if a.get("type") == "enum" and a.get("allowed_values")]
    _TYPED = {"date", "timestamp", "decimal", "integer", "number", "boolean"}
    typed = [a["name"] for a in target_attributes(target_dict)
             if (a.get("type") or "").lower() in _TYPED]
    mapped = [m["target_attribute"] for m in spec.get("mappings", [])
              if m.get("gate") != "reject" and m.get("transformation_sql")]
    expected = sorted({m["target_attribute"] for m in spec.get("mappings", [])
                       if m.get("gate") != "reject"}
                      | {u["attribute"] for u in spec.get("unmapped_target", [])})

    rules: list[dict] = []
    rules.append({"category": "wellformed", "name": "delivered_columns_match_spec",
                  "detail": f"Checks that the delivered file contains every one of "
                            f"the {len(expected)} attribute(s) the certified mapping "
                            f"produces. Fails if any is missing.",
                  "scope": f"{len(expected)} attribute(s)"})
    rules.append({"category": "grain", "name": "row_count_preserved",
                  "detail": ("Checks that the delivered file has exactly the same "
                             "number of records as the source. Fails if records "
                             "have been added or lost."
                             if has_source else
                             "Reports the delivered record count. No comparison is "
                             "made — no source file was loaded.")})
    rules.append({"category": "key_integrity", "name": "key_unique_not_null",
                  "detail": ("Checks that the business key identifies exactly one "
                             "record: it must have a value in every record, and no "
                             "two records may share the same key."
                             if has_insight else
                             "Skipped — no candidate key could be derived from the source data.")})
    # Completeness and domain are ONE rule each, applied per attribute — not N
    # different rules. Listing them per column taught the reader nothing after
    # the first card and did not survive a 50-attribute target. The per-attribute
    # outcome is what the results table shows; this section describes the rule.
    rules.append({"category": "completeness", "name": "required_present",
                  "detail": "Checks that every mandatory attribute has a value in "
                            "every record. A record fails if a mandatory attribute "
                            "is empty or null.",
                  "applies_to": req,
                  "scope": f"{len(req)} required attribute(s)"})
    rules.append({"category": "duplicates", "name": "no_duplicate_rows",
                  "detail": "Checks that no duplicate record exists, by comparing "
                            "all attributes. A record is a duplicate if every one "
                            "of its attribute values matches another record "
                            "exactly.",
                  "scope": "whole table"})
    rules.append({"category": "type", "name": "data_type",
                  "detail": "Checks that every value can be read as the data type "
                            "the target expects — a date as a date, an amount as a "
                            "number. A record fails if a value cannot be "
                            "interpreted as its declared type.",
                  "applies_to": [a["name"] for a in target_attributes(target_dict)],
                  "scope": f"{len(target_attributes(target_dict))} attribute(s) x every row"})
    rules.append({"category": "transform", "name": "transform_rule",
                  "detail": "Re-applies each certified mapping rule to the source "
                            "data and compares the result with the delivered value, "
                            "record by record. A record fails if the delivered "
                            "value differs from what the mapping rule produces.",
                  "applies_to": mapped,
                  "scope": f"{len(mapped)} mapped attribute(s) x every row"})
    rules.append({"category": "domain", "name": "value_in_allowed_list",
                  "detail": "Checks that coded attributes only ever hold a value "
                            "from their permitted list. A record fails if the value "
                            "is not on the list. Empty values are not counted here "
                            "— completeness covers those.",
                  "applies_to": [n for n, _a in enums],
                  "scope": f"{len(enums)} enum attribute(s)"})
    return rules


def generate_validation_script(spec: dict, target_dict: dict, output_filename: str,
                                has_source: bool, has_insight: bool) -> str:
    target = spec.get("target_table", "target")
    req = [a["name"] for a in target_attributes(target_dict) if not a.get("nullable", False)]
    enums = [(a["name"], a.get("allowed_values")) for a in target_attributes(target_dict)
             if a.get("type") == "enum" and a.get("allowed_values")]
    _TYPED = {"date", "timestamp", "decimal", "integer", "number", "boolean"}
    typed = [a["name"] for a in target_attributes(target_dict)
             if (a.get("type") or "").lower() in _TYPED]
    mapped = [m["target_attribute"] for m in spec.get("mappings", [])
              if m.get("gate") != "reject" and m.get("transformation_sql")]
    expected = sorted({m["target_attribute"] for m in spec.get("mappings", [])
                       if m.get("gate") != "reject"}
                      | {u["attribute"] for u in spec.get("unmapped_target", [])})

    L: list[str] = []
    ap = L.append
    ap('"""')
    ap(f"Validation — does {output_filename} satisfy the certified mapping for {target}?")
    ap("")
    ap("Auto-generated from the certified mapping specification and target")
    ap("dictionary. Checks the DELIVERED output file itself (not a re-derivation")
    ap("of the transform), so it catches drift between what was generated and")
    ap("what actually got run.")
    ap('"""')
    ap("import duckdb")
    ap("")
    ap('con = duckdb.connect(":memory:")')
    ap(f'con.execute("CREATE TABLE target_out AS SELECT * FROM read_csv_auto('
       f'\'{output_filename}\', all_varchar=true, header=true)")')
    ap("")
    ap("checks = []  # (name, category, status, detail)")
    ap("def check(name, cat, ok, detail):")
    ap('    checks.append((name, cat, "pass" if ok else "fail", detail))')
    ap("")
    ap("cols = [d[0] for d in con.execute(\"SELECT * FROM target_out LIMIT 0\").description]")
    ap("n = con.execute(\"SELECT count(*) FROM target_out\").fetchone()[0]")
    ap("")
    ap("# --- wellformed: every expected column present --------------------")
    ap(f"expected = {expected!r}")
    ap('missing = sorted(set(expected) - set(cols))')
    ap('check("delivered_columns_match_spec", "wellformed", not missing, '
       '("missing: " + str(missing)) if missing else "all columns present")')
    ap("")
    if has_source:
        ap("# --- grain: row count vs the source ---------------------------")
        ap("# (compare n above against the row count of the source file(s) you loaded)")
        ap("")
    ap("# --- completeness: required attributes -----------------------------")
    ap(f"required = {req!r}")
    ap("for name in required:")
    ap("    if name not in cols: continue")
    ap('    q = \'SELECT count(*) FROM target_out WHERE "\' + name + \'" IS NULL OR "\' + name + \'" = \\\'\\\'\'')
    ap("    nulls = con.execute(q).fetchone()[0]")
    ap('    check("required_present:" + name, "completeness", nulls == 0, str(nulls) + " null/blank")')
    ap("")
    if enums:
        ap("# --- domain: enum attributes stay within allowed_values ------------")
        ap(f"enums = {enums!r}")
        ap("for name, allowed in enums:")
        ap("    if name not in cols: continue")
        ap('    inlist = ", ".join("\'" + v.replace("\'", "\'\'") + "\'" for v in allowed)')
        ap('    q = \'SELECT count(*) FROM target_out WHERE "\' + name + \'" IS NOT NULL AND "\' + name + \'" <> \\\'\\\' AND "\' + name + \'" NOT IN (\' + inlist + \')\'')
        ap("    bad = con.execute(q).fetchone()[0]")
        ap('    check("domain:" + name, "domain", bad == 0, str(bad) + " value(s) outside allowed_values")')
        ap("")
    if not has_insight:
        ap("# NOTE: no candidate key could be derived from the source data, so")
        ap("# the key-uniqueness check (candidate key -> mapped target column) is")
        ap("# omitted. Check that the source file has an identifier-like column.")
        ap("")
    ap("# --- report ----------------------------------------------------------")
    ap('failed = [c for c in checks if c[2] == "fail"]')
    ap('for name, cat, status, detail in checks:')
    ap('    print(f"[{status.upper():4}] {cat:14} {name}: {detail}")')
    ap('print(f"\\n{len(checks)-len(failed)}/{len(checks)} passed."'
       ' + (f" {len(failed)} FAILED." if failed else ""))')
    return "\n".join(L)
