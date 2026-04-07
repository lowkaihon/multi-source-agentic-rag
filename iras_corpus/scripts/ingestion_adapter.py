#!/usr/bin/env python3
"""
IRAS Corporate Tax Compliance — Ingestion Adapter
==================================================
Phase 1, Step 6 of Project 2 (IRAS swap): the architectural boundary between
domain-specific code and the domain-agnostic pipeline.

Produces the three outputs the pipeline already consumes (contract shapes
defined in project2_domain_spec.md Section 7):

  1. opensearch_documents: List[Document]    - chunked, embedded, ready to index
  2. sql_records:          Dict[table_name, List[Row]] - structured data ready to INSERT
  3. metadata_manifest:    Dict              - corpus stats for pipeline config

This adapter is structurally identical to mas-corpus/scripts/ingestion_adapter.py
but with these IRAS-specific differences:

  - DOC_TYPE_PATTERNS: just "e_tax_guide" (instead of MAS's six types)
  - TOPIC_KEYWORDS: tax-domain vocabulary (corporate_tax, transfer_pricing,
    withholding_tax, dta, anti_avoidance, tax_incentive, substance_requirements,
    tax_administration)
  - SQL tables: tax_rates, tax_instruments, double_tax_agreements
  - Manifest pdf_categories: matches the IRAS manifest's category names
  - corpus_name in the output manifest

Everything else (chunking strategies, section heading extraction, embedding
computation, output file shapes) is byte-for-byte the same logic as the MAS
adapter, because the contract IS the same. The whole point of the swap.

Usage:
    python ingestion_adapter.py \\
        --pdf-dir ./pdfs \\
        --sql-seed-dir ./data/sql/seed \\
        --output-dir ./ingestion_output \\
        --embedding-model text-embedding-3-small \\
        --chunk-strategy recursive_character \\
        --chunk-size 1000 \\
        --chunk-overlap 200

Prerequisites:
    pip install pymupdf langchain-text-splitters openai
"""

import json
import os
import re
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ingestion")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
CORPUS_DIR = SCRIPT_DIR.parent  # Pre-fix from MAS Phase 1
DEFAULT_PDF_DIR = CORPUS_DIR / "pdfs"
DEFAULT_SQL_SEED = CORPUS_DIR / "data" / "sql" / "seed"
DEFAULT_SQL_DDL = CORPUS_DIR / "data" / "sql" / "init_schema.sql"
DEFAULT_OUTPUT = CORPUS_DIR / "ingestion_output"
DEFAULT_MANIFEST_PATH = CORPUS_DIR / "manifests" / "iras_corpus_manifest.json"

# Document type detection from filename patterns. IRAS only has one type in
# this corpus — e_tax_guide. The pattern dict is kept (instead of hardcoding)
# so future expansion (circulars, rulings, ITA sections) is one line away.
DOC_TYPE_PATTERNS = {
    "e_tax_guide": [r"^etg_", r"_etg_", r"e[_-]?tax[_-]?guide"],
    "circular":    [r"_circular_", r"^circular_"],
    "ruling":      [r"_ruling_", r"^advance_ruling_"],
    "income_tax_act_section": [r"^ita_s\d+", r"_ita_section_"],
}

# Topic tag extraction keywords. IRAS-specific vocabulary mirroring the
# MAS TOPIC_KEYWORDS structure. Per-chunk keyword matching, NOT per-document
# tags from the registry — same convention as MAS, gives ~70%+ tagging coverage.
TOPIC_KEYWORDS = {
    "corporate_tax":          ["corporate income tax", "chargeable income", "year of assessment",
                                "form c", "tax computation", "company tax", "cit ",
                                "headline tax rate", "trade or business"],
    "transfer_pricing":       ["transfer pricing", "arm's length", "arms length", "related party",
                                "tpd", "comparability analysis", "advance pricing arrangement",
                                "apa ", "tnmm", "cup method", "cost plus", "resale price",
                                "intercompany", "intra-group", "intragroup"],
    "withholding_tax":        ["withholding tax", "wht ", "section 45", "non-resident",
                                "non resident", "royalty payment", "interest payment",
                                "technical fee", "service fee paid"],
    "dta":                    ["double tax agreement", "double taxation", "treaty partner",
                                " dta ", "tax treaty", "treaty rate", "mutual agreement procedure",
                                "competent authority", "permanent establishment", " mli "],
    "anti_avoidance":         ["section 33", "general anti-avoidance", "gaar", "tax avoidance",
                                "tax avoidance arrangement", "scheme and purpose",
                                "main purpose condition", "bona fide commercial",
                                "artificial", "contrived"],
    "tax_incentive":          ["tax exemption", "tax incentive", "section 13", "pioneer",
                                "development and expansion", "fund tax incentive",
                                "13o", "13u", "tax holiday", "concessionary rate"],
    "substance_requirements": ["economic substance", "core income generating",
                                "substantive business activities", "substance requirement",
                                "section 10l", "section 10(l)", "qualifying entity",
                                "headquarters", "place of effective management"],
    "tax_administration":     ["mytax portal", "form c-s", "form c filing", "estimated chargeable income",
                                " eci ", "objection", "notice of assessment", " noa ",
                                "filing due date", "audit", "voluntary disclosure"],
}


# ---------------------------------------------------------------------------
# Data classes (contract shapes — DO NOT modify without spec change)
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
    embedding: list[float]
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
    """Extract text from PDF using PyMuPDF (fitz), page by page."""
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
    text = md_path.read_text(encoding="utf-8")
    return [{"page_number": 1, "text": text}]


def extract_text(file_path: Path) -> list[dict]:
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
# IRAS e-Tax Guides use the same numbered-heading format as MAS Notices
# ("5.3 Documentation requirements"), so the regex transfers directly.
# Validated against the Transfer Pricing Guidelines and the Section 13(12)
# guide in the web session.

def extract_section_headings(text: str) -> list[tuple[int, str]]:
    """Extract section headings and their character positions from text."""
    patterns = [
        # Numbered sections: "3.1 Title", "3.1.2 Title"
        r'^(\d+(?:\.\d+)*)\s+([A-Z][^\n]{3,80})',
        # Markdown headings (markdown intake path)
        r'^#{1,4}\s+(.+)',
        # ALL CAPS headings (Annex titles, "TABLE OF CONTENTS", etc.)
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
    """Recursive character splitting with page boundary awareness."""
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

    full_text = ""
    page_boundaries = []
    for page in pages:
        page_boundaries.append((len(full_text), page["page_number"]))
        full_text += page["text"] + "\n\n"

    headings = extract_section_headings(full_text)
    text_chunks = splitter.split_text(full_text)

    chunks = []
    char_pos = 0
    for i, chunk_text in enumerate(text_chunks):
        chunk_start = full_text.find(chunk_text, char_pos)
        if chunk_start == -1:
            chunk_start = char_pos
        char_pos = chunk_start + 1

        page_num = 1
        for boundary_pos, pn in page_boundaries:
            if boundary_pos <= chunk_start:
                page_num = pn
            else:
                break

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
    """Section-aware splitting — better for IRAS guides with worked examples
    that should not be split mid-calculation. If MAS Phase 1 retro showed
    section-aware was useful for any structured doc, IRAS will benefit even
    more given Annex A of the Transfer Pricing Guidelines (6 worked examples).
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
        return chunk_recursive_character(pages, chunk_size=max_chunk_size)

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

    sub_splitter = RecursiveCharacterTextSplitter(
        chunk_size=max_chunk_size,
        chunk_overlap=100,
        separators=["\n\n", "\n", ". ", " "],
    )

    chunks = []
    chunk_idx = 0
    for section in sections:
        if len(section["text"]) <= max_chunk_size:
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
# SQL schema description extraction (project2_domain_spec.md Section 7)
# ---------------------------------------------------------------------------
# Per the new Section 7 contract, the manifest must include a
# `sql_schema_description` field that the agent uses at startup to learn the
# table structure and CHECK constraint values. Without these, the agent
# generates SQL with invalid filter values (e.g., 'income tax' instead of
# 'corporate_income_tax' for rate_category).
#
# The schema is extracted from init_schema.sql DDL at adapter runtime — the
# database isn't populated yet, so information_schema isn't an option.
#
# Code provenance: this function and its helper are copied verbatim from
# src/msrag/tools/builder.py in the pipeline. In the current MAS setup, the
# pipeline calls this at startup against a hardcoded DDL path; the IRAS swap
# moves the call into the adapter so the manifest carries the description
# and the pipeline becomes truly DDL-agnostic. The MAS adapter should be
# updated to do the same as a follow-up — see HANDOFF_PROMPT.md hidden
# assumption audit findings. Until then, the function is duplicated here
# rather than imported from the pipeline, so the iras_corpus/ plugin
# remains a self-contained drop-in.

def _extract_check_values(table_body: str, column_name: str) -> list[str]:
    """Extract CHECK constraint values for a column from the table body."""
    # Pattern: CHECK (column_name IN ('val1', 'val2', ...))
    pattern = rf"CHECK\s*\(\s*{re.escape(column_name)}\s+IN\s*\((.*?)\)\s*\)"
    match = re.search(pattern, table_body, re.DOTALL)
    if match:
        values_str = match.group(1)
        return re.findall(r"'([^']+)'", values_str)
    return []


def extract_sql_schema_description(ddl_path: "str | Path") -> str:
    """Parse init_schema.sql to extract tables, columns, types, and CHECK constraint values.

    Critical: without CHECK values, the agent generates SQL with invalid filter values.
    """
    ddl = Path(ddl_path).read_text(encoding="utf-8")

    tables = []
    # Match CREATE TABLE blocks
    for match in re.finditer(
        r"CREATE TABLE (\w+)\s*\((.*?)\);",
        ddl,
        re.DOTALL,
    ):
        table_name = match.group(1)
        body = match.group(2)

        columns = []
        # Parse each column definition — character class includes single quotes
        # to handle DEFAULT 'value' patterns (e.g., penalty_currency TEXT DEFAULT 'SGD')
        for col_match in re.finditer(
            r"^\s+(\w+)\s+([\w\[\]()., ']+?)(?:\s+--\s*(.+?))?$",
            body,
            re.MULTILINE,
        ):
            col_name = col_match.group(1)
            col_type_raw = col_match.group(2).strip()
            comment = col_match.group(3) or ""

            # Skip constraints that aren't column definitions
            if col_name.upper() in ("CHECK", "PRIMARY", "UNIQUE", "FOREIGN", "CREATE"):
                continue

            # Clean up type (remove NOT NULL, DEFAULT ..., PRIMARY KEY, trailing commas)
            col_type = col_type_raw.split("NOT NULL")[0].split("DEFAULT")[0].strip()
            col_type = re.sub(r"\s*PRIMARY\s+KEY", "", col_type)
            col_type = col_type.rstrip(",").strip()
            col_type = re.sub(r"\s+", " ", col_type)

            # Look for CHECK constraint on this column
            check_values = _extract_check_values(body, col_name)

            col_desc = f"  - {col_name} ({col_type})"
            if check_values:
                col_desc += f"  -- allowed values: {', '.join(check_values)}"
            elif comment:
                col_desc += f"  -- {comment.strip()}"

            columns.append(col_desc)

        if columns:
            tables.append(f"Table: {table_name}\n" + "\n".join(columns))

    # Extract views
    views = []
    for match in re.finditer(
        r"CREATE OR REPLACE VIEW (\w+) AS\s*(SELECT.*?);",
        ddl,
        re.DOTALL,
    ):
        view_name = match.group(1)
        views.append(f"View: {view_name}")

    result = "\n\n".join(tables)
    if views:
        result += "\n\n" + "\n".join(views)
    return result


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
    filename_lower = filename.lower()
    for doc_type, patterns in DOC_TYPE_PATTERNS.items():
        if any(re.search(p, filename_lower) for p in patterns):
            return doc_type
    return "e_tax_guide"  # Default — most things in this corpus


# ---------------------------------------------------------------------------
# Embedding (placeholder when no API key — same fallback as MAS adapter)
# ---------------------------------------------------------------------------

def compute_embeddings_batch(
    texts: list[str],
    model: str = "text-embedding-3-small",
    batch_size: int = 100,
) -> list[list[float]]:
    """Compute embeddings using OpenAI API. Returns placeholder zero vectors
    if API key is not set — same convention as MAS adapter, allows pipeline
    structural validation without an API key."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("OPENAI_API_KEY not set - using placeholder zero vectors")
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
    sql_ddl_path: Path = DEFAULT_SQL_DDL,
    output_dir: Path = DEFAULT_OUTPUT,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    embedding_model: str = "text-embedding-3-small",
    chunk_strategy: str = "recursive_character",
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> dict:
    """Main ingestion function. Produces the three required outputs."""

    output_dir.mkdir(parents=True, exist_ok=True)
    chunk_fn = CHUNK_STRATEGIES[chunk_strategy]

    log.info("=" * 60)
    log.info("IRAS Corporate Tax Compliance - Ingestion Adapter")
    log.info("=" * 60)
    log.info(f"  PDF directory:     {pdf_dir}")
    log.info(f"  Chunk strategy:    {chunk_strategy}")
    log.info(f"  Chunk size:        {chunk_size}")
    log.info(f"  Chunk overlap:     {chunk_overlap}")
    log.info(f"  Embedding model:   {embedding_model}")

    # -----------------------------------------------------------------------
    # Output 1: OpenSearch Documents
    # -----------------------------------------------------------------------
    log.info("\n[1/3] Processing PDFs -> OpenSearch documents...")

    all_chunks = []
    document_count = 0

    # Pull category names from the manifest rather than hardcoding — this is
    # one of the hidden assumptions worth fixing in the swap
    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)
        pdf_categories = list(manifest_data.get("documents", {}).keys())
    else:
        # Fallback: scan pdf_dir for any subdirectories
        pdf_categories = [d.name for d in pdf_dir.iterdir() if d.is_dir()] if pdf_dir.exists() else []

    log.info(f"  Categories: {pdf_categories}")

    for category in pdf_categories:
        category_dir = pdf_dir / category
        if not category_dir.exists():
            log.warning(f"  Category dir not found: {category_dir}")
            continue

        files = sorted(category_dir.glob("*"))
        for file_path in files:
            if file_path.suffix.lower() not in (".pdf", ".md", ".txt"):
                continue

            log.info(f"  Processing: {file_path.name}")
            document_count += 1

            try:
                pages = extract_text(file_path)
            except Exception as e:
                log.error(f"    Failed to extract text: {e}")
                continue

            if not pages:
                log.warning(f"    No text extracted from {file_path.name}")
                continue

            doc_type = detect_document_type(file_path.name)

            if chunk_strategy == "section_aware":
                chunks = chunk_fn(pages, max_chunk_size=chunk_size)
            else:
                chunks = chunk_fn(pages, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

            log.info(f"    -> {len(chunks)} chunks ({len(pages)} pages)")

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

    log.info(f"\n  Total: {document_count} documents -> {len(all_chunks)} chunks")

    # Compute embeddings
    log.info(f"\n  Computing embeddings ({embedding_model})...")
    texts = [c["content"] for c in all_chunks]
    embeddings = compute_embeddings_batch(texts, model=embedding_model)
    for i, emb in enumerate(embeddings):
        all_chunks[i]["embedding"] = emb

    os_docs_path = output_dir / "opensearch_documents.json"
    with open(os_docs_path, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, indent=2)
    log.info(f"  Saved to: {os_docs_path}")

    # -----------------------------------------------------------------------
    # Output 2: SQL Records
    # -----------------------------------------------------------------------
    log.info("\n[2/3] Loading SQL seed data...")

    sql_records = {}
    sql_row_counts = {}
    # IRAS table set — different names from MAS but same role
    for table_name in ["tax_rates", "tax_instruments", "double_tax_agreements"]:
        json_path = sql_seed_dir / f"{table_name}.json"
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                records = json.load(f)
            sql_records[table_name] = records
            sql_row_counts[table_name] = len(records)
            log.info(f"  {table_name}: {len(records)} records")
        else:
            sql_records[table_name] = []
            sql_row_counts[table_name] = 0
            log.warning(f"  {table_name}: no seed data found at {json_path}")

    sql_records_path = output_dir / "sql_records.json"
    with open(sql_records_path, "w", encoding="utf-8") as f:
        json.dump(sql_records, f, indent=2, default=str)
    log.info(f"  Saved to: {sql_records_path}")

    # -----------------------------------------------------------------------
    # Output 3: Metadata Manifest
    # -----------------------------------------------------------------------
    log.info("\n[3/3] Generating metadata manifest...")

    # Per spec Section 7: extract schema description from DDL at runtime so
    # the agent's system prompt has table structure + CHECK constraint values
    # without needing a database connection.
    log.info(f"  Extracting SQL schema description from {sql_ddl_path}...")
    sql_schema_description = extract_sql_schema_description(sql_ddl_path)
    log.info(f"  Schema description: {len(sql_schema_description)} chars")

    manifest = {
        "corpus_name": "IRAS Corporate Tax Compliance",
        "document_count": document_count,
        "chunk_count": len(all_chunks),
        "sql_tables": list(sql_records.keys()),
        "sql_row_counts": sql_row_counts,
        "sql_schema_description": sql_schema_description,
        "last_ingested": datetime.now().isoformat() + "Z",
        "embedding_model": embedding_model,
        "chunk_strategy": f"{chunk_strategy}_{chunk_size}_{chunk_overlap}",

        # Agent tool description construction data (spec Section 7)
        "tool_description_context": {
            "vector_store_summary": (
                f"You have access to {document_count} IRAS e-Tax Guides "
                f"({len(all_chunks)} chunks) indexed in the vector store. "
                f"Documents cover corporate income tax, transfer pricing, "
                f"withholding tax, double tax agreements, anti-avoidance rules, "
                f"tax incentives, substance requirements, and tax administration."
            ),
            "sql_summary": (
                f"You have access to a SQL database with: "
                f"{sql_row_counts.get('tax_rates', 0)} tax rate records (CIT, "
                f"PTE/SUTE exemption tiers, withholding tax rates by category, rebates), "
                f"{sql_row_counts.get('tax_instruments', 0)} tax instrument records, "
                f"and {sql_row_counts.get('double_tax_agreements', 0)} DTA records. "
                f"Use SQL for questions about specific rates, treaty terms, "
                f"instrument metadata, or quantitative comparisons."
            ),
            "web_search_summary": (
                f"The corpus was last updated on "
                f"{datetime.now().strftime('%d %B %Y')}. "
                f"Queries about events after this date - new Budget announcements, "
                f"recent IRAS rulings, or industry interpretation - may need web search."
            ),
        },

        "document_type_distribution": {},
        "topic_tag_distribution": {},
    }

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
    with open(manifest_path_out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    log.info(f"  Saved to: {manifest_path_out}")

    # -----------------------------------------------------------------------
    # Summary (ASCII-only — pre-fix from MAS Phase 1)
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

    parser = argparse.ArgumentParser(description="IRAS Corporate Tax Corpus Ingestion Adapter")
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument("--sql-seed-dir", type=Path, default=DEFAULT_SQL_SEED)
    parser.add_argument("--sql-ddl-path", type=Path, default=DEFAULT_SQL_DDL,
                        help="Path to init_schema.sql for sql_schema_description extraction")
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
        sql_ddl_path=args.sql_ddl_path,
        output_dir=args.output_dir,
        manifest_path=args.manifest,
        embedding_model=args.embedding_model,
        chunk_strategy=args.chunk_strategy,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
    )


if __name__ == "__main__":
    main()
