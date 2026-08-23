# datamap — Agentic Source-to-Target Data Mapping

An agentic pipeline that turns an opaque legacy extract (a life & pensions policy
master) into a validated, audited target mapping — with a real-time demo UI.

## The honesty contract (hostile-code behaviour)

The demo COBOL contains NO narrative comments — every extracted business rule
is discovered from code structure alone (dataflow slicing, 88-level condition
names, screen labels, copybook alignment). `data/hostile/NBCOMM74.cbl` is a
deliberately realistic-ugly fixture (SECTIONs, GO TO flow, PERFORM ... THRU,
period-terminated IFs, multi-record REDEFINES, OCCURS/SEARCH, EXEC SQL/CICS,
STRING/INSPECT, COPY REPLACING). The tested contract on such code: never
crash, never be confidently wrong — the slice is still found, parse coverage
drops (~60% on the fixture), confidence is capped, and the rule ships flagged
for LLM assist + SME review instead of shipping a guess.

To see it in the UI: on tab 1 remove the demo source/code chips (× on each),
add `data/hostile/NBEXTRACT.csv` + `data/hostile/NBCOMM74.cbl`, and run
Flow A — a new knowledge version appears whose rule card reads "parse 62%"
with confidence capped at 0.6. Both estates coexist as separate versions in
the store (Reset wipes the store; the chip × buttons do not).

## Two-flow architecture

The pipeline is split around a PERSISTED, versioned knowledge graph
(`data/knowledge.duckdb` — the certified asset; the staging warehouse is just
a cache):

**Flow A — source understanding (one-off, re-run on change only)**

    stage -> analyst -> legacy expert -> persist knowledge

    python -m cli.run_flow_a --source data/EFAS0042.csv --code data/legacy

Inputs are fingerprinted (SHA-256 per file); unchanged inputs reuse the
existing version and skip the agents. Output: knowledge vN (status `draft`)
holding the graph (nodes/edges with provenance), REIFIED business rules
(resolved text + COBOL evidence + structured decision tables + input roles),
and the serialized insight/dictionary artifacts.

**Certification gate (human)**

    python -m cli.kg certify 1 --by "Name" --notes "sign-off"
    python -m cli.kg list | show | export | lineage

Certifying a version supersedes the previously certified one. Export writes a
committable JSON of the whole graph.

**Demo UI** — the dashboard (`uvicorn api.server:app`) now mirrors the split:
tab 1 runs Flow A and shows the knowledge banner (version, fingerprint,
certify); tab 2 is the knowledge explorer (rules with COBOL evidence,
provenance, lineage queries); tab 3 runs Flow B against the chosen version.
Re-running Flow A with unchanged inputs replays instantly from the store.

**Tab 4 — transformation workspace (Flow C)** turns the mapping certified on
tab 3 into an executable ETL and runs it end to end. It shows the input feed
(source file(s), target dictionary, mapping spec), generates a Python
(pandas + DuckDB) script from the certified spec, executes it against the loaded
source file(s) — rebuilding the joined workset per the spec's `join_plan` and
keeping the primary file's row grain — then renders the materialised target
dataset in a grid with a CSV export. The transform runs the spec's own DuckDB
SQL (`strptime` / `TRY_CAST` / `CASE` / `NULLIF`) verbatim, so tab 4's output is
consistent with what tab 3 certified. Endpoints: `POST /api/transform/codegen`
and `POST /api/transform/run` (both stateless — they consume the spec the client
already holds and read the current input files only).

**Certifying decisions (Flow B) — apply, don't re-map.** When a reviewer resolves
the queue and certifies, the client posts the exact spec it reviewed plus the
decisions to `POST /api/flow_b/certify`. That path runs
`load_kg → load_target → seed_spec → apply_decisions → validation → review`: it
adopts the reviewed spec verbatim and applies the human choices on top, then
re-validates — it does **not** re-run the mapping agent. This keeps decisions
honest (they apply to what was on screen) and avoids regenerating a mapping the
human already signed off. Unmapped targets can be resolved with a load-time
default (a type-appropriate value is suggested; the reviewer accepts or edits it),
which is promoted into a concrete mapping by `apply_decisions`.

**Flow B — per-target mapping (repeatable)**

    load knowledge -> mapping -> validation -> review

    python -m cli.run_flow_b --target-dict data/target_dict/<your-file>.json

Consumes the latest certified version by default (draft with a warning
otherwise) and stamps `kg_version` / `kg_fingerprint` / `kg_status` on the
mapping spec, so every mapping is traceable to the exact knowledge it came
from. The combined single-run pipeline (`python -m cli.run`) still works.

Five agents run as a LangGraph pipeline:

1. **Data Analyst** — profiles the data, scans PII, and runs an EXECUTABLE
   data-quality rule library (UK NI number format, UK postcode grammar,
   calendar-valid dates, discovered cross-date invariants, conditional
   completeness from mined dependencies, key uniqueness). Every rule carries
   its violation-count SQL; `python -m cli.run_analyst` writes the whole
   library as `<table>_dq_rules.sql` for full-volume reruns during cleansing.
2. **Legacy Expert** — decodes business meaning from COBOL + screen, and *extracts
   calculation logic* for batch-derived fields (e.g. loyalty bonus, early-exit
   penalty computed by `BONCALC.cbl`) that appear on no screen — the COBOL
   procedure code is the only documentation. Deterministic extraction finds the
   `COMPUTE`/assignment paragraphs via BACKWARD SLICING (fixed point over the
   working-storage chain, any depth), reads 88-level condition names as free
   business vocabulary and value decodes, handles GIVING arithmetic and
   reference modification, and reports a statement-coverage metric per rule —
   when the parser understood under 80% of a slice, confidence drops and the
   rule is flagged for LLM assist + SME review. The LLM translates the sliced
   logic into plain-English business rules.
3. **Mapping** — aligns source to target, builds transforms, gates by confidence.
   Mappings whose source is itself a calculated field are always held at
   *review* — the pass-through validates, but a human must confirm the extracted
   calculation matches the target attribute's definition.
4. **Validation** — materialises the target and checks the whole spec.
5. **Review** — surfaces only what needs a human, with full lineage.

**Derived source insight (mapping workspace).** The manual Mapping Workspace
needs a `TableInsight` for validation's key-integrity and crossfield checks, but
does not require the user to supply one. `engine/agents/analyst.py:analyze_light`
derives exactly the fields those checks consume — `candidate_keys` and
`dependencies` — reusing the same `_profile` / `_dependencies` helpers the full
analyst uses, so there is one definition of "candidate key" and one of
"populated", never two that can drift. It deliberately skips the DQ rule
library, the PII scan and both LLM calls, which is where `analyze()`'s cost
lives. The result is cached in the STAGING warehouse (`derived_insight`, keyed
by table + SHA-256 of the source file) so the mapping run, the certify pass and
the tab 3 / tab 4 output checks all read the same document; `engine/insight_cache.py`
is the single entry point. A user-uploaded insight always wins — bucket D on
tab 1 is an override, not a required input. Cache invalidation is by file
content, and `/api/inputs/reset` deletes the warehouse, so a stale insight can
never be served. Note this path is still O(rows) in Python (`fetch_dicts`);
scaling it means reimplementing profiling as DuckDB aggregate SQL.

**Multi-source relationship discovery.** When more than one source file is
loaded, a relationship-discovery step (`engine/agents/relationship.py`) finds
how the staged tables relate purely from the data in DuckDB — which columns
share values, which of those are join keys (one side unique), which are
merely a shared code vocabulary — never from name matching alone (a name
match only nominates a pair for value testing). `engine/composite.py` then
picks a primary (driving) table and LEFT JOINs every other file that has a
safe N:1/1:1 path to it, so the primary's row grain never fans out; files
without a safe path are excluded and reported, not silently joined. The
result is one combined workset (a DuckDB view + merged dictionary) that the
mapping, validation and review agents consume unchanged. The discovered
relationships are persisted per input fingerprint and served at
`GET /api/relationships` for the UI's relationship view.

## Architecture

The project is layered so the brains are independent of how they're delivered.

```
datamap/
  engine/                # the domain core — no HTTP / UI knowledge
    agents/              #   the five agents + relationship discovery + shared contracts
    orchestration/       #   LangGraph: graph.py, nodes.py, state.py
    staging.py           #   DuckDB staging layer (Warehouse)
    composite.py         #   multi-source join planning -> one workset
    kgraph.py            #   in-memory knowledge graph + resolver (used by legacy expert)
    kgstore.py           #   PERSISTED, versioned knowledge graph (data/knowledge.duckdb)
    models.py            #   gating model (Gate, decide_gate)
    config.py            #   env / LLM client (.env loader)
  api/                   # delivery: FastAPI HTTP surface (JSON + SSE)
    server.py
    transform.py         #   Flow C: spec -> Python (pandas/DuckDB) codegen + run
  web/                   # delivery: the frontend client (static SPA)
    static/              #   index.html, styles.css, app.js
  cli/                   # delivery: command-line entrypoints
    run.py, run_*.py
  data/                  # synthetic source, legacy code, target dictionary
  tests/                 # pytest suite
```

**Dependency rule:** `api/`, `web/`, and `cli/` depend on `engine/`; `engine/`
depends on none of them. The engine is independently testable and reusable; any
number of front doors (the API, the CLI, a future React frontend) sit on top.

**Simplified per-file dictionaries + multi-source mapping (v9).** Bucket A and
bucket C both take several files; each dictionary names its file in `table`, so
upload order is irrelevant. The dictionary is four fields per column
(`name`, `business_name`, `description`, `inferred_type`) plus optional
`value_decode`, `aliases` and `join_key`. The COBOL/screen provenance fields and
the seven `derivation_*` fields are gone from the declared schema
(`extra='allow'` keeps legacy-expert output round-tripping). `value_decode`
stays deliberately: it is business knowledge, not legacy metadata, and enum
matching, the auto-accept gate and the decode SQL all depend on it.

Relationships between files are discovered from the DATA
(`agents/relationship.py` — a name match only nominates a column pair for a
value-containment test), the finest-grain file becomes the primary, and safe
grain-preserving edges are LEFT JOINed into one workset
(`composite.build_workset`). The mapping agent therefore needs no multi-file
logic: it sees one wide table, and `origin_table` on every combined column is
what lets each mapping report the file it came from. Every mapping element
carries `source_files`, which survives certification and is shown in the UI.

**Script fidelity.** The generated scripts are the reproducibility claim — "run
this and you get what we got" — which only holds if the SQL in the script is a
byte-faithful copy of the certified spec. It was not: the transform generator
embedded the expression in a normal Python string literal, so any backslash
escape Python recognised was reinterpreted. A certified `'\t'` reached DuckDB as
a literal tab, and the handed-over script silently executed different SQL from
the one the workspace validated. Now emitted as a raw literal, with a test
asserting every certified expression appears verbatim in the script.

The generators embed the certified expression and never parse it, so they are
agnostic to the transformation PATTERN: window functions, regex, nested CASE and
date arithmetic — none of which the deterministic synthesiser can produce —
generate and run correctly. Unfamiliar patterns fail at SYNTHESIS, not at
generation, which is why the escalation tier belongs at `_synth`.

**Derivation gaps — the transform, not the match.** The deterministic
synthesiser has a fixed SINGLE-COLUMN repertoire: enum decode, date parse,
numeric cast, trim, copy. It cannot compose. So a target needing concatenation,
unit conversion, arithmetic across columns or reformatting silently received a
copy of the best-matching single column — and certified clean, because every
downstream check can confirm a value is well-formed but not that it is the value
the target asked for. Observed on a composite fixture: `full_name` <- SURNAME
(forename dropped), `annual_premium_gbp` <- PREM_PENCE (out by 100x-1200x),
`age_at_commencement` <- COMMDT (a date where a count of years belongs) — all
three certified, and the age even passed the data-type check because 20190220
parses as an integer.

`_derivation_gap` detects this from the artefacts rather than a rule list, by
comparing what the TARGET says it needs against what the SQL actually
references: (a) the target description names another source column's business
vocabulary, (b) target and source carry conflicting members of a unit/scale
family (pence vs gbp, monthly vs annual), (c) the description contains an
explicit format instruction. Evidence must be DISTINCTIVE — a token shared
across the dictionary ('scheme', 'policy') proves nothing and falsely flagged
six correct mappings before that constraint was added. A gap caps the gate at
REVIEW and the confidence at 0.60, outranking every other cap.

`_llm_synthesise` is the escalation tier for gapped mappings: given the target
type, description, source samples and the stated gap, the LLM proposes composite
SQL. It is not trusted, it is TESTED — the proposal must reference only real
columns, must not be a statement, must EXECUTE against staged data, and must not
lose rows against what it replaces. It is capped at REVIEW and never
auto-accepts. A failing proposal is discarded and the deterministic mapping
stands, so the tier can only improve on what was there. Offline, nothing
changes.

**Matcher correctness.** Type compatibility is no longer allowed to carry a
mapping on its own. Two string columns are type-compatible with every string
target, so without a floor ~20 candidates tied at exactly 0.4 and the winner was
decided by column order — which is how `POLNO` came to feed both
`tax_file_number` and `investor_id` while `NINO` and `CUSTID` were reported as
"no target attribute". The same principle was already applied to boolean and
enum targets; plain strings were the gap. Matches that are not lexical at all
are recovered through authored `aliases` rather than a synonym table buried in
the matcher.

**Reconciliation is a four-step workflow**, mirroring the mapping workspace's
propose → review → certify shape, which it previously lacked:

    1. Rule Generation   controls derived from the certified mapping, the target
                         dictionary and the source insight (api/recon_rules.py)
    2. Human Review      a reviewer deselects what does not apply and may add the
                         business's own controls
    3. Script Generation the standalone script, built from the CERTIFIED set
    4. Reconciliation    executed against the delivered file, from the same set

Step 1 used to happen silently when the tab rendered, so the workflow appeared
to begin at review and the derivation was invisible. It is now an explicit
action. Steps 3 and 4 are gated on certification by DISABLING their actions, not
by hiding the panels — hiding removed the steps from view entirely and made the
tool look as though it had none.

Business-authored controls are AGGREGATES — sum / average / min / max / count /
distinct count of a column, optionally broken down by categorical columns, e.g.
"total sum assured for each product reconciles". Chosen entirely from the target
dictionary, so an invalid control cannot be expressed: there is no prose, and
therefore nothing to translate into SQL. Cross-field conditions are mined from
the data already, so asking a human to re-key them added nothing.

**Reconciliation workspace.** Three families, and the count matters: a check
that cannot fail, or that a stronger check already covers, costs the reader
attention and earns nothing. *Control totals* are the figures a migration
control sheet carries — rows source vs delivered, columns against the certified
spec, populated cells out of total, distinct business keys, and the total of
every numeric column. *Category counts* group record counts by value on both
sides for every low-cardinality attribute carrying a transform, so "how many in
force, how many exited, how many per product" falls out of the data rather than
a configured rule. *Cross-field rules* enforce the conditional-population
patterns mined from the source.

Three families were removed rather than kept for volume. `value_loss` was an
aggregate populated-count comparison, strictly subsumed by validation's
transform check, which re-executes every certified transform value by value and
names the offending record — weaker and noisier. `aggregate` sums stopped being
a check for the same reason (if a value moved, the cell-level check caught it)
and became a reported control total, because a reviewer still expects a money
column to tie out on the face of the report. `derivation` read a field removed
when the dictionary was simplified and could never fire. Row count moved into
control totals: it was the one check name literally duplicated between the two
workspaces, and a test now asserts the two share none.

Cross-field rules are mined in SOURCE terms (`STATCD in {CL}`) but evaluated
against delivered data holding the TRANSFORMED value (`CLOSED`). Driver codes
are therefore translated through the driver's own certified transform before
comparison — without that, all five rules failed 100% on correct data.

All four workspaces now share the same furniture: an inputs panel naming what
feeds them (with view links), a two-station agent rail (rule generation →
execution), a generated script, and results.

**Validation workspace.** Four panels: the inputs it is validating against
(certified spec, delivered output, target dictionary, source files — mirroring
the transformation workspace), the generated script, the rules that script
checks, and the results.

*Rules are described per FAMILY, not per column.* Completeness and domain are
one rule each applied across N attributes, with the affected attribute list
carried on the rule so it stays auditable. A rule-per-column preview was 17
cards on a 23-attribute target and would be 40+ on a realistic one, all saying
the same thing.

*Results are a table, one row per target attribute*, columns for the check
families that can apply to an attribute (completeness, domain, key integrity).
Table-level checks (wellformed, grain) sit above it, since they belong to no
attribute. Filter by issues / untested, search by name, download as CSV. Every
declared attribute gets a row even when no test applies to it — that
no-silent-skips invariant is what lets the table be read as coverage, without
a self-reported percentage to argue about.

*A failure names the offending records.* Every failing check carries a sample
of what actually broke, keyed by the mapped business key so a reviewer can find
the record in the source system — for a transform failure that is
`policy_reference | expected | delivered`, a genuine row-level diff, not just a
count. Samples render as a table and download as CSV. Row numbers are file
positions, not positions within the filtered set (`row_number() OVER ()` applied
after a `WHERE` numbers the survivors, so the first offender always read "row 1").
Because rows are aligned by ordinal position, a row-count mismatch suspends the
transform comparison entirely rather than cascading one extra row into a dozen
spurious column failures that bury the real defect.

*Every executed check carries the SQL it ran and the population it scanned.*
A green tick is a claim; a green tick with its SQL and row count is evidence.
Cells expand on click (lazily — a 50-attribute table would otherwise build 150
evidence blocks it never shows) to the SQL, the violation and scan counts, and
sample offending rows. The same SQL appears in the downloadable script, so the
results and the script are reconcilable.

## What the demo UI exposes vs. what the engine holds

The shipped UI is four tabs — mapping, transformation, validation,
reconciliation — all driven by `flow_mapping_manual`. The earlier
source-understanding and knowledge-explorer tabs were retired, and their
delivery-layer code (15 HTTP endpoints, their SSE generators, the per-agent
input-label builder, the `which=` mode of `/api/raw`, and the analyst /
legacy-expert cards in the frontend) has been **deleted** rather than left
unreachable.

The ENGINE behind those tabs was deliberately kept: `analyst.analyze`,
`legacy_expert`, `kgraph`, `kgstore`, `composite` and `relationship` are still
present, still exercised by the test suite, and still reachable through the CLI
(`cli/run_flow_a.py`, `cli/run_flow_b.py`, `cli/kg.py`). That is the COBOL
comprehension and certification story; it is not dead code just because one UI
stopped calling it.

Two helpers moved out of modules they didn't belong to, so the live path no
longer imports the retired one: `_llm_json` (LLM JSON repair + retry) went from
`agents/legacy_expert.py` to `engine/llmjson.py`, and `file_sha256` from
`kgstore.py` to `engine/hashing.py`. Both are re-exported from their old homes,
so existing imports keep working.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Demo UI (one process: serves the SPA + streams the pipeline)
python -m uvicorn api.server:app --port 8000
# open http://127.0.0.1:8000

# CLI: full pipeline
python -m cli.run --source data/EFAS0042.csv --target-dict data/target_dict/<your-file>.json --code data/legacy

# Render the pipeline graph (Mermaid)
python -m engine.orchestration.graph

# Tests
python -m pytest -q
```

## LLM mode (optional)

The pipeline runs deterministically offline by default. To enable live LLM
reasoning (PII analysis, narration), copy `.env.example` to `.env` and set either
a standard OpenAI key (`OPENAI_API_KEY`) or the Azure OpenAI variables. The UI
pill shows which provider is active.
