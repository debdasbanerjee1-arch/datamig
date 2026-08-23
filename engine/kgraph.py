"""kgraph — a small in-memory knowledge graph over the three evidence sources
(COBOL code, screen code, source extract), and a resolver that walks it.

The value of the graph is JOINS across evidence types. A single fact chain like

    WS-END-YYYY --subfield-of--> WS-END-DT --assigned-from--> PR-EXIT-DT
    --aligned-to--> XA06 --labelled-as--> 'Exit Date' (screen)

lets `explain()` render a calculated field's business rule in human terms
("the year part of the Exit Date (XA06)") instead of raw COBOL symbols.

Node kinds: record fields, extract columns, working-storage variables,
constants, sub-fields (REDEFINES), screen labels. Edge kinds: positional
alignment, screen labelling, REDEFINES decomposition, conditional dataflow
assignment (with the governing IF/EVALUATE conditions), value decodes.

Deliberately dependency-free and sized for a PoC: the same shape scales to a
real graph store (DuckDB edge tables, Neptune, Neo4j) when the estate grows to
many programs, JCL and DB2 tables.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# statement keywords that START a new logical line (anything else continues)
_KEYWORDS = {"MOVE", "COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE",
             "IF", "ELSE", "END-IF", "EVALUATE", "WHEN", "END-EVALUATE",
             "PERFORM", "GO", "GOBACK", "READ", "OPEN", "CLOSE", "REWRITE",
             "WRITE", "DISPLAY", "EXIT", "AT", "NOT",
             # statement starters we don't parse yet — must still break the
             # continuation join or they corrupt the previous expression
             "INSPECT", "STRING", "UNSTRING", "SET", "INITIALIZE", "CALL",
             "SEARCH", "SORT", "MERGE", "ACCEPT", "RELEASE", "RETURN",
             "START", "DELETE", "CANCEL", "ALTER", "EXEC", "STOP", "CONTINUE"}

_FIELD_DEF = re.compile(
    r"(?m)^\s*(\d{2})\s+([A-Z][A-Z0-9-]+)"
    r"(?:\s+REDEFINES\s+([A-Z][A-Z0-9-]+))?"
    r"(?:\s+PIC\s+([X9VSP()0-9.,]+))?"
    r"(?:\s+VALUES?\s+([A-Z0-9' .-]+?))?\s*\.?\s*$"
)
_PARA = re.compile(r"(?m)^ {7}([0-9A-Z][A-Z0-9-]*)\s*\.\s*$")
_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")


def _pic_len(pic: str) -> int:
    """Character length of a PIC clause (V is an implied point: zero width)."""
    n, i = 0, 0
    while i < len(pic):
        ch = pic[i]
        if ch in "9XA":
            if i + 1 < len(pic) and pic[i + 1] == "(":
                j = pic.index(")", i)
                n += int(pic[i + 2:j]); i = j + 1
            else:
                n += 1; i += 1
        else:
            i += 1                      # S, V, P, punctuation
    return n


@dataclass
class Rule:
    """One conditional assignment: target ← expr when all conds hold."""
    target: str
    expr: str
    conds: list[str] = field(default_factory=list)
    rounded: bool = False
    self_update: bool = False           # expr references its own target


class KGraph:
    def __init__(self):
        self.labels: dict[str, str] = {}                 # node -> business label
        self.column_of: dict[str, str] = {}              # record field -> extract column
        self.decode: dict[str, dict[str, str]] = {}      # extract column -> {code: meaning}
        self.constants: dict[str, str] = {}              # var -> literal VALUE
        self.subfields: dict[str, tuple[str, int, int]] = {}   # child -> (parent, from, to) 1-based
        self.rules: dict[str, list[Rule]] = {}           # target -> ordered assignments
        self.pics: dict[str, str] = {}
        self.conditions: dict[str, tuple[str, list[str]]] = {}   # 88 name -> (parent field, values)

    def resolve_token(self, tok: str) -> str:
        """Condition names stand for a test on their parent field."""
        return self.conditions[tok][0] if tok in self.conditions else tok

    # ----------------------------------------------------------- construction
    def add_alignment(self, fld: str, column: str | None, label: str, pic: str):
        self.labels[fld] = label
        self.pics[fld] = pic
        if column:
            self.column_of[fld] = column

    def add_storage(self, cobol_text: str, humanize):
        """Working-storage: labels, VALUE constants, REDEFINES sub-fields,
        and 88-level condition names (business vocabulary for coded values)."""
        redefining, offset = None, 0
        parent = None
        for m in _FIELD_DEF.finditer(cobol_text):
            level, name, redef, pic, value = m.groups()
            if level == "88":
                if parent and value:
                    vals = re.findall(r"'[^']*'", value) or [value]
                    self.conditions[name] = (parent, vals)
                    self.labels.setdefault(name, humanize(name))
                continue
            if pic:
                parent = name
            self.labels.setdefault(name, humanize(name))
            if pic:
                self.pics.setdefault(name, pic)
            if redef and not pic:                        # REDEFINES group opens
                redefining, offset = redef, 0
                continue
            if pic is None or level == "01":
                if not redef:
                    redefining = None                    # any other group closes it
            if redefining and pic and level != "01":
                ln = _pic_len(pic)
                self.subfields[name] = (redefining, offset + 1, offset + ln)
                offset += ln
            if value and pic and value not in ("ZEROS", "ZEROES", "SPACES"):
                self.constants[name] = value.strip("'").rstrip(".")

    def add_dataflow(self, cobol_text: str):
        for chunk in re.split(r"(?=IDENTIFICATION\s+DIVISION)", cobol_text):
            parts = re.split(r"PROCEDURE\s+DIVISION\s*\.", chunk, maxsplit=1)
            if len(parts) < 2:
                continue
            pieces = _PARA.split(parts[1])
            for i in range(2, len(pieces), 2):
                self._parse_paragraph(pieces[i])

    # ------------------------------------------------- dataflow (per paragraph)
    def _logical_lines(self, text: str) -> list[str]:
        out: list[str] = []
        for raw in text.splitlines():
            s = raw.strip().rstrip(".")
            if not s or s.startswith("*"):
                continue
            first = s.split()[0]
            if out and first not in _KEYWORDS:
                out[-1] += " " + s                       # continuation line
            else:
                out.append(s)
        return out

    def _parse_paragraph(self, text: str):
        stack: list[str] = []            # open IF conditions
        goto_guard: list[bool] = []      # block contained GO TO and no assignment
        rest: list[str] = []             # guard conditions for the remainder
        eval_subject: str | None = None
        in_eval = False
        for ln in self._logical_lines(text):
            if ln.startswith("IF "):
                stack.append(ln[3:].strip()); goto_guard.append(False)
            elif ln == "ELSE":
                if stack:
                    stack[-1] = _negate(stack[-1]); goto_guard[-1] = False
            elif ln.startswith("END-IF"):
                if stack:
                    cond = stack.pop()
                    if goto_guard.pop():
                        rest.append(_negate(cond))       # guard: rest of paragraph
            elif ln.startswith("EVALUATE"):
                subj = ln.split(None, 1)[1].strip()
                eval_subject = None if subj == "TRUE" else subj
                in_eval = True
            elif ln.startswith("END-EVALUATE"):
                in_eval, eval_subject = False, None
            elif ln.startswith("WHEN ") and in_eval:
                body = ln[5:].strip()
                m = re.search(r"\b(MOVE|COMPUTE|ADD|SUBTRACT)\b", body)
                cond_part = (body[:m.start()] if m else body).strip()
                if cond_part in ("OTHER", "OTHERS"):
                    cond = "otherwise"
                elif eval_subject:
                    cond = f"{eval_subject} = {cond_part}"
                else:
                    cond = cond_part
                if m:
                    self._statement(body[m.start():], stack + rest + [cond])
            elif ln.startswith("GO TO") or ln.startswith("GO "):
                if stack:
                    goto_guard[-1] = True
            else:
                if self._statement(ln, stack + rest) and stack:
                    goto_guard[-1] = False               # block does real work

    def _statement(self, s: str, conds: list[str]) -> bool:
        conds = [c for c in conds if c]
        m = re.match(r"MOVE\s+(.+?)\s+TO\s+([A-Z][A-Z0-9-]+)$", s)
        if m:
            self._add_rule(Rule(m.group(2), m.group(1), conds))
            return True
        m = re.match(r"COMPUTE\s+([A-Z][A-Z0-9-]+)(\s+ROUNDED)?\s*=\s*(.+)$", s)
        if m:
            tgt, expr = m.group(1), m.group(3).strip()
            self._add_rule(Rule(tgt, expr, conds, rounded=bool(m.group(2)),
                                self_update=bool(re.search(rf"\b{tgt}\b", expr))))
            return True
        m = re.match(r"ADD\s+(.+?)\s+TO\s+([A-Z][A-Z0-9-]+)$", s)
        if m:
            self._add_rule(Rule(m.group(2), f"{m.group(2)} + {m.group(1)}",
                                conds, self_update=True))
            return True
        m = re.match(r"SUBTRACT\s+(.+?)\s+FROM\s+([A-Z][A-Z0-9-]+)$", s)
        if m:
            self._add_rule(Rule(m.group(2), f"{m.group(2)} - {m.group(1)}",
                                conds, self_update=True))
            return True
        m = re.match(r"ADD\s+(.+?)\s+TO\s+(.+?)\s+GIVING\s+([A-Z][A-Z0-9-]+)$", s)
        if m:
            self._add_rule(Rule(m.group(3), f"{m.group(2)} + {m.group(1)}", conds))
            return True
        m = re.match(r"SUBTRACT\s+(.+?)\s+FROM\s+(.+?)\s+GIVING\s+([A-Z][A-Z0-9-]+)$", s)
        if m:
            self._add_rule(Rule(m.group(3), f"{m.group(2)} - {m.group(1)}", conds))
            return True
        m = re.match(r"MULTIPLY\s+(.+?)\s+BY\s+(.+?)\s+GIVING\s+([A-Z][A-Z0-9-]+)$", s)
        if m:
            self._add_rule(Rule(m.group(3), f"{m.group(1)} * {m.group(2)}", conds))
            return True
        m = re.match(r"DIVIDE\s+(.+?)\s+INTO\s+(.+?)\s+GIVING\s+([A-Z][A-Z0-9-]+)$", s)
        if m:
            self._add_rule(Rule(m.group(3), f"{m.group(2)} / {m.group(1)}", conds))
            return True
        return False

    def _add_rule(self, r: Rule):
        self.rules.setdefault(r.target, []).append(r)

    # ------------------------------------------------------------- rendering
    def _name(self, tok: str) -> str:
        if tok in self.conditions:
            parent, vals = self.conditions[tok]
            test = f"= {vals[0]}" if len(vals) == 1 else f"IN ({', '.join(vals)})"
            return f"{self._name(parent)} {test}"
        if tok in self.constants:
            return f"{self.constants[tok]} (the {self.labels.get(tok, tok).lower()}, {tok})"
        label = self.labels.get(tok)
        col = self.column_of.get(tok)
        if col:
            return f"{label} ({col})"
        if label and label.upper().replace(" ", "-") != tok:
            return f"{label.lower()} ({tok})"
        return tok

    def _render(self, s: str) -> str:
        s = re.sub(r"([A-Z][A-Z0-9-]+)\s*\((\d+):(\d+)\)",
                   lambda m: f"characters {m.group(2)}\u2013{m.group(3)} of {m.group(1)}", s)
        s = _TOKEN.sub(lambda m: self._name(m.group(0)), s)
        s = (s.replace(" NOT = ", " ≠ ").replace(" * ", " × ").replace(" / ", " ÷ ")
              .replace(" >= ", " ≥ ").replace(" <= ", " ≤ ")
              .replace("ZEROS", "zeros").replace("ZEROES", "zeros"))
        # decode coded literals: "... (STATCD) = 'CL'" -> append the meaning.
        # Matches ANY rendered column-code token in parens (not just legacy
        # demo-style "XA05" codes) -- a real estate's columns are just as
        # often POLNO, STATCD, EXITRSN, etc.
        def _dec(m):
            col, op, code = m.group(1), m.group(2), m.group(3)
            meaning = self.decode.get(col, {}).get(code)
            return m.group(0) + (f" [{meaning}]" if meaning else "")
        return re.sub(r"\(([A-Z][A-Z0-9_-]*)\)\s*(=|≠)\s*'([A-Z]+)'", _dec, s)

    def _when(self, conds: list[str]) -> str:
        real = [c for c in conds if c != "otherwise"]
        if not real and "otherwise" in conds:
            return "otherwise"
        if not real:
            return ""
        parts = [f"({self._render(c)})" if " OR " in c else self._render(c)
                 for c in real]
        return "when " + " and ".join(parts)

    def _deps(self, name: str) -> list[str]:
        """Working-storage variables the field depends on, first-seen order."""
        seen, order, queue = set(), [], [name]
        while queue:
            cur = queue.pop(0)
            texts = [r.expr + " " + " ".join(r.conds) for r in self.rules.get(cur, [])]
            if cur in self.subfields:
                texts.append(self.subfields[cur][0])
            for t in texts:
                for tok in _TOKEN.findall(t):
                    tok = self.resolve_token(tok)
                    if tok == cur or tok in self.column_of or tok in self.constants:
                        continue
                    if (tok in self.rules or tok in self.subfields) and tok not in seen:
                        seen.add(tok); order.append(tok); queue.append(tok)
        return order

    def _subfield_phrase(self, name: str) -> str:
        parent, a, b = self.subfields[name]
        piece = f"characters {a}–{b} of {self._name(parent)}"
        if self.pics.get(parent, "").startswith("9(08)"):      # YYYYMMDD dates
            if (a, b) == (1, 4):
                piece = f"the year part (chars 1–4) of {self._name(parent)}"
            elif (a, b) == (5, 8):
                piece = f"the month-day part (chars 5–8) of {self._name(parent)}"
        return piece

    def _rendered_rule(self, r: Rule) -> str:
        if r.expr in ("ZEROS", "ZEROES"):
            return "0"
        expr = self._render(r.expr)
        if r.self_update:
            prefix = self._name(r.target)
            if expr.startswith(prefix):
                expr = "result" + expr[len(prefix):]
        return expr

    @staticmethod
    def _common(rules: list[Rule]) -> set[str]:
        """Conditions shared by EVERY rule of a variable are context (the
        enclosing guards), not discriminators — suppress them in rendering."""
        if len(rules) < 2:
            return set()
        common = set(rules[0].conds) - {"otherwise"}
        for r in rules[1:]:
            common &= set(r.conds)
        return common

    def classify_inputs(self, fld: str) -> dict[str, list[str]]:
        """For each record field feeding a calculation: is it used as an
        operand (in an expression), a condition (in a guard/WHEN), or both?"""
        roles: dict[str, set[str]] = {}
        for var in [fld] + self._deps(fld):
            for r in self.rules.get(var, []):
                for tok in _TOKEN.findall(r.expr):
                    tok = self.resolve_token(tok)
                    if tok in self.column_of and tok != fld:
                        roles.setdefault(tok, set()).add("operand")
                for c in r.conds:
                    for tok in _TOKEN.findall(c):
                        tok = self.resolve_token(tok)
                        if tok in self.column_of and tok != fld:
                            roles.setdefault(tok, set()).add("condition")
        return {k: sorted(v) for k, v in roles.items()}

    def rule_structure(self, fld: str) -> dict:
        """Machine-readable rule: the target's assignment steps plus one
        decision table per dependency variable — this is what persists, and
        what a mapper can compile into target-side SQL CASE logic."""
        def _rows(var: str) -> list[dict]:
            common = self._common(self.rules.get(var, []))
            out = []
            for r in self.rules.get(var, []):
                out.append({
                    "expr": r.expr,
                    "expr_rendered": self._rendered_rule(r),
                    "conds": r.conds,
                    "conds_rendered": [self._render(c) for c in r.conds
                                       if c not in common and c != "otherwise"],
                    "rounded": r.rounded, "self_update": r.self_update,
                })
            return out
        deps = []
        for dep in self._deps(fld):
            entry = {"variable": dep, "label": self.labels.get(dep, dep)}
            if dep in self.subfields:
                parent, a, b = self.subfields[dep]
                entry["subfield_of"] = {"parent": parent, "from": a, "to": b,
                                        "rendered": self._subfield_phrase(dep)}
            else:
                entry["rows"] = _rows(dep)
            deps.append(entry)
        return {"field": fld, "column": self.column_of.get(fld),
                "label": self.labels.get(fld, fld), "steps": _rows(fld),
                "dependencies": deps, "inputs": self.classify_inputs(fld),
                "constants": {k: v for k, v in self.constants.items()}}

    def explain(self, fld: str) -> str:
        """Human-readable, fully resolved business rule for a calculated field."""
        head = f"{self.labels.get(fld, fld)} ({self.column_of.get(fld, '?')}) ← {fld}:"
        lines = [head]
        prev: set[str] = set()
        for r in self.rules.get(fld, []):
            expr = self._rendered_rule(r)
            # a follow-on adjustment repeats the guards of the rule above it —
            # show only what is NEW about this step
            show = [c for c in r.conds if c not in prev] if r.self_update else r.conds
            prev = set(r.conds)
            expr = ("then " if r.self_update else "= ") + expr
            if r.rounded:
                expr += " [ROUNDED]"
            w = self._when(show)
            lines.append(f"  {expr}" + (f", {w}" if w else (" (default)" if not r.conds else "")))
        for dep in self._deps(fld):
            if dep in self.subfields:
                lines.append(f"  where {self.labels.get(dep, dep).lower()} ({dep}) "
                             f"= {self._subfield_phrase(dep)}")
                continue
            rules = self.rules.get(dep, [])
            common = self._common(rules)
            parts = []
            for r in rules:
                expr = self._rendered_rule(r)
                if r.self_update:
                    expr = "then " + expr
                w = self._when([c for c in r.conds if c not in common])
                parts.append(f"{expr} {w}".strip() if w else expr)
            if parts:
                lines.append(f"  where {self.labels.get(dep, dep).lower()} ({dep}) = "
                             + "; ".join(parts))
        return "\n".join(lines)


def _negate(cond: str) -> str:
    def flip(c: str) -> str:
        c = c.strip()
        if " NOT = " in c:
            return c.replace(" NOT = ", " = ", 1)
        for a, b in ((" = ", " NOT = "), (" < ", " >= "), (" > ", " <= ")):
            if a in c:
                return c.replace(a, b, 1)
        return f"NOT ({c})"
    if " AND " in cond:
        return " OR ".join(flip(p) for p in cond.split(" AND "))
    if " OR " in cond:
        return " AND ".join(flip(p) for p in cond.split(" OR "))
    return flip(cond)


def build(cobol_text: str, copybook: list[tuple[str, str]],
          name_to_col: dict[str, str], labels_by_col: dict[str, str],
          decode_by_col: dict[str, dict], humanize) -> KGraph:
    """Assemble the graph from the already-extracted evidence."""
    g = KGraph()
    for fname, pic in copybook:
        col = name_to_col.get(fname)
        label = (labels_by_col.get(col) if col else None) or humanize(fname)
        g.add_alignment(fname, col, label, pic)
    g.decode = dict(decode_by_col)
    g.add_storage(cobol_text, humanize)
    g.add_dataflow(cobol_text)
    return g
