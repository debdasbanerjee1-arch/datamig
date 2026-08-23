"""Run the whole pipeline as one orchestrated LangGraph.

    python -m cli.run --source data/EFAS0042.csv --table EFAS0042 \
        --target-dict data/target_dictionary.json --code data/legacy

Writes all four artifacts (inferred + enriched dictionaries, mapping spec,
validation report) and prints the final verdict.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from engine import config  # noqa: F401  (loads .env)
from engine.orchestration.graph import app
from .run_analyst import to_markdown as analyst_md
from .run_legacy_expert import to_markdown as legacy_md
from .run_mapping import to_markdown as mapping_md
from .run_review import to_markdown as review_md
from .run_validate import to_markdown as validate_md


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
    final = app.invoke({
        "source_csv": args.source, "table": table, "code_dir": args.code,
        "target_dict_path": args.target_dict, "warehouse_path": args.warehouse,
    })

    out = src.parent
    for stem, obj, md in [
        ("inferred_dictionary", final["insight"], analyst_md(final["insight"])),
        ("enriched_dictionary", final["enriched"], legacy_md(final["enriched"])),
        ("mapping_spec", final["spec"], mapping_md(final["spec"])),
        ("validation_report", final["report"], validate_md(final["report"])),
        ("review_queue", final["review_queue"], review_md(final["review_queue"])),
    ]:
        (out / f"{table}_{stem}.json").write_text(
            json.dumps(obj.model_dump(), indent=2, default=str), encoding="utf-8")
        (out / f"{table}_{stem}.md").write_text(md, encoding="utf-8")

    final["warehouse"].close()
    r = final["report"]
    print(f"pipeline complete -> 5 artifacts written for {table}")
    print(f"  mapping: {final['spec'].stats}")
    print(f"  validation verdict = {r.verdict} | {r.stats}")
    print(f"  review queue: {final['review_queue'].stats}")


if __name__ == "__main__":
    main()
