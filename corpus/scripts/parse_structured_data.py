#!/usr/bin/env python3
"""
MAS Regulatory Compliance Corpus — Data Parsers
================================================
Phase 1: Parse OpenSanctions data and corpus manifest into SQL-ready records.

Produces three outputs matching the ingestion adapter interface (spec Section 7):
  1. enforcement_actions records (from OpenSanctions + manual supplements)
  2. regulatory_instruments records (from corpus manifest)
  3. regulated_entities records (from OpenSanctions entity data + manual supplements)

Usage:
    python parse_structured_data.py \
        --opensanctions-csv ./data/opensanctions/sg_mas_enforcement_actions.csv \
        --manifest ./manifests/corpus_manifest.json \
        --output-dir ./data/sql/seed

Prerequisites:
    pip install pandas
"""

import json
import csv
import os
import sys
from pathlib import Path
from datetime import datetime, date
from typing import Optional

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    print("WARNING: pandas not installed. Will use csv module (less robust).")


SCRIPT_DIR = Path(__file__).parent
DEFAULT_OS_CSV = SCRIPT_DIR.parent / "data" / "opensanctions" / "sg_mas_enforcement_actions.csv"
DEFAULT_OS_JSON = SCRIPT_DIR.parent / "data" / "opensanctions" / "sg_mas_enforcement_actions.json"
DEFAULT_MANIFEST = SCRIPT_DIR.parent / "manifests" / "corpus_manifest.json"
DEFAULT_OUTPUT = SCRIPT_DIR.parent / "data" / "sql" / "seed"


# ---------------------------------------------------------------------------
# Violation category mapping
# ---------------------------------------------------------------------------

VIOLATION_KEYWORDS = {
    "aml_cft": [
        "money laundering", "aml", "cft", "terrorism financing",
        "suspicious transaction", "customer due diligence", "cdd",
        "know your customer", "kyc", "sanctions", "notice 626",
        "beneficial owner", "screening",
    ],
    "market_abuse": [
        "insider trading", "market manipulation", "front running",
        "false trading", "market rigging", "securities fraud",
        "sfa s197", "sfa s201", "sfa s210",
    ],
    "technology_risk": [
        "technology risk", "cyber", "data breach", "it security",
        "system outage", "trm", "notice cmg-n02",
    ],
    "business_conduct": [
        "misconduct", "misrepresentation", "unsuitable recommendation",
        "churning", "fair dealing", "client money",
    ],
    "disclosure": [
        "disclosure", "false statement", "misleading",
        "prospectus", "offer document",
    ],
    "unlicensed_activity": [
        "unlicensed", "without licence", "carrying on business",
        "exempt", "unregulated",
    ],
    "fit_and_proper": [
        "fit and proper", "prohibition order", "representative",
    ],
}

ACTION_TYPE_KEYWORDS = {
    "composition_penalty": ["composition", "compound"],
    "prohibition_order": ["prohibition order", "prohibited"],
    "reprimand": ["reprimand"],
    "criminal_conviction": ["conviction", "convicted", "criminal", "sentenced", "imprisonment"],
    "civil_penalty": ["civil penalty"],
    "warning": ["warning", "advisory"],
    "licence_revocation": ["revoke", "revocation", "cancel"],
    "licence_suspension": ["suspend", "suspension"],
    "direction": ["direction", "directed"],
}


def classify_text(text: str, keyword_map: dict, default: str = "other") -> str:
    """Classify text using keyword matching. Returns the best-match category."""
    if not text:
        return default
    text_lower = text.lower()
    scores = {}
    for category, keywords in keyword_map.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[category] = score
    if scores:
        return max(scores, key=scores.get)
    return default


# ---------------------------------------------------------------------------
# OpenSanctions parser
# ---------------------------------------------------------------------------

def parse_opensanctions_ftm_json(json_path: Path) -> tuple[list[dict], list[dict]]:
    """
    Parse OpenSanctions FTM (FollowTheMoney) JSON into enforcement_actions
    and regulated_entities records.

    The FTM JSON has one entity per line with schemas:
      - Person (201): enforcement target individuals
      - Company (116): enforcement target institutions
      - Sanction (337): enforcement actions linking to Person/Company via entity ref
      - Article (132): media releases about enforcement actions
      - Documentation (389): supporting documents

    We cross-reference Sanction records with Person/Company entities to build
    enforcement_actions rows with proper dates, action types, and descriptions.

    Returns:
        (enforcement_records, entity_records)
    """
    if not json_path.exists():
        print(f"WARNING: OpenSanctions FTM JSON not found at {json_path}")
        print("  Run download_corpus.py first, or download manually from:")
        print("  https://data.opensanctions.org/datasets/latest/sg_mas_enforcement_actions/entities.ftm.json")
        return [], []

    # Load all entities into a lookup by ID
    entities_by_id = {}
    sanctions = []
    articles = []

    with open(json_path, "r", encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line.strip())
            entities_by_id[obj["id"]] = obj
            schema = obj.get("schema", "")
            if schema == "Sanction":
                sanctions.append(obj)
            elif schema == "Article":
                articles.append(obj)

    # Build article lookup by source URL for description enrichment
    article_by_url = {}
    for art in articles:
        for url in art.get("properties", {}).get("sourceUrl", []):
            article_by_url[url] = art

    persons_companies = {
        eid: e for eid, e in entities_by_id.items()
        if e.get("schema") in ("Person", "Company")
    }

    print(f"  Loaded {len(entities_by_id)} total FTM entities")
    print(f"  Persons/Companies: {len(persons_companies)}, Sanctions: {len(sanctions)}, Articles: {len(articles)}")

    enforcement_records = []
    entity_records = []
    seen_entities = set()

    # Strategy: For each Sanction, resolve the linked entity and build an enforcement record
    for sanction in sanctions:
        props = sanction.get("properties", {})
        entity_refs = props.get("entity", [])
        sanction_date = (props.get("date", [None]) or [None])[0]
        sanction_status = (props.get("status", [""]) or [""])[0]
        source_urls = props.get("sourceUrl", [])
        source_url = source_urls[0] if source_urls else None

        # Build description from article title if available
        description = sanction.get("caption", "")
        if source_url and source_url in article_by_url:
            art = article_by_url[source_url]
            titles = art.get("properties", {}).get("title", [])
            if titles:
                description = titles[0]

        # Resolve each linked entity
        for entity_ref in entity_refs:
            entity = entities_by_id.get(entity_ref)
            if not entity:
                continue
            schema = entity.get("schema", "")
            if schema not in ("Person", "Company"):
                continue

            e_props = entity.get("properties", {})
            names = e_props.get("name", [])
            name = names[0] if names else entity.get("caption", "Unknown")
            entity_type = "individual" if schema == "Person" else "institution"

            # Combine description: sanction status + article title + source URL context
            full_desc_parts = []
            if sanction_status:
                full_desc_parts.append(f"Action: {sanction_status}")
            if description and description != "Sanction":
                full_desc_parts.append(description)
            full_desc = " | ".join(full_desc_parts) if full_desc_parts else None

            # Classify using combined text
            classify_input = f"{full_desc or ''} {sanction_status}"
            violation_category = classify_text(classify_input, VIOLATION_KEYWORDS)
            action_type = classify_text(sanction_status or classify_input, ACTION_TYPE_KEYWORDS)

            # Parse date
            action_date = sanction_date
            if not action_date:
                # Fall back to entity dates
                for date_field in ["last_change", "first_seen"]:
                    ds = entity.get(date_field, "")
                    if ds:
                        try:
                            action_date = datetime.fromisoformat(ds.replace("Z", "+00:00")).strftime("%Y-%m-%d")
                            break
                        except (ValueError, TypeError):
                            pass
            if not action_date:
                action_date = "2024-01-01"

            enforcement_records.append({
                "entity_name": name.strip(),
                "entity_type": entity_type,
                "action_date": action_date,
                "action_type": action_type,
                "violation_category": violation_category,
                "penalty_amount": None,
                "penalty_currency": "SGD",
                "regulation_breached": None,
                "prohibition_years": None,
                "description": full_desc[:500] if full_desc else None,
                "source_url": source_url or f"https://www.opensanctions.org/entities/{entity.get('id', '')}/",
                "report_period": None,
                "opensanctions_id": entity.get("id", ""),
            })

            # Deduplicated entity record
            if name.strip() not in seen_entities:
                seen_entities.add(name.strip())
                entity_records.append({
                    "entity_name": name.strip(),
                    "entity_type": "individual" if entity_type == "individual" else "other",
                    "sector": "other",
                    "licence_types": [],
                    "is_active": True,
                    "fi_directory_id": None,
                })

    # Also add Person/Company entities that have no linked Sanction records
    entities_with_sanctions = set()
    for sanction in sanctions:
        for ref in sanction.get("properties", {}).get("entity", []):
            entities_with_sanctions.add(ref)

    for eid, entity in persons_companies.items():
        if eid in entities_with_sanctions:
            continue
        e_props = entity.get("properties", {})
        names = e_props.get("name", [])
        name = names[0] if names else entity.get("caption", "Unknown")
        if name.strip() in seen_entities:
            continue

        schema = entity.get("schema", "")
        entity_type = "individual" if schema == "Person" else "institution"
        source_urls = e_props.get("sourceUrl", [])
        source_url = source_urls[0] if source_urls else None

        # Build description from source URL article
        description = entity.get("caption", "")
        if source_url and source_url in article_by_url:
            art = article_by_url[source_url]
            titles = art.get("properties", {}).get("title", [])
            if titles:
                description = titles[0]

        violation_category = classify_text(description, VIOLATION_KEYWORDS)
        action_type = classify_text(description, ACTION_TYPE_KEYWORDS)

        action_date = None
        for date_field in ["last_change", "first_seen"]:
            ds = entity.get(date_field, "")
            if ds:
                try:
                    action_date = datetime.fromisoformat(ds.replace("Z", "+00:00")).strftime("%Y-%m-%d")
                    break
                except (ValueError, TypeError):
                    pass
        if not action_date:
            action_date = "2024-01-01"

        enforcement_records.append({
            "entity_name": name.strip(),
            "entity_type": entity_type,
            "action_date": action_date,
            "action_type": action_type,
            "violation_category": violation_category,
            "penalty_amount": None,
            "penalty_currency": "SGD",
            "regulation_breached": None,
            "prohibition_years": None,
            "description": description[:500] if description else None,
            "source_url": source_url or f"https://www.opensanctions.org/entities/{eid}/",
            "report_period": None,
            "opensanctions_id": eid,
        })

        seen_entities.add(name.strip())
        entity_records.append({
            "entity_name": name.strip(),
            "entity_type": "individual" if entity_type == "individual" else "other",
            "sector": "other",
            "licence_types": [],
            "is_active": True,
            "fi_directory_id": None,
        })

    print(f"  Parsed {len(enforcement_records)} enforcement records")
    print(f"  Parsed {len(entity_records)} unique entities")
    return enforcement_records, entity_records


def parse_opensanctions_csv(csv_path: Path) -> tuple[list[dict], list[dict]]:
    """Legacy CSV parser — falls back to FTM JSON if CSV is empty."""
    # Check if FTM JSON exists and prefer it (CSV simple format is often empty)
    json_path = csv_path.parent / "sg_mas_enforcement_actions.json"
    if json_path.exists():
        print(f"  Found FTM JSON at {json_path}, using it instead of CSV")
        return parse_opensanctions_ftm_json(json_path)

    if not csv_path.exists():
        print(f"WARNING: OpenSanctions data not found at {csv_path}")
        return [], []

    # Original CSV parsing logic as fallback
    if HAS_PANDAS:
        df = pd.read_csv(csv_path, low_memory=False)
        rows = df.to_dict("records")
    else:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

    if not rows:
        print(f"  CSV is empty (headers only), no data to parse")
        return [], []

    print(f"  Loaded {len(rows)} records from OpenSanctions CSV")
    # ... original CSV parsing would go here but we prefer FTM JSON
    return [], []


# ---------------------------------------------------------------------------
# Regulatory instruments registry from manifest
# ---------------------------------------------------------------------------

def parse_manifest_instruments(manifest_path: Path) -> list[dict]:
    """
    Generate regulatory_instruments records from the corpus manifest.
    Each document in the manifest becomes an instrument record.
    """
    with open(manifest_path, "r") as f:
        manifest = json.load(f)

    records = []
    doc_categories = ["core_guidelines", "consultation_papers", "enforcement_reports", "info_papers_circulars"]

    for category in doc_categories:
        docs = manifest.get("documents", {}).get(category, [])
        for doc in docs:
            instrument_id = doc["id"].replace("-", "_")
            records.append({
                "instrument_id": instrument_id,
                "instrument_type": doc.get("instrument_type", "guideline"),
                "title": doc["title"],
                "short_name": doc.get("short_name"),
                "effective_date": doc.get("effective_date"),
                "last_revised_date": doc.get("last_revised_date"),
                "applicable_sectors": doc.get("applicable_sectors", []),
                "applicable_entities": doc.get("applicable_entities", []),
                "topic_tags": doc.get("topic_tags", []),
                "status": doc.get("status", "in_force"),
                "superseded_by": None,
                "pdf_filename": doc.get("filename"),
                "source_url": doc.get("url"),
                "description": doc.get("notes"),
            })

    print(f"  Generated {len(records)} regulatory instrument records from manifest")
    return records


# ---------------------------------------------------------------------------
# Major Singapore regulated entities (manual seed data)
# ---------------------------------------------------------------------------

MAJOR_REGULATED_ENTITIES = [
    {"entity_name": "DBS Bank Ltd", "entity_type": "local_bank", "sector": "banking", "licence_types": ["full_bank"], "is_active": True},
    {"entity_name": "Oversea-Chinese Banking Corporation Limited", "entity_type": "local_bank", "sector": "banking", "licence_types": ["full_bank"], "is_active": True},
    {"entity_name": "United Overseas Bank Limited", "entity_type": "local_bank", "sector": "banking", "licence_types": ["full_bank"], "is_active": True},
    {"entity_name": "Citibank N.A.", "entity_type": "full_bank", "sector": "banking", "licence_types": ["full_bank"], "is_active": True},
    {"entity_name": "HSBC", "entity_type": "full_bank", "sector": "banking", "licence_types": ["full_bank"], "is_active": True},
    {"entity_name": "Standard Chartered Bank", "entity_type": "full_bank", "sector": "banking", "licence_types": ["full_bank"], "is_active": True},
    {"entity_name": "JPMorgan Chase Bank, N.A.", "entity_type": "full_bank", "sector": "banking", "licence_types": ["full_bank"], "is_active": True},
    {"entity_name": "Maybank Singapore Limited", "entity_type": "full_bank", "sector": "banking", "licence_types": ["full_bank"], "is_active": True},
    {"entity_name": "Bank of China Limited", "entity_type": "full_bank", "sector": "banking", "licence_types": ["full_bank"], "is_active": True},
    {"entity_name": "Goldman Sachs (Singapore) Pte.", "entity_type": "merchant_bank", "sector": "banking", "licence_types": ["merchant_bank", "capital_markets_services"], "is_active": True},
    {"entity_name": "UBS AG", "entity_type": "wholesale_bank", "sector": "banking", "licence_types": ["wholesale_bank"], "is_active": True},
    {"entity_name": "Credit Suisse AG", "entity_type": "wholesale_bank", "sector": "banking", "licence_types": ["wholesale_bank"], "is_active": False},
    {"entity_name": "Singapore Exchange Limited", "entity_type": "capital_markets_intermediary", "sector": "capital_markets", "licence_types": ["approved_exchange"], "is_active": True},
    {"entity_name": "GrabPay Pte Ltd", "entity_type": "payment_service_provider", "sector": "payments", "licence_types": ["major_payment_institution"], "is_active": True},
    {"entity_name": "Great Eastern Life Assurance Co Ltd", "entity_type": "insurer", "sector": "insurance", "licence_types": ["life_insurer"], "is_active": True},
    {"entity_name": "Prudential Assurance Company Singapore", "entity_type": "insurer", "sector": "insurance", "licence_types": ["life_insurer"], "is_active": True},
    {"entity_name": "AIA Singapore Private Limited", "entity_type": "insurer", "sector": "insurance", "licence_types": ["life_insurer"], "is_active": True},
]


# ---------------------------------------------------------------------------
# Output generators
# ---------------------------------------------------------------------------

def save_sql_inserts(records: list[dict], table_name: str, output_dir: Path):
    """Generate SQL INSERT statements from records."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / f"seed_{table_name}.sql"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"-- Seed data for {table_name}\n")
        f.write(f"-- Generated: {datetime.now().isoformat()}\n")
        f.write(f"-- Record count: {len(records)}\n\n")

        for record in records:
            columns = []
            values = []
            for col, val in record.items():
                columns.append(col)
                if val is None:
                    values.append("NULL")
                elif isinstance(val, bool):
                    values.append("TRUE" if val else "FALSE")
                elif isinstance(val, (int, float)):
                    values.append(str(val))
                elif isinstance(val, list):
                    # PostgreSQL array literal
                    arr_items = ", ".join(f"'{v}'" for v in val)
                    values.append(f"ARRAY[{arr_items}]" if val else "'{}'")
                else:
                    escaped = str(val).replace("'", "''")
                    values.append(f"'{escaped}'")

            cols_str = ", ".join(columns)
            vals_str = ", ".join(values)
            f.write(f"INSERT INTO {table_name} ({cols_str}) VALUES ({vals_str});\n")

    print(f"  Wrote {len(records)} records to {filepath}")


def save_json(records: list[dict], filename: str, output_dir: Path):
    """Save records as JSON for the ingestion adapter."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)
    print(f"  Wrote {len(records)} records to {filepath}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Parse structured data for MAS compliance corpus")
    parser.add_argument("--opensanctions-csv", type=Path, default=DEFAULT_OS_CSV)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    print("=" * 60)
    print("MAS Regulatory Compliance — Structured Data Parser")
    print("=" * 60)

    # 1. Parse OpenSanctions enforcement data
    print("\n[1/3] Parsing OpenSanctions enforcement data...")
    enforcement_records, os_entity_records = parse_opensanctions_csv(args.opensanctions_csv)

    # 2. Parse manifest into regulatory instruments
    print("\n[2/3] Generating regulatory instruments registry...")
    instrument_records = parse_manifest_instruments(args.manifest)

    # 3. Merge entity records
    print("\n[3/3] Building regulated entities registry...")
    all_entities = MAJOR_REGULATED_ENTITIES.copy()
    existing_names = {e["entity_name"] for e in all_entities}
    for entity in os_entity_records:
        if entity["entity_name"] not in existing_names:
            all_entities.append(entity)
            existing_names.add(entity["entity_name"])
    print(f"  Total entities: {len(all_entities)} ({len(MAJOR_REGULATED_ENTITIES)} major + {len(all_entities) - len(MAJOR_REGULATED_ENTITIES)} from OpenSanctions)")

    # Save outputs
    print("\n" + "=" * 60)
    print("Saving outputs...")
    print("=" * 60)

    # SQL inserts
    save_sql_inserts(enforcement_records, "enforcement_actions", args.output_dir)
    save_sql_inserts(instrument_records, "regulatory_instruments", args.output_dir)
    save_sql_inserts(all_entities, "regulated_entities", args.output_dir)

    # JSON (for ingestion adapter)
    save_json(enforcement_records, "enforcement_actions.json", args.output_dir)
    save_json(instrument_records, "regulatory_instruments.json", args.output_dir)
    save_json(all_entities, "regulated_entities.json", args.output_dir)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  enforcement_actions:    {len(enforcement_records)} records")
    print(f"  regulatory_instruments: {len(instrument_records)} records")
    print(f"  regulated_entities:     {len(all_entities)} records")
    print(f"  Output directory:       {args.output_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
