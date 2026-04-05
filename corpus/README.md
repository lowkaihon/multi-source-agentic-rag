# MAS Regulatory Compliance Corpus — Assembly Toolkit

**Project 2, Phase 1**: Download/parse/ingest tooling for the multi-source RAG pipeline corpus.

## What This Is

Scripts and configuration for assembling the MAS regulatory compliance corpus. **The data has not yet been downloaded** — this toolkit contains the tooling, verified URLs, and seed data generators. Running the download script on a machine with access to `mas.gov.sg` and `data.opensanctions.org` is the first step.

| Source | Type | Status |
|--------|------|--------|
| **A — Vector Store** | 32 MAS regulatory PDFs → chunks → embeddings | URLs verified in manifest. **PDFs not yet downloaded.** |
| **B — SQL Database** | Enforcement actions, instruments, entities | Schema ready. 32 instrument records + 17 entity records seeded from manifest. **Enforcement actions empty** until OpenSanctions downloaded. |
| **C — Web Fallback** | Real-time | Handled by pipeline at query time. |

## Quick Start

Requires network access to `mas.gov.sg` and `data.opensanctions.org`.

```bash
pip install requests tqdm pandas pymupdf langchain-text-splitters openai

# 1. Download 32 MAS PDFs + OpenSanctions data
python scripts/download_corpus.py

# 2. Parse OpenSanctions → enforcement SQL records
python scripts/parse_structured_data.py

# 3. Initialize PostgreSQL
psql -d mas_compliance -f data/sql/init_schema.sql
psql -d mas_compliance -f data/sql/seed/seed_regulatory_instruments.sql
psql -d mas_compliance -f data/sql/seed/seed_regulated_entities.sql
psql -d mas_compliance -f data/sql/seed/seed_enforcement_actions.sql

# 4. Run ingestion adapter (chunks PDFs, computes embeddings, produces 3 outputs)
python scripts/ingestion_adapter.py --chunk-strategy recursive_character --chunk-size 1000 --chunk-overlap 200
```

## Directory Structure

```
corpus/
├── manifests/
│   └── corpus_manifest.json       # Single source of truth — all documents, URLs, metadata
├── scripts/
│   ├── download_corpus.py         # PDF + OpenSanctions downloader
│   ├── parse_structured_data.py   # OpenSanctions → SQL seed records
│   └── ingestion_adapter.py       # PDF → chunks → embeddings → 3-output interface
├── pdfs/                          # Empty until download_corpus.py is run
│   ├── core_guidelines/           # 13 documents
│   ├── consultation_papers/       # 5 documents
│   ├── enforcement_reports/       # 5 documents
│   └── info_papers_circulars/     # 9 documents
├── data/
│   ├── opensanctions/             # Empty until download_corpus.py is run
│   └── sql/
│       ├── init_schema.sql        # PostgreSQL schema (3 tables + views)
│       └── seed/                  # Manifest-derived seed data (SQL + JSON)
```

## What Exists Now vs. What Populates on First Run

| Component | Now | After `download_corpus.py` + `parse_structured_data.py` |
|-----------|-----|-------------------------------------------------------|
| PDFs | 0 | 32 |
| OpenSanctions CSV | absent | ~1,175 entities |
| enforcement_actions | 0 rows | ~200–300 rows |
| regulatory_instruments | 32 rows (manifest-derived) | 32 rows |
| regulated_entities | 17 rows (hand-seeded) | 17 + OpenSanctions entities |

## Models

- **Embedding**: `text-embedding-3-small`
- **LLM**: `gpt-4o-mini` (all pipeline nodes)
- **PDF extraction**: PyMuPDF
