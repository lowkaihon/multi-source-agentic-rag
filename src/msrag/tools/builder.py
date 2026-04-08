"""Tool factory, prompt builder, and SQL schema parser.

Bridges the ingestion adapter's metadata manifest to the pipeline's tool
definitions and agent system prompt.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from langchain_core.tools import tool

from msrag.tools.web_search import search_web


def extract_sql_schema_description(ddl_path: str | Path) -> str:
    """Parse init_schema.sql to extract tables, columns, types, and CHECK constraint values.

    Critical: without CHECK values, the agent generates SQL with invalid filter values.
    """
    ddl = Path(ddl_path).read_text(encoding="utf-8")

    tables = []
    # Match CREATE TABLE blocks
    for match in re.finditer(
        r"CREATE TABLE (\w+)\s*\((.*?)\);",
        ddl,
        re.DOTALL,
    ):
        table_name = match.group(1)
        body = match.group(2)

        columns = []
        # Parse each column definition — character class includes single quotes
        # to handle DEFAULT 'value' patterns (e.g., penalty_currency TEXT DEFAULT 'SGD')
        for col_match in re.finditer(
            r"^\s+(\w+)\s+([\w\[\]()., ']+?)(?:\s+--\s*(.+?))?$",
            body,
            re.MULTILINE,
        ):
            col_name = col_match.group(1)
            col_type_raw = col_match.group(2).strip()
            comment = col_match.group(3) or ""

            # Skip constraints that aren't column definitions
            if col_name.upper() in ("CHECK", "PRIMARY", "UNIQUE", "FOREIGN", "CREATE"):
                continue

            # Clean up type (remove NOT NULL, DEFAULT ..., PRIMARY KEY, trailing commas)
            col_type = col_type_raw.split("NOT NULL")[0].split("DEFAULT")[0].strip()
            col_type = re.sub(r"\s*PRIMARY\s+KEY", "", col_type)
            col_type = col_type.rstrip(",").strip()
            col_type = re.sub(r"\s+", " ", col_type)

            # Look for CHECK constraint on this column
            check_values = _extract_check_values(body, col_name)

            col_desc = f"  - {col_name} ({col_type})"
            if check_values:
                col_desc += f"  -- allowed values: {', '.join(check_values)}"
            elif comment:
                col_desc += f"  -- {comment.strip()}"

            columns.append(col_desc)

        if columns:
            tables.append(f"Table: {table_name}\n" + "\n".join(columns))

    # Extract views
    views = []
    for match in re.finditer(
        r"CREATE OR REPLACE VIEW (\w+) AS\s*(SELECT.*?);",
        ddl,
        re.DOTALL,
    ):
        view_name = match.group(1)
        views.append(f"View: {view_name}")

    result = "\n\n".join(tables)
    if views:
        result += "\n\n" + "\n".join(views)
    return result


def _extract_check_values(table_body: str, column_name: str) -> list[str]:
    """Extract CHECK constraint values for a column from the table body."""
    # Pattern: CHECK (column_name IN ('val1', 'val2', ...))
    pattern = rf"CHECK\s*\(\s*{re.escape(column_name)}\s+IN\s*\((.*?)\)\s*\)"
    match = re.search(pattern, table_body, re.DOTALL)
    if match:
        values_str = match.group(1)
        return re.findall(r"'([^']+)'", values_str)
    return []


def build_tools(manifest: dict, opensearch_client, sql_engine) -> list:
    """Build the three @tool closures from manifest + infrastructure clients."""

    @tool
    def vector_search(query: str, search_mode: str = "hybrid") -> str:
        """Search the regulatory document corpus."""
        results = opensearch_client.search(query, mode=search_mode, k=6)

        def sigmoid(x: float) -> float:
            return 1.0 / (1.0 + math.exp(-max(-500, min(500, x))))

        formatted = []
        for doc, logit_score in results:
            formatted.append(
                {
                    "content": doc.page_content,
                    "score": round(sigmoid(logit_score), 3),
                    "metadata": doc.metadata,
                }
            )
        return json.dumps(formatted)

    @tool
    def sql_query(query: str) -> str:
        """Query the structured enforcement/regulatory database."""
        if not query.strip().upper().startswith("SELECT"):
            return json.dumps({"error": "Only SELECT queries allowed"})
        try:
            results = sql_engine.execute(query)
            return json.dumps([dict(row) for row in results], default=str)
        except Exception as e:
            return json.dumps({
                "error": str(e),
                "hint": "Query information_schema to discover columns: "
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = '<table>'",
            })

    @tool
    def web_search_tool(query: str) -> str:
        """Search the web for recent regulatory developments not in the corpus."""
        results = search_web(query)
        return json.dumps(results)

    # Override tool names and docstrings from manifest
    web_search_tool.name = "web_search"

    ctx = manifest.get("tool_description_context", {})

    vector_search.__doc__ = (
        ctx.get(
            "vector_store_summary",
            f"Search {manifest['corpus_name']} ({manifest['document_count']} documents, "
            f"{manifest['chunk_count']} chunks).",
        )
        + " Args: query (str), search_mode ('hybrid' default, 'keyword' for exact "
        "references like 'paragraph 10.3', 'semantic' for conceptual queries)."
    )

    sql_summary = ctx.get(
        "sql_summary",
        f"Query structured data. Tables: {', '.join(manifest['sql_tables'])}. "
        f"Row counts: {json.dumps(manifest['sql_row_counts'])}.",
    )
    sql_query.__doc__ = f"{sql_summary} SELECT only."

    web_search_tool.__doc__ = ctx.get(
        "web_search_summary",
        f"Search the web. Corpus last updated: {manifest['last_ingested']}. "
        "Use for events after this date, external commentary, cross-jurisdictional comparisons.",
    )

    return [vector_search, sql_query, web_search_tool]


def build_agent_system_prompt(manifest: dict) -> str:
    """Construct the agent system prompt from manifest fields."""
    ctx = manifest.get("tool_description_context", {})

    vector_desc = ctx.get(
        "vector_store_summary",
        f"Search across {manifest['document_count']} documents ({manifest['chunk_count']} chunks).",
    )
    sql_desc = ctx.get(
        "sql_summary",
        f"Query structured data across {len(manifest['sql_tables'])} tables.",
    )
    web_desc = ctx.get(
        "web_search_summary",
        f"Search the web. Corpus last updated: {manifest['last_ingested']}.",
    )

    return f"""You are a retrieval agent for {manifest['corpus_name']}.

## Available Tools

### vector_search
{vector_desc}

Use search_mode="hybrid" (default) for most queries.
Use search_mode="keyword" for exact references (e.g., "paragraph 10.3 of Notice 626").
Use search_mode="semantic" for purely conceptual queries.

### sql_query
{sql_desc}

Schema:
{manifest['sql_schema_description']}

Generate SELECT queries only. The database is read-only.

### web_search
{web_desc}

## Guidelines
- Call the most specific tool first. Prefer SQL for counts/dates/amounts, vector for regulatory interpretation.
- You may call multiple tools if the query has both structured and semantic components.
- For follow-up questions, resolve coreferences from conversation history before calling tools.

## Answer Format
After retrieving sufficient information, provide your final answer.
- Attribute each factual claim to its source inline, e.g.:
  "Banks must file STRs within 15 days (MAS Notice 626, Section 13.4)"
  "Goldman Sachs Singapore paid US$122 million (enforcement database)"
  "MAS updated Notice 626 in June 2025 (mas.gov.sg)"
- Use the document filename or section when citing corpus results.
- Use "enforcement database" when citing SQL results.
- Use the site name or URL when citing web results.
- Do not include claims you cannot attribute to a retrieved source.
- Do NOT invent facts or extrapolate beyond retrieved context.
- If information is insufficient, say so explicitly.
"""
