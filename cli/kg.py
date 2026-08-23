"""Manage the persisted knowledge store.

    python -m cli.kg list
    python -m cli.kg show 2
    python -m cli.kg certify 2 --by "A. Sen" --notes "sprint 12 sign-off"
    python -m cli.kg export 2 --out data/knowledge_v2.json
    python -m cli.kg lineage 2 col:XA06
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from engine.kgstore import KGStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["list", "show", "certify", "export", "lineage"])
    ap.add_argument("version", nargs="?", type=int, default=None)
    ap.add_argument("node", nargs="?", default=None, help="node id for lineage")
    ap.add_argument("--kg", default="data/knowledge.duckdb")
    ap.add_argument("--by", default=None, help="SME name for certify")
    ap.add_argument("--notes", default="")
    ap.add_argument("--out", default=None, help="output path for export")
    args = ap.parse_args()

    store = KGStore(args.kg)
    try:
        if args.command == "list":
            for m in store.versions():
                cert = f" by {m['certified_by']}" if m["certified_by"] else ""
                if (m.get("notes") or "").startswith("degraded"):
                    cert += " · DEGRADED"
                print(f"v{m['version']}  [{m['status']}{cert}]  "
                      f"fp={m['fingerprint']}  table={m['source_table']}  "
                      f"created={m['created_at']}")
            if not store.versions():
                print("knowledge store is empty — run Flow A first")

        elif args.command == "show":
            v = args.version or store.latest()
            doc = store.export_json(v)
            kinds: dict = {}
            for n in doc["nodes"]:
                kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
            print(f"knowledge v{v} [{doc['meta']['status']}] "
                  f"fp={doc['meta']['fingerprint']}")
            print(f"  inputs: {', '.join(i['name'] for i in doc['inputs'])}")
            print(f"  nodes: {sum(kinds.values())} ({json.dumps(kinds)})")
            print(f"  edges: {len(doc['edges'])}")
            for r in doc["rules"]:
                print(f"  rule {r['id']} [{r['status']}, {r['provenance']}] "
                      f"-> {r['target_column']}, inputs "
                      f"{sorted(r['inputs'])}")

        elif args.command == "certify":
            if args.version is None or not args.by:
                raise SystemExit("usage: certify VERSION --by NAME")
            store.certify(args.version, args.by, args.notes)
            print(f"certified v{args.version} by {args.by}")

        elif args.command == "export":
            v = args.version or store.latest()
            doc = store.export_json(v)
            out = Path(args.out or f"data/knowledge_v{v}.json")
            out.write_text(json.dumps(doc, indent=2, default=str))
            print(f"exported knowledge v{v} -> {out}")

        elif args.command == "lineage":
            if args.version is None or not args.node:
                raise SystemExit("usage: lineage VERSION NODE_ID (e.g. col:XA06)")
            for e in store.lineage(args.version, args.node):
                print(f"  {e['src']} -[{e['kind']}]-> {e['dst']}"
                      + (f"  {json.dumps(e['props'])}" if e["props"] else ""))
    finally:
        store.close()


if __name__ == "__main__":
    main()
