"""Quality gate node: policy enforcement between retrieval and generation.

Three deterministic checks, zero LLM calls. The agent can already see its own
tool responses — the quality gate enforces policies the agent might not apply.
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from msrag.state import PipelineState


def quality_gate_node(state: PipelineState, config: RunnableConfig) -> dict:
    """Policy enforcement point. No LLM calls."""
    attempts = (state.get("retrieval_attempts") or 0) + 1
    chunks = state.get("retrieved_chunks") or []
    sql_results = state.get("sql_results") or []
    web_results = state.get("web_results") or []
    tools_called = state.get("tools_called") or []

    has_primary = bool(chunks) or bool(sql_results)

    # Policy 1: Primary source requirement.
    # Regulatory answers must cite indexed sources, not web-only.
    # Only fires if agent never attempted primary sources — if it tried and
    # got nothing, web-only is acceptable (legitimate out-of-corpus query).
    if not has_primary and bool(web_results):
        primary_attempted = (
            "vector_search" in tools_called or "sql_query" in tools_called
        )
        if not primary_attempted:
            return {
                "quality_passed": False,
                "quality_feedback": (
                    "Answer must be grounded in indexed corpus or structured database. "
                    "Try vector_search or sql_query before relying on web results."
                ),
                "retrieval_attempts": attempts,
            }

    # Policy 2: All sources empty — retry only if untried tools remain.
    if not chunks and not sql_results and not web_results:
        untried = [
            t
            for t in ["vector_search", "sql_query", "web_search"]
            if t not in tools_called
        ]
        if untried:
            return {
                "quality_passed": False,
                "quality_feedback": f"All results empty. Try: {', '.join(untried)}.",
                "retrieval_attempts": attempts,
            }
        # All tools tried, all empty — retrying won't help. Let generate
        # node produce an explicit "insufficient information" response.
        return {"quality_passed": True, "retrieval_attempts": attempts}

    # Policy 3: SQL results reference specific regulations → retrieve the text.
    # Data-driven: checks if regulation_breached field is populated.
    if sql_results and not chunks:
        has_regulation_refs = any(
            row.get("regulation_breached") for row in sql_results
        )
        if has_regulation_refs:
            return {
                "quality_passed": False,
                "quality_feedback": (
                    "SQL results reference specific regulations. "
                    "Use vector_search to retrieve the cited regulation text."
                ),
                "retrieval_attempts": attempts,
            }

    return {"quality_passed": True, "retrieval_attempts": attempts}
