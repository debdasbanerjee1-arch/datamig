"""Demo server — one FastAPI process that runs the LangGraph pipeline and streams
each agent's completion (with its artifact) to the browser over SSE, and serves
the single-page UI. Built for an executive demo: live, controlled, no extra stack.

    uvicorn api.server:app --reload      (from the project root)
    then open http://127.0.0.1:8000
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from engine import config  # noqa: F401  -- loads .env so LLM creds apply to the dashboard
from engine.orchestration.graph import (flow_mapping_manual,
                                        flow_mapping_manual_certify)

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web" / "static"
UPLOAD = Path(tempfile.gettempdir()) / "datamap_inputs"
WH_DB = str(UPLOAD / "warehouse.duckdb")     # staging cache + derived-insight store

# the three visible agents of the mapping workspace (the graph also has
# manual_inputs / apply_decisions, which produce no card)
MAPPING_AGENTS = ("mapping", "validation", "review")
AGENTS = {
    "mapping": ("Mapping", "spec"),
    "validation": ("Validation", "report"),
    "review": ("Review", "review_queue"),
}

# how uploaded files are bucketed by extension
SOURCE_EXT = {".csv", ".tsv"}
TARGET_EXT = {".json"}
CODE_EXT = {".cbl", ".cob", ".cpy", ".cobol", ".cobc",
            ".php", ".inc", ".phtml", ".jsp", ".asp", ".html"}


def _empty_state() -> dict:
    for sub in ("source", "target", "code", "enriched"):
        (UPLOAD / sub).mkdir(parents=True, exist_ok=True)
    return {"mode": "empty", "sources": [], "active_source": None,
            "codes": [], "code_dir": str(UPLOAD / "code"),
            "targets": [], "active_target": None,
            # Mapping Workspace manual-mode artefact: the enriched source
            # dictionary (required — Agent 2's shape). The source insight
            # validation needs is NOT collected: it is derived from the source
            # data and cached (engine/insight_cache.py).
            "enricheds": [], "active_enriched": None}


STATE = _empty_state()


def _rebuild_registry() -> None:
    """The upload workspace persists across server restarts, but STATE is
    in-memory. Re-register every file found on disk so the UI always shows
    exactly what staging and fingerprinting will use — no invisible leftovers."""
    for sub, role, bucket, active in (("source", "source", "sources", "active_source"),
                                      ("target", "target", "targets", "active_target"),
                                      ("code", "code", "codes", None),
                                      ("enriched", "enriched", "enricheds", "active_enriched")):
        d = UPLOAD / sub
        if not d.exists():
            continue
        for f in sorted(x for x in d.iterdir() if x.is_file()):
            rec = {"id": uuid.uuid4().hex[:8], "name": f.name, "path": str(f)}
            STATE[bucket].append(rec)
            if active and STATE[active] is None:
                STATE[active] = rec["id"]
    if STATE["sources"] or STATE["codes"] or STATE["targets"] \
            or STATE["enricheds"]:
        STATE["mode"] = "custom"


_rebuild_registry()


def _pick(items: list[dict], active_id: str | None) -> dict | None:
    return next((x for x in items if x["id"] == active_id), items[0] if items else None)


def manual_ready() -> bool:
    """Readiness for the manual Mapping Workspace: at least one source file with
    a matching dictionary, plus the target dictionary. The source insight is
    derived, not uploaded."""
    return bool(STATE["sources"] and STATE["enricheds"] and STATE["targets"])


def inputs_manifest() -> dict:
    keep = lambda xs: [{"id": x["id"], "name": x["name"]} for x in xs]
    return {
        "mode": STATE["mode"], "manual_ready": manual_ready(),
        "sources": keep(STATE["sources"]), "active_source": STATE["active_source"],
        "codes": keep(STATE["codes"]),
        "targets": keep(STATE["targets"]), "active_target": STATE["active_target"],
        "enricheds": keep(STATE["enricheds"]), "active_enriched": STATE["active_enriched"],
    }


# roles that hold at most one MEANINGFUL active file at a time (like target) —
# uploading a new one replaces which file is "the" one the run consumes
# only the target dictionary is single-active now: sources and their
# dictionaries are lists, matched to each other by table name
_SINGLE_ACTIVE = {"target": ("targets", "active_target")}


def _ingest(role: str, name: str, raw_bytes: bytes | None, src_path: Path | None) -> None:
    """Register one file in its bucket. Code is copied into the single code_dir so the
    pipeline can glob it; source/target/enriched are kept (uploaded copies, or
    referenced in place)."""
    if role == "code":
        dest = UPLOAD / "code" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if raw_bytes is not None:
            dest.write_bytes(raw_bytes)
        elif src_path is not None:
            shutil.copyfile(src_path, dest)
        path = dest
    else:  # source / target / enriched
        if raw_bytes is not None:
            dest = UPLOAD / role / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw_bytes)
            path = dest
        else:
            path = src_path  # load-from-path: reference in place
    rec = {"id": uuid.uuid4().hex[:8], "name": name, "path": str(path)}
    bucket = {"source": "sources", "target": "targets", "code": "codes",
             "enriched": "enricheds"}[role]
    STATE[bucket] = [x for x in STATE[bucket] if x["name"] != name]
    if role == "source":
        STATE["sources"].append(rec)
        # active_source is just which file the explorer/single-file run focuses
        # on — the FIRST uploaded. It is NOT the join primary: the driving
        # (finest-grain) file is chosen automatically at mapping time from the
        # discovered relationships, never from the UI.
        if STATE.get("active_source") is None:
            STATE["active_source"] = rec["id"]
    elif role in _SINGLE_ACTIVE:
        b, active_key = _SINGLE_ACTIVE[role]
        STATE[b].append(rec); STATE[active_key] = rec["id"]
    else:
        # enricheds (one dictionary per source file) and codes are plain lists —
        # no single active, because every uploaded dictionary participates
        STATE[bucket].append(rec)
    STATE["mode"] = "custom"


# ---------------------------------------------------------------------------
# Source insight — DERIVED from the source data and cached in the staging
# warehouse. It is not a user input. One resolver so the mapping run, the
# certify pass and the tab 3 / tab 4 output checks all see the SAME document
# instead of each recomputing it (or silently skipping their checks).
# ---------------------------------------------------------------------------
def _insight_for_source(source_path: str | None) -> tuple[dict | None, str]:
    """Return (insight_doc, origin) where origin is 'derived' | 'none'.

    The source insight is never uploaded — it is derived from the source data
    and cached. Derivation is best-effort: on failure the caller degrades to
    "checks skipped" rather than failing the request.
    """
    if not source_path:
        return None, "none"
    from engine.insight_cache import get_or_derive_doc
    doc = get_or_derive_doc(WH_DB, source_path, Path(source_path).stem)
    return (doc, "derived") if doc else (None, "none")


def _active_source_path() -> str | None:
    rec = _pick(STATE["sources"], STATE["active_source"])
    return rec["path"] if rec else None


def _source_bundle() -> tuple[list[dict], list[dict], list[str]]:
    """Pair every uploaded source file with its dictionary.

    Matching is by the dictionary's own `table` field, falling back to the
    source file's stem — so a user uploads N files and N dictionaries in any
    order and the server works out which describes which. Returns
    (sources, dicts, tables_without_a_dictionary); a source with no dictionary
    still participates (it contributes raw column names) rather than being
    silently dropped.
    """
    sources = [{"path": f["path"], "table": Path(f["path"]).stem}
               for f in STATE["sources"]]
    tables = {s["table"] for s in sources}
    dicts, claimed = [], set()
    for f in STATE["enricheds"]:
        try:
            d = json.loads(Path(f["path"]).read_text(encoding="utf-8"))
        except Exception:      # noqa: BLE001 — a bad dictionary shouldn't kill the run
            continue
        t = d.get("table") or Path(f["path"]).stem
        if t not in tables and len(tables) == 1:
            t = next(iter(tables))          # single source: name mismatch is harmless
            d["table"] = t
        dicts.append(d)
        claimed.add(t)
    return sources, dicts, sorted(tables - claimed)


def _classify(ext: str) -> str | None:
    if ext in SOURCE_EXT:
        return "source"
    if ext in TARGET_EXT:
        return "target"
    if ext in CODE_EXT:
        return "code"
    return None


app = FastAPI()


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, default=str)}\n\n"


def _guarded(gen):
    """A stream must never die silently: any exception becomes an error event,
    so the client always receives a terminal event."""
    def _inner():
        try:
            yield from gen
        except Exception as exc:                      # noqa: BLE001
            yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    return _inner()


# ---------------------------------------------------------------------------
# Manual Mapping Workspace — every artefact the mapping agent (and the
# validation it triggers) consumes is uploaded directly by the user instead
# of being computed by Flow A / loaded from the knowledge store:
#   enriched dictionary (required)  · target dictionary (required)
#   source data (required)
# The source insight validation needs is derived from the source data, not
# uploaded — see engine/insight_cache.py.
# ---------------------------------------------------------------------------
_MANUAL_MISSING = ("Load a source file, an enriched source dictionary, "
                   "and a target dictionary first.")


def _run_mapping_manual(decisions: dict | None):
    if not manual_ready():
        yield _sse({"type": "error", "message": _MANUAL_MISSING})
        return
    tgt = _pick(STATE["targets"], STATE["active_target"])
    try:
        sources, dicts, unmatched = _source_bundle()
        target_dict = json.loads(Path(tgt["path"]).read_text(encoding="utf-8"))
    except Exception as exc:                          # noqa: BLE001
        yield _sse({"type": "error",
                    "message": f"Couldn't parse an uploaded artefact: {type(exc).__name__}: {exc}"})
        return
    names = ", ".join(s["table"] for s in sources)
    labels = {
        "mapping": [f"Source data: {names}"
                    + (" (joined — relationships discovered from the data)"
                       if len(sources) > 1 else ""),
                    f"Source dictionaries: {len(dicts)} (one per file)",
                    f"Target dictionary: {tgt['name']}"]
                   + ([f"No dictionary for: {', '.join(unmatched)}"] if unmatched else []),
        "validation": ["Mapping specification", "Source data (transforms executed)",
                       f"Source insight: derived from {names}"],
        "review": ["Mapping spec + validation report"],
    }
    yield _sse({"type": "start", "inputs": labels,
                "agents": [{"id": k, "label": AGENTS[k][0]} for k in MAPPING_AGENTS]})

    # WH_DB (not :memory:) so the derived insight cached by the mapping run is
    # still there for the certify pass and for tabs 3/4 — recomputing it per
    # request would be wasteful and could drift from what validation just used.
    init = {"sources": sources, "enriched_dicts": dicts,
            "warehouse_path": WH_DB, "target_dict": target_dict}
    if decisions:
        init["decisions"] = decisions

    merged, final_wh = {}, None
    for update in flow_mapping_manual.stream(init, stream_mode="updates"):
        for node, partial in update.items():
            merged.update(partial)
            if node == "manual_inputs":
                final_wh = partial.get("warehouse")
            if node in MAPPING_AGENTS:
                label, key = AGENTS[node]
                artifact = merged.get(key)
                yield _sse({"type": "node", "node": node, "label": label,
                            "input": labels.get(node, []),
                            "artifact": artifact.model_dump() if artifact else {}})
    if "spec" not in merged or "report" not in merged or "review_queue" not in merged:
        if final_wh is not None:
            final_wh.close()
        yield _sse({"type": "error",
                    "message": "Mapping did not complete — check the uploaded artefacts and try again."})
        return
    summary = {"verdict": merged["report"].verdict, "mapping_stats": merged["spec"].stats,
               "review_stats": merged["review_queue"].stats}
    yield _sse({"type": "complete", "summary": summary,
                "final_spec": merged["spec"].model_dump(),
                "source_table": merged["spec"].source_table})
    if final_wh is not None:
        final_wh.close()


def _run_mapping_manual_certify(spec_in: dict, decisions: dict | None):
    """Apply the reviewer's decisions to the spec they reviewed WITHOUT
    re-running the mapping agent — same contract as /api/flow_b/certify,
    minus the knowledge-store lookup."""
    if not manual_ready():
        yield _sse({"type": "error", "message": _MANUAL_MISSING})
        return
    if not isinstance(spec_in, dict) or not spec_in.get("mappings"):
        yield _sse({"type": "error", "message": "No mapping to certify — run the mapping first."})
        return
    tgt = _pick(STATE["targets"], STATE["active_target"])
    try:
        sources, dicts, _unmatched = _source_bundle()
        target_dict = json.loads(Path(tgt["path"]).read_text(encoding="utf-8"))
    except Exception as exc:                          # noqa: BLE001
        yield _sse({"type": "error",
                    "message": f"Couldn't parse an uploaded artefact: {type(exc).__name__}: {exc}"})
        return
    labels = {
        "validation": ["Mapping specification", "Source data (transforms executed)"],
        "review": ["Mapping spec + validation report"],
    }
    yield _sse({"type": "start", "inputs": labels,
                "agents": [{"id": k, "label": AGENTS[k][0]} for k in ("validation", "review")]})

    init = {"sources": sources, "enriched_dicts": dicts,
            "warehouse_path": WH_DB, "target_dict": target_dict,
            "spec_in": spec_in, "decisions": decisions or {}}

    merged, final_wh = {}, None
    for update in flow_mapping_manual_certify.stream(init, stream_mode="updates"):
        for node, partial in update.items():
            merged.update(partial)
            if node == "manual_seed_spec":
                final_wh = partial.get("warehouse")
            if node in ("validation", "review"):
                label, key = AGENTS[node]
                artifact = merged.get(key)
                yield _sse({"type": "node", "node": node, "label": label,
                            "input": labels.get(node, []),
                            "artifact": artifact.model_dump() if artifact else {}})
    if "spec" not in merged or "report" not in merged or "review_queue" not in merged:
        if final_wh is not None:
            final_wh.close()
        yield _sse({"type": "error",
                    "message": "Certification did not complete — the decided mapping "
                               "could not be validated. Check the server log and try again."})
        return
    summary = {"verdict": merged["report"].verdict, "mapping_stats": merged["spec"].stats,
               "review_stats": merged["review_queue"].stats}
    yield _sse({"type": "complete", "summary": summary,
                "final_spec": merged["spec"].model_dump(),
                "source_table": merged["spec"].source_table})
    if final_wh is not None:
        final_wh.close()


@app.post("/api/mapping/run")
async def run_mapping_manual_ep(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    return StreamingResponse(_guarded(_run_mapping_manual(body.get("decisions") or None)),
                             media_type="text/event-stream")


@app.post("/api/mapping/certify")
async def certify_mapping_manual_ep(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    return StreamingResponse(
        _guarded(_run_mapping_manual_certify(body.get("spec") or {}, body.get("decisions") or None)),
        media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Tab 4 · Transformation workspace (additive)
# Turns a certified mapping spec into an executable ETL and runs it against the
# loaded source file(s). Reads inputs only — never mutates Flow A / Flow B state.
# ---------------------------------------------------------------------------
def _source_path_for(table: str) -> str | None:
    """Resolve a spec source_table name (a CSV stem like 'EFAS0042') to the
    path of a currently-loaded source file."""
    for x in STATE["sources"]:
        if Path(x["path"]).stem == table:
            return x["path"]
    return None


def _source_name_for(table: str) -> str:
    for x in STATE["sources"]:
        if Path(x["path"]).stem == table:
            return x["name"]
    return f"{table}.csv"


def _spec_from_body(body: dict) -> dict | None:
    """The client sends the certified spec it already holds (state.finalSpec).
    Kept server-stateless: tab 4 works off whatever tab 3 produced."""
    spec = body.get("spec")
    return spec if isinstance(spec, dict) and spec.get("mappings") else None


@app.post("/api/transform/codegen")
async def transform_codegen(request: Request):
    from api import transform as T

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    spec = _spec_from_body(body)
    if not spec:
        return {"error": "No mapping specification provided. Certify a mapping on tab 3 first."}
    tables = spec.get("source_tables") or (
        [spec["source_table"]] if spec.get("source_table") not in (None, "__workset") else []
    )
    filenames = {t: _source_name_for(t) for t in tables}
    paths = {t: _source_path_for(t) for t in tables if _source_path_for(t)}
    code = T.generate_python(spec, filenames, paths or None)
    return {"language": "python", "code": code,
            "target_table": spec.get("target_table"),
            "source_tables": tables,
            "missing_files": [t for t in tables if t not in paths]}


@app.post("/api/transform/run")
async def transform_run(request: Request):
    from api import transform as T

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    spec = _spec_from_body(body)
    if not spec:
        return {"error": "No mapping specification provided. Certify a mapping on tab 3 first."}
    paths = T.resolve_source_paths(spec, _source_path_for)
    tables = spec.get("source_tables") or (
        [spec["source_table"]] if spec.get("source_table") not in (None, "__workset") else []
    )
    missing = [t for t in tables if t not in paths]
    if missing or not paths:
        return {"error": "Source file(s) not loaded: "
                + (", ".join(missing) or "none found")
                + ". Add them on tab 1 (or the input feed above)."}
    limit = int(body.get("preview_limit") or 200)
    try:
        columns, rows, csv_text, stats = T.run_transform(spec, paths, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"columns": columns, "rows": rows, "stats": stats,
            "csv": csv_text, "target_table": spec.get("target_table")}


# ---------------------------------------------------------------------------
# Tab 5(new)/3 · Validation workspace & Tab 4 · Reconciliation workspace
# Both check the DELIVERED transformed output (the CSV tab 4/transform already
# produced) against the certified spec — server-stateless like tab 4: the
# client posts the spec + the CSV it already holds, this reads the currently
# loaded target dictionary / enriched dictionary / source file(s), and the
# insight derived from the source data.
# ---------------------------------------------------------------------------
def _active_json(bucket: str) -> dict | None:
    rec = _pick(STATE[bucket], STATE.get("active_" + bucket[:-1]))
    if not rec:
        return None
    try:
        return json.loads(Path(rec["path"]).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _csv_from_body(body: dict) -> str | None:
    csv_text = body.get("csv")
    return csv_text if isinstance(csv_text, str) and csv_text.strip() else None


@app.post("/api/validate/codegen")
async def validate_codegen(request: Request):
    from api import validate as V

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    spec = _spec_from_body(body)
    if not spec:
        return {"error": "No mapping specification provided. Certify a mapping on tab 1 first."}
    target_dict = _active_json("targets")
    if not target_dict:
        return {"error": "No target dictionary loaded. Add one on tab 1."}
    tables = spec.get("source_tables") or (
        [spec["source_table"]] if spec.get("source_table") not in (None, "__workset") else []
    )
    has_source = any(_source_path_for(t) for t in tables)
    src_path = next((_source_path_for(t) for t in tables if _source_path_for(t)), None)
    insight, _origin = _insight_for_source(src_path or _active_source_path())
    has_insight = bool(insight)
    code = V.generate_validation_script(spec, target_dict,
                                        f"{spec.get('target_table','target')}.csv",
                                        has_source, has_insight)
    rules = V.describe_validation_rules(spec, target_dict, has_source, has_insight)
    return {"language": "python", "code": code, "rules": rules, "target_table": spec.get("target_table")}


@app.post("/api/validate/run")
async def validate_run(request: Request):
    from api import validate as V

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    spec = _spec_from_body(body)
    if not spec:
        return {"error": "No mapping specification provided. Certify a mapping on tab 1 first."}
    csv_text = _csv_from_body(body)
    if not csv_text:
        return {"error": "No transformed output to validate. Run the transformation on tab 2 first."}
    target_dict = _active_json("targets")
    if not target_dict:
        return {"error": "No target dictionary loaded. Add one on tab 1."}
    from api import transform as T

    paths = T.resolve_source_paths(spec, _source_path_for)
    insight, insight_origin = _insight_for_source(
        next(iter(paths.values()), None) or _active_source_path())
    try:
        report = V.run_output_validation(spec, target_dict, csv_text, paths, insight)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    return report | {"insight_origin": insight_origin}


# ---------------------------------------------------------------------------
# Reconciliation rules — proposed, certified, then executed
# ---------------------------------------------------------------------------
# Reconciliation was the one workspace with no certification gate: rules were
# derived, run and discarded, leaving no answer to "which controls did we sign
# off, and who signed them?". The certified set is now held for the session and
# is what BOTH the script generator and the runner consume.
CERTIFIED_RULES: dict = {"rules": None}


def _recon_context(spec: dict):
    tables = spec.get("source_tables") or [spec.get("source_table")]
    filenames = {t: _source_name_for(t) for t in tables if _source_path_for(t)}
    src_path = next((_source_path_for(t) for t in tables if _source_path_for(t)), None)
    insight, _o = _insight_for_source(src_path or _active_source_path())
    target_dict = _active_json("targets") or {}
    return filenames, insight, target_dict


@app.post("/api/reconcile/rules")
async def reconcile_rules(request: Request):
    """Propose the rule set for review. Never executes anything."""
    from api import recon_rules as RR

    body = await request.json()
    spec = body.get("spec") or {}
    if not spec.get("mappings"):
        return {"error": "No certified mapping — run and certify a mapping first."}
    filenames, insight, target_dict = _recon_context(spec)
    candidates = RR.derive_candidates(spec, target_dict, filenames, insight)
    # a semantic pass over the proposals: flags controls that cannot inform
    # anyone, and proposes business controls the data cannot suggest for itself.
    # Neither is applied — both are offered to the reviewer.
    candidates = RR.llm_review_rules(candidates, target_dict)
    # the target vocabulary, so the "add a control" form can only offer real
    # attributes and their declared values — this is what makes a user-authored
    # rule safe without any natural-language step
    attrs = [{"name": a["name"], "type": a.get("type", "string"),
              "allowed_values": a.get("allowed_values") or [],
              "nullable": bool(a.get("nullable", False))}
             for a in (target_dict.get("attributes") or []) if isinstance(a, dict)]
    # Proposing INVALIDATES any earlier certification. CERTIFIED_RULES is
    # process-global and survived across runs, so pressing "Propose controls"
    # handed back a certification from a previous session: the panel came up
    # already certified, every control greyed out, and the reviewer could not
    # deselect or add anything. A certification also cannot honestly outlive the
    # rule set it was given — these candidates were just re-derived, so nobody
    # has approved them yet.
    CERTIFIED_RULES["rules"] = None
    return {"rules": [dict(r, id=RR.rule_id(r)) for r in candidates],
            "attributes": attrs,
            "certified": []}


@app.post("/api/reconcile/certify")
async def reconcile_certify(request: Request):
    """Apply the reviewer's decisions and any hand-authored controls.

    A user rule is the SAME structured object as a mined one — no natural
    language, nothing to translate into SQL — and is validated against the
    target dictionary, so it cannot name a column that does not exist or a
    value outside a declared domain.
    """
    from api import recon_rules as RR

    body = await request.json()
    spec = body.get("spec") or {}
    if not spec.get("mappings"):
        return {"error": "No certified mapping — run and certify a mapping first."}
    filenames, insight, target_dict = _recon_context(spec)
    candidates = RR.derive_candidates(spec, target_dict, filenames, insight)
    certified, rejected = RR.certify(
        candidates, body.get("decisions") or {}, body.get("added") or [],
        target_dict, body.get("certified_by") or "reviewer")
    CERTIFIED_RULES["rules"] = certified
    return {"certified": [dict(r, id=RR.rule_id(r)) for r in certified],
            "rejected": rejected,
            "counts": {"total": len(certified),
                       "user_added": sum(1 for r in certified
                                         if r.get("origin") == "user_added"),
                       "rejected": len(rejected)}}


@app.post("/api/reconcile/codegen")
async def reconcile_codegen(request: Request):
    from api import reconcile as R

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    spec = _spec_from_body(body)
    if not spec:
        return {"error": "No mapping specification provided. Certify a mapping on tab 1 first."}
    tables = spec.get("source_tables") or (
        [spec["source_table"]] if spec.get("source_table") not in (None, "__workset") else []
    )
    filenames = {t: _source_name_for(t) for t in tables if _source_path_for(t)}
    target_dict = _active_json("targets") or {}
    src_path = next((_source_path_for(t) for t in tables if _source_path_for(t)), None)
    insight, _origin = _insight_for_source(src_path or _active_source_path())
    enriched = _active_json("enricheds")
    certified = CERTIFIED_RULES["rules"]
    code = R.generate_reconciliation_script(spec, f"{spec.get('target_table','target')}.csv",
                                            filenames, insight, enriched, target_dict,
                                            rules=certified)
    rules = R.describe_reconciliation_rules(spec, filenames, insight, enriched,
                                            target_dict, rules=certified)
    return {"language": "python", "code": code, "rules": rules, "target_table": spec.get("target_table")}


@app.post("/api/reconcile/run")
async def reconcile_run(request: Request):
    from api import reconcile as R

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    spec = _spec_from_body(body)
    if not spec:
        return {"error": "No mapping specification provided. Certify a mapping on tab 1 first."}
    csv_text = _csv_from_body(body)
    if not csv_text:
        return {"error": "No transformed output to reconcile. Run the transformation on tab 2 first."}
    target_dict = _active_json("targets") or {}
    from api import transform as T

    paths = T.resolve_source_paths(spec, _source_path_for)
    insight, insight_origin = _insight_for_source(
        next(iter(paths.values()), None) or _active_source_path())
    enriched = _active_json("enricheds")
    try:
        report = R.run_reconciliation(spec, target_dict, csv_text, paths, enriched,
                                      insight, rules=CERTIFIED_RULES["rules"])
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    return report | {"insight_origin": insight_origin}


@app.get("/api/llm/check")
def llm_check_ep():
    from engine.llm_check import diagnose
    steps = diagnose()
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


@app.get("/api/mode")
def mode():
    return {"live": config.llm_ready(), "label": config.llm_label()}


@app.get("/api/raw")
def raw(id: str):
    """File content by registry id — what the input chips' "view" button opens.

    The old `which=source|target|cobol|php` mode served the retired source-
    understanding tab's raw viewer; nothing calls it now, so the endpoint is
    id-only and the code/target-path resolution it needed is gone with it.
    """
    for bucket in ("sources", "codes", "targets", "enricheds"):
        for f in STATE[bucket]:
            if f["id"] == id:
                p = Path(f["path"])
                return {"name": f["name"],
                        "content": p.read_text(encoding="utf-8", errors="replace")
                                   if p.exists() else "not found"}
    return {"name": "", "content": "not found"}


@app.get("/api/inputs")
def get_inputs():
    return inputs_manifest()


@app.post("/api/inputs/upload")
async def upload_inputs(files: list[UploadFile] = File(...), role: str | None = Form(None)):
    # `.json` alone can't disambiguate target from enriched-dictionary —
    # a widget that already knows which artefact it's collecting passes `role`
    # explicitly, overriding extension-based classification for this batch.
    # An explicitly-supplied role that is NOT recognised is IGNORED, never
    # silently reclassified by extension: a stale client posting the retired
    # "insight" role would otherwise have its .json land in the target bucket
    # and quietly replace the active target dictionary.
    added = {"source": 0, "code": 0, "target": 0, "enriched": 0, "ignored": []}
    valid_roles = {"source", "code", "target", "enriched"}
    for f in files:
        if role is not None and role not in valid_roles:
            added["ignored"].append(f.filename)
            continue
        r = role if role in valid_roles else _classify(Path(f.filename or "").suffix.lower())
        if r is None:
            added["ignored"].append(f.filename)
            continue
        _ingest(r, Path(f.filename).name, await f.read(), None)
        added[r] += 1
    return inputs_manifest() | {"added": added}


@app.post("/api/inputs/remove")
async def remove_input(request: Request):
    b = await request.json()
    role, _id = b.get("role"), b.get("id")
    key = {"source": "sources", "code": "codes", "target": "targets",
          "enriched": "enricheds"}.get(role)
    if key:
        # delete from disk too: staging and fingerprinting glob the directory,
        # so a registry-only removal would silently keep influencing runs
        for x in STATE[key]:
            if x["id"] == _id:
                Path(x["path"]).unlink(missing_ok=True)
        STATE[key] = [x for x in STATE[key] if x["id"] != _id]
        if role == "source" and STATE["active_source"] == _id:
            STATE["active_source"] = STATE["sources"][0]["id"] if STATE["sources"] else None
        active_key = {"target": "active_target",
                     "enriched": "active_enriched"}.get(role)
        if active_key and STATE[active_key] == _id:
            STATE[active_key] = STATE[key][0]["id"] if STATE[key] else None
    return inputs_manifest()


@app.post("/api/inputs/reset")
def reset_inputs():
    global STATE
    for sub in ("source", "target", "code", "enriched"):
        d = UPLOAD / sub
        if d.exists():
            shutil.rmtree(d)
    Path(WH_DB).unlink(missing_ok=True)
    STATE = _empty_state()
    return inputs_manifest()


_ASSET_RE = re.compile(r'(?P<attr>(?:href|src)=")(?P<path>/static/[\w./-]+?)'
                       r'(?:\?v=[^"]*)?"')


@app.get("/")
def index():
    """Serve index.html with asset versions derived from FILE CONTENT.

    The version strings used to be hand-edited (`app.js?v=53`), which meant a
    change to app.js shipped with a stale version and every returning browser
    kept running the cached previous build — silently, and indistinguishably
    from the code not working. A content hash cannot be forgotten: edit the
    asset and the URL changes.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    def stamp(m: re.Match) -> str:
        rel = m.group("path")[len("/static/"):]
        f = STATIC / rel
        try:
            digest = hashlib.sha256(f.read_bytes()).hexdigest()[:10]
        except OSError:
            return m.group(0)
        return f'{m.group("attr")}{m.group("path")}?v={digest}"'

    return HTMLResponse(_ASSET_RE.sub(stamp, html),
                        headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
