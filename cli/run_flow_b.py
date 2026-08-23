"""Flow B — per-target mapping from PERSISTED knowledge.

    load knowledge -> mapping -> validation -> review

Consumes the latest CERTIFIED knowledge version by default (falls back to the
latest draft with a warning). Never re-runs comprehension.

    python -m cli.run_flow_b --target-dict data/target_dictionary.json
    python -m cli.run_flow_b ... --kg-version 2 --source data/EFAS0042.csv
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from engine.kgstore import KGStore
from engine.orchestration.graph import flow_b


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target-dict", required=True)
    ap.add_argument("--kg", default="data/knowledge.duckdb")
    ap.add_argument("--kg-version", type=int, default=None,
                    help="knowledge version (default: latest certified)")
    ap.add_argument("--warehouse", default="data/warehouse.duckdb")
    ap.add_argument("--source", default=None,
                    help="source CSV — only needed to restage if the "
                         "warehouse cache was cleared")
    ap.add_argument("--out", default="data")
    args = ap.parse_args()

    store = KGStore(args.kg)
    v = args.kg_version or store.latest(certified_only=True)
    if v is None:
        v = store.latest()
        if v is not None:
            print(f"WARNING: no certified knowledge — using draft v{v}. "
                  f"Certify with: python -m cli.kg certify {v} --by NAME")
    meta = store.meta(v) if v is not None else None
    store.close()
    if meta is None:
        raise SystemExit("knowledge store is empty — run Flow A first")

    state = {"kg_path": args.kg, "kg_version": v,
             "warehouse_path": args.warehouse,
             "target_dict_path": args.target_dict}
    if args.source:
        state["source_csv"] = args.source
    out = flow_b.invoke(state)

    spec, report, queue = out["spec"], out["report"], out["review_queue"]
    table = spec.source_table
    outdir = Path(args.out)
    (outdir / f"{table}_mapping_spec.json").write_text(spec.model_dump_json(indent=2))
    (outdir / f"{table}_validation_report.json").write_text(report.model_dump_json(indent=2))
    (outdir / f"{table}_review_queue.json").write_text(queue.model_dump_json(indent=2))

    print(f"flow B complete <- knowledge v{spec.kg_version} [{spec.kg_status}] "
          f"fingerprint {spec.kg_fingerprint}")
    print(f"  mapping: {json.dumps(spec.stats)}")
    print(f"  validation: {report.verdict}")
    print(f"  review queue: {json.dumps(queue.stats)}")


if __name__ == "__main__":
    main()
