"""Agent retrieval node: invoke the agent subgraph and extract structured state."""

from __future__ import annotations

import json
import logging

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from langchain_core.runnables import RunnableConfig
from langgraph.config import CONFIG_KEY_RUNTIME

from msrag.state import Context, State

logger = logging.getLogger(__name__)


def _build_sources_from_results(extracted: dict) -> list[dict]:
    """Build sources_consulted from tool results, not LLM text.

    Extracts document filenames, SQL source label, and web URLs
    from the structured results the tools actually returned.
    """
    sources: list[dict] = []
    seen: set[str] = set()

    for chunk in extracted.get("retrieved_chunks", []):
        source = chunk.get("metadata", {}).get("source_document", "")
        if source and source not in seen:
            seen.add(source)
            sources.append({"type": "document", "source": source})

    if extracted.get("sql_results"):
        sources.append({"type": "sql", "source": "structured database"})

    for result in extracted.get("web_results", []):
        url = result.get("url", "")
        if url and url not in seen:
            seen.add(url)
            sources.append({"type": "web", "source": url})

    return sources


def _deduplicate_chunks(chunks: list[dict]) -> list[dict]:
    """Deduplicate retrieved chunks by content hash."""
    seen: set[int] = set()
    unique = []
    for chunk in chunks:
        h = hash(chunk.get("content", ""))
        if h not in seen:
            seen.add(h)
            unique.append(chunk)
    return unique


def _deduplicate_rows(rows: list[dict]) -> list[dict]:
    """Deduplicate SQL rows by their content."""
    seen: set[frozenset] = set()
    unique = []
    for row in rows:
        key = frozenset(
            (k, str(v)) for k, v in sorted(row.items())
        )
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def _deduplicate_web(results: list[dict]) -> list[dict]:
    """Deduplicate web results by URL."""
    seen: set[str] = set()
    unique = []
    for r in results:
        url = r.get("url", "")
        if url not in seen:
            seen.add(url)
            unique.append(r)
    return unique


def parse_tool_results(messages: list) -> dict:
    """Extract structured results from agent's ToolMessage objects.

    Pure function — no side effects, testable with synthetic ToolMessages.
    Accumulates results across all tool calls, then deduplicates.
    """
    extracted: dict = {
        "retrieved_chunks": [],
        "sql_results": [],
        "web_results": [],
        "tools_called": [],
    }

    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue

        tool_name = msg.name
        extracted["tools_called"].append(tool_name)

        try:
            content = json.loads(msg.content)
        except (json.JSONDecodeError, TypeError):
            continue

        if isinstance(content, dict) and "error" in content:
            continue

        if tool_name == "vector_search":
            extracted["retrieved_chunks"].extend(
                content if isinstance(content, list) else []
            )
        elif tool_name == "sql_query":
            extracted["sql_results"].extend(
                content if isinstance(content, list) else []
            )
        elif tool_name == "web_search":
            extracted["web_results"].extend(
                content if isinstance(content, list) else []
            )

    # Deduplicate tools_called while preserving order
    seen: set[str] = set()
    extracted["tools_called"] = [
        t for t in extracted["tools_called"] if not (t in seen or seen.add(t))
    ]

    # Deduplicate accumulated results
    extracted["retrieved_chunks"] = _deduplicate_chunks(extracted["retrieved_chunks"])
    extracted["sql_results"] = _deduplicate_rows(extracted["sql_results"])
    extracted["web_results"] = _deduplicate_web(extracted["web_results"])

    return extracted


def agent_retrieve_node(state: State, config: RunnableConfig) -> dict:
    """Invoke agent subgraph, then extract structured state from ToolMessages."""
    ctx: Context = config["configurable"][CONFIG_KEY_RUNTIME].context
    agent = ctx.agent_subgraph

    # On retry, inject quality gate feedback as a SystemMessage
    feedback = state.get("quality_feedback")
    if feedback and state.get("retrieval_attempts", 0) > 0:
        messages = list(state.get("messages", []))
        messages.append(
            SystemMessage(
                content=f"Quality gate feedback from previous attempt: {feedback}"
            )
        )
        result = agent.invoke({**state, "messages": messages})
    else:
        result = agent.invoke(state)

    extracted = parse_tool_results(result["messages"])

    # Extract final answer from the last non-tool-call AIMessage
    final_answer = None
    for msg in reversed(result["messages"]):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            final_answer = msg.content
            break

    sources = _build_sources_from_results(extracted)

    output = {
        "messages": result["messages"],
        "final_answer": final_answer,
        "sources_consulted": sources,
        **extracted,
    }

    logger.info(
        "agent_retrieve_complete",
        extra={"structured": {
            "event": "agent_retrieve_complete",
            "tools_called": extracted["tools_called"],
            "chunk_count": len(extracted["retrieved_chunks"]),
            "sql_row_count": len(extracted["sql_results"]),
            "web_result_count": len(extracted["web_results"]),
            "sources_consulted_count": len(sources),
            "attempt": state.get("retrieval_attempts", 0) + 1,
        }},
    )

    return output
