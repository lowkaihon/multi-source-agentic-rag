# IRAS Corporate Tax Compliance Corpus — Assembly Toolkit

**Project 2, Phase 1 (IRAS swap)**: Download/parse/ingest tooling for the
second-domain corpus that validates the corpus-swappable design of the
multi-source RAG pipeline.

## What This Is

Scripts and configuration for assembling the IRAS corporate tax corpus.
**The data has not yet been downloaded** — this toolkit contains the tooling,
verified URLs, and seed data generators. Running the download script on a
machine with access to `iras.gov.sg` is the first step.

This is structurally a clone of `mas-corpus/` (the MAS toolkit). The directory
layout, script names, output shapes, dataclasses, and CLI arguments are
deliberately identical. Anything that diverges is documented inline as a
domain-specific adapter decision. **The pipeline does not change.**

| Source | Type | Status |
|--------|------|--------|
| **A — Vector Store** | 11 IRAS e-Tax Guides → chunks → embeddings | URLs verified in manifest. **PDFs not yet downloaded.** Local session is expected to add 1-3 more guides to hit the 12-15 target — see "Known Coverage Gaps" below. |
| **B — SQL Database** | Tax rates, instruments, DTAs | Schema ready. 32 tax rate records + 24 DTA records hand-seeded. **Instrument records derived from manifest at seed time.** |
| **C — Web Fallback** | Real-time | Handled by pipeline at query time. |

## Quick Start

Requires network access to `iras.gov.sg`.

```bash
pip install requests tqdm pymupdf langchain-text-splitters openai

# 1. Download 11 IRAS e-Tax Guides (target 12-15 — add 1-3 more for WHT/DTA coverage)
python scripts/download_corpus.py

# 2. Generate SQL seed records (tax_rates, tax_instruments, double_tax_agreements)
python scripts/seed_iras_data.py

# 3. Initialize PostgreSQL
psql -d iras_compliance -f data/sql/init_schema.sql
psql -d iras_compliance -f data/sql/seed/seed_tax_rates.sql
psql -d iras_compliance -f data/sql/seed/seed_tax_instruments.sql
psql -d iras_compliance -f data/sql/seed/seed_double_tax_agreements.sql

# 4. Run ingestion adapter (chunks PDFs, computes embeddings, produces 3 outputs)
python scripts/ingestion_adapter.py --chunk-strategy recursive_character --chunk-size 1000 --chunk-overlap 200
```

## Directory Structure

```
iras_corpus/
├── manifests/
│   └── iras_corpus_manifest.json   # Single source of truth — 12 documents, URLs, topic tags
├── scripts/
│   ├── download_corpus.py          # PDF downloader (browser UA, magic-byte verification)
│   ├── seed_iras_data.py           # Hand-curated structured data → SQL seed records
│   └── ingestion_adapter.py        # PDF → chunks → embeddings → 3-output contract
├── pdfs/                           # Empty until download_corpus.py is run
│   ├── core_corporate_tax/         # 5 documents
│   ├── transfer_pricing/           # 2 documents
│   ├── anti_avoidance/             # 2 documents
│   └── tax_incentives/             # 2 documents
├── data/
│   └── sql/
│       ├── init_schema.sql         # PostgreSQL schema (3 tables + views)
│       └── seed/                   # Seed data (JSON for adapter, SQL for psql)
└── ingestion_output/               # Created by ingestion_adapter.py
    ├── opensearch_documents.json
    ├── sql_records.json
    └── metadata_manifest.json
```

## What Exists Now vs. What Populates on First Run

| Component | Now | After `download_corpus.py` + `seed_iras_data.py` |
|-----------|-----|-------------------------------------------------|
| PDFs | 0 | 11 (target 12-15 after coverage-gap additions) |
| tax_rates | 0 rows | 32 rows (hand-curated from IRAS rate history) |
| tax_instruments | 0 rows | 11 rows (derived 1:1 from manifest documents) |
| double_tax_agreements | 0 rows | 24 rows (hand-curated from IRAS DTA list) |

## Known Coverage Gaps (for the local session to address)

The current 11-document manifest does not evenly cover all 8 topic tags in
`TOPIC_KEYWORDS`. Specifically:

- **`withholding_tax`**: NO document is centrally about WHT in the current
  selection. WHT will appear only in passing references in the Transfer
  Pricing Guidelines and the Foreign-Sourced Income guide. Topic tag
  coverage for `withholding_tax` is expected to be very low (< 5%) on first
  ingestion.
- **`dta`**: only 1 document (FSIE) explicitly references DTAs in its scope.
  Coverage will be low but not zero.

The local session should add 1-3 more documents from `iras.gov.sg` to close
these gaps and hit the spec's 12-15 target. Suggested searches on the IRAS
guide library:

- "Withholding Tax on Payments to Non-Residents" e-Tax Guide
- "Avoidance of Double Taxation Agreements" e-Tax Guide
- (Optional) Any e-Tax Guide on the Multilateral Instrument or BEPS-related
  rules

## Differences From the MAS Toolkit (and Why)

The IRAS toolkit is structurally identical to `mas-corpus/` by design — that's
the whole point of validating the corpus-swappable claim. Where it differs,
it differs minimally and intentionally:

1. **No `parse_structured_data.py` equivalent — `seed_iras_data.py` instead.**
   The MAS structured data comes from OpenSanctions (an actively-maintained
   public dataset). IRAS publishes no equivalent dump for tax rates or DTA
   terms, so the IRAS structured data is hand-curated from the IRAS website
   and rate-history pages. The output shape (JSON files in `data/sql/seed/`,
   one per table) is identical so the adapter doesn't notice.

2. **`DOC_TYPE_PATTERNS` is shorter** — IRAS publishes one document type
   (e-Tax Guide), while MAS publishes six (notice, guideline, circular, etc.).
   The pattern dict is kept (vs. hardcoded) so future additions (circulars,
   advance rulings) are a one-line change.

3. **`TOPIC_KEYWORDS` is a tax-domain vocabulary** — `corporate_tax`,
   `transfer_pricing`, `withholding_tax`, `dta`, `anti_avoidance`,
   `tax_incentive`, `substance_requirements`, `tax_administration`. Same
   per-chunk keyword-matching mechanism as MAS, just with different
   vocabulary. Topic tag distribution should land in the same ~70%+ coverage
   range MAS reported.

4. **Three table names differ** (`tax_rates`, `tax_instruments`,
   `double_tax_agreements` vs. `enforcement_actions`, `regulatory_instruments`,
   `regulated_entities`). The agent learns these from the schema in its system
   prompt, which is built from the `sql_schema_description` field of the
   manifest at startup — no pipeline code knows the table names.

5. **`sql_schema_description` is generated by the adapter, not the pipeline.**
   Per the updated project2_domain_spec.md Section 7, the manifest must include
   a `sql_schema_description` text block that lists each table's columns and
   CHECK constraint values. The IRAS adapter does this by calling
   `extract_sql_schema_description(init_schema.sql)` during ingest and storing
   the result in the manifest. **This is a change from the current MAS setup**,
   where the same function lives in `src/msrag/tools/builder.py` and is called
   by the pipeline at startup against a hardcoded DDL path. The function
   itself is copied verbatim from the pipeline into this adapter — see the
   "Hidden assumptions surfaced" section below.

Everything else (chunking strategies, section heading regex, embedding
fallback, contract dataclasses, output file shapes, CLI args) is byte-for-byte
the same as the MAS adapter.

## Hidden assumptions surfaced

The corpus-swappable claim says "swap the adapter, don't touch the pipeline."
Building this plugin surfaced one place where that's not yet true:

**`extract_sql_schema_description()` lives in pipeline code.** It's currently
in `src/msrag/tools/builder.py` and the pipeline calls it at startup against
the MAS DDL path. To plug in the IRAS corpus, that pipeline call would
otherwise need to be repointed at the IRAS DDL — a config change, but a
config change in pipeline code, not in the adapter. The new Section 7 spec
fixes this by moving the call into the adapter and putting the result in the
manifest. The IRAS adapter is the first place this contract field is
implemented, so until the MAS adapter is updated to do the same, the
function exists in two places: the original in `src/msrag/tools/builder.py`
and a verbatim copy in `iras_corpus/scripts/ingestion_adapter.py`.

The right follow-up is to (a) update the MAS adapter to populate
`sql_schema_description` in its manifest the same way, then (b) make the
pipeline read from `manifest["sql_schema_description"]` with a fallback to
the existing DDL-path-based call for backward compatibility, then (c)
deduplicate the function into a shared `corpus_common/` module that both
adapters import. None of those are blockers for the IRAS swap itself —
the IRAS adapter conforms to the new contract today.

## Models

- **Embedding**: `text-embedding-3-small` (same as MAS)
- **LLM**: `gpt-5.4-mini` (same as MAS — pipeline default, no override)
- **PDF extraction**: PyMuPDF (same as MAS)

## Phase 1 Pre-fixes Baked In

The MAS Phase 1 retro surfaced several bugs that would have hit the IRAS
swap if the toolkit were copied naively. These are pre-fixed:

- **Browser User-Agent** in `download_corpus.py` — `python-requests/X.Y` is
  blocked by some government CDNs.
- **`CORPUS_DIR = SCRIPT_DIR.parent`** in both scripts — avoids the path bug
  where `DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "pdfs"` puts files in the wrong
  place.
- **ASCII-only print summaries** — no `✗`/`✓` characters that crash on
  Windows `cp1252` consoles. Replaced with `OK`/`FAIL`.
- **Magic-byte PDF verification** after download — IRAS CMS sometimes serves
  HTML error pages with `200 OK` when a manifest URL is stale. The downloader
  checks for `%PDF` and deletes non-PDF responses.
- **Direct-PDF URL precheck** — manifest URLs that don't end in `.pdf` are
  surfaced as failed (not silently downloaded as HTML).
