"""FastAPI server for the Multi-Source RAG pipeline."""

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from langchain_core.messages import HumanMessage

from msrag.api.schemas import (
    ConfigResponse,
    HealthResponse,
    QueryRequest,
    QueryResponse,
    ReadyResponse,
)
from msrag.cache import SemanticCache
from msrag.graph import build_context, build_graph
from msrag.logging_config import configure_logging

logger = logging.getLogger(__name__)

# Module-level state (initialized in lifespan)
graph = None
context = None
semantic_cache: SemanticCache = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize pipeline dependencies on startup."""
    global graph, context, semantic_cache

    load_dotenv()
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))

    print("Initializing pipeline...")
    try:
        context = build_context(
            manifest_path=os.getenv(
                "MANIFEST_PATH", "corpus/ingestion_output/metadata_manifest.json"
            ),
            ddl_path=os.getenv("DDL_PATH", "corpus/data/sql/init_schema.sql"),
            opensearch_host=os.getenv("OPENSEARCH_HOST", "localhost"),
            opensearch_port=int(os.getenv("OPENSEARCH_PORT", "9200")),
            opensearch_index=os.getenv("OPENSEARCH_INDEX", "mas_regulatory"),
            pg_host=os.getenv("PG_HOST", "localhost"),
            pg_port=int(os.getenv("PG_PORT", "5432")),
            pg_dbname=os.getenv("PG_DBNAME", "mas_compliance"),
            pg_user=os.getenv("PG_USER", "msrag"),
            pg_password=os.getenv("PG_PASSWORD", "msrag_dev"),
        )
        graph = build_graph()
        print("Pipeline initialized successfully")
    except Exception as e:
        print(f"Warning: Pipeline initialization failed: {e}")
        print("Service will start but /v1/ready will report not_ready")

    # Initialize semantic cache
    semantic_cache = SemanticCache()
    if semantic_cache.available:
        print("Semantic cache initialized (Redis connected)")
    else:
        print("Semantic cache disabled (CACHE_ENABLED=false or Redis unavailable)")

    yield

    print("Shutting down...")


DESCRIPTION = """
Multi-source agentic RAG pipeline for MAS regulatory compliance.

Intelligently routes queries across **vector search** (regulatory PDFs),
**SQL** (structured enforcement data), and **web search** (recent publications).

## Corpus

**Source A — Vector Store**: 32 MAS regulatory PDFs (3,261 chunks)

| Category | Documents |
|----------|-----------|
| Core Guidelines | 13 |
| Consultation Papers | 5 |
| Enforcement Reports | 5 |
| Info Papers & Circulars | 9 |

**Source B — SQL Database**: Enforcement data (PostgreSQL)

| Table | Rows |
|-------|------|
| enforcement_actions | 337 |
| regulated_entities | 317 |
| regulatory_instruments | 32 |

Action types: 166 prohibition orders · 64 criminal convictions · 50 composition penalties · 25 civil penalties · 14 reprimands

Violation categories: 163 fit_and_proper · 44 market_abuse · 35 aml_cft

**Source C — Web Search**: Real-time via Tavily (recent MAS publications)

## Architecture

2-node LangGraph pipeline: `agent_retrieve` → `quality_gate`

- **agent_retrieve**: ReAct tool-calling loop — picks tools, evaluates results, produces cited answer
- **quality_gate**: 3 deterministic policies, zero LLM calls — routes to retry or END
"""

app = FastAPI(
    title="Multi-Source Agentic RAG API",
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def root():
    """Redirect root to API documentation."""
    return RedirectResponse(url="/docs")


@app.get("/v1/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    """Liveness probe — checks if the service is running."""
    return HealthResponse(status="healthy")


@app.get("/v1/ready", response_model=ReadyResponse, tags=["Health"])
async def readiness_check():
    """Readiness probe — checks OpenSearch and PostgreSQL connectivity."""
    os_connected = False
    os_doc_count = 0
    pg_connected = False

    if context is not None:
        try:
            health = context.opensearch_client.health_check()
            os_connected = True
            os_doc_count = health["doc_count"]
        except Exception:
            pass

        try:
            context.sql_engine.health_check()
            pg_connected = True
        except Exception:
            pass

    is_ready = os_connected and pg_connected

    return ReadyResponse(
        status="ready" if is_ready else "not_ready",
        opensearch_connected=os_connected,
        postgres_connected=pg_connected,
        opensearch_doc_count=os_doc_count,
        message="Service is ready" if is_ready else "Dependencies not available",
    )


@app.get("/v1/config", response_model=ConfigResponse, tags=["Configuration"])
async def get_config():
    """Get corpus metadata and cache status."""
    manifest = context.manifest if context else {}

    return ConfigResponse(
        version="1.0.0",
        corpus_name=manifest.get("corpus_name", "mas_regulatory"),
        document_count=manifest.get("document_count", 0),
        chunk_count=manifest.get("chunk_count", 0),
        cache_enabled=semantic_cache.available if semantic_cache else False,
    )


@app.post("/v1/query", response_model=QueryResponse, tags=["RAG"])
async def query_rag(request: QueryRequest):
    """Query the RAG pipeline.

    Runs the full agentic RAG pipeline:
    1. Agent selects tools and produces a cited answer
    2. Quality gate enforces retrieval policies (retry or pass)

    Supports semantic caching for repeated/similar questions.
    """
    if graph is None or context is None:
        raise HTTPException(
            status_code=503, detail="Pipeline not initialized. Check /v1/ready."
        )

    # Check semantic cache first
    if request.use_cache and semantic_cache and semantic_cache.available:
        start_time = time.time()
        cached = semantic_cache.lookup(request.question)
        if cached:
            lookup_time = time.time() - start_time
            response_data = cached["response"]
            response_data["cache_hit"] = True
            response_data["cache_similarity"] = cached["similarity"]
            response_data["processing_time_seconds"] = round(lookup_time, 2)
            logger.info(
                "query_complete",
                extra={"structured": {
                    "event": "query_complete",
                    "question": request.question[:80],
                    "cache_hit": True,
                    "processing_time": round(lookup_time, 2),
                }},
            )
            return QueryResponse(**response_data)

    # Build initial state (same pattern as main.py)
    initial_state = {
        "user_question": request.question,
        "messages": [HumanMessage(content=request.question)],
        "retrieval_attempts": 0,
    }

    thread_id = request.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    try:
        start_time = time.time()

        # MemorySaver is sync-only — wrap in to_thread for async correctness
        final_state = await asyncio.to_thread(
            _run_graph, initial_state, config
        )

        processing_time = time.time() - start_time

        response_fields = dict(
            answer=final_state.get("final_answer", "No answer generated"),
            tools_called=final_state.get("tools_called") or [],
            sources_consulted=final_state.get("sources_consulted") or [],
            retrieval_attempts=final_state.get("retrieval_attempts", 0),
            quality_passed=final_state.get("quality_passed", False),
            confidence_caveat=final_state.get("confidence_caveat"),
            processing_time_seconds=round(processing_time, 2),
        )

        logger.info(
            "query_complete",
            extra={"structured": {
                "event": "query_complete",
                "question": request.question[:80],
                "cache_hit": False,
                "tools_called": response_fields["tools_called"],
                "quality_passed": response_fields["quality_passed"],
                "retrieval_attempts": response_fields["retrieval_attempts"],
                "sources_consulted_count": len(response_fields["sources_consulted"]),
                "processing_time": response_fields["processing_time_seconds"],
            }},
        )

        # Store in semantic cache
        if request.use_cache and semantic_cache and semantic_cache.available:
            semantic_cache.store(request.question, response_fields)

        return QueryResponse(**response_fields)

    except Exception as e:
        logger.exception(
            "query_error",
            extra={"structured": {
                "event": "query_error",
                "question": request.question[:80],
            }},
        )
        raise HTTPException(
            status_code=500, detail=f"Error processing query: {str(e)}"
        )


def _run_graph(initial_state: dict, config: dict) -> dict:
    """Run the graph synchronously, collecting the final state."""
    final_state = {}
    for event in graph.stream(
        initial_state, config, context=context, stream_mode="updates"
    ):
        for node_name, node_output in event.items():
            final_state.update(node_output)
    return final_state


@app.get("/v1/cache/stats", tags=["Cache"])
async def cache_stats():
    """Get semantic cache statistics (hit rate, size, etc.)."""
    if semantic_cache is None:
        return {"enabled": False, "available": False}
    return semantic_cache.get_stats()


@app.post("/v1/cache/flush", tags=["Cache"])
async def cache_flush():
    """Flush all cached entries for current corpus version."""
    if semantic_cache is None or not semantic_cache.available:
        return {"flushed": 0, "message": "Cache not available"}
    count = semantic_cache.flush()
    return {"flushed": count, "message": f"Flushed {count} cache entries"}
