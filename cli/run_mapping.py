"""Run Agent 3 (data mapping agent): produce the source->target mapping spec.

    python -m cli.run_mapping --source data/EFAS0042.csv --table EFAS0042 \
        --target-dict data/target_dictionary.json --code data/legacy

Chains Agents 1->2->3 if their outputs aren't already present, stages the source
into the warehouse for validation, and writes <table>_mapping_spec.json and .md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from engine import config  # noqa: F401
from engine.agents.analyst import analyze
from engine.agents.contracts import TableInsight
from engine.agents.legacy_expert import EnrichedDictionary, enrich
from engine.agents.mapping_agent import MappingSpec, map_to_target
from engine.staging import Warehouse


def to_markdown(s: MappingSpec) -> str:
    L = [f"# {s.source_table} → {s.target_table} — data mapping specification",
         f"_Agent 3 (data mapping agent, {s.generated_by}). Confidence is earned: each "
         "transformation was executed on the real source data and scored on target-domain coverage._",
         "", "## Stats",
         f"- {s.stats['mapped']} of {s.stats['target_attributes']} target attributes mapped "
         f"({s.stats['auto_accept']} auto-accept, {s.stats['review']} review, {s.stats['reject']} reject).",
         f"- {s.stats['unmapped_target']} target attribute(s) with no source; "
         f"{s.stats['unmapped_source']} source attribute(s) not migrated.",
         "", "## Mappings", "",
         "| Target | Source | Card. | Transformation | Cov. | Conf. | Gate |",
         "|--------|--------|-------|----------------|------|-------|------|"]
    for m in s.mappings:
        cov = "—" if m.validation_coverage is None else f"{m.validation_coverage:.0%}"
        L.append(f"| {m.target_attribute} | {', '.join(m.source_attributes)} | {m.cardinality} "
                 f"| `{m.transformation_sql}` | {cov} | {m.confidence:.2f} | {m.gate} |")
    L += ["", "### Rationale / review notes"]
    for m in s.mappings:
        if m.gate != "auto_accept" or m.unmapped_codes:
            L.append(f"- **{m.target_attribute}** ({m.gate}): {m.rationale}")
    if s.unmapped_target:
        L += ["", "## Target attributes with no source"]
        L += [f"- **{u['attribute']}** — {u['reason']}" for u in s.unmapped_target]
    if s.unmapped_source:
        L += ["", "## Source attributes not migrated"]
        L += [f"- **{u['attribute']}** ({u['business_name']}) — {u['reason']}" for u in s.unmapped_source]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--table", default=None)
    ap.add_argument("--target-dict", required=True)
    ap.add_argument("--code", default="data/legacy")
    ap.add_argument("--warehouse", default="data/warehouse.duckdb")
    args = ap.parse_args()

    src = Path(args.source)
    table = args.table or src.stem
    wh = Warehouse(args.warehouse)
    wh.stage_csv(args.source, table, all_varchar=True)   # stage source for validation

    # Agent 1
    j1 = src.parent / f"{table}_inferred_dictionary.json"
    insight = (TableInsight(**json.loads(j1.read_text(encoding="utf-8"))) if j1.exists()
               else analyze(args.source, table, warehouse=wh))
    # Agent 2
    code = Path(args.code)
    cobol = "\n".join(p.read_text(encoding="utf-8") for p in code.glob("*.cbl"))
    php = "\n".join(p.read_text(encoding="utf-8") for p in code.glob("*.php"))
    enriched = enrich(insight, cobol, php)
    # Agent 3
    target_dict = json.loads(Path(args.target_dict).read_text(encoding="utf-8"))
    spec = map_to_target(enriched, target_dict, wh, table)

    (src.parent / f"{table}_mapping_spec.json").write_text(
        json.dumps(spec.model_dump(), indent=2, default=str), encoding="utf-8")
    (src.parent / f"{table}_mapping_spec.md").write_text(to_markdown(spec), encoding="utf-8")
    print(f"mapping done ({spec.generated_by}) -> {table}_mapping_spec.[json|md]")
    print(f"  {spec.stats}")
    wh.close()


if __name__ == "__main__":
    main()
