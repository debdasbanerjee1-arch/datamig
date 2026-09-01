"""Reconciliation rules as a certified artefact — derived ONCE.

WHY THIS MODULE EXISTS
The cross-field rules used to be derived in three separate places: the runner,
the rule preview, and the standalone script generator. Three implementations of
the same logic, which is why one defect (driver codes compared in SOURCE terms
against DELIVERED values, so `policy_status NOT IN ('CL')` was true for every
row) took three separate fixes: the report, then the preview, then the script.
Three derivations means three chances to disagree, and they did.

So the rules become an ARTEFACT, the way the mapping specification already is:

    derive_candidates()  ->  [proposed rules]
                                  |
                          human certifies / adds
                                  |
                          certified rule set  ->  script generation
                                              ->  execution

Both consumers read the same certified set, so a script and the results it is
meant to reproduce cannot diverge — there is nothing left to derive twice.

It also closes an audit gap. Reconciliation was the one workspace with no
certification step: rules appeared, ran, and vanished, leaving no answer to
"which controls did we sign off, and who signed them?". Every rule now carries
its origin (mined / llm_proposed / user_added) and, once certified, who approved
it.

RULE SHAPE
A rule is data, never prose and never SQL. `kind` selects the executor;
`params` carries only what that executor needs. A user-authored rule is the same
object as a mined one — it just arrives with origin="user_added" — so user rules
need no separate machinery, no natural-language translation step, and no second
execution path that could behave differently from the first.
"""
from __future__ import annotations

import json
import re
from typing import Any

_CASE_PAIR = re.compile(r"WHEN\s+'((?:[^']|'')*)'\s+THEN\s+'((?:[^']|'')*)'", re.I)

# every rule kind the executor understands. A rule naming anything else is
# rejected at certification rather than silently skipped at run time.
KINDS = (
    "control_total:rows",
    "control_total:columns",
    "control_total:populated_cells",
    "control_total:distinct_keys",
    "control_total:numeric_sums",
    "category_profile",
    "crossfield",
    # authored by the business: an aggregate of one column, optionally broken
    # down by one or more categorical columns, reconciled source vs delivered.
    # "does the total sum assured for each product still agree?" is the control
    # a business reviewer actually asks for, and it is expressible as structured
    # data — column, function, group-by — with no prose to translate.
    "aggregate_by",
)
TECHNICAL_KINDS = {k for k in KINDS if k.startswith("control_total")}
BUSINESS_KINDS = {"category_profile", "crossfield", "aggregate_by"}

_NUMERIC_TYPES = {"number", "integer", "int", "float", "decimal", "numeric"}
_TEMPORAL_TYPES = {"date", "datetime", "timestamp", "time"}
_NONDET_TOKENS = ("now(", "current_timestamp", "current_date", "current_localtime",
                  "localtimestamp", "random(", "uuid(")


def _is_nondet(expr: str) -> bool:
    low = (expr or "").lower().replace(" ", "")
    return any(t in low for t in _NONDET_TOKENS)


def decode_pairs(sql: str) -> dict[str, str]:
    """Source code -> target value, read out of a certified CASE expression.

    Lets a rule be stated in the DELIVERED file's vocabulary without a database:
    a dependency mined from the source says "populated only when STATCD in
    {CL}", but the delivered data holds 'CLOSED'.
    """
    return {a.replace("''", "'"): b.replace("''", "'")
            for a, b in _CASE_PAIR.findall(sql or "")}


_CODED_TYPES = {"enum", "enumeration", "categorical", "category", "code",
                "coded", "lookup", "picklist", "domain", "list", "boolean"}


def _is_coded_target_dict(attr: dict) -> bool:
    """A target attribute that declares a value domain, however its type is
    spelled. allowed_values is structural evidence; the type name is
    author-supplied vocabulary."""
    if attr.get("allowed_values") or attr.get("value_meanings"):
        return True
    return (attr.get("type") or "").lower() in _CODED_TYPES


def _rule(kind: str, *, title: str, params: dict[str, Any],
          origin: str = "mined", severity: str = "hard",
          attribute: str | None = None) -> dict:
    return {"kind": kind, "title": title, "params": params, "origin": origin,
            "severity": severity, "attribute": attribute,
            "category": "control_total" if kind.startswith("control_total")
                        else kind,
            "certified_by": None}


def derive_candidates(spec: dict, target_dict: dict,
                      source_filenames: dict[str, str] | None = None,
                      insight: dict | None = None) -> list[dict]:
    """Propose the reconciliation rules for this specification.

    Nothing here executes: it reads the certified mapping specification, the
    target dictionary and the derived source insight, and returns rules as data.
    The same list feeds the preview, the script and the run.
    """
    has_source = bool(source_filenames)
    attrs = [a for a in (target_dict.get("attributes")
                         or target_dict.get("columns")
                         or target_dict.get("fields") or []) if isinstance(a, dict)]
    type_of = {a["name"]: (a.get("type") or "string").lower() for a in attrs}
    required = [a["name"] for a in attrs if not a.get("nullable", False)]

    live = [m for m in spec.get("mappings", []) if m.get("gate") != "reject"]
    sql_of = {m.get("target_attribute"): m.get("transformation_sql") or ""
              for m in live}
    expected_cols = sorted({m["target_attribute"] for m in live}
                           | {u["attribute"] for u in spec.get("unmapped_target", [])})

    one_to_one = {}
    for m in live:
        if (m.get("cardinality") == "1:1"
                and len(m.get("source_attributes") or []) == 1):
            one_to_one.setdefault(m["source_attributes"][0], m["target_attribute"])
    candidate_keys = (insight or {}).get("candidate_keys") or []
    key_src = next((k for k in candidate_keys if k in one_to_one), None)
    key_tgt = one_to_one.get(key_src) if key_src else None

    rules: list[dict] = [
        _rule("control_total:rows",
              title="Delivered row count equals the source row count",
              params={"has_source": has_source}),
        _rule("control_total:columns",
              title=f"All {len(expected_cols)} column(s) the certified mapping "
                    f"produces are present in the delivered file",
              params={"expected_columns": expected_cols}),
        _rule("control_total:populated_cells",
              title=f"No blank value in the {len(required)} attribute(s) the "
                    f"target declares mandatory",
              params={"required_attributes": required}),
    ]
    if key_tgt:
        rules.append(_rule("control_total:distinct_keys",
                           title=f"Distinct '{key_tgt}' matches the source",
                           params={"key_target": key_tgt, "key_source": key_src},
                           attribute=key_tgt))

    numeric = [{"column": m["target_attribute"], "expr": sql_of[m["target_attribute"]]}
               for m in live
               if type_of.get(m["target_attribute"]) in _NUMERIC_TYPES
               and sql_of.get(m["target_attribute"])
               and not _is_nondet(sql_of[m["target_attribute"]])]
    if numeric and has_source:
        # name the columns: "every numeric column" left the reviewer guessing
        # which ones, and the answer is knowable
        cols = ", ".join(x["column"] for x in numeric)
        rules.append(_rule("control_total:numeric_sums",
                           title=f"Totals agree source vs delivered for {cols}",
                           params={"columns": numeric}))

    # ---- business controls ------------------------------------------------
    # A category profile needs POSITIVE evidence that the attribute is a
    # category. Excluding numerics and dates was not enough: it left every
    # identifier and free-text column proposed, so the reviewer was offered
    # "record counts per value of policy_number", "... of account_holder_name"
    # and "... of bank_account_number" — one bucket per record, which is not a
    # control. The executor skipped them at run time on cardinality, so they
    # were never even reported, just noise in the rule list.
    #
    # Two derivable signals, both already to hand:
    #   * the TARGET declares a value domain (allowed_values), or
    #   * the certified transform is a DECODE — a CASE mapping codes to values,
    #     which only exists for a coded column
    # Neither needs the delivered data or a word list, so this works on any
    # extract.
    coded = {a["name"] for a in attrs if _is_coded_target_dict(a)}
    for m in live:
        attr, expr = m["target_attribute"], sql_of.get(m["target_attribute"], "")
        if (not expr or _is_nondet(expr)
                or type_of.get(attr) in _NUMERIC_TYPES
                or type_of.get(attr) in _TEMPORAL_TYPES):
            continue
        if attr not in coded and not decode_pairs(expr):
            continue                    # no evidence this attribute is a category
        rules.append(_rule("category_profile",
                           title=f"Record counts per value of '{attr}' reconcile",
                           params={"attribute": attr, "expr": expr},
                           attribute=attr))

    for d in (insight or {}).get("dependencies", []) or []:
        dep_tgt = one_to_one.get(d.get("dependent"))
        mobj = re.search(r"(\w+)\s+in\s+\{([^}]*)\}", d.get("condition") or "")
        if not (dep_tgt and mobj):
            continue
        driver_tgt = one_to_one.get(mobj.group(1))
        if not driver_tgt:
            continue
        decode = decode_pairs(sql_of.get(driver_tgt, ""))
        values = sorted({decode.get(c.strip(), c.strip())
                         for c in mobj.group(2).split(",")})
        rules.append(_rule(
            "crossfield",
            title=f"'{dep_tgt}' is populated only when '{driver_tgt}' is "
                  f"{', '.join(values)}",
            params={"attribute": dep_tgt, "driver": driver_tgt, "values": values},
            attribute=dep_tgt))
    return rules


def validate_rule(rule: dict, target_dict: dict) -> str | None:
    """Reject a rule that cannot be executed faithfully. Returns a reason.

    This is what makes user-authored rules safe without a natural-language
    step: a rule is structured data validated against the target dictionary, so
    it cannot reference a column that does not exist or a value outside a
    declared domain. There is nothing to mistranslate.
    """
    kind = rule.get("kind")
    if kind not in KINDS:
        return f"unknown rule kind {kind!r}"
    attrs = {a["name"]: a for a in (target_dict.get("attributes") or [])
             if isinstance(a, dict)}
    p = rule.get("params") or {}

    if kind == "crossfield":
        for field in ("attribute", "driver"):
            if p.get(field) not in attrs:
                return f"{field} {p.get(field)!r} is not a target attribute"
        allowed = attrs[p["driver"]].get("allowed_values")
        vals = p.get("values") or []
        if not vals:
            return "no driver values given"
        if allowed:
            unknown = [v for v in vals if v not in allowed]
            if unknown:
                return (f"{unknown} are not declared values of "
                        f"'{p['driver']}' ({allowed})")
    if kind == "category_profile" and p.get("attribute") not in attrs:
        return f"attribute {p.get('attribute')!r} is not a target attribute"

    if kind == "aggregate_by":
        fn = (p.get("function") or "").lower()
        if fn not in ("sum", "count", "avg", "min", "max", "count_distinct"):
            return f"unsupported aggregate {p.get('function')!r}"
        col = p.get("column")
        if fn != "count" and col not in attrs:
            return f"column {col!r} is not a target attribute"
        if fn in ("sum", "avg") and col in attrs:
            t = (attrs[col].get("type") or "").lower()
            if t not in ("number", "integer", "int", "float", "decimal", "numeric"):
                return f"'{col}' is type '{t}' — {fn} needs a numeric column"
        for g in (p.get("group_by") or []):
            if g not in attrs:
                return f"group-by {g!r} is not a target attribute"
    return None


def certify(candidates: list[dict], decisions: dict[str, dict] | None = None,
            added: list[dict] | None = None, target_dict: dict | None = None,
            certified_by: str = "reviewer") -> tuple[list[dict], list[dict]]:
    """Apply a reviewer's decisions and any hand-authored rules.

    Returns (certified_rules, rejected) where each rejected entry carries a
    reason. A rule that fails validation is never silently dropped: an assurance
    reviewer must be able to see that a control they asked for did not make it
    into the set, and why.
    """
    decisions = decisions or {}
    out, rejected = [], []
    for rule in candidates:
        action = (decisions.get(rule_id(rule)) or {}).get("action", "accept")
        if action == "reject":
            rejected.append(dict(rule, reason="excluded by the reviewer"))
            continue
        out.append(dict(rule, certified_by=certified_by))
    for rule in (added or []):
        rule = dict(rule)
        rule.setdefault("origin", "user_added")
        rule.setdefault("severity", "hard")
        rule.setdefault("category", "control_total"
                        if str(rule.get("kind", "")).startswith("control_total")
                        else rule.get("kind"))
        rule.setdefault("attribute", (rule.get("params") or {}).get("attribute"))
        rule.setdefault("title", rule.get("kind", "user rule"))
        why = validate_rule(rule, target_dict or {})
        if why:
            rejected.append(dict(rule, reason=why))
            continue
        out.append(dict(rule, certified_by=certified_by))
    return out, rejected


def rule_id(rule: dict) -> str:
    """Stable identity, so a reviewer's decision survives a re-derivation."""
    p = rule.get("params") or {}
    suffix = p.get("attribute") or p.get("key_target") or ""
    if rule.get("kind") == "crossfield":
        suffix = f"{p.get('attribute')}~{p.get('driver')}"
    if rule.get("kind") == "aggregate_by":
        suffix = (f"{p.get('function')}:{p.get('column') or '*'}"
                  + (f"~{'+'.join(p.get('group_by') or [])}" if p.get("group_by") else ""))
    return f"{rule.get('kind')}:{suffix}" if suffix else str(rule.get("kind"))


def split_families(rules: list[dict]) -> tuple[list[dict], list[dict]]:
    return ([r for r in rules if r.get("kind") in TECHNICAL_KINDS],
            [r for r in rules if r.get("kind") in BUSINESS_KINDS])

_RULE_REVIEW_SYS = (
    "You are advising on RECONCILIATION controls for a data migration. A control "
    "compares the delivered data against the source and must be able to FAIL "
    "informatively: a reviewer should learn something from the result.\n"
    "\n"
    "You are given the target attributes with their types and descriptions, and "
    "the controls already proposed. Do two things.\n"
    "\n"
    "1. FLAG proposed controls that cannot tell anyone anything. A per-value "
    "record count is useful for a status or a product code, where the buckets "
    "are few and meaningful, and useless for an identifier, a name, a reference "
    "or a free-text field, where every record is its own bucket.\n"
    "\n"
    "2. PROPOSE controls the data cannot suggest for itself — business rules "
    "that follow from what the attributes MEAN. Two shapes are available:\n"
    "   * aggregate_by: sum/avg/min/max/count/count_distinct of a column, "
    "optionally broken down by other columns. Use for money and volume "
    "controls, e.g. the total of an amount by product.\n"
    "   * crossfield: an attribute that may only be populated when another "
    "holds certain values, e.g. an exit reason only where the policy has "
    "exited.\n"
    "Only reference attributes that exist, and only values a coded attribute "
    "actually permits. Propose nothing you cannot justify from the "
    "descriptions.\n"
    'Respond with JSON only: {"flag": [{"id": "<rule id>", "reason": "..."}], '
    '"propose": [{"kind": "aggregate_by|crossfield", "title": "...", '
    '"params": {...}, "reason": "..."}]}'
)


def llm_review_rules(candidates: list[dict], target_dict: dict) -> list[dict]:
    """Ask the model to flag useless controls and propose business ones.

    Neither result is applied: a flag annotates the rule so the reviewer sees
    the objection, and a proposal joins the candidate list with
    origin="llm_proposed". Everything still passes through certification, so
    the model influences what is OFFERED and never what is executed — the same
    asymmetry the mapping tiers use.

    Proposals are validated against the target dictionary before they are
    offered, so a control naming an attribute that does not exist, or a value
    outside a coded attribute's domain, is discarded rather than shown.

    Offline this returns the candidates unchanged.
    """
    from engine import config
    from engine.llmjson import _llm_json

    client, model = config.llm_client()
    if client is None:
        return candidates

    attrs = [a for a in (target_dict.get("attributes") or []) if isinstance(a, dict)]
    payload = {
        "target_attributes": [
            {"name": a["name"], "type": a.get("type", ""),
             "description": a.get("description", ""),
             "allowed_values": a.get("allowed_values") or []}
            for a in attrs],
        "proposed_controls": [
            {"id": rule_id(r), "kind": r["kind"], "title": r.get("title", ""),
             "params": r.get("params", {})}
            for r in candidates],
    }

    try:
        out = _llm_json(client, model,
                        [{"role": "system", "content": _RULE_REVIEW_SYS},
                         {"role": "user", "content": json.dumps(payload, default=str)}],
                        max_tokens=400 + 60 * len(candidates))
    except Exception as exc:                    # noqa: BLE001
        for r in candidates:
            r.setdefault("llm_note", f"rule review unavailable: {exc}")
        return candidates

    flagged = {f.get("id"): f.get("reason", "")
               for f in (out.get("flag") or []) if isinstance(f, dict)}
    for r in candidates:
        note = flagged.get(rule_id(r))
        if note:
            # annotate, never drop: a reviewer decides, and a model that
            # misreads a control should not silently remove it
            r["llm_flag"] = note

    for p in (out.get("propose") or []):
        if not isinstance(p, dict):
            continue
        rule = _rule(str(p.get("kind")), title=str(p.get("title") or p.get("kind")),
                     params=dict(p.get("params") or {}), origin="llm_proposed")
        why = validate_rule(rule, target_dict)
        if why:
            continue                            # cannot be executed faithfully
        if any(rule_id(rule) == rule_id(r) for r in candidates):
            continue                            # already proposed deterministically
        rule["llm_reason"] = str(p.get("reason") or "")
        candidates.append(rule)
    return candidates
