"""Run Agent 2 (legacy-system expert): enrich the analyst's dictionary using code.

    python -m cli.run_legacy_expert --source data/EFAS0042.csv --table EFAS0042 \
        --code data/legacy

Uses the analyst's <table>_inferred_dictionary.json if present (else runs Agent 1
first), reads COBOL (*.cbl, *.cpy) and PHP (*.php) from --code, and writes
<table>_enriched_dictionary.json and .md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from engine import config  # noqa: F401  (loads .env)
from engine.agents.analyst import analyze
from engine.agents.contracts import TableInsight
from engine.agents.legacy_expert import EnrichedDictionary, enrich
from engine.staging import Warehouse


def to_markdown(e: EnrichedDictionary) -> str:
    lines = [f"# {e.table} — enriched data dictionary",
             f"_Agent 2 (legacy-system expert, {e.generated_by}): business meaning inferred from "
             "the COBOL program and PHP screen, on top of the analyst's data findings._",
             "", "## Columns", "",
             "| Column | Business name | COBOL field | Screen label | Decoded values | Conf. | Evidence |",
             "|--------|---------------|-------------|--------------|----------------|-------|----------|"]
    for c in e.columns:
        dec = "; ".join(f"{k}={v}" for k, v in c.value_decode.items()) or "—"
        lines.append(
            f"| {c.name} | **{c.business_name}** | {c.cobol_name or '—'} "
            f"| {c.screen_label or '—'} | {dec} | {c.confidence:.2f} | {'; '.join(c.sources)} |"
        )
    if e.rules:
        lines += ["", "## Business rules (data pattern → decoded meaning)"]
        lines += [f"- {r}" for r in e.rules]
    calc = [c for c in e.columns if c.derivation]
    if calc:
        lines += ["", "## Calculated fields — logic recovered from COBOL"]
        for c in calc:
            lin = c.derivation_lineage or {}
            inputs = ", ".join(f"`{f}` ({col})" if col else f"`{f}`"
                               for f, col in (lin.get("inputs") or {}).items())
            lines += [f"### {c.name} — {c.business_name} (computed by `{c.derived_in_program}`)",
                      "", c.derivation_narrative or c.derivation_resolved or c.derivation or "", ""]
            if c.derivation_narrative and c.derivation_resolved:
                lines += ["<details><summary>Auditable resolution (knowledge graph)</summary>",
                          "", c.derivation_resolved, "", "</details>", ""]
            if lin:
                lines += [f"**Lineage:** inputs {inputs} — record `{lin.get('record')}` "
                          f"(COPY `{lin.get('copybook')}`), file `{lin.get('file')}` = "
                          f"dataset `{lin.get('dataset')}`, written back by `{lin.get('program')}`.", ""]
            lines += ["<details><summary>COBOL evidence</summary>", "",
                      "```cobol", c.derivation_cobol or "", "```", "</details>", ""]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--table", default=None)
    ap.add_argument("--code", default="data/legacy", help="folder with *.cbl / *.cpy and *.php")
    args = ap.parse_args()

    src = Path(args.source)
    table = args.table or src.stem

    # Agent 1 input: reuse its JSON if present, else run it now.
    j = src.parent / f"{table}_inferred_dictionary.json"
    if j.exists():
        insight = TableInsight(**json.loads(j.read_text(encoding="utf-8")))
    else:
        insight = analyze(args.source, table, warehouse=Warehouse(":memory:"))

    code = Path(args.code)
    cobol_text = "\n".join(p.read_text(encoding="utf-8") for pat in ("*.cbl", "*.cob", "*.cpy")
                          for p in sorted(code.glob(pat)))
    php_text = "\n".join(p.read_text(encoding="utf-8") for p in code.glob("*.php"))

    enriched = enrich(insight, cobol_text, php_text)
    (src.parent / f"{table}_enriched_dictionary.json").write_text(
        json.dumps(enriched.model_dump(), indent=2, default=str), encoding="utf-8")
    (src.parent / f"{table}_enriched_dictionary.md").write_text(to_markdown(enriched), encoding="utf-8")
    print(f"legacy-expert done ({enriched.generated_by}) -> {table}_enriched_dictionary.[json|md]")
    hi = sum(1 for c in enriched.columns if c.confidence >= 0.9)
    print(f"  {len(enriched.columns)} columns enriched; {hi} at high confidence; "
          f"{len(enriched.rules)} business rule(s) decoded.")


if __name__ == "__main__":
    main()
