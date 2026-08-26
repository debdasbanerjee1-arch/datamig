"""Agent 3 — the data mapping (matcher) agent.

Inputs:
  * enriched source dictionary  (EnrichedDictionary, from Agent 2)
  * target data dictionary       (well-documented: name, type, description, enums)
  * the staged source data        (Warehouse) — used to VALIDATE transformations

Output: a MappingSpec — per target attribute, the source attribute(s), the
transformation, a confidence score, and the gate. Confidence is *earned*: every
proposed transformation is executed against the real source data and scored on
how much of its output lands in the target's domain.

Deterministic layer proposes signals + transformations and validates them; the
LLM (Azure) refines wording/edge cases. Stub runs offline.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from ..models import Gate, decide_gate, target_attributes, target_table_name
from ..staging import Warehouse
from .contracts import EnrichedColumn, EnrichedDictionary, MappingEntry, MappingSpec
from ..llmjson import _llm_json

SENTINELS = ("00000000", "")


# ------------------------------------------------------------------ helpers
def _lit(value: str) -> str:
    """A single-quoted SQL literal with embedded quotes doubled."""
    return "'" + str(value).replace("'", "''") + "'"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(s).lower()))


# format/type words that every field of a given type shares — they say nothing
# about *meaning*, so they must not drive name similarity (otherwise every date
# column matches every date target merely on the token "date").
GENERIC = {"date", "datetime", "timestamp", "time", "amount", "value", "number",
           "no", "code", "id", "flag", "indicator", "ind", "ref", "reference",
           "the", "of"}


def _content(s: str) -> set[str]:
    t = _tokens(s) - GENERIC
    return t or _tokens(s)          # fall back to raw if a name is purely generic


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a | b) else 0.0


def _bool_like(src: EnrichedColumn) -> bool:
    """A source that can legitimately feed a boolean target: a two-value coded
    column (Y/N, T/F, 0/1, a pair of decoded states), or one whose name itself
    signals a flag/indicator. An identifier, name, amount or free-text field is
    NOT boolean-like, however its words happen to overlap the target."""
    vd = getattr(src, "value_decode", None) or {}
    if 0 < len(vd) <= 2:
        return True
    keys = {str(k).strip().upper() for k in vd}
    if keys and keys <= {"Y", "N", "T", "F", "0", "1", "TRUE", "FALSE"}:
        return True
    blob = (src.business_name + " " + " ".join(getattr(src, "evidence", []) or [])).lower()
    return any(w in blob for w in ("flag", "indicator", "boolean", "y/n", "yes/no"))


def _type_compat(src_type: str, tgt_type: str, src: Optional[EnrichedColumn] = None) -> float:
    fam = {
        "IDENTIFIER": "string", "FREE_TEXT": "string", "CATEGORICAL_CODE": "string",
        "DATE_YYYYMMDD": "date", "DECIMAL": "decimal", "INTEGER": "decimal",
        "EMPTY": "string", "CONSTANT": "string",
    }
    t = {"enum": "string", "number": "decimal", "numeric": "decimal", "timestamp": "date"}
    # a boolean target needs boolean-like evidence — a flag or two-value coded
    # source. A customer id / name / amount landing in a boolean is a spurious
    # fit even when a shared word (e.g. "customer") lifts the name score.
    if tgt_type == "boolean":
        return 1.0 if (src is not None and _bool_like(src)) else 0.15
    if fam.get(src_type, "string") != t.get(tgt_type, tgt_type):
        return 0.4
    # a coded target (enum) is properly fed by a coded source — an identifier or
    # free-text column landing in an enum is a weak structural fit, even if it's
    # nominally "string".
    if tgt_type == "enum" and src_type != "CATEGORICAL_CODE":
        return 0.3
    return 1.0


def _value_overlap(src: EnrichedColumn, target: dict) -> Optional[float]:
    allowed = target.get("allowed_values")
    if not (allowed and src.value_decode):
        return None
    norm_allowed = {_norm(v) for v in allowed}
    # Count a code as covered if EITHER its decoded label or the raw code lands
    # in the target domain. This must stay consistent with _synth(), which
    # builds the CASE the same way — otherwise a mapping whose codes all
    # translate cleanly would still score as having no value evidence.
    hits = sum(1 for code, lbl in src.value_decode.items()
               if _norm(lbl) in norm_allowed or _norm(code) in norm_allowed)
    return hits / len(src.value_decode)


def _code_only_enum_match(target: dict, src: EnrichedColumn) -> bool:
    """True when a coded target matches ONLY because the raw codes coincide.

    'Surrender' -> 'SURRENDER' is semantic evidence: the decoded meaning agrees.
    'C' -> 'C' is not — it only says both sides draw from the same small
    alphabet, and single-letter flags are common enough that two unrelated
    fields can share a domain by chance. So a code-only match is treated as
    weaker evidence and is not allowed to auto-accept on its own.
    """
    allowed = target.get("allowed_values")
    if not (allowed and src.value_decode):
        return False
    norm_allowed = {_norm(v) for v in allowed}
    by_label = by_code = 0
    for code, lbl in src.value_decode.items():
        if _norm(lbl) in norm_allowed:
            by_label += 1
        elif _norm(code) in norm_allowed:
            by_code += 1
    return by_code > 0 and by_label == 0


def _name_sim(src: EnrichedColumn, target: dict) -> float:
    """Best evidence across the column's business name and any authored aliases.

    Aliases exist because some real matches are not lexical at all: NINO
    ("NI Number") is the correct source for tax_file_number, and CUSTID
    ("Customer Reference") for investor_id, but neither shares a single
    content token with its target. No similarity metric recovers that — it is
    domain knowledge, so it is authored in the dictionary and read here.
    """
    tn, tdsc = _content(target["name"]), _content(target.get("description", ""))
    best = 0.0
    for phrase in [src.business_name, *(getattr(src, "aliases", None) or [])]:
        st = _content(phrase)
        best = max(best, _jaccard(st, tn), 0.6 * _jaccard(st, tdsc))

    # The SOURCE DESCRIPTION was previously never read: the comparison was
    # asymmetric, taking the target's description in but only the source's NAME
    # out. So "Number of months passed since the last premium paid" and
    # "Elapsed months since the most recent premium payment" — the same fact in
    # two vocabularies — scored zero against each other, while a shared "policy"
    # token sent the column to policy_number instead. Effort spent authoring
    # source descriptions bought nothing offline.
    #
    # Scored by CONTAINMENT rather than Jaccard: a description is many times
    # longer than a name, so Jaccard's union term buries a real overlap. The
    # weights are calibrated, not guessed — below 0.7, a two-token semantic
    # overlap between descriptions ("months", "premium") still lost to a single
    # shared token on a name ("Premium Amount"), which is the wrong ordering.
    # They stay under 1.0 so a long description cannot out-score an exact name
    # match. This can only ever RAISE a score, never lower one.
    sd = _prose(getattr(src, "description", "") or "")
    if sd:
        best = max(best,
                   0.75 * _containment(sd, _prose(target.get("description", "")) or tdsc),
                   0.60 * _containment(sd, tn))
    return best


# English function words. _content strips DOMAIN-generic terms ("number", "code"),
# which is enough for short business names but not for prose: two unrelated
# descriptions both opening "Whether the ..." overlapped on that single word, and
# a boolean target matched a scheme-status column on the strength of it.
_STOP = {
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at", "by",
    "with", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "has", "have", "had", "that", "this", "these", "those", "it", "its",
    "which", "whether", "when", "where", "if", "then", "than", "but", "not",
    "no", "any", "all", "each", "per", "into", "onto", "up", "down", "out",
    "over", "under", "again", "further", "here", "there", "so", "such", "only",
    "own", "same", "other", "another", "new", "used", "using", "use", "may",
    "must", "will", "would", "should", "can", "could", "shall",
}


def _prose(text: str) -> set:
    """Content tokens of a DESCRIPTION: domain-generic and function words out."""
    return _content(text) - _STOP


def _containment(a: set, b: set) -> float:
    """|a & b| / min(|a|, |b|) — overlap relative to the SMALLER side.

    Jaccard divides by the union, so comparing a 12-word description against a
    2-word target name scores near zero even when the name is wholly contained
    in the description. Containment asks the question that actually matters: how
    much of the shorter phrase is present in the longer one.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _composite(src: EnrichedColumn, target: dict) -> float:
    n = _name_sim(src, target)
    t = _type_compat(src.inferred_type, target["type"], src)
    v = _value_overlap(src, target)
    # a boolean target needs BOTH boolean-like structure AND name/semantic
    # agreement. Structure alone can't win: a two-value flag whose meaning is
    # unrelated (e.g. a contact-trace flag vs. a vulnerability flag) must not be
    # force-fit just because it is boolean-shaped. So the name acts as a gate —
    # a bool-like source with no name overlap stays weak. (Derived boolean rules
    # such as is_group_policy are handled in _derived and never reach here.)
    if target["type"] == "boolean":
        if t < 1.0:                 # not boolean-like at all -> structurally unfit
            return 0.15 * n
        return 0.65 * n + 0.35 * t  # bool-like: name must still carry the match
    # a coded target (enum) can only be trusted with coded evidence — a source
    # whose decoded values don't land in the target domain (or that has no decode
    # at all) is a weak match, NOT a free pass on type alone.
    if target.get("allowed_values"):
        return 0.30 * n + 0.20 * t + 0.50 * (v if v is not None else 0.0)
    # Type compatibility is NOT evidence of meaning. Two string columns are
    # type-compatible with every string target, so without this floor ~20
    # candidates tie at 0.4 and the winner is decided by column order — which
    # is exactly how POLNO came to feed tax_file_number and investor_id while
    # NINO and CUSTID were reported as "no target attribute". The same
    # principle is already applied to boolean and enum targets above; plain
    # strings were the gap. A target with no name and no value evidence is
    # better left unmapped (and handed to the LLM recovery tier) than filled
    # confidently from the wrong column.
    if n <= 0.0 and not v:
        return 0.12 * t
    if v is None:
        return 0.6 * n + 0.4 * t
    return 0.35 * n + 0.15 * t + 0.50 * v


# ----------------------------------------------------- transformation synthesis

# ---------------------------------------------------------------------------
# Derivation gap — "the target describes work this SQL does not do"
# ---------------------------------------------------------------------------
# The deterministic synthesiser has a fixed repertoire of SINGLE-COLUMN
# patterns: enum decode, date parse, numeric cast, trim, copy. It cannot
# COMPOSE. So when a target needs concatenation, unit conversion, arithmetic
# across columns or reformatting, the mapping silently falls through to "copy
# the best-matching single column" — and every downstream check passes, because
# validation can confirm the value is a well-formed integer but not that it is
# the integer the target asked for.
#
# Observed: full_name <- SURNAME (forename dropped), annual_premium_gbp <-
# PREM_PENCE (out by 100x-1200x), age_at_commencement <- COMMDT (a date where a
# count of years belongs). All three certified clean.
#
# These signals are derived from the artefacts, not from a keyword list of
# business rules: they compare what the TARGET says it needs against what the
# SQL actually references.
_BARE_COPY = re.compile(r"^\s*(NULLIF\s*\(\s*(TRIM\s*\(\s*)?\"[^\"]+\"|TRY_CAST\s*\(\s*\"[^\"]+\"|\"[^\"]+\")",
                        re.I)

# Unit / scale families. A target and its source naming DIFFERENT members of the
# same family is a conversion the SQL must perform and a bare copy does not.
_UNIT_FAMILIES = (
    {"pence", "penny", "pennies", "p"},
    {"pound", "pounds", "gbp", "sterling"},
    {"cent", "cents"}, {"dollar", "dollars", "usd"},
    {"monthly", "month"}, {"quarterly", "quarter"}, {"annual", "annually",
                                                     "annualised", "annualized",
                                                     "yearly", "year", "annum"},
    {"gross"}, {"net"},
)
_FORMAT_HINTS = ("uppercase", "upper case", "lowercase", "lower case",
                 "normalised", "normalized", "formatted", "padded", "trimmed to",
                 "single space", "without spaces", "title case", "as '")


def _is_bare_copy(sql: str) -> bool:
    """True when the SQL only reads one column and applies at most a cast/trim."""
    if not sql:
        return False
    if "CASE" in sql.upper():
        return False
    cols = set(re.findall(r'"([^"]+)"', sql))
    return len(cols) <= 1 and bool(_BARE_COPY.match(sql.strip()))


def _unit_conflict(target: dict, srcs: list[str], by_name: dict) -> str | None:
    """A unit or scale word in the target that the source contradicts."""
    t_tokens = _tokens(f"{target['name']} {target.get('description','')}")
    s_tokens: set[str] = set()
    for name in srcs:
        c = by_name.get(name)
        if c:
            s_tokens |= _tokens(f"{name} {c.business_name} {c.description}")
    for family in _UNIT_FAMILIES:
        t_hit, s_hit = t_tokens & family, s_tokens & family
        if t_hit and not s_hit:
            for other in _UNIT_FAMILIES:
                if other is family:
                    continue
                if s_tokens & other and _same_dimension(family, other):
                    return (f"the target is expressed in {sorted(t_hit)[0]} but "
                            f"{srcs[0]} is in {sorted(s_tokens & other)[0]}")
    return None


_DIMENSIONS = (
    [{"pence", "penny", "pennies", "p"}, {"pound", "pounds", "gbp", "sterling"}],
    [{"cent", "cents"}, {"dollar", "dollars", "usd"}],
    [{"monthly", "month"}, {"quarterly", "quarter"},
     {"annual", "annually", "annualised", "annualized", "yearly", "year", "annum"}],
    [{"gross"}, {"net"}],
)


def _same_dimension(a: set, b: set) -> bool:
    return any(a in group and b in group for group in _DIMENSIONS)


def _distinctive_tokens(by_name: dict) -> dict[str, set[str]]:
    """Per column, the content tokens that identify IT and not its neighbours.

    A shared token is no evidence: 'scheme' appears in Scheme Name, Scheme
    Number and Scheme Reference, and 'policy' in half the dictionary, so a
    target description mentioning either matched three columns at once and
    flagged six correct mappings as composition gaps. Only a token unique to one
    column within this dictionary can say the description means THAT column.
    """
    freq: dict[str, int] = {}
    per: dict[str, set[str]] = {}
    for name, col in by_name.items():
        toks = set()
        for phrase in [col.business_name, *(getattr(col, "aliases", None) or [])]:
            toks |= _content(phrase)
        per[name] = toks
        for t in toks:
            freq[t] = freq.get(t, 0) + 1
    return {n: {t for t in toks if freq.get(t, 0) == 1} for n, toks in per.items()}


def _value_evidence(src: EnrichedColumn, target: dict) -> bool:
    """True when decoded values back the match, which is real evidence."""
    v = _value_overlap(src, target)
    return v is not None and v > 0.0


def _shared_vocabulary(by_name: dict, target_dict: dict) -> set[str]:
    """Tokens too common, on EITHER side, to identify anything.

    In a policy-master migration almost every column is about a policy, so
    "policy" carries no information — yet it is what sent
    `policy_unpaid_count` ("months since the last premium paid") to
    `policy_number` instead of `premium_arrears_months`, beating a correct
    match that shared no name token at all.

    A token is shared vocabulary when it appears in a third or more of the
    columns of either dictionary. Computed per run from the two dictionaries
    actually supplied, never a fixed word list, so it adapts to a claims or
    member extract without configuration.
    """
    def freq(phrases: list[str]) -> dict:
        out: dict[str, int] = {}
        for ph in phrases:
            for tok in _content(ph):
                out[tok] = out.get(tok, 0) + 1
        return out

    src_names = [c.business_name for c in by_name.values()]
    tgt_names = [a["name"] for a in target_attributes(target_dict)]
    shared = set()
    for names in (src_names, tgt_names):
        if not names:
            continue
        threshold = max(2, len(names) // 3)
        shared |= {t for t, n in freq(names).items() if n >= threshold}
    return shared


def _evidence_tokens(src: EnrichedColumn, target: dict) -> set[str]:
    """The tokens the name match actually rests on."""
    phrases = [src.business_name, *(getattr(src, "aliases", None) or []),
               getattr(src, "description", "") or ""]
    st: set[str] = set()
    for ph in phrases:
        st |= _content(ph)
    return st & (_content(target["name"]) | _content(target.get("description", "")))


def _derivation_gap(target: dict, sql: str, srcs: list[str],
                    by_name: dict) -> list[str]:
    """Reasons the target describes work this SQL demonstrably does not do."""
    if not _is_bare_copy(sql):
        return []
    reasons: list[str] = []
    desc = (target.get("description") or "")
    t_tokens = _tokens(f"{target['name']} {desc}")

    # (a) COMPOSITION: the target's own description names other source columns.
    #     full_name says "Forename Surname"; age_at_commencement says "date of
    #     birth and commencement". Both are in the dictionary, both unused.
    referenced = []
    distinctive = _distinctive_tokens(by_name)
    for name, col in by_name.items():
        if name in srcs:
            continue
        # match on BUSINESS vocabulary, not the raw column name: a target
        # description says "date of birth", never "BIRTHDT", so including the
        # physical name in the terms made the subset test impossible to satisfy
        for phrase in [col.business_name, *(getattr(col, "aliases", None) or [])]:
            terms = _content(phrase)
            if not terms or not (terms <= t_tokens):
                continue
            if not (terms & distinctive.get(name, set())):
                continue        # only shared vocabulary matched — no evidence
            referenced.append(col.business_name or name)
            break
    if referenced:
        reasons.append(
            f"the target description also refers to "
            f"{', '.join(sorted(referenced))}, which this transform does not read")

    # (b) UNIT / SCALE
    unit = _unit_conflict(target, srcs, by_name)
    if unit:
        reasons.append(unit + ", and no conversion is applied")

    # (c) EXPLICIT FORMAT INSTRUCTION
    low = desc.lower()
    hit = next((h for h in _FORMAT_HINTS if h in low), None)
    if hit:
        reasons.append(f"the target asks for a specific format ('{hit}...') that "
                       f"this transform does not produce")
    return reasons


def _synth(target: dict, src: EnrichedColumn) -> tuple[str, str, list[str]]:
    """Build the SQL and a PLAIN-ENGLISH note describing what it does.

    The note is read by business reviewers and executives, not only engineers,
    so it says what happens to the data rather than naming the mechanism.
    "Decode 0 XA22 code(s) to target enum; 2 unmapped (C, U)" told a reader
    almost nothing — worst of all, it buried the fact that a zero-decode
    mapping produces an empty column for every single row.
    """
    col, ttype = src.name, target["type"]
    allowed = target.get("allowed_values")

    if allowed and src.value_decode:  # code -> target enum
        norm_allowed = {_norm(v): v for v in allowed}
        mapped, unmapped, by_code = {}, [], 0
        for code, lbl in src.value_decode.items():
            # Match on the decoded LABEL first ('Surrender' -> 'SURRENDER'),
            # then fall back to the RAW CODE. A target legitimately sometimes
            # keeps the legacy codes themselves as its allowed values (the new
            # platform stores 'C'/'U' rather than re-coding them). Comparing
            # only the label made that case unmappable: every code was reported
            # unmatched even though the target listed those exact codes.
            tv = norm_allowed.get(_norm(lbl))
            if tv is None:
                tv = norm_allowed.get(_norm(code))
                if tv is not None:
                    by_code += 1
            if tv:
                mapped[code] = tv
            else:
                unmapped.append(code)
        # SQL string literals must be escaped. A target enum value containing an
        # apostrophe ("O'Brien Scheme TPD") produced WHEN 'TPD' THEN 'O'Brien...'
        # — invalid SQL. The agent validates by EXECUTING, so execution failed,
        # coverage came back None, confidence collapsed and a perfectly good
        # mapping was silently rejected. Safe (no corrupt data) but wrong, and
        # the rationale gave no clue why.
        whens = " ".join(f"WHEN {_lit(c)} THEN {_lit(v)}" for c, v in mapped.items())
        # keep the CASE even for a code-for-code match: it still enforces the
        # target's allowed list, so an unexpected new code cannot slip through
        sql = f'CASE "{col}" {whens} ELSE NULL END' if whens else f'"{col}"'
        total = len(mapped) + len(unmapped)
        codes = ", ".join(unmapped)
        if not mapped:
            # the important case: NOTHING translates, so every row lands empty
            note = (f"No {col} value matches the target list — "
                    f"{codes} have no equivalent, so every row would be empty.")
        elif unmapped:
            note = (f"Translate {col} to the target list: "
                    f"{len(mapped)} of {total} values match; "
                    f"{codes} {'has' if len(unmapped) == 1 else 'have'} no "
                    f"equivalent and would be left empty.")
        elif by_code == total:
            # the target stores the legacy codes themselves
            note = (f"The target keeps the source system's {col} codes, so all "
                    f"{total} are carried across unchanged.")
        else:
            note = f"Translate all {total} {col} values to the target list."
        return sql, note, unmapped

    if ttype == "date":
        return (f"CASE WHEN \"{col}\" IN ('00000000','') THEN NULL "
                f"ELSE strptime(\"{col}\", '%Y%m%d')::DATE END",
                'Read the YYYYMMDD text as a date; 00000000 means "no date" '
                "in the source system and becomes null.", [])
    if ttype in ("decimal", "number", "numeric"):
        return f'TRY_CAST("{col}" AS DOUBLE)', "Convert the text to a number.", []
    if src.inferred_type == "FREE_TEXT":
        # NOTE: the SQL is unchanged — NULLIF still turns a blank into NULL.
        # The note simply stops describing it: "blanks become empty" was
        # circular and told a business reader nothing. The exact SQL remains
        # visible on hover and in the review card.
        return (f'NULLIF(TRIM("{col}"), \'\')',
                "Copy across, trimming spaces.", [])
    return f'NULLIF("{col}", \'\')', "Copy across as-is.", []


def _derived(target: dict, by_name: dict[str, EnrichedColumn]) -> Optional[tuple]:
    """Heuristics for many:1 / derived targets that no single source covers."""
    tn = target["name"].lower()
    # annualised premium = amount * frequency multiplier
    if "annual" in tn and target["type"] in ("decimal", "number"):
        amt = next((c for c in by_name.values() if "premium" in c.business_name.lower()
                    and c.inferred_type in ("DECIMAL", "INTEGER")), None)
        frq = next((c for c in by_name.values()
                    if set(c.value_decode) & {"M", "Q", "A", "H", "SP"}), None)
        if amt and frq:
            mult = "CASE \"%s\" WHEN 'M' THEN 12 WHEN 'Q' THEN 4 WHEN 'H' THEN 2 " \
                   "WHEN 'A' THEN 1 ELSE 0 END" % frq.name
            sql = f'TRY_CAST("{amt.name}" AS DOUBLE) * ({mult})'
            return ([amt.name, frq.name], "many:1", sql,
                    f"Annualise {amt.business_name} using {frq.business_name}.", [])
    # boolean group flag derived from product code
    if target["type"] == "boolean" and "group" in tn:
        prod = next((c for c in by_name.values()
                     if any("group" in v.lower() for v in c.value_decode.values())), None)
        if prod:
            codes = [k for k, v in prod.value_decode.items() if "group" in v.lower()]
            inlist = ", ".join(_lit(c) for c in codes)
            sql = f'CASE WHEN "{prod.name}" IN ({inlist}) THEN true ELSE false END'
            return ([prod.name], "derived", sql,
                    f"True when {prod.business_name} indicates a group scheme.", [])
    return None


def _is_system(target: dict) -> bool:
    return bool(re.search(r"migrat|source_system|created|updated|loaded",
                          target["name"], re.I))


# ------------------------------------------------------------- validation
def _populated_predicate(cols: list[str]) -> str:
    # no source column -> a constant / load-time default: every row qualifies.
    if not cols:
        return "TRUE"
    return " OR ".join(f"(\"{c}\" IS NOT NULL AND \"{c}\" NOT IN ('00000000',''))" for c in cols)


def _validate(wh: Warehouse, table: str, sql: str, source_cols: list[str]) -> Optional[float]:
    try:
        denom = wh.con.execute(
            f'SELECT count(*) FROM "{table}" WHERE {_populated_predicate(source_cols)}'
        ).fetchone()[0]
        num = wh.con.execute(
            f'SELECT count(*) FROM "{table}" WHERE ({sql}) IS NOT NULL '
            f'AND ({_populated_predicate(source_cols)})'
        ).fetchone()[0]
    except Exception:
        return None
    return (num / denom) if denom else 1.0


# ------------------------------------------------------------------ the agent
# Tunable matcher thresholds. Confidence is on a 0–1 scale; see models.py for the
# auto/review/reject bands. These govern *which source* gets proposed and how the
# match is judged semantically, before the confidence band is applied.
PROPOSE_FLOOR = 0.12      # below this, no source even resembles the target -> unmapped
AMBIG_PLAUSIBLE = 0.45    # a rival candidate this strong is genuinely plausible
AMBIG_MARGIN = 0.12       # ...and within this of the best -> the choice is ambiguous
LOW_SIGNAL = 0.34         # best candidate weaker than this -> can't be trusted -> reject


def map_to_target(enriched: EnrichedDictionary, target_dict: dict,
                  warehouse: Warehouse, source_table: str) -> MappingSpec:
    by_name = {c.name: c for c in enriched.columns}

    def _dead(c: EnrichedColumn) -> bool:
        # constant / filler columns carry no per-record meaning, so they can't be a
        # legitimate source for any target. Trust the analyst's determination.
        if c.inferred_type in ("CONSTANT", "EMPTY"):
            return True
        blob = (c.description + " " + " ".join(getattr(c, "evidence", []) or [])).lower()
        return any(k in blob for k in ("constant", "filler", "no information"))

    candidates = [c for c in enriched.columns if not _dead(c)]
    # computed once per run from the dictionaries actually supplied
    shared_vocab = _shared_vocabulary(by_name, target_dict)
    used: set[str] = set()
    mappings: list[MappingEntry] = []
    unmapped_target: list[dict] = []

    for target in target_attributes(target_dict):
        if _is_system(target):
            unmapped_target.append({"attribute": target["name"],
                                    "reason": "system-generated / default at load (no source)."})
            continue

        alternatives: list[dict] = []
        ambiguous = False
        derived = _derived(target, by_name)
        if derived:
            srcs, card, sql, note, unmapped_codes = derived
            signal = 0.6                       # derived rules have a real, explainable basis
        else:
            ranked = sorted(((_composite(c, target), c) for c in candidates),
                            key=lambda x: x[0], reverse=True)
            best_score, best = ranked[0]
            if best_score < PROPOSE_FLOOR:     # truly nothing resembles this target
                unmapped_target.append({"attribute": target["name"],
                                        "reason": "no source attribute resembles this target (needs human input).",
                                        "_target": target})       # kept for LLM escalation
                continue
            signal = best_score
            # a boolean target with no boolean-like source anywhere has no honest
            # match — don't surface a spurious id/name/amount as a rejected
            # a boolean target with no boolean-like source has no honest match —
            # don't surface a spurious id/name/amount as a rejected candidate,
            # mark it unmapped so a human (or the LLM step) supplies one. The same
            # applies when the only boolean-shaped sources share no name/semantic
            # ground with the target (a contact-trace flag is not a vulnerability
            # flag just because both are two-valued): require real name overlap.
            if target["type"] == "boolean" and (
                not _bool_like(best) or _name_sim(best, target) <= 0.0
            ):
                unmapped_target.append({"attribute": target["name"],
                                        "reason": "no boolean/flag source resembles this target (needs human input).",
                                        "_target": target})       # kept for LLM escalation
                continue
            # competing candidates: other plausible sources close to the best
            for sc, c in ranked[1:4]:
                if sc >= AMBIG_PLAUSIBLE and (best_score - sc) <= AMBIG_MARGIN:
                    alternatives.append({"source": c.name, "business_name": c.business_name,
                                         "score": round(sc, 3)})
            ambiguous = bool(alternatives)
            sql, note, unmapped_codes = _synth(target, best)
            srcs, card = [best.name], "1:1"

        used.update(srcs)
        coverage = _validate(warehouse, source_table, sql, srcs)
        conf = round(min(1.0, 0.5 * signal + 0.5 * (coverage if coverage is not None else 0.0)), 3)
        if ambiguous:
            conf = round(min(conf, 0.82), 3)   # can't auto-accept while the source is in doubt

        # a source field that is itself CALCULATED in the legacy code (loyalty
        # bonus, penalties, ...) carries embedded business logic — the mapping is
        # a pass-through, but a human must confirm the extracted calculation
        # matches what the target attribute is defined to hold.
        calc_src = next((by_name[s] for s in srcs
                         if getattr(by_name.get(s), "derivation_cobol", None)), None)

        # the target describes work this SQL does not do — the synthesiser has
        # no composite repertoire, so it quietly copied one column instead
        gap_reasons = _derivation_gap(target, sql, srcs, by_name)

        # A match whose only evidence is vocabulary shared across the whole
        # dictionary is not evidence. Applied as a GATE CAP rather than a score
        # penalty: the deterministic layer cannot tell a lucky token from a real
        # one, so it must not auto-accept on one — but demoting to review is
        # safe, whereas re-scoring risks displacing correct matches elsewhere.
        thin_evidence = False
        if best is not None and card == "1:1" and not _value_evidence(best, target):
            ev = _evidence_tokens(best, target)
            thin_evidence = bool(ev) and ev <= shared_vocab

        gate = decide_gate(conf)
        # gate caps reflect *semantic* trust, not just whether the SQL runs:
        if signal < LOW_SIGNAL:                # nothing confirms this is the right column
            gate = Gate.REJECT
        elif ambiguous and gate == Gate.AUTO_ACCEPT:
            gate = Gate.REVIEW
        elif (unmapped_codes or card in ("many:1", "derived")) and gate == Gate.AUTO_ACCEPT:
            gate = Gate.REVIEW
        elif calc_src and gate == Gate.AUTO_ACCEPT:
            gate = Gate.REVIEW
        elif (card == "1:1" and _code_only_enum_match(target, best)
              and gate == Gate.AUTO_ACCEPT):
            # the codes line up but nothing says the MEANING does
            gate = Gate.REVIEW
        if thin_evidence and gate == Gate.AUTO_ACCEPT:
            gate = Gate.REVIEW
        if gap_reasons and gate == Gate.AUTO_ACCEPT:
            # NOT an elif: a derivation gap outranks every other cap. Every
            # downstream check can pass on a value that is well-formed and
            # wrong, so this must never reach auto-accept.
            gate = Gate.REVIEW

        rationale = note
        if thin_evidence:
            rationale += (" Matched only on vocabulary common to this dictionary "
                          f"({', '.join(sorted(_evidence_tokens(best, target)))}), "
                          "which does not identify a column — confirm the source "
                          "is the intended one.")
        if gap_reasons:
            conf = round(min(conf, 0.60), 3)     # honest about what is unknown
            rationale += (" This transform copies a single column, but "
                          + "; ".join(gap_reasons)
                          + ". A human must supply the derivation.")
        if card == "1:1" and _code_only_enum_match(target, best):
            rationale += (f" Matched because the target lists the same raw codes as "
                          f"{srcs[0]} in the source system, not because their meanings "
                          f"were confirmed — a human should verify this is the right field.")
        if ambiguous:
            alts = ", ".join(a["source"] for a in alternatives)
            rationale += (f" Ambiguous source: chose {srcs[0]}, but {alts} could equally be "
                          f"this target — a human must confirm which is correct.")
        if signal < LOW_SIGNAL:
            rationale += (f" Weak match: nothing in the data, COBOL or screen confirms {srcs[0]} "
                          f"means '{target['name']}'. Rejected pending a domain decision.")
        if unmapped_codes:
            # the transformation note already names the unmatched codes and says
            # what happens to them, so don't restate it — just ask for the
            # decision, and never render a raw Python list at a human
            rationale += (" A business decision is needed on "
                          + ("that code" if len(unmapped_codes) == 1 else "those codes")
                          + " before load.")
        if calc_src:
            rationale += (f" Source {calc_src.name} is itself calculated in COBOL "
                          f"({calc_src.derived_in_program}); the legacy expert extracted the "
                          f"business calculation — a human should confirm it matches the "
                          f"target definition. Extracted logic: "
                          f"{getattr(calc_src, 'derivation', None)}")

        mappings.append(MappingEntry(
            target_attribute=target["name"], source_attributes=srcs, cardinality=card,
            transformation_sql=sql, transformation_note=note,
            confidence=conf, gate=gate.value, validation_coverage=coverage,
            unmapped_codes=unmapped_codes, alternatives=alternatives, ambiguous=ambiguous,
            rationale=rationale,
            deterministic_confidence=conf, match_source="deterministic",
            derivation_gap=gap_reasons,
        ))

    unmapped_source = [
        {"attribute": c.name, "business_name": c.business_name,
         "reason": ("structural filler / no business meaning" if c.business_name.lower().startswith("filler")
                    else "control/constant field — not migrated" if "constant" in c.description.lower()
                    else "no target attribute")}
        for c in enriched.columns if c.name not in used
    ]

    gates = [m.gate for m in mappings]
    stats = {
        "target_attributes": len(target_attributes(target_dict)),
        "mapped": len(mappings),
        "auto_accept": gates.count("auto_accept"),
        "review": gates.count("review"),
        "reject": gates.count("reject"),
        "unmapped_target": len(unmapped_target),
        "unmapped_source": len(unmapped_source),
    }
    spec = MappingSpec(
        source_table=enriched.table, target_table=target_table_name(target_dict),
        mappings=mappings, unmapped_source=unmapped_source,
        unmapped_target=unmapped_target, stats=stats,
    )
    # LLM escalation tier: only the mappings the deterministic layer could NOT
    # place confidently (everything below auto-accept) plus unmapped targets.
    _llm_recover(spec, enriched, target_dict, warehouse, source_table, candidates, by_name)
    # second escalation tier: the MATCH may be right while the TRANSFORM is not
    _llm_synthesise(spec, enriched, target_dict, warehouse, source_table, by_name)
    # third tier: a second opinion on what the deterministic layer was CONFIDENT
    # about. It can only demote, never replace.
    _llm_verify(spec, enriched, target_dict, warehouse, source_table, by_name)
    _drop_self_alternatives(spec)
    _recompute_stats(spec, target_dict)
    _annotate_source_files(spec, by_name, enriched.table)
    return _narrate(spec, enriched, target_dict)


def _drop_self_alternatives(spec: MappingSpec) -> None:
    """Invariant: a mapping is never offered as its own competing candidate.

    Alternatives are recorded against whichever source was chosen at the time.
    Any later step that changes the chosen source (today that is LLM recovery,
    tomorrow it could be something else) can leave the previous list stale and
    produce a review card that reads 'chose COVSTDT; COVSTDT is an equally
    plausible match'. Enforce it once, here, rather than trusting every caller.
    """
    for m in spec.mappings:
        if not m.alternatives:
            continue
        chosen = set(m.source_attributes or [])
        alts = [a for a in m.alternatives if a.get("source") not in chosen]
        if len(alts) != len(m.alternatives):
            m.alternatives = alts
            m.ambiguous = bool(alts)


def _annotate_source_files(spec: MappingSpec, by_name: dict,
                           default_table: str) -> None:
    """Multi-source provenance: which physical file(s) each mapping reads.
    When a composite workset is in play, columns carry their origin; a mapping
    drawing on a joined-in file (or several files) says so explicitly."""
    for m in spec.mappings:
        files, display = [], []
        for s in m.source_attributes:
            c = by_name.get(s)
            t = (c.origin_table if c and c.origin_table else default_table)
            n = (c.origin_name if c and c.origin_name else s)
            if t not in files:
                files.append(t)
            display.append(f"{t}.{n}")
        m.source_files = files
        if len(files) > 1:
            m.transformation_note = (m.transformation_note + " " if m.transformation_note else "") \
                + f"Cross-file: combines {', '.join(display)} via the discovered join."
        elif files and files != [default_table] and default_table.startswith("__"):
            m.transformation_note = (m.transformation_note + " " if m.transformation_note else "") \
                + f"Sourced from joined file {files[0]}."
    spec.stats["cross_file_mappings"] = sum(
        1 for m in spec.mappings if len(m.source_files) > 1)


# ------------------------------------------------ LLM escalation (recovery) tier
_RECOVER_SYS = (
    "You are a data-migration mapping specialist with broad knowledge of how the "
    "same business concept is named across systems and countries (e.g. 'NI "
    "Number' = 'Tax File Number' = 'SSN' = national taxpayer id; 'commencement "
    "date' = 'policy start' = 'inception'; 'sum assured' = 'death benefit'). You "
    "are given a LIST of target attributes the deterministic matcher could not "
    "place, and the source columns with their business names, DESCRIPTIONS, "
    "types, decoded values AND sample values.\n"
    "CRITICAL: weigh the SAMPLE VALUES most heavily — they reveal the true "
    "meaning regardless of the column name. A national insurance / tax id looks "
    "like 'AB123456C' (2 letters, 6 digits, 1 letter); a policy or account "
    "number is a plain numeric run like '0000012345'; a person's name looks "
    "like 'SMITH, JOHN'; a date looks like '19860214'. Match each target to the "
    "source whose sample values have the SAME shape and meaning as the concept "
    "the target describes. Do not match a taxpayer identifier to a policy "
    "number just because both are identifiers. If nothing genuinely matches, "
    "return null for that target — do NOT force a match. Two different targets "
    "MAY legitimately resolve to different sources; judge each independently.\n"
    "Return ONLY JSON, one entry per target given, keyed by target name: "
    '{"proposals": {"<target name>": {"source": "<column id or null>", '
    '"reason": "<one sentence citing the sample values or evidence>"}}}.'
)

# How many targets to put in one recovery call. The source digest is identical
# for every target, so asking one-at-a-time re-sent ~1,200 tokens of candidate
# columns per call and cost one network round-trip each — 12 targets meant 12
# sequential calls and 25-50s of wall time. Batching collapses that to 1-2
# calls. Kept at 10 so a batch stays well inside the response budget and a
# single unparseable reply costs at most 10 targets, which the per-target
# fallback below then recovers individually.
_RECOVER_BATCH = 10


def _llm_recover(spec, enriched, target_dict, warehouse, source_table,
                 candidates, by_name):
    """For every mapping below auto-accept (and every unmapped target), ask the
    LLM to propose a semantically-equivalent source, then VALIDATE that proposal
    against real data exactly like the deterministic path. Recovered matches are
    capped at REVIEW — the human confirms the cross-vocabulary equivalence."""
    from .. import config
    client, model = config.llm_client()
    if client is None:
        return                                    # offline: behave exactly as before

    # sample VALUES are the strongest disambiguator across vocabularies — an NI
    # number 'AB098246B' identifies a taxpayer id far more reliably than the
    # business name alone. Include a few real samples per candidate.
    def _samples(col_name: str) -> list:
        try:
            rows = warehouse.con.execute(
                f'SELECT DISTINCT "{col_name}" FROM {source_table} '
                f'WHERE "{col_name}" IS NOT NULL AND trim("{col_name}") <> \'\' '
                f'LIMIT 4').fetchall()
            return [str(r[0]) for r in rows]
        except Exception:
            return []
    # The DESCRIPTION was missing from this payload: the tier that decides which
    # column matches was the one flying blind on the field most likely to carry
    # the answer, while the target side already sent its description.
    # screen_label went with the simplified dictionary and was always None.
    src_digest = [{"id": c.name, "business_name": c.business_name,
                   "description": getattr(c, "description", ""),
                   "type": c.inferred_type,
                   "sample_values": _samples(c.name),
                   "decodes": list((c.value_decode or {}).values())[:6]}
                  for c in candidates]

    def _ask(targets: list[dict]) -> dict:
        """One call proposing a source for EACH target given.

        Returns {target_name: (SourceColumn|None, reason)}. The per-target
        semantics are unchanged — every proposal is still validated against
        real data and shape-checked by the caller. Only the transport is
        batched.
        """
        if not targets:
            return {}
        payload = {"targets": [{"name": t["name"], "type": t["type"],
                                "description": t.get("description", ""),
                                "allowed_values": t.get("allowed_values")}
                               for t in targets],
                   "sources": src_digest}
        # ~120 output tokens per target, with headroom, so a full batch cannot
        # be truncated (finish_reason=length raises inside _llm_json)
        out = _llm_json(client, model,
                        [{"role": "system", "content": _RECOVER_SYS},
                         {"role": "user", "content": json.dumps(payload, default=str)}],
                        max_tokens=200 + 150 * len(targets))
        props = out.get("proposals") or {}
        resolved = {}
        for t in targets:
            p = props.get(t["name"]) or {}
            sid = p.get("source") if isinstance(p, dict) else None
            resolved[t["name"]] = ((by_name.get(sid) if sid in by_name else None),
                                   (p.get("reason", "") if isinstance(p, dict) else ""))
        return resolved

    def _resolve(targets: list[dict]) -> dict:
        """Batch the targets, degrading gracefully.

        A malformed reply to a batch would otherwise cost all 10 targets, so a
        failed batch is retried ONE TARGET AT A TIME — the same degradation
        isolation the legacy expert uses for per-rule narration. Worst case we
        are back to the old one-call-per-target behaviour; best case (the norm)
        we make one call instead of ten.
        """
        out = {}
        for i in range(0, len(targets), _RECOVER_BATCH):
            chunk = targets[i:i + _RECOVER_BATCH]
            try:
                out.update(_ask(chunk))
            except Exception:                                 # noqa: BLE001
                for t in chunk:
                    try:
                        out.update(_ask([t]))
                    except Exception:                         # noqa: BLE001
                        out[t["name"]] = (None, "")
        return out

    # ---- collect every target needing recovery BEFORE calling the model ----
    # (a) low-confidence existing mappings
    recover_mappings = []
    for m in spec.mappings:
        if m.gate == Gate.AUTO_ACCEPT.value:
            continue
        # A "many:1"/"derived" mapping (e.g. annualised premium = amount ×
        # frequency multiplier) is gated to REVIEW purely so a human signs
        # off on the BUSINESS LOGIC of the combination -- it is not a match
        # failure. The LLM recovery path can only propose a single plain
        # column (see _synth below), which is structurally the wrong SHAPE of
        # answer for a target that genuinely needs several source columns
        # combined. Its naive cast of just the raw amount scores deceptively
        # well on data coverage (the amount is populated for nearly every
        # row) and would silently win the confidence comparison below,
        # corrupting the value for every non-annual payment frequency. Never
        # let it touch an already-solved multi-column derivation.
        if m.cardinality in ("many:1", "derived"):
            continue
        tgt = next((t for t in target_attributes(target_dict)
                    if t["name"] == m.target_attribute), None)
        if tgt is None or _is_system(tgt):
            continue
        recover_mappings.append((m, tgt))

    # (b) targets left entirely unmapped
    recover_unmapped = [(u, u.get("_target")) for u in spec.unmapped_target
                        if u.get("_target") is not None]

    # one pass over the model for everything that needs it
    wanted, seen = [], set()
    for _, tgt in recover_mappings + recover_unmapped:
        if tgt["name"] not in seen:
            seen.add(tgt["name"]); wanted.append(tgt)
    proposals = _resolve(wanted)

    # ---- apply, with the SAME per-target validation as before --------------
    for m, tgt in recover_mappings:
        src, reason = proposals.get(tgt["name"], (None, ""))
        if src is None:
            continue
        sql, note, unmapped_codes = _synth(tgt, src)
        cov = _validate(warehouse, source_table, sql, [src.name])
        llm_conf = round(min(1.0, 0.5 * 0.75 + 0.5 * (cov if cov is not None else 0.0)), 3)
        # only surface the LLM path if it VALIDATES at least as well as the
        # deterministic attempt — the data must back the semantic proposal
        if cov is None or llm_conf <= (m.deterministic_confidence or 0):
            continue
        # shape guard: for known concepts (tax id, name, date...) the proposed
        # source's VALUES must actually look like that concept — catches a
        # confident but wrong identifier-to-identifier match that coverage misses
        ok, expected = _shape_ok(warehouse, source_table, src.name, tgt["name"])
        if not ok:
            m.rationale = (f"LLM proposed {src.name} ({src.business_name}) for "
                           f"{tgt['name']}, but its values do not match the "
                           f"expected shape ({expected}); left for human review.")
            continue
        m.llm_confidence = llm_conf
        m.llm_recovered = True
        m.match_source = "llm"
        # The competing candidates were computed against the DETERMINISTIC
        # winner. Recovery has just replaced that winner, so the list has to be
        # recomputed: drop the source we have now chosen (otherwise it is shown
        # competing with itself), and promote the source it displaced — that is
        # the genuine alternative the reviewer must decide between.
        prev = (m.source_attributes or [None])[0]
        alts = [a for a in (m.alternatives or []) if a.get("source") != src.name]
        if prev and prev != src.name and not any(a.get("source") == prev for a in alts):
            prev_col = by_name.get(prev)
            alts.insert(0, {"source": prev,
                            "business_name": (prev_col.business_name if prev_col else prev),
                            "score": m.deterministic_confidence or 0.0})
        m.alternatives = alts
        m.ambiguous = bool(alts)
        m.source_attributes = [src.name]
        m.transformation_sql, m.transformation_note = sql, note
        m.unmapped_codes = unmapped_codes
        m.validation_coverage = cov
        m.confidence = llm_conf
        m.gate = Gate.REVIEW.value                # recovered matches ALWAYS reviewed
        m.rationale = (f"LLM recovery: deterministic matching could not place this "
                       f"target (score {m.deterministic_confidence}). The model "
                       f"proposes source {src.name} ({src.business_name}) — {reason} "
                       f"Validated coverage {cov:.0%} on real data. A human must "
                       f"confirm this cross-vocabulary match before load.")

    still_unmapped = [u for u in spec.unmapped_target if u.get("_target") is None]
    for u, tgt in recover_unmapped:
        src, reason = proposals.get(tgt["name"], (None, ""))
        if src is None:
            still_unmapped.append(u); continue
        sql, note, unmapped_codes = _synth(tgt, src)
        cov = _validate(warehouse, source_table, sql, [src.name])
        ok, _exp = _shape_ok(warehouse, source_table, src.name, tgt["name"])
        if cov is None or cov < 0.5 or not ok:
            still_unmapped.append(u); continue
        llm_conf = round(min(1.0, 0.5 * 0.75 + 0.5 * cov), 3)
        spec.mappings.append(MappingEntry(
            target_attribute=tgt["name"], source_attributes=[src.name],
            cardinality="1:1", transformation_sql=sql, transformation_note=note,
            confidence=llm_conf, gate=Gate.REVIEW.value, validation_coverage=cov,
            unmapped_codes=unmapped_codes,
            deterministic_confidence=0.0, llm_confidence=llm_conf,
            llm_recovered=True, match_source="llm",
            rationale=(f"LLM recovery of a previously unmapped target. Proposes "
                       f"source {src.name} ({src.business_name}) — {reason} "
                       f"Validated coverage {cov:.0%}. Human confirmation required.")))
    # drop the internal _target key from whatever stays unmapped
    spec.unmapped_target = [{k: v for k, v in u.items() if k != "_target"}
                            for u in still_unmapped]


_SHAPE_HINTS = {
    # target concept keyword -> (regex the SOURCE values should broadly match, label)
    "tax": (r"^[A-Za-z]{2}\s?\d{6}\s?[A-Za-z]?$", "national insurance / tax id (AB123456C)"),
    "ni": (r"^[A-Za-z]{2}\s?\d{6}\s?[A-Za-z]?$", "national insurance number"),
    "insurance_number": (r"^[A-Za-z]{2}\s?\d{6}\s?[A-Za-z]?$", "national insurance number"),
    "name": (r"[A-Za-z]{2,},?\s+[A-Za-z]{2,}|[A-Za-z]{2,}\s+[A-Za-z]{2,}", "person name"),
    "date": (r"^\d{8}$|^\d{4}-\d{2}-\d{2}$", "date"),
    "birth": (r"^\d{8}$|^\d{4}-\d{2}-\d{2}$", "date"),
    "email": (r"^[^@\s]+@[^@\s]+$", "email address"),
    "postcode": (r"^[A-Za-z]{1,2}\d", "uk postcode"),
}


def _shape_ok(warehouse, source_table, src_col: str, target_name: str) -> tuple[bool, str]:
    """Best-effort shape agreement between a recovered source's values and the
    target concept. Returns (ok, expected_label). Unknown concepts pass (we do
    not over-constrain); known ones must broadly match or the match is suspect."""
    import re as _re
    tl = target_name.lower()
    hint = next((v for k, v in _SHAPE_HINTS.items() if k in tl), None)
    if hint is None:
        return True, ""
    pattern, label = hint
    try:
        rows = warehouse.con.execute(
            f'SELECT "{src_col}" FROM {source_table} '
            f'WHERE "{src_col}" IS NOT NULL AND trim("{src_col}") <> \'\' '
            f'LIMIT 20').fetchall()
    except Exception:
        return True, label
    vals = [str(r[0]).strip() for r in rows]
    if not vals:
        return True, label
    hits = sum(1 for v in vals if _re.match(pattern, v))
    return (hits / len(vals) >= 0.6), label


def _recompute_stats(spec, target_dict):
    gates = [m.gate for m in spec.mappings]
    spec.stats.update({
        "target_attributes": len(target_attributes(target_dict)),
        "mapped": len(spec.mappings),
        "auto_accept": gates.count("auto_accept"),
        "review": gates.count("review"),
        "reject": gates.count("reject"),
        "unmapped_target": len(spec.unmapped_target),
        "llm_recovered": sum(1 for m in spec.mappings if m.llm_recovered),
    })


# ----------------------------------------------------- LLM refinement (optional)
def _narrate(spec: MappingSpec, enriched: EnrichedDictionary, target_dict: dict) -> MappingSpec:
    from .. import config
    client, model = config.llm_client()
    if client is None:
        spec.generated_by = "deterministic+offline_stub"
        return spec

    sys = (
        "You are a data migration mapping specialist. Given an enriched source "
        "dictionary, a target dictionary, and a draft mapping spec (with validated "
        "coverage per mapping), improve the rationale and flag risks. Do not change "
        "transformations that already validate well. Return ONLY JSON with key "
        "'mappings' (list of {target_attribute, rationale})."
    )
    try:
        resp = client.chat.completions.create(
            model=model, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": json.dumps(
                          {"target": target_dict, "draft": spec.model_dump()}, default=str)}],
        )
        upd = {m["target_attribute"]: m.get("rationale", "")
               for m in json.loads(resp.choices[0].message.content).get("mappings", [])}
        for m in spec.mappings:
            if upd.get(m.target_attribute):
                m.rationale = upd[m.target_attribute]
        spec.generated_by = "deterministic+llm"
    except Exception as e:
        spec.stats["llm_note"] = f"refinement skipped: {e}"
        spec.generated_by = "deterministic+offline_stub"
    return spec

def _llm_synthesise(spec, enriched, target_dict, warehouse, source_table, by_name):
    """Escalation tier for the TRANSFORM, not the match.

    The deterministic synthesiser has a fixed single-column repertoire, so a
    target needing concatenation, unit conversion, date arithmetic or
    reformatting silently receives a copy of one column instead. _derivation_gap
    detects that; this asks the LLM to write the composite SQL.

    The LLM is not trusted — it is TESTED, exactly like the deterministic path:
      1. the proposal must reference only real source columns
      2. it must EXECUTE against the staged data
      3. it must produce a non-null rate at least as good as what it replaces
      4. it is capped at REVIEW, never auto-accept
    A proposal failing any step is discarded and the deterministic mapping
    stands, so this tier can only improve on what was there.
    """
    from .. import config
    client, model = config.llm_client()
    if client is None:
        return                                   # offline: nothing changes

    gapped = [m for m in spec.mappings
              if getattr(m, "derivation_gap", None) and m.gate != Gate.REJECT.value]
    if not gapped:
        return

    cols = warehouse.column_names(source_table)
    def _samples(name, n=3):
        try:
            rows = warehouse.con.execute(
                f'SELECT DISTINCT "{name}" FROM {source_table} '
                f'WHERE "{name}" IS NOT NULL AND trim("{name}") <> \'\' LIMIT {n}').fetchall()
            return [str(r[0]) for r in rows]
        except Exception:                        # noqa: BLE001
            return []

    src_digest = [{"column": c, "business_name": getattr(by_name.get(c), "business_name", c),
                   "description": getattr(by_name.get(c), "description", ""),
                   "samples": _samples(c)} for c in cols]
    by_target = {a["name"]: a for a in target_attributes(target_dict)}

    asks = [{"target": m.target_attribute,
             "target_type": by_target.get(m.target_attribute, {}).get("type", "string"),
             "target_description": by_target.get(m.target_attribute, {}).get("description", ""),
             "current_sql": m.transformation_sql,
             "why_insufficient": m.derivation_gap} for m in gapped]

    system = (
        "You write DuckDB SQL expressions for a data migration. For each target "
        "you are given the target's type and description, the source columns "
        "with sample values, and why the current expression is insufficient. "
        "Return ONE scalar SQL expression per target, referencing only the "
        "listed source columns, quoted with double quotes. No SELECT, no FROM, "
        "no trailing semicolon. If you cannot express it faithfully, return "
        "null for that target rather than guessing. Respond with JSON only: "
        '{"results": [{"target": "...", "sql": "...", "reason": "..."}]}')
    user = json.dumps({"source_columns": src_digest, "targets": asks}, default=str)

    try:
        out = _llm_json(client, model,
                        [{"role": "system", "content": system},
                         {"role": "user", "content": user}])
        results = {r.get("target"): r for r in (out.get("results") or [])}
    except Exception as exc:                     # noqa: BLE001
        spec.stats["llm_note"] = f"transform synthesis skipped: {exc}"
        return

    applied = 0
    for m in gapped:
        r = results.get(m.target_attribute) or {}
        proposed = (r.get("sql") or "").strip().rstrip(";")
        if not proposed:
            continue
        referenced = set(re.findall(r'"([^"]+)"', proposed))
        if not referenced or not referenced <= set(cols):
            continue                             # invented a column: discard
        if re.search(r"\b(select|from|insert|update|delete|drop|attach)\b",
                     proposed, re.I):
            continue                             # a statement, not an expression
        before = _validate(warehouse, source_table, m.transformation_sql,
                           m.source_attributes)
        after = _validate(warehouse, source_table, proposed, sorted(referenced))
        if after is None or (before is not None and after < before):
            continue                             # did not execute, or lost rows

        m.transformation_sql = proposed
        m.source_attributes = sorted(referenced)
        m.cardinality = "1:1" if len(referenced) == 1 else "many:1"
        m.validation_coverage = after
        m.llm_recovered = True
        m.match_source = "llm_transform"
        m.gate = Gate.REVIEW.value                # composite logic ALWAYS reviewed
        m.confidence = round(min(m.confidence + 0.15, 0.85), 3)
        m.rationale = (
            f"Composite transform proposed by the LLM because the deterministic "
            f"synthesiser could not express it: {'; '.join(m.derivation_gap)}. "
            f"{r.get('reason') or ''} Executed against the source data "
            f"({after:.0%} of rows produce a value). A human must confirm the "
            f"logic matches the target definition.").strip()
        applied += 1
    if applied:
        spec.stats["llm_transforms"] = applied

_VERIFY_SYS = (
    "You are auditing automated column mappings for a data migration. For each "
    "mapping you are given the source column (business name, description, type, "
    "sample values) and the target attribute (name, type, description). Judge "
    "ONE thing: could this source plausibly be the intended origin of this "
    "target?\n"
    "Be conservative. Approve unless the mapping is clearly wrong — a mismatch "
    "of MEANING, not of wording. Two columns describing the same fact in "
    "different vocabulary are correct. Object when the source measures a "
    "different quantity from the target (an identifier feeding a duration, an "
    "amount feeding a count, a status feeding a name), especially where the "
    "only thing they share is a word common to the whole system.\n"
    'Return ONLY JSON: {"verdicts": {"<target>": {"ok": true|false, '
    '"reason": "<one sentence, required when ok is false>"}}}'
)


def _llm_verify(spec, enriched, target_dict, warehouse, source_table, by_name):
    """Second opinion on AUTO-ACCEPTED mappings. Can only DEMOTE.

    The deterministic layer cannot detect this class of error itself: a match
    won on a word shared across the dictionary scores exactly as well as one
    won on meaning, so confidence is high precisely when it should not be. Only
    something that reads the two descriptions as language can tell them apart.

    Deliberately asymmetric, for the same reason the other tiers are: the model
    may raise a hand, never sign the certificate. Agreement changes nothing;
    disagreement moves the gate to REVIEW with the objection recorded. It never
    substitutes its own choice, so the worst case is additional human review and
    a correct mapping can never be replaced by a model's guess. Offline this is
    a no-op and the deterministic gates stand.
    """
    from .. import config
    client, model = config.llm_client()
    if client is None:
        return

    auto = [m for m in spec.mappings
            if m.gate == Gate.AUTO_ACCEPT.value and m.source_attributes]
    if not auto:
        return
    by_target = {a["name"]: a for a in target_attributes(target_dict)}

    def _samples(col, n=3):
        try:
            rows = warehouse.con.execute(
                f'SELECT DISTINCT "{col}" FROM {source_table} WHERE "{col}" '
                f"IS NOT NULL AND trim(\"{col}\") <> '' LIMIT {n}").fetchall()
            return [str(r[0]) for r in rows]
        except Exception:                        # noqa: BLE001
            return []

    payload = []
    for m in auto:
        src = by_name.get(m.source_attributes[0])
        tgt = by_target.get(m.target_attribute, {})
        payload.append({
            "target": m.target_attribute,
            "target_type": tgt.get("type", ""),
            "target_description": tgt.get("description", ""),
            "source_column": m.source_attributes[0],
            "source_business_name": getattr(src, "business_name", ""),
            "source_description": getattr(src, "description", ""),
            "source_type": getattr(src, "inferred_type", ""),
            "source_samples": _samples(m.source_attributes[0]),
        })

    try:
        out = _llm_json(client, model,
                        [{"role": "system", "content": _VERIFY_SYS},
                         {"role": "user", "content": json.dumps(
                             {"mappings": payload}, default=str)}],
                        max_tokens=200 + 120 * len(payload))
        verdicts = out.get("verdicts") or {}
    except Exception as exc:                     # noqa: BLE001
        spec.stats["llm_note"] = f"verification skipped: {exc}"
        return

    demoted = 0
    for m in auto:
        v = verdicts.get(m.target_attribute)
        if not isinstance(v, dict) or v.get("ok") is not False:
            continue                             # silence and approval both pass
        reason = (v.get("reason") or "").strip()
        if not reason:
            continue                             # an objection without a reason is not one
        m.gate = Gate.REVIEW.value
        m.confidence = round(min(m.confidence, 0.70), 3)
        m.rationale = (f"{m.rationale} Held for review: an independent check "
                       f"disputes this mapping — {reason}").strip()
        demoted += 1
    if demoted:
        spec.stats["llm_demotions"] = demoted

