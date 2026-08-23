"""kgstore — the PERSISTED knowledge graph (the certified asset of Flow A).

Flow A (analyst + legacy expert) runs once — and again only when inputs change —
producing a versioned knowledge graph plus the source dictionary. Flow B
(mapping, validation, review) consumes a chosen (ideally certified) version.

Storage is DuckDB in its own file (default data/knowledge.duckdb), separate
from the staging warehouse: the warehouse is a disposable cache, the knowledge
store is the asset. Tables:

  kg_version   one row per Flow A run: fingerprint, status lifecycle
               (draft -> certified -> superseded), certification audit
  kg_input     the fingerprinted inputs (name, kind, sha256) per version
  kg_node      graph nodes: id, kind, label, props JSON, provenance, confidence
  kg_edge      graph edges: src, dst, kind, props JSON, provenance
  kg_rule      REIFIED business rules: resolved plain-English text, raw COBOL
               evidence, structured decision tables, input roles, provenance
  kg_artifact  serialized agent artifacts (insight, dictionary) so Flow B can
               start without re-running Flow A

Provenance values: 'parser' (deterministic extraction), 'llm' (model-narrated),
'human' (SME certification / edits). Every node, edge and rule carries one.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .agents.contracts import EnrichedDictionary, TableInsight
from .config import llm_label, llm_ready

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_version (
    version INTEGER PRIMARY KEY, created_at TIMESTAMP, status VARCHAR,
    fingerprint VARCHAR, source_table VARCHAR, row_count INTEGER,
    certified_by VARCHAR, certified_at TIMESTAMP, notes VARCHAR);
CREATE TABLE IF NOT EXISTS kg_input (
    version INTEGER, name VARCHAR, kind VARCHAR, sha256 VARCHAR);
CREATE TABLE IF NOT EXISTS kg_node (
    version INTEGER, id VARCHAR, kind VARCHAR, label VARCHAR,
    props JSON, provenance VARCHAR, confidence DOUBLE);
CREATE TABLE IF NOT EXISTS kg_edge (
    version INTEGER, src VARCHAR, dst VARCHAR, kind VARCHAR,
    props JSON, provenance VARCHAR);
CREATE TABLE IF NOT EXISTS kg_rule (
    version INTEGER, id VARCHAR, field VARCHAR, target_column VARCHAR,
    program VARCHAR, paragraphs JSON, resolved VARCHAR, cobol VARCHAR,
    decision_tables JSON, inputs JSON, provenance VARCHAR, status VARCHAR);
CREATE TABLE IF NOT EXISTS kg_artifact (
    version INTEGER, kind VARCHAR, payload JSON);
CREATE TABLE IF NOT EXISTS kg_edit (
    version INTEGER, kind VARCHAR, key VARCHAR, patch JSON,
    edited_by VARCHAR, edited_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS kg_relset (
    fingerprint VARCHAR, tables JSON, payload JSON, created_at TIMESTAMP);
"""


# Bump when extraction/enrichment logic changes shape: a knowledge version is
# a function of (inputs x engine), so an engine upgrade must invalidate the
# fingerprint match and trigger a fresh build instead of replaying stale
# artifacts produced by older logic.
ENGINE_VERSION = "3"


from .hashing import file_sha256   # noqa: F401  (re-exported: callers import it from here)


def fingerprint_inputs(inputs: list[dict]) -> str:
    """Combined fingerprint over the named inputs: order-independent, so the
    same files always yield the same fingerprint."""
    lines = sorted(f"{i['kind']}:{i['name']}:{i['sha256']}" for i in inputs)
    lines.append(f"engine::{ENGINE_VERSION}")
    # enrichment mode is part of what a version IS: knowledge built offline
    # must not replay once an LLM comes alive (and vice versa)
    lines.append(f"enrichment::{llm_label() if llm_ready() else 'offline'}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def collect_inputs(source_csv: str, code_dir: str) -> list[dict]:
    """Fingerprint everything Flow A depends on: the extract + every code file."""
    inputs = [{"name": Path(source_csv).name, "kind": "source",
               "sha256": file_sha256(source_csv)}]
    code = Path(code_dir)
    cobol = ("*.cbl", "*.cob", "*.cpy", "*.cobol", "*.cobc")
    screen = ("*.php", "*.inc", "*.phtml", "*.jsp", "*.asp", "*.html")
    for globs, kind in ((cobol, "cobol"), (screen, "screen")):
        for p in sorted({q for g in globs for q in code.glob(g)}):
            inputs.append({"name": p.name, "kind": kind, "sha256": file_sha256(p)})
    return inputs


class KGStore:
    def __init__(self, db_path: str = "data/knowledge.duckdb"):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(db_path)
        for stmt in _SCHEMA.strip().split(";"):
            if stmt.strip():
                self.con.execute(stmt)

    # ------------------------------------------------------------ versioning
    def new_version(self, inputs: list[dict], source_table: str,
                    row_count: int, notes: str = "") -> int:
        v = (self.con.execute("SELECT coalesce(max(version),0)+1 FROM kg_version")
             .fetchone()[0])
        self.con.execute(
            "INSERT INTO kg_version VALUES (?,?,?,?,?,?,NULL,NULL,?)",
            [v, datetime.now(timezone.utc), "draft", fingerprint_inputs(inputs),
             source_table, row_count, notes])
        for i in inputs:
            self.con.execute("INSERT INTO kg_input VALUES (?,?,?,?)",
                             [v, i["name"], i["kind"], i["sha256"]])
        return v

    def find_by_fingerprint(self, fp: str) -> int | None:
        """Healthy versions only: degraded runs (LLM configured but failed)
        never satisfy a replay — the next run retries instead."""
        r = self.con.execute(
            "SELECT max(version) FROM kg_version WHERE fingerprint = ? "
            "AND coalesce(notes, '') NOT LIKE 'degraded%'", [fp]).fetchone()
        return r[0] if r and r[0] is not None else None

    def latest(self, certified_only: bool = False) -> int | None:
        q = "SELECT max(version) FROM kg_version"
        if certified_only:
            q += " WHERE status = 'certified'"
        r = self.con.execute(q).fetchone()
        return r[0] if r and r[0] is not None else None

    def latest_for_table(self, table: str,
                         certified_only: bool = False) -> int | None:
        """Newest (preferring certified) knowledge version for ONE source
        file — the multi-source lookup: each file has its own versions."""
        q = "SELECT max(version) FROM kg_version WHERE source_table = ?"
        if certified_only:
            q += " AND status = 'certified'"
        r = self.con.execute(q, [table]).fetchone()
        return r[0] if r and r[0] is not None else None

    # ------------------------------------------------- cross-file knowledge
    # Relationships span source files, so they are keyed by a fingerprint over
    # the participating files' input fingerprints — not by any one version.
    def save_relationships(self, fp: str, tables: list[str],
                           payload: dict) -> None:
        self.con.execute("DELETE FROM kg_relset WHERE fingerprint=?", [fp])
        self.con.execute(
            "INSERT INTO kg_relset VALUES (?,?,?,?)",
            [fp, json.dumps(sorted(tables)), json.dumps(payload),
             datetime.now(timezone.utc)])

    def load_relationships(self, fp: str) -> dict | None:
        r = self.con.execute(
            "SELECT payload FROM kg_relset WHERE fingerprint=?", [fp]).fetchone()
        return json.loads(r[0]) if r else None

    def latest_relationships(self) -> dict | None:
        r = self.con.execute(
            "SELECT payload FROM kg_relset ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return json.loads(r[0]) if r else None

    def meta(self, version: int) -> dict | None:
        r = self.con.execute("SELECT * FROM kg_version WHERE version = ?",
                             [version]).fetchone()
        if not r:
            return None
        cols = [d[0] for d in self.con.description]
        return dict(zip(cols, r))

    def versions(self) -> list[dict]:
        rows = self.con.execute(
            "SELECT * FROM kg_version ORDER BY version").fetchall()
        cols = [d[0] for d in self.con.description]
        return [dict(zip(cols, r)) for r in rows]

    def certify(self, version: int, by: str, notes: str = "") -> None:
        """SME sign-off: this version becomes THE certified knowledge; any
        previously certified version is superseded. Rules inherit the status."""
        self.con.execute(
            "UPDATE kg_version SET status='superseded' WHERE status='certified'")
        self.con.execute(
            "UPDATE kg_version SET status='certified', certified_by=?, "
            "certified_at=?, notes=? WHERE version=?",
            [by, datetime.now(timezone.utc), notes, version])
        self.con.execute(
            "UPDATE kg_rule SET status='certified' WHERE version=?", [version])

    def is_stale(self, version: int, inputs: list[dict]) -> bool:
        m = self.meta(version)
        return bool(m) and m["fingerprint"] != fingerprint_inputs(inputs)

    # ------------------------------------------------------------- amendments
    # SME corrections BEFORE certification: certified knowledge is immutable,
    # so the workflow is amend the draft -> certify. Every amendment lands in
    # the versioned artifacts (so Flow B and fingerprint replay see the
    # corrected knowledge), flips provenance to 'human', and is journalled in
    # kg_edit for audit.
    def _assert_amendable(self, version: int) -> None:
        m = self.meta(version)
        if not m:
            raise ValueError(f"no knowledge version {version}")
        if m["status"] != "draft":
            raise ValueError(
                f"v{version} is {m['status']} — certified knowledge is "
                "immutable; amendments apply to a draft before certification")

    def _log_edit(self, version: int, kind: str, key: str,
                  patch: dict, by: str) -> None:
        self.con.execute(
            "INSERT INTO kg_edit VALUES (?,?,?,?,?,?)",
            [version, kind, key, json.dumps(patch), by,
             datetime.now(timezone.utc)])

    def _store_artifact(self, version: int, kind: str, payload: dict) -> None:
        self.con.execute(
            "UPDATE kg_artifact SET payload=? WHERE version=? AND kind=?",
            [json.dumps(payload), version, kind])

    def edits(self, version: int) -> list[dict]:
        rows = self.con.execute(
            "SELECT kind, key, patch, edited_by, edited_at FROM kg_edit "
            "WHERE version=? ORDER BY edited_at", [version]).fetchall()
        return [{"kind": r[0], "key": r[1], "patch": json.loads(r[2]),
                 "edited_by": r[3], "edited_at": str(r[4])} for r in rows]

    def amend_pii(self, version: int, column: str, patch: dict,
                  by: str = "SME", note: str = "") -> dict:
        """Correct a PII finding, or flag a column the detector missed."""
        self._assert_amendable(version)
        if note and "rationale" not in patch:
            patch = {**patch, "rationale": note}
        ins = self.load_artifact(version, "insight")
        if column not in {c["name"] for c in ins.get("columns", [])}:
            raise ValueError(f"'{column}' is not a column of this source")
        findings = ins.setdefault("pii", [])
        f = next((x for x in findings if x["column"] == column), None)
        if f is None:
            f = {"column": column, "is_pii": True, "category": "None",
                 "sensitivity": "medium", "confidence": 1.0,
                 "method": "human", "rationale": "",
                 "sample_evidence": [], "recommended_action": "retain"}
            findings.append(f)
        allowed = ("is_pii", "category", "sensitivity", "rationale",
                   "recommended_action")
        for k in allowed:
            if k in patch:
                f[k] = patch[k]
        if not f["is_pii"]:
            f["category"], f["sensitivity"] = "None", "none"
        f["method"], f["confidence"] = "human", 1.0
        flagged = [x for x in findings if x["is_pii"]]
        ins["pii_summary"] = {**ins.get("pii_summary", {}),
                              "pii_columns": len(flagged),
                              "amended_by_sme": True}
        self._store_artifact(version, "insight", ins)
        self._log_edit(version, "pii", column, patch, by)
        return f

    def amend_dict(self, version: int, name: str, patch: dict,
                   by: str = "SME", note: str = "") -> dict:
        """Correct a dictionary entry: business name, description, decodes.
        The matching graph node is kept in sync so lineage shows the same
        labels the dictionary does."""
        self._assert_amendable(version)
        d = self.load_artifact(version, "dictionary")
        c = next((x for x in d["columns"] if x["name"] == name), None)
        if c is None:
            raise ValueError(f"'{name}' is not in the dictionary")
        allowed = ("business_name", "description", "value_decode")
        applied = [k for k in allowed if k in patch]
        for k in applied:
            c[k] = patch[k]
        c["confidence"] = 1.0
        c.setdefault("sources", [])
        if "human" not in c["sources"]:
            c["sources"].append("human")
        c.setdefault("evidence", []).append(
            f"SME correction by {by}: {', '.join(applied) or 'no fields'}"
            + (f" — {note}" if note else ""))
        self._store_artifact(version, "dictionary", d)
        r = self.con.execute(
            "SELECT props FROM kg_node WHERE version=? AND id=?",
            [version, f"col:{name}"]).fetchone()
        if r:
            props = json.loads(r[0])
            props["description"] = c.get("description", "")
            props["decode"] = c.get("value_decode", {})
            self.con.execute(
                "UPDATE kg_node SET label=?, props=?, provenance='human', "
                "confidence=1.0 WHERE version=? AND id=?",
                [c["business_name"], json.dumps(props), version, f"col:{name}"])
        self._log_edit(version, "dictionary", name, patch, by)
        return c

    def amend_rule(self, version: int, rule_id: str, patch: dict,
                   by: str = "SME", note: str = "") -> dict:
        """Correct the business-English statement of a reified rule. The
        deterministic COBOL resolution is the audit layer and stays machine-
        owned; the SME's wording becomes the narrative, provenance 'human'.
        rule_id resolves as the node id ('rule:PR-X') or the COBOL field."""
        self._assert_amendable(version)
        r = self.con.execute(
            "SELECT id, field, decision_tables FROM kg_rule "
            "WHERE version=? AND (id=? OR id=? OR field=?)",
            [version, rule_id, f"rule:{rule_id}", rule_id]).fetchone()
        if not r:
            raise ValueError(f"no rule '{rule_id}' in v{version}")
        rid, field, dt = r[0], r[1], json.loads(r[2])
        narrative = (patch.get("narrative") or "").strip()
        if not narrative:
            raise ValueError("amended rule text is empty")
        dt["narrative"] = narrative
        dt["narrative_source"] = f"SME ({by})" + (f" — {note}" if note else "")
        self.con.execute(
            "UPDATE kg_rule SET decision_tables=?, provenance='human' "
            "WHERE version=? AND id=?",
            [json.dumps(dt), version, rid])
        self.con.execute(
            "UPDATE kg_node SET provenance='human', confidence=1.0 "
            "WHERE version=? AND id=?", [version, rid])
        d = self.load_artifact(version, "dictionary")
        c = next((x for x in d["columns"] if x.get("cobol_name") == field), None)
        if c:
            c["derivation_narrative"] = narrative
            self._store_artifact(version, "dictionary", d)
        self._log_edit(version, "rule", rid, patch, by)
        return {"id": rid, "narrative": narrative, "provenance": "human"}

    def amend_dq(self, version: int, rule_id: str, patch: dict,
                 by: str = "SME", note: str = "") -> dict:
        """SME judgement on a data-quality rule — typically suppression when
        the rule is wrong for this book (annotated, kept in the SQL library
        as a commented-out block rather than silently deleted)."""
        self._assert_amendable(version)
        ins = self.load_artifact(version, "insight")
        r = next((x for x in ins.get("dq_rules", []) if x["id"] == rule_id), None)
        if r is None:
            raise ValueError(f"no DQ rule '{rule_id}' in v{version}")
        allowed = ("suppressed", "severity", "description")
        for k in allowed:
            if k in patch:
                r[k] = patch[k]
        if "suppressed" in patch:
            r["suppress_note"] = (f"{by}: {note}" if note else by) \
                                 if patch["suppressed"] else ""
        self._store_artifact(version, "insight", ins)
        self._log_edit(version, "dq_rule", rule_id, patch, by)
        return r

    def apply_edit(self, version: int, kind: str, key: str, field: str,
                   value, by: str = "SME", note: str = "") -> dict:
        """Single-field SME curation edit — the primitive behind the UI's
        multi-field amendments. kinds: pii | column | dq_rule | rule."""
        patch = {field: value}
        fn = {"pii": self.amend_pii, "column": self.amend_dict,
              "dictionary": self.amend_dict, "dq_rule": self.amend_dq,
              "rule": self.amend_rule}.get(kind)
        if not fn:
            raise ValueError("kind must be pii, column, dq_rule or rule")
        return fn(version, key, patch, by, note=note)

    # -------------------------------------------------------------- persisting
    def save(self, version: int, insight: TableInsight,
             enriched: EnrichedDictionary, evidence: dict,
             default_provenance: str = "parser") -> dict:
        """Persist the whole Flow A output for one version: graph nodes/edges,
        reified rules, and the serialized artifacts Flow B starts from."""
        kg, cob = evidence["kg"], evidence["cob"]
        nodes, edges = [], []

        def node(nid, kind, label, props=None, prov="parser", conf=0.9):
            nodes.append((version, nid, kind, label,
                          json.dumps(props or {}), prov, conf))

        def edge(src, dst, kind, props=None, prov="parser"):
            edges.append((version, src, dst, kind, json.dumps(props or {}), prov))

        # ---- columns (the extract) — labelled and decoded
        by_col = {c.name: c for c in enriched.columns}
        for i, c in enumerate(enriched.columns):
            node(f"col:{c.name}", "column", c.business_name,
                 {"position": i + 1, "type": c.inferred_type,
                  "description": c.description, "decode": c.value_decode},
                 default_provenance, c.confidence)
            if c.screen_label:
                node(f"scr:{c.name}", "screen_label", c.screen_label,
                     {"source": "screen"})
                edge(f"col:{c.name}", f"scr:{c.name}", "LABELLED_AS")

        # ---- record, fields, alignment
        rec = cob.get("record_name") or "SOURCE-RECORD"
        node(f"rec:{rec}", "record", rec)
        for i, (fname, pic) in enumerate(cob["copybook"]):
            node(f"fld:{fname}", "field", kg.labels.get(fname, fname),
                 {"pic": pic, "position": i + 1})
            edge(f"rec:{rec}", f"fld:{fname}", "HAS_FIELD", {"position": i + 1})
            col = evidence["name_to_col"].get(fname)
            if col:
                edge(f"fld:{fname}", f"col:{col}", "ALIGNED_TO",
                     {"position": i + 1})

        # ---- programs, files, datasets
        for prog, io in cob.get("io", {}).items():
            node(f"pgm:{prog}", "program", prog,
                 {"copy_members": io.get("copy_members", [])})
            for fname, dataset in io.get("files", {}).items():
                node(f"ds:{dataset}", "dataset", dataset, {"file": fname})
                edge(f"pgm:{prog}", f"ds:{dataset}", "READS",
                     {"file": fname, "mode": "I-O"})
                edge(f"rec:{rec}", f"ds:{dataset}", "STORED_ON")

        # ---- working-storage dataflow: DEPENDS_ON for impact queries
        seen_vars = set()
        for var, rules in kg.rules.items():
            deps = set()
            for r in rules:
                toks = [kg.resolve_token(x) for x in
                        re.findall(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b",
                                   r.expr + " " + " ".join(r.conds))]
                deps |= {t for t in toks
                         if t != var and (t in kg.rules or t in kg.subfields
                                          or t in kg.column_of or t in kg.constants)}
            if not deps:
                continue
            if var not in kg.column_of and var not in seen_vars:
                node(f"var:{var}", "variable", kg.labels.get(var, var),
                     {"pic": kg.pics.get(var)})
                seen_vars.add(var)
            src = f"fld:{var}" if var in kg.column_of else f"var:{var}"
            for d in deps:
                dst = (f"fld:{d}" if d in kg.column_of else f"var:{d}")
                if d in kg.constants and d not in kg.rules:
                    node(f"var:{d}", "constant", kg.labels.get(d, d),
                         {"value": kg.constants[d]})
                    dst = f"var:{d}"
                elif d not in kg.column_of and d not in seen_vars:
                    node(f"var:{d}", "variable", kg.labels.get(d, d),
                         {"pic": kg.pics.get(d)})
                    seen_vars.add(d)
                edge(src, dst, "DEPENDS_ON", {"via": "dataflow"})
        for child, (parent, a, b) in kg.subfields.items():
            if child not in seen_vars:
                node(f"var:{child}", "variable", kg.labels.get(child, child), {})
                seen_vars.add(child)
            edge(f"var:{child}", f"var:{parent}", "SUBFIELD_OF",
                 {"from": a, "to": b})

        # ---- REIFIED business rules
        rule_rows = []
        for c in enriched.columns:
            if not c.derivation_cobol:
                continue
            der = cob["derivations"][c.cobol_name]
            rid = f"rule:{c.cobol_name}"
            prov = default_provenance
            node(rid, "rule", f"{c.business_name} calculation",
                 {"program": c.derived_in_program,
                  "parse_coverage": der.get("coverage")}, prov, c.confidence)
            edge(f"fld:{c.cobol_name}", rid, "DERIVED_BY")
            edge(rid, f"pgm:{c.derived_in_program}", "DEFINED_IN",
                 {"paragraphs": der["paragraphs"]})
            structure = kg.rule_structure(c.cobol_name)
            structure["coverage"] = der.get("coverage")
            structure["resolved_calc"] = c.derivation_resolved
            structure["narrative"] = c.derivation_narrative
            structure["lineage"] = c.derivation_lineage
            for fld, roles in structure["inputs"].items():
                edge(rid, f"fld:{fld}", "USES", {"roles": roles})
            rule_rows.append((
                version, rid, c.cobol_name, c.name, c.derived_in_program,
                json.dumps(der["paragraphs"]), c.derivation, c.derivation_cobol,
                json.dumps(structure), json.dumps(structure["inputs"]),
                prov, "draft"))

        if nodes:
            self.con.executemany(
                "INSERT INTO kg_node VALUES (?,?,?,?,?,?,?)", nodes)
        if edges:
            self.con.executemany(
                "INSERT INTO kg_edge VALUES (?,?,?,?,?,?)", edges)
        if rule_rows:
            self.con.executemany(
                "INSERT INTO kg_rule VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rule_rows)
        self.con.execute("INSERT INTO kg_artifact VALUES (?,?,?)",
                         [version, "insight", insight.model_dump_json()])
        self.con.execute("INSERT INTO kg_artifact VALUES (?,?,?)",
                         [version, "dictionary", enriched.model_dump_json()])
        return {"nodes": len(nodes), "edges": len(edges), "rules": len(rule_rows)}

    # ---------------------------------------------------------------- loading
    def load_artifact(self, version: int, kind: str) -> dict | None:
        r = self.con.execute(
            "SELECT payload FROM kg_artifact WHERE version=? AND kind=?",
            [version, kind]).fetchone()
        return json.loads(r[0]) if r else None

    def load_insight(self, version: int) -> TableInsight:
        return TableInsight.model_validate(self.load_artifact(version, "insight"))

    def load_dictionary(self, version: int) -> EnrichedDictionary:
        return EnrichedDictionary.model_validate(
            self.load_artifact(version, "dictionary"))

    def rules(self, version: int) -> list[dict]:
        rows = self.con.execute(
            "SELECT * FROM kg_rule WHERE version=? ORDER BY id", [version]).fetchall()
        cols = [d[0] for d in self.con.description]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["paragraphs"] = json.loads(d["paragraphs"])
            d["decision_tables"] = json.loads(d["decision_tables"])
            d["inputs"] = json.loads(d["inputs"])
            out.append(d)
        return out

    def export_json(self, version: int) -> dict:
        """Whole graph as one JSON document (UI / portability / audit)."""
        def rows(q, params):
            cur = self.con.execute(q, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        nodes = rows("SELECT id, kind, label, props, provenance, confidence "
                     "FROM kg_node WHERE version=?", [version])
        edges = rows("SELECT src, dst, kind, props, provenance "
                     "FROM kg_edge WHERE version=?", [version])
        for n in nodes:
            n["props"] = json.loads(n["props"])
        for e in edges:
            e["props"] = json.loads(e["props"])
        meta = self.meta(version)
        if meta:
            for k in ("created_at", "certified_at"):
                if meta.get(k) is not None:
                    meta[k] = str(meta[k])
        return {"meta": meta,
                "inputs": rows("SELECT name, kind, sha256 FROM kg_input "
                               "WHERE version=?", [version]),
                "nodes": nodes, "edges": edges, "rules": self.rules(version),
                "edits": self.edits(version)}

    # ----------------------------------------------------------------- queries
    def find(self, version: int, q: str, limit: int = 12) -> list[dict]:
        """Friendly search over the graph: matches ids and labels."""
        ql = f"%{q.strip().lower()}%"
        rows = self.con.execute(
            "SELECT id, kind, label FROM kg_node WHERE version=? AND "
            "(lower(id) LIKE ? OR lower(label) LIKE ?) "
            "ORDER BY length(label), id LIMIT ?",
            [version, ql, ql, limit]).fetchall()
        return [{"id": r[0], "kind": r[1], "label": r[2]} for r in rows]

    def resolve(self, version: int, q: str) -> str | None:
        """Resolve a human term (XA06, 'exit date', PR-EXIT-DT, BONCALC...)
        to a node id — exact id first, then prefixed forms, then label match."""
        q = q.strip()
        ids = {r[0] for r in self.con.execute(
            "SELECT id FROM kg_node WHERE version=?", [version]).fetchall()}
        if q in ids:
            return q
        up = q.upper()
        for pfx in ("col", "fld", "rule", "var", "pgm", "ds", "rec", "scr"):
            if f"{pfx}:{up}" in ids:
                return f"{pfx}:{up}"
        hits = self.find(version, q, 1)
        return hits[0]["id"] if hits else None

    def nodes_info(self, version: int, ids: list[str]) -> dict:
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        rows = self.con.execute(
            f"SELECT id, kind, label, props, provenance, confidence "
            f"FROM kg_node WHERE version=? AND id IN ({ph})",
            [version, *ids]).fetchall()
        return {r[0]: {"kind": r[1], "label": r[2], "props": json.loads(r[3]),
                       "provenance": r[4], "confidence": r[5]} for r in rows}


    def lineage(self, version: int, node_id: str, depth: int = 4) -> list[dict]:
        """Edges reachable from a node in either direction (impact/lineage).
        Hub nodes (record, program, dataset) are reported but not expanded
        THROUGH — otherwise every query fans out to the whole record."""
        hubs = ("rec:", "pgm:", "ds:")
        seen, frontier, out = {node_id}, {node_id}, []
        for _ in range(depth):
            if not frontier:
                break
            ph = ",".join("?" * len(frontier))
            rows = self.con.execute(
                f"SELECT src, dst, kind, props FROM kg_edge WHERE version=? "
                f"AND (src IN ({ph}) OR dst IN ({ph}))",
                [version, *frontier, *frontier]).fetchall()
            frontier = set()
            for src, dst, kind, props in rows:
                rec = {"src": src, "dst": dst, "kind": kind,
                       "props": json.loads(props)}
                if rec not in out:
                    out.append(rec)
                for n in (src, dst):
                    if n not in seen and not n.startswith(hubs):
                        seen.add(n); frontier.add(n)
                    seen.add(n)
        return out

    def close(self):
        self.con.close()
