"""The composite workset — how several source files become one mappable source.

Strategy: pick a primary (driving) table, then LEFT JOIN every other file that
has a safe path to it — an edge where the joined-in side is unique on the join
column, so the primary's row grain is preserved (no fan-out). Files without a
safe path are excluded and reported, never silently joined.

The result is a DuckDB VIEW plus one combined EnrichedDictionary whose columns
carry origin_table/origin_name, so the mapping, validation and review agents
run completely unchanged against the workset — multi-source becomes a staging
concern, not an agent concern.
"""
from __future__ import annotations

from .agents.contracts import (EnrichedColumn, EnrichedDictionary,
                               SourceRelationships)
from .agents.relationship import joinable_edges
from .staging import Warehouse

WORKSET = "__workset"


def _q(c: str) -> str:
    return '"' + c.replace('"', '""') + '"'


def choose_primary(tables: list[str], rels: SourceRelationships,
                   row_counts: dict[str, int] | None = None
                   ) -> tuple[str, list[list[str]]]:
    """Pick the driving (finest-grain) file automatically — never left to the
    user. The primary is the file that anchors the join: everything else must
    reach it without fanning its rows out.

    Principle, not a size heuristic: build the grain-preserving join graph
    (only N:1 / 1:1 edges, oriented from the 'many' side toward the 'one'
    side) and pick the file from which the most other files are reachable
    WITHOUT ever traversing a 1:N (fan-out) step. That file is the finest
    grain — the 'many' end of every chain — so a LEFT JOIN outward from it
    preserves one output row per primary row. Row count breaks ties (finest
    grain = most rows), which also decides a set with no joins at all.

    Returns (primary, components) where components lists the connected groups
    of files under grain-preserving joins; more than one component means the
    files don't share a single grain and some will be excluded.
    """
    row_counts = row_counts or {}
    edges = joinable_edges(rels)

    # directed reach along fan-in edges: many --> one (child points at parent).
    # If the child side (many) is unique it's really 1:1; treat either unique
    # side as a safe outward hop from the many-end.
    reach: dict[str, set[str]] = {t: set() for t in tables}
    undirected: dict[str, set[str]] = {t: set() for t in tables}
    for r in edges:
        lt, rt, card = r.left_table, r.right_table, r.cardinality
        undirected[lt].add(rt); undirected[rt].add(lt)
        # the 'many' end can safely join TOWARD the unique 'one' end
        if card == "N:1":      reach[lt].add(rt)             # left is many
        elif card == "1:N":    reach[rt].add(lt)             # right is many
        elif card == "1:1":    reach[lt].add(rt); reach[rt].add(lt)

    def outward(root: str) -> set[str]:
        seen, stack = {root}, [root]
        while stack:
            cur = stack.pop()
            for nxt in reach[cur]:
                if nxt not in seen:
                    seen.add(nxt); stack.append(nxt)
        return seen - {root}

    # connected components under grain-preserving joins (for reporting)
    comps: list[list[str]] = []
    unseen = set(tables)
    while unseen:
        root = next(iter(unseen))
        grp, stack = {root}, [root]
        while stack:
            cur = stack.pop()
            for nxt in undirected[cur]:
                if nxt not in grp:
                    grp.add(nxt); stack.append(nxt)
        comps.append(sorted(grp)); unseen -= grp

    primary = max(tables, key=lambda t: (len(outward(t)),
                                         row_counts.get(t, 0), t == tables[0]))
    return primary, comps


def plan_joins(primary: str, tables: list[str],
               rels: SourceRelationships) -> tuple[list[dict], list[dict]]:
    """BFS out from the primary along safe edges. A table joins in only when
    the column on ITS side of the edge is unique (grain-preserving).
    Returns (join_plan, excluded)."""
    edges = joinable_edges(rels)
    joined = {primary}
    plan: list[dict] = []
    progress = True
    while progress:
        progress = False
        for r in edges:
            inside, outside = None, None
            if r.left_table in joined and r.right_table not in joined:
                inside, outside = (r.left_table, r.left_column), (r.right_table, r.right_column)
                new_side_unique = r.cardinality in ("N:1", "1:1")
            elif r.right_table in joined and r.left_table not in joined:
                inside, outside = (r.right_table, r.right_column), (r.left_table, r.left_column)
                new_side_unique = r.cardinality in ("1:N", "1:1")
            else:
                continue
            if not new_side_unique:
                continue
            if outside[0] in {t for t in tables}:
                plan.append({"table": outside[0], "on": outside[1],
                             "to_table": inside[0], "to_column": inside[1],
                             "cardinality": r.cardinality,
                             "evidence": r.evidence})
                joined.add(outside[0])
                progress = True
    excluded = []
    for t in tables:
        if t in joined:
            continue
        has_key = any(r.kind == "join_key" and t in (r.left_table, r.right_table)
                      for r in rels.relationships)
        excluded.append({"table": t, "reason": (
            "a join key exists but joining it would multiply the primary's "
            "rows (1:N toward it) — needs aggregation; review with an SME"
            if has_key else
            "no join key discovered to the joined set")})
    return plan, excluded


def build_workset(wh: Warehouse, primary: str, tables: list[str],
                  dicts: dict[str, EnrichedDictionary],
                  rels: SourceRelationships
                  ) -> tuple[str, EnrichedDictionary, list[dict], list[dict]]:
    """Create/replace the workset view and the combined dictionary.

    Column naming: the primary keeps its names; a joined file's column keeps
    its name if globally free, else becomes {table}_{name}. origin_* on every
    combined column records where it really lives."""
    plan, excluded = plan_joins(primary, tables, rels)
    order = [primary] + [j["table"] for j in plan]

    taken: set[str] = set()
    select_parts: list[str] = []
    columns: list[EnrichedColumn] = []
    alias = {t: f"t{i}" for i, t in enumerate(order)}
    rename: dict[tuple, str] = {}

    for t in order:
        d = dicts[t]
        for c in d.columns:
            out = c.name if c.name not in taken else f"{t}_{c.name}"
            taken.add(out)
            rename[(t, c.name)] = out
            select_parts.append(f"{alias[t]}.{_q(c.name)} AS {_q(out)}")
            cc = c.model_copy(deep=True)
            cc.origin_table, cc.origin_name, cc.name = t, c.name, out
            if t != primary:
                # note the provenance in plain English — origin_table carries
                # the machine-readable form
                note = f"Joined in from {t} (see discovered relationships)."
                cc.description = f"{cc.description} {note}".strip()
            columns.append(cc)

    sql = f'CREATE OR REPLACE VIEW {_q(WORKSET)} AS SELECT ' \
          + ", ".join(select_parts) + f' FROM "{primary}" {alias[primary]}'
    for j in plan:
        sql += (f' LEFT JOIN "{j["table"]}" {alias[j["table"]]} '
                f'ON trim({alias[j["to_table"]]}.{_q(j["to_column"])}) = '
                f'trim({alias[j["table"]]}.{_q(j["on"])})')
    wh.con.execute(sql)

    combined = EnrichedDictionary(table=WORKSET, columns=columns)
    return WORKSET, combined, plan, excluded
