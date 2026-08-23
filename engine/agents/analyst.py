"""Agent 1 — the pure data analyst.

No mainframe/domain knowledge. It looks only at schema + sample data and recovers
structure empirically:
  * per-column profiling (type, cardinality, frequency, sentinels)
  * candidate keys, constants, dead columns
  * conditional-population mining ("B is populated only when A in {..}")
  * co-population groups (columns that fill in together)

The deterministic layer produces the evidence and a baseline narrative; the LLM
(Azure OpenAI) rephrases it into analyst-style insight. Falls back to the
deterministic narrative when Azure isn't configured, so it runs today.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

from .contracts import (ColumnInsight, DependencyFinding, DqFinding,
                        PiiFinding, TableInsight)
from ..staging import Warehouse

# values that legacy systems use to mean "not set" — treated as not-populated
def _sql_lit(value) -> str:
    """A single-quoted SQL literal with embedded quotes doubled — values come
    from the DATA, so an apostrophe in a policyholder name or a scheme title
    would otherwise break the generated query."""
    return "'" + str(value).replace("'", "''") + "'"


SENTINELS = {
    "00000000", "99999999", "99991231", "0000-00-00", "9999-12-31",
    "N/A", "NA", "NULL", "NONE", "UNKNOWN", "-", ".", "?",
}
LOW_CARD = 15


# ---------------------------------------------------------------- profiling
def _populated(v: str | None) -> bool:
    if v is None:
        return False
    s = v.strip()
    return s != "" and s not in SENTINELS


def _infer_type(distinct: list[str]) -> str:
    if not distinct:
        return "EMPTY"
    full = lambda p: all(re.fullmatch(p, x) for x in distinct)
    if full(r"\d{8}") and all(
        1900 <= int(x[:4]) <= 2099 and 1 <= int(x[4:6]) <= 12 and 1 <= int(x[6:8]) <= 31
        for x in distinct
    ):
        return "DATE_YYYYMMDD"
    if full(r"-?\d+\.\d+"):
        return "DECIMAL"
    if full(r"\d+"):
        widths = {len(x) for x in distinct}
        zero_padded = any(x.startswith("0") and len(x) > 1 for x in distinct)
        if (zero_padded or len(widths) == 1) and len(distinct) / max(1, len(distinct)) >= 0.0:
            # promotion to IDENTIFIER decided later using distinct_ratio
            return "INTEGER"
        return "INTEGER"
    if len(distinct) <= LOW_CARD and all(len(x) <= 8 for x in distinct) and full(r"[A-Za-z0-9_/\-]+"):
        return "CATEGORICAL_CODE"
    return "FREE_TEXT"


def _profile(rows: list[dict], col: str) -> ColumnInsight:
    n = len(rows)
    raw = [r.get(col) for r in rows]
    nonblank = [v.strip() for v in raw if v is not None and v.strip() != ""]
    populated = [v for v in nonblank if v not in SENTINELS]
    sentinels = sorted({v for v in nonblank if v in SENTINELS})
    distinct = sorted(set(populated))
    dcount = len(distinct)
    pfrac = len(populated) / n if n else 0.0
    dratio = dcount / len(populated) if populated else 0.0

    itype = _infer_type(distinct)
    # promote long, near-unique numerics to IDENTIFIER
    if itype == "INTEGER" and pfrac == 1.0 and dratio >= 0.99 and distinct and len(distinct[0]) >= 6:
        itype = "IDENTIFIER"

    top: dict[str, float] = {}
    if 0 < dcount <= LOW_CARD and populated:
        for v, c in Counter(populated).most_common():
            top[v] = round(c / len(populated), 3)

    # role
    if not populated:
        role = "dead / redundant (always blank)"
    elif dcount == 1:
        role = "constant (no information)"
    elif itype == "IDENTIFIER":
        role = "candidate key / identifier"
    elif itype.startswith("DATE"):
        role = "date"
    elif itype in ("DECIMAL", "INTEGER"):
        role = "measure / numeric"
    elif itype == "CATEGORICAL_CODE":
        role = "categorical code"
    else:
        role = "free text"
    if 0.0 < pfrac < 1.0:
        role += " (conditionally populated)"

    # baseline observations + hypotheses (data-derived)
    obs: list[str] = [f"Populated in {pfrac:.0%} of rows; {dcount} distinct value(s)."]
    if sentinels:
        obs.append(f"Sentinel value(s) {sentinels} treated as 'not set' (null).")
    if top and itype == "CATEGORICAL_CODE":
        shown = ", ".join(f"{k} ({v:.0%})" for k, v in list(top.items())[:6])
        obs.append(f"Coded values: {shown}.")

    hyp: list[str] = []
    if role.startswith("dead"):
        hyp.append("Carries no information — safe to ignore in mapping.")
    elif role.startswith("constant"):
        hyp.append(f"Constant '{distinct[0]}' — a flag/region marker, not a real attribute.")
    elif itype == "IDENTIFIER":
        hyp.append("Unique across rows — likely the primary identifier / reference number.")
    elif itype.startswith("DATE"):
        hyp.append("A date encoded as YYYYMMDD; needs reformatting to a real date type.")
    elif itype == "CATEGORICAL_CODE":
        hyp.append("An opaque code — values need a lookup to interpret; candidate for reconciliation.")
    elif itype in ("DECIMAL", "INTEGER"):
        hyp.append("A numeric measure.")

    return ColumnInsight(
        name=col, inferred_type=itype, role=role, row_count=n,
        populated_fraction=round(pfrac, 3), distinct_count=dcount,
        distinct_ratio=round(dratio, 3), top_values=top, sentinels=sentinels,
        observations=obs, hypotheses=hyp,
    )


# ------------------------------------------------ conditional-population mining
def _dependencies(rows: list[dict], cols: list[str], profiles: dict[str, ColumnInsight]):
    n = len(rows)
    pop = {c: [_populated(r.get(c)) for r in rows] for c in cols}

    deps: list[DependencyFinding] = []
    for b in cols:
        pf = profiles[b].populated_fraction
        if not (0.0 < pf < 1.0):       # only conditionally-populated columns
            continue
        b_idx = [i for i in range(n) if pop[b][i]]
        best = None
        for a in cols:
            if a == b:
                continue
            # require A populated wherever B is populated
            if not all(pop[a][i] for i in b_idx):
                continue
            a_vals = {rows[i][a].strip() for i in b_idx}
            a_card = profiles[a].distinct_count
            if a_card == 0 or len(a_vals) >= a_card or len(a_vals) > 3:
                continue  # not a clean, small conditioning set
            # reverse coverage: of rows where A in a_vals, how many have B populated?
            cond_idx = [i for i in range(n) if pop[a][i] and rows[i][a].strip() in a_vals]
            cov = sum(pop[b][i] for i in cond_idx) / len(cond_idx) if cond_idx else 0.0
            cand = (len(a_vals), -cov, a, sorted(a_vals), cov)
            if best is None or cand < best:
                best = cand
        if best:
            _, _, a, a_vals, cov = best
            cond = f"{a} in {{{', '.join(a_vals)}}}"
            vals = ", ".join(_sql_lit(v) for v in a_vals)
            where = f"{a} = {vals}" if len(a_vals) == 1 else f"{a} is one of {vals}"
            cov_txt = "all of them" if cov >= 0.999 else f"{cov:.0%} of them"
            deps.append(DependencyFinding(
                statement=(
                    f"{b} is only populated for records where {where} — "
                    f"{len(b_idx)} such record(s), and {cov_txt} have {b} filled in."
                ),
                dependent=b, drivers=[a], condition=cond,
                support_rows=len(b_idx), confidence=round(cov, 3),
            ))

    # co-population (linked) groups: identical populated row-sets
    seen, groups = set(), []
    sig = {c: tuple(pop[c]) for c in cols if 0.0 < profiles[c].populated_fraction < 1.0}
    for c1 in sig:
        if c1 in seen:
            continue
        grp = [c1]
        for c2 in sig:
            if c2 != c1 and c2 not in seen and sig[c2] == sig[c1]:
                grp.append(c2)
        if len(grp) > 1:
            groups.append(grp)
            seen.update(grp)
    return deps, groups


# ------------------------------------------------- executable DQ rule library
# Every rule is a finding AND runnable SQL. The sample extract gives discovery;
# the SQL library is what reruns at full volume during cleansing.

_NI_SQL = ("(regexp_matches(trim({c}), '^[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z][0-9]{{6}}[A-D]$') "
           "AND substr(trim({c}),1,2) NOT IN ('BG','GB','KN','NK','NT','TN','ZZ'))")
_POSTCODE_SQL = ("regexp_matches(upper(trim({c})), "
                 "'^(GIR 0AA|[A-Z]{{1,2}}[0-9][A-Z0-9]? ?[0-9][A-Z]{{2}})$')")


def _q(c: str) -> str:
    return f'"{c}"'


def _sentinel_sql() -> str:
    return "(" + ", ".join(_sql_lit(x) for x in sorted(SENTINELS)) + ")"


def _exec_rule(wh: Warehouse, table: str, rule_id: str, name: str, category: str,
               columns: list[str], severity: str, description: str,
               applies: str, violation: str, sample_expr: str | None = None) -> "DQRule":
    """Materialise one rule: run it on the staged sample, keep the SQL for reruns."""
    from .contracts import DQRule
    sql = (f"-- {rule_id}: {name}\n"
           f"SELECT count(*) AS violations FROM {table}\n"
           f"WHERE ({applies})\n  AND ({violation});")
    total = wh.con.execute(
        f"SELECT count(*) FROM {table} WHERE {applies}").fetchone()[0]
    failed = wh.con.execute(
        f"SELECT count(*) FROM {table} WHERE ({applies}) AND ({violation})").fetchone()[0]
    samples: list[str] = []
    if failed:
        expr = sample_expr or _q(columns[0])
        samples = [str(r[0]) for r in wh.con.execute(
            f"SELECT DISTINCT {expr} FROM {table} WHERE ({applies}) AND ({violation}) LIMIT 3"
        ).fetchall()]
    return DQRule(id=rule_id, name=name, category=category, columns=columns,
                  severity=severity if failed else "info", description=description,
                  sql=sql, total=total, failed=failed,
                  pass_rate=round(1 - failed / total, 4) if total else 1.0,
                  samples=samples)


def _dq_rules(wh: Warehouse, table: str, cols: list[str], profiles: dict,
              pii: list[PiiFinding], deps: list[DependencyFinding],
              candidate_keys: list[str]) -> list:
    rules = []
    sent = _sentinel_sql()
    pop = lambda c: f"{_q(c)} IS NOT NULL AND trim({_q(c)}) <> '' AND trim({_q(c)}) NOT IN {sent}"
    by_cat = {p.column: p.category for p in pii if p.is_pii}

    # 1/2 — UK format rules on columns the PII scan identified
    for c in cols:
        if by_cat.get(c) == "National Insurance Number":
            rules.append(_exec_rule(
                wh, table, f"ni_format:{c}", "NI number format", "format", [c], "major",
                "2 prefix letters (D/F/I/Q/U/V and pairs BG/GB/KN/NK/NT/TN/ZZ disallowed) "
                "+ 6 digits + suffix A–D.",
                pop(c), "NOT " + _NI_SQL.format(c=_q(c))))
        if by_cat.get(c) == "Postcode":
            rules.append(_exec_rule(
                wh, table, f"uk_postcode:{c}", "UK postcode format", "format", [c], "minor",
                "Outward + inward code per the UK postcode grammar.",
                pop(c), "NOT " + _POSTCODE_SQL.format(c=_q(c))))

    # 3 — date validity for every YYYYMMDD column (sentinels excluded by pop())
    for c in cols:
        if profiles[c].inferred_type == "DATE_YYYYMMDD":
            rules.append(_exec_rule(
                wh, table, f"date_valid:{c}", "Calendar-valid date", "validity", [c], "major",
                "Value must parse as a real YYYYMMDD calendar date.",
                pop(c), f"try_strptime(trim({_q(c)}), '%Y%m%d') IS NULL"))

    # 4 — cross-date ordering: pairs that hold on >=90% of co-populated rows are
    # treated as business invariants; the exceptions are the finding
    dates = [c for c in cols if profiles[c].inferred_type == "DATE_YYYYMMDD"]
    for i, a in enumerate(dates):
        for b in dates[i + 1:]:
            both = f"({pop(a)}) AND ({pop(b)})"
            n = wh.con.execute(f"SELECT count(*) FROM {table} WHERE {both}").fetchone()[0]
            if n < 10:
                continue
            le = wh.con.execute(
                f"SELECT count(*) FROM {table} WHERE {both} AND {_q(a)} <= {_q(b)}").fetchone()[0]
            lo, hi = (a, b) if le >= n - le else (b, a)
            ratio = max(le, n - le) / n
            if ratio < 0.9:
                continue
            rules.append(_exec_rule(
                wh, table, f"date_order:{lo}<={hi}", f"Date order {lo} ≤ {hi}",
                "consistency", [lo, hi], "minor",
                f"{lo} precedes {hi} on {ratio:.0%} of co-populated rows — "
                f"treated as an invariant; exceptions listed.",
                both, f"{_q(lo)} > {_q(hi)}",
                sample_expr=f"{_q(lo)} || ' > ' || {_q(hi)}"))

    # 5 — conditional completeness from the mined dependencies
    for d in deps:
        m = re.fullmatch(r"(\w+) in \{(.+)\}", d.condition or "")
        if not m or d.confidence < 0.9:
            continue
        drv, vals = m.group(1), [v.strip() for v in m.group(2).split(",")]
        in_list = ", ".join(_sql_lit(v) for v in vals)
        rules.append(_exec_rule(
            wh, table, f"conditional:{d.dependent}", f"{d.dependent} required when "
            f"{drv} in ({in_list})", "completeness", [d.dependent, drv], "minor",
            d.statement, f"trim({_q(drv)}) IN ({in_list})",
            f"NOT ({pop(d.dependent)})", sample_expr=_q(drv)))

    # 6 — uniqueness of every candidate key
    for k in candidate_keys:
        rules.append(_exec_rule(
            wh, table, f"key_unique:{k}", f"Key {k} unique", "uniqueness", [k], "major",
            "Candidate key must not repeat.",
            pop(k),
            f"{_q(k)} IN (SELECT {_q(k)} FROM {table} GROUP BY {_q(k)} HAVING count(*) > 1)"))
    return rules


def sql_library(insight: TableInsight) -> str:
    """The whole rule library as one annotated, rerunnable SQL script."""
    head = (f"-- Data-quality rule library for {insight.table}\n"
            f"-- Generated by the analyst agent; rerun at full volume during cleansing.\n"
            f"-- Each statement returns the violation count for one rule.\n")
    def block(r):
        if r.suppressed:
            body = "\n".join("-- " + ln for ln in r.sql.splitlines())
            return (f"-- [SUPPRESSED {r.suppress_note}] {r.name} — "
                    f"excluded from cleansing\n{body}")
        return (f"-- [{r.category}/{r.severity}] {r.name} — sample run: "
                f"{r.failed}/{r.total} violations\n{r.sql}")
    return head + "\n\n".join(block(r) for r in insight.dq_rules) + "\n"



_TYPE_RE = {
    "DATE_YYYYMMDD": re.compile(r"^\d{8}$"),
    "DECIMAL": re.compile(r"^-?\d+\.\d+$"),
    "INTEGER": re.compile(r"^\d+$"),
    "IDENTIFIER": re.compile(r"^\d+$"),
}
_NINO_RE = re.compile(r"^[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]\d{6}[A-D]$", re.I)
_POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", re.I)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SORTCODE_RE = re.compile(r"^\d{2}-?\d{2}-?\d{2}$")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]+,\s+[A-Za-z'\-]+$")
_ORG_RE = re.compile(r"\b(scheme|plan|group|council|pension|ltd|limited|company|trust|fund)\b", re.I)


def _samples(rows: list[dict], col: str, k: int = 10) -> list[str]:
    out, seen = [], set()
    for r in rows:
        v = (r.get(col) or "").strip()
        if v and v not in SENTINELS and v not in seen:
            seen.add(v)
            out.append(v)
            if len(out) >= k:
                break
    return out


def _valid_ymd(v: str) -> bool:
    try:
        return 1900 <= int(v[:4]) <= 2099 and 1 <= int(v[4:6]) <= 12 and 1 <= int(v[6:8]) <= 31
    except Exception:
        return False


def _validity(rows: list[dict], col: str, itype: str) -> float:
    pat = _TYPE_RE.get(itype)
    if pat is None:
        return 1.0
    vals = [(r.get(col) or "").strip() for r in rows]
    vals = [v for v in vals if v and v not in SENTINELS]
    if not vals:
        return 1.0
    ok = sum(1 for v in vals if pat.fullmatch(v) and (itype != "DATE_YYYYMMDD" or _valid_ymd(v)))
    return ok / len(vals)


def _data_quality(rows: list[dict], cols: list[str], profiles: dict) -> list[DqFinding]:
    findings = []
    for c in cols:
        p = profiles[c]
        comp = round(p.populated_fraction, 3)
        val = round(_validity(rows, c, p.inferred_type), 3)
        issues: list[str] = []
        if p.role.startswith("dead"):
            issues.append("entirely empty (dead column)")
        elif 0.0 < comp < 1.0 and not p.role.startswith("constant") and "conditionally" not in p.role:
            issues.append(f"{(1 - comp):.0%} of rows blank/sentinel")
        if val < 1.0 and p.inferred_type in ("DATE_YYYYMMDD", "DECIMAL", "INTEGER", "IDENTIFIER"):
            issues.append(f"{(1 - val):.0%} of values fail {p.inferred_type} format")
        vals = [(r.get(c) or "") for r in rows]
        if any(v and v != v.strip() for v in vals):
            issues.append("leading/trailing whitespace")
        sev = "major" if val < 0.9 else ("minor" if issues else "ok")
        findings.append(DqFinding(column=c, completeness=comp, validity=val,
                                  distinct_ratio=round(p.distinct_ratio, 3),
                                  issues=issues, severity=sev))
    return findings


def _looks_like_dob(rows: list[dict], col: str) -> bool:
    yrs = [int(v[:4]) for r in rows if re.fullmatch(r"\d{8}", (v := (r.get(col) or "").strip()))]
    return bool(yrs) and min(yrs) < 1980 and max(yrs) <= 2010


def _rate(samples: list[str], pat) -> float:
    return sum(1 for s in samples if pat.fullmatch(s)) / len(samples) if samples else 0.0


def _detect_pii(rows: list[dict], cols: list[str], profiles: dict) -> list[PiiFinding]:
    """Deterministic baseline: structured PII by pattern, plus name/DOB heuristics.
    Runs with no LLM, so the privacy scan is never empty (offline-safe)."""
    out = []
    for c in cols:
        p = profiles[c]
        s = _samples(rows, c, 12)
        cat, sens, act, conf, why = "None", "none", "retain", 0.0, ""
        if s:
            if _rate(s, _NINO_RE) >= 0.8:
                cat, sens, act, conf = "National Insurance Number", "high", "tokenize", round(_rate(s, _NINO_RE), 2)
                why = "Values match the UK National Insurance number format (AA######A)."
            elif _rate(s, _POSTCODE_RE) >= 0.8:
                cat, sens, act, conf = "Postcode", "medium", "mask", round(_rate(s, _POSTCODE_RE), 2)
                why = "Values match the UK postcode format; part of a personal address."
            elif _rate(s, _EMAIL_RE) >= 0.8:
                cat, sens, act, conf = "Email", "medium", "mask", round(_rate(s, _EMAIL_RE), 2)
                why = "Values are email addresses."
            elif _rate(s, _SORTCODE_RE) >= 0.8:
                cat, sens, act, conf = "Bank Sort Code", "high", "tokenize", 0.9
                why = "Values match a UK bank sort-code format."
            elif p.inferred_type == "DATE_YYYYMMDD" and _looks_like_dob(rows, c):
                cat, sens, act, conf = "Date of Birth", "medium", "mask", 0.7
                why = "Date column whose years fall in a birth range, well before policy dates."
            elif p.inferred_type == "FREE_TEXT" and _rate(s, _NAME_RE) >= 0.6 \
                    and not any(_ORG_RE.search(x) for x in s):
                cat, sens, act, conf = "Name", "medium", "pseudonymize", 0.7
                why = "Free-text values resemble personal names (surname/forename)."
        is_pii = cat != "None"
        out.append(PiiFinding(column=c, is_pii=is_pii, category=cat, sensitivity=sens,
                              confidence=conf, method="pattern", rationale=why,
                              sample_evidence=(s[:3] if is_pii else []), recommended_action=act))
    return out


def _pii_llm(baseline: list[PiiFinding], rows: list[dict], cols: list[str],
             profiles: dict) -> list[PiiFinding]:
    """Primary PII pass: an LLM reasons over sample values per column. Falls back to
    the deterministic baseline when no LLM is configured."""
    from .. import config
    client, model = config.llm_client()
    if client is None:
        return baseline
    payload = [{"column": c, "inferred_type": profiles[c].inferred_type,
                "samples": _samples(rows, c, 10)} for c in cols]
    sys = (
        "You are a data privacy officer reviewing a LEGACY data extract whose column "
        "names are opaque and non-descriptive. Using ONLY the sample values, decide for "
        "each column whether it holds personal data (PII / special-category data) under "
        "UK GDPR — consider names, dates of birth, National Insurance numbers, "
        "postcodes/addresses, email, phone, and bank/sort codes. Be precise: identifiers "
        "like policy numbers are NOT personal data. Return ONLY JSON of the form "
        '{"columns":[{"column","is_pii"(bool),"category","sensitivity"(high|medium|low|none),'
        '"confidence"(0..1),"rationale","recommended_action"(mask|tokenize|pseudonymize|retain|drop)}]}'
    )
    try:
        resp = client.chat.completions.create(
            model=model, temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": json.dumps(payload, default=str)}],
        )
        by = {d["column"]: d for d in json.loads(resp.choices[0].message.content).get("columns", [])}
    except Exception:
        return baseline

    base = {f.column: f for f in baseline}
    merged = []
    for c in cols:
        b, d = base[c], by.get(c)
        if not d:
            merged.append(b)
            continue
        llm_pii = bool(d.get("is_pii"))
        if b.is_pii and b.confidence >= 0.8 and not llm_pii:   # keep a hard pattern hit
            merged.append(b)
        elif llm_pii:
            merged.append(PiiFinding(
                column=c, is_pii=True, category=d.get("category", b.category),
                sensitivity=d.get("sensitivity", "medium"),
                confidence=float(d.get("confidence", 0.8)),
                method="llm+pattern" if b.is_pii else "llm",
                rationale=d.get("rationale", ""), sample_evidence=_samples(rows, c, 3),
                recommended_action=d.get("recommended_action", "mask")))
        else:
            merged.append(PiiFinding(column=c, is_pii=False, method="llm"))
    return merged


def _pii_summary(pii: list[PiiFinding]) -> dict:
    flagged = [f for f in pii if f.is_pii]
    return {
        "pii_columns": len(flagged),
        "high_sensitivity": sum(1 for f in flagged if f.sensitivity == "high"),
        "require_masking": sum(1 for f in flagged if f.recommended_action in ("mask", "tokenize", "pseudonymize")),
        "categories": sorted({f.category for f in flagged}),
    }


def _dq_summary(dq: list[DqFinding]) -> dict:
    return {
        "overall_completeness": round(sum(f.completeness for f in dq) / len(dq), 3) if dq else 0.0,
        "columns_with_issues": sum(1 for f in dq if f.severity != "ok"),
        "major": sum(1 for f in dq if f.severity == "major"),
    }


def _smooth_dependencies(deps: list[DependencyFinding]) -> list[DependencyFinding]:
    """Optional LLM polish: make the pattern sentences read naturally. Stays
    strictly domain-blind — codes/column names are kept verbatim, never decoded.
    No-op (deterministic phrasing) when no LLM is configured."""
    if not deps:
        return deps
    from .. import config
    client, model = config.llm_client()
    if client is None:
        return deps
    payload = [{"id": i, "text": d.statement} for i, d in enumerate(deps)]
    sys = (
        "Rewrite each statement as clear, natural English for a business reader. "
        "CRITICAL CONSTRAINT: do NOT interpret, expand, translate, or guess the meaning of any "
        "column name (e.g. XA05) or code value (e.g. 'CL'). Keep every such token EXACTLY as "
        "written, quoted where quoted, treated as an opaque label — you have no idea what they "
        "mean and must not pretend to. Only improve fluency; preserve all facts and numbers. "
        'Return ONLY JSON: {"items":[{"id":int,"text":str}]}.'
    )
    try:
        resp = client.chat.completions.create(
            model=model, temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": json.dumps(payload)}],
        )
        by = {it["id"]: it["text"] for it in json.loads(resp.choices[0].message.content).get("items", [])}
        for i, d in enumerate(deps):
            if i in by and by[i].strip():
                d.statement = by[i].strip()
    except Exception:
        pass
    return deps


# ------------------------------------------------------------------- the agent
def analyze(csv_path: str, table_name: str | None = None,
            warehouse: Warehouse | None = None) -> TableInsight:
    """Run the analyst on a staged source table. Reads evidence from the DuckDB
    staging layer (Warehouse) — never opens its own connection or reads the file
    directly. If no warehouse is supplied, one is created and closed locally."""
    table = table_name or Path(csv_path).stem
    own = warehouse is None
    wh = warehouse or Warehouse()
    wh.stage_csv(csv_path, table, all_varchar=True)   # legacy text fidelity

    cols = wh.column_names(table)
    rows = wh.fetch_dicts(table)
    n = len(rows)

    profiles = {c: _profile(rows, c) for c in cols}
    deps, groups = _dependencies(rows, cols, profiles)
    deps = _smooth_dependencies(deps)

    dq = _data_quality(rows, cols, profiles)
    pii = _pii_llm(_detect_pii(rows, cols, profiles), rows, cols, profiles)
    dq_summary, pii_summary = _dq_summary(dq), _pii_summary(pii)

    candidate_keys = [c for c in cols if profiles[c].inferred_type == "IDENTIFIER"]
    dq_rules = _dq_rules(wh, table, cols, profiles, pii, deps, candidate_keys)
    failing = [r for r in dq_rules if r.failed]
    dq_summary["rules_total"] = len(dq_rules)
    dq_summary["rules_failing"] = len(failing)
    dq_summary["rule_violations"] = sum(r.failed for r in dq_rules)
    dead = [c for c in cols if profiles[c].role.startswith("dead")]
    constants = [c for c in cols if profiles[c].role.startswith("constant")]

    summary = [
        f"{n} rows, {len(cols)} columns. Column names are non-descriptive; "
        "structure below was recovered purely from the data.",
        f"Candidate key(s): {candidate_keys or 'none found'}.",
        f"Dead/blank columns: {dead or 'none'}; constant columns: {constants or 'none'}.",
        f"{len(deps)} conditional-population pattern(s) and "
        f"{len(groups)} linked column group(s) detected.",
        f"{pii_summary['pii_columns']} column(s) flagged as personal data (PII); "
        f"{pii_summary['require_masking']} recommended for masking/tokenisation.",
    ]

    insight = TableInsight(
        table=table, row_count=n, column_count=len(cols),
        candidate_keys=candidate_keys, dead_columns=dead, linked_groups=groups,
        columns=[profiles[c] for c in cols], dependencies=deps,
        dq=dq, dq_rules=dq_rules, pii=pii, dq_summary=dq_summary, pii_summary=pii_summary,
        summary=summary,
    )
    result = _narrate(insight)
    if own:
        wh.close()
    return result


# --------------------------------------------------------- lightweight variant
# Everything downstream of the MAPPING workspace reads exactly three fields off
# a TableInsight: `table`, `candidate_keys` and `dependencies` (validator's
# key-integrity + crossfield checks, the reviewer's data_patterns, and the
# tab 3/4 output checks). Deriving those does NOT require the DQ rule library,
# the PII scan, or either LLM call — which is where analyze()'s cost lives.
#
# This is deliberately NOT a separate implementation: it reuses _profile /
# _dependencies / _smooth_dependencies, so "candidate key" and "populated"
# (sentinel handling) keep exactly one definition. Anything analyze() learns
# about those, analyze_light() learns too.
_LIGHT_STAGES = ("profiling", "candidate keys", "conditional-population mining")


def analyze_light(csv_path: str, table_name: str | None = None,
                  warehouse: Warehouse | None = None) -> TableInsight:
    """Derive ONLY the structural facts the mapping/validation path consumes.

    Returns a valid TableInsight with `columns` intentionally empty — no
    consumer of this artifact reads per-column profiles, and omitting them
    keeps the cached document small. Use analyze() when the full profile,
    DQ rule library and PII findings are actually wanted (Flow A).
    """
    table = table_name or Path(csv_path).stem
    own = warehouse is None
    wh = warehouse or Warehouse()
    wh.stage_csv(csv_path, table, all_varchar=True)   # legacy text fidelity

    cols = wh.column_names(table)
    rows = wh.fetch_dicts(table)

    profiles = {c: _profile(rows, c) for c in cols}
    deps, groups = _dependencies(rows, cols, profiles)
    deps = _smooth_dependencies(deps)
    candidate_keys = [c for c in cols if profiles[c].inferred_type == "IDENTIFIER"]

    insight = TableInsight(
        table=table, row_count=len(rows), column_count=len(cols),
        candidate_keys=candidate_keys, linked_groups=groups,
        columns=[], dependencies=deps,
        summary=[
            f"Derived automatically from {Path(csv_path).name} "
            f"({len(rows)} rows, {len(cols)} columns).",
            f"Candidate key(s): {candidate_keys or 'none found'}.",
            f"{len(deps)} conditional-population pattern(s) detected.",
            "Structural facts only — data-quality rules, PII findings and "
            "per-column profiles are not derived on this path.",
        ],
        generated_by="deterministic+light",
    )
    if own:
        wh.close()
    return insight


# ----------------------------------------------------- LLM narration (optional)
def _narrate(insight: TableInsight) -> TableInsight:
    """Rephrase deterministic facts into analyst insight. Azure if configured,
    else keep the deterministic baseline narrative."""
    from .. import config
    client, model = config.llm_client()
    if client is None:
        insight.generated_by = "deterministic+offline_stub"
        return insight

    facts = insight.model_dump()
    sys = (
        "You are a data analyst with NO knowledge of the source system or its domain. "
        "Given these computed profiling facts, write concise, strictly evidence-based "
        "observations and hypotheses per column, and a short overall summary. Hypothesize "
        "role (identifier/code/date/measure/dead) and relationships ONLY from the data — "
        "never invent business meaning. Return ONLY JSON with keys: columns "
        "(list of {name, observations[], hypotheses[]}) and summary (list of strings)."
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": json.dumps(facts, default=str)}],
        )
        data = json.loads(resp.choices[0].message.content)
        by_name = {c["name"]: c for c in data.get("columns", [])}
        for col in insight.columns:
            if col.name in by_name:
                col.observations = by_name[col.name].get("observations", col.observations)
                col.hypotheses = by_name[col.name].get("hypotheses", col.hypotheses)
        if data.get("summary"):
            insight.summary = data["summary"]
        insight.generated_by = "deterministic+llm"
    except Exception as e:
        insight.summary.append(f"[LLM narration skipped: {e}]")
        insight.generated_by = "deterministic+offline_stub"
    return insight
