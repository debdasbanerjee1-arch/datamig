"""Derived source-insight cache — one place every consumer reads from.

The Mapping Workspace used to require the user to upload a source insight
(bucket D) purely so validation's key-integrity / crossfield checks could run.
That artifact is derivable from the source CSV alone, so it is derived here
instead — once per (table, file content) — and cached so the mapping run, the
certify pass, and the tab 3 / tab 4 output checks all read the SAME document
rather than each recomputing (or silently skipping their checks).

WHERE IT LIVES: the STAGING warehouse (data/warehouse.duckdb), NOT the
knowledge store. The manual mapping path deliberately bypasses KGStore, whose
versioning/certification semantics don't apply to a derived cache; and the
cache belongs next to the staged table it was computed from. `/api/inputs/reset`
already deletes the warehouse file, so invalidation comes for free.

INVALIDATION: keyed by (table_name, sha256 of the source file). Re-uploading a
changed file under the same name changes the sha, so a stale insight can never
be served — the same fingerprint contract Flow A uses for knowledge versions.

PRECEDENCE (applied by callers, not here): a user-uploaded insight always wins.
This module only answers "what would we derive for this file?".
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .agents.analyst import analyze_light
from .agents.contracts import TableInsight
from .hashing import file_sha256
from .staging import Warehouse

_SCHEMA = """
CREATE TABLE IF NOT EXISTS derived_insight (
    table_name   VARCHAR,
    file_sha     VARCHAR,
    insight_json VARCHAR,
    generated_by VARCHAR,
    created_at   TIMESTAMP,
    PRIMARY KEY (table_name, file_sha)
);
"""


def _ensure_schema(wh: Warehouse) -> None:
    wh.con.execute(_SCHEMA)


def load(wh: Warehouse, table: str, sha: str) -> TableInsight | None:
    """Return the cached insight for this exact file content, or None."""
    _ensure_schema(wh)
    row = wh.con.execute(
        "SELECT insight_json FROM derived_insight "
        "WHERE table_name = ? AND file_sha = ?", [table, sha]).fetchone()
    if not row:
        return None
    try:
        return TableInsight.model_validate_json(row[0])
    except Exception:      # noqa: BLE001 — a corrupt row must not break a run
        return None


def save(wh: Warehouse, table: str, sha: str, insight: TableInsight) -> None:
    _ensure_schema(wh)
    wh.con.execute(
        "INSERT OR REPLACE INTO derived_insight "
        "(table_name, file_sha, insight_json, generated_by, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [table, sha, insight.model_dump_json(), insight.generated_by,
         datetime.now(timezone.utc)])


def get_or_derive(wh: Warehouse, source_path: str,
                  table: str | None = None) -> TableInsight:
    """The single entry point: cached insight for this file, deriving it (and
    caching it) on a miss. Raises only if the source file itself is unreadable.
    """
    table = table or Path(source_path).stem
    sha = file_sha256(source_path)
    cached = load(wh, table, sha)
    if cached is not None:
        return cached
    insight = analyze_light(source_path, table, warehouse=wh)
    save(wh, table, sha, insight)
    return insight


def get_or_derive_doc(warehouse_path: str, source_path: str,
                      table: str | None = None) -> dict | None:
    """Convenience for callers that hold a path rather than a live Warehouse
    (the HTTP layer). Opens, derives, closes. Returns a plain dict — the shape
    /api/validate and /api/reconcile already expect — or None if derivation
    fails, so a missing insight degrades to "checks skipped" exactly as an
    absent upload always did, instead of failing the request.
    """
    wh = None
    try:
        wh = Warehouse(warehouse_path)
        return get_or_derive(wh, source_path, table).model_dump(mode="json")
    except Exception:      # noqa: BLE001
        return None
    finally:
        if wh is not None:
            wh.close()


def summary(insight: TableInsight | dict) -> dict:
    """The small payload the UI's bucket-D preview renders."""
    d = insight if isinstance(insight, dict) else insight.model_dump(mode="json")
    return {"table": d.get("table"),
            "row_count": d.get("row_count"),
            "column_count": d.get("column_count"),
            "candidate_keys": d.get("candidate_keys") or [],
            "dependencies": len(d.get("dependencies") or []),
            "generated_by": d.get("generated_by")}
