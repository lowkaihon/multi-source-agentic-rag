# Multi-stage Dockerfile for Multi-Source Agentic RAG API
# Stage 1: Builder - Install dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv for fast dependency management
RUN pip install --no-cache-dir uv

# Copy dependency files and source (needed for editable install)
COPY pyproject.toml uv.lock* ./
COPY README.md ./
COPY src/ ./src/

# Create virtual environment and install dependencies
RUN uv sync --frozen --no-dev || uv sync --no-dev

# Stage 2: Runtime - Minimal production image
FROM python:3.12-slim AS runtime

WORKDIR /app

# Install curl for entrypoint health wait
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy source code
COPY src/ /app/src/

# Copy only the corpus files needed at runtime (not the 140MB opensearch_documents.json)
COPY corpus/ingestion_output/metadata_manifest.json /app/corpus/ingestion_output/metadata_manifest.json
COPY corpus/data/sql/init_schema.sql /app/corpus/data/sql/init_schema.sql

# Copy entrypoint
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Set environment variables
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"
ENV PYTHONUNBUFFERED=1

# Prevent OpenMP/MKL threading issues in containers
ENV OMP_NUM_THREADS=1
ENV MKL_NUM_THREADS=1
ENV TOKENIZERS_PARALLELISM=false

# Set HuggingFace cache to persistent location in image
ENV HF_HOME=/app/.cache/huggingface
ENV TRANSFORMERS_CACHE=/app/.cache/huggingface

# Pre-download CrossEncoder model for Python-side reranking fallback
RUN python -c "\
from sentence_transformers import CrossEncoder; \
print('Downloading CrossEncoder model...'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2'); \
print('CrossEncoder model downloaded successfully')"

# Force offline mode for HuggingFace - no runtime downloads
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/v1/health')" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
