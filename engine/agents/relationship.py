"""Cross-file relationship discovery.

Given several staged source tables, find how they relate — the evidence a
migration analyst would gather by hand: which columns share values, which of
those are join keys (one side unique), and which are merely a shared code
vocabulary. Everything is computed from the data in DuckDB; nothing is guessed
from names alone (a name match only nominates a pair for value testing).

The output feeds two consumers: the UI (show the discovered relationships) and
the composite workset builder (join files safely along N:1 / 1:1 edges).
"""
from __future__ import annotations

from .contracts import Relationship, SourceRelationships, TableInsight
from ..staging import Warehouse

# a column with ~no distinct values can't evidence a relationship; a column
# whose every value is distinct on BOTH sides with low overlap is noise.
MIN_DISTINCT = 2
JOIN_CONTAINMENT = 0.60      # weakest containment we call a join
DOMAIN_MAX_DISTINCT = 40     # code vocabularies are small
UNIQUE_RATIO = 0.99          # near-unique = key side


def _q(c: str) -> str:
    return '"' + c.replace('"', '""') + '"'


def _profile(wh: Warehouse, table: str) -> dict[str, dict]:
    """Per-column: rows with a value, distinct count, uniqueness."""
    out: dict[str, dict] = {}
    for col in wh.column_names(table):
        n, d = wh.query(
            f"SELECT count({_q(col)}), count(DISTINCT {_q(col)}) "
            f'FROM "{table}" WHERE {_q(col)} IS NOT NULL '
            f"AND trim({_q(col)}) <> ''")[0]
        out[col] = {"n": n, "distinct": d,
                    "unique": bool(n) and d / n >= UNIQUE_RATIO}
    return out


def _overlap(wh: Warehouse, lt: str, lc: str, rt: str, rc: str) -> int:
    """|distinct L ∩ distinct R| — trimmed, empties excluded."""
    return wh.query(
        f"SELECT count(*) FROM "
        f"(SELECT DISTINCT trim({_q(lc)}) v FROM \"{lt}\" "
        f" WHERE {_q(lc)} IS NOT NULL AND trim({_q(lc)}) <> '') l "
        f"JOIN "
        f"(SELECT DISTINCT trim({_q(rc)}) v FROM \"{rt}\" "
        f" WHERE {_q(rc)} IS NOT NULL AND trim({_q(rc)}) <> '') r "
        f"USING (v)")[0][0]


def _pairs_to_test(lt: str, lp: dict, li: TableInsight | None,
                   rt: str, rp: dict, ri: TableInsight | None) -> set[tuple]:
    """Nominate column pairs worth the value test: any pair where at least one
    side is a candidate key / near-unique, plus exact name matches."""
    lkeys = set((li.candidate_keys if li else [])) | \
            {c for c, p in lp.items() if p["unique"] and p["distinct"] >= MIN_DISTINCT}
    rkeys = set((ri.candidate_keys if ri else [])) | \
            {c for c, p in rp.items() if p["unique"] and p["distinct"] >= MIN_DISTINCT}
    pairs: set[tuple] = set()
    for lk in lkeys:
        for rc in rp:
            pairs.add((lk, rc))
    for rk in rkeys:
        for lc in lp:
            pairs.add((lc, rk))
    for c in set(lp) & set(rp):                     # same physical name
        pairs.add((c, c))
    return {(a, b) for a, b in pairs
            if lp[a]["distinct"] >= MIN_DISTINCT and rp[b]["distinct"] >= MIN_DISTINCT}


def _classify(lp: dict, rp: dict, cl: float, cr: float) -> tuple[str, str, float]:
    """(kind, cardinality, confidence) for a tested pair."""
    lu, ru = lp["unique"], rp["unique"]
    card = "1:1" if lu and ru else "1:N" if lu else "N:1" if ru else "M:N"
    best = max(cl, cr)
    if best >= JOIN_CONTAINMENT and (lu or ru):
        kind = "join_key"
        conf = round(min(1.0, best * (0.75 + 0.25 * (lu and ru))), 3)
    elif (best >= JOIN_CONTAINMENT
          and lp["distinct"] <= DOMAIN_MAX_DISTINCT
          and rp["distinct"] <= DOMAIN_MAX_DISTINCT):
        kind, conf = "shared_domain", round(best * 0.6, 3)
    else:
        kind, conf = "value_overlap", round(best * 0.4, 3)
    return kind, card, conf


def discover_relationships(wh: Warehouse, tables: list[str],
                           insights: dict[str, TableInsight] | None = None
                           ) -> SourceRelationships:
    """Test every nominated column pair across every pair of tables and keep
    the meaningful edges: all join keys, plus the strongest shared-domain
    edges. One winning edge per (table pair, right column family)."""
    insights = insights or {}
    profiles = {t: _profile(wh, t) for t in tables}
    rels: list[Relationship] = []
    for i, lt in enumerate(tables):
        for rt in tables[i + 1:]:
            lp, rp = profiles[lt], profiles[rt]
            found: list[Relationship] = []
            for lc, rc in sorted(_pairs_to_test(
                    lt, lp, insights.get(lt), rt, rp, insights.get(rt))):
                inter = _overlap(wh, lt, lc, rt, rc)
                if not inter:
                    continue
                cl = round(inter / lp[lc]["distinct"], 3)
                cr = round(inter / rp[rc]["distinct"], 3)
                if max(cl, cr) < JOIN_CONTAINMENT:
                    continue
                kind, card, conf = _classify(lp[lc], rp[rc], cl, cr)
                side = (f"{rt}.{rc} is unique" if rp[rc]["unique"]
                        else f"{lt}.{lc} is unique" if lp[lc]["unique"]
                        else "neither side unique")
                found.append(Relationship(
                    left_table=lt, left_column=lc,
                    right_table=rt, right_column=rc,
                    containment_left=cl, containment_right=cr,
                    cardinality=card, kind=kind, confidence=conf,
                    evidence=(f"{inter} shared value(s): "
                              f"{cl:.0%} of {lt}.{lc} found in {rt}.{rc}, "
                              f"{cr:.0%} the other way; {side}.")))
            # keep the best edge per column on each side — a key matching a
            # key beats the same key matching a code column
            found.sort(key=lambda r: (-{"join_key": 2, "shared_domain": 1,
                                        "value_overlap": 0}[r.kind], -r.confidence))
            kept, seen_l, seen_r = [], set(), set()
            for r in found:
                if r.left_column in seen_l or r.right_column in seen_r:
                    continue
                kept.append(r)
                seen_l.add(r.left_column); seen_r.add(r.right_column)
            rels.extend(kept)
    rels.sort(key=lambda r: -r.confidence)
    return SourceRelationships(tables=list(tables), relationships=rels)


def joinable_edges(rels: SourceRelationships) -> list[Relationship]:
    """Edges the workset may join on: a join key where at least one side is
    unique (so a LEFT JOIN toward the unique side cannot fan rows out)."""
    return [r for r in rels.relationships
            if r.kind == "join_key" and r.cardinality in ("1:1", "N:1", "1:N")]
