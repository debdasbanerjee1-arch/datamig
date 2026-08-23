"""The DuckDB layer — staging + profiling + rule execution.

Sits BETWEEN the raw source and the agents. Sources (and the existing target)
are loaded once into a DuckDB store; the analyst, matcher, and validator all read
evidence from here rather than re-reading files or holding raw data. This is also
where generated transformation rules are executed for validation.

Default store is a file (data/warehouse.duckdb) so staged tables persist across agent
runs and LangGraph nodes; pass ":memory:" for throwaway use.
"""
from __future__ import annotations

import duckdb


class Warehouse:
    def __init__(self, db_path: str = "data/warehouse.duckdb"):
        self.db_path = db_path
        self.con = duckdb.connect(db_path)

    # ---- staging -------------------------------------------------------
    def stage_csv(self, path: str, table: str, all_varchar: bool = True) -> str:
        """Load a CSV into a staged table. all_varchar=True preserves legacy
        text fidelity (YYYYMMDD, sentinels, leading zeros); set False when typed
        inference is wanted (e.g. the matcher's numeric/date signals)."""
        self.con.execute(
            f'CREATE OR REPLACE TABLE "{table}" AS '
            "SELECT * FROM read_csv_auto(?, all_varchar=?, header=true)",
            [path, all_varchar],
        )
        return table

    # ---- read primitives ----------------------------------------------
    def list_tables(self) -> list[str]:
        return [r[0] for r in self.con.execute("SHOW TABLES").fetchall()]

    def column_names(self, table: str) -> list[str]:
        return [r[1] for r in self.con.execute(f"PRAGMA table_info('{table}')").fetchall()]

    def column_info(self, table: str) -> list[tuple]:
        return self.con.execute(f"PRAGMA table_info('{table}')").fetchall()

    def row_count(self, table: str) -> int:
        return self.con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]

    def fetch_dicts(self, table: str) -> list[dict]:
        cols = self.column_names(table)
        return [dict(zip(cols, r)) for r in self.con.execute(f'SELECT * FROM "{table}"').fetchall()]

    def query(self, sql: str, params: list | None = None) -> list[tuple]:
        return self.con.execute(sql, params or []).fetchall()

    # ---- rule execution (validation) ----------------------------------
    def apply_expression(self, table: str, expr: str) -> list[tuple]:
        """Execute a generated SQL expression over a staged table. Raises on
        malformed SQL so the validator can feed the error back."""
        return self.con.execute(f'SELECT {expr} AS out FROM "{table}"').fetchall()

    def close(self) -> None:
        self.con.close()
