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
GENERIC = {
    # Words that identify nothing on their own. TYPE-BEARING words are
    # deliberately NOT here: "amount", "indicator", "flag", "count" and "date"
    # are what distinguish a quantity from a flag from a timestamp. Stripping
    # them reduced "Loan Indicator" and "loan_amount" both to {loan}, giving a
    # perfect 1.00 name match between a two-valued code and a decimal — the type
    # veto is only 40% of the score and could not overcome it.
    #
    # "date" DOES stay: it distinguishes a kind, but in a dictionary of dates
    # every column carries it, so releasing it made EXITDT, UNPDDT and BIRTHDT
    # all rivals for inception_date. The general form of that argument is
    # document frequency, which _token_document_frequency already computes for
    # the split-date matcher; this list is the cheap approximation.
    # "number" also stays: it is an IDENTIFIER word, interchangeable with
    # "reference"/"id"/"ref", not a quantity word. Releasing it cost
    # "Scheme Number" its match to scheme_reference.
    "id", "no", "ref", "reference", "number", "code", "of", "the",
    "date", "datetime", "timestamp", "time", "value",
}


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


# Both dictionaries are HAND-AUTHORED, so any literal comparison against
# author-supplied vocabulary is a latent bug. A target declaring allowed_values
# IS coded whatever word its author chose for `type`; the spelling is a hint,
# the value domain is evidence.
_CODED_TARGET_TYPES = {"enum", "enumeration", "categorical", "category",
                       "code", "coded", "lookup", "picklist", "domain",
                       "list", "valuelist", "reference"}
_CODED_SOURCE_TYPES = {"CATEGORICAL_CODE", "CODE", "ENUM", "FLAG", "BOOLEAN",
                       "CATEGORICAL"}


def _is_coded_target(target: dict) -> bool:
    """True when the target declares a value domain.

    _type_compat used to test `tgt_type == "enum"` while _composite tested
    `target.get("allowed_values")`, so the two disagreed whenever a dictionary
    spelled the type differently — and an identical correct mapping dropped from
    1.00 to 0.40 on the word "categorical" alone.
    """
    if target.get("allowed_values"):
        return True
    return (target.get("type") or "").lower() in _CODED_TARGET_TYPES


def _target_value_index(target: dict) -> dict:
    """Normalised meaning -> the target value to EMIT.

    The source can say what its codes mean (value_decode); the target could not.
    So a source decoding {"1": "Yes"} against a target declaring ["Y", "N"]
    scored zero — both sides coded, neither able to say the codes meant the same
    thing. `value_meanings` on the target closes that:

        "allowed_values": ["Y", "N"],
        "value_meanings": {"Y": "Yes", "N": "No"}

    Maps to the value to EMIT, not to the meaning: emitting 'Yes' into a column
    that accepts only 'Y' would score well and produce invalid data. With no
    value_meanings this is the identity over allowed_values — exactly the
    previous behaviour.
    """
    idx = {_norm(v): v for v in (target.get("allowed_values") or [])}
    for value, meaning in (target.get("value_meanings") or {}).items():
        idx[_norm(meaning)] = value
    return idx


# Target families whose type genuinely restricts the value space. A source from
# another family is a contradiction here, not a weaker fit.
_CONSTRAINED_FAMILIES = {"decimal", "date"}


def _type_compat(src_type: str, tgt_type: str, target: dict | None, src: Optional[EnrichedColumn] = None) -> float:
    # Source inferred_type -> family. Dictionaries are HAND-AUTHORED, so the
    # vocabulary must cover what a person would naturally write: "AMOUNT" and
    # "DATE" are obvious choices that were absent, defaulted to "string", and
    # were then vetoed against numeric and date targets — rejecting two correct
    # mappings. A veto must fire on a known contradiction, never on our own
    # ignorance of a synonym.
    fam = {
        "IDENTIFIER": "string", "FREE_TEXT": "string", "TEXT": "string",
        "STRING": "string", "VARCHAR": "string", "CHAR": "string",
        "CATEGORICAL_CODE": "string", "CODE": "string", "ENUM": "string",
        "BOOLEAN": "string", "FLAG": "string",
        "DATE_YYYYMMDD": "date", "DATE": "date", "DATETIME": "date",
        "TIMESTAMP": "date", "TIME": "date",
        "DECIMAL": "decimal", "INTEGER": "decimal", "INT": "decimal",
        "NUMERIC": "decimal", "NUMBER": "decimal", "AMOUNT": "decimal",
        "FLOAT": "decimal", "DOUBLE": "decimal", "MONEY": "decimal",
        "CURRENCY": "decimal", "PERCENT": "decimal", "PERCENTAGE": "decimal",
        "COUNT": "decimal", "QUANTITY": "decimal",
        "EMPTY": "string", "CONSTANT": "string",
    }
    # Target type -> family. Anything not listed keeps its own name, so a type
    # spelled differently by the target dictionary ("integer", "int", "float")
    # must be normalised here or it silently escapes the constrained-family
    # check below and falls back to the lenient penalty.
    t = {"enum": "string", "varchar": "string", "text": "string", "char": "string",
         "number": "decimal", "numeric": "decimal", "float": "decimal",
         "double": "decimal", "money": "decimal",
         "integer": "decimal", "int": "decimal", "bigint": "decimal",
         "smallint": "decimal",
         "timestamp": "date", "datetime": "date", "time": "date"}
    # a boolean target needs boolean-like evidence — a flag or two-value coded
    # source. A customer id / name / amount landing in a boolean is a spurious
    # fit even when a shared word (e.g. "customer") lifts the name score.
    if tgt_type == "boolean":
        return 1.0 if (src is not None and _bool_like(src)) else 0.15
    # A CONSTRAINED target — one whose type restricts which values are even
    # representable — cannot be fed by an incompatible source. This is a
    # category error, not a near miss: a coded flag is not a decimal, an
    # identifier is not a date. The soft 0.4 penalty could be overcome by a
    # single shared word, which is how "Loan Indicator" (CATEGORICAL_CODE)
    # reached loan_amount (decimal) at 0.76, and POLNO reached
    # policy_start_date. Boolean and enum targets already had this protection;
    # numeric and temporal ones did not.
    #
    # Text targets deliberately keep the lenient penalty: any value is
    # representable as text, so an incompatible source there really is a near
    # miss rather than a contradiction.
    known = (src_type or "").upper() in fam
    src_fam, tgt_fam = fam.get((src_type or "").upper(), "string"), t.get(tgt_type, tgt_type)

    # A coded target is properly fed by a coded source. This MUST come before
    # the family comparison: sitting after it, a DECIMAL source exited at 0.40
    # while a genuine code column tagged "CODE" rather than "CATEGORICAL_CODE"
    # was penalised to 0.30 — a numeric scoring better against an enum than a
    # code does.
    if target is not None and _is_coded_target(target):
        if (src_type or "").upper() in _CODED_SOURCE_TYPES:
            return 1.0
        # numeric or temporal into a coded target is a category error, the same
        # judgement the constrained-family veto makes in the other direction
        return 0.30 if src_fam == "string" else 0.10

    if tgt_fam in _CONSTRAINED_FAMILIES and known:
        # only veto on a KNOWN contradiction: an unrecognised source type means
        # the dictionary used vocabulary we do not model, which is a reason to
        # be lenient rather than to reject
        return 1.0 if src_fam == tgt_fam else 0.10
    if src_fam != tgt_fam or not known:
        return 0.4
    return 1.0


def _value_overlap(src: EnrichedColumn, target: dict) -> Optional[float]:
    allowed = target.get("allowed_values")
    if not (allowed and src.value_decode):
        return None
    # the SAME index _synth uses, so scoring and SQL cannot disagree about
    # whether a code translates
    idx = _target_value_index(target)
    norm_allowed = set(idx)
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
    t = _type_compat(src.inferred_type, target["type"], target, src)
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
    if _is_coded_target(target):
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


# A failure in the per-target semantic call is recorded here for the run rather
# than swallowed. Two states used to return the same value — "no model
# configured" and "model configured but failing" — so a broken gateway was
# indistinguishable from offline: the agreement rule quietly stopped applying,
# every mapping fell back to the deterministic answer, and nothing said why.
# The call is made per target, so a failing endpoint also meant one dead
# round-trip per attribute; after the first failure the rest are skipped.
_LLM_PROPOSE_FAILURE: dict = {}
# target name -> (source, reason, has_view), filled by one batched call per run
_PROPOSE_CACHE: dict = {}
# True once a batch has completed without raising. After that, a target absent
# from the cache means the model HAD NO VIEW on it — re-asking per target would
# reinstate the 23 round-trips the batch exists to remove, and cost more than
# the unbatched version did.
_PROPOSE_STATE: dict = {"batched": False}
_PROPOSE_BATCH = 10


def _llm_propose_all(targets: list, by_name: dict, warehouse, source_table) -> None:
    """Ask for every target's semantic view in a few calls, not one per target.

    _RECOVER_SYS already accepts a LIST of targets and the payload shape already
    had a "targets" array — it was simply being sent one element at a time, so a
    23-attribute dictionary cost 23 sequential round-trips. At two to four
    seconds each that is a minute or more before anything appears, and with
    retries on a slow gateway it reads as a hang. _llm_recover has batched from
    the start; this now matches it.

    A failed or unparseable batch leaves those targets out of the cache, and the
    per-target path below still runs for them — so a bad batch costs accuracy
    for nobody, only the speed-up.
    """
    from .. import config
    try:
        client, model = config.llm_client()
    except Exception as exc:                    # noqa: BLE001
        _LLM_PROPOSE_FAILURE.setdefault("error", f"{type(exc).__name__}: {exc}")
        return
    if client is None:
        return

    def _samples(col_name: str) -> list:
        try:
            rows = warehouse.con.execute(
                f'SELECT DISTINCT "{col_name}" FROM {source_table} '
                f'WHERE "{col_name}" IS NOT NULL AND trim("{col_name}") <> \'\' '
                f"LIMIT 4").fetchall()
            return [str(r[0]) for r in rows]
        except Exception:                       # noqa: BLE001
            return []

    source_digest = [{
        "id": c.name,
        "business_name": c.business_name,
        "description": getattr(c, "description", ""),
        "type": c.inferred_type,
        "sample_values": _samples(c.name),
        "decodes": list((c.value_decode or {}).values())[:6],
    } for c in by_name.values()]

    for i in range(0, len(targets), _PROPOSE_BATCH):
        chunk = targets[i:i + _PROPOSE_BATCH]
        payload = {
            "targets": [{"name": t["name"], "type": t.get("type", ""),
                         "description": t.get("description", ""),
                         "allowed_values": t.get("allowed_values")}
                        for t in chunk],
            "sources": source_digest,
        }
        try:
            out = _llm_json(client, model,
                            [{"role": "system", "content": _RECOVER_SYS},
                             {"role": "user", "content": json.dumps(payload, default=str)}],
                            max_tokens=150 + 120 * len(chunk))
            proposals = out.get("proposals") or {}
        except Exception as exc:                # noqa: BLE001
            _LLM_PROPOSE_FAILURE.setdefault("error", f"{type(exc).__name__}: {exc}")
            return
        _PROPOSE_STATE["batched"] = True
        for t in chunk:
            pr = proposals.get(t["name"])
            if not isinstance(pr, dict):
                continue                        # no answer: fall through per-target
            sid = pr.get("source")
            reason = pr.get("reason") or ""
            if sid is None:
                _PROPOSE_CACHE[t["name"]] = (None, reason, False)
            elif by_name.get(sid) is not None:
                _PROPOSE_CACHE[t["name"]] = (by_name[sid].name, reason, True)
            else:
                _PROPOSE_CACHE[t["name"]] = (None, reason, False)


def _llm_propose_target_source(target: dict, by_name: dict[str, EnrichedColumn],
                              warehouse: Warehouse, source_table: str):
    """Ask the LLM for an independent semantic candidate for one target.

    Returns (source_name, reason, available) where `available` is:
      * None -> no LLM configured / offline, so the deterministic gate should
        remain authoritative.
      * True -> a semantic view was issued by the LLM.
      * False -> the LLM explicitly said no valid source.
    """
    from .. import config
    try:
        client, model = config.llm_client()
    except Exception as exc:                    # noqa: BLE001
        # llm_client() raises when a gateway is configured but unreachable —
        # that is a fault to report, not a licence to behave as if offline.
        _LLM_PROPOSE_FAILURE.setdefault("error", f"{type(exc).__name__}: {exc}")
        return None, "", None
    if client is None:
        return None, "", None                   # genuinely offline
    cached = _PROPOSE_CACHE.get(target["name"])
    if cached is not None:
        return cached                           # answered by the batched pass
    if _PROPOSE_STATE["batched"]:
        return None, "", False                  # batch ran and had no view on it
    if _LLM_PROPOSE_FAILURE.get("error"):
        return None, "", None                   # already failed; don't hammer it

    def _samples(col_name: str) -> list[str]:
        try:
            rows = warehouse.con.execute(
                f'SELECT DISTINCT "{col_name}" FROM {source_table} '
                f'WHERE "{col_name}" IS NOT NULL AND trim("{col_name}") <> "" '
                f'LIMIT 4').fetchall()
            return [str(r[0]) for r in rows]
        except Exception:                       # noqa: BLE001
            return []

    source_digest = [{
        "id": c.name,
        "business_name": c.business_name,
        "description": getattr(c, "description", ""),
        "type": c.inferred_type,
        "sample_values": _samples(c.name),
        "decodes": list((c.value_decode or {}).values())[:6],
    } for c in by_name.values()]
    payload = {
        "targets": [{
            "name": target["name"],
            "type": target["type"],
            "description": target.get("description", ""),
            "allowed_values": target.get("allowed_values"),
        }],
        "sources": source_digest,
    }
    try:
        out = _llm_json(client, model,
                        [{"role": "system", "content": _RECOVER_SYS},
                         {"role": "user", "content": json.dumps(payload, default=str)}],
                        max_tokens=250)
        proposals = out.get("proposals") or {}
        p = proposals.get(target["name"]) or {}
        if not isinstance(p, dict):
            return None, "", False
        sid = p.get("source")
        if sid is None:
            return None, (p.get("reason") or ""), False
        src = by_name.get(sid)
        if src is None:
            return None, (p.get("reason") or ""), False
        return src.name, (p.get("reason") or ""), True
    except Exception as exc:                    # noqa: BLE001
        _LLM_PROPOSE_FAILURE.setdefault("error", f"{type(exc).__name__}: {exc}")
        return None, "", None


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
        # Normalised meaning -> the value to EMIT, so a source decoding
        # {"1": "Yes"} against a target listing ["Y","N"] with
        # value_meanings {"Y": "Yes"} emits 'Y' and not 'Yes'.
        norm_allowed = _target_value_index(target)
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



_DATE_ROLE_WORDS = {
    "day": ("day", "dy", "dd"),
    "month": ("month", "mth", "mon", "mm"),
    "year": ("year", "yr", "yyyy", "ccyy"),
}
# stripped when deriving what a group is ABOUT, so "Month of Entry" and
# "Year of Entry" reduce to the same concept
_DATE_PART_NOISE = {"day", "dy", "dd", "month", "mth", "mon", "mm",
                    "year", "yr", "yyyy", "ccyy", "date", "dt", "dte", "of"}


def _date_role(col: EnrichedColumn) -> Optional[str]:
    """day / month / year, read from the BUSINESS NAME only.

    Never the description: legacy dictionaries cross-reference the sibling
    columns ("Month and year in other column X and Y"), which made every part
    match every role.
    """
    text = (col.business_name or "").lower()
    for role, words in _DATE_ROLE_WORDS.items():
        if any(re.search(rf"\b{w}\b", text) for w in words):
            return role
    # No fallback to the physical name's trailing letter: OB3250_TABCOD,
    # OB3250_HOWPD and OB3250_STORDD all end in D, and OB3250_CURRPREM,
    # OB3250_TERM and OB3250_BKACNAM end in M. A guess from one character
    # turned six unrelated columns into date parts.
    return None


def _token_document_frequency(by_name: dict) -> dict:
    """How many columns of this dictionary use each token.

    A token used everywhere ("policy", "premium") identifies nothing; one used
    twice ("cessation") identifies its columns almost by itself. Computed per
    run from the dictionary supplied, never a fixed list.
    """
    df: dict = {}
    for col in by_name.values():
        for tok in _prose(f"{col.business_name} {col.description}"):
            df[tok] = df.get(tok, 0) + 1
    return df


def _split_date_groups(by_name: dict) -> dict:
    """Concept tokens -> {role: column} for every set of split date parts.

    A group is the set of parts describing the same underlying date. The
    concept comes from the business name AND description with the role words
    removed, so "Day of Entry for the Policy/Commencement Date" and "Year of
    Entry ..." share {entry, policy, commencement} and group together, while
    "Month of Maturity" forms its own.
    """
    groups: dict = {}
    for col in by_name.values():
        if col.inferred_type not in ("INTEGER", "NUMBER", "INT", "NUMERIC"):
            continue
        role = _date_role(col)
        if role is None:
            continue
        # GROUP on the business name alone: it is written precisely
        # ("Month of Maturity"), so parts of the same date reduce to an
        # identical concept. Descriptions add shared words — "policy", "other",
        # "column" — and grouping on any overlap merged maturity and premium
        # expiry into the entry group, losing them entirely.
        key = frozenset(_prose(col.business_name or "") - _DATE_PART_NOISE)
        if not key:
            continue
        groups.setdefault(key, {}).setdefault(role, col)

    # matching a group to a TARGET is a different question from grouping, and
    # wants the richer vocabulary the descriptions carry
    for key, parts in groups.items():
        tokens = set(key)
        for col in parts.values():
            tokens |= (_prose(f"{col.business_name} {col.description}")
                       - _DATE_PART_NOISE)
        parts["_tokens"] = {t for t in tokens
                            if t.upper() not in {n.upper() for n in by_name}
                            and not t.startswith("ob3250")}
    return groups


def _derived(target: dict, by_name: dict[str, EnrichedColumn]) -> Optional[tuple]:
    """Heuristics for many:1 / derived targets that no single source covers."""
    tn = target["name"].lower()

    # ---- split dates: choose the RIGHT group, and the right part for each role
    #
    # The previous version tested for "month"/"year" in the business name OR the
    # description, then returned the FIRST pair whose tokens intersected — with
    # no reference to the target at all. Two failures followed on real data:
    #
    #   1. Descriptions cross-reference their siblings ("Day of Entry ... Month
    #      and year in other column OB3250_DOFEM and OB3250_DOFEY"), which is
    #      genuinely helpful to a human but made EVERY part qualify as both a
    #      month and a year. Roles are therefore read from the BUSINESS NAME
    #      only, where "Day of Entry" / "Month of Entry" / "Year of Entry" are
    #      unambiguous.
    #   2. Returning the first matching pair gave start_date, maturity_date,
    #      premium_cessation_date and paid_clear_to_date the SAME columns. The
    #      group is now scored against the target, so each date target gets the
    #      group that describes it.
    #
    # The two together produced MAKE_DATE(month, day, 1) -> 0002-01-01 for a
    # policy that commenced in February 1982, silently, on every date column.
    if target["type"] in ("date", "datetime", "timestamp"):
        groups = _split_date_groups(by_name)
        best, best_score = None, 0.0
        t_tokens = _prose(f"{target['name']} {target.get('description','')}")
        # Rarity weighting, not a plain count. premium_cessation_date overlaps
        # "premium, cessation" with the expiry group and "premium, paid" with
        # the last-premium-paid group — two tokens each, so a count ties and the
        # winner falls to column order. "cessation" appears in two columns of
        # this dictionary and "paid" in several, so the rare word decides, which
        # is the whole reason it was written.
        df = _token_document_frequency(by_name)
        for concept, parts in groups.items():
            if "year" not in parts:
                continue                    # a date without a year is not a date
            tokens = parts.get("_tokens") or set(concept)
            overlap = tokens & t_tokens
            if not overlap:
                continue
            score = sum(1.0 / df.get(t, 1) for t in overlap)
            if score > best_score:
                best, best_score = parts, score
        if best:
            year, month = best["year"], best.get("month")
            day = best.get("day")
            # Legacy extracts mark an absent date with 0, and MAKE_DATE(0,0,1)
            # THROWS ("Date out of range") rather than returning NULL — so the
            # whole transform failed to execute, coverage came back empty, and a
            # correct derivation was rejected. NULLIF turns the sentinel into a
            # NULL the date function tolerates; the day falls back to 01, which
            # is the stated convention for a month-and-year source.
            part = lambda c: f'NULLIF(TRY_CAST("{c.name}" AS INTEGER), 0)'
            m_sql = part(month) if month else "1"
            d_sql = f"COALESCE({part(day)}, 1)" if day else "1"
            sql = f"MAKE_DATE({part(year)}, {m_sql}, {d_sql})"
            cols = [c.name for c in (day, month, year) if c is not None]
            note = ("Construct a date from "
                    + ", ".join(c.business_name for c in (day, month, year) if c)
                    + ("" if day else ", assuming the day is 01"))
            return (cols, "many:1", sql, note, [])

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
    # Some source fields are integers or numeric, not text; a sentinel like
    # '00000000' should be treated as empty only when it is a string value.
    # Casting the value to VARCHAR makes the check safe across both text and
    # numeric staging layouts, and keeps the validation semantics unchanged for
    # the legacy date columns that use 00000000 as "no date".
    return " OR ".join(
        f'(TRY_CAST("{c}" AS VARCHAR) IS NOT NULL AND '
        f'TRY_CAST("{c}" AS VARCHAR) NOT IN (\'\', \'00000000\'))'
        for c in cols
    )


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
    _LLM_PROPOSE_FAILURE.clear()                # per-run, not per-process
    _PROPOSE_CACHE.clear()
    _PROPOSE_STATE["batched"] = False
    _llm_propose_all(target_attributes(target_dict), by_name, warehouse, source_table)
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
        best = None
        derived = _derived(target, by_name)
        if derived:
            srcs, card, sql, note, unmapped_codes = derived
            signal = 0.6                       # derived rules have a real, explainable basis
            best = by_name.get(srcs[0]) if srcs else None
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
                # A date PART is not a rival for a non-date target. "Month of
                # last premium paid" is an integer month, but it shares the word
                # "premium" with the `premium` target and scored within the
                # margin — so a correct money mapping was marked ambiguous,
                # capped at 0.82 and held for review. That in turn left
                # OB3250_CURRPREM unsettled, which is why policy_value and
                # loan_amount could keep reading it.
                if _date_role(c) is not None and target["type"] not in (
                        "date", "datetime", "timestamp"):
                    continue
                if sc >= AMBIG_PLAUSIBLE and (best_score - sc) <= AMBIG_MARGIN:
                    alternatives.append({"source": c.name, "business_name": c.business_name,
                                         "score": round(sc, 3)})
            ambiguous = bool(alternatives)
            sql, note, unmapped_codes = _synth(target, best)
            srcs, card = [best.name], "1:1"

        llm_source, llm_reason, llm_has_view = _llm_propose_target_source(
            target, by_name, warehouse, source_table)

        used.update(srcs)
        coverage = _validate(warehouse, source_table, sql, srcs)
        conf = round(min(1.0, 0.5 * signal + 0.5 * (coverage if coverage is not None else 0.0)), 3)
        # "Ambiguous" means THE DETERMINISTIC LAYER cannot choose between two
        # candidates — not that the answer is genuinely in doubt. When the
        # semantic tier, working from descriptions rather than token overlap,
        # independently picks the same column, the tie is broken by corroboration
        # from a different kind of evidence and the cap has done its job.
        #
        # account_holder_name is the case: "Bank Account Name" (0.700) and "Bank
        # Account Number" (0.625) sit 0.075 apart, inside the 0.12 margin, so a
        # correct mapping was held for review against a rival that could never be
        # a person's name. A reader of the two descriptions is not in any doubt.
        #
        # Strictly one-directional: corroboration lifts the cap, disagreement
        # never does. If the model prefers the RIVAL that is a stronger reason to
        # stop, and the existing disagreement path already handles it.
        corroborated = (ambiguous and llm_has_view is True and llm_source is not None
                        and best is not None and str(llm_source) == str(best.name))
        if ambiguous and not corroborated:
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
        elif ambiguous and not corroborated and gate == Gate.AUTO_ACCEPT:
            # `corroborated` means the semantic tier independently chose the same
            # column, so the tie is settled — see the confidence cap above. Both
            # caps have to lift together: lifting only the score left the mapping
            # at 0.85 and still gated to review, which reads as an inconsistency.
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

        if gate == Gate.AUTO_ACCEPT:
            if llm_has_view is None:
                gate = Gate.AUTO_ACCEPT
            elif not llm_has_view or llm_source is None:
                gate = Gate.REVIEW
            elif str(llm_source) != str(best.name):
                gate = Gate.REVIEW
            else:
                gate = Gate.AUTO_ACCEPT

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
        if ambiguous and corroborated:
            alts = ", ".join(a["source"] for a in alternatives)
            rationale += (f" {alts} scored close enough to be a rival, but an "
                          f"independent semantic check also chose {srcs[0]}, so "
                          f"the tie is resolved.")
        elif ambiguous:
            alts = ", ".join(a["source"] for a in alternatives)
            rationale += (f" Ambiguous source: chose {srcs[0]}, but {alts} could equally be "
                          f"this target — a human must confirm which is correct.")
        if gate == Gate.REVIEW and llm_has_view is True and llm_source is not None and str(llm_source) != str(best.name):
            rationale += (f" Semantic disagreement: deterministic matcher selected {best.name}, "
                          f"but the LLM semantic check preferred {llm_source} ({llm_reason}).")
        elif gate == Gate.REVIEW and llm_has_view is True and llm_source is None:
            rationale += " Semantic peer found no valid source for this target — review required."
        elif gate == Gate.REVIEW and llm_has_view is False:
            rationale += " The semantic peer explicitly found no valid source for this target — review required."
        elif gate == Gate.REVIEW and llm_has_view is None:
            rationale += " Offline mode: no LLM semantic corroboration available, so the deterministic match remains authoritative."
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
            llm_has_view=llm_has_view, llm_source=llm_source,
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
    # Settle contention FIRST. A target whose column is auto-accepted elsewhere
    # is unmapped here, and the escalation tiers below read UNMAPPED targets —
    # so running them first meant original_sum_assured was still holding
    # OB3250_SASSC when recovery looked, and by the time contention released it
    # every tier that could have proposed OB3250_SASSI had already finished. The
    # gap has to exist before anything can fill it.
    _resolve_source_contention(spec, by_name, target_dict, candidates)

    # LLM escalation tier: only the mappings the deterministic layer could NOT
    # place confidently (everything below auto-accept) plus unmapped targets.
    _llm_recover(spec, enriched, target_dict, warehouse, source_table, candidates, by_name)
    # second escalation tier: the MATCH may be right while the TRANSFORM is not
    _llm_synthesise(spec, enriched, target_dict, warehouse, source_table, by_name)
    # the column can be right while the CODE TRANSLATION is impossible
    # deterministically — no source label resembles a permitted target value
    _llm_map_values(spec, enriched, target_dict, warehouse, source_table, by_name)
    # third tier: a second opinion on what the deterministic layer was CONFIDENT
    # about. It can only demote, never replace.
    _llm_verify(spec, enriched, target_dict, warehouse, source_table, by_name)
    if _LLM_PROPOSE_FAILURE.get("error"):
        # Visible in the report and the UI: a configured model that failed is a
        # different situation from no model at all, and the reviewer needs to
        # know the agreement rule did not run.
        spec.stats["llm_error"] = (
            "semantic check unavailable — the deterministic result stands "
            "without a second opinion: " + _LLM_PROPOSE_FAILURE["error"])
    _drop_targets_the_model_rejects(spec)
    _drop_self_alternatives(spec)
    _recompute_stats(spec, target_dict)
    _annotate_source_files(spec, by_name, enriched.table)
    _list_unmapped_as_rejects(spec, target_dict)
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



def _resolve_source_contention(spec: MappingSpec, by_name: dict, target_dict: dict,
                               candidates: list) -> None:
    """One source column claimed by several targets is usually an error.

    Every target picks its best source INDEPENDENTLY, so nothing stops one
    column winning twice while another goes unused. On a real policy extract
    OB3250_SASSC ("Sum Assured") took both sum_assured (1.00) and
    original_sum_assured (0.90 — AUTO-ACCEPTED), while OB3250_SASSI ("Initial
    Sum Assured when the policy was taken out"), which is what
    original_sum_assured actually wants, was never used at all. It loses on name
    similarity only because its business name is longer and more precise, which
    is backwards.

    Reuse is not banned — a column can legitimately feed two attributes — but
    the second claimant must justify itself. The strongest claim keeps the
    column; each other claimant is re-ranked over the sources nobody has taken.
    If a viable alternative exists it is adopted and held for REVIEW, because a
    reassignment is exactly the kind of decision a human should confirm. If none
    exists the mapping stands, demoted to review and told why.
    """
    by_target = {a["name"]: a for a in target_attributes(target_dict)}
    dropped: list = []
    single = [m for m in spec.mappings
              if m.cardinality == "1:1" and len(m.source_attributes or []) == 1]
    claims: dict = {}
    for m in single:
        claims.setdefault(m.source_attributes[0], []).append(m)

    for src, contenders in claims.items():
        if len(contenders) < 2:
            continue
        contenders.sort(key=lambda m: (m.confidence, m.target_attribute), reverse=True)
        winner = contenders[0]
        for m in contenders[1:]:
            target = by_target.get(m.target_attribute)
            if target is None:
                continue
            # every column already in use, not just the 1:1 ones — the date
            # parts feeding start_date are spoken for even though that mapping
            # is many:1, and treating them as free offered "Day of Entry" to
            # claim_reference_number
            taken = {c for x in spec.mappings if x is not m
                     for c in (x.source_attributes or [])}
            free = [c for c in candidates if c.name not in taken]
            ranked = sorted(((_composite(c, target), c) for c in free),
                            key=lambda x: x[0], reverse=True)
            # Reassign only when the unclaimed column is at least as good as
            # the contested one. This pass redistributes; it cannot repair a
            # SCORING error, and trying made things worse: original_sum_assured
            # and policy_value score within a whisker of each other on
            # OB3250_SASSI, so a greedy swap simply moved the mistake. Where the
            # alternative is weaker the mapping stands and the contention is
            # reported — a human resolving a flagged clash beats the tool
            # guessing between two near-equal candidates.
            # A column AUTO-ACCEPTED for another target is settled: nothing
            # weaker may also read it. That is the decisive signal, and it is
            # narrow enough to be safe — only a contested target is re-ranked,
            # so nothing else in the specification moves. Where the winner is
            # itself only a review-grade match the contest is genuine, and the
            # mapping is kept and flagged rather than shuffled.
            settled = winner.gate == Gate.AUTO_ACCEPT.value
            if settled:
                # The column is spoken for by a confident claim, so this target
                # does not have it. Do NOT hunt for a substitute: searching the
                # unclaimed columns simply moves the error — policy_value took
                # OB3250_SASSI on 0.76 and displaced original_sum_assured, which
                # wanted the same column on 0.67 and is the one that actually
                # means it. Unmapping states the honest position and hands the
                # target to the recovery tier, which reads UNMAPPED targets and
                # can propose what no score distinguishes.
                dropped.append(m)
                spec.unmapped_target.append({
                    "attribute": m.target_attribute,
                    "reason": (f"'{src}' is auto-accepted for "
                               f"'{winner.target_attribute}'; this target has no "
                               f"source of its own in this extract."),
                    "_target": target})
                continue
            good_enough = (ranked and ranked[0][0] >= AMBIG_PLAUSIBLE
                           and ranked[0][0] >= m.confidence - 0.02)
            if good_enough:
                score, col = ranked[0]
                sql, note, unmapped_codes = _synth(target, col)
                m.source_attributes = [col.name]
                m.transformation_sql, m.transformation_note = sql, note
                m.unmapped_codes = unmapped_codes
                m.confidence = round(min(score, 0.80), 3)
                m.gate = Gate.REVIEW.value
                m.rationale = (
                    f"{note} Reassigned: '{src}' is a stronger match for "
                    f"'{winner.target_attribute}' ({winner.confidence:.2f}), so this "
                    f"target was re-ranked over the unclaimed columns. Confirm the "
                    f"source is the intended one.")
            else:
                # No unclaimed column is plausible and the contested one belongs
                # to a stronger claim, so this target has no honest source. Say
                # so, rather than keeping a weak duplicate: OB3250_POLNO is the
                # policy number, and claim_reference_number is not a policy
                # number just because nothing better exists.
                #
                # Unmapping is also what gives the LLM its say. The escalation
                # tiers read UNMAPPED targets; a weak deterministic mapping
                # occupies the slot and pre-empts them entirely. Removing the
                # guess is what lets a semantic view be heard, and a human sees
                # an explicit gap instead of a plausible-looking wrong answer.
                # Do NOT unmap on the deterministic evidence alone. Scores
                # cannot separate the two situations: inception_date scores
                # 0.512 on COMMDT and IS commencement_date under another name,
                # while claim_reference_number scores 0.625 on POLNO and is not
                # a policy number at all. The legitimate reuse scores LOWER, so
                # no threshold divides them — only a reading of the two
                # descriptions does.
                #
                # So the contention is RECORDED, the gate capped at review, and
                # the mapping offered to the semantic tier below, which unmaps
                # it if the model agrees there is no source. Offline a human
                # sees a flagged clash, which is the honest outcome.
                m.gate = Gate.REVIEW.value
                m.confidence = round(min(m.confidence, 0.70), 3)
                m.contested_with = winner.target_attribute
                m.rationale = (
                    f"{m.rationale} Held for review: '{src}' is also claimed by "
                    f"'{winner.target_attribute}' at higher confidence "
                    f"({winner.confidence:.2f}), and no unclaimed column matches "
                    f"this target better. Confirm whether one column should feed "
                    f"both, or whether this target has no source.")

    if dropped:
        spec.mappings = [m for m in spec.mappings if m not in dropped]
        spec.stats["contested_unmapped"] = len(dropped)


def _drop_targets_the_model_rejects(spec: MappingSpec) -> None:
    """Unmap a target the semantic tier says has no source at all.

    Some targets simply have no source. claim_reference_number is not a policy
    number, and policy_value is not a premium — but both score above the floor
    on a column already auto-accepted elsewhere, so the deterministic layer
    keeps a plausible-looking wrong answer.

    Nothing in the scoring can settle this. On real dictionaries the LEGITIMATE
    reuse scores LOWER than the spurious one — inception_date scores 0.512 on
    COMMDT and genuinely is commencement_date under a second name, while
    claim_reference_number scores 0.625 on POLNO and is not a policy number at
    all — so no threshold separates them. Only a reading of the two descriptions
    does, which is what the semantic view is for.

    The rule is therefore: a contested mapping survives only if the semantic
    tier independently picked the same source. If the model looked and chose
    something else, or nothing, the target is unmapped — which also hands it to
    the recovery and synthesis tiers, since those read UNMAPPED targets and a
    weak mapping in the slot pre-empts them entirely. With no model configured
    nothing is dropped and the flagged review stands.
    """
    dropped = []
    for m in list(spec.mappings):
        if not getattr(m, "llm_has_view", False):
            continue                       # offline, or the model had no opinion
        # An explicit "no source" is the only verdict that unmaps. A model
        # preferring a DIFFERENT column is a disagreement, already handled by
        # capping the gate at review and recording both candidates — the tier
        # may raise a hand, not overrule.
        #
        # Widened beyond contested mappings because global assignment removes
        # the contention signal without removing the error: once policy_value
        # holds SASSI outright nothing is contested, yet "Initial Sum Assured"
        # is still not a surrender value, and no other tier looks at a
        # review-gated mapping.
        if m.llm_source is not None:
            continue
        if getattr(m, "cardinality", "1:1") != "1:1":
            continue                       # derived logic is judged on execution
        dropped.append(m)
        spec.unmapped_target.append({
            "attribute": m.target_attribute,
            "reason": ("an independent semantic check found no source column "
                       "for this target"
                       + (f"; '{m.source_attributes[0]}' is claimed by "
                          f"'{m.contested_with}'"
                          if getattr(m, "contested_with", None) else "")
                       + "."),
            "_target": getattr(m, "_target", None)})
    if dropped:
        spec.mappings = [m for m in spec.mappings if m not in dropped]
        spec.stats["contested_unmapped"] = len(dropped)



# An "absent concept" check once lived here — unmap a target whose defining
# word appears nowhere in the source vocabulary, the generalised form of a
# hand-written domain-term list. It is UNSAFE and was removed: "commencement"
# appears nowhere in the EFAS0042 dictionary (COMMDT is called "Policy Entry
# Date"), yet commencement_date <- COMMDT is a perfect match. A target word
# missing from the source is exactly the cross-vocabulary case the matcher
# exists to solve, not evidence the concept is absent.


# NOTE — a strict one-column-per-target assignment was tried here and REMOVED.
# Resolving contention by global score does fix the order dependence (premium
# keeps CURRPREM, inception_date moves to the better-fitting COVSTDT, and
# claim_reference_number is left unmapped because every plausible column is
# spoken for). But exclusivity is the wrong model for this domain: several
# targets legitimately read one column — a code feeding both a status and a
# derived flag, a date feeding both commencement_date and inception_date — and
# enforcing it starved the reference fixture from 19 mapped targets down to 11.
# A greedy "consume on first claim" variant fails the same way, only sooner,
# with the winner decided by position in the target dictionary.
#
# Contention is therefore REPORTED (see _resolve_source_contention) and settled
# semantically, not by an assignment rule.


def _list_unmapped_as_rejects(spec: MappingSpec, target_dict: dict) -> None:
    """Give every target attribute a row, including the ones with no source.

    An unmapped target used to exist only in spec.unmapped_target, so it
    disappeared from the mapping output entirely — the reviewer saw 19 rows for
    a 23-attribute target and had to notice the four that were missing. A
    target with no source is still a target someone must decide about, so it
    appears with an empty source and a REJECT gate, carrying the reason it was
    not mapped.

    spec.unmapped_target is left intact: the review queue, the load-time
    defaults in api/transform.py and the LLM recovery tiers all read it, and
    this is a presentation row, not a second source of truth. The reject gate
    also means the validator never materialises these (a rejected mapping is
    not executed), so no empty column is manufactured downstream.

    Runs last, after every LLM tier, so a target the model recovered is not
    also listed as unmapped.
    """
    already = {m.target_attribute for m in spec.mappings}
    by_target = {a["name"]: a for a in target_attributes(target_dict)}
    for u in spec.unmapped_target:
        name = u["attribute"] if isinstance(u, dict) else getattr(u, "attribute", None)
        if not name or name in already:
            continue
        reason = (u.get("reason") if isinstance(u, dict) else None) or "no source attribute matched."
        spec.mappings.append(MappingEntry(
            target_attribute=name,
            source_attributes=[],
            cardinality="1:1",
            transformation_sql="",
            transformation_note="",
            confidence=0.0,
            gate=Gate.REJECT.value,
            validation_coverage=None,
            unmapped_codes=[],
            alternatives=[],
            ambiguous=False,
            rationale=f"No source column for this target: {reason}",
            deterministic_confidence=0.0,
            match_source="unmapped",
            derivation_gap=[],
        ))
        already.add(name)
    # keep the report in target-dictionary order so the two line up
    order = {a["name"]: i for i, a in enumerate(target_attributes(target_dict))}
    spec.mappings.sort(key=lambda m: order.get(m.target_attribute, 10_000))

    # The gate counts were computed before these rows existed, so the summary
    # would have said "0 rejects" above a table showing four. Recomputed here,
    # the three gates now account for every target attribute exactly once;
    # unmapped_target stays as a SUBSET of reject — the rejects that have no
    # source at all, as opposed to one that was found and refused.
    gates = [m.gate for m in spec.mappings]
    spec.stats["auto_accept"] = gates.count(Gate.AUTO_ACCEPT.value)
    spec.stats["review"] = gates.count(Gate.REVIEW.value)
    spec.stats["reject"] = gates.count(Gate.REJECT.value)
    spec.stats["mapped"] = sum(1 for m in spec.mappings if m.source_attributes)


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

_SYNTH_SYS = (
    "You write DuckDB SQL expressions for a data migration. For each target "
    "you are given the target's type and description, the source columns "
    "with descriptions and sample values, and why the current expression is "
    "insufficient (which may be that no single column matched at all).\n"
    "\n"
    "Legacy systems routinely SPLIT one target value across several source "
    "columns. Combine them. Recurring patterns:\n"
    "  * SPLIT DATES — separate year, month and day columns. Where the day "
    "is absent, use 01: a month-and-year source means the first of that "
    "month. Where the month is absent and the target is a year-level date, "
    "use 01 for both. NEVER invent a year. Return NULL when a required "
    "component is missing, empty or zero.\n"
    "  * SPLIT NAMES — surname and forename into a full name, in the order "
    "the target's description specifies.\n"
    "  * AMOUNT PLUS FREQUENCY — annualise by the frequency multiplier "
    "(monthly x12, quarterly x4, half-yearly x2, annual x1).\n"
    "  * AMOUNT PLUS UNIT — convert where the units differ (pence to "
    "pounds is a division by 100).\n"
    "\n"
    "For a split date prefer make_date(y, m, d), casting each component "
    "with TRY_CAST first so bad data yields NULL rather than an error, "
    "e.g. make_date(TRY_CAST(\"YR\" AS INTEGER), "
    "TRY_CAST(\"MTH\" AS INTEGER), 1). Use strptime only when the "
    "components are text needing a format.\n"
    "\n"
    "Return ONE scalar SQL expression per target, referencing only the "
    "listed source columns, quoted with double quotes. No SELECT, no FROM, "
    "no trailing semicolon. If you cannot express it faithfully, return "
    "null for that target rather than guessing. Respond with JSON only: "
    '{"results": [{"target": "...", "sql": "...", "reason": "..."}]}')


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

    # A target can need COMPOSITION without a gap ever being flagged. A date
    # assembled from separate month and year columns is the clearest case: each
    # component scores badly against a date target (correctly — an integer month
    # is not a date), so the target arrives here REJECTED or UNMAPPED, never
    # "gapped", because there is no plausible single-column mapping for the gap
    # detector to compare against. Restricting this tier to gapped mappings left
    # the entire split-column class unreachable however good the prompt was.
    #
    # So three populations are offered:
    #   (a) gapped   — a copy where the target describes more work
    #   (b) rejected — nothing single-column was good enough
    #   (c) unmapped — no candidate cleared the floor at all
    # (b) and (c) have nothing to lose: any proposal that executes and produces
    # values is an improvement on a mapping that was going to be discarded.
    by_target = {a["name"]: a for a in target_attributes(target_dict)}
    gapped = [m for m in spec.mappings
              if (getattr(m, "derivation_gap", None) or m.gate == Gate.REJECT.value)]

    # (c) unmapped targets carry no mapping element, so build a placeholder the
    # same shape as the others; it is only kept if a proposal survives testing.
    placeholders = {}
    for u in spec.unmapped_target:
        name = u["attribute"] if isinstance(u, dict) else getattr(u, "attribute", None)
        if not name or name not in by_target or _is_system(by_target[name]):
            continue
        ph = MappingEntry(target_attribute=name, source_attributes=[],
                          cardinality="derived", transformation_sql="",
                          transformation_note="", confidence=0.0,
                          gate=Gate.REJECT.value,
                          rationale="No single source column matched.")
        placeholders[name] = ph
        gapped.append(ph)

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

    system = _SYNTH_SYS
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
        before = (_validate(warehouse, source_table, m.transformation_sql,
                            m.source_attributes)
                  if m.transformation_sql else None)
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
        # A placeholder starts at 0.0 and a reject at its (low) score, so
        # "+0.15" left a proposal that reproduces on every row sitting at 0.40 —
        # below the review threshold, which reads as a weak result for a mapping
        # that demonstrably works. Base it on MEASURED coverage instead, capped
        # so a composite always stops at review for a human to confirm.
        m.confidence = round(min(max(m.confidence + 0.15, 0.60 + 0.20 * after),
                                 0.80), 3)
        # the reason differs by population: a gapped mapping has a stated gap,
        # a rejected or unmapped one simply had no single-column answer
        why = ("; ".join(m.derivation_gap) if m.derivation_gap
               else "no single source column could express this target")
        m.rationale = (
            f"Composite transform proposed by the LLM because the deterministic "
            f"synthesiser could not express it: {why}. {r.get('reason') or ''} "
            f"Executed against the source data ({after:.0%} of rows produce a "
            f"value). A human must confirm the logic matches the target "
            f"definition.").strip()
        applied += 1
    for name, ph in placeholders.items():
        if ph.llm_recovered:                     # a proposal survived testing
            spec.mappings.append(ph)
            spec.unmapped_target = [
                u for u in spec.unmapped_target
                if (u["attribute"] if isinstance(u, dict)
                    else getattr(u, "attribute", None)) != name]
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
    "If the mapping is acceptable, include the same source column name in the "
    "result so a second check can confirm the candidate agrees with the "
    "deterministic match.\n"
    'Return ONLY JSON: {"verdicts": {"<target>": {"ok": true|false, '
    '"source": "<same source column name or null>", '
    '"reason": "<one sentence, required when ok is false>"}}}'
)


def _semantic_gate_decision(current_gate, deterministic_source, llm_source,
                           llm_has_view: bool | None):
    """Require semantic agreement for auto-accept when an LLM is configured.

    Offline mode is deliberately exempt: without a semantic peer, the
    deterministic layer remains authoritative. With an LLM configured, the
    agreement rule is enforced exactly as designed.
    """
    if current_gate != Gate.AUTO_ACCEPT.value:
        return current_gate
    if deterministic_source is None:
        return Gate.REVIEW.value
    if llm_has_view is None:
        return Gate.AUTO_ACCEPT.value
    if not llm_has_view or llm_source is None:
        return Gate.REVIEW.value
    if str(llm_source) == str(deterministic_source):
        return Gate.AUTO_ACCEPT.value
    return Gate.REVIEW.value


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
        if not isinstance(v, dict):
            # The model returned no verdict for this mapping. If the PROPOSE
            # tier already picked the same column, corroboration exists and
            # silence here must not undo it — otherwise a reply that simply
            # omits a few keys demotes correct, already-corroborated mappings,
            # and a model is under no obligation to echo back every target it
            # was sent.
            if (getattr(m, "llm_has_view", False) and m.llm_source
                    and m.source_attributes
                    and str(m.llm_source) == str(m.source_attributes[0])):
                continue
            # Otherwise: no semantic view at all => maintain the review
            # requirement for a second opinion. The deterministic layer may still
            # have a plausible source, but not enough corroboration.
            det_source = (m.source_attributes or [None])[0]
            new_gate = _semantic_gate_decision(m.gate, det_source, None, False)
            if new_gate != m.gate:
                m.gate = new_gate
                m.confidence = round(min(m.confidence, 0.70), 3)
                m.rationale = (f"{m.rationale} Held for review: no independent "
                               f"semantic corroboration was supplied.").strip()
                demoted += 1
            continue

        det_source = (m.source_attributes or [None])[0]
        llm_source = v.get("source")
        llm_ok = bool(v.get("ok") is not False)
        new_gate = _semantic_gate_decision(m.gate, det_source, llm_source, llm_ok)
        if new_gate == m.gate:
            continue

        m.gate = new_gate
        m.confidence = round(min(m.confidence, 0.70), 3)
        reason = (v.get("reason") or "").strip()
        if reason:
            m.rationale = (f"{m.rationale} Held for review: an independent check "
                           f"disputes this mapping — {reason}").strip()
        else:
            m.rationale = (f"{m.rationale} Held for review: the LLM did not "
                           f"corroborate the deterministic source choice.").strip()
        demoted += 1
    if demoted:
        spec.stats["llm_demotions"] = demoted


_VALUE_MAP_SYS = (
    "You translate legacy CODE VALUES onto a target system's permitted values "
    "for a data migration. For each mapping you are given the source column "
    "with its code->meaning table and row counts per code, and the target "
    "attribute with its description, permitted values and what each permitted "
    "value means.\n"
    "\n"
    "Decide, for EVERY source code, which permitted target value it becomes. "
    "Judge by MEANING, not by spelling: the two systems were designed "
    "separately and rarely share vocabulary. A target with two permitted values "
    "is usually a yes/no reduction of a richer source code set, so several "
    "source codes will collapse onto the same target value — that is expected, "
    "not an error.\n"
    "\n"
    "Return null for a code only when no permitted value can represent it. Do "
    "not invent a value outside the permitted list. Where a code is genuinely "
    "ambiguous, choose the reading the target's description supports and say so "
    "in the reason.\n"
    'Respond with JSON only: {"mappings": {"<target attribute>": '
    '{"values": {"<source code>": "<target value or null>"}, '
    '"reason": "<one sentence>"}}}'
)


def _llm_map_values(spec, enriched, target_dict, warehouse, source_table, by_name):
    """Translate source CODES onto target values when no lexical rule can.

    The column match can be right while the value translation is impossible
    deterministically. OB3250_JOINT decodes to "Joint or assigned to a third
    party", "single", "Assigned absolutely to one of 2 original owners" and
    "Joint life single owner"; joint_flag permits "0" and "1" meaning "No Joint
    Flag" and "Joint Flag". Not one label matches a permitted value or its
    meaning, so every code was unmapped and the transform degraded to a bare
    copy that would deliver an empty column.

    No rule reaches this. Knowing that "Joint life single owner" is a joint life
    and "Assigned absolutely to one of 2 original owners" is not requires
    reading the phrases. That is what this tier is for, and it is deliberately
    free of domain vocabulary: it passes both dictionaries' own words through
    and asks for a code-by-code assignment, so it works on a claims or member
    extract without anyone maintaining a list.

    Tested, not trusted, exactly like the other tiers:
      * every key must be a real source code
      * every value must be in the target's permitted list, or null
      * the rebuilt CASE must EXECUTE and produce values
      * the result is capped at REVIEW — collapsing a four-way code onto a
        yes/no is a business decision, not a mechanical one
    A proposal failing any check is discarded and the deterministic outcome
    stands, so this tier can only improve on what was there.
    """
    from .. import config
    client, model = config.llm_client()
    if client is None:
        return

    by_target = {a["name"]: a for a in target_attributes(target_dict)}
    todo = []
    for m in spec.mappings:
        if m.gate == Gate.REJECT.value or len(m.source_attributes or []) != 1:
            continue
        if not m.unmapped_codes:
            continue                          # every code already translates
        col = by_name.get(m.source_attributes[0])
        target = by_target.get(m.target_attribute)
        if col is None or target is None or not col.value_decode:
            continue
        if not (target.get("allowed_values") or _is_coded_target(target)):
            continue
        todo.append((m, col, target))
    if not todo:
        return

    def _counts(col_name: str) -> dict:
        try:
            rows = warehouse.con.execute(
                f'SELECT CAST("{col_name}" AS VARCHAR), count(*) '
                f"FROM {source_table} GROUP BY 1").fetchall()
            return {str(r[0]): r[1] for r in rows}
        except Exception:                     # noqa: BLE001
            return {}

    payload = []
    for m, col, target in todo:
        counts = _counts(col.name)
        payload.append({
            "target": m.target_attribute,
            "target_description": target.get("description", ""),
            "permitted_values": list(target.get("allowed_values") or []),
            "value_meanings": dict(target.get("value_meanings") or {}),
            "source_column": col.name,
            "source_business_name": col.business_name,
            "source_description": col.description,
            "source_codes": {code: {"meaning": label,
                                    "rows": counts.get(code, 0)}
                             for code, label in col.value_decode.items()},
        })

    try:
        out = _llm_json(client, model,
                        [{"role": "system", "content": _VALUE_MAP_SYS},
                         {"role": "user", "content": json.dumps(
                             {"mappings": payload}, default=str)}],
                        max_tokens=300 + 150 * len(payload))
        proposed = out.get("mappings") or {}
    except Exception as exc:                  # noqa: BLE001
        spec.stats["llm_note"] = f"value mapping skipped: {exc}"
        return

    applied = 0
    for m, col, target in todo:
        p = proposed.get(m.target_attribute)
        if not isinstance(p, dict):
            continue
        pairs = p.get("values")
        if not isinstance(pairs, dict):
            continue
        allowed = {str(v) for v in (target.get("allowed_values") or [])}
        chosen: dict = {}
        for code, value in pairs.items():
            if code not in col.value_decode:
                continue                      # invented a code: ignore it
            if value is None:
                continue                      # explicitly no equivalent
            if allowed and str(value) not in allowed:
                chosen = {}                   # outside the contract: reject wholesale
                break
            chosen[code] = str(value)
        if not chosen:
            continue

        whens = " ".join(f"WHEN {_lit(c)} THEN {_lit(v)}" for c, v in chosen.items())
        sql = f'CASE "{col.name}" {whens} ELSE NULL END'
        coverage = _validate(warehouse, source_table, sql, [col.name])
        if not coverage:
            continue                          # did not execute, or produced nothing

        still = [c for c in col.value_decode if c not in chosen]
        m.transformation_sql = sql
        m.unmapped_codes = still
        m.validation_coverage = coverage
        m.llm_recovered = True
        m.match_source = "llm_values"
        m.gate = Gate.REVIEW.value
        m.confidence = round(min(0.60 + 0.20 * coverage, 0.80), 3)
        shown = ", ".join(f"{c}->{v}" for c, v in chosen.items())
        m.transformation_note = f"Translate {col.business_name} codes: {shown}."
        m.rationale = (
            f"Code translation proposed by the LLM because no source label "
            f"matches a permitted target value: {shown}. "
            f"{p.get('reason') or ''} Executed against the source data "
            f"({coverage:.0%} of rows produce a value)"
            + (f"; {', '.join(still)} left unmapped" if still else "")
            + ". A human must confirm the business meaning.").strip()
        applied += 1
    if applied:
        spec.stats["llm_value_maps"] = applied
