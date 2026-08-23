"""Render the review queue (Agent 5 output) and, optionally, apply decisions.

The review queue is normally produced by the full pipeline (`python -m cli.run`).
This module provides the markdown renderer and a CLI to re-run with a decisions
file so a resolved exception re-validates.

    python -m cli.run_review --source data/EFAS0042.csv --table EFAS0042 \
        --target-dict data/target_dictionary.json --code data/legacy \
        --decisions decisions.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from engine import config  # noqa: F401
from engine.agents.contracts import ReviewQueue
from engine.orchestration.graph import app


def to_markdown(q: ReviewQueue) -> str:
    L = [f"# {q.source_table} → {q.target_table} — review queue",
         f"_Exception-driven: {q.stats['to_review']} item(s) need a human; "
         f"{q.stats['auto_accepted']} mapping(s) auto-accepted and flowed through. "
         f"Validation verdict: **{q.verdict}**._", ""]
    if not q.items:
        L.append("Nothing to review — all mappings auto-accepted and validated. ✅")
    for it in q.items:
        L += [f"## {it.target_attribute}  ·  _{it.kind}_  ·  gate `{it.gate}`"
              + (f"  ·  conf {it.confidence:.2f}" if it.confidence else ""),
              f"**Why:** {it.reason}"]
        if it.source_attributes:
            srcs = ", ".join(f"{a} ({b})" for a, b in
                             zip(it.source_attributes, it.source_business_names or it.source_attributes))
            L.append(f"**Source:** {srcs}")
        if it.transformation_sql:
            L.append(f"**Transform:** `{it.transformation_sql}`")
        if it.source_decode:
            L.append(f"**Decoded values:** " + ", ".join(f"{k}={v}" for k, v in it.source_decode.items()))
        if it.data_patterns:
            L.append(f"**Data pattern (analyst):** " + "; ".join(it.data_patterns))
        if it.upstream_evidence:
            L.append(f"**Evidence (legacy expert):** " + "; ".join(it.upstream_evidence[:4]))
        if it.validator_exceptions:
            L.append(f"**Validator:** " + "; ".join(it.validator_exceptions))
        if it.offending_rows:
            L.append(f"**Offending rows:** " + "; ".join(str(r) for r in it.offending_rows))
        if it.suggested_resolution:
            L.append(f"**Suggested:** {it.suggested_resolution}")
        L.append(f"**Actions:** {', '.join(it.actions)}")
        L.append("")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--table", default=None)
    ap.add_argument("--target-dict", required=True)
    ap.add_argument("--code", default="data/legacy")
    ap.add_argument("--warehouse", default="data/warehouse.duckdb")
    ap.add_argument("--decisions", default=None, help="JSON file of human decisions")
    args = ap.parse_args()

    src = Path(args.source)
    table = args.table or src.stem
    state = {"source_csv": args.source, "table": table, "code_dir": args.code,
             "target_dict_path": args.target_dict, "warehouse_path": args.warehouse}
    if args.decisions:
        state["decisions"] = json.loads(Path(args.decisions).read_text(encoding="utf-8"))

    final = app.invoke(state)
    q = final["review_queue"]
    (src.parent / f"{table}_review_queue.json").write_text(
        json.dumps(q.model_dump(), indent=2, default=str), encoding="utf-8")
    (src.parent / f"{table}_review_queue.md").write_text(to_markdown(q), encoding="utf-8")
    final["warehouse"].close()
    print(f"review queue -> {table}_review_queue.[json|md] | verdict={final['report'].verdict} | {q.stats}")


if __name__ == "__main__":
    main()
