"""Pydantic request/response models for the RAG API."""

from typing import Optional

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    """Request model for RAG query endpoint."""

    question: str = Field(..., min_length=1, description="The question to ask")
    thread_id: Optional[str] = Field(
        None, description="Thread ID for multi-turn conversations. Auto-generated if not provided."
    )
    use_cache: bool = Field(
        True, description="Use semantic cache for faster responses on repeated/similar questions"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "question": "What are MAS's CDD requirements for PEPs?",
                    "thread_id": None,
                    "use_cache": True,
                }
            ]
        }
    }


class QueryResponse(BaseModel):
    """Response model for RAG query endpoint."""

    answer: str = Field(..., description="The generated answer")
    tools_called: list[str] = Field(..., description="Tools invoked during retrieval")
    citations: list[dict] = Field(..., description="Source citations [{type, source}]")
    retrieval_attempts: int = Field(..., ge=0, description="Number of retrieval attempts")
    quality_passed: bool = Field(..., description="Whether the quality gate passed")
    confidence_caveat: Optional[str] = Field(None, description="Caveat if quality gate failed")
    processing_time_seconds: float = Field(..., ge=0, description="Total processing time")
    cache_hit: bool = Field(False, description="Whether response was served from cache")
    cache_similarity: Optional[float] = Field(None, description="Similarity score of cache match")


class HealthResponse(BaseModel):
    """Response model for health check endpoint."""

    status: str = Field(..., description="Health status: healthy or unhealthy")


class ReadyResponse(BaseModel):
    """Response model for readiness check endpoint."""

    status: str = Field(..., description="Readiness status: ready or not_ready")
    opensearch_connected: bool = Field(..., description="Whether OpenSearch is reachable")
    postgres_connected: bool = Field(..., description="Whether PostgreSQL is reachable")
    opensearch_doc_count: int = Field(0, description="Number of indexed documents")
    message: Optional[str] = Field(None, description="Additional status message")


class ConfigResponse(BaseModel):
    """Response model for configuration endpoint."""

    version: str = Field(..., description="API version")
    corpus_name: str = Field(..., description="Corpus identifier")
    document_count: int = Field(..., description="Number of source documents")
    chunk_count: int = Field(..., description="Number of indexed chunks")
    cache_enabled: bool = Field(..., description="Whether semantic cache is enabled")
