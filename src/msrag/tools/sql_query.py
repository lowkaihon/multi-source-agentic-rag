"""Read-only SQL engine wrapper for PostgreSQL."""

from __future__ import annotations

import psycopg2
import psycopg2.extras


class SQLEngine:
    """Read-only PostgreSQL connection with statement timeout."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        dbname: str = "mas_compliance",
        user: str = "msrag",
        password: str = "msrag_dev",
    ):
        self.conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
        )
        self.conn.set_session(readonly=True, autocommit=True)
        # Set statement timeout to 10 seconds
        with self.conn.cursor() as cur:
            cur.execute("SET statement_timeout = '10s'")

    def execute(self, query: str) -> list[dict]:
        """Execute a SELECT query and return results as list of dicts."""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query)
            rows = cur.fetchall()
            return [dict(row) for row in rows]

    def health_check(self) -> dict:
        """Check PostgreSQL connectivity and row counts."""
        counts = {}
        for table in [
            "enforcement_actions",
            "regulatory_instruments",
            "regulated_entities",
        ]:
            result = self.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
            counts[table] = result[0]["cnt"]
        return counts

    def close(self):
        if self.conn and not self.conn.closed:
            self.conn.close()
