"""Agent 2 — the legacy-system expert.

Takes Agent 1's data-derived dictionary (TableInsight) and enriches it with
*business meaning* by reading the code the table is used in:

  * COBOL copybook  -> field names line up positionally with the columns;
                       PROCEDURE logic reveals what the codes mean and the rules.
  * PHP screen      -> human-readable labels per column, plus decode tables
                       (code -> label) for the categorical fields.

Evidence is extracted deterministically; the LLM (Azure) reasons over it to
produce the enriched dictionary. Offline stub fuses the same evidence so it runs
today. Every enrichment carries its evidence and a confidence.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .. import kgraph
from .contracts import EnrichedColumn, EnrichedDictionary, TableInsight


# ------------------------------------------------------- evidence extraction
_FIELD_RE = re.compile(
    r"(?m)^\s*(\d{2})\s+([A-Z][A-Z0-9-]+)"
    r"(?:\s+REDEFINES\s+[A-Z][A-Z0-9-]+)?"
    r"(?:\s+PIC\s+([X9VSP()0-9.,]+))?"
)
_PARA_RE = re.compile(r"(?m)^ {7}([0-9A-Z][A-Z0-9-]*)\s*\.\s*$")
_ARITH_RE = re.compile(r"\b(COMPUTE|ADD|SUBTRACT|MULTIPLY|DIVIDE)\b")


def _programs(text: str) -> list[tuple[str, str]]:
    """Split concatenated COBOL source into (PROGRAM-ID, body) units."""
    out = []
    for chunk in re.split(r"(?=IDENTIFICATION\s+DIVISION)", text):
        m = re.search(r"PROGRAM-ID\.\s*([A-Z0-9-]+)", chunk)
        if m:
            out.append((m.group(1), chunk))
    return out or [("UNKNOWN", text)]


def _record_layouts(text: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """ALL candidate record layouts: every contiguous run of PIC'd elementary
    fields under a group item, with the group (record) name."""
    runs, cur = [], ("", [])
    def _flush():
        nonlocal cur
        if cur[1]:
            runs.append(cur)
        cur = ("", [])
    for m in _FIELD_RE.finditer(text):
        level, name, pic = m.group(1), m.group(2), m.group(3)
        if level in ("88", "66"):       # condition names / RENAMES: not storage,
            continue                    # must not break the contiguous field run
        if pic is None or level == "01":
            _flush()                    # group header / standalone 01 ends a run
            if pic is None:
                cur = (name, [])
                continue
        cur[1].append((name, pic))
    _flush()
    return runs


def _record_layout(text: str, column_count: int | None = None,
                   type_hints: list[str] | None = None
                   ) -> tuple[str | None, list[tuple[str, str]]]:
    """Pick the record layout for the ACTIVE source. A multi-estate workspace
    holds several programs' copybooks at once, so 'longest run' alone is wrong:
    prefer the run whose field count MATCHES the extract's column count (data-
    validated alignment, light form), fall back to the longest run.

    Several copybooks can share a field count, so exact-count ties are broken
    by the DATA: positional PIC-vs-inferred-type compatibility (a PIC 9 field
    over text values is a contradiction), then by how much the procedure code
    actually uses the layout's fields."""
    runs = _record_layouts(text)
    if not runs:
        return None, []
    if column_count:
        exact = [r for r in runs if len(r[1]) == column_count]
        if len(exact) == 1:
            return exact[0][0] or None, exact[0][1]
        if exact:
            numeric_hint = {"DATE_YYYYMMDD", "DECIMAL", "INTEGER", "IDENTIFIER"}
            def _fit(run):
                score = 0
                for i, (_, pic) in enumerate(run[1]):
                    if not type_hints or i >= len(type_hints):
                        break
                    pic_numeric = bool(re.match(r"S?9", (pic or "").upper()))
                    if pic_numeric:
                        score += 1 if type_hints[i] in numeric_hint else -1
                return score
            def _usage(run):
                return sum(len(re.findall(rf"\b{re.escape(n)}\b", text)) - 1
                           for n, _ in run[1])
            win = max(exact, key=lambda r: (_fit(r), _usage(r)))
            return win[0] or None, win[1]
    best = max(runs, key=lambda r: len(r[1]))
    return best[0] or None, best[1]


def _io_map(text: str) -> dict[str, dict]:
    """Per program: physical file lineage — SELECT <file> ASSIGN TO <dataset>,
    which record each FD holds (COPY member or inline 01), and all COPY members."""
    io: dict[str, dict] = {}
    for prog, body in _programs(text):
        selects = re.findall(r"SELECT\s+([A-Z0-9-]+)\s+ASSIGN\s+TO\s+([A-Z0-9-]+)", body)
        fds = re.findall(r"FD\s+([A-Z0-9-]+)\s*\.(?:\s*\*[^\n]*\n|\s)*?"
                         r"(?:COPY\s+([A-Z0-9-]+)|01\s+([A-Z0-9-]+))", body)
        copies = re.findall(r"COPY\s+([A-Z0-9-]+)", body)
        io[prog] = {
            "files": {f: ds for f, ds in selects},
            "fd_records": {f: (cpy or rec) for f, cpy, rec in fds},
            "copy_members": sorted(set(copies)),
        }
    return io


_L88_RE = re.compile(r"(?m)^\s*88\s+([A-Z][A-Z0-9-]+)\s+VALUES?\s+(.+?)\s*\.\s*$")


def extract_condition_names(text: str) -> dict[str, dict]:
    """88-level condition names — free business vocabulary AND value decodes:
    `88 PR-CLOSED VALUE 'CL'` names the state and documents the code."""
    out: dict[str, dict] = {}
    parent = None
    for m in re.finditer(r"(?m)^\s*(\d{2})\s+([A-Z][A-Z0-9-]+)"
                         r"(?:\s+REDEFINES\s+[A-Z][A-Z0-9-]+)?"
                         r"(?:\s+PIC\s+[X9VSP()0-9.,]+)?"
                         r"(?:\s+VALUES?\s+(.+?))?\s*\.?\s*$", text):
        level, name, values = m.group(1), m.group(2), m.group(3)
        if level == "88" and parent and values:
            vals = re.findall(r"'([^']*)'", values) or values.split()
            out[name] = {"field": parent, "values": vals}
        elif level != "88" and "PIC" in m.group(0):
            parent = name
    return out


def _paragraphs(body: str) -> dict[str, str]:
    """Paragraph name -> paragraph source (comments included), in order."""
    parts = re.split(r"PROCEDURE\s+DIVISION\s*\.", body, maxsplit=1)
    if len(parts) < 2:
        return {}
    pieces = _PARA_RE.split(parts[1])
    return {pieces[i]: pieces[i + 1] for i in range(1, len(pieces) - 1, 2)}


def _assigns(field: str, para: str) -> bool:
    return bool(re.search(rf"(?:COMPUTE\s+{field}\b|\bTO\s+{field}\b|GIVING\s+{field}\b)", para))


# verbs whose semantics the parser UNDERSTANDS (dataflow / control flow)
_VERBS = {"MOVE", "COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "IF",
          "ELSE", "END-IF", "EVALUATE", "WHEN", "END-EVALUATE", "PERFORM", "GO",
          "GOBACK", "READ", "OPEN", "CLOSE", "REWRITE", "WRITE", "DISPLAY",
          "EXIT", "AT", "NOT", "STOP", "CONTINUE", "END-READ", "END-PERFORM"}
# verbs that START a statement but are NOT yet understood — they must break
# the continuation join (or they corrupt the previous expression) and they
# count AGAINST coverage, which is the whole point of the honesty metric
_EXOTIC = {"INSPECT", "STRING", "UNSTRING", "SET", "INITIALIZE", "CALL",
           "SEARCH", "SORT", "MERGE", "ACCEPT", "RELEASE", "RETURN", "START",
           "DELETE", "CANCEL", "ALTER", "EXEC"}
_BOUNDARY = _VERBS | _EXOTIC

_ASSIGN_PATTERNS = (
    r"COMPUTE\s+([A-Z][A-Z0-9-]+)",
    r"\bTO\s+([A-Z][A-Z0-9-]+)\b",            # MOVE/ADD ... TO x
    r"\bGIVING\s+([A-Z][A-Z0-9-]+)\b",        # arithmetic ... GIVING x
    r"SUBTRACT\s+.+?\s+FROM\s+([A-Z][A-Z0-9-]+)\b(?!\s+GIVING)",
)


def _targets(para: str) -> set[str]:
    out: set[str] = set()
    for pat in _ASSIGN_PATTERNS:
        out |= set(re.findall(pat, para))
    return out - {"ZEROS", "ZEROES", "SPACES"}


def _logical_lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        s = raw.strip().rstrip(".")
        if not s or s.startswith("*"):
            continue
        first = s.split()[0]
        if out and first not in _BOUNDARY:
            out[-1] += " " + s
        else:
            out.append(s)
    return out


def _coverage(paras_text: list[str]) -> float:
    """Fraction of sliced statements the parser recognises. The honesty metric:
    when it drops, the deterministic story is incomplete and the raw COBOL
    should be routed to the LLM (and the SME) with lower confidence."""
    total = recognised = 0
    for text in paras_text:
        for ln in _logical_lines(text):
            total += 1
            if ln.split()[0] in _VERBS:
                recognised += 1
    return round(recognised / total, 3) if total else 1.0


def extract_derivations(text: str, record_fields: list[tuple[str, str]],
                        cond_names: dict[str, dict] | None = None) -> dict:
    """BACKWARD SLICE per calculated field: start from the paragraphs that
    assign it (with real arithmetic — a housekeeping MOVE SPACES is not a
    derivation), then iterate to a fixed point over every working-storage
    variable the slice reads, pulling in the paragraphs that define those too.
    Generalises the old one-hop closure to chains of any depth, and reports a
    statement-coverage metric for the slice."""
    cond_names = cond_names or {}
    derivations: dict[str, dict] = {}
    names = [n for n, _ in record_fields]
    name_set = set(names)

    def resolve(tok: str) -> str:                # 88 name -> its parent field
        return cond_names.get(tok, {}).get("field", tok)

    for prog, body in _programs(text):
        paras = _paragraphs(body)
        tgt_map = {p: _targets(tx) for p, tx in paras.items()}
        tok_map = {p: {resolve(tk) for tk in re.findall(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b", tx)}
                   for p, tx in paras.items()}
        assignable = set().union(*tgt_map.values()) if tgt_map else set()

        for field in names:
            seeds = {p for p in paras if field in tgt_map[p]}
            if not seeds or not any(_ARITH_RE.search(paras[p]) for p in seeds):
                continue
            # fixed point: variables the slice depends on, at any depth
            sliced_vars = {field}
            while True:
                grown = set(sliced_vars)
                for p in paras:
                    if tgt_map[p] & sliced_vars:
                        grown |= {tk for tk in tok_map[p]
                                  if tk in assignable and tk not in name_set}
                if grown == sliced_vars:
                    break
                sliced_vars = grown
            keep = [p for p in paras if tgt_map[p] & sliced_vars]
            snippet = "\n".join(f"       {p}.{paras[p]}".rstrip() for p in keep)
            comments = [c.strip() for c in
                        re.findall(r"^\s*\*\s+(.*?)\s*$", snippet, re.MULTILINE)]
            inputs = [n for n in names if n != field and
                      any(n in tok_map[p] for p in keep)]
            derivations[field] = {
                "program": prog, "paragraphs": keep, "cobol": snippet,
                "comments": comments, "inputs": inputs,
                "coverage": _coverage([paras[p] for p in keep]),
                "slice_vars": sorted(sliced_vars - {field}),
            }
    return derivations


def extract_cobol(text: str, column_count: int | None = None,
                  type_hints: list[str] | None = None) -> dict:
    """Ordered copybook fields (-> positional map) + code meanings + comments
    + per-field derivation (calculation) logic.

    Naming-agnostic: the record layout is detected structurally (longest field
    run under a group item), so real copybooks enrich too, and calculation
    programs with their own working storage don't disturb the positional map.
    """
    record_name, copybook = _record_layout(text, column_count, type_hints)
    cond_names = extract_condition_names(text)
    code_meanings: dict[str, dict[str, str]] = {}
    for field, body in re.findall(r"EVALUATE\s+([A-Z][A-Z0-9-]+)(.*?)END-EVALUATE", text, re.DOTALL):
        pairs = re.findall(r"WHEN\s+'([^']+)'\s+DISPLAY\s+'([^']+)'", body)
        if pairs:
            code_meanings[field] = {c: t for c, t in pairs}
    # 88-level condition names are decodes too: 88 PR-CLOSED VALUE 'CL'
    for name, info in cond_names.items():
        for v in info["values"]:
            code_meanings.setdefault(info["field"], {}).setdefault(v, _humanize(name))
    comments = re.findall(r"^\s*\*\s+(.*?)\s*$", text, re.MULTILINE)
    return {"copybook": copybook, "record_name": record_name, "io": _io_map(text),
            "code_meanings": code_meanings, "comments": comments,
            "cond_names": cond_names,
            "derivations": extract_derivations(text, copybook, cond_names)}


def extract_php(text: str) -> dict:
    """Column -> screen label, decode tables, and the SELECTed columns/table.

    Column tokens are matched generically (not just XA##), so any screen parses.
    """
    sel = re.search(r"SELECT(.*?)FROM\s+(\w+)", text, re.DOTALL)
    select_cols = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sel.group(1)) if sel else []
    table = sel.group(2) if sel else None

    arrays: dict[str, dict[str, str]] = {}
    for var, body in re.findall(r"\$(\w+)\s*=\s*\[(.*?)\];", text, re.DOTALL):
        if var == "fields":
            continue
        pairs = re.findall(r"'([^']+)'\s*=>\s*'([^']+)'", body)
        if pairs:
            arrays[var] = {c: t for c, t in pairs}

    fields = []  # (col, label, decode_array_name|None)
    for col, label, _, dname in re.findall(
        r"'col'\s*=>\s*'([A-Za-z0-9_]+)',\s*'label'\s*=>\s*'([^']+)',\s*'decode'\s*=>\s*(null|'(\w+)')",
        text,
    ):
        fields.append((col, label, dname or None))
    return {"select_cols": select_cols, "table": table, "arrays": arrays, "fields": fields}


# ------------------------------------------------------------------- helpers
_ABBR = {
    "PR": "", "NO": "Number", "DT": "Date", "RSN": "Reason", "NM": "Name",
    "PROD": "Product", "PREM": "Premium", "AMT": "Amount", "FREQ": "Frequency",
    "UNPAID": "Unpaid", "COMMENCE": "Commencement", "REC": "Record",
    "SUM": "Sum", "ASSURED": "Assured", "REGION": "Region", "FILLER": "Filler",
    "TYPE": "Type", "STATUS": "Status", "EXIT": "Exit", "SCHEME": "Scheme",
    "POLICY": "Policy", "PEN": "Penalty", "PCT": "Percent",
    "VALN": "Valuation", "YRS": "Years", "COMM": "Commencement", "END": "End",
    "YYYY": "Year", "MMDD": "Month-Day", "CALC": "Calculation",
}


def _humanize(cobol_name: str) -> str:
    toks = cobol_name.split("-")
    # Strip a leading short (2-3 char) ALL-CAPS record-prefix token. Every
    # COBOL shop uses its OWN prefix per 01-level record purely to
    # disambiguate identically-suffixed fields across different records
    # (PR- for a policy record, SR- for a scheme record, CL- for a claim
    # record, WS- for working-storage, ...). It carries no business meaning
    # by itself. This is a structural COBOL naming convention, not a fixed
    # list of prefixes we happened to see in one estate -- a hardcoded list
    # here would silently mislabel every OTHER record's fields (exactly what
    # happened to "SR-SCHEME-NAME", which humanized to the literal word "Sr
    # Scheme Name" and lost a mapping contest it should have won).
    if (toks and 2 <= len(toks[0]) <= 3 and toks[0].isalpha()
            and toks[0].isupper() and toks[0] not in _ABBR):
        toks = toks[1:]
    parts = [p for p in toks if p]
    return " ".join(_ABBR.get(p, p.capitalize()) for p in parts).strip()


# ------------------------------------------------------------------ the agent
def _lineage_sentence(lin: dict) -> str:
    """Human-readable data lineage for a calculated field."""
    inputs = ", ".join(f"{f} ({c})" if c else f for f, c in lin["inputs"].items())
    src = lin.get("record") or "the source record"
    bits = [f"Lineage: inputs {inputs}" if inputs else "Lineage:",
            f"all read from record {src}"]
    if lin.get("copybook"):
        bits[-1] += f" (COPY {lin['copybook']})"
    if lin.get("file") and lin.get("dataset"):
        bits.append(f"held on file {lin['file']} = dataset {lin['dataset']}")
    bits.append(f"computed and written back in place by {lin['program']}")
    return "; ".join(bits) + "."


def build_evidence(insight: TableInsight, cobol_text: str, php_text: str) -> dict:
    """Extract and JOIN the three evidence sources once. Returns everything a
    consumer needs: raw extractions, the positional alignment, decodes, and the
    in-memory knowledge graph. Deterministic — safe to call repeatedly (e.g. by
    both `enrich` and the KG persistence node)."""
    cob = extract_cobol(cobol_text, column_count=len(insight.columns),
                        type_hints=[c.inferred_type for c in insight.columns])
    php = extract_php(php_text)

    # positional copybook -> column: i-th copybook field aligns with i-th column,
    # whatever the columns are named (demo XA##, or any real extract).
    # Multi-source guard: positional alignment is only defensible when the
    # chosen record layout has EXACTLY this file's column count — otherwise
    # (e.g. a scheme master profiled in a workspace whose only copybook is the
    # policy record) binding would hand this file another file's business
    # meanings. No alignment beats a wrong alignment; the file still enriches
    # from its own data and screen evidence.
    cols_order = [c.name for c in insight.columns]
    aligned = len(cob["copybook"]) == len(cols_order)
    pos_map = ({cols_order[i]: cob["copybook"][i]
                for i in range(len(cols_order))} if aligned else {})
    name_to_col = {v[0]: k for k, v in pos_map.items()}

    php_label = {c: lbl for c, lbl, _ in php["fields"]}
    php_decode = {c: php["arrays"].get(d, {}) for c, _, d in php["fields"] if d and d in php["arrays"]}

    # knowledge graph over the three evidence sources: copybook alignment,
    # screen labels/decodes, and COBOL dataflow — used to RESOLVE calculated
    # fields into business terms (WS intermediates traced back to labelled
    # record fields and constants).
    decode_by_col: dict[str, dict] = {}
    for c in insight.columns:
        cn = pos_map.get(c.name, (None, None))[0]
        d: dict[str, str] = {}
        if cn and cn in cob["code_meanings"]:
            d.update({k: v.title() for k, v in cob["code_meanings"][cn].items()})
        d.update(php_decode.get(c.name, {}))
        if d:
            decode_by_col[c.name] = d
    kg = kgraph.build(cobol_text, cob["copybook"], name_to_col,
                      php_label, decode_by_col, _humanize)
    return {"cob": cob, "php": php, "pos_map": pos_map, "name_to_col": name_to_col,
            "php_label": php_label, "php_decode": php_decode,
            "decode_by_col": decode_by_col, "kg": kg}


def enrich(insight: TableInsight, cobol_text: str, php_text: str) -> EnrichedDictionary:
    ev = build_evidence(insight, cobol_text, php_text)
    cob, php, pos_map = ev["cob"], ev["php"], ev["pos_map"]
    name_to_col, php_label, php_decode = ev["name_to_col"], ev["php_label"], ev["php_decode"]
    kg = ev["kg"]

    enriched: list[EnrichedColumn] = []
    for col in insight.columns:
        cname = pos_map.get(col.name, (None, None))[0]
        cpic = pos_map.get(col.name, (None, None))[1]
        label = php_label.get(col.name)

        # value decode: COBOL meanings first, PHP labels override (cleaner)
        vdec: dict[str, str] = {}
        if cname and cname in cob["code_meanings"]:
            vdec.update({k: v.title() for k, v in cob["code_meanings"][cname].items()})
        vdec.update(php_decode.get(col.name, {}))

        der = cob.get("derivations", {}).get(cname) if cname else None

        sources, evidence, conf = [], [], 0.4
        if label:
            sources.append("screen"); evidence.append(f"screen label '{label}' (policy_view.php)")
            conf = 0.9
        if cname:
            sources.append("cobol"); evidence.append(f"COBOL {cname} PIC {cpic}")
            conf = max(conf, 0.7)
        if cname and cname in cob["code_meanings"]:
            evidence.append(f"COBOL EVALUATE {cname} branch meanings")
        if der:
            sources.append("cobol-calc")
            evidence.append(f"calculated in COBOL {der['program']} "
                            f"({', '.join(der['paragraphs'])}); parser understood "
                            f"{der['coverage']:.0%} of the sliced statements")
            if der["coverage"] >= 0.8:
                conf = max(conf, 0.75)
            else:
                evidence.append("LOW PARSE COVERAGE — deterministic story is "
                                "incomplete; LLM assist + SME review recommended")
                conf = min(conf, 0.6)
        sources.append("data"); evidence.append(f"analyst: {col.role}, {col.inferred_type}")
        if label and cname:
            conf = 0.95

        business = label or (_humanize(cname) if cname else col.role.split(" (")[0].title())
        desc = f"{business} — {col.inferred_type.lower().replace('_', ' ')}."
        if col.role.startswith("dead"):
            desc = f"{business} — structural filler, no business meaning."
        elif col.role.startswith("constant"):
            desc = f"{business} — control field (constant value)."
        if vdec:
            desc += f" Coded values decode to: {', '.join(f'{k}={v}' for k, v in vdec.items())}."

        derivation_cov = der["coverage"] if der else None
        derivation = derivation_cobol = derived_in = lineage = None
        narrative = resolved = None
        if der:
            derived_in = der["program"]
            derivation_cobol = der["cobol"]
            # ---- lineage: which fields feed the calculation, and where the
            # record physically comes from (FILE-CONTROL / FD / COPY member)
            io = cob.get("io", {}).get(derived_in, {})
            file_name, dataset = next(iter(io.get("files", {}).items()), (None, None))
            # the record's copybook is the one the FD copies — a program may
            # COPY other members (rate tables etc.) that are NOT the record
            fd_rec = io.get("fd_records", {}).get(file_name)
            copy_member = fd_rec if fd_rec in io.get("copy_members", []) else None
            ordered_inputs = [n for n, _ in cob["copybook"] if n in der.get("inputs", [])]
            lineage = {
                "program": derived_in,
                "record": cob.get("record_name"),
                "copybook": copy_member,
                "file": file_name,
                "dataset": dataset,
                "inputs": {n: name_to_col.get(n) for n in ordered_inputs},
            }
            # offline plain-English: the program's own comments give the
            # business narrative; the knowledge graph resolves the actual
            # calculation into labelled business terms. The LLM pass polishes
            # this further when live.
            resolved = kg.explain(cname)
            der["resolved"] = resolved          # also grounds the LLM bundle
            narrative = " ".join(der["comments"]) if der["comments"] else None
            parts = []
            if narrative:                       # real estates rarely narrate —
                parts.append(narrative)         # use it when present
            parts.append("Resolved calculation (knowledge graph):\n" + resolved)
            parts.append(_lineage_sentence(lineage))
            derivation = "\n\n".join(parts)
            desc += (f" Calculated field — derived by {derived_in}, not keyed.")

        enriched.append(EnrichedColumn(
            name=col.name, business_name=business, description=desc,
            inferred_type=col.inferred_type, cobol_name=cname, cobol_pic=cpic,
            screen_label=label, value_decode=vdec, evidence=evidence,
            sources=sources, confidence=round(conf, 2),
            derivation=derivation, derivation_cobol=derivation_cobol,
            derivation_narrative=(narrative if der else None),
            derivation_resolved=(resolved if der else None),
            derived_in_program=derived_in, derivation_lineage=lineage,
            derivation_coverage=derivation_cov,
        ))

    # explain Agent-1's conditional rules using the decoded driver values
    by_name = {c.name: c for c in enriched}
    decode_of = {c.name: c.value_decode for c in enriched}
    rules: list[str] = []
    for d in insight.dependencies:
        drv = d.drivers[0] if d.drivers else None
        codes = re.findall(r"\{([^}]*)\}", d.condition or "")
        vals = [v.strip() for v in codes[0].split(",")] if codes else []
        decoded = ", ".join(decode_of.get(drv, {}).get(v, v) for v in vals) if drv else ""
        dep_name = by_name[d.dependent].business_name if d.dependent in by_name else d.dependent
        drv_name = by_name[drv].business_name if drv in by_name else drv
        rules.append(
            f"{dep_name} is recorded only when {drv_name} is {decoded or vals} "
            f"({d.support_rows} rows) — a business rule, confirmed in the COBOL logic."
        )

    result = EnrichedDictionary(table=insight.table, columns=enriched, rules=rules)
    return _narrate(result, insight, cob, php)


# ----------------------------------------------------- LLM reasoning (optional)
# LLM JSON plumbing lives in engine/llmjson.py — re-exported here because this
# module's own narration path uses it, and to keep any existing import working.
from ..llmjson import _llm_json, _repair_json   # noqa: F401,E402

def _compact_bundle(result: EnrichedDictionary, insight: TableInsight,
                    cob: dict, php: dict) -> dict:
    """A per-column DIGEST instead of three overlapping full dumps: the single
    biggest lever against LLM failure is payload size (rate limits, timeouts,
    truncation and malformed JSON all scale with it)."""
    by_prof = {c.name: c for c in insight.columns}
    cols = []
    for c in result.columns:
        prof = by_prof.get(c.name)
        cols.append({
            "name": c.name, "draft_business_name": c.business_name,
            "draft_description": (c.description or "")[:240],
            "type": c.inferred_type, "role": prof.role if prof else None,
            "cobol": f"{c.cobol_name} PIC {c.cobol_pic}" if c.cobol_name else None,
            "screen_label": c.screen_label,
            "decode": c.value_decode or None,
            "top_values": dict(list((prof.top_values or {}).items())[:3]) if prof else None,
            "evidence": (c.evidence or [])[:2],
            "is_calculated": bool(c.derivation_cobol),
        })
    return {"table": insight.table, "record": cob.get("record_name"),
            "columns": cols, "code_comments": cob.get("comments", [])[:15],
            "screen_fields": php.get("fields", [])[:40]}


_MEANINGS_SYS = (
    "You are a legacy life & pensions systems expert. You are given a per-column "
    "evidence digest (draft names/descriptions, COBOL copybook names, screen "
    "labels, value decodes, sample values). Refine each column's business "
    "meaning, citing the evidence: screen labels and copybook names are the "
    "primary signals. Do NOT invent meaning unsupported by the evidence; lower "
    "confidence where evidence is thin. Return ONLY JSON with key 'columns' "
    "(list of {name, business_name, description, value_decode, confidence}) "
    "and 'rules' (list of strings, business rules evident from the data/code). "
    "Write plain UTF-8 in all strings; never emit backslash-u escape sequences."
)

_RULE_SYS = (
    "You are a legacy life & pensions systems expert. Translate ONE calculated "
    "field's logic into precise plain-English business prose. The 'resolved' "
    "text is your factual backbone — every number, band, factor, cap and "
    "condition must come from it; keep field references like 'Exit Date (XA06)' "
    "intact; nothing invented. State: what the field is, when it applies "
    "(eligibility), the calculation step by step (rate bands / factor tables "
    "with exact values, formula, caps, rounding), and end with the data lineage "
    "exactly as given. Return ONLY JSON: {\"calculation\": \"...\"}. Plain "
    "UTF-8; never emit backslash-u escape sequences."
)


def _apply_meanings(result: EnrichedDictionary, data: dict) -> None:
    """Apply per column with per-entry isolation: one malformed entry (e.g. a
    non-numeric confidence) must not void the other twenty-three."""
    by_name = {c.get("name"): c for c in data.get("columns", [])}
    for col in result.columns:
        u = by_name.get(col.name)
        if not u:
            continue
        try:
            col.business_name = u.get("business_name") or col.business_name
            col.description = u.get("description") or col.description
            if isinstance(u.get("value_decode"), dict):
                col.value_decode = u["value_decode"]
            col.confidence = float(u.get("confidence", col.confidence))
        except (TypeError, ValueError):
            continue
    if isinstance(data.get("rules"), list):
        result.rules = [str(r) for r in data["rules"]]


def _narrate(result: EnrichedDictionary, insight: TableInsight, cob: dict, php: dict) -> EnrichedDictionary:
    from .. import config
    client, model = config.llm_client()
    if client is None:
        result.generated_by = "deterministic+offline_stub"
        return result

    # ---- call 1 (small): column meanings for the whole table
    try:
        bundle = _compact_bundle(result, insight, cob, php)
        data = _llm_json(client, model,
                         [{"role": "system", "content": _MEANINGS_SYS},
                          {"role": "user", "content": json.dumps(bundle, default=str)}],
                         max_tokens=4000)
        _apply_meanings(result, data)
        result.generated_by = "deterministic+llm"
    except Exception as e:                                    # noqa: BLE001
        cause = e.__cause__ or getattr(e, "__context__", None)
        detail = f"{type(e).__name__}: {e}" + (
            f" — cause: {type(cause).__name__}: {cause}" if cause else "")
        result.rules.append(f"[LLM enrichment skipped: {detail}]")
        result.generated_by = "deterministic+offline_stub"
        return result                        # no point narrating rules either

    # ---- one tiny, focused call PER calculated field: a failure degrades
    # that single rule to its deterministic rendering, nothing else
    for col in result.columns:
        if not col.derivation_cobol:
            continue
        payload = {
            "field": col.cobol_name, "column": col.name,
            "business_name": col.business_name,
            "resolved": col.derivation_resolved,
            "lineage": col.derivation_lineage,
            "cobol": (col.derivation_cobol or "")[:4000],
            "parse_coverage": col.derivation_coverage,
        }
        try:
            out = _llm_json(client, model,
                            [{"role": "system", "content": _RULE_SYS},
                             {"role": "user", "content": json.dumps(payload, default=str)}],
                            max_tokens=900)
            if out.get("calculation"):
                col.derivation_narrative = str(out["calculation"])
        except Exception as e:                                # noqa: BLE001
            result.rules.append(
                f"[LLM narration incomplete for {col.name}: "
                f"{type(e).__name__}: {e} — deterministic rendering shown]")
    return result
