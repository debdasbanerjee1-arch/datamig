"""Transformation workspace (tab 4) — additive.

Turns a certified MappingSpec into an executable ETL and runs it against the
source file(s). Everything here is derived from the spec the mapping workspace
(tab 3) already produced, so tab 4's output is guaranteed consistent with what
was certified: the same DuckDB SQL dialect the mapping agent emitted
(strptime / TRY_CAST / NULLIF / CASE) is executed verbatim, not re-implemented.

Nothing in this module touches Flow A / Flow B state — it only reads a spec plus
the current input files and returns a target dataset.
"""
from __future__ import annotations

import io
from pathlib import Path

import duckdb

# non-migrating gate: rows the mapping workspace excluded from load
REJECT_GATE = "reject"


# ---------------------------------------------------------------------------
# workset — rebuild the exact relation the spec was written against
# ---------------------------------------------------------------------------
def _q(name: str) -> str:
    """Quote a SQL identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def _read_csv(alias: str) -> str:
    return f"read_csv_auto(?, all_varchar=true, header=true)"


def resolve_source_paths(spec: dict, file_lookup) -> dict[str, str]:
    """Map every source_table named in the spec to a real file path.

    file_lookup(table_name) -> path|None. Tables come from spec.source_tables
    (e.g. 'EFAS0042', 'ESCH0009'); a mapping spec built from a single file has
    just one entry and no join_plan.
    """
    tables = spec.get("source_tables") or []
    if not tables:
        st = spec.get("source_table")
        if st and st != "__workset":
            tables = [st]
    paths: dict[str, str] = {}
    for t in tables:
        p = file_lookup(t)
        if p:
            paths[t] = p
    return paths


def build_workset_sql(spec: dict, paths: dict[str, str]) -> tuple[str, list[str]]:
    """Return (sql_defining_the_workset_relation, ordered_param_paths).

    Single file  -> SELECT * FROM read_csv_auto(<file>)
    Joined files -> primary LEFT JOIN each joined file per spec.join_plan,
                    keeping the primary file's row grain.

    This MUST reproduce engine/composite.build_workset exactly, because the
    spec's transformation_sql was written against the columns that view
    exposes. Three things therefore have to match, and previously did not:

      * join keys are compared TRIMMED — COBOL fields are space-padded, so a
        raw `=` silently produced zero matches and every joined column came
        back NULL;
      * a column name that clashes with one already taken is exposed as
        "{table}_{column}", NOT dropped — the spec may reference that exact
        renamed column, and dropping it made the transform fail with a
        missing-column error;
      * joins chain — an entry's `to_table` may be a previously joined file,
        not the primary, so each table needs its own alias.
    """
    tables = list(paths.keys())
    join_plan = [j for j in (spec.get("join_plan") or []) if j.get("table") in paths]

    if len(tables) == 1 or not join_plan:
        t = tables[0]
        return f"SELECT * FROM {_read_csv(t)}", [paths[t]]

    # the primary is the join target that is never itself joined IN
    joined_in = {j.get("table") for j in join_plan}
    primary = next((j.get("to_table") for j in join_plan
                    if j.get("to_table") not in joined_in and j.get("to_table") in paths),
                   None)
    if primary not in paths:
        primary = tables[0]

    alias = {primary: "p"}
    params: list[str] = [paths[primary]]
    taken: set[str] = set()
    select_cols: list[str] = []

    for c in _csv_columns(paths[primary]):      # the primary keeps its names
        taken.add(c)
        select_cols.append(f"p.{_q(c)} AS {_q(c)}")

    joins: list[str] = []
    for i, j in enumerate(join_plan):
        jt = j.get("table")
        a = f"j{i}"
        alias[jt] = a
        left_tbl = j.get("to_table")
        left_alias = alias.get(left_tbl, "p")   # chained joins resolve here
        left_key, right_key = j.get("to_column"), j.get("on")
        joins.append(
            f"LEFT JOIN {_read_csv(jt)} AS {a} "
            f"ON trim({left_alias}.{_q(left_key)}) = trim({a}.{_q(right_key)})"
        )
        params.append(paths[jt])
        for c in _csv_columns(paths[jt]):
            out = c if c not in taken else f"{jt}_{c}"
            taken.add(out)
            select_cols.append(f"{a}.{_q(c)} AS {_q(out)}")

    sql = (
        "SELECT " + ", ".join(select_cols) + "\n"
        f"FROM {_read_csv(primary)} AS p\n" + "\n".join(joins)
    )
    return sql, params


def _csv_columns(path: str) -> list[str]:
    con = duckdb.connect(":memory:")
    try:
        rel = con.execute(
            "SELECT * FROM read_csv_auto(?, all_varchar=true, header=true) LIMIT 0",
            [path],
        )
        return [d[0] for d in rel.description]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# SELECT — the certified column list
# ---------------------------------------------------------------------------
def build_select(spec: dict, migrated_at: str | None = None) -> list[tuple[str, str]]:
    """Ordered (target_attribute, sql_expression) for every migrating column,
    plus system-defaulted targets. Rejected mappings are skipped.

    migrated_at, when given, is injected as a plain string literal rather than
    CURRENT_TIMESTAMP. DuckDB's now()/CURRENT_TIMESTAMP is TIMESTAMP WITH TIME
    ZONE, which pulls in Python's pytz on materialisation; a text literal keeps
    the transform runnable on any interpreter without that optional dependency.
    """
    cols: list[tuple[str, str]] = []
    for m in spec.get("mappings", []):
        if m.get("gate") == REJECT_GATE:
            continue
        expr = (m.get("transformation_sql") or "").strip() or "NULL"
        cols.append((m["target_attribute"], expr))
    # unmapped targets are defaulted at load; surface them so the target shape
    # matches the dictionary. source_system / migrated_at get real defaults.
    for u in spec.get("unmapped_target", []):
        attr = u["attribute"]
        if attr == "source_system":
            cols.append((attr, "'"+ (spec.get("source_tables") or ["SOURCE"])[0] + "'"))
        elif attr == "migrated_at":
            if migrated_at:
                cols.append((attr, "'" + migrated_at.replace("'", "''") + "'"))
            else:
                cols.append((attr, "strftime(current_localtimestamp(), '%Y-%m-%d %H:%M:%S')"))
        else:
            cols.append((attr, "NULL"))
    return cols


def build_full_sql(spec: dict, paths: dict[str, str],
                   migrated_at: str | None = None) -> tuple[str, list[str]]:
    workset_sql, params = build_workset_sql(spec, paths)
    select = build_select(spec, migrated_at)
    lines = [f"  {expr} AS {_q(attr)}" for attr, expr in select]
    sql = (
        "SELECT\n" + ",\n".join(lines) + "\n"
        "FROM (\n" + _indent(workset_sql) + "\n) AS src"
    )
    return sql, params


def _text_fetch_sql(inner_sql: str, columns: list[str]) -> str:
    """Wrap a query so every column comes back as VARCHAR.

    DuckDB can materialise DATE / TIMESTAMP results through a code path that
    imports pytz; casting to text at the boundary keeps values clean
    (dates as 'YYYY-MM-DD') and removes that optional dependency entirely.
    Booleans and numbers survive the round-trip as their string forms, which
    is exactly what the CSV export and the grid render anyway.
    """
    casts = ", ".join(f"CAST({_q(c)} AS VARCHAR) AS {_q(c)}" for c in columns)
    return f"SELECT {casts}\nFROM (\n{_indent(inner_sql)}\n) AS materialised"


def _indent(text: str, n: int = 2) -> str:
    pad = " " * n
    return "\n".join(pad + ln for ln in text.splitlines())


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
def run_transform(spec: dict, paths: dict[str, str], limit: int | None = None):
    """Execute the certified transform. Returns (columns, rows, csv_text, stats).

    The result is fetched with every column cast to VARCHAR so DuckDB never
    materialises DATE/TIMESTAMP values through its pytz-dependent path — the
    transform runs whether or not pytz is installed. Dates come back as
    'YYYY-MM-DD', which is what the CSV and grid display anyway.
    """
    from datetime import datetime

    migrated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inner_sql, params = build_full_sql(spec, paths, migrated_at)
    columns = [attr for attr, _ in build_select(spec, migrated_at)]
    fetch_sql = _text_fetch_sql(inner_sql, columns)

    con = duckdb.connect(":memory:")
    try:
        rel = con.execute(fetch_sql, params)
        columns = [d[0] for d in rel.description]
        all_rows = rel.fetchall()
        total = len(all_rows)
        rows = all_rows[:limit] if limit else all_rows
        # CSV of the full result (not the preview slice)
        buf = io.StringIO()
        import csv as _csv

        w = _csv.writer(buf)
        w.writerow(columns)
        for r in all_rows:
            w.writerow(["" if v is None else v for v in r])
        csv_text = buf.getvalue()
    finally:
        con.close()

    # cell values -> JSON-safe strings, preserving a null marker for the grid
    def cell(v):
        return None if v is None else (v if isinstance(v, (int, float, bool)) else str(v))

    stats = {
        "row_count": total,
        "column_count": len(columns),
        "returned": len(rows),
        "migrating_columns": len([m for m in spec.get("mappings", []) if m.get("gate") != REJECT_GATE]),
        "defaulted_columns": len(spec.get("unmapped_target", [])),
    }
    return columns, [[cell(v) for v in r] for r in rows], csv_text, stats


# ---------------------------------------------------------------------------
# code generation — a standalone, readable Python/Pandas + DuckDB ETL script
# ---------------------------------------------------------------------------
def generate_python(spec: dict, filenames: dict[str, str],
                    paths: dict[str, str] | None = None) -> str:
    """Produce a self-contained ETL script that reproduces run_transform.

    filenames maps source_table -> the on-disk file name shown in the script.
    paths maps source_table -> a real readable path used only to introspect
    columns for the join workset (falls back to filenames when omitted). The
    script is what an engineer would hand-write: pandas for I/O, DuckDB for the
    certified SQL so the transform semantics are identical to this runtime.
    """
    paths = paths or filenames
    target = spec.get("target_table", "target")
    tables = spec.get("source_tables") or (
        [spec["source_table"]] if spec.get("source_table") and spec["source_table"] != "__workset" else []
    )
    join_plan = spec.get("join_plan") or []
    # a fixed migration timestamp string keeps the generated script free of
    # DuckDB's timezone-aware CURRENT_TIMESTAMP (which needs the optional pytz)
    from datetime import datetime

    migrated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    select = build_select(spec, migrated_at)

    L: list[str] = []
    ap = L.append
    ap('"""')
    ap(f"ETL — {tables and ' + '.join(tables) or 'source'}  ->  {target}")
    ap("")
    ap("Auto-generated from the certified mapping specification.")
    ap("Reads the source file(s) with pandas, then applies the certified")
    ap("transformation with DuckDB so the SQL semantics match the mapping")
    ap("workspace exactly (strptime / TRY_CAST / CASE / NULLIF).")
    ap('"""')
    ap("import pandas as pd")
    ap("import duckdb")
    ap("")
    ap("# --- 1. load source file(s) ---------------------------------------")
    for t in tables:
        fn = filenames.get(t, f"{t}.csv")
        ap(f'{t.lower()} = pd.read_csv("{fn}", dtype=str, keep_default_na=False)')
    ap("")
    ap("con = duckdb.connect(\":memory:\")")
    for t in tables:
        ap(f'con.register("{t}", {t.lower()})')
    ap("")

    # workset
    if len(tables) > 1 and join_plan:
        ap("# --- 2. rebuild the joined workset (keeps primary row grain) ------")
        primary = None
        for j in join_plan:
            primary = j.get("to_table") or primary
        primary = primary or tables[0]
        # build the collision-safe workset SQL the same way the runtime does,
        # then rewrite the positional read_csv_auto(?) calls back to the
        # registered table names so the script reads naturally.
        workset_sql, _ = build_workset_sql(
            spec, {t: paths.get(t, filenames.get(t, f"{t}.csv")) for t in tables}
        )
        ordered = [primary] + [
            j.get("table") for j in join_plan if j.get("table") in tables
        ]
        readable = workset_sql
        for t in ordered:
            readable = readable.replace(
                "read_csv_auto(?, all_varchar=true, header=true)", t, 1
            )
        ap('workset_sql = """')
        for ln in readable.splitlines():
            ap(ln)
        ap('"""')
        ap('con.execute("CREATE OR REPLACE TEMP VIEW workset AS " + workset_sql)')
        ap("")
        src_rel = "workset"
    else:
        src_rel = tables[0] if tables else "src"

    ap("# --- 3. certified column transforms -------------------------------")
    # The SQL is emitted as a RAW triple-quoted literal. Embedding it in a
    # normal string meant Python re-interpreted any backslash escape it
    # recognised: a certified expression containing '\t' reached DuckDB as a
    # literal tab, so the handed-over script executed DIFFERENT SQL from the one
    # the workspace validated — silently. A script that is not a faithful copy
    # of the certified spec cannot back a reproducibility claim.
    body = "SELECT\n" + ",\n".join(
        f'  {expr} AS "{attr}"' for attr, expr in select) + f"\nFROM {src_rel}\n"
    if '"""' in body:
        # fall back to an escaped literal rather than emit broken Python
        ap(f"transform_sql = {body!r}")
    else:
        ap('transform_sql = r"""')
        for line in body.rstrip("\n").splitlines():
            ap(line)
        ap('"""')
    ap("")
    ap("# --- 4. execute + export ------------------------------------------")
    ap("# Export straight from DuckDB (COPY) so DATE/TIMESTAMP columns never")
    ap("# round-trip through pandas — the transform runs without optional")
    ap("# timezone packages, and dates are written as YYYY-MM-DD.")
    ap(f'con.execute(f"""COPY ({{transform_sql}}) TO \'{target}.csv\' '
       f'(HEADER, DELIMITER \',\')""")')
    ap(f'n = con.execute(f"SELECT count(*) FROM ({{transform_sql}})").fetchone()[0]')
    ap(f'print(f"Wrote {{n}} rows to {target}.csv")')
    ap("")
    ap("# Prefer a DataFrame? Read the CSV back (all-text, pytz-free):")
    ap(f'# {target}_df = pd.read_csv("{target}.csv", dtype=str, keep_default_na=False)')
    return "\n".join(L)
