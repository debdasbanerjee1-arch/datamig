# datamap — agentic source-to-target data mapping

Turns an opaque legacy extract — a life & pensions policy master — into a
**certified mapping**, an **executable ETL**, and the **evidence** a migration
assurance reviewer needs, across four workspaces.

Everything runs **offline and deterministically**. The LLM is an escalation tier
for cases deterministic logic cannot reach; with no API key configured the whole
pipeline, including all three script generators, works unchanged.

---

## The four workspaces

    1 · Mapping         source data + dictionaries  ->  certified mapping spec
    2 · Transformation  certified spec              ->  ETL script + target data
    3 · Validation      target data                 ->  is it right?
    4 · Reconciliation  target data vs source       ->  do the totals agree?

Each has the same shape: **an inputs panel** naming what feeds it, **an agent
rail** showing the steps, **a generated script**, and **results with evidence**.

### 1 · Mapping workspace

Three input buckets, each accepting several files:

| | | |
|---|---|---|
| **A** | Source dictionaries | one JSON per source file |
| **B** | Target dictionary | the target definition |
| **C** | Source data | one or more CSVs — joins are discovered |

    manual_inputs -> mapping -> validation -> review [-> apply_decisions]

The mapping agent scores candidates deterministically, **earns** confidence by
executing each proposed transform against staged data, and routes every mapping
to one of three gates: `auto_accept`, `review`, `reject`. The reviewer resolves
only the exceptions, then certifies. Certification applies decisions to *the spec
that was reviewed* — it never re-runs the mapping agent, so a decision cannot be
applied to a spec the reviewer never saw.

### 2 · Transformation workspace

Compiles the certified spec into a standalone Python (pandas + DuckDB) script and
executes it, rebuilding the joined workset from the spec's `join_plan`.

### 3 · Validation workspace

Asks whether the delivered data is **right**. Eight check families, and the
headline is not how much work was done but what it found:

> **CERTIFIED** — Nothing failed. 1,150 values re-derived from source and
> compared, plus 41 rule checks across data type, completeness, domain, key,
> duplicates, grain, wellformed.

### 4 · Reconciliation workspace

Asks whether the delivered data **agrees with the source**. Four steps:

    1. Rule Generation    controls derived from the spec, dictionary and insight
    2. Human Review       a reviewer deselects what doesn't apply, adds their own
    3. Script Generation  the standalone script, built from the CERTIFIED set
    4. Reconciliation     executed, from that same set

---

## Design principles

These are the rules the code actually follows. Each was learned from a defect.

### Deterministic first, LLM as escalation

The deterministic tier does the work: DuckDB scoring, value-decode matching, join
discovery by value containment. The LLM escalates only where deterministic logic
cannot reach — cross-vocabulary matches, and composite transforms the synthesiser
cannot express. It is never trusted, only **tested**: a proposal must reference
real columns, be an expression rather than a statement, execute against staged
data, and not lose rows. It is capped at `review` and never auto-accepts. A
failing proposal is discarded and the deterministic mapping stands, so the tier
can only improve on what was there.

### Never confidently wrong

Type compatibility cannot carry a mapping on its own. Two string columns are
type-compatible with every string target, so without a floor ~20 candidates tied
at exactly 0.4 and the winner was decided by **column order** — which is how
`POLNO` came to feed both `tax_file_number` and `investor_id` while `NINO` and
`CUSTID` were reported as "no target attribute". Matches that are not lexical at
all are recovered through authored `aliases` in the dictionary, not a synonym
table buried in the matcher.

### Derivation gaps — the transform, not the match

The synthesiser has a fixed **single-column** repertoire: enum decode, date
parse, numeric cast, trim, copy. It cannot compose. So a target needing
concatenation, unit conversion or date arithmetic silently received a copy of one
column — and certified clean, because every downstream check can confirm a value
is well-formed but not that it is the value the target asked for. Observed:
`full_name` ← SURNAME (forename dropped), `annual_premium_gbp` ← PREM_PENCE (out
by 100×–1200×), `age_at_commencement` ← COMMDT — all three certified, the age
even passing the data-type check because `20190220` parses as an integer.

`_derivation_gap` detects this by comparing what the **target says it needs**
against what the **SQL actually references**: (a) the description names another
column's business vocabulary, (b) target and source carry conflicting members of
a unit family (pence vs gbp, monthly vs annual), (c) the description contains an
explicit format instruction. Evidence must be **distinctive** — a token shared
across the dictionary ("scheme", "policy") proves nothing, and matching on shared
vocabulary falsely flagged six correct mappings before that constraint existed.

### No silent skips

Every attribute the target dictionary declares produces a row in the validation
report — an executed assertion, or an explicit `skipped` row stating why none
applies. Previously a nullable non-enum attribute produced no rows at all, so
"not examined" and "examined and clean" were indistinguishable. `skipped` never
affects the verdict and is never counted as a pass.

### Evidence, not assertion

Every executed check records **the SQL it ran** and the population it scanned. A
green tick is a claim; a green tick with its SQL and row count is evidence. Every
failing check names the offending records — for a transform failure that is
`policy_reference | expected | delivered`, a genuine row-level diff. Row numbers
are file positions, not positions within the filtered set (`row_number() OVER ()`
applied after a `WHERE` numbers the survivors, so the first offender always read
"row 1").

Because rows are aligned by ordinal position, a row-count mismatch **suspends**
the transform comparison rather than cascading one extra row into a dozen
spurious column failures that bury the real defect.

### One derivation, one artefact

Reconciliation rules were once derived in **four** places — the runner, the
preview, the script generator, and again inside the generator for categorical
attributes. Four implementations of one rule is why a single defect (driver codes
compared in *source* terms against *delivered* values, so `policy_status NOT IN
('CL')` was true for every row) needed four separate fixes.

Rules are now an artefact, derived once in `api/recon_rules.py`:

    derive_candidates() -> human certifies / adds -> certified set
                                                       |          |
                                              script generation   execution

Both consumers read the same set, so a script and the results it is meant to
reproduce cannot diverge. A test asserts the app and the standalone script run
**identical check names**. Every rule carries its origin (`mined` /
`llm_proposed` / `user_added`) and who certified it — reconciliation previously
had no certification gate at all, leaving no answer to "which controls did we
sign off, and who signed them?".

### Structured rules, never prose

A business-authored control is the same object as a mined one, arriving with
`origin: "user_added"`. It is an **aggregate** — sum / average / min / max /
count / distinct count of a column, optionally broken down by categorical columns
("total sum assured for each product reconciles") — chosen entirely from the
target dictionary. An invalid control **cannot be expressed**, there is no
natural-language step, and therefore nothing to mistranslate into SQL. Invalid
requests are rejected with a reason and displayed, never silently dropped.

### Scripts are reproducible by construction

No LLM touches script generation. The generated script is the audit artefact:
"run this yourself and you get what we got" only holds if it is a deterministic
function of the certified spec. Generating the same scripts five times produces
byte-identical output.

The SQL is emitted as a **raw** literal — embedding it in a normal Python string
meant a certified `'\t'` reached DuckDB as a literal tab, so the handed-over
script silently executed different SQL from the one the workspace validated.

The generators embed the certified expression and never parse it, so they are
agnostic to the transformation *pattern*: window functions, regex, nested CASE
and date arithmetic all generate and run correctly. Unfamiliar patterns fail at
**synthesis**, not generation — which is why the escalation tier belongs there.

---

## Architecture

    engine/                 the domain core — depends on nothing else
      agents/               plain functions, graph-agnostic
        analyst.py          profiling, candidate keys, conditional dependencies
        mapping_agent.py    scoring, SQL synthesis, gates, LLM escalation
        validator.py        in-pipeline validation of a proposed spec
        reviewer.py         exception queue, apply_decisions
        relationship.py     join discovery by value containment
        legacy_expert.py    COBOL comprehension (see below)
        contracts.py        shared shapes — no agent imports another agent
      orchestration/        LangGraph nodes + graphs (thin adapters)
      composite.py          N sources + N dictionaries -> one workset
      insight_cache.py      derived source insight, cached by file content hash
      staging.py            DuckDB warehouse

    api/                    HTTP surface
      server.py             endpoints + SSE streaming
      transform.py          ETL generation + execution
      validate.py           output validation
      recon_rules.py        reconciliation rules as a certified artefact
      reconcile.py          reconciliation execution + script generation

    web/static/             single-page UI (vanilla JS, no build step)
    cli/                    a second front door onto the same engine
    tests/                  98 tests (4 skipped — need live LLM)

**Dependency rule:** `api/`, `web/` and `cli/` depend on `engine/`; `engine/`
depends on none of them.

Two guards exist because their failure modes are invisible. `tests/test_frontend_scope.py`
walks every top-level IIFE in `app.js` and fails if a helper is used outside the
scope that defines it — a cross-scope reference only surfaces when a user reaches
that tab and clicks. And asset URLs are stamped with a **content hash** at serve
time: hand-maintained `?v=` strings meant an edited `app.js` shipped with a stale
version and every returning browser kept running the cached previous build,
indistinguishable from the fix not working.

### The source dictionary

Four fields per column, plus three optional:

```json
{
  "name": "NINO",
  "business_name": "NI Number",
  "description": "The National Insurance number.",
  "inferred_type": "FREE_TEXT",
  "aliases": ["National Insurance number", "tax identifier"],
  "value_decode": { "CL": "Claimed" },
  "join_key": true
}
```

The COBOL/screen provenance fields and the seven `derivation_*` fields were
removed when the comprehension pipeline stopped running — asking a human to
author them produced noise. `value_decode` **stays**, and is the field people
mistake for legacy metadata: enum targets may only match on coded evidence, the
auto-accept gate depends on every code having a target equivalent, and the decode
SQL is generated from it. It is business knowledge — an analyst knows `CL` means
Claimed. `aliases` is where domain knowledge lives that no similarity metric
recovers: `NINO` wins `tax_file_number` only because of them.

### What the UI exposes vs. what the engine holds

The UI drives `flow_mapping_manual`. The earlier source-understanding and
knowledge-explorer tabs were retired and their delivery-layer code **deleted**
rather than left unreachable — 15 endpoints, their SSE generators, and the
frontend cards.

The **engine** behind them was deliberately kept: `analyst.analyze`,
`legacy_expert`, `kgraph`, `kgstore`, `composite` and `relationship` are still
present, still exercised by the test suite, and still reachable through the CLI.
That is the COBOL comprehension and certification story — dataflow slicing,
88-level condition names, parse-coverage caps that force SME review instead of a
confident guess — and it is not dead code merely because one UI stopped calling
it.

---

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Demo UI — serves the SPA and streams the pipeline
python -m uvicorn api.server:app --port 8000
# open http://127.0.0.1:8000

# Tests
python -m pytest -q

# Render the pipeline graph (Mermaid)
python -m engine.orchestration.graph
```

### Worked example

`data/revised/` holds a matched two-file set. Upload to workspace 1:

| File | Bucket |
|---|---|
| `EFAS0042.csv`, `ESCH0009.csv` | **C** · Source data |
| `dict_EFAS0042.json`, `dict_ESCH0009.json` | **A** · Source dictionaries |
| `../target_dict/*.json` | **B** · Target dictionary |

The join is discovered from the **data**, not from names: `SCHNO` and `SCHREF`
share no name, but every `SCHNO` value is contained in `SCHREF` and `SCHREF` is
unique — so an N:1 edge is inferred, the policy file becomes the primary, and
`employer_name` maps from the second file.

`tests/fixtures/` holds a deliberately hostile second domain — a claims extract
with a space in a column name, a unicode column, and an apostrophe inside a
target enum value — used to prove the generators survive content they have never
seen.

## LLM mode (optional)

Deterministic and offline by default. To enable the escalation tiers, copy
`.env.example` to `.env`, set either `OPENAI_API_KEY` or the Azure OpenAI
variables, and install the optional `openai` package. Readiness requires
**both**: credentials without the SDK used to report ready and then raise
`ModuleNotFoundError` mid-run, taking the whole mapping down. The UI pill shows
which provider is active, and distinguishes "no credentials" from "credentials
set, but the `openai` package is not installed".
