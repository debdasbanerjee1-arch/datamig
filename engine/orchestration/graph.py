"""Assemble the five agents into one LangGraph pipeline.

    stage_source -> analyst -> legacy_expert -> mapping -> validation -> review

`review` is the single human-in-the-loop gate (exception-driven). If decisions
are supplied, they are applied and the spec is re-validated, then reviewed again
(the loop terminates because apply_decisions clears the decisions). With no
decisions, the graph ends at review with the queue ready for a human / the UI.

Run `python -m engine.orchestration.graph` to print the Mermaid diagram (no data needed).

NOTE ON REACH: the demo UI (api/server.py + web/) now exposes only the mapping
workspace, so `flow_mapping_manual` / `flow_mapping_manual_certify` are the
graphs it drives. `app`, `flow_a`, `flow_b` and `flow_b_certify` are NOT dead —
they are the CLI's entrypoints (cli/run.py, cli/run_flow_a.py, cli/run_flow_b.py)
and the harness the test suite uses to exercise the comprehension agents
(analyst, legacy_expert) and the knowledge store. Deleting them would take the
COBOL-comprehension coverage with them, so they stay.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from . import nodes
from .state import PipelineState


def _route_after_review(state: PipelineState) -> str:
    return "apply" if state.get("decisions") else "end"


def build():
    g = StateGraph(PipelineState)
    g.add_node("stage_source", nodes.stage_source)
    g.add_node("analyst", nodes.analyst_node)
    g.add_node("legacy_expert", nodes.legacy_expert_node)
    g.add_node("mapping", nodes.mapping_node)
    g.add_node("validation", nodes.validation_node)
    g.add_node("review", nodes.review_node)
    g.add_node("apply_decisions", nodes.apply_decisions_node)

    g.add_edge(START, "stage_source")
    g.add_edge("stage_source", "analyst")
    g.add_edge("analyst", "legacy_expert")
    g.add_edge("legacy_expert", "mapping")
    g.add_edge("mapping", "validation")
    g.add_edge("validation", "review")
    g.add_conditional_edges("review", _route_after_review,
                            {"apply": "apply_decisions", "end": END})
    g.add_edge("apply_decisions", "validation")   # re-validate after edits
    return g.compile()


app = build()


def build_flow_a():
    """Flow A (one-off source understanding): runs on new/changed inputs only.

        stage_source -> analyst -> legacy_expert -> persist_kg

    Output: a versioned, fingerprinted knowledge graph + source dictionary in
    the knowledge store (status 'draft' until an SME certifies it).
    """
    g = StateGraph(PipelineState)
    g.add_node("stage_source", nodes.stage_source)
    g.add_node("analyst", nodes.analyst_node)
    g.add_node("legacy_expert", nodes.legacy_expert_node)
    g.add_node("persist_kg", nodes.persist_kg_node)
    g.add_edge(START, "stage_source")
    g.add_edge("stage_source", "analyst")
    g.add_edge("analyst", "legacy_expert")
    g.add_edge("legacy_expert", "persist_kg")
    g.add_edge("persist_kg", END)
    return g.compile()


def build_flow_b():
    """Flow B (per-target mapping): starts from PERSISTED knowledge.

        load_kg -> load_target -> mapping -> validation -> review [-> apply]

    Consumes the latest certified knowledge version by default (or an explicit
    kg_version); never re-runs comprehension.
    """
    g = StateGraph(PipelineState)
    g.add_node("load_kg", nodes.load_kg_node)
    g.add_node("load_target", nodes.load_target_node)
    g.add_node("mapping", nodes.mapping_node)
    g.add_node("validation", nodes.validation_node)
    g.add_node("review", nodes.review_node)
    g.add_node("apply_decisions", nodes.apply_decisions_node)
    g.add_edge(START, "load_kg")
    g.add_edge("load_kg", "load_target")
    g.add_edge("load_target", "mapping")
    g.add_edge("mapping", "validation")
    g.add_edge("validation", "review")
    g.add_conditional_edges("review", _route_after_review,
                            {"apply": "apply_decisions", "end": END})
    g.add_edge("apply_decisions", "validation")
    return g.compile()


flow_a = build_flow_a()
flow_b = build_flow_b()


def build_flow_b_certify():
    """Certify path — apply the reviewer's decisions to the spec they already
    reviewed, WITHOUT re-running the mapping agent.

        load_kg -> load_target -> seed_spec -> apply_decisions -> validation -> review

    The mapping agent is skipped entirely: the client sends back the exact spec
    it reviewed (spec_in), we adopt it, apply the human decisions on top, then
    re-validate and rebuild the queue so the verdict reflects the final,
    decided spec. Validation is kept (it's cheap and confirms the decided spec
    still loads and preserves grain) but no mapping is re-derived.
    """
    g = StateGraph(PipelineState)
    g.add_node("load_kg", nodes.load_kg_node)
    g.add_node("load_target", nodes.load_target_node)
    g.add_node("seed_spec", nodes.seed_spec_node)
    g.add_node("apply_decisions", nodes.apply_decisions_node)
    g.add_node("validation", nodes.validation_node)
    g.add_node("review", nodes.review_node)
    g.add_edge(START, "load_kg")
    g.add_edge("load_kg", "load_target")
    g.add_edge("load_target", "seed_spec")
    g.add_edge("seed_spec", "apply_decisions")
    g.add_edge("apply_decisions", "validation")
    g.add_edge("validation", "review")
    g.add_edge("review", END)
    return g.compile()


flow_b_certify = build_flow_b_certify()


def build_flow_mapping_manual():
    """Manual Mapping Workspace: the enriched source dictionary (Agent 2's
    artefact) is uploaded directly by the user instead of being computed by
    Flow A / loaded from the knowledge store.

        manual_inputs -> mapping -> validation -> review [-> apply]

    Mirrors flow_b, minus load_kg/load_target — target_dict arrives in the
    initial state directly (same as it always has), and manual_inputs_node
    supplies `enriched` (and an optional `insight`) from the client's upload.
    """
    g = StateGraph(PipelineState)
    g.add_node("manual_inputs", nodes.manual_inputs_node)
    g.add_node("mapping", nodes.mapping_node)
    g.add_node("validation", nodes.validation_node)
    g.add_node("review", nodes.review_node)
    g.add_node("apply_decisions", nodes.apply_decisions_node)
    g.add_edge(START, "manual_inputs")
    g.add_edge("manual_inputs", "mapping")
    g.add_edge("mapping", "validation")
    g.add_edge("validation", "review")
    g.add_conditional_edges("review", _route_after_review,
                            {"apply": "apply_decisions", "end": END})
    g.add_edge("apply_decisions", "validation")
    return g.compile()


flow_mapping_manual = build_flow_mapping_manual()


def build_flow_mapping_manual_certify():
    """Certify path for the manual Mapping Workspace — applies the reviewer's
    decisions to the spec they reviewed WITHOUT re-running the mapping agent,
    same contract as flow_b_certify but without a knowledge version to load.

        manual_seed_spec -> apply_decisions -> validation -> review
    """
    g = StateGraph(PipelineState)
    g.add_node("manual_seed_spec", nodes.manual_seed_spec_node)
    g.add_node("apply_decisions", nodes.apply_decisions_node)
    g.add_node("validation", nodes.validation_node)
    g.add_node("review", nodes.review_node)
    g.add_edge(START, "manual_seed_spec")
    g.add_edge("manual_seed_spec", "apply_decisions")
    g.add_edge("apply_decisions", "validation")
    g.add_edge("validation", "review")
    g.add_edge("review", END)
    return g.compile()


flow_mapping_manual_certify = build_flow_mapping_manual_certify()


if __name__ == "__main__":
    print(app.get_graph().draw_mermaid())
