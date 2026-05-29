"""OpenSearch setup: index creation, ML model registration, search pipeline, bulk indexing.

Usage:
    uv run python scripts/setup_opensearch.py

Requires: OpenSearch running via docker-compose (port 9200).
Requires: corpus/ingestion_output/opensearch_documents.json from Phase 1 ingestion.
"""

import json
import os
import sys
import time
from pathlib import Path

from opensearchpy import OpenSearch, helpers

OPENSEARCH_HOST = os.getenv("OPENSEARCH_HOST", "localhost")
OPENSEARCH_PORT = int(os.getenv("OPENSEARCH_PORT", "9200"))
INDEX_NAME = "mas_regulatory"
EMBEDDING_DIM = 1536
SEARCH_PIPELINE_NAME = "hybrid_rrf_rerank_pipeline"
DOCUMENTS_PATH = Path("corpus/ingestion_output/opensearch_documents.json")

# Cross-encoder model for reranking
CROSS_ENCODER_MODEL_NAME = "huggingface/cross-encoders/ms-marco-MiniLM-L-6-v2"
CROSS_ENCODER_MODEL_VERSION = "1.0.2"


def get_client() -> OpenSearch:
    return OpenSearch(
        hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
        use_ssl=False,
        verify_certs=False,
    )


def wait_for_opensearch(client: OpenSearch, max_retries: int = 30) -> None:
    """Wait for OpenSearch to be ready."""
    for i in range(max_retries):
        try:
            info = client.info()
            print(f"OpenSearch {info['version']['number']} ready")
            return
        except Exception:
            if i < max_retries - 1:
                print(f"Waiting for OpenSearch... ({i + 1}/{max_retries})")
                time.sleep(2)
    raise RuntimeError("OpenSearch not reachable")


def create_index(client: OpenSearch) -> None:
    """Create the mas_regulatory index with kNN mappings."""
    if client.indices.exists(index=INDEX_NAME):
        print(f"Index '{INDEX_NAME}' already exists, deleting...")
        client.indices.delete(index=INDEX_NAME)

    index_body = {
        "settings": {
            "index": {
                "knn": True,
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
        },
        "mappings": {
            "properties": {
                "content": {"type": "text", "analyzer": "standard"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": EMBEDDING_DIM,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "lucene",
                        "parameters": {"ef_construction": 256, "m": 16},
                    },
                },
                "chunk_id": {"type": "keyword"},
                "source_document": {"type": "keyword"},
                "document_type": {"type": "keyword"},
                "section_heading": {"type": "text"},
                "page_number": {"type": "integer"},
                "topic_tags": {"type": "keyword"},
                "category": {"type": "keyword"},
            }
        },
    }

    client.indices.create(index=INDEX_NAME, body=index_body)
    print(f"Index '{INDEX_NAME}' created")


def register_cross_encoder(client: OpenSearch) -> str | None:
    """Register and deploy the cross-encoder model via ML plugin.

    Returns the model_id if successful, None if ML plugin unavailable.
    """
    try:
        # Register model group (or reuse existing one)
        group_body = {
            "name": "msrag_rerankers",
            "description": "Cross-encoder models for MSRAG pipeline",
        }
        try:
            group_resp = client.transport.perform_request(
                "POST", "/_plugins/_ml/model_groups/_register", body=group_body
            )
            model_group_id = group_resp.get("model_group_id")
            print(f"Model group created: {model_group_id}")
        except Exception as e:
            if "already being used" in str(e):
                # Search for existing group
                search_resp = client.transport.perform_request(
                    "POST", "/_plugins/_ml/model_groups/_search",
                    body={"query": {"match": {"name": "msrag_rerankers"}}},
                )
                hits = search_resp.get("hits", {}).get("hits", [])
                if hits:
                    model_group_id = hits[0]["_id"]
                    print(f"Reusing existing model group: {model_group_id}")
                else:
                    raise
            else:
                raise

        # Register model
        register_body = {
            "name": CROSS_ENCODER_MODEL_NAME,
            "version": CROSS_ENCODER_MODEL_VERSION,
            "model_group_id": model_group_id,
            "model_format": "TORCH_SCRIPT",
            "function_name": "TEXT_SIMILARITY",
        }
        register_resp = client.transport.perform_request(
            "POST", "/_plugins/_ml/models/_register", body=register_body
        )
        task_id = register_resp.get("task_id")
        print(f"Model registration task: {task_id}")

        # Wait for registration
        model_id = _wait_for_ml_task(client, task_id, "registration")
        if not model_id:
            return None

        # Deploy model
        deploy_resp = client.transport.perform_request(
            "POST", f"/_plugins/_ml/models/{model_id}/_deploy"
        )
        deploy_task_id = deploy_resp.get("task_id")
        print(f"Model deploy task: {deploy_task_id}")

        # Wait for deployment
        _wait_for_ml_task(client, deploy_task_id, "deployment")

        # Verify deployed
        model_info = client.transport.perform_request(
            "GET", f"/_plugins/_ml/models/{model_id}"
        )
        status = model_info.get("model_state")
        if status == "DEPLOYED":
            print(f"Cross-encoder model deployed: {model_id}")
            return model_id
        else:
            print(f"Model status: {status} (expected DEPLOYED)")
            return None

    except Exception as e:
        print(f"ML plugin unavailable or model registration failed: {e}")
        print("Pipeline will use Python-side reranking as fallback.")
        return None


def _wait_for_ml_task(
    client: OpenSearch, task_id: str, task_type: str, max_wait: int = 120
) -> str | None:
    """Poll ML task until complete. Returns model_id if available."""
    for _ in range(max_wait // 2):
        try:
            task = client.transport.perform_request(
                "GET", f"/_plugins/_ml/tasks/{task_id}"
            )
            state = task.get("state")
            if state == "COMPLETED":
                model_id = task.get("model_id")
                print(f"ML {task_type} completed: model_id={model_id}")
                return model_id
            elif state in ("FAILED", "COMPLETED_WITH_ERROR"):
                print(f"ML {task_type} failed: {task.get('error')}")
                return None
        except Exception:
            pass
        time.sleep(2)
    print(f"ML {task_type} timed out after {max_wait}s")
    return None


def create_search_pipeline(client: OpenSearch, model_id: str | None) -> None:
    """Create the hybrid RRF + rerank search pipeline."""
    pipeline_body: dict = {
        "description": "Hybrid min-max normalization with arithmetic mean combination",
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {"technique": "arithmetic_mean"},
                }
            }
        ],
    }

    # Add cross-encoder reranking if model is deployed
    if model_id:
        pipeline_body["response_processors"] = [
            {
                "rerank": {
                    "ml_opensearch": {
                        "model_id": model_id,
                    },
                    "context": {"document_fields": ["content"]},
                }
            }
        ]
        print(f"Search pipeline includes cross-encoder reranking (model: {model_id})")
    else:
        print("Search pipeline: RRF only (no cross-encoder — using Python fallback)")

    client.transport.perform_request(
        "PUT",
        f"/_search/pipeline/{SEARCH_PIPELINE_NAME}",
        body=pipeline_body,
    )
    print(f"Search pipeline '{SEARCH_PIPELINE_NAME}' created")


def bulk_index_documents(client: OpenSearch) -> int:
    """Bulk-index documents from the ingestion output JSONL file."""
    if not DOCUMENTS_PATH.exists():
        raise FileNotFoundError(f"Documents not found: {DOCUMENTS_PATH}")

    with open(DOCUMENTS_PATH, "r", encoding="utf-8") as f:
        docs = json.load(f)

    print(f"Loaded {len(docs)} documents from {DOCUMENTS_PATH}")

    def generate_actions():
        for doc in docs:
            metadata = doc.get("metadata", {})
            yield {
                "_index": INDEX_NAME,
                "_id": doc["chunk_id"],
                "_source": {
                    "content": doc["content"],
                    "embedding": doc["embedding"],
                    "chunk_id": doc["chunk_id"],
                    "source_document": metadata.get("source_document", ""),
                    "document_type": metadata.get("document_type", ""),
                    "section_heading": metadata.get("section_heading", ""),
                    "page_number": metadata.get("page_number"),
                    "topic_tags": metadata.get("topic_tags", []),
                    "category": metadata.get("category", ""),
                },
            }

    success, errors = helpers.bulk(client, generate_actions(), chunk_size=500)
    if errors:
        print(f"Bulk indexing errors: {errors[:5]}")
    print(f"Indexed {success} documents")

    # Refresh to make documents searchable
    client.indices.refresh(index=INDEX_NAME)
    return success


def verify_index(client: OpenSearch) -> None:
    """Verify index has expected document count and search works."""
    count = client.count(index=INDEX_NAME)["count"]
    print(f"Index document count: {count}")

    # Test hybrid search
    test_query = {
        "size": 3,
        "query": {
            "hybrid": {
                "queries": [
                    {"match": {"content": "AML requirements"}},
                    {
                        "knn": {
                            "embedding": {
                                "vector": [0.0] * EMBEDDING_DIM,
                                "k": 3,
                            }
                        }
                    },
                ]
            }
        },
    }

    try:
        results = client.search(
            index=INDEX_NAME,
            body=test_query,
            params={"search_pipeline": SEARCH_PIPELINE_NAME},
        )
        hits = results["hits"]["total"]["value"]
        print(f"Hybrid search test returned {hits} results")
    except Exception as e:
        print(f"Hybrid search test: {e}")
        # Fall back to simple match
        simple = client.search(
            index=INDEX_NAME, body={"query": {"match": {"content": "AML"}}, "size": 3}
        )
        print(
            f"Simple search fallback: {simple['hits']['total']['value']} results"
        )


def main():
    client = get_client()

    print("=== OpenSearch Setup ===\n")

    # 1. Wait for OpenSearch
    wait_for_opensearch(client)

    # 2. Create index
    print()
    create_index(client)

    # 3. Register cross-encoder model
    print()
    model_id = register_cross_encoder(client)

    # 4. Create search pipeline
    print()
    create_search_pipeline(client, model_id)

    # 5. Bulk index documents
    print()
    indexed = bulk_index_documents(client)

    # 6. Verify
    print()
    verify_index(client)

    print(f"\n=== Setup complete: {indexed} documents indexed ===")

    if not model_id:
        print(
            "\nNote: Cross-encoder not deployed in OpenSearch. "
            "Python-side reranking will be used (sentence-transformers fallback)."
        )


if __name__ == "__main__":
    main()
