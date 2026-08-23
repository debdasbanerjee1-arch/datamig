"""Flow A — one-off source understanding.

    stage -> analyst -> legacy expert -> persist knowledge (versioned)

Runs the comprehension agents and persists a fingerprinted knowledge-graph
version. If the inputs are unchanged since an existing version, it reuses that
version and skips the agents entirely (override with --force).

    python -m cli.run_flow_a --source data/EFAS0042.csv --code data/legacy
    python -m cli.run_flow_a ... --certify "A. Sen" --notes "sprint 12 review"
"""
from __future__ import annotations

import argparse

from engine.kgstore import KGStore, collect_inputs, fingerprint_inputs
from engine.orchestration.graph import flow_a


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="source extract CSV")
    ap.add_argument("--code", default="data/legacy",
                    help="folder with COBOL (*.cbl/*.cpy) and screen code")
    ap.add_argument("--warehouse", default="data/warehouse.duckdb")
    ap.add_argument("--kg", default="data/knowledge.duckdb",
                    help="knowledge store path")
    ap.add_argument("--force", action="store_true",
                    help="re-run even if the input fingerprint is unchanged")
    ap.add_argument("--certify", metavar="NAME", default=None,
                    help="SME name — certify the resulting version")
    ap.add_argument("--notes", default="")
    args = ap.parse_args()

    # cheap pre-check: unchanged inputs -> no agent run at all
    inputs = collect_inputs(args.source, args.code)
    fp = fingerprint_inputs(inputs)
    store = KGStore(args.kg)
    existing = store.find_by_fingerprint(fp)
    if existing is not None and not args.force:
        meta = store.meta(existing)
        print(f"inputs unchanged (fingerprint {fp}) -> knowledge v{existing} "
              f"[{meta['status']}] already covers them; nothing to do "
              f"(--force to re-run)")
        version = existing
    else:
        store.close()
        out = flow_a.invoke({"source_csv": args.source, "code_dir": args.code,
                             "warehouse_path": args.warehouse,
                             "kg_path": args.kg, "force": args.force})
        r = out["kg_result"]
        version = r["version"]
        print(f"flow A complete -> knowledge v{version} [{r['status']}] "
              f"fingerprint {r['fingerprint']}")
        if not r.get("reused"):
            print(f"  graph: {r['nodes']} nodes, {r['edges']} edges, "
                  f"{r['rules']} business rules")
        store = KGStore(args.kg)

    if args.certify:
        store.certify(version, args.certify, args.notes)
        print(f"  certified v{version} by {args.certify}")
    store.close()


if __name__ == "__main__":
    main()
