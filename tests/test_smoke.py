"""Smoke tests — no Azure needed (agents use their offline stubs)."""
from pathlib import Path as _Path


def _target_dict_path() -> str:
    """Resolve the target dictionary by GLOBBING data/target_dict/, never by a
    fixed filename.

    In the product the target dictionary is uploaded by the user, so its name is
    arbitrary — the app reads whatever JSON it is handed. A test that hardcodes
    `data/target_dictionary.json` therefore asserts something the application
    never promises, and silently breaks the whole suite when the fixture is
    renamed. Globbing keeps the tests honest about the real contract.
    """
    d = _Path("data/target_dict")
    files = sorted(d.glob("*.json"))
    if not files:
        raise FileNotFoundError(
            f"no target dictionary in {d}/ — drop any *.json there "
            "(name does not matter; the app takes whatever the user uploads)")
    if len(files) > 1:
        raise RuntimeError(
            f"{d}/ holds {len(files)} JSON files ({[f.name for f in files]}); "
            "the fixture is ambiguous — keep exactly one")
    return str(files[0])


TARGET_DICT = _target_dict_path()


def _target_dict(**attr_overrides):
    """The fixture dictionary, with named attributes patched.

    A few tests depend on a specific target DEFINITION, not just on some target
    existing — e.g. "a coded source whose decoded labels match nothing in the
    target domain must be rejected" only means anything if the target domain
    really doesn't contain those labels. Reading that premise out of whatever
    JSON happens to sit in data/target_dict/ makes the test silently change
    meaning when the fixture does. So those tests state their premise inline
    here instead, and the fixture supplies everything else.

    Usage: _target_dict(gone_away={"allowed_values": ["GONE_AWAY", "CONTACTABLE"]})
    """
    import json as __json
    td = __json.loads(open(TARGET_DICT).read())
    for a in td.get("attributes", []):
        if a["name"] in attr_overrides:
            a.update(attr_overrides[a["name"]])
    return td


# The gone-away premise, shared by the tests below: XA22 decodes to
# 'On Record' / 'Untraceable', so a target domain of GONE_AWAY / CONTACTABLE
# gives the matcher NO value evidence — which is the situation under test.
_SEMANTIC_GONE_AWAY = {"gone_away": {"allowed_values": ["GONE_AWAY", "CONTACTABLE"]}}

from engine.orchestration.graph import app


def test_graph_renders():
    m = app.get_graph().draw_mermaid()
    assert "analyst" in m and "mapping" in m and "validation" in m


def test_pipeline_end_to_end():
    out = app.invoke({
        "source_csv": "data/EFAS0042.csv", "table": "EFAS0042",
        "code_dir": "data/legacy", "target_dict_path": TARGET_DICT,
        "warehouse_path": ":memory:",
    })
    # every agent produced its artifact
    assert out["insight"].table == "EFAS0042"
    assert out["enriched"].columns and out["spec"].stats["mapped"] >= 10
    assert out["report"].verdict in ("certified", "needs_review", "blocked")


# ---- Agent 1: analyst ----
from engine.agents.analyst import analyze


def test_analyst_recovers_structure():
    t = analyze("data/EFAS0042.csv", "EFAS0042")
    assert "POLNO" in t.candidate_keys            # policy number = key
    assert set(t.dead_columns) == {"XA08", "XA14"}
    # scheme number conditional on product code
    dep = {(d.dependent, tuple(d.drivers)) for d in t.dependencies}
    assert ("SCHNO", ("PRODCD",)) in dep
    # exit date is sentinel-aware and conditionally populated
    xa06 = next(c for c in t.columns if c.name == "EXITDT")
    assert "00000000" in xa06.sentinels and xa06.populated_fraction < 1.0


# ---- Agent 2: legacy expert ----
from engine.agents.legacy_expert import enrich, extract_cobol, extract_php


def _code(ext):
    from pathlib import Path
    pats = {"cbl": ("*.cbl", "*.cob", "*.cpy"), "php": ("*.php",)}[ext]
    return "\n".join(p.read_text() for pat in pats
                     for p in sorted(Path("data/legacy").glob(pat)))


def test_legacy_expert_decodes_meaning():
    t = analyze("data/EFAS0042.csv", "EFAS0042")
    e = enrich(t, _code("cbl"), _code("php"))
    by = {c.name: c for c in e.columns}
    assert by["POLNO"].business_name == "Policy Number"          # opaque -> named
    assert by["PRODCD"].value_decode.get("GPEN") == "Group Pension"  # code decoded
    assert by["XA08"].screen_label is None and by["XA08"].confidence < 0.9  # filler, off-screen
    assert any("Group Pension" in r for r in e.rules)           # rule decoded
    assert by["POLNO"].cobol_name == "PR-POLICY-NO"              # positional copybook map


def test_legacy_expert_extracts_calculation_logic():
    """LOYBONUS/EXTPNPCT are batch-calculated by BONCALC and absent from the screen —
    the derivation must be recovered from the COBOL procedure code alone."""
    t = analyze("data/EFAS0042.csv", "EFAS0042")
    e = enrich(t, _code("cbl"), _code("php"))
    by = {c.name: c for c in e.columns}

    lb, ep = by["LOYBONUS"], by["EXTPNPCT"]
    # positional copybook map survives the second COBOL program's working storage
    assert lb.cobol_name == "PR-LOYALTY-BONUS" and ep.cobol_name == "PR-EXIT-PEN-PCT"
    assert lb.screen_label is None and ep.screen_label is None   # not on the screen
    # the calculation was found, attributed, and explained
    assert lb.derived_in_program == "BONCALC" and ep.derived_in_program == "BONCALC"
    assert "cobol-calc" in lb.sources and "cobol-calc" in ep.sources
    assert "COMPUTE" in lb.derivation_cobol and "1000-DERIVE-TENURE" in lb.derivation_cobol
    assert "tenure" in lb.derivation.lower() and "paid up" in lb.derivation.lower()
    # all of this is now discovered from CODE alone — the sources carry no
    # narrative comments: decodes come from 88s/screen, structure from slicing
    assert "surrender" in ep.derivation.lower()          # [Surrender] decode
    assert "result + 2.00" in ep.derivation              # the MVA loading step
    assert "10.00 when" in ep.derivation                 # the cap rule
    # lineage: inputs traced to the record, copybook, file and physical dataset
    lin = lb.derivation_lineage
    assert lin["record"] == "EFAS-POLICY-RECORD" and lin["copybook"] == "EFASPOL"
    assert lin["file"] == "POLICY-FILE" and lin["dataset"] == "EFAS0042"
    assert lin["inputs"]["PR-SUM-ASSURED"] == "SUMASSD"
    assert lin["inputs"]["PR-STATUS"] == "STATCD" and lin["inputs"]["PR-PROD-CODE"] == "PRODCD"
    assert set(ep.derivation_lineage["inputs"]) == {
        "PR-PROD-CODE", "PR-COMMENCE-DT", "PR-STATUS", "PR-EXIT-DT", "PR-EXIT-RSN"}
    assert "EFAS0042" in lb.derivation      # lineage sentence rides with the rule
    # two-layer contract: the deterministic resolution is its own field (the
    # audit layer) and offline runs carry no LLM narrative
    assert "Exit Date (EXITDT)" in lb.derivation_resolved
    assert lb.derivation_narrative is None
    # knowledge-graph resolution: WS intermediates traced back to labelled fields
    assert "Exit Date (EXITDT)" in lb.derivation and "Policy Status (STATCD)" in lb.derivation
    assert "valuation date" in lb.derivation           # constant resolved with meaning
    assert "[Closed]" in lb.derivation                 # literals decoded via screen/COBOL
    assert "year part" in lb.derivation                # REDEFINES sub-field resolved
    assert "Sum Assured (SUMASSD)" in lb.derivation and "0.50" in lb.derivation
    assert "1.20" in ep.derivation and "10.00" in ep.derivation   # penalty formula + cap
    # ordinary fields must NOT acquire a derivation (MOVE SPACES is housekeeping)
    assert by["SCHNO"].derivation is None and by["POLNO"].derivation is None


def test_mapping_calculated_fields_held_for_review():
    """Pass-through mappings of calculated source fields validate at full
    coverage but are gated at review so a human confirms the extracted logic."""
    s = _spec()
    by = {m.target_attribute: m for m in s.mappings}
    lb, ep = by["loyalty_bonus_amount"], by["early_exit_penalty_percent"]
    assert lb.source_attributes == ["LOYBONUS"] and ep.source_attributes == ["EXTPNPCT"]
    assert lb.validation_coverage == 1.0 and ep.validation_coverage == 1.0
    assert lb.gate == "review" and ep.gate == "review"
    assert "BONCALC" in lb.rationale and "BONCALC" in ep.rationale


# ---- Agent 3: data mapping agent ----
import json as _json
from engine.agents.legacy_expert import enrich as _enrich
from engine.agents.mapping_agent import map_to_target
from engine.staging import Warehouse


def _spec():
    # _SEMANTIC_GONE_AWAY: these tests assert the three-tier gate SPREAD, which
    # needs one honest reject. That reject is gone_away — a coded source whose
    # decoded labels land nowhere in the target domain. State it rather than
    # inherit it from the fixture (see _target_dict).
    insight = analyze("data/EFAS0042.csv", "EFAS0042")
    e = _enrich(insight, _code("cbl"), _code("php"))
    td = _target_dict(**_SEMANTIC_GONE_AWAY)
    wh = Warehouse(":memory:"); wh.stage_csv("data/EFAS0042.csv", "EFAS0042", all_varchar=True)
    return map_to_target(e, td, wh, "EFAS0042")


def test_mapping_spec_elements():
    s = _spec()
    by = {m.target_attribute: m for m in s.mappings}
    # validated 1:1 enum mapping auto-accepts
    assert by["policy_status"].gate == "auto_accept" and by["policy_status"].validation_coverage == 1.0
    # the DTH reconciliation gap is caught and held for review
    assert "DTH" in by["exit_reason"].unmapped_codes and by["exit_reason"].gate == "review"
    # many:1 and derived cardinalities
    assert by["annual_premium"].cardinality == "many:1" and len(by["annual_premium"].source_attributes) == 2
    assert by["is_group_policy"].cardinality == "derived"
    # unmapped both directions
    assert {u["attribute"] for u in s.unmapped_source} >= {"XA08", "XA14"}
    assert {u["attribute"] for u in s.unmapped_target} >= {"source_system", "migrated_at"}
    # ambiguity: two source dates plausibly fit commencement_date -> review w/ alternatives
    cm = by["commencement_date"]
    assert cm.ambiguous and cm.gate == "review"
    assert {a["source"] for a in cm.alternatives} == {"COVSTDT"}
    assert cm.source_attributes[0] == "COMMDT"
    # reject: semantic uncertainty (trace flag ~ gone away, unconfirmable) and no source
    assert by["gone_away"].gate == "reject" and by["gone_away"].source_attributes == ["XA22"]
    # vulnerable_customer_flag is a BOOLEAN target and no source is a real
    # vulnerability flag. CUSTID shares the word "customer" but is a string id,
    # and XA22 is a two-value flag whose meaning (contact trace) is unrelated.
    # The matcher must not force-fit either onto a boolean it has no evidence
    # for -- the target is left UNMAPPED for a human to supply, never proposed.
    assert "vulnerable_customer_flag" not in by
    assert "vulnerable_customer_flag" in {u["attribute"] for u in s.unmapped_target}
    # the spread spans all three tiers
    gates = [m.gate for m in s.mappings]
    assert gates.count("auto_accept") >= 6
    assert gates.count("review") >= 3
    assert gates.count("reject") >= 1        # the spread reaches the bottom tier


# ---- Agent 4: validation agent ----
from engine.agents.validator import validate_spec


def test_validation_report():
    insight = analyze("data/EFAS0042.csv", "EFAS0042")
    e = _enrich(insight, _code("cbl"), _code("php"))
    td = _json.loads(open(TARGET_DICT).read())
    wh = Warehouse(":memory:"); wh.stage_csv("data/EFAS0042.csv", "EFAS0042", all_varchar=True)
    spec = map_to_target(e, td, wh, "EFAS0042")
    r = validate_spec(spec, td, insight, wh, "EFAS0042")
    by = {c.name: c for c in r.checks}
    assert r.verdict == "needs_review"
    assert by["row_count_preserved"].status == "pass"               # grain
    assert by["key_unique_not_null"].status == "pass"               # key integrity
    assert by["crossfield:exit_date~STATCD"].status == "pass"         # business rule holds in target
    assert by["reconciliation:exit_reason"].status == "warn"        # DTH value loss caught
    assert by["reconciliation:exit_reason"].offending_rows >= 1
    assert by["required_present:source_system"].status == "warn"    # required, must default at load


# ---- Agent 5: reviewer + feedback loop ----
from engine.agents.reviewer import build_review_queue, apply_decisions
from engine.agents.validator import validate_spec as _validate


def test_review_queue_and_feedback_loop():
    insight = analyze("data/EFAS0042.csv", "EFAS0042")
    e = _enrich(insight, _code("cbl"), _code("php"))
    td = _json.loads(open(TARGET_DICT).read())
    wh = Warehouse(":memory:"); wh.stage_csv("data/EFAS0042.csv", "EFAS0042", all_varchar=True)
    spec = map_to_target(e, td, wh, "EFAS0042")
    report = _validate(spec, td, insight, wh, "EFAS0042")

    q = build_review_queue(spec, report, e, insight, td)
    items = {i.target_attribute: i for i in q.items}
    # exception-driven: auto-accepted mappings are NOT in the queue
    assert "policy_status" not in items and "policy_status" in q.auto_accepted
    # the DTH item carries provenance + a suggested fix
    ex = items["exit_reason"]
    assert ex.offending_rows and ex.suggested_sql and "DEATH" in ex.suggested_resolution

    # apply the reviewer's suggested edit, then re-validate -> loss resolved
    spec2 = apply_decisions(spec, {"exit_reason": {"action": "edit",
                                                   "transformation_sql": ex.suggested_sql}})
    report2 = _validate(spec2, td, insight, wh, "EFAS0042")
    by = {c.name: c for c in report2.checks}
    assert by["reconciliation:exit_reason"].status == "pass"   # DTH now mapped, no value loss


def test_confirmed_rejects_do_not_block_certification():
    # needs a reject to confirm — same premise as _spec()
    insight = analyze("data/EFAS0042.csv", "EFAS0042")
    e = _enrich(insight, _code("cbl"), _code("php"))
    td = _target_dict(**_SEMANTIC_GONE_AWAY)
    wh = Warehouse(":memory:"); wh.stage_csv("data/EFAS0042.csv", "EFAS0042", all_varchar=True)
    spec = map_to_target(e, td, wh, "EFAS0042")
    report = _validate(spec, td, insight, wh, "EFAS0042")
    q = build_review_queue(spec, report, e, insight, td)

    # human rules on every queue item: accept reviews (edit where suggested), reject the rejects
    decisions = {}
    for it in q.items:
        if it.kind == "unmapped_target":
            continue
        if it.gate == "reject":
            decisions[it.target_attribute] = {"action": "reject"}
        elif it.suggested_sql:
            decisions[it.target_attribute] = {"action": "edit", "transformation_sql": it.suggested_sql}
        else:
            decisions[it.target_attribute] = {"action": "accept"}
    spec2 = apply_decisions(spec, decisions)
    report2 = _validate(spec2, td, insight, wh, "EFAS0042")
    # an unreviewed reject blocks; a reviewer-confirmed one is a deliberate exclusion
    assert report.verdict == "needs_review"
    assert report2.verdict == "certified"
    # every reject the human confirmed stays excluded — the count is whatever
    # the matcher honestly rejected (it rose when the type-only score floor was
    # removed and weak-evidence matches stopped being force-fit), so assert the
    # property, not a magic number
    rejected = {m.target_attribute for m in spec2.mappings if m.gate == "reject"}
    confirmed = {t for t, d in decisions.items() if d["action"] == "reject"}
    assert rejected == confirmed and rejected


def test_unmapped_targets_carry_type_and_suggested_default():
    """An unmapped target must arrive in the queue with its declared type and a
    type-appropriate load-time default the reviewer can accept or change, and a
    supplied default must promote it into a real mapping."""
    insight = analyze("data/EFAS0042.csv", "EFAS0042")
    e = _enrich(insight, _code("cbl"), _code("php"))
    td = _json.loads(open(TARGET_DICT).read())
    wh = Warehouse(":memory:"); wh.stage_csv("data/EFAS0042.csv", "EFAS0042", all_varchar=True)
    spec = map_to_target(e, td, wh, "EFAS0042")
    report = _validate(spec, td, insight, wh, "EFAS0042")
    q = build_review_queue(spec, report, e, insight, td)
    unmapped = {i.target_attribute: i for i in q.items if i.kind == "unmapped_target"}

    # vulnerable_customer_flag is boolean and has no source -> default "false"
    vcf = unmapped["vulnerable_customer_flag"]
    assert vcf.target_type == "boolean"
    assert vcf.suggested_default == "false" and vcf.suggested_sql == "false"
    # source_system is system-owned: the queue must propose the SAME value the
    # ETL actually loads (the originating file), not NULL — a NULL default on a
    # non-nullable target is guaranteed to fail the completeness check the
    # moment the reviewer accepts it.
    ss = unmapped["source_system"]
    assert ss.target_type == "string"
    assert ss.suggested_default == "EFAS0042" and ss.suggested_sql == "'EFAS0042'"

    # accepting the suggested default promotes it into a concrete mapping
    spec2 = apply_decisions(spec, {"vulnerable_customer_flag":
                                   {"action": "edit", "transformation_sql": vcf.suggested_sql}})
    by = {m.target_attribute: m for m in spec2.mappings}
    assert "vulnerable_customer_flag" in by
    assert by["vulnerable_customer_flag"].transformation_sql == "false"
    assert by["vulnerable_customer_flag"].gate == "auto_accept"
    assert "vulnerable_customer_flag" not in {u["attribute"] for u in spec2.unmapped_target}


# ---- PII + data quality scan (analyst) ----
def test_analyst_pii_and_dq():
    from engine.agents.analyst import analyze
    ins = analyze("data/EFAS0042.csv", "EFAS0042")
    pii = {f.column: f for f in ins.pii if f.is_pii}
    assert ins.pii_summary["pii_columns"] >= 4
    assert pii["NINO"].category == "National Insurance Number"   # found from values alone
    assert pii["NINO"].sensitivity == "high"
    assert pii["PCODE"].category == "Postcode"
    assert pii["MBRNAME"].category == "Name"
    assert pii["BIRTHDT"].category == "Date of Birth"
    assert "SCHNM" not in pii                                     # scheme name is NOT a person
    dq = {f.column: f for f in ins.dq}
    assert dq["XA08"].severity != "ok"                          # dead column flagged
    assert dq["POLNO"].validity == 1.0                           # policy number well-formed


# ---- Knowledge store: persistence, versioning, certification, two flows ----
from engine.kgstore import KGStore, collect_inputs, fingerprint_inputs   # noqa: E402
from engine.orchestration.graph import flow_a, flow_b                    # noqa: E402


def test_kg_persist_certify_and_reload(tmp_path):
    kg_path = str(tmp_path / "knowledge.duckdb")
    out = flow_a.invoke({"source_csv": "data/EFAS0042.csv",
                         "code_dir": "data/legacy",
                         "warehouse_path": ":memory:", "kg_path": kg_path})
    r = out["kg_result"]
    assert r["version"] == 1 and r["status"] == "draft" and not r["reused"]
    assert r["rules"] == 2 and r["nodes"] > 60 and r["edges"] > 80

    store = KGStore(kg_path)
    # fingerprint covers every input file
    inputs = {i["name"] for i in store.export_json(1)["inputs"]}
    assert {"EFAS0042.csv", "BONCALC.cbl", "EFASPOL.cpy",
            "POLMAINT.cbl", "policy_view.php"} <= inputs
    # reified rule: resolved text, COBOL evidence, structured decision tables
    rules = {x["field"]: x for x in store.rules(1)}
    lb = rules["PR-LOYALTY-BONUS"]
    assert "Exit Date (EXITDT)" in lb["decision_tables"]["resolved_calc"]
    assert lb["decision_tables"]["lineage"]["dataset"] == "EFAS0042"
    assert lb["target_column"] == "LOYBONUS" and lb["program"] == "BONCALC"
    assert "Exit Date (EXITDT)" in lb["resolved"] and "COMPUTE" in lb["cobol"]
    assert lb["inputs"]["PR-SUM-ASSURED"] == ["operand"]
    assert "condition" in lb["inputs"]["PR-STATUS"]
    bands = next(d for d in lb["decision_tables"]["dependencies"]
                 if d["variable"] == "WS-RATE-PCT")
    assert len(bands["rows"]) == 5                      # the five tenure bands
    # provenance on every node and edge
    doc = store.export_json(1)
    assert all(n["provenance"] in ("parser", "llm", "human") for n in doc["nodes"])
    # certification lifecycle
    store.certify(1, "Debdas", "unit test")
    assert store.meta(1)["status"] == "certified"
    assert store.latest(certified_only=True) == 1
    assert all(x["status"] == "certified" for x in store.rules(1))
    # friendly graph queries: humans type terms, not node ids
    assert store.resolve(1, "EXITDT") == "col:EXITDT"
    assert store.resolve(1, "PR-LOYALTY-BONUS") == "fld:PR-LOYALTY-BONUS"
    assert store.resolve(1, "BONCALC") == "pgm:BONCALC"
    assert store.find(1, "exit date")[0]["id"] == "col:EXITDT"
    assert store.resolve(1, "no-such-thing") is None
    # the dictionary round-trips intact
    d = store.load_dictionary(1)
    assert {c.name: c for c in d.columns}["LOYBONUS"].derived_in_program == "BONCALC"
    store.close()


def test_fingerprint_reuse_and_staleness(tmp_path):
    kg_path = str(tmp_path / "knowledge.duckdb")
    inputs = collect_inputs("data/EFAS0042.csv", "data/legacy")
    fp = fingerprint_inputs(inputs)
    assert fp == fingerprint_inputs(list(reversed(inputs)))   # order-independent
    flow_a.invoke({"source_csv": "data/EFAS0042.csv", "code_dir": "data/legacy",
                   "warehouse_path": ":memory:", "kg_path": kg_path})
    # identical inputs -> reuse, no second version
    out2 = flow_a.invoke({"source_csv": "data/EFAS0042.csv",
                          "code_dir": "data/legacy",
                          "warehouse_path": ":memory:", "kg_path": kg_path})
    assert out2["kg_result"]["reused"] and out2["kg_result"]["version"] == 1
    store = KGStore(kg_path)
    assert len(store.versions()) == 1
    assert not store.is_stale(1, inputs)
    changed = [dict(i) for i in inputs]
    changed[0]["sha256"] = "0" * 64
    assert store.is_stale(1, changed)
    store.close()


def test_flow_b_runs_from_persisted_knowledge(tmp_path):
    kg_path = str(tmp_path / "knowledge.duckdb")
    wh_path = str(tmp_path / "warehouse.duckdb")
    flow_a.invoke({"source_csv": "data/EFAS0042.csv", "code_dir": "data/legacy",
                   "warehouse_path": wh_path, "kg_path": kg_path})
    KGStore(kg_path).certify(1, "Debdas")
    # Flow B: no code_dir, no enrichment — knowledge comes from the store;
    # source_csv provided only so the table can be restaged if needed
    out = flow_b.invoke({"kg_path": kg_path, "warehouse_path": wh_path,
                         "source_csv": "data/EFAS0042.csv",
                         "target_dict_path": TARGET_DICT})
    spec = out["spec"]
    assert spec.kg_version == 1 and spec.kg_status == "certified"
    assert spec.kg_fingerprint == fingerprint_inputs(
        collect_inputs("data/EFAS0042.csv", "data/legacy"))
    by = {m.target_attribute: m for m in spec.mappings}
    assert by["loyalty_bonus_amount"].gate == "review"
    assert "BONCALC" in by["loyalty_bonus_amount"].rationale
    assert out["report"].verdict in ("pass", "needs_review")
    assert out["review_queue"].stats["auto_accepted"] >= 6


def test_certify_applies_decisions_without_remapping(tmp_path):
    """The certify path adopts the spec the reviewer saw (spec_in) and applies
    decisions to it — it does NOT re-run the mapping agent. A supplied default
    for an unmapped boolean is promoted into the final spec, and the verdict
    reflects the decided spec."""
    from engine.orchestration.graph import flow_b_certify

    kg_path = str(tmp_path / "knowledge.duckdb")
    wh_path = str(tmp_path / "warehouse.duckdb")
    flow_a.invoke({"source_csv": "data/EFAS0042.csv", "code_dir": "data/legacy",
                   "warehouse_path": wh_path, "kg_path": kg_path})
    KGStore(kg_path).certify(1, "Debdas")
    base = flow_b.invoke({"kg_path": kg_path, "warehouse_path": wh_path,
                          "source_csv": "data/EFAS0042.csv",
                          "target_dict_path": TARGET_DICT})
    spec = base["spec"]
    # vulnerable_customer_flag has no source -> it is unmapped
    assert "vulnerable_customer_flag" in {u["attribute"] for u in spec.unmapped_target}

    # tamper the reviewed spec so we can PROVE mapping wasn't re-derived: if the
    # certify path re-ran map_to_target this marker would be overwritten.
    reviewed = spec.model_dump()
    reviewed["generated_by"] = "REVIEWED_BY_HUMAN_MARKER"

    out = flow_b_certify.invoke({
        "kg_path": kg_path, "warehouse_path": wh_path,
        "source_csv": "data/EFAS0042.csv",
        "target_dict_path": TARGET_DICT,
        "spec_in": reviewed,
        "decisions": {"vulnerable_customer_flag":
                      {"action": "edit", "transformation_sql": "false"}},
    })
    final = out["spec"]
    # the reviewed spec was adopted verbatim (mapping agent never ran again)
    assert final.generated_by.startswith("REVIEWED_BY_HUMAN_MARKER")
    # the default was applied: promoted into a concrete mapping
    by = {m.target_attribute: m for m in final.mappings}
    assert by["vulnerable_customer_flag"].transformation_sql == "false"
    assert by["vulnerable_customer_flag"].gate == "auto_accept"
    assert "vulnerable_customer_flag" not in {u["attribute"] for u in final.unmapped_target}
    # re-validated + re-reviewed so a verdict is produced
    assert out["report"].verdict in ("certified", "pass", "needs_review")

    # certifying with NO decisions (everything already auto-accepted) must not
    # crash apply_decisions — it just re-validates the reviewed spec unchanged.
    out2 = flow_b_certify.invoke({
        "kg_path": kg_path, "warehouse_path": wh_path,
        "source_csv": "data/EFAS0042.csv",
        "target_dict_path": TARGET_DICT,
        "spec_in": reviewed,
        # no "decisions" key at all
    })
    assert out2["spec"].generated_by.startswith("REVIEWED_BY_HUMAN_MARKER")
    assert out2["report"].verdict in ("certified", "pass", "needs_review")


# ---- (3) analyst DQ rule library + (4) parser hardening / slicing ----
from engine.agents.analyst import sql_library                            # noqa: E402
from engine.agents.legacy_expert import (extract_condition_names,        # noqa: E402
                                         extract_derivations)
from engine.staging import Warehouse                                     # noqa: E402


def test_analyst_dq_rule_library():
    t = analyze("data/EFAS0042.csv", "EFAS0042")
    rules = {r.id: r for r in t.dq_rules}
    # the planted defects are caught, with sample evidence
    ni = rules["ni_format:NINO"]
    assert ni.failed == 1 and "QQ123456C" in ni.samples and ni.severity == "major"
    pc = rules["uk_postcode:PCODE"]
    assert pc.failed == 1 and "ZZ999" in pc.samples
    # date validity + discovered cross-date invariants pass
    assert rules["date_valid:COMMDT"].failed == 0
    assert rules["date_order:BIRTHDT<=COMMDT"].failed == 0        # birth <= commencement
    assert rules["key_unique:POLNO"].failed == 0
    assert any(r.category == "completeness" for r in t.dq_rules)   # conditional rules
    assert t.dq_summary["rules_failing"] == 2
    # every rule is EXECUTABLE: rerun each SQL standalone and match the finding
    wh = Warehouse(":memory:")
    wh.stage_csv("data/EFAS0042.csv", "EFAS0042", all_varchar=True)
    for r in t.dq_rules:
        stmt = r.sql.split("\n", 1)[1]                       # drop the comment line
        assert wh.con.execute(stmt).fetchone()[0] == r.failed, r.id
    wh.close()
    lib = sql_library(t)
    assert "ni_format:NINO" in lib and "rerun at full volume" in lib


def test_backward_slicing_multi_hop_and_coverage():
    """The slice must follow WS chains of ANY depth (the old one-hop closure
    missed WS-C here), and the coverage metric must flag unknown statements."""
    cobol = """
       IDENTIFICATION DIVISION.
       PROGRAM-ID. SLICETST.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  REC.
           05  PR-IN            PIC 9(04).
           05  PR-OUT           PIC 9(04).
       PROCEDURE DIVISION.
       0100-STEP-C.
           MULTIPLY PR-IN BY 2 GIVING WS-C.
       0200-STEP-B.
           ADD WS-C TO WS-B.
       0300-STEP-A.
           COMPUTE WS-A = WS-B + 1
           MOVE WS-A TO PR-OUT.
       0400-UNRELATED.
           MOVE ZEROS TO WS-Z.
"""
    d = extract_derivations(cobol, [("PR-IN", "9(04)"), ("PR-OUT", "9(04)")])
    der = d["PR-OUT"]
    assert der["paragraphs"] == ["0100-STEP-C", "0200-STEP-B", "0300-STEP-A"]
    assert "0400-UNRELATED" not in der["paragraphs"]
    assert der["inputs"] == ["PR-IN"] and der["coverage"] == 1.0
    assert set(der["slice_vars"]) == {"WS-A", "WS-B", "WS-C"}
    # an exotic statement in the slice lowers coverage — the honesty metric
    exotic = cobol.replace("           COMPUTE WS-A = WS-B + 1",
                           "           COMPUTE WS-A = WS-B + 1\n"
                           "           INSPECT WS-A REPLACING ALL '0' BY '9'")
    d2 = extract_derivations(exotic, [("PR-IN", "9(04)"), ("PR-OUT", "9(04)")])
    assert d2["PR-OUT"]["coverage"] < 1.0


def test_88_condition_names_flow_through():
    t = analyze("data/EFAS0042.csv", "EFAS0042")
    cobol, php = _code("cbl"), _code("php")
    cn = extract_condition_names(cobol)
    assert cn["PR-CLOSED"] == {"field": "PR-STATUS", "values": ["CL"]}
    e = enrich(t, cobol, php)
    lb = {c.name: c for c in e.columns}["LOYBONUS"]
    # the copybook still aligns despite 88 lines inside the record
    assert lb.cobol_name == "PR-LOYALTY-BONUS"
    # the 88-based guard in 1000-DERIVE-TENURE resolves to the parent field
    assert "Policy Status (STATCD) = 'CL' [Closed]" in lb.derivation
    assert lb.derivation_coverage == 1.0
    # 88s decode codes the screen doesn't cover
    assert {c.name: c for c in e.columns}["STATCD"].value_decode.get("LA")


# ---- hostile enterprise COBOL: the system must DEGRADE GRACEFULLY ----
# NBCOMM74.cbl is deliberately realistic-ugly: SECTIONs, GO TO flow with
# PERFORM ... THRU and EXIT paragraphs, period-terminated IFs (no END-IF),
# a multi-record REDEFINES file, OCCURS/INDEXED BY with SEARCH/SET,
# EXEC SQL host variables, EXEC CICS, STRING/INSPECT, COPY REPLACING.
# The contract: never crash, never be confidently wrong — coverage and
# confidence drop, and the rule is flagged for LLM assist + SME review.
from pathlib import Path as _P                                           # noqa: E402
import pytest                                                            # noqa: E402
from engine.agents.legacy_expert import extract_cobol                    # noqa: E402

# The hostile estate is a large binary-ish fixture that is not always present
# in a trimmed/shared checkout. Reading it at MODULE level meant a missing file
# raised during COLLECTION and took the ENTIRE suite down with it — 39 healthy
# tests reported as errors because of 2 absent fixtures. Guard the read and
# skip only the tests that actually need it.
_HOSTILE_CBL = _P("data/hostile/NBCOMM74.cbl")
_HOSTILE_CSV = _P("data/hostile/NBEXTRACT.csv")
HOSTILE = _HOSTILE_CBL.read_text() if _HOSTILE_CBL.exists() else ""

needs_hostile = pytest.mark.skipif(
    not (_HOSTILE_CBL.exists() and _HOSTILE_CSV.exists()),
    reason="hostile COBOL fixtures (data/hostile/) not present in this checkout")

_NB_FIELDS = [("NB-REC-TYPE", "X(01)"), ("NB-POLICY-NO", "X(10)"),
              ("NB-PROD-CODE", "X(04)"), ("NB-AGENT-NO", "X(06)"),
              ("NB-APE-AMT", "9(07)V99"), ("NB-COMM-AMT", "9(07)V99")]


@needs_hostile
def test_hostile_cobol_never_crashes_and_reports_low_coverage():
    cob = extract_cobol(HOSTILE)                 # must not raise
    assert cob["record_name"]                    # picked SOME layout
    d = extract_derivations(HOSTILE, _NB_FIELDS)
    der = d["NB-COMM-AMT"]
    # the slice is FOUND (both paragraphs, through the PERFORM THRU flow) ...
    assert set(der["paragraphs"]) == {"B100-GET-RATE", "C100-CALC-COMM"}
    assert "NB-APE-AMT" in der["inputs"] and "NB-REC-TYPE" in der["inputs"]
    # ... but the parser is HONEST about what it did not understand
    assert der["coverage"] < 0.8, "EXEC SQL/SEARCH/STRING must count against coverage"
    assert der["coverage"] > 0.3, "the arithmetic core WAS understood"


@needs_hostile
def test_hostile_code_in_estate_does_not_break_good_extraction():
    """Dumping an ugly program into the code folder must not disturb the
    clean source's alignment or its fully-resolved rules."""
    t = analyze("data/EFAS0042.csv", "EFAS0042")
    e = enrich(t, _code("cbl") + "\n" + HOSTILE, _code("php"))
    lb = {c.name: c for c in e.columns}["LOYBONUS"]
    assert lb.cobol_name == "PR-LOYALTY-BONUS"        # 25-field alignment intact
    assert lb.derivation_coverage == 1.0              # clean slice stays clean
    assert "Exit Date (EXITDT)" in lb.derivation


@needs_hostile
def test_hostile_end_to_end_flags_instead_of_guessing(tmp_path):
    """Full path on a hostile-only estate: the calculated column is still
    identified and sliced, but ships flagged with reduced confidence."""
    import csv as _csv
    src = tmp_path / "NBEXTRACT.csv"
    with open(src, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["C1", "C2", "C3", "C4", "C5"])
        for i in range(12):
            w.writerow([f"PRD{i % 3}", f"A{i:05d}", f"{1000 + i * 7}.00",
                        f"{25 + i}.00", f"FILL{i}"])
    t = analyze(str(src), "NBEXTRACT")
    e = enrich(t, HOSTILE, "")                        # no screen evidence at all
    by = {c.name: c for c in e.columns}
    comm = by["C4"]                                    # aligns to NB-COMM-AMT
    assert comm.cobol_name == "NB-COMM-AMT"
    assert comm.derived_in_program == "NBCOMM74"
    assert comm.derivation is not None                 # partial story, not silence
    assert comm.derivation_coverage < 0.8
    assert comm.confidence <= 0.6                      # trust reduced, not asserted
    ev = " ".join(comm.evidence)
    assert "LOW PARSE COVERAGE" in ev and "SME review" in ev
    # lineage is still recovered — and does not mis-attribute the copybook
    # (NBRATES is a rate table the program COPYs, not the record layout)
    lin = comm.derivation_lineage
    assert lin["dataset"] == "NBMAST01" and lin["file"] == "NB-FILE"
    assert lin["copybook"] is None and lin["program"] == "NBCOMM74"


@needs_hostile
def test_mixed_estate_workspace_aligns_per_active_source():
    """A workspace holding SEVERAL estates' code (the restart-rebuild makes
    this common) must align each source against ITS OWN record layout — the
    extract's column count picks the copybook, not global 'longest run'."""
    mixed = _code("cbl") + "\n" + HOSTILE
    # demo source -> 25-field EFAS record wins, both rules present
    t = analyze("data/EFAS0042.csv", "EFAS0042")
    e = enrich(t, mixed, _code("php"))
    calc = {c.name: c for c in e.columns if c.derivation}
    assert set(calc) == {"LOYBONUS", "EXTPNPCT"} and calc["LOYBONUS"].derivation_coverage == 1.0
    # hostile source -> 5-field NB layout wins despite the longer EFAS run
    import csv as _csv, io
    t2 = analyze("data/hostile/NBEXTRACT.csv", "NBEXTRACT")
    e2 = enrich(t2, mixed, "")
    calc2 = {c.name: c for c in e2.columns if c.derivation}
    assert set(calc2) == {"NB04"}
    assert calc2["NB04"].derived_in_program == "NBCOMM74"
    assert calc2["NB04"].derivation_coverage < 0.8


def test_flow_a_survives_source_only_estate(tmp_path):
    """Incremental workflows run Flow A before any code exists. A code-less
    knowledge version (no edges, no rules) must persist cleanly — the
    empty-edge-set crash here used to kill the stream mid-flight."""
    empty_code = tmp_path / "code"; empty_code.mkdir()
    out = flow_a.invoke({"source_csv": "data/EFAS0042.csv",
                         "code_dir": str(empty_code),
                         "warehouse_path": ":memory:",
                         "kg_path": str(tmp_path / "kg.duckdb")})
    r = out["kg_result"]
    assert r["version"] == 1 and r["rules"] == 0 and r["edges"] == 0
    store = KGStore(str(tmp_path / "kg.duckdb"))
    assert store.load_dictionary(1).columns          # dictionary still usable
    store.close()


def test_engine_version_salts_the_fingerprint(monkeypatch):
    """A knowledge version is a function of inputs x ENGINE: upgrading the
    extraction logic must invalidate fingerprint matches so stale artifacts
    are never replayed after a package update."""
    import engine.kgstore as ks
    inputs = collect_inputs("data/EFAS0042.csv", "data/legacy")
    fp_now = fingerprint_inputs(inputs)
    orig_engine = ks.ENGINE_VERSION
    monkeypatch.setattr(ks, "ENGINE_VERSION", "test-next")
    assert ks.fingerprint_inputs(inputs) != fp_now
    # ... and so does the enrichment mode, in ISOLATION: offline-built
    # knowledge must not replay once an LLM is configured
    monkeypatch.setattr(ks, "ENGINE_VERSION", orig_engine)
    monkeypatch.setattr(ks, "llm_ready", lambda: True)
    monkeypatch.setattr(ks, "llm_label", lambda: "Azure · gpt-4o")
    assert ks.fingerprint_inputs(inputs) != fp_now


def test_llm_diagnose_offline_is_safe():
    """Unconfigured environments must diagnose instantly with guidance, and
    a bad endpoint must fail at the right step — never with a bare
    'Connection error.'"""
    from engine.llm_check import diagnose, summary
    steps = diagnose()
    assert steps[0]["step"] == "config" and not steps[0]["ok"]
    assert ".env" in steps[0]["hint"] and "config" in summary(steps)


def test_json_escape_repair():
    """A malformed LLM escape (the exact 'Invalid \\uXXXX escape' failure)
    must repair and parse, while VALID escapes survive untouched."""
    import json as _j
    from engine.agents.legacy_expert import _repair_json
    BS = chr(92)
    raw = ('{"ok": "caf' + BS + 'u00e9", "bad": "x' + BS + 'uZZ12y", '
           '"stray": "a' + BS + 'q b"}')
    parsed = _j.loads(_repair_json(raw))
    assert parsed["ok"] == "café"                 # valid escape preserved
    assert "uZZ12" in parsed["bad"] and "q" in parsed["stray"]
    fenced = "```json" + chr(10) + '{"a": 1}' + chr(10) + "```"
    assert _j.loads(_repair_json(fenced)) == {"a": 1}


def test_degraded_runs_do_not_replay(tmp_path, monkeypatch):
    """LLM configured but enrichment failed -> the version persists for
    history, marked degraded, and the SAME inputs run fresh next time
    instead of replaying the failure."""
    import engine.orchestration.nodes as nodes
    monkeypatch.setattr(nodes, "llm_ready", lambda: True)   # configured...
    kg_path = str(tmp_path / "kg.duckdb")                    # ...but offline
    init = {"source_csv": "data/EFAS0042.csv", "code_dir": "data/legacy",
            "warehouse_path": ":memory:", "kg_path": kg_path}
    r1 = flow_a.invoke(init)["kg_result"]
    assert r1["degraded"] and r1["version"] == 1
    r2 = flow_a.invoke(init)["kg_result"]
    assert not r2.get("reused") and r2["version"] == 2       # retried, not frozen
    store = KGStore(kg_path)
    assert store.meta(1)["notes"].startswith("degraded")
    assert store.find_by_fingerprint(r1["fingerprint"]) is None
    store.close()


def test_llm_failure_prevention_layers():
    """The anti-failure set: a small meanings payload, per-entry apply
    isolation, and a degraded predicate that also catches PARTIAL failure."""
    import json as _j
    from engine.agents.legacy_expert import (_apply_meanings, _compact_bundle,
                                             build_evidence, extract_php)
    import engine.orchestration.nodes as nodes
    t = analyze("data/EFAS0042.csv", "EFAS0042")
    cobol, php_t = _code("cbl"), _code("php")
    e = enrich(t, cobol, php_t)
    ev = build_evidence(t, cobol, php_t)
    # payload diet: the meanings bundle must stay a fraction of the old dump
    n = len(_j.dumps(_compact_bundle(e, t, ev["cob"], extract_php(php_t)), default=str))
    assert n < 20000, f"meanings payload grew to {n} chars"
    # per-entry isolation: one malformed column entry must not void the rest
    _apply_meanings(e, {"columns": [
        {"name": "STATCD", "confidence": "very high"},          # malformed
        {"name": "POLNO", "business_name": "Policy Number OK", "confidence": 0.99},
    ]})
    assert {c.name: c for c in e.columns}["POLNO"].business_name == "Policy Number OK"
    # degraded predicate catches PARTIAL failure (one narrative missing)
    class _Ready:
        def __call__(self): return True
    orig = nodes.llm_ready
    nodes.llm_ready = _Ready()
    try:
        e.generated_by = "deterministic+llm"
        assert nodes._is_degraded(e)                # narratives absent offline
        for c in e.columns:
            if c.derivation_cobol:
                c.derivation_narrative = "narrated"
        assert not nodes._is_degraded(e)            # complete -> healthy
    finally:
        nodes.llm_ready = orig


def test_sme_curation_edits(tmp_path):
    """Curation before certification: SME corrections persist INTO the stored
    artifacts (so Flow B consumes them), carry human provenance, are logged,
    and certification makes the version immutable."""
    kg_path = str(tmp_path / "kg.duckdb")
    flow_a.invoke({"source_csv": "data/EFAS0042.csv", "code_dir": "data/legacy",
                   "warehouse_path": ":memory:", "kg_path": kg_path})
    store = KGStore(kg_path)

    # the user's exact scenario: BIRTHDT flagged as Date of Birth -> it's not PII
    store.apply_edit(1, "pii", "BIRTHDT", "is_pii", False, "Debdas",
                     "entry date, not date of birth")
    ins = store.load_insight(1)
    e = next(x for x in ins.pii if x.column == "BIRTHDT")
    assert not e.is_pii and e.method == "human" and "entry date" in e.rationale

    # dictionary correction flows into the artifact AND the graph node label
    store.apply_edit(1, "column", "BIRTHDT", "business_name",
                     "Policy Entry Date (legacy)", "Debdas", "")
    d = store.load_dictionary(1)
    col = next(c for c in d.columns if c.name == "BIRTHDT")
    assert col.business_name == "Policy Entry Date (legacy)"
    assert any("SME correction by Debdas" in ev for ev in col.evidence)
    node = store.nodes_info(1, ["col:BIRTHDT"])["col:BIRTHDT"]
    assert node["label"] == "Policy Entry Date (legacy)"
    assert node["provenance"] == "human"

    # DQ suppression drops the rule from the cleansing library (annotated)
    store.apply_edit(1, "dq_rule", "uk_postcode:PCODE", "suppressed", True,
                     "Debdas", "offshore book, non-UK addresses expected")
    ins2 = store.load_insight(1)
    rule = next(r for r in ins2.dq_rules if r.id == "uk_postcode:PCODE")
    assert rule.suppressed and "offshore" in rule.suppress_note
    lib = sql_library(ins2)
    assert "SUPPRESSED" in lib and "offshore" in lib

    # rule wording edit: narrative updated, resolution untouched, provenance human
    store.apply_edit(1, "rule", "PR-LOYALTY-BONUS", "narrative",
                     "Loyalty bonus rewards long tenure (SME wording).",
                     "Debdas", "")
    rr = {x["field"]: x for x in store.rules(1)}["PR-LOYALTY-BONUS"]
    assert rr["decision_tables"]["narrative"].startswith("Loyalty bonus rewards")
    assert rr["decision_tables"]["narrative_source"] == "SME (Debdas)"
    assert "Exit Date (EXITDT)" in rr["decision_tables"]["resolved_calc"]

    # the log is complete, and certification locks the version
    assert len(store.edits(1)) == 4
    store.certify(1, "Debdas", "curated then certified")
    try:
        store.apply_edit(1, "column", "BIRTHDT", "description", "x", "Debdas")
        raise AssertionError("edit on certified version must fail")
    except ValueError as ex:
        assert "immutable" in str(ex)
    store.close()


# ---- multi-source: relationships, composite workset, cross-file mapping ----
from engine.agents.relationship import discover_relationships, joinable_edges  # noqa: E402
from engine.composite import build_workset, plan_joins                         # noqa: E402
from engine.staging import Warehouse as _WH                                    # noqa: E402


def _both_flow_a(tmp_path):
    kg, wh = str(tmp_path / "kg.duckdb"), str(tmp_path / "wh.duckdb")
    for csv, t in (("data/EFAS0042.csv", "EFAS0042"),
                   ("data/ESCH0009.csv", "ESCH0009")):
        flow_a.invoke({"source_csv": csv, "table": t, "code_dir": "data/legacy",
                       "warehouse_path": wh, "kg_path": kg})
    return kg, wh


def test_relationship_discovery():
    """The scheme master is found via value evidence: SCHNO -> SCHREF, N:1."""
    wh = _WH(":memory:")
    wh.stage_csv("data/EFAS0042.csv", "EFAS0042")
    wh.stage_csv("data/ESCH0009.csv", "ESCH0009")
    rels = discover_relationships(wh, ["EFAS0042", "ESCH0009"])
    edges = joinable_edges(rels)
    e = next(r for r in edges if r.left_column == "SCHNO")
    assert e.right_column == "SCHREF" and e.cardinality == "N:1" \
        and e.kind == "join_key"
    # the messy in-file scheme name must NOT be linked to the clean master name
    assert not any(r.left_column == "SCHNM" for r in rels.relationships)
    wh.close()


def test_composite_grain_protection():
    """With the SMALL file as primary, joining the policy file would fan its
    rows out — the planner must refuse and say why."""
    wh = _WH(":memory:")
    wh.stage_csv("data/EFAS0042.csv", "EFAS0042")
    wh.stage_csv("data/ESCH0009.csv", "ESCH0009")
    rels = discover_relationships(wh, ["EFAS0042", "ESCH0009"])
    plan, excluded = plan_joins("ESCH0009", ["EFAS0042", "ESCH0009"], rels)
    assert plan == []
    assert excluded and excluded[0]["table"] == "EFAS0042" \
        and "aggregation" in excluded[0]["reason"]
    wh.close()


def test_multi_source_flow_b(tmp_path):
    """Two files -> per-file knowledge -> composite workset -> a target
    attribute mapped cross-file, with provenance, and no degradation of the
    single-source mappings."""
    kg, wh = _both_flow_a(tmp_path)
    srcs = [{"table": "EFAS0042", "path": "data/EFAS0042.csv"},
            {"table": "ESCH0009", "path": "data/ESCH0009.csv"}]
    out = flow_b.invoke({"kg_path": kg, "warehouse_path": wh,
                         "target_dict_path": TARGET_DICT,
                         "sources": srcs, "code_dir": "data/legacy"})
    spec = out["spec"]
    assert spec.source_tables == ["EFAS0042", "ESCH0009"]
    assert spec.join_plan and spec.join_plan[0]["table"] == "ESCH0009" \
        and spec.join_plan[0]["on"] == "SCHREF"
    # the workset preserves the primary's grain
    assert out["report"].verdict in ("needs_review", "certified")
    emp = next(m for m in spec.mappings if m.target_attribute == "employer_name")
    assert emp.source_attributes == ["EMPNM"] and emp.source_files == ["ESCH0009"]
    assert emp.gate == "auto_accept" and "ESCH0009" in emp.transformation_note
    assert spec.stats["cross_file_mappings"] >= 1
    # scheme_reference: EFAS0042.SCHNO and ESCH0009.SCHREF are the two sides
    # of the very join that combined these files, so once both humanize to
    # the same clean "Scheme Number" label they correctly tie and the
    # mapping is held for human confirmation -- same treatment as
    # scheme_name's messy-copy-vs-clean-master tie, and consistent with it.
    sr = next(m for m in spec.mappings if m.target_attribute == "scheme_reference")
    assert sr.ambiguous and sr.source_attributes == ["SCHNO"]
    assert {a["source"] for a in sr.alternatives} == {"SCHREF"}
    # single-source parity: shared targets keep their source and gate
    out1 = flow_b.invoke({"kg_path": kg, "warehouse_path": wh,
                          "target_dict_path": TARGET_DICT,
                          "sources": [srcs[0]],
                          "kg_version": 1, "source_csv": "data/EFAS0042.csv",
                          "code_dir": "data/legacy"})
    single = {m.target_attribute: (tuple(m.source_attributes), m.gate)
              for m in out1["spec"].mappings}
    multi = {m.target_attribute: (tuple(m.source_attributes), m.gate)
             for m in spec.mappings}
    for t, sg in single.items():
        if t in ("employer_name",           # improved by the second file, by design
                 "scheme_reference",        # now correctly ties with its join partner
                 "scheme_name"):            # now correctly ties messy copy vs clean master
            continue
        assert multi.get(t) == sg, f"{t} degraded in composite mode: {sg} -> {multi.get(t)}"


def test_relset_persist_and_replay(tmp_path):
    """Relationships persist keyed by the source-set fingerprint."""
    from engine.kgstore import KGStore
    from engine.orchestration.nodes import relset_fingerprint
    kg, wh_path = _both_flow_a(tmp_path)
    srcs = [{"table": "EFAS0042", "path": "data/EFAS0042.csv"},
            {"table": "ESCH0009", "path": "data/ESCH0009.csv"}]
    fp = relset_fingerprint(srcs, "data/legacy")
    store = KGStore(kg)
    assert store.load_relationships(fp) is None
    flow_b.invoke({"kg_path": kg, "warehouse_path": wh_path,
                   "target_dict_path": TARGET_DICT,
                   "sources": srcs, "code_dir": "data/legacy"})
    doc = store.load_relationships(fp)
    assert doc and any(r["left_column"] == "SCHNO" for r in doc["relationships"])
    assert store.latest_for_table("ESCH0009") == 2
    store.close()


def test_primary_chosen_by_grain_not_order(tmp_path):
    """The driving file is the finest-grain one (policies), chosen from the
    discovered join — regardless of upload/list order or file size. This is
    the invariant behind removing the user-facing 'primary' control."""
    from engine.composite import choose_primary
    from engine.agents.relationship import discover_relationships
    from engine.staging import Warehouse as _WH2
    wh = _WH2(":memory:")
    wh.stage_csv("data/EFAS0042.csv", "EFAS0042")
    wh.stage_csv("data/ESCH0009.csv", "ESCH0009")
    rels = discover_relationships(wh, ["EFAS0042", "ESCH0009"])
    rc = {"EFAS0042": wh.row_count("EFAS0042"),
          "ESCH0009": wh.row_count("ESCH0009")}
    for order in (["EFAS0042", "ESCH0009"], ["ESCH0009", "EFAS0042"]):
        primary, comps = choose_primary(order, rels, rc)
        assert primary == "EFAS0042", f"order {order} picked {primary}"
        assert comps == [["EFAS0042", "ESCH0009"]]      # one shared grain
    wh.close()


def test_flow_b_auto_primary_ignores_list_order(tmp_path):
    """Full Flow B: passing the scheme file first must still map policy-grained,
    joining the scheme master in — the loader ignores order and any hint."""
    kg, wh = _both_flow_a(tmp_path)
    srcs = [{"table": "ESCH0009", "path": "data/ESCH0009.csv"},   # small file first
            {"table": "EFAS0042", "path": "data/EFAS0042.csv"}]
    out = flow_b.invoke({"kg_path": kg, "warehouse_path": wh,
                         "target_dict_path": TARGET_DICT,
                         "sources": srcs, "code_dir": "data/legacy"})
    assert out["kg_result"]["primary"] == "EFAS0042"
    emp = next(m for m in out["spec"].mappings
               if m.target_attribute == "employer_name")
    assert emp.source_files == ["ESCH0009"] and emp.gate == "auto_accept"


# ---- LLM recovery tier must never downgrade an already-solved multi-column
# derivation (annualised premium, group-policy flag) into a naive single-
# column guess, even when the LLM's proposal validates with higher coverage
# than the correct answer. A plain cast of the raw premium AMOUNT is always
# well-populated data, so it can out-score the true amount*frequency formula
# on coverage alone while being financially wrong for every non-annual payer.
def test_llm_recovery_never_overrides_derived_multi_column_mapping(tmp_path, monkeypatch):
    import engine.agents.mapping_agent as MA
    from engine import config

    monkeypatch.setattr(config, "llm_client", lambda: (object(), "fake-model"))

    def fake_llm_json(client, model, messages, max_tokens=300):
        payload = messages[-1]["content"]
        if '"annual_premium"' in payload:
            return {"source": "PREMAMT", "reason": "closest numeric premium field"}
        if '"is_group_policy"' in payload:
            return {"source": "PRODCD", "reason": "product code correlates with group"}
        return {"source": None}
    monkeypatch.setattr(MA, "_llm_json", fake_llm_json)

    kg_path, wh_path = str(tmp_path / "kg.duckdb"), str(tmp_path / "wh.duckdb")
    flow_a.invoke({"source_csv": "data/EFAS0042.csv", "table": "EFAS0042",
                   "code_dir": "data/legacy", "warehouse_path": wh_path, "kg_path": kg_path})
    out = flow_b.invoke({"kg_path": kg_path, "warehouse_path": wh_path,
                         "target_dict_path": TARGET_DICT,
                         "sources": [{"table": "EFAS0042", "path": "data/EFAS0042.csv"}],
                         "code_dir": "data/legacy"})
    by = {m.target_attribute: m for m in out["spec"].mappings}

    ap = by["annual_premium"]
    assert ap.cardinality == "many:1"
    assert ap.source_attributes == ["PREMAMT", "PREMFRQ"], \
        "LLM recovery dropped the frequency column from an already-solved derivation"
    assert "CASE" in ap.transformation_sql and "PREMFRQ" in ap.transformation_sql, \
        "annualisation multiplier was silently lost -- would misreport non-annual premiums"
    assert not ap.llm_recovered

    gp = by["is_group_policy"]
    assert gp.cardinality == "derived"
    assert gp.source_attributes == ["PRODCD"] and not gp.llm_recovered


# ---- humanize() must strip ANY estate's record-prefix convention, not just
# the ones we happened to hardcode. A copybook's leading token (PR-, SR-,
# CL-, ...) disambiguates fields across DIFFERENT records and carries no
# business meaning -- a hardcoded allow-list silently mislabels every other
# record's fields, which is exactly what let a messy denormalised copy beat
# the clean master record in a mapping contest it should have tied.
def test_humanize_strips_any_record_prefix_not_just_pr_ws():
    from engine.agents.legacy_expert import _humanize
    assert _humanize("PR-SCHEME-NM") == "Scheme Name"
    assert _humanize("SR-SCHEME-NAME") == "Scheme Name"          # was "Sr Scheme Name"
    assert _humanize("SR-EMPLOYER-NAME") == "Employer Name"
    assert _humanize("WS-TENURE-YRS") == "Tenure Years"
    assert _humanize("PR-COMMENCE-DT") == "Commencement Date"


def test_scheme_name_ties_messy_policy_copy_against_clean_master(tmp_path):
    """EFAS0042.SCHNM is a messy denormalised in-record copy of the scheme
    name; ESCH0009.SCHNAME (via SR-SCHEME-NAME) is the clean master record.
    Once both humanize to the same clean label they must tie and go to
    human review -- never silently prefer the messy copy because of a
    labelling artifact."""
    kg, wh = _both_flow_a(tmp_path)
    srcs = [{"table": "EFAS0042", "path": "data/EFAS0042.csv"},
            {"table": "ESCH0009", "path": "data/ESCH0009.csv"}]
    out = flow_b.invoke({"kg_path": kg, "warehouse_path": wh,
                         "target_dict_path": TARGET_DICT,
                         "sources": srcs, "code_dir": "data/legacy"})
    sn = next(m for m in out["spec"].mappings if m.target_attribute == "scheme_name")
    assert sn.ambiguous and sn.gate == "review"
    assert {a["source"] for a in sn.alternatives} == {"SCHNAME"}


# ---- certify convergence: the review loop must terminate ------------------
# Regression for the reported defect: resolving every queue item and pressing
# Certify returned the SAME queue again (an auto-accepted mapping carrying a
# soft warn, plus the three unmapped targets), so the verdict never reached
# 'certified' and the button never settled.
def _flow_b_fixture():
    insight = analyze("data/EFAS0042.csv", "EFAS0042")
    e = _enrich(insight, _code("cbl"), _code("php"))
    td = _json.loads(open(TARGET_DICT).read())
    wh = Warehouse(":memory:")
    wh.stage_csv("data/EFAS0042.csv", "EFAS0042", all_varchar=True)
    spec = map_to_target(e, td, wh, "EFAS0042")
    report = _validate(spec, td, insight, wh, "EFAS0042")
    q = build_review_queue(spec, report, e, insight, td)
    return insight, e, td, wh, spec, report, q


def test_certify_converges_and_is_idempotent():
    import copy
    insight, e, td, wh, spec, report, q = _flow_b_fixture()
    decisions = {i.target_attribute: {"action": "accept"}
                 for i in q.items if i.kind == "mapping_review"}

    def certify(base):
        s = apply_decisions(copy.deepcopy(base), decisions, td)
        r = _validate(s, td, insight, wh, "EFAS0042")
        return s, r, build_review_queue(s, r, e, insight, td)

    s1, r1, q1 = certify(spec)
    assert r1.verdict == "certified", [c.detail for c in r1.checks if c.status != "pass"]
    assert q1.items == [], [(i.kind, i.target_attribute) for i in q1.items]
    assert s1.unmapped_target == []

    # pressing certify again must not resurrect the queue or change the verdict
    s2, r2, q2 = certify(spec)
    assert r2.verdict == "certified" and q2.items == []
    assert len(s2.mappings) == len(s1.mappings)
    assert s2.generated_by.count("reviewed") == 1


def test_soft_warn_does_not_requeue_an_accepted_mapping():
    """A reconciliation warn is an observation about the DATA — no reviewer
    decision can clear it, so it must never drag an accepted mapping back into
    the queue (that is what made the loop non-terminating)."""
    import copy
    insight, e, td, wh, spec, report, q = _flow_b_fixture()
    warned = {c.target_attribute for c in report.checks
              if c.status == "warn" and c.category == "reconciliation"}
    assert warned, "fixture no longer produces a reconciliation warn"
    decisions = {i.target_attribute: {"action": "accept"}
                 for i in q.items if i.kind == "mapping_review"}
    s = apply_decisions(copy.deepcopy(spec), decisions, td)
    r = _validate(s, td, insight, wh, "EFAS0042")
    q2 = build_review_queue(s, r, e, insight, td)
    assert not (warned & {i.target_attribute for i in q2.items})
    assert warned <= set(q2.auto_accepted)
    # the warn itself is still reported — suppressed from the queue, not hidden
    assert any(c.status == "warn" and c.category == "reconciliation" for c in r.checks)


def test_load_time_defaults_validate_without_crashing():
    """A promoted default has NO source column. Every validator query that
    splices a source-column list must tolerate that (it used to emit
    'WHERE ' / 'SELECT DISTINCT  FROM' and raise a ParserException)."""
    import copy
    insight, e, td, wh, spec, report, q = _flow_b_fixture()
    s = apply_decisions(copy.deepcopy(spec), {}, td)          # no decisions at all
    defaults = {m.target_attribute: m.transformation_sql
                for m in s.mappings if not m.source_attributes}
    assert defaults["vulnerable_customer_flag"] == "false"
    assert defaults["source_system"] == "'EFAS0042'"
    assert "current_localtimestamp" in defaults["migrated_at"]
    r = _validate(s, td, insight, wh, "EFAS0042")             # must not raise
    # a required target defaulted at load must now be POPULATED, not null
    for attr in defaults:
        bad = [c for c in r.checks
               if c.target_attribute == attr and c.category == "completeness"
               and c.status == "fail"]
        assert not bad, bad


# ---- tab 4 must rebuild the SAME relation the spec was written against ----
def test_transform_workset_matches_composite_view(tmp_path):
    """engine/composite builds the joined view the mapping agent writes SQL
    against; api/transform rebuilds it independently at execution time. If the
    two disagree, a certified spec either fails with a missing column or
    silently returns NULLs. Pin the three things that used to differ: trimmed
    join keys, rename-on-clash, and chained-join aliasing."""
    import duckdb
    from api import transform as T
    from engine.composite import build_workset
    from engine.agents.contracts import (EnrichedColumn, EnrichedDictionary,
                                         Relationship, SourceRelationships)

    # COBOL fields are space-padded: the key on the driving side has trailing
    # blanks, and both files share a NAME column (a real clash).
    a = tmp_path / "AAA.csv"; a.write_text("KEY,NAME\nK1  ,alpha\nK2,beta\n")
    b = tmp_path / "BBB.csv"; b.write_text("KEY,NAME\nK1,employer-one\nK2,employer-two\n")

    wh = Warehouse(":memory:")
    wh.stage_csv(str(a), "AAA", all_varchar=True)
    wh.stage_csv(str(b), "BBB", all_varchar=True)

    def _dict(t):
        return EnrichedDictionary(table=t, columns=[
            EnrichedColumn(name="KEY", business_name="Key"),
            EnrichedColumn(name="NAME", business_name="Name")])

    rels = SourceRelationships(tables=["AAA", "BBB"], relationships=[
        Relationship(left_table="AAA", left_column="KEY",
                     right_table="BBB", right_column="KEY",
                     kind="join_key", cardinality="N:1", confidence=0.9)])

    view, combined, plan, _excluded = build_workset(
        wh, "AAA", ["AAA", "BBB"], {"AAA": _dict("AAA"), "BBB": _dict("BBB")}, rels)
    engine_cols = [c.name for c in combined.columns]
    assert "BBB_NAME" in engine_cols, engine_cols        # renamed, not dropped

    spec = {"source_tables": ["AAA", "BBB"], "join_plan": plan, "mappings": []}
    sql, params = T.build_workset_sql(spec, {"AAA": str(a), "BBB": str(b)})
    con = duckdb.connect(":memory:")
    cur = con.execute(sql, params)
    transform_cols = [d[0] for d in cur.description]
    rows = cur.fetchall()

    # identical column vocabulary — the spec's SQL resolves in both
    assert transform_cols == engine_cols, (transform_cols, engine_cols)
    # the padded key still joins (this is the trim() contract)
    joined = transform_cols.index("BBB_NAME")
    assert [r[joined] for r in rows] == ["employer-one", "employer-two"]
    # grain preserved: one output row per driving row
    assert len(rows) == wh.row_count("AAA")


def test_certified_spec_can_be_amended_and_recertified():
    """A certified mapping must remain amendable: a reviewer can change one
    decision and re-certify without discarding the others, and the decision
    annotations must not accumulate on each pass."""
    import copy
    insight, e, td, wh, spec, report, q = _flow_b_fixture()
    dec = {i.target_attribute: {"action": "accept"}
           for i in q.items if i.kind == "mapping_review"}

    s1 = apply_decisions(copy.deepcopy(spec), dec, td)
    r1 = _validate(s1, td, insight, wh, "EFAS0042")
    assert r1.verdict == "certified"
    by1 = {m.target_attribute: m for m in s1.mappings}
    assert by1["tax_file_number"].gate == "auto_accept"

    # amend ON TOP of the certified spec — one decision changed, rest re-sent
    dec2 = dict(dec); dec2["tax_file_number"] = {"action": "reject"}
    s2 = apply_decisions(copy.deepcopy(s1), dec2, td)
    r2 = _validate(s2, td, insight, wh, "EFAS0042")
    by2 = {m.target_attribute: m for m in s2.mappings}

    assert r2.verdict == "certified"
    assert by2["tax_file_number"].gate == "reject"
    # every other decision survives the amendment
    for attr in ("exit_reason", "annual_premium", "scheme_name"):
        assert by2[attr].gate == "auto_accept", attr
    # and the annotation is recorded once, not once per certify pass
    assert by2["exit_reason"].rationale.count("accepted by reviewer") == 1
    assert s2.generated_by.count("reviewed") == 1


def test_target_that_keeps_legacy_codes_is_mapped_but_not_auto_accepted():
    """A target may legitimately keep the legacy codes as its allowed values
    (the new platform stores 'C'/'U' rather than re-coding them). Matching only
    on the DECODED LABEL made that unmappable — every code was reported
    unmatched even though the target listed those exact codes.

    But a code-only match is weak evidence: it says both sides draw from the
    same small alphabet, not that they mean the same thing. So it maps, and it
    goes to a human."""
    import copy
    insight = analyze("data/EFAS0042.csv", "EFAS0042")
    e = _enrich(insight, _code("cbl"), _code("php"))
    td = _target_dict(**_SEMANTIC_GONE_AWAY)
    wh = Warehouse(":memory:")
    wh.stage_csv("data/EFAS0042.csv", "EFAS0042", all_varchar=True)

    # baseline: labels ('On Record'/'Untraceable') match neither target value
    base = {m.target_attribute: m for m in map_to_target(e, td, wh, "EFAS0042").mappings}
    assert base["gone_away"].unmapped_codes == ["C", "U"]
    assert base["gone_away"].gate == "reject"

    # the target is re-specified to keep the legacy codes themselves
    td2 = copy.deepcopy(td)
    for a in td2["attributes"]:
        if a["name"] == "gone_away":
            a["allowed_values"] = ["U", "C"]
    spec2 = map_to_target(e, td2, wh, "EFAS0042")
    m = {x.target_attribute: x for x in spec2.mappings}["gone_away"]

    assert m.unmapped_codes == []                    # now fully decoded
    assert "'C' THEN 'C'" in m.transformation_sql    # CASE still enforces the domain
    assert m.confidence >= 0.85                      # value evidence is now complete
    assert m.gate == "review"                        # ...but a code-only match is weak
    assert "raw codes" in m.rationale

    # label-matched enums must be entirely unaffected by the fallback
    for attr in ("product_category", "policy_status"):
        assert base[attr].gate == "auto_accept", attr
        assert base[attr].unmapped_codes == [], attr


def test_recovery_never_offers_the_chosen_source_as_its_own_alternative():
    """Competing candidates are recorded against whichever source was chosen at
    the time. LLM recovery replaces that source, so a stale list produced a
    review card reading 'chose COVSTDT; COVSTDT is an equally plausible match'.
    After recovery the list must name the DISPLACED source instead."""
    import types
    from engine.agents import mapping_agent as MA
    from engine import config as _cfg

    insight = analyze("data/EFAS0042.csv", "EFAS0042")
    e = _enrich(insight, _code("cbl"), _code("php"))
    td = _json.loads(open(TARGET_DICT).read())
    wh = Warehouse(":memory:")
    wh.stage_csv("data/EFAS0042.csv", "EFAS0042", all_varchar=True)

    # deterministic baseline: COMMDT wins, COVSTDT is the rival
    base = {m.target_attribute: m for m in map_to_target(e, td, wh, "EFAS0042").mappings}
    assert base["inception_date"].source_attributes == ["COMMDT"]
    assert [a["source"] for a in base["inception_date"].alternatives] == ["COVSTDT"]

    # a model that proposes the rival, flipping the choice
    class _R:
        def __init__(s, c):
            s.choices = [types.SimpleNamespace(
                message=types.SimpleNamespace(content=c), finish_reason="stop")]

    class _C:
        def create(s, model, temperature, max_tokens, response_format, messages):
            payload = _json.loads(messages[1]["content"])
            return _R(_json.dumps({"proposals": {
                t["name"]: {"source": ("COVSTDT" if t["name"] == "inception_date" else None),
                            "reason": "dates align with cover start"}
                for t in payload["targets"]}}))

    prev = _cfg.llm_client
    _cfg.llm_client = lambda: (
        types.SimpleNamespace(chat=types.SimpleNamespace(completions=_C())), "fake")
    MA.config = _cfg
    try:
        spec = MA.map_to_target(e, td, wh, "EFAS0042")
    finally:
        _cfg.llm_client = prev

    m = {x.target_attribute: x for x in spec.mappings}["inception_date"]
    assert m.source_attributes == ["COVSTDT"]
    alts = [a["source"] for a in m.alternatives]
    assert "COVSTDT" not in alts, "a mapping cannot compete with itself"
    assert "COMMDT" in alts, "the displaced source is the real alternative"

    # and the invariant holds for EVERY mapping, not just this one
    for x in spec.mappings:
        assert not (set(x.source_attributes) & {a.get("source") for a in (x.alternatives or [])}), \
            x.target_attribute


# ---- derived source insight (bucket D is an override, not a requirement) ----
from engine.agents.analyst import analyze_light                        # noqa: E402
from engine.insight_cache import get_or_derive, load as _cache_load    # noqa: E402
from engine.kgstore import file_sha256 as _sha                         # noqa: E402
from engine.orchestration.graph import flow_mapping_manual             # noqa: E402
from engine.agents.contracts import TableInsight                       # noqa: E402
from pathlib import Path                                               # noqa: E402


def test_analyze_light_matches_full_analyze_on_the_fields_consumed():
    """The lightweight path must agree with the full analyst on EXACTLY the
    fields anything downstream reads — candidate_keys and the structural parts
    of each dependency. Prose (`statement`) is display-only and may differ when
    the full path is LLM-narrated, so it is not compared."""
    full = analyze("data/EFAS0042.csv", "EFAS0042")
    light = analyze_light("data/EFAS0042.csv", "EFAS0042")

    assert light.candidate_keys == full.candidate_keys
    key = lambda d: (d.dependent, tuple(d.drivers), d.condition,
                     d.support_rows, round(d.confidence, 6))
    assert sorted(map(key, light.dependencies)) == sorted(map(key, full.dependencies))
    # ...and it skips the expensive stages rather than computing them
    assert light.generated_by == "deterministic+light"
    assert light.columns == [] and light.dq_rules == [] and light.pii == []


def test_derived_insight_is_cached_and_invalidated_by_content(tmp_path):
    wh = Warehouse(str(tmp_path / "wh.duckdb"))
    src = tmp_path / "S.csv"
    src.write_text("POLNO,STATCD,EXITDT\nP1,CL,20200101\nP2,IF,00000000\n")

    first = get_or_derive(wh, str(src), "S")
    assert _cache_load(wh, "S", _sha(str(src))) is not None      # written through
    assert get_or_derive(wh, str(src), "S").model_dump() == first.model_dump()

    old_sha = _sha(str(src))
    src.write_text("POLNO,STATCD,EXITDT\nP1,CL,20200101\nP2,CL,20200202\n")
    assert _sha(str(src)) != old_sha
    assert _cache_load(wh, "S", _sha(str(src))) is None           # stale never served
    assert get_or_derive(wh, str(src), "S") is not None
    wh.close()


def _manual_state(tmp_path):
    return {"source_csv": "data/revised/EFAS0042.csv", "table": "EFAS0042",
            "warehouse_path": str(tmp_path / "wh.duckdb"),
            "enriched_json": _json.loads(
                Path("data/revised/enriched_dictionary.json").read_text()),
            "target_dict": _json.loads(
                Path("data/revised/target_dictionary.json").read_text())}


def test_manual_mapping_derives_insight_from_source_data(tmp_path):
    """The source insight is no longer a user input. Key-integrity and
    crossfield checks must run off the derived one."""
    out = flow_mapping_manual.invoke(_manual_state(tmp_path))
    assert out["insight"].generated_by == "deterministic+light"
    assert out["insight"].candidate_keys == ["POLNO"]
    cats = [c.category for c in out["report"].checks]
    assert "key_integrity" in cats
    assert cats.count("crossfield") == 5


def test_derived_insight_matches_what_the_removed_upload_supplied(tmp_path):
    """Regression guard for removing bucket D: the derived insight must produce
    exactly the checks the hand-supplied source_insight.json used to enable, so
    dropping the upload loses nothing."""
    out = flow_mapping_manual.invoke(_manual_state(tmp_path))
    derived = out["insight"]
    uploaded = TableInsight.model_validate(
        _json.loads(Path("data/revised/source_insight.json").read_text()))

    assert derived.candidate_keys == uploaded.candidate_keys
    key = lambda d: (d.dependent, tuple(d.drivers), d.condition, d.support_rows)
    assert sorted(map(key, derived.dependencies)) == sorted(map(key, uploaded.dependencies))


def test_retired_insight_role_is_ignored_not_reclassified():
    """Bucket D is gone. A stale client posting role='insight' must have the
    file IGNORED — never fall through to extension classification, which would
    put a .json in the target bucket and silently replace the active target
    dictionary."""
    from fastapi.testclient import TestClient
    from api import server

    server.STATE = server._empty_state()
    c = TestClient(server.app)
    payload = Path("data/revised/source_insight.json").read_bytes()

    r = c.post("/api/inputs/upload",
               files={"files": ("source_insight.json", payload)},
               data={"role": "insight"}).json()
    assert r["added"]["ignored"] == ["source_insight.json"]
    assert r["targets"] == [] and r["sources"] == [] and r["enricheds"] == []
    assert "insights" not in r            # the bucket is gone from the manifest

    # a roleless upload still classifies by extension
    r = c.post("/api/inputs/upload",
               files={"files": ("target_dictionary.json", payload)}).json()
    assert r["added"]["target"] == 1
    server.STATE = server._empty_state()


# ---- validation coverage: the claim must be computed, not asserted ----
from api import validate as _V                                         # noqa: E402


def _delivered(tmp_path):
    """A spec + the CSV the transformation workspace delivers.

    Uses the SIMPLIFIED per-file dictionary (the current input model), not the
    legacy-expert one — without its aliases, tax_file_number and investor_id
    never get mapped, so they carry no transform to re-execute and the
    per-value examination figure is understated.
    """
    from api import transform as _T
    state = {"sources": [{"path": "data/revised/EFAS0042.csv", "table": "EFAS0042"}],
             "enriched_dicts": [_json.loads(Path(DICT_A).read_text())],
             "target_dict": _json.loads(Path("data/revised/target_dictionary.json").read_text()),
             "warehouse_path": str(tmp_path / "wh.duckdb")}
    out = flow_mapping_manual.invoke(state)
    spec = out["spec"].model_dump(mode="json")
    td = _json.loads(Path("data/revised/target_dictionary.json").read_text())
    paths = {"EFAS0042": "data/revised/EFAS0042.csv"}
    _cols, _rows_, csv_text, _stats = _T.run_transform(spec, paths)
    return spec, td, csv_text, paths


def test_every_target_attribute_appears_in_the_report(tmp_path):
    """No silent skips. Every attribute the target dictionary declares must
    produce at least one row in the report — executed or explicitly skipped.
    This is what lets the results table be read as coverage: the reader counts
    the rows instead of trusting a percentage."""
    spec, td, csv_text, paths = _delivered(tmp_path)
    rep = _V.run_output_validation(spec, td, csv_text, paths, None)

    declared = {a["name"] for a in td["attributes"]}
    seen = {c["target_attribute"] for c in rep["checks"] if c["target_attribute"]}
    assert declared <= seen, f"absent from the report entirely: {declared - seen}"


def test_every_executed_check_carries_its_sql_as_evidence(tmp_path):
    """A green tick is a claim; a green tick with the SQL it ran and the
    population it scanned is evidence. Passes must carry it too, not just
    failures — a pass is exactly what a reviewer would want to probe."""
    spec, td, csv_text, paths = _delivered(tmp_path)
    rep = _V.run_output_validation(spec, td, csv_text, paths, None)

    ran = [c for c in rep["checks"] if c["status"] in ("pass", "fail")]
    assert ran
    # structural checks compare column names and read no row values, so they
    # legitimately report a population of 0 — claiming otherwise is what made
    # the wellformed card read like the grain one
    STRUCTURAL = {"wellformed"}
    for c in ran:
        assert c["sql"], f"{c['name']} ({c['status']}) ran without recording its SQL"
        if c["category"] in STRUCTURAL:
            assert c["rows_scanned"] == 0, \
                f"{c['name']} is structural but claims a row population"
        else:
            assert c["rows_scanned"] > 0, f"{c['name']} reports no population scanned"
    assert any(c["status"] == "pass" for c in ran)
    # anything that did NOT run must say why instead
    for c in rep["checks"]:
        if c["status"] in ("warn", "skipped"):
            assert c["detail"].strip(), f"{c['name']} gave no reason for not running"


def test_rule_preview_describes_families_not_columns(tmp_path):
    """At 50 attributes a rule-per-column preview is 40+ cards saying the same
    thing. Completeness and domain are ONE rule each, applied per attribute."""
    spec, td, csv_text, _paths = _delivered(tmp_path)
    rules = _V.describe_validation_rules(spec, td, has_source=True, has_insight=True)

    per_cat = {}
    for r in rules:
        per_cat.setdefault(r["category"], []).append(r)
    assert len(per_cat["completeness"]) == 1
    assert len(per_cat["domain"]) == 1
    # ...but the preview still says WHICH attributes it covers, so it stays auditable
    assert per_cat["completeness"][0]["applies_to"]
    assert per_cat["domain"][0]["applies_to"]


def test_skipped_checks_state_a_reason_and_never_change_the_verdict(tmp_path):
    spec, td, csv_text, paths = _delivered(tmp_path)
    rep = _V.run_output_validation(spec, td, csv_text, paths, None)
    skipped = [c for c in rep["checks"] if c["status"] == "skipped"]

    assert skipped, "nullable non-enum attributes must yield explicit skips"
    for c in skipped:
        assert c["detail"].strip(), f"{c['name']} skipped with no reason given"
        assert c["target_attribute"]
    # the headline stats count EXECUTED checks only, so a skip can never
    # inflate 'passed' or flip the verdict
    assert rep["stats"]["checks"] == len([c for c in rep["checks"]
                                          if c["status"] != "skipped"])
    assert rep["stats"]["passed"] + rep["stats"]["warnings"] + \
           rep["stats"]["failures"] == rep["stats"]["checks"]


def _multi_state(tmp_path):
    return {"sources": [{"path": "data/revised/EFAS0042.csv", "table": "EFAS0042"},
                        {"path": "data/revised/ESCH0009.csv", "table": "ESCH0009"}],
            "enriched_dicts": [_json.loads(Path(DICT_A).read_text()),
                               _json.loads(Path(DICT_B).read_text())],
            "target_dict": _json.loads(Path(TARGET_DICT).read_text()),
            "warehouse_path": str(tmp_path / "wh.duckdb")}


# ---- v9: simplified per-file dictionaries + multi-source mapping ----
DICT_A = "data/revised/dict_EFAS0042.json"
DICT_B = "data/revised/dict_ESCH0009.json"


def _multi_state(tmp_path):
    return {"sources": [{"path": "data/revised/EFAS0042.csv", "table": "EFAS0042"},
                        {"path": "data/revised/ESCH0009.csv", "table": "ESCH0009"}],
            "enriched_dicts": [_json.loads(Path(DICT_A).read_text()),
                               _json.loads(Path(DICT_B).read_text())],
            "target_dict": _json.loads(Path(TARGET_DICT).read_text()),
            "warehouse_path": str(tmp_path / "wh.duckdb")}


def test_simplified_dictionary_has_no_legacy_provenance_fields():
    """The authored dictionary must not ask a human for COBOL/screen metadata.
    value_decode STAYS — it is business knowledge (code -> meaning) and the only
    evidence an enum target is allowed to match on."""
    gone = {"cobol_name", "cobol_pic", "screen_label", "evidence", "sources",
            "confidence", "derivation", "derivation_narrative",
            "derivation_resolved", "derivation_cobol", "derived_in_program",
            "derivation_lineage", "derivation_coverage"}
    for path in (DICT_A, DICT_B):
        d = _json.loads(Path(path).read_text())
        for c in d["columns"]:
            assert not (set(c) & gone), f"{path}:{c['name']} still carries {set(c) & gone}"
            assert c["name"] and c["business_name"]
    a = _json.loads(Path(DICT_A).read_text())
    coded = [c for c in a["columns"] if c.get("value_decode")]
    assert coded, "value_decode must survive simplification — enums depend on it"


def test_aliases_recover_matches_no_similarity_metric_can():
    """NINO->tax_file_number and CUSTID->investor_id share no content token with
    their targets. Authored aliases are what make them findable; without them
    the matcher previously fell back to column order and picked POLNO."""
    from engine.agents.contracts import EnrichedDictionary
    e = EnrichedDictionary.model_validate(_json.loads(Path(DICT_A).read_text()))
    td = _json.loads(Path(TARGET_DICT).read_text())
    wh = Warehouse(":memory:")
    wh.stage_csv("data/revised/EFAS0042.csv", "EFAS0042", all_varchar=True)
    by = {m.target_attribute: m for m in
          map_to_target(e, td, wh, "EFAS0042").mappings}

    assert by["tax_file_number"].source_attributes == ["NINO"]
    assert by["investor_id"].source_attributes == ["CUSTID"]
    assert by["life_assured_full_name"].source_attributes == ["MBRNAME"]
    assert by["policy_reference"].source_attributes == ["POLNO"]   # unbroken


def test_type_compatibility_alone_cannot_carry_a_mapping():
    """Regression guard for the flat-tie bug: two string columns are type
    compatible with every string target, so without a floor ~20 candidates tied
    at 0.4 and the winner was decided by column order."""
    from engine.agents.contracts import EnrichedColumn
    from engine.agents import mapping_agent as MA
    src = EnrichedColumn(name="ZZZZ", business_name="Totally Unrelated Thing",
                         inferred_type="FREE_TEXT")
    target = {"name": "employer_name", "type": "string",
              "description": "Legal name of the sponsoring employer."}
    assert MA._name_sim(src, target) == 0.0
    assert MA._composite(src, target) < 0.2


def test_multi_source_discovers_the_join_and_maps_across_files(tmp_path):
    out = flow_mapping_manual.invoke(_multi_state(tmp_path))
    spec = out["spec"]

    # the join is discovered from the DATA, not from matching column names
    # (SCHNO on the policy file vs SCHREF on the scheme file)
    assert len(out["join_plan"]) == 1
    j = out["join_plan"][0]
    assert (j["table"], j["on"], j["to_table"], j["to_column"]) == \
           ("ESCH0009", "SCHREF", "EFAS0042", "SCHNO")
    assert j["cardinality"] == "N:1"          # grain-preserving
    assert not out["excluded_sources"]

    by = {m.target_attribute: m for m in spec.mappings}
    assert by["employer_name"].source_attributes == ["EMPNM"]
    assert by["employer_name"].source_files == ["ESCH0009"]
    assert spec.stats["cross_file_mappings"] >= 1


def test_every_mapping_names_its_source_file(tmp_path):
    """Items 4 and 5: the file each mapping came from must be on the spec, and
    must survive certification."""
    out = flow_mapping_manual.invoke(_multi_state(tmp_path))
    spec = out["spec"]
    assert set(spec.source_tables) == {"EFAS0042", "ESCH0009"}
    for m in spec.mappings:
        assert m.source_files, f"{m.target_attribute} does not name its source file"
        assert set(m.source_files) <= set(spec.source_tables)

    decisions = {m.target_attribute: {"action": "accept"}
                 for m in spec.mappings if m.gate == "review"}
    certified = apply_decisions(spec, decisions)
    assert certified.source_tables == spec.source_tables
    assert {m.target_attribute: m.source_files for m in certified.mappings} == \
           {m.target_attribute: m.source_files for m in spec.mappings}


# ---- per-value examination: the claim the summary dashboard makes ----
def test_transform_rule_check_catches_a_single_tampered_cell(tmp_path):
    """The check that earns the confidence claim. Every other check asks whether
    the delivered file is internally plausible; this re-executes each certified
    transform and compares value by value, so one wrong cell in 1,150 fails."""
    import csv as _csv, io as _io
    spec, td, csv_text, paths = _delivered(tmp_path)
    clean = _V.run_output_validation(spec, td, csv_text, paths, None)
    # the spec here is uncertified, so an unmapped required attribute fails
    # completeness — that is expected. What must be clean is the TRANSFORM
    # family: an untampered file reproduces its certified SQL exactly.
    assert not [c for c in clean["checks"]
                if c["category"] == "transform" and c["status"] == "fail"]

    rows = list(_csv.reader(_io.StringIO(csv_text)))
    col = next(c for c in rows[0]
               if c in {m["target_attribute"] for m in spec["mappings"]
                        if m.get("transformation_sql")})
    rows[1][rows[0].index(col)] = "TAMPERED"
    buf = _io.StringIO()
    _csv.writer(buf, lineterminator="\n").writerows(rows)

    dirty = _V.run_output_validation(spec, td, buf.getvalue(), paths, None)
    failed = [c for c in dirty["checks"] if c["status"] == "fail"]
    assert failed, "a tampered cell went undetected"
    assert any(c["name"] == f"transform_rule:{col}" for c in failed)
    assert dirty["verdict"] == "blocked"


def test_non_deterministic_transforms_are_declared_not_failed(tmp_path):
    """now()/current_date can never reproduce on re-execution. Reporting that as
    a mismatch would fail every load-audit column on every run."""
    spec, td, csv_text, paths = _delivered(tmp_path)
    spec = _json.loads(_json.dumps(spec))
    victim = spec["mappings"][0]["target_attribute"]
    spec["mappings"][0]["transformation_sql"] = "CAST(now() AS VARCHAR)"

    rep = _V.run_output_validation(spec, td, csv_text, paths, None)
    c = next(x for x in rep["checks"] if x["name"] == f"transform_rule:{victim}")
    assert c["status"] == "skipped"
    assert "non-deterministic" in c["detail"]


def test_every_column_receives_per_value_examination(tmp_path):
    """The summary dashboard claims N of R x C values were read. That must be
    counted from checks that actually ran, and any column that escaped
    per-value examination must be named rather than hidden."""
    spec, td, csv_text, paths = _delivered(tmp_path)
    cc = _V.run_output_validation(spec, td, csv_text, paths, None)["coverage_cells"]

    assert cc["cells_total"] == cc["rows"] * cc["columns_total"]
    assert cc["cells_examined"] == cc["rows"] * cc["columns_examined"]
    assert cc["columns_examined"] == cc["columns_total"], cc["columns_not_examined"]
    assert cc["columns_not_examined"] == []
    assert cc["assertions"] >= cc["cells_examined"]


def test_duplicate_rows_are_detected_independently_of_the_key(tmp_path):
    """A file can carry a unique key and still hold identical row bodies."""
    import csv as _csv, io as _io
    spec, td, csv_text, paths = _delivered(tmp_path)
    rows = list(_csv.reader(_io.StringIO(csv_text)))
    rows.append(list(rows[1]))                     # exact duplicate row
    buf = _io.StringIO()
    _csv.writer(buf, lineterminator="\n").writerows(rows)

    rep = _V.run_output_validation(spec, td, buf.getvalue(), paths, None)
    dup = next(c for c in rep["checks"] if c["name"] == "no_duplicate_rows")
    assert dup["status"] == "fail" and dup["offending_rows"] == 1


def test_nondeterminism_detector_matches_underscored_builtins():
    """Regression: `current_localtimestamp()` slipped past a \\b-anchored regex
    because the underscore is a word character. The transform check then only
    passed when the transform and the validation ran inside the same second —
    a time-dependent pass, which hides rather than fails."""
    nd = _V._is_nondeterministic
    for expr in ("strftime(current_localtimestamp(), '%Y-%m-%d %H:%M:%S')",
                 "now()", "CURRENT_TIMESTAMP", "CAST(now () AS VARCHAR)",
                 "current_date", "localtimestamp"):
        assert nd(expr), expr
    for expr in ("'EFAS0042'", 'NULLIF(TRIM("NINO"), \'\')',
                 "CASE WHEN x = 'CL' THEN 'CLAIMED' END"):
        assert not nd(expr), expr


def test_data_type_check_covers_every_declared_attribute(tmp_path):
    """(3) Data type is asserted for ALL attributes against the type the mapping
    spec targets — not only the numerically typed ones."""
    spec, td, csv_text, paths = _delivered(tmp_path)
    rep = _V.run_output_validation(spec, td, csv_text, paths, None)

    declared = {a["name"] for a in td["attributes"]}
    typed = {c["target_attribute"] for c in rep["checks"]
             if c["category"] == "type" and c["status"] in ("pass", "fail")}
    assert typed == declared, declared - typed


def test_recorded_sql_is_the_query_that_actually_ran(tmp_path):
    """Evidence must be the executed query, not an illustration of it. The
    wellformed check used to record `SELECT * FROM target_out LIMIT 0` while
    doing its comparison in Python, and inherited a '50 rows scanned' line from
    the row-based checks — which made it read like the grain query in the wrong
    card. A structural check reports no row population."""
    spec, td, csv_text, paths = _delivered(tmp_path)
    rep = _V.run_output_validation(spec, td, csv_text, paths, None)
    by = {c["name"]: c for c in rep["checks"]}

    wf, gr = by["delivered_columns_match_spec"], by["row_count_preserved"]
    # each check's SQL must name what that check is about
    assert "information_schema.columns" in wf["sql"]
    assert "count(*)" not in wf["sql"].lower()
    assert wf["rows_scanned"] == 0                     # columns, not rows
    assert "count(*)" in gr["sql"].lower()
    assert gr["rows_scanned"] > 0

    # and the wellformed SQL must be executable and agree with the verdict
    import duckdb
    con = duckdb.connect(":memory:")
    _V._load_output(con, csv_text)
    rows = con.execute(wf["sql"].split("--")[-1].split("\n", 1)[1]
                       if wf["sql"].startswith("--") else wf["sql"]).fetchall()
    con.close()
    assert (len(rows) == 0) == (wf["status"] == "pass")


# ---- offending records: a failure you cannot see is half an answer ----
def _tamper(csv_text, col, row_idx, value):
    import csv as _csv, io as _io
    rows = list(_csv.reader(_io.StringIO(csv_text)))
    rows[row_idx][rows[0].index(col)] = value
    buf = _io.StringIO()
    _csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def test_transform_failure_shows_expected_vs_delivered_keyed_by_record(tmp_path):
    """'1 of 50 values differ' says something is wrong, not WHICH record. The
    sample must carry the business key plus both sides of the diff."""
    spec, td, csv_text, paths = _delivered(tmp_path)
    col = "life_assured_full_name"
    rep = _V.run_output_validation(spec, td, _tamper(csv_text, col, 1, "TAMPERED"),
                                   paths, None)

    c = next(x for x in rep["checks"] if x["name"] == f"transform_rule:{col}")
    assert c["status"] == "fail" and c["sample"]
    row = c["sample"][0]
    assert row["expected"] and row["delivered"] == "TAMPERED"
    assert row["expected"] != row["delivered"]
    # traceable back to a record, not just a position
    assert any(k not in ("row_number", "expected", "delivered") for k in row)


def test_offending_row_numbers_are_file_positions_not_filtered_positions(tmp_path):
    """row_number() OVER () applied AFTER a WHERE numbers the filtered set, so
    the first offender always read 'row 1' wherever it sat in the file."""
    spec, td, csv_text, paths = _delivered(tmp_path)
    rep = _V.run_output_validation(spec, td,
                                   _tamper(csv_text, "commencement_date", 5, "not-a-date"),
                                   paths, None)

    dt = next(x for x in rep["checks"] if x["name"] == "data_type:commencement_date")
    assert dt["status"] == "fail"
    assert dt["sample"][0]["row_number"] == 5
    tr = next(x for x in rep["checks"] if x["name"] == "transform_rule:commencement_date")
    assert tr["sample"][0]["row_number"] == 5      # both checks agree


def test_row_count_mismatch_suspends_transform_comparison(tmp_path):
    """Rows are aligned by ordinal position. One extra row shifts every
    subsequent column and would cascade into a dozen spurious failures that
    bury the real defect — so the comparison declines to run and says why."""
    import csv as _csv, io as _io
    spec, td, csv_text, paths = _delivered(tmp_path)
    rows = list(_csv.reader(_io.StringIO(csv_text)))
    rows.append(list(rows[2]))
    buf = _io.StringIO()
    _csv.writer(buf, lineterminator="\n").writerows(rows)
    rep = _V.run_output_validation(spec, td, buf.getvalue(), paths, None)

    assert next(x for x in rep["checks"]
                if x["name"] == "row_count_preserved")["status"] == "fail"
    tr = [x for x in rep["checks"] if x["category"] == "transform"]
    assert tr and all(x["status"] == "skipped" for x in tr)
    assert "grain" in tr[0]["detail"]
    # the duplicate itself is still reported, with the offending key
    dup = next(x for x in rep["checks"] if x["name"] == "no_duplicate_rows")
    assert dup["status"] == "fail" and dup["sample"]


# ---- reconciliation: control totals + generated category profiles ----
from api import reconcile as _R                                        # noqa: E402


def _reconciled(tmp_path):
    from api import transform as _T
    from engine.insight_cache import get_or_derive
    wh = Warehouse(str(tmp_path / "wh.duckdb"))
    out = flow_mapping_manual.invoke(dict(_multi_state(tmp_path), warehouse=wh))
    spec = apply_decisions(out["spec"], {m.target_attribute: {"action": "accept"}
                                        for m in out["spec"].mappings
                                        if m.gate == "review"}).model_dump(mode="json")
    td = _json.loads(Path(TARGET_DICT).read_text())
    paths = {"EFAS0042": "data/revised/EFAS0042.csv",
             "ESCH0009": "data/revised/ESCH0009.csv"}
    _c, _r, csv_text, _s = _T.run_transform(spec, paths)
    insight = get_or_derive(wh, "data/revised/EFAS0042.csv", "EFAS0042").model_dump(mode="json")
    enriched = _json.loads(Path(DICT_A).read_text())
    return _R.run_reconciliation(spec, td, csv_text, paths, enriched, insight)


def test_control_totals_are_reported(tmp_path):
    """The figures a migration control sheet carries — all derivable from any
    spec plus any delivered file, so nothing here is file-specific."""
    rep = _reconciled(tmp_path)
    names = {c["name"] for c in rep["checks"] if c["category"] == "control_total"}
    assert {"control_total:columns", "control_total:populated_cells",
            "control_total:distinct_keys"} <= names
    cells = next(c for c in rep["checks"] if c["name"] == "control_total:populated_cells")
    # fill rate must distinguish a LEGITIMATE blank (nullable attribute) from a
    # blank in an attribute that requires a value. "916 populated of 1,150" read
    # as 234 cells lost, when nearly all were nullable-and-empty by design.
    assert "hold a value" in cells["detail"]
    assert "blank" in cells["detail"]
    assert ("declares\n" in cells["detail"] or "nullable" in cells["detail"]
            or "require a value" in cells["detail"])


def test_category_profiles_are_derived_not_configured(tmp_path):
    """'How many live policies, how many exited, how many per product' must fall
    out of the DATA — every low-cardinality attribute is profiled, so pointing
    this at a claims or member extract profiles whatever it happens to hold."""
    rep = _reconciled(tmp_path)
    profiles = {c["target_attribute"]: c for c in rep["checks"]
                if c["category"] == "category_profile"}
    assert "policy_status" in profiles and "product_category" in profiles
    assert all(c["status"] == "pass" for c in profiles.values())
    # per-value record counts, not just a distinct-count
    assert "IN_FORCE=" in profiles["policy_status"]["detail"]

    # identifiers and free text are not categories, and dates are not either:
    # "how many exited on 2017-07-14" is not a business control
    assert "policy_reference" not in profiles
    assert "exit_date" not in profiles


def test_category_profile_catches_a_lost_bucket(tmp_path):
    """A dropped category must fail, with the bucket named."""
    import csv as _csv, io as _io
    from api import transform as _T
    from engine.insight_cache import get_or_derive
    wh = Warehouse(str(tmp_path / "wh.duckdb"))
    out = flow_mapping_manual.invoke(dict(_multi_state(tmp_path), warehouse=wh))
    spec = apply_decisions(out["spec"], {m.target_attribute: {"action": "accept"}
                                        for m in out["spec"].mappings
                                        if m.gate == "review"}).model_dump(mode="json")
    td = _json.loads(Path(TARGET_DICT).read_text())
    paths = {"EFAS0042": "data/revised/EFAS0042.csv",
             "ESCH0009": "data/revised/ESCH0009.csv"}
    _c, _r, csv_text, _s = _T.run_transform(spec, paths)

    rows = list(_csv.reader(_io.StringIO(csv_text)))
    i = rows[0].index("policy_status")
    for r in rows[1:]:
        if r[i] == "LAPSED":
            r[i] = "IN_FORCE"                      # a whole bucket moves
    buf = _io.StringIO()
    _csv.writer(buf, lineterminator="\n").writerows(rows)
    insight = get_or_derive(wh, "data/revised/EFAS0042.csv", "EFAS0042").model_dump(mode="json")

    rep = _R.run_reconciliation(spec, td, buf.getvalue(), paths, None, insight)
    c = next(x for x in rep["checks"] if x["name"] == "category_profile:policy_status")
    assert c["status"] == "fail" and c["sample"]
    moved = {s["value"] for s in c["sample"]}
    assert {"LAPSED", "IN_FORCE"} <= moved


def test_crossfield_translates_source_codes_into_target_terms(tmp_path):
    """The dependency is mined from the SOURCE ('STATCD in {CL}') but evaluated
    against delivered data where the transform has already decoded CL->CLOSED.
    Comparing the two directly made all five rules fail 100% on correct data."""
    rep = _reconciled(tmp_path)
    cf = [c for c in rep["checks"] if c["category"] == "crossfield"]
    assert cf, "the mined dependencies should yield cross-field rules"
    assert all(c["status"] == "pass" for c in cf), \
        [c["detail"] for c in cf if c["status"] != "pass"]
    # the rule must be stated in TARGET vocabulary, not source codes
    joined = " ".join(c["detail"] for c in cf)
    assert "CLOSED" in joined and "{CL}" not in joined


def test_reconciliation_carries_no_redundant_families(tmp_path):
    """Families removed on purpose: value_loss (subsumed by validation's
    value-by-value transform check), aggregate as a check (demoted to a reported
    control total), derivation (structurally dead once the dictionary was
    simplified). Row count moved into control totals — it was the one check name
    duplicated with the validation workspace."""
    rep = _reconciled(tmp_path)
    cats = {c["category"] for c in rep["checks"]}
    assert cats == {"control_total", "category_profile", "crossfield"}
    assert not (cats & {"value_loss", "aggregate", "derivation", "grain"})

    names = {c["name"] for c in rep["checks"]}
    assert {"control_total:rows", "control_total:columns",
            "control_total:populated_cells", "control_total:distinct_keys",
            "control_total:numeric_sums"} <= names


def test_no_check_name_is_shared_with_the_validation_workspace(tmp_path):
    """The two workspaces must not run the same check twice under the same
    name — a reviewer seeing it in both cannot tell whether it was corroborated
    or merely repeated."""
    from api import transform as _T
    from engine.insight_cache import get_or_derive
    wh = Warehouse(str(tmp_path / "wh.duckdb"))
    out = flow_mapping_manual.invoke(dict(_multi_state(tmp_path), warehouse=wh))
    spec = apply_decisions(out["spec"], {m.target_attribute: {"action": "accept"}
                                        for m in out["spec"].mappings
                                        if m.gate == "review"}).model_dump(mode="json")
    td = _json.loads(Path(TARGET_DICT).read_text())
    paths = {"EFAS0042": "data/revised/EFAS0042.csv",
             "ESCH0009": "data/revised/ESCH0009.csv"}
    _c, _r, csv_text, _s = _T.run_transform(spec, paths)
    insight = get_or_derive(wh, "data/revised/EFAS0042.csv", "EFAS0042").model_dump(mode="json")

    val = {c["name"] for c in
           _V.run_output_validation(spec, td, csv_text, paths, insight)["checks"]}
    rec = {c["name"] for c in
           _R.run_reconciliation(spec, td, csv_text, paths, None, insight)["checks"]}
    assert not (val & rec), f"duplicated across workspaces: {sorted(val & rec)}"


def test_llm_readiness_requires_the_sdk_not_just_a_key(monkeypatch):
    """Credentials alone are not readiness. With OPENAI_API_KEY set but the
    `openai` package absent, llm_ready() said True and the first LLM call raised
    ModuleNotFoundError mid-pipeline, taking the whole mapping run down — a
    crash caused by ADDING a key to a machine without the optional dependency."""
    import importlib.util
    from engine import config

    monkeypatch.setenv("OPENAI_API_KEY", "sk-placeholder")
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda n, *a, **k: None if n == "openai" else real(n, *a, **k))

    assert config.credentials_present() is True
    assert config.llm_ready() is False          # not ready: no usable client
    assert config.llm_client() == (None, None)  # must not raise
    assert "not installed" in config.llm_label()


def test_pipeline_and_codegen_run_with_no_llm_at_all(monkeypatch, tmp_path):
    """The whole offline story in one assertion: with no credentials, mapping
    completes deterministically and all three generators emit real scripts.
    Code generation never touches an LLM by design — an auditor's script has to
    be reproducible, so it is built from the certified spec, not generated."""
    from api import transform as _T
    for var in ("OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
                "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    from engine import config
    assert config.llm_ready() is False

    out = flow_mapping_manual.invoke(_manual_state(tmp_path))
    spec = out["spec"].model_dump(mode="json")
    assert spec["stats"]["mapped"] > 0
    assert spec["stats"]["llm_recovered"] == 0

    td = _json.loads(Path(TARGET_DICT).read_text())
    names = {"EFAS0042": "EFAS0042.csv"}
    assert len(_T.generate_python(spec, names).splitlines()) > 20
    assert len(_V.generate_validation_script(spec, td, "policy.csv", True, True).splitlines()) > 20
    assert len(_R.generate_reconciliation_script(spec, "policy.csv", names).splitlines()) > 10


# ---- generality: a different domain, generated scripts must RUN ----
FIX = Path("tests/fixtures")


def test_generated_scripts_run_standalone_on_an_unseen_domain(tmp_path):
    """The three generators are template-driven, so the question is whether they
    survive a spec with entirely different CONTENT. This fixture is a claims
    extract, not policies, and is deliberately awkward: a space in a column name
    ('CLM REF'), a unicode column and values ('Ünïcode Col' -> α/β/γ), and an
    apostrophe inside a target enum value ("O'Brien Scheme TPD") — which used to
    emit WHEN 'TPD' THEN 'O'Brien...', invalid SQL that silently downgraded a
    good mapping to reject.

    Scripts are not merely parsed: they are EXECUTED in a temp directory and
    must exit 0, because a script that parses but throws at runtime is no use
    to an auditor.
    """
    import shutil
    import subprocess
    import sys as _sys
    from api import transform as _T

    for f in ("CLAIMS99.csv",):
        shutil.copy(FIX / f, tmp_path / f)
    out = flow_mapping_manual.invoke({
        "sources": [{"path": str(FIX / "CLAIMS99.csv"), "table": "CLAIMS99"}],
        "enriched_dicts": [_json.loads((FIX / "dict_CLAIMS99.json").read_text())],
        "target_dict": _json.loads((FIX / "target_claim.json").read_text()),
        "warehouse_path": str(tmp_path / "wh.duckdb")})
    spec = apply_decisions(out["spec"], {m.target_attribute: {"action": "accept"}
                                        for m in out["spec"].mappings
                                        if m.gate == "review"}).model_dump(mode="json")
    # the apostrophe enum must survive as a real mapping, not a reject
    ct = next(m for m in spec["mappings"] if m["target_attribute"] == "claim_type")
    assert ct["gate"] == "auto_accept" and "''Brien" in ct["transformation_sql"]

    td = _json.loads((FIX / "target_claim.json").read_text())
    names = {"CLAIMS99": "CLAIMS99.csv"}
    (tmp_path / "t.py").write_text(_T.generate_python(spec, names))
    (tmp_path / "v.py").write_text(
        _V.generate_validation_script(spec, td, "claim.csv", True, True))
    (tmp_path / "r.py").write_text(
        _R.generate_reconciliation_script(spec, "claim.csv", names, None, None, td))

    for script in ("t.py", "v.py", "r.py"):
        proc = subprocess.run([_sys.executable, script], cwd=tmp_path,
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{script} failed:\n{proc.stdout}\n{proc.stderr}"
    assert (tmp_path / "claim.csv").exists()


def test_sql_literals_are_escaped_everywhere_they_are_built():
    """Values reaching SQL come from DATA and from the target dictionary, so an
    apostrophe is a matter of when, not if — a scheme called "St John's" or a
    policyholder named O'Neill breaks any unescaped literal."""
    from engine.agents.mapping_agent import _lit
    assert _lit("O'Brien") == "'O''Brien'"
    assert _lit("plain") == "'plain'"
    assert _lit("it's a 'quoted' thing") == "'it''s a ''quoted'' thing'"


# ---- derivation gap: the target describes work the SQL does not do ----
GEN3 = Path("tests/fixtures")


def _composite_spec(tmp_path):
    return flow_mapping_manual.invoke({
        "sources": [{"path": str(GEN3 / "SRC.csv"), "table": "SRC"}],
        "enriched_dicts": [_json.loads((GEN3 / "dict_SRC.json").read_text())],
        "target_dict": _json.loads((GEN3 / "target_member.json").read_text()),
        "warehouse_path": str(tmp_path / "wh.duckdb")})


def test_composite_targets_never_auto_accept_a_single_column_copy(tmp_path):
    """The synthesiser has a single-column repertoire, so a target needing
    concatenation, unit conversion, date arithmetic or reformatting used to
    receive a silent copy of one column — and CERTIFY CLEAN, because every
    downstream check can confirm a value is well-formed but not that it is the
    value the target asked for."""
    spec = _composite_spec(tmp_path)["spec"]
    by = {m.target_attribute: m for m in spec.mappings}

    for attr in ("full_name", "annual_premium_gbp",
                 "age_at_commencement", "postcode_normalised"):
        m = by[attr]
        assert m.derivation_gap, f"{attr}: composite target not detected"
        assert m.gate != "auto_accept", f"{attr}: silently auto-accepted"
        assert "copies a single column" in m.rationale
    # the one genuinely simple mapping is unaffected
    assert by["policy_reference"].gate == "auto_accept"
    assert not by["policy_reference"].derivation_gap


def test_derivation_gap_needs_distinctive_evidence_not_shared_words(tmp_path):
    """'scheme' appears in Scheme Name, Scheme Number and Scheme Reference, and
    'policy' in half the dictionary. Matching on shared vocabulary flagged six
    correct mappings on the real data as composition gaps."""
    out = flow_mapping_manual.invoke(_multi_state(tmp_path))
    flagged = {m.target_attribute for m in out["spec"].mappings if m.derivation_gap}
    for attr in ("employer_name", "scheme_name", "scheme_reference",
                 "sum_assured", "early_exit_penalty_percent"):
        assert attr not in flagged, f"{attr} falsely flagged as a derivation gap"


def test_llm_transform_proposals_are_tested_not_trusted(tmp_path, monkeypatch):
    """The escalation tier must reject a proposal that invents a column, is a
    statement rather than an expression, or fails to execute — so it can only
    improve on the deterministic mapping, never damage it."""
    from engine.agents import mapping_agent as MA

    proposals = {}

    class _Stub:
        pass

    from engine import config as _cfg
    monkeypatch.setattr(_cfg, "llm_client", lambda: (_Stub(), "stub-model"))
    monkeypatch.setattr(MA, "_llm_json",
                        lambda *a, **k: {"results": list(proposals.values())})

    # 1. a proposal referencing a column that does not exist must be discarded
    proposals["full_name"] = {"target": "full_name",
                              "sql": '"NO_SUCH_COL" || \' \' || "SURNAME"',
                              "reason": "invented"}
    spec = _composite_spec(tmp_path)["spec"]
    m = next(x for x in spec.mappings if x.target_attribute == "full_name")
    assert not m.llm_recovered and m.source_attributes == ["SURNAME"]

    # 2. a statement rather than a scalar expression must be discarded
    proposals["full_name"] = {"target": "full_name",
                              "sql": 'SELECT "FORENAME" FROM SRC', "reason": "stmt"}
    spec = _composite_spec(tmp_path)["spec"]
    assert not next(x for x in spec.mappings
                    if x.target_attribute == "full_name").llm_recovered

    # 3. a valid composite IS applied — and capped at review, never auto-accept
    proposals["full_name"] = {"target": "full_name",
                              "sql": 'TRIM("FORENAME") || \' \' || TRIM("SURNAME")',
                              "reason": "concatenate forename and surname"}
    spec = _composite_spec(tmp_path)["spec"]
    m = next(x for x in spec.mappings if x.target_attribute == "full_name")
    assert m.llm_recovered and m.gate == "review"
    assert sorted(m.source_attributes) == ["FORENAME", "SURNAME"]
    assert m.match_source == "llm_transform"


def test_generated_script_executes_byte_identical_sql(tmp_path):
    """The generated script is the reproducibility claim: 'run this and you get
    what we got'. That only holds if the SQL in the script is a FAITHFUL copy of
    the certified spec. It was not — the expression was embedded in a normal
    Python string, so any backslash escape Python recognised was reinterpreted:
    a certified '\\t' reached DuckDB as a literal tab, and the handed-over script
    silently executed different SQL from the one the workspace validated."""
    import ast as _ast
    from api import transform as _T

    exotic = {
        "tabbed": "regexp_replace(\"PCODE\", '\\t', ' ', 'g')",
        "digits": "regexp_replace(\"PCODE\", '\\\\d', 'X', 'g')",
        "quoted": "'O''Brien'",
        "windowed": 'row_number() OVER (PARTITION BY "PRODCD" ORDER BY "COMMDT")',
    }
    spec = {"target_table": "x", "source_table": "EFAS0042",
            "source_tables": ["EFAS0042"], "unmapped_target": [], "join_plan": [],
            "mappings": [{"target_attribute": n, "source_attributes": ["PCODE"],
                          "cardinality": "1:1", "transformation_sql": sql,
                          "transformation_note": "n", "gate": "auto_accept",
                          "confidence": 1.0, "source_files": ["EFAS0042"]}
                         for n, sql in exotic.items()]}

    code = _T.generate_python(spec, {"EFAS0042": "EFAS0042.csv"})
    executed = next(node.value for node in _ast.walk(_ast.parse(code))
                    if isinstance(node, _ast.Constant)
                    and isinstance(node.value, str) and "regexp_replace" in node.value)
    for sql in exotic.values():
        assert sql in executed, f"the script does not execute the certified SQL: {sql!r}"

    # ...and it still runs
    import shutil, subprocess, sys as _sys
    shutil.copy("data/revised/EFAS0042.csv", tmp_path)
    (tmp_path / "t.py").write_text(code)
    proc = subprocess.run([_sys.executable, "t.py"], cwd=tmp_path,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_generator_is_agnostic_to_transform_pattern(tmp_path):
    """The generators embed the certified expression; they never parse it. So a
    transformation pattern the deterministic SYNTHESISER could not have produced
    (window function, regex, nested CASE, date arithmetic) still generates and
    runs — unfamiliar patterns fail at synthesis, not at generation."""
    import shutil, subprocess, sys as _sys
    from api import transform as _T

    unsynthesisable = [
        ("rank", 'row_number() OVER (PARTITION BY "PRODCD" ORDER BY "COMMDT" DESC)'),
        ("nested", 'CASE WHEN "STATCD"=\'CL\' THEN CASE WHEN "EXITRSN"=\'D\' '
                   'THEN \'Death\' ELSE \'Other\' END ELSE \'Active\' END'),
        ("concat", 'TRIM("MBRNAME") || \' [\' || "POLNO" || \']\''),
        ("datemath", 'date_diff(\'year\', strptime("BIRTHDT", \'%Y%m%d\'), '
                     'strptime("COMMDT", \'%Y%m%d\'))'),
    ]
    spec = {"target_table": "x", "source_table": "EFAS0042",
            "source_tables": ["EFAS0042"], "unmapped_target": [], "join_plan": [],
            "mappings": [{"target_attribute": n, "source_attributes": ["POLNO"],
                          "cardinality": "1:1", "transformation_sql": sql,
                          "transformation_note": "n", "gate": "auto_accept",
                          "confidence": 1.0, "source_files": ["EFAS0042"]}
                         for n, sql in unsynthesisable]}
    shutil.copy("data/revised/EFAS0042.csv", tmp_path)
    (tmp_path / "t.py").write_text(_T.generate_python(spec, {"EFAS0042": "EFAS0042.csv"}))
    proc = subprocess.run([_sys.executable, "t.py"], cwd=tmp_path,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / "x.csv").exists()


def test_fill_rate_separates_legitimate_blanks_from_missing_required(tmp_path):
    """A blank in a nullable attribute is by design; a blank in a required one is
    a defect. Reporting a single 'populated of total' figure conflated them and
    made a healthy file look like it had lost hundreds of cells."""
    rep = _reconciled(tmp_path)
    c = next(x for x in rep["checks"] if x["name"] == "control_total:populated_cells")
    assert "hold a value" in c["detail"]
    # offending_rows counts ONLY blanks in attributes that require a value
    if c["status"] == "pass":
        assert c["offending_rows"] == 0
        assert "nullable" in c["detail"]
    else:
        assert c["offending_rows"] > 0
        assert "require a value" in c["detail"]


def test_rule_preview_speaks_the_delivered_files_vocabulary(tmp_path):
    """The preview claimed rules were 'evaluated in target terms' and then listed
    SOURCE codes: 'policy_status in {CL}' while the report said {CLOSED}. Preview
    and report must not disagree — the preview has no database, so the decode is
    read out of the certified CASE expression instead."""
    from engine.insight_cache import get_or_derive
    from api import transform as _T

    wh = Warehouse(str(tmp_path / "wh.duckdb"))
    out = flow_mapping_manual.invoke(dict(_multi_state(tmp_path), warehouse=wh))
    spec = apply_decisions(out["spec"], {m.target_attribute: {"action": "accept"}
                                        for m in out["spec"].mappings
                                        if m.gate == "review"}).model_dump(mode="json")
    insight = get_or_derive(wh, "data/revised/EFAS0042.csv", "EFAS0042").model_dump(mode="json")

    rules = _R.describe_reconciliation_rules(
        spec, {"EFAS0042": "EFAS0042.csv"}, insight, None)
    cf = next(r for r in rules if r["category"] == "crossfield")

    # itemised, not a semicolon-joined paragraph
    assert cf["items"] and all({"attribute", "driver", "values"} <= set(i)
                               for i in cf["items"])
    values = {v for i in cf["items"] for v in i["values"]}
    assert "CLOSED" in values and "CL" not in values, values

    # ...and the same vocabulary the report uses
    td = _json.loads(Path(TARGET_DICT).read_text())
    paths = {"EFAS0042": "data/revised/EFAS0042.csv",
             "ESCH0009": "data/revised/ESCH0009.csv"}
    _c, _r, csv_text, _s = _T.run_transform(spec, paths)
    rep = _R.run_reconciliation(spec, td, csv_text, paths, None, insight)
    reported = " ".join(c["detail"] for c in rep["checks"]
                        if c["category"] == "crossfield")
    for v in values:
        assert v in reported, f"preview says {v!r}, report never mentions it"


def test_decode_pairs_reads_a_certified_case_expression():
    from api.reconcile import decode_pairs
    sql = ("CASE \"XA22\" WHEN 'C' THEN 'On Record' WHEN 'U' THEN 'O''Brien' "
           "ELSE NULL END")
    assert decode_pairs(sql) == {"C": "On Record", "U": "O'Brien"}
    assert decode_pairs('NULLIF(TRIM("X"), \'\')') == {}
    assert decode_pairs("") == {}


def test_standalone_recon_script_uses_decoded_driver_values(tmp_path):
    """The decode bug lived in THREE places: the report, the preview, and the
    generated script. The script is the one a client runs unsupervised, so a
    source code there means five red failures on correct data with nobody
    around to explain them."""
    import shutil, subprocess, sys as _sys
    from engine.insight_cache import get_or_derive
    from api import transform as _T

    wh = Warehouse(str(tmp_path / "wh.duckdb"))
    out = flow_mapping_manual.invoke(dict(_multi_state(tmp_path), warehouse=wh))
    spec = apply_decisions(out["spec"], {m.target_attribute: {"action": "accept"}
                                        for m in out["spec"].mappings
                                        if m.gate == "review"}).model_dump(mode="json")
    td = _json.loads(Path(TARGET_DICT).read_text())
    paths = {"EFAS0042": "data/revised/EFAS0042.csv",
             "ESCH0009": "data/revised/ESCH0009.csv"}
    _c, _r, csv_text, _s = _T.run_transform(spec, paths)
    insight = get_or_derive(wh, "data/revised/EFAS0042.csv", "EFAS0042").model_dump(mode="json")

    for f in paths.values():
        shutil.copy(f, tmp_path)
    (tmp_path / "policy.csv").write_text(csv_text)
    (tmp_path / "r.py").write_text(_R.generate_reconciliation_script(
        spec, "policy.csv", {"EFAS0042": "EFAS0042.csv"}, insight, None, td))

    proc = subprocess.run([_sys.executable, "r.py"], cwd=tmp_path,
                          capture_output=True, text=True)
    assert "CLOSED" in proc.stdout and "'CL'" not in proc.stdout
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_recon_summary_does_not_restate_the_detail_figures(tmp_path):
    """The summary answers 'does it reconcile?'. It must not repeat rows,
    columns, keys, fill rate and numeric totals — every one of those appears
    verbatim in the technical cards immediately below, so restating them made
    the reader pay for the same information twice. What belongs here is the
    verdict and how many controls of each family held."""
    rep = _reconciled(tmp_path)
    tech = [c for c in rep["checks"] if c["category"] == "control_total"]
    biz = [c for c in rep["checks"]
           if c["category"] in ("category_profile", "crossfield")]
    assert tech and biz

    # the counts the summary reports must be derivable from the report itself,
    # so the UI never invents a figure the detail cannot substantiate
    assert rep["stats"]["technical"] == len(tech)
    assert rep["stats"]["business_rule"] == len(biz)
    passed = lambda s: sum(c["status"] == "pass" for c in s)
    assert passed(tech) + passed(biz) <= rep["stats"]["checks"]


# ---- reconciliation rules as a certified artefact ----
from api import recon_rules as _RR                                     # noqa: E402


def _candidates(tmp_path):
    from engine.insight_cache import get_or_derive
    wh = Warehouse(str(tmp_path / "wh.duckdb"))
    out = flow_mapping_manual.invoke(dict(_multi_state(tmp_path), warehouse=wh))
    spec = apply_decisions(out["spec"], {m.target_attribute: {"action": "accept"}
                                        for m in out["spec"].mappings
                                        if m.gate == "review"}).model_dump(mode="json")
    td = _json.loads(Path(TARGET_DICT).read_text())
    insight = get_or_derive(wh, "data/revised/EFAS0042.csv", "EFAS0042").model_dump(mode="json")
    names = {"EFAS0042": "EFAS0042.csv", "ESCH0009": "ESCH0009.csv"}
    return spec, td, insight, _RR.derive_candidates(spec, td, names, insight)


def test_rules_are_derived_exactly_once(tmp_path):
    """The runner, the preview and the script generator each used to derive the
    rules themselves. Three implementations of one rule is why the driver-code
    decode defect needed three separate fixes. Only recon_rules derives now."""
    import re as _re
    for mod in ("api/reconcile.py",):
        src = Path(mod).read_text()
        assert 'get("dependencies"' not in src, f"{mod} still derives rules itself"
    assert Path("api/recon_rules.py").read_text().count('get("dependencies"') == 1


def test_script_and_run_execute_the_same_certified_rules(tmp_path):
    """The point of the artefact: a script and the results it is meant to
    reproduce cannot diverge, because neither derives anything."""
    from api import transform as _T
    spec, td, insight, cands = _candidates(tmp_path)
    certified, _rej = _RR.certify(
        cands, {"category_profile:gone_away": {"action": "reject"}}, [], td, "tester")

    paths = {"EFAS0042": "data/revised/EFAS0042.csv",
             "ESCH0009": "data/revised/ESCH0009.csv"}
    _c, _r, csv_text, _s = _T.run_transform(spec, paths)
    rep = _R.run_reconciliation(spec, td, csv_text, paths, None, insight, rules=certified)
    code = _R.generate_reconciliation_script(
        spec, "policy.csv", {"EFAS0042": "EFAS0042.csv"}, insight, None, td,
        rules=certified)

    # the excluded control runs in neither. (gone_away still appears in the
    # expected-columns control total, which is correct — it IS a delivered
    # column; what must be absent is its category profile.)
    assert not any(c["name"] == "category_profile:gone_away" for c in rep["checks"])
    assert "'gone_away'" not in code.split("categorical = ")[1].split("\n")[0]
    # every crossfield rule in the set appears in both
    for r in certified:
        if r["kind"] == "crossfield":
            attr = r["params"]["attribute"]
            assert any(attr in c["name"] for c in rep["checks"])
            assert attr in code


def test_a_user_rule_is_the_same_object_as_a_mined_one(tmp_path):
    """No natural-language step, so nothing to mistranslate: a business-authored
    control is structured data that flows through the identical execution path,
    and carries its origin so a reviewer can tell the two apart."""
    from api import transform as _T
    spec, td, insight, cands = _candidates(tmp_path)
    added = [{"kind": "crossfield", "title": "business control",
              "params": {"attribute": "exit_reason", "driver": "policy_status",
                         "values": ["CLOSED"]}}]
    certified, rejected = _RR.certify(cands, {}, added, td, "D. Banerjee")
    assert not rejected

    mine = next(r for r in certified if r["origin"] == "user_added")
    assert mine["certified_by"] == "D. Banerjee"

    paths = {"EFAS0042": "data/revised/EFAS0042.csv",
             "ESCH0009": "data/revised/ESCH0009.csv"}
    _c, _r, csv_text, _s = _T.run_transform(spec, paths)
    rep = _R.run_reconciliation(spec, td, csv_text, paths, None, insight, rules=certified)
    assert any(c["origin"] == "user_added" for c in rep["checks"])


def test_invalid_user_rules_are_rejected_with_a_reason(tmp_path):
    """Structured entry is what makes user rules safe: a rule cannot name a
    column that does not exist or a value outside the declared domain — and a
    rejected control is REPORTED, never silently dropped, because a reviewer
    must see that a control they asked for did not make it in."""
    spec, td, insight, cands = _candidates(tmp_path)
    bad = [
        {"kind": "crossfield", "params": {"attribute": "exit_reason",
                                          "driver": "policy_status",
                                          "values": ["NOT_A_STATUS"]}},
        {"kind": "crossfield", "params": {"attribute": "no_such_column",
                                          "driver": "policy_status",
                                          "values": ["CLOSED"]}},
        {"kind": "sql_injection", "params": {}},
    ]
    certified, rejected = _RR.certify(cands, {}, bad, td, "tester")
    assert len(rejected) == 3
    assert not any(r["origin"] == "user_added" for r in certified)
    reasons = " ".join(r["reason"] for r in rejected)
    assert "not declared values" in reasons
    assert "not a target attribute" in reasons
    assert "unknown rule kind" in reasons


def test_app_and_standalone_script_run_identical_checks(tmp_path):
    """The whole point of a certified rule set: the results a reviewer sees and
    the script they can re-run must be the SAME set of controls. When the two
    executors were written independently the script emitted one check per
    numeric column where the app emitted one aggregate, and omitted the
    distinct-key control entirely — 20 checks against 18 for one rule set."""
    import re as _re, shutil, subprocess, sys as _sys
    from engine.insight_cache import get_or_derive
    from api import transform as _T

    spec, td, insight, cands = _candidates(tmp_path)
    certified, _rej = _RR.certify(cands, {}, [], td, "tester")
    paths = {"EFAS0042": "data/revised/EFAS0042.csv",
             "ESCH0009": "data/revised/ESCH0009.csv"}
    _c, _r, csv_text, _s = _T.run_transform(spec, paths)

    rep = _R.run_reconciliation(spec, td, csv_text, paths, None, insight,
                                rules=certified)
    for f in paths.values():
        shutil.copy(f, tmp_path)
    (tmp_path / "policy.csv").write_text(csv_text)
    (tmp_path / "r.py").write_text(_R.generate_reconciliation_script(
        spec, "policy.csv", {"EFAS0042": "EFAS0042.csv"}, insight, None, td,
        rules=certified))
    proc = subprocess.run([_sys.executable, "r.py"], cwd=tmp_path,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    in_app = {c["name"] for c in rep["checks"]}
    in_script = set(_re.findall(r"\]\s+\S+\s+([\w:~]+):", proc.stdout))
    assert in_app == in_script, (f"only in app: {sorted(in_app - in_script)}; "
                                 f"only in script: {sorted(in_script - in_app)}")


def test_rules_endpoint_supplies_the_vocabulary_the_form_needs():
    """The 'add a control' form must be built from the target dictionary, not
    free text — that is what makes a user-authored control safe without a
    natural-language step. The endpoint therefore returns the attributes and
    their declared domains alongside the proposed rules."""
    from fastapi.testclient import TestClient
    from api import server

    server.STATE = server._empty_state()
    server.CERTIFIED_RULES["rules"] = None
    client = TestClient(server.app)
    for path, role in (("data/revised/EFAS0042.csv", "source"),
                       ("data/revised/dict_EFAS0042.json", "enriched"),
                       (TARGET_DICT, "target")):
        client.post("/api/inputs/upload",
                    files={"files": (Path(path).name, Path(path).read_bytes())},
                    data={"role": role})
    spec = {"mappings": [{"target_attribute": "policy_status",
                          "source_attributes": ["STATCD"], "cardinality": "1:1",
                          "gate": "auto_accept",
                          "transformation_sql": "CASE \"STATCD\" WHEN 'CL' THEN 'CLOSED' END"}],
            "unmapped_target": [], "source_tables": ["EFAS0042"]}

    r = client.post("/api/reconcile/rules", json={"spec": spec}).json()
    assert r["rules"], "no controls proposed"
    assert r["attributes"], "the form has no vocabulary to offer"
    drivers = [a for a in r["attributes"] if a["allowed_values"]]
    assert drivers, "no attribute carries a domain a cross-field rule could use"
    assert all({"name", "type", "allowed_values"} <= set(a) for a in r["attributes"])
    server.STATE = server._empty_state()
    server.CERTIFIED_RULES["rules"] = None


def test_business_can_author_an_aggregate_control(tmp_path):
    """The control a business reviewer actually asks for: 'does the total sum
    assured for each product still agree?'. Expressed as structured data —
    function, column, breakdown — so there is no prose to translate into SQL,
    and it runs through the identical execution path as a mined control."""
    from api import transform as _T
    spec, td, insight, cands = _candidates(tmp_path)
    added = [{"kind": "aggregate_by", "title": "sum assured by product",
              "params": {"function": "sum", "column": "sum_assured",
                         "group_by": ["product_category"]}},
             {"kind": "aggregate_by", "title": "policies by status",
              "params": {"function": "count", "column": None,
                         "group_by": ["policy_status"]}}]
    certified, rejected = _RR.certify(cands, {}, added, td, "D. Banerjee")
    assert not rejected, rejected

    paths = {"EFAS0042": "data/revised/EFAS0042.csv",
             "ESCH0009": "data/revised/ESCH0009.csv"}
    _c, _r, csv_text, _s = _T.run_transform(spec, paths)
    rep = _R.run_reconciliation(spec, td, csv_text, paths, None, insight, rules=certified)

    aggs = [c for c in rep["checks"] if c["category"] == "aggregate_by"]
    assert len(aggs) == 2
    assert all(c["status"] == "pass" for c in aggs), [c["detail"] for c in aggs]
    assert all(c["origin"] == "user_added" for c in aggs)
    # evidence is per bucket, both sides
    sums = next(c for c in aggs if "sum_assured" in c["name"])
    assert sums["sample"] and {"group", "source", "delivered", "ties"} <= set(sums["sample"][0])


def test_aggregate_control_rejects_a_nonsensical_request(tmp_path):
    """Summing a text column, or grouping by a column that does not exist, must
    be refused at certification — the form should not be able to express it, and
    the server must not trust that it didn't."""
    spec, td, insight, cands = _candidates(tmp_path)
    bad = [
        {"kind": "aggregate_by", "params": {"function": "sum",
                                            "column": "policy_status",
                                            "group_by": []}},
        {"kind": "aggregate_by", "params": {"function": "sum",
                                            "column": "sum_assured",
                                            "group_by": ["nope"]}},
        {"kind": "aggregate_by", "params": {"function": "median",
                                            "column": "sum_assured",
                                            "group_by": []}},
    ]
    _certified, rejected = _RR.certify(cands, {}, bad, td, "tester")
    assert len(rejected) == 3
    reasons = " ".join(r["reason"] for r in rejected)
    assert "needs a numeric column" in reasons
    assert "group-by 'nope'" in reasons
    assert "unsupported aggregate" in reasons


def test_every_control_total_carries_its_numbers_as_evidence(tmp_path):
    """A control total stating '50 source rows -> 50 delivered' inside a sentence
    is an assertion; the same figures as source/delivered evidence rows are
    something a reviewer can check. Every control now carries them."""
    rep = _reconciled(tmp_path)
    totals = [c for c in rep["checks"] if c["category"] == "control_total"]
    assert totals
    for c in totals:
        assert c["sample"], f"{c['name']} reports no figures"
        row = c["sample"][0]
        # each evidence row must present at least two comparable sides
        assert len(row) >= 3, row

    fill = next(c for c in totals if c["name"] == "control_total:populated_cells")
    assert len(fill["sample"]) > 1        # one row per delivered column
    assert {"attribute", "populated", "blank", "mandatory"} <= set(fill["sample"][0])
