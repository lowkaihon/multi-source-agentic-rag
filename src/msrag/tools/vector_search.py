"""OpenSearch client wrapper for hybrid vector/keyword/semantic search."""

from __future__ import annotations

from langchain_core.documents import Document
from openai import OpenAI
from opensearchpy import OpenSearch

SEARCH_PIPELINE_NAME = "hybrid_rrf_rerank_pipeline"


class OpenSearchClient:
    """Wraps OpenSearch with embedding, hybrid search, and optional Python-side reranking."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 9200,
        index_name: str = "mas_regulatory",
        embedding_model: str = "text-embedding-3-small",
        search_pipeline: str = SEARCH_PIPELINE_NAME,
        use_python_reranker: bool = False,
    ):
        self.client = OpenSearch(
            hosts=[{"host": host, "port": port}],
            use_ssl=False,
            verify_certs=False,
            timeout=30,
        )
        self.index_name = index_name
        self.embedding_model = embedding_model
        self.search_pipeline = search_pipeline
        self.openai_client = OpenAI()
        self.use_python_reranker = use_python_reranker
        self._cross_encoder = None

    def embed_query(self, text: str) -> list[float]:
        """Generate embedding for a query using OpenAI."""
        response = self.openai_client.embeddings.create(
            input=text, model=self.embedding_model
        )
        return response.data[0].embedding

    def search(
        self, query: str, mode: str = "hybrid", k: int = 6
    ) -> list[tuple[Document, float]]:
        """Search the index. Returns list of (Document, score) tuples.

        Args:
            query: Search query text.
            mode: "hybrid" (BM25 + kNN + RRF), "keyword" (BM25 only),
                  "semantic" (kNN only).
            k: Number of results to return.
        """
        query_embedding = self.embed_query(query)

        if mode == "keyword":
            body = self._keyword_query(query, k)
            params = {}
        elif mode == "semantic":
            body = self._semantic_query(query_embedding, k)
            params = {}
        else:  # hybrid
            body = self._hybrid_query(query, query_embedding, k)
            params = {"search_pipeline": self.search_pipeline}

        # If using Python reranker, retrieve more candidates
        fetch_k = k * 4 if self.use_python_reranker else k
        body["size"] = fetch_k

        response = self.client.search(
            index=self.index_name, body=body, params=params
        )

        results = []
        for hit in response["hits"]["hits"]:
            source = hit["_source"]
            doc = Document(
                page_content=source.get("content", ""),
                metadata={
                    "chunk_id": source.get("chunk_id", ""),
                    "source_document": source.get("source_document", ""),
                    "document_type": source.get("document_type", ""),
                    "section_heading": source.get("section_heading", ""),
                    "page_number": source.get("page_number"),
                    "topic_tags": source.get("topic_tags", []),
                    "category": source.get("category", ""),
                },
            )
            results.append((doc, hit["_score"]))

        if self.use_python_reranker and results:
            results = self._python_rerank(query, results, k)

        return results[:k]

    def _hybrid_query(
        self, query: str, embedding: list[float], k: int
    ) -> dict:
        body: dict = {
            "query": {
                "hybrid": {
                    "queries": [
                        {"match": {"content": {"query": query}}},
                        {
                            "knn": {
                                "embedding": {
                                    "vector": embedding,
                                    "k": k,
                                }
                            }
                        },
                    ]
                }
            },
        }
        # Include rerank context for the ML cross-encoder in the search pipeline
        if not self.use_python_reranker:
            body["ext"] = {
                "rerank": {
                    "query_context": {
                        "query_text": query,
                    }
                }
            }
        return body

    def _keyword_query(self, query: str, k: int) -> dict:
        return {"query": {"match": {"content": {"query": query}}}}

    def _semantic_query(self, embedding: list[float], k: int) -> dict:
        return {
            "query": {
                "knn": {
                    "embedding": {
                        "vector": embedding,
                        "k": k,
                    }
                }
            }
        }

    def _python_rerank(
        self,
        query: str,
        results: list[tuple[Document, float]],
        k: int,
    ) -> list[tuple[Document, float]]:
        """Fallback: rerank with sentence-transformers CrossEncoder."""
        if self._cross_encoder is None:
            from sentence_transformers import CrossEncoder

            self._cross_encoder = CrossEncoder(
                "cross-encoder/ms-marco-MiniLM-L-6-v2"
            )

        pairs = [(query, doc.page_content) for doc, _ in results]
        scores = self._cross_encoder.predict(pairs)

        reranked = [
            (doc, float(score))
            for (doc, _), score in zip(results, scores)
        ]
        reranked.sort(key=lambda x: x[1], reverse=True)
        return reranked[:k]

    def health_check(self) -> dict:
        """Check OpenSearch connectivity and index status."""
        info = self.client.info()
        count = self.client.count(index=self.index_name)["count"]
        return {
            "version": info["version"]["number"],
            "index": self.index_name,
            "doc_count": count,
        }
