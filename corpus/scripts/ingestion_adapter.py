#!/usr/bin/env python3
"""
MAS Regulatory Compliance — Ingestion Adapter
==============================================
Phase 1, Step 6: The architectural boundary between domain-specific code
and the domain-agnostic pipeline.

Produces three outputs per the spec (Section 7):
  1. opensearch_documents: List[Document]    — chunked, embedded, ready to index
  2. sql_records: Dict[table_name, List[Row]] — structured data ready to INSERT
  3. metadata_manifest: Dict                  — corpus stats for pipeline config

This adapter handles:
  - PDF text extraction (PyMuPDF / pdfplumber)
  - Chunking with multiple strategies (recursive character, section-aware)
  - Metadata extraction (section headings, page numbers, document type)
  - Embedding computation (OpenAI text-embedding-3-small)
  - SQL record loading from parsed structured data
  - Manifest generation for agent tool description construction

Usage:
    python ingestion_adapter.py \
        --pdf-dir ./pdfs \
        --sql-seed-dir ./data/sql/seed \
        --output-dir ./ingestion_output \
        --embedding-model text-embedding-3-small \
        --chunk-strategy recursive_character \
        --chunk-size 1000 \
        --chunk-overlap 200

Prerequisites:
    pip install pymupdf tiktoken langchain-text-splitters openai
"""

import json
import os
import re
import sys
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional, Literal

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ingestion")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
DEFAULT_PDF_DIR = SCRIPT_DIR.parent / "pdfs"
DEFAULT_SQL_SEED = SCRIPT_DIR.parent / "data" / "sql" / "seed"
DEFAULT_OUTPUT = SCRIPT_DIR.parent / "ingestion_output"
DEFAULT_MANIFEST_PATH = SCRIPT_DIR.parent / "manifests" / "corpus_manifest.json"

# Document type detection from filename patterns
DOC_TYPE_PATTERNS = {
    "notice": [r"notice_", r"mas_notice"],
    "guideline": [r"guidelines_", r"guideline_"],
    "consultation_paper": [r"consultation_", r"response_.*consultation"],
    "enforcement_report": [r"enforcement_report", r"enforcement_monograph"],
    "information_paper": [r"info_paper", r"amlcft_", r"guidance_", r"feat_", r"mindforge"],
    "circular": [r"circular_"],
}

# Topic tag extraction keywords
TOPIC_KEYWORDS = {
    "aml_cft": ["money laundering", "aml", "cft", "terrorism financing", "cdd", "kyc",
                 "suspicious transaction", "notice 626", "beneficial owner"],
    "technology_risk": ["technology risk", "trm", "cyber", "it security", "cloud",
                        "incident", "system", "network"],
    "outsourcing": ["outsourcing", "outsource", "service provider", "vendor",
                    "third party", "third-party"],
    "ai_governance": ["artificial intelligence", "ai ", "machine learning", "ml ",
                      "model risk", "feat", "fairness", "explainability", "generative ai"],
    "business_continuity": ["business continuity", "bcm", "disaster recovery", "resilience",
                            "rto", "rpo"],
    "enforcement": ["enforcement", "penalty", "prohibition order", "reprimand",
                    "civil penalty", "composition"],
    "business_conduct": ["fair dealing", "conduct", "accountability", "iac",
                         "customer protection", "disclosure"],
    "sanctions": ["sanctions", "screening", "cosmic", "targeted financial"],
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ChunkMetadata:
    source_document: str
    document_type: str
    section_heading: str
    page_number: int
    chunk_index: int
    topic_tags: list[str]


@dataclass
class OpenSearchDocument:
    chunk_id: str
    content: str
    embedding: list[float]     # Placeholder — populated by embedding step
    metadata: dict


@dataclass
class MetadataManifest:
    corpus_name: str
    document_count: int
    chunk_count: int
    sql_tables: list[str]
    sql_row_counts: dict[str, int]
    last_ingested: str
    embedding_model: str
    chunk_strategy: str


# ---------------------------------------------------------------------------
# PDF Text Extraction
# ---------------------------------------------------------------------------

def extract_text_pymupdf(pdf_path: Path) -> list[dict]:
    """
    Extract text from PDF using PyMuPDF (fitz), page by page.
    Returns list of {page_number: int, text: str}.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ImportError("PyMuPDF required: pip install pymupdf")

    doc = fitz.open(str(pdf_path))
    pages = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")
        if text.strip():
            pages.append({"page_number": page_num + 1, "text": text})
    doc.close()
    return pages


def extract_text_markdown(md_path: Path) -> list[dict]:
    """Extract text from markdown files."""
    text = md_path.read_text(encoding="utf-8")
    # Treat entire markdown as single page
    return [{"page_number": 1, "text": text}]


def extract_text(file_path: Path) -> list[dict]:
    """Route to appropriate text extraction based on file type."""
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return extract_text_pymupdf(file_path)
    elif suffix in (".md", ".txt"):
        return extract_text_markdown(file_path)
    else:
        log.warning(f"Unsupported file type: {suffix} for {file_path.name}")
        return []


# ---------------------------------------------------------------------------
# Section heading extraction
# ---------------------------------------------------------------------------

def extract_section_headings(text: str) -> list[tuple[int, str]]:
    """
    Extract section headings and their character positions from text.
    Handles numbered sections (e.g., "3.1 Access Controls") and
    markdown headings (e.g., "## 3. Governance").
    """
    patterns = [
        # Numbered sections: "3.1 Title", "3.1.2 Title"
        r'^(\d+(?:\.\d+)*)\s+([A-Z][^\n]{3,80})',
        # Markdown headings
        r'^#{1,4}\s+(.+)',
        # ALL CAPS headings (common in MAS docs)
        r'^([A-Z][A-Z\s]{5,80})$',
    ]
    headings = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.MULTILINE):
            heading_text = match.group(0).strip().lstrip("#").strip()
            headings.append((match.start(), heading_text[:100]))

    headings.sort(key=lambda x: x[0])
    return headings


def get_section_for_position(position: int, headings: list[tuple[int, str]]) -> str:
    """Find the nearest preceding section heading for a given text position."""
    current_section = "Document Start"
    for heading_pos, heading_text in headings:
        if heading_pos <= position:
            current_section = heading_text
        else:
            break
    return current_section


# ---------------------------------------------------------------------------
# Chunking Strategies
# ---------------------------------------------------------------------------

def chunk_recursive_character(
    pages: list[dict],
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> list[dict]:
    """
    Recursive character splitting with page boundary awareness.
    Splits on paragraph boundaries first, then sentence boundaries,
    then word boundaries.
    """
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    except ImportError:
        raise ImportError("langchain-text-splitters required: pip install langchain-text-splitters")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
        length_function=len,
    )

    # Concatenate all pages with page markers
    full_text = ""
    page_boundaries = []  # (char_position, page_number)
    for page in pages:
        page_boundaries.append((len(full_text), page["page_number"]))
        full_text += page["text"] + "\n\n"

    # Extract section headings from full text
    headings = extract_section_headings(full_text)

    # Split
    text_chunks = splitter.split_text(full_text)

    chunks = []
    char_pos = 0
    for i, chunk_text in enumerate(text_chunks):
        # Find the chunk's position in the original text
        chunk_start = full_text.find(chunk_text, char_pos)
        if chunk_start == -1:
            chunk_start = char_pos  # Fallback
        char_pos = chunk_start + 1

        # Determine page number
        page_num = 1
        for boundary_pos, pn in page_boundaries:
            if boundary_pos <= chunk_start:
                page_num = pn
            else:
                break

        # Determine section heading
        section = get_section_for_position(chunk_start, headings)

        chunks.append({
            "chunk_index": i,
            "content": chunk_text.strip(),
            "page_number": page_num,
            "section_heading": section,
        })

    return chunks


def chunk_section_aware(
    pages: list[dict],
    max_chunk_size: int = 1500,
    min_chunk_size: int = 200,
) -> list[dict]:
    """
    Section-aware splitting: split at section boundaries first,
    then apply recursive splitting to oversized sections.
    Better for structured regulatory documents with numbered paragraphs.
    """
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    except ImportError:
        raise ImportError("langchain-text-splitters required: pip install langchain-text-splitters")

    full_text = ""
    page_boundaries = []
    for page in pages:
        page_boundaries.append((len(full_text), page["page_number"]))
        full_text += page["text"] + "\n\n"

    headings = extract_section_headings(full_text)

    if not headings:
        # Fall back to recursive character splitting
        return chunk_recursive_character(pages, chunk_size=max_chunk_size)

    # Split text into sections
    sections = []
    for i, (pos, heading) in enumerate(headings):
        end_pos = headings[i + 1][0] if i + 1 < len(headings) else len(full_text)
        section_text = full_text[pos:end_pos].strip()
        if section_text:
            sections.append({
                "heading": heading,
                "text": section_text,
                "start_pos": pos,
            })

    # Process each section
    sub_splitter = RecursiveCharacterTextSplitter(
        chunk_size=max_chunk_size,
        chunk_overlap=100,
        separators=["\n\n", "\n", ". ", " "],
    )

    chunks = []
    chunk_idx = 0

    for section in sections:
        if len(section["text"]) <= max_chunk_size:
            # Section fits in one chunk
            if len(section["text"]) >= min_chunk_size:
                page_num = 1
                for bp, pn in page_boundaries:
                    if bp <= section["start_pos"]:
                        page_num = pn
                chunks.append({
                    "chunk_index": chunk_idx,
                    "content": section["text"],
                    "page_number": page_num,
                    "section_heading": section["heading"],
                })
                chunk_idx += 1
        else:
            # Section too large — sub-split
            sub_chunks = sub_splitter.split_text(section["text"])
            for sc in sub_chunks:
                sc_pos = full_text.find(sc[:50], section["start_pos"])
                page_num = 1
                for bp, pn in page_boundaries:
                    if bp <= (sc_pos if sc_pos >= 0 else section["start_pos"]):
                        page_num = pn
                chunks.append({
                    "chunk_index": chunk_idx,
                    "content": sc.strip(),
                    "page_number": page_num,
                    "section_heading": section["heading"],
                })
                chunk_idx += 1

    return chunks


CHUNK_STRATEGIES = {
    "recursive_character": chunk_recursive_character,
    "section_aware": chunk_section_aware,
}


# ---------------------------------------------------------------------------
# Topic tagging
# ---------------------------------------------------------------------------

def extract_topic_tags(text: str) -> list[str]:
    """Extract topic tags from chunk text using keyword matching."""
    text_lower = text.lower()
    tags = []
    for tag, keywords in TOPIC_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            tags.append(tag)
    return tags


# ---------------------------------------------------------------------------
# Document type detection
# ---------------------------------------------------------------------------

def detect_document_type(filename: str) -> str:
    """Detect document type from filename patterns."""
    filename_lower = filename.lower()
    for doc_type, patterns in DOC_TYPE_PATTERNS.items():
        if any(re.search(p, filename_lower) for p in patterns):
            return doc_type
    return "guideline"  # Default


# ---------------------------------------------------------------------------
# Embedding (placeholder — requires API key)
# ---------------------------------------------------------------------------

def compute_embeddings_batch(
    texts: list[str],
    model: str = "text-embedding-3-small",
    batch_size: int = 100,
) -> list[list[float]]:
    """
    Compute embeddings using OpenAI API.
    Returns placeholder zero vectors if API key is not set.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("OPENAI_API_KEY not set — using placeholder zero vectors")
        dim = 1536 if "small" in model else 3072
        return [[0.0] * dim for _ in texts]

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        all_embeddings = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            response = client.embeddings.create(model=model, input=batch)
            batch_embeddings = [item.embedding for item in response.data]
            all_embeddings.extend(batch_embeddings)
            log.info(f"  Embedded batch {i // batch_size + 1}/{(len(texts) - 1) // batch_size + 1}")

        return all_embeddings
    except Exception as e:
        log.error(f"Embedding failed: {e}")
        dim = 1536 if "small" in model else 3072
        return [[0.0] * dim for _ in texts]


# ---------------------------------------------------------------------------
# Main ingestion adapter
# ---------------------------------------------------------------------------

def ingest(
    pdf_dir: Path = DEFAULT_PDF_DIR,
    sql_seed_dir: Path = DEFAULT_SQL_SEED,
    output_dir: Path = DEFAULT_OUTPUT,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    embedding_model: str = "text-embedding-3-small",
    chunk_strategy: str = "recursive_character",
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> dict:
    """
    Main ingestion function. Produces the three required outputs:
      1. opensearch_documents
      2. sql_records
      3. metadata_manifest
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    chunk_fn = CHUNK_STRATEGIES[chunk_strategy]

    log.info("=" * 60)
    log.info("MAS Regulatory Compliance — Ingestion Adapter")
    log.info("=" * 60)
    log.info(f"  PDF directory:     {pdf_dir}")
    log.info(f"  Chunk strategy:    {chunk_strategy}")
    log.info(f"  Chunk size:        {chunk_size}")
    log.info(f"  Chunk overlap:     {chunk_overlap}")
    log.info(f"  Embedding model:   {embedding_model}")

    # -----------------------------------------------------------------------
    # Output 1: OpenSearch Documents
    # -----------------------------------------------------------------------
    log.info("\n[1/3] Processing PDFs → OpenSearch documents...")

    all_chunks = []
    document_count = 0
    pdf_categories = ["core_guidelines", "consultation_papers", "enforcement_reports",
                      "info_papers_circulars"]

    for category in pdf_categories:
        category_dir = pdf_dir / category
        if not category_dir.exists():
            continue

        files = sorted(category_dir.glob("*"))
        for file_path in files:
            if file_path.suffix.lower() not in (".pdf", ".md", ".txt"):
                continue

            log.info(f"  Processing: {file_path.name}")
            document_count += 1

            # Extract text
            try:
                pages = extract_text(file_path)
            except Exception as e:
                log.error(f"    Failed to extract text: {e}")
                continue

            if not pages:
                log.warning(f"    No text extracted from {file_path.name}")
                continue

            # Detect document type
            doc_type = detect_document_type(file_path.name)

            # Chunk
            if chunk_strategy == "section_aware":
                chunks = chunk_fn(pages, max_chunk_size=chunk_size)
            else:
                chunks = chunk_fn(pages, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

            log.info(f"    → {len(chunks)} chunks ({len(pages)} pages)")

            # Build OpenSearch document records
            for chunk in chunks:
                topic_tags = extract_topic_tags(chunk["content"])
                chunk_id = hashlib.md5(
                    f"{file_path.name}:{chunk['chunk_index']}".encode()
                ).hexdigest()

                all_chunks.append({
                    "chunk_id": chunk_id,
                    "content": chunk["content"],
                    "embedding": [],  # Populated in embedding step
                    "metadata": {
                        "source_document": file_path.name,
                        "document_type": doc_type,
                        "section_heading": chunk.get("section_heading", ""),
                        "page_number": chunk.get("page_number", 1),
                        "chunk_index": chunk["chunk_index"],
                        "topic_tags": topic_tags,
                        "category": category,
                    },
                })

    log.info(f"\n  Total: {document_count} documents → {len(all_chunks)} chunks")

    # Compute embeddings
    log.info(f"\n  Computing embeddings ({embedding_model})...")
    texts = [c["content"] for c in all_chunks]
    embeddings = compute_embeddings_batch(texts, model=embedding_model)
    for i, emb in enumerate(embeddings):
        all_chunks[i]["embedding"] = emb

    # Save opensearch documents
    os_docs_path = output_dir / "opensearch_documents.json"
    with open(os_docs_path, "w") as f:
        json.dump(all_chunks, f, indent=2)
    log.info(f"  Saved to: {os_docs_path}")

    # -----------------------------------------------------------------------
    # Output 2: SQL Records
    # -----------------------------------------------------------------------
    log.info("\n[2/3] Loading SQL seed data...")

    sql_records = {}
    sql_row_counts = {}
    for table_name in ["enforcement_actions", "regulatory_instruments", "regulated_entities"]:
        json_path = sql_seed_dir / f"{table_name}.json"
        if json_path.exists():
            with open(json_path) as f:
                records = json.load(f)
            sql_records[table_name] = records
            sql_row_counts[table_name] = len(records)
            log.info(f"  {table_name}: {len(records)} records")
        else:
            sql_records[table_name] = []
            sql_row_counts[table_name] = 0
            log.warning(f"  {table_name}: no seed data found at {json_path}")

    sql_records_path = output_dir / "sql_records.json"
    with open(sql_records_path, "w") as f:
        json.dump(sql_records, f, indent=2, default=str)
    log.info(f"  Saved to: {sql_records_path}")

    # -----------------------------------------------------------------------
    # Output 3: Metadata Manifest
    # -----------------------------------------------------------------------
    log.info("\n[3/3] Generating metadata manifest...")

    manifest = {
        "corpus_name": "MAS Regulatory Compliance",
        "document_count": document_count,
        "chunk_count": len(all_chunks),
        "sql_tables": list(sql_records.keys()),
        "sql_row_counts": sql_row_counts,
        "last_ingested": datetime.now().isoformat() + "Z",
        "embedding_model": embedding_model,
        "chunk_strategy": f"{chunk_strategy}_{chunk_size}_{chunk_overlap}",

        # Agent tool description construction data (spec Section 7)
        "tool_description_context": {
            "vector_store_summary": (
                f"You have access to {document_count} MAS regulatory documents "
                f"({len(all_chunks)} chunks) indexed in the vector store. "
                f"Documents include notices, guidelines, consultation papers, "
                f"enforcement reports, information papers, and internal policy documents. "
                f"Topics covered: AML/CFT, technology risk, outsourcing, AI governance, "
                f"business continuity, enforcement, business conduct, and sanctions."
            ),
            "sql_summary": (
                f"You have access to a SQL database with: "
                f"{sql_row_counts.get('enforcement_actions', 0)} enforcement action records, "
                f"{sql_row_counts.get('regulatory_instruments', 0)} regulatory instrument records, "
                f"and {sql_row_counts.get('regulated_entities', 0)} regulated entity records. "
                f"Use SQL for questions about counts, amounts, dates, filtered lists, "
                f"or structured comparisons."
            ),
            "web_search_summary": (
                f"The corpus was last updated on "
                f"{datetime.now().strftime('%d %B %Y')}. "
                f"Queries about events after this date, or about topics not covered "
                f"by the indexed corpus (e.g., cross-jurisdictional comparisons, "
                f"industry commentary), may need web search."
            ),
        },

        # Document type distribution for debugging
        "document_type_distribution": {},
        "topic_tag_distribution": {},
    }

    # Compute distributions
    type_dist = {}
    tag_dist = {}
    for chunk in all_chunks:
        dt = chunk["metadata"]["document_type"]
        type_dist[dt] = type_dist.get(dt, 0) + 1
        for tag in chunk["metadata"]["topic_tags"]:
            tag_dist[tag] = tag_dist.get(tag, 0) + 1
    manifest["document_type_distribution"] = type_dist
    manifest["topic_tag_distribution"] = tag_dist

    manifest_path_out = output_dir / "metadata_manifest.json"
    with open(manifest_path_out, "w") as f:
        json.dump(manifest, f, indent=2)
    log.info(f"  Saved to: {manifest_path_out}")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    log.info("\n" + "=" * 60)
    log.info("INGESTION COMPLETE")
    log.info("=" * 60)
    log.info(f"  Documents processed:  {document_count}")
    log.info(f"  Chunks created:       {len(all_chunks)}")
    log.info(f"  SQL tables loaded:    {len(sql_records)}")
    log.info(f"  Total SQL rows:       {sum(sql_row_counts.values())}")
    log.info(f"  Output directory:     {output_dir}")
    log.info("=" * 60)

    return {
        "opensearch_documents": all_chunks,
        "sql_records": sql_records,
        "metadata_manifest": manifest,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="MAS Compliance Corpus Ingestion Adapter")
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument("--sql-seed-dir", type=Path, default=DEFAULT_SQL_SEED)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--embedding-model", default="text-embedding-3-small")
    parser.add_argument("--chunk-strategy", default="recursive_character",
                        choices=["recursive_character", "section_aware"])
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--chunk-overlap", type=int, default=200)
    args = parser.parse_args()

    ingest(
        pdf_dir=args.pdf_dir,
        sql_seed_dir=args.sql_seed_dir,
        output_dir=args.output_dir,
        manifest_path=args.manifest,
        embedding_model=args.embedding_model,
        chunk_strategy=args.chunk_strategy,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
    )


if __name__ == "__main__":
    main()
