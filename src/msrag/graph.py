"""Graph builder and context factory for the RAG pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import tiktoken
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from msrag.nodes.agent_retrieve import agent_retrieve_node
from msrag.nodes.generate import generate_node
from msrag.nodes.quality_gate import quality_gate_node
from msrag.state import Context, PipelineState
from msrag.tools.builder import (
    build_agent_system_prompt,
    build_tools,
    extract_sql_schema_description,
)
from msrag.tools.sql_query import SQLEngine
from msrag.tools.vector_search import OpenSearchClient


def route_after_quality_gate(
    state: PipelineState,
) -> Literal["agent_retrieve", "generate"]:
    """Route: quality passed → generate; max retries → generate with caveat; else retry."""
    if state.get("quality_passed"):
        return "generate"
    if (state.get("retrieval_attempts") or 0) >= 2:
        return "generate"  # Proceed with confidence caveat
    return "agent_retrieve"  # Retry with feedback


def build_graph():
    """Build and compile the 3-node RAG pipeline graph."""
    builder = StateGraph(PipelineState, context_schema=Context)

    builder.add_node("agent_retrieve", agent_retrieve_node)
    builder.add_node("quality_gate", quality_gate_node)
    builder.add_node("generate", generate_node)

    builder.add_edge(START, "agent_retrieve")
    builder.add_edge("agent_retrieve", "quality_gate")
    builder.add_conditional_edges(
        "quality_gate",
        route_after_quality_gate,
        {"agent_retrieve": "agent_retrieve", "generate": "generate"},
    )
    builder.add_edge("generate", END)

    return builder.compile(checkpointer=MemorySaver())


def _build_trim_middleware(encoder: tiktoken.Encoding):
    """Build a middleware that trims conversation history to fit context window."""
    MAX_HISTORY_TOKENS = 100_000

    @dynamic_prompt
    def trim_history(request: ModelRequest) -> str:
        """Trim conversation history before each model call."""
        messages = request.state.get("messages", [])
        if not messages:
            return ""

        system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
        conversation_msgs = [m for m in messages if not isinstance(m, SystemMessage)]

        system_tokens = sum(
            len(encoder.encode(str(m.content))) for m in system_msgs
        )
        remaining_budget = MAX_HISTORY_TOKENS - system_tokens

        kept = []
        for msg in reversed(conversation_msgs):
            msg_tokens = len(encoder.encode(str(msg.content)))
            if remaining_budget - msg_tokens < 0:
                break
            kept.append(msg)
            remaining_budget -= msg_tokens

        request.state["messages"] = system_msgs + list(reversed(kept))
        return ""

    return trim_history


def build_context(
    manifest_path: str = "corpus/ingestion_output/metadata_manifest.json",
    ddl_path: str = "corpus/data/sql/init_schema.sql",
    opensearch_host: str = "localhost",
    opensearch_port: int = 9200,
    pg_host: str = "localhost",
    pg_port: int = 5432,
    pg_dbname: str = "mas_compliance",
    pg_user: str = "msrag",
    pg_password: str = "msrag_dev",
) -> Context:
    """Create the runtime Context with all dependencies.

    Performs startup health checks — fails fast if Docker services aren't running.
    """
    # Load manifest
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))

    # Parse SQL schema and inject into manifest
    sql_schema_description = extract_sql_schema_description(ddl_path)
    manifest["sql_schema_description"] = sql_schema_description

    # Initialize infrastructure clients
    print("Connecting to OpenSearch...", end=" ", flush=True)
    opensearch_client = OpenSearchClient(
        host=opensearch_host, port=opensearch_port
    )
    try:
        health = opensearch_client.health_check()
        print(f"OK ({health['doc_count']} docs)")
    except Exception as e:
        raise RuntimeError(
            f"OpenSearch not reachable at {opensearch_host}:{opensearch_port}. "
            f"Run 'docker compose up -d' first. Error: {e}"
        ) from e

    # Check if OpenSearch ML reranker is available; fall back to Python reranking
    try:
        pipelines = opensearch_client.client.transport.perform_request(
            "GET", "/_search/pipeline"
        )
        pipeline_config = pipelines.get("hybrid_rrf_rerank_pipeline", {})
        has_ml_reranker = bool(pipeline_config.get("response_processors"))
        if not has_ml_reranker:
            print("  Note: No ML reranker in pipeline, using Python-side reranking")
            opensearch_client.use_python_reranker = True
    except Exception:
        print("  Note: Could not check search pipeline, using Python-side reranking")
        opensearch_client.use_python_reranker = True

    print("Connecting to PostgreSQL...", end=" ", flush=True)
    sql_engine = SQLEngine(
        host=pg_host,
        port=pg_port,
        dbname=pg_dbname,
        user=pg_user,
        password=pg_password,
    )
    try:
        counts = sql_engine.health_check()
        total = sum(counts.values())
        print(f"OK ({total} rows across {len(counts)} tables)")
    except Exception as e:
        raise RuntimeError(
            f"PostgreSQL not reachable at {pg_host}:{pg_port}. "
            f"Run 'docker compose up -d' first. Error: {e}"
        ) from e

    # Build tools and agent system prompt
    tools = build_tools(manifest, opensearch_client, sql_engine)
    agent_system_prompt = build_agent_system_prompt(manifest)

    # Initialize tiktoken encoder (cached, reused across calls)
    try:
        encoder = tiktoken.encoding_for_model("gpt-5.4-mini")
    except KeyError:
        encoder = tiktoken.get_encoding("o200k_base")

    # Build middleware for history trimming
    trim_middleware = _build_trim_middleware(encoder)

    # Create agent subgraph
    model = ChatOpenAI(model="gpt-5.4-mini", temperature=0)
    agent_subgraph = create_agent(
        model=model,
        tools=tools,
        system_prompt=agent_system_prompt,
        middleware=[trim_middleware],
    )

    return Context(
        opensearch_client=opensearch_client,
        sql_engine=sql_engine,
        manifest=manifest,
        tools=tools,
        agent_system_prompt=agent_system_prompt,
        agent_subgraph=agent_subgraph,
        tiktoken_encoder=encoder,
    )
