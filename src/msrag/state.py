"""Pipeline state schema and runtime context for dependency injection."""

from dataclasses import dataclass, field
from typing import Any, Optional

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages
from typing_extensions import Annotated, TypedDict


class State(TypedDict):
    """Full state schema for the 2-node RAG pipeline."""

    # === INPUT ===
    user_question: str
    messages: Annotated[list[BaseMessage], add_messages]

    # === AGENT RETRIEVAL ===
    retrieved_chunks: Optional[list[dict]]  # [{content, metadata, score}]
    sql_results: Optional[list[dict]]  # Raw rows
    web_results: Optional[list[dict]]  # [{snippet, url}]
    tools_called: Optional[list[str]]  # ["vector_search", "sql_query"]
    final_answer: Optional[str]  # Agent's cited answer
    sources_consulted: Optional[list[dict]]  # [{type, source}]

    # === QUALITY GATE ===
    quality_passed: Optional[bool]
    quality_feedback: Optional[str]  # Structured feedback for retry
    retrieval_attempts: Optional[int]  # Max 2: initial + 1 retry
    confidence_caveat: Optional[str]  # Set if quality gate failed twice


@dataclass
class Context:
    """Runtime dependencies injected via LangGraph context_schema.

    Accessed in nodes via: config['configurable'][CONFIG_KEY_RUNTIME].context
    """

    opensearch_client: Any  # OpenSearchClient
    sql_engine: Any  # SQLEngine
    manifest: dict  # Metadata manifest from ingestion adapter
    tools: list  # Pre-built tools (from build_tools())
    agent_system_prompt: str  # Pre-built from manifest
    agent_subgraph: Any  # Compiled create_agent graph
    tiktoken_encoder: Any  # Cached tiktoken encoder for trim_conversation_history
    store: Any = None  # Required by LangGraph runtime
    cache: Any = None  # Optional semantic cache (Phase 4)
