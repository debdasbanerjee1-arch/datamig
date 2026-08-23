"""Run Agent 4 (validation agent): validate the mapping spec holistically.

    python -m cli.run_validate --source data/EFAS0042.csv --table EFAS0042 \
        --target-dict data/target_dictionary.json --code data/legacy

Chains Agents 1->2->3->4 and writes <table>_validation_report.[json|md].
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from engine import config  # noqa: F401
from engine.agents.analyst import analyze
from engine.agents.contracts import ValidationReport
from engine.agents.legacy_expert import enrich
from engine.agents.mapping_agent import map_to_target
from engine.agents.validator import validate_spec
from engine.staging import Warehouse

VERDICT_ICON = {"certified": "✅ CERTIFIED", "needs_review": "⚠️ NEEDS REVIEW", "blocked": "⛔ BLOCKED"}


def to_markdown(r: ValidationReport) -> str:
    L = [f"# {r.source_table} → {r.target_table} — validation report",
         f"**Verdict: {VERDICT_ICON.get(r.verdict, r.verdict)}** "
         f"({r.generated_by})", "",
         f"{r.stats['passed']} passed, {r.stats['warnings']} warning(s), "
         f"{r.stats['failures']} failure(s); {r.stats['gate_demotions']} gate demotion(s).",
         "", "## Checks", "",
         "| Check | Category | Status | Detail |",
         "|-------|----------|--------|--------|"]
    icon = {"pass": "pass", "warn": "⚠ warn", "fail": "✗ FAIL"}
    for c in r.checks:
        L.append(f"| {c.name} | {c.category} | {icon.get(c.status, c.status)} | {c.detail} |")
    flagged = [c for c in r.checks if c.status != "pass" and c.sample]
    if flagged:
        L += ["", "## Offending rows (samples)"]
        for c in flagged:
            L.append(f"- **{c.name}** ({c.offending_rows} row(s)): "
                     + "; ".join(str(s) for s in c.sample))
    if r.gate_adjustments:
        L += ["", "## Gate demotions"]
        L += [f"- **{g.target_attribute}**: {g.from_gate} → {g.to_gate} ({g.reason})"
              for g in r.gate_adjustments]
    if r.stats.get("llm_summary"):
        L += ["", "## Reviewer summary"]
        L += [f"- {s}" for s in r.stats["llm_summary"]]
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
    wh.stage_csv(args.source, table, all_varchar=True)

    insight = analyze(args.source, table, warehouse=wh)
    code = Path(args.code)
    cobol = "\n".join(p.read_text(encoding="utf-8") for p in code.glob("*.cbl"))
    php = "\n".join(p.read_text(encoding="utf-8") for p in code.glob("*.php"))
    enriched = enrich(insight, cobol, php)
    target_dict = json.loads(Path(args.target_dict).read_text(encoding="utf-8"))
    spec = map_to_target(enriched, target_dict, wh, table)

    report = validate_spec(spec, target_dict, insight, wh, table)
    (src.parent / f"{table}_validation_report.json").write_text(
        json.dumps(report.model_dump(), indent=2, default=str), encoding="utf-8")
    (src.parent / f"{table}_validation_report.md").write_text(to_markdown(report), encoding="utf-8")
    print(f"validation done ({report.generated_by}) -> {table}_validation_report.[json|md]")
    print(f"  verdict={report.verdict} | {report.stats}")
    wh.close()


if __name__ == "__main__":
    main()
