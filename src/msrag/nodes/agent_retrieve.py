"""Agent retrieval node: invoke the agent subgraph and extract structured state."""

from __future__ import annotations

import json
import re

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from langchain_core.runnables import RunnableConfig
from langgraph.config import CONFIG_KEY_RUNTIME

from msrag.state import Context, PipelineState


def _extract_citations(answer_text: str) -> list[dict]:
    """Extract citations from the agent's final answer text.

    Recognizes patterns: [Source: filename], [SQL Result], [Web: url]
    """
    citations = []
    seen: set[str] = set()

    for match in re.finditer(r"\[Source:\s*([^\]]+)\]", answer_text):
        ref = match.group(1).strip()
        if ref not in seen:
            seen.add(ref)
            citations.append({"type": "document", "source": ref})

    if "[SQL Result]" in answer_text:
        citations.append({"type": "sql", "source": "structured database"})

    for match in re.finditer(r"\[Web:\s*([^\]]+)\]", answer_text):
        url = match.group(1).strip()
        if url not in seen:
            seen.add(url)
            citations.append({"type": "web", "source": url})

    return citations


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


def agent_retrieve_node(state: PipelineState, config: RunnableConfig) -> dict:
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

    return {
        "messages": result["messages"],
        "final_answer": final_answer,
        "citations": _extract_citations(final_answer or ""),
        **extracted,
    }
