#!/usr/bin/env python3
"""
IRAS Corporate Tax Compliance — Structured Data Seed
=====================================================
Phase 1, Step 2 of Project 2 (IRAS swap).

The MAS analogue of this file (parse_structured_data.py) parses the
OpenSanctions FTM JSON to produce enforcement records. IRAS has no equivalent
public structured-data dump for tax rates and DTA terms, so this script
emits hand-curated seed records sourced from:

  - IRAS website (corporate income tax rate history, partial/start-up
    exemption tiers, withholding tax rates)
  - IRAS DTA list page at iras.gov.sg/taxes/international-tax/list-of-dtas
  - The corpus manifest at manifests/iras_corpus_manifest.json (for the
    tax_instruments table — one row per e-Tax Guide in the vector store)

Outputs:
    data/sql/seed/tax_rates.json
    data/sql/seed/tax_instruments.json
    data/sql/seed/double_tax_agreements.json

The corresponding .sql files are also emitted for psql ingestion. The
JSON files are what the ingestion adapter loads.

Usage:
    python seed_iras_data.py [--manifest ./manifests/iras_corpus_manifest.json] \\
                             [--output-dir ./data/sql/seed]

Re-verification before running:
    Tiered DTA rates (China dividends, India dividends, Vietnam dividends)
    are flattened to the LOWER tier with a note in the `notes` column. If you
    spot-check against the IRAS DTA list and find rates have changed, update
    the DTA records below — DO NOT silently keep stale rates.
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
CORPUS_DIR = SCRIPT_DIR.parent
DEFAULT_MANIFEST = CORPUS_DIR / "manifests" / "iras_corpus_manifest.json"
DEFAULT_OUTPUT_DIR = CORPUS_DIR / "data" / "sql" / "seed"


# ===========================================================================
# tax_rates — time-series factual data
# ===========================================================================
# Sourced from IRAS website's corporate income tax rate history page,
# Budget summaries, and the rate tables embedded in the e-Tax Guides.

def build_tax_rates() -> list[dict]:
    rates: list[dict] = []

    # Headline corporate income tax rate, YA 2010 onward (flat 17%)
    for ya in range(2010, 2027):
        rates.append({
            "year_of_assessment": ya,
            "rate_category": "corporate_income_tax",
            "rate_value": 0.17,
            "threshold_amount": None,
            "threshold_currency": "SGD",
            "applicable_to": "all_companies",
            "description": "Headline corporate income tax rate on chargeable income",
            "source_guide": "iras_corporate_income_tax_basics_page",
            "effective_from": date(2009, 1, 1).isoformat(),
            "effective_to": None,
        })

    # Partial Tax Exemption (PTE) — pre-YA 2020 tiers
    rates += [
        {
            "year_of_assessment": 2019,
            "rate_category": "partial_exemption_first_tier",
            "rate_value": 0.75,
            "threshold_amount": 10000.00,
            "threshold_currency": "SGD",
            "applicable_to": "all_companies",
            "description": "75% exemption on first $10,000 of normal chargeable income (pre-YA2020 PTE)",
            "source_guide": "iras_corporate_income_tax_basics_page",
            "effective_from": date(2008, 1, 1).isoformat(),
            "effective_to": date(2019, 12, 31).isoformat(),
        },
        {
            "year_of_assessment": 2019,
            "rate_category": "partial_exemption_second_tier",
            "rate_value": 0.50,
            "threshold_amount": 290000.00,
            "threshold_currency": "SGD",
            "applicable_to": "all_companies",
            "description": "50% exemption on next $290,000 of normal chargeable income (pre-YA2020 PTE)",
            "source_guide": "iras_corporate_income_tax_basics_page",
            "effective_from": date(2008, 1, 1).isoformat(),
            "effective_to": date(2019, 12, 31).isoformat(),
        },
        # Post-YA 2020 PTE tiers (cap reduced)
        {
            "year_of_assessment": 2020,
            "rate_category": "partial_exemption_first_tier",
            "rate_value": 0.75,
            "threshold_amount": 10000.00,
            "threshold_currency": "SGD",
            "applicable_to": "all_companies",
            "description": "75% exemption on first $10,000 of normal chargeable income (YA2020 onwards)",
            "source_guide": "iras_corporate_income_tax_basics_page",
            "effective_from": date(2019, 1, 1).isoformat(),
            "effective_to": None,
        },
        {
            "year_of_assessment": 2020,
            "rate_category": "partial_exemption_second_tier",
            "rate_value": 0.50,
            "threshold_amount": 190000.00,
            "threshold_currency": "SGD",
            "applicable_to": "all_companies",
            "description": "50% exemption on next $190,000 of normal chargeable income (YA2020 onwards)",
            "source_guide": "iras_corporate_income_tax_basics_page",
            "effective_from": date(2019, 1, 1).isoformat(),
            "effective_to": None,
        },
    ]

    # Start-up Tax Exemption (SUTE)
    rates += [
        {
            "year_of_assessment": 2019,
            "rate_category": "startup_exemption_first_tier",
            "rate_value": 1.00,
            "threshold_amount": 100000.00,
            "threshold_currency": "SGD",
            "applicable_to": "qualifying_startups",
            "description": "100% exemption on first $100,000 (first 3 YAs, pre-YA2020 SUTE)",
            "source_guide": "iras_corporate_income_tax_basics_page",
            "effective_from": date(2008, 1, 1).isoformat(),
            "effective_to": date(2019, 12, 31).isoformat(),
        },
        {
            "year_of_assessment": 2019,
            "rate_category": "startup_exemption_second_tier",
            "rate_value": 0.50,
            "threshold_amount": 200000.00,
            "threshold_currency": "SGD",
            "applicable_to": "qualifying_startups",
            "description": "50% exemption on next $200,000 (first 3 YAs, pre-YA2020 SUTE)",
            "source_guide": "iras_corporate_income_tax_basics_page",
            "effective_from": date(2008, 1, 1).isoformat(),
            "effective_to": date(2019, 12, 31).isoformat(),
        },
        {
            "year_of_assessment": 2020,
            "rate_category": "startup_exemption_first_tier",
            "rate_value": 0.75,
            "threshold_amount": 100000.00,
            "threshold_currency": "SGD",
            "applicable_to": "qualifying_startups",
            "description": "75% exemption on first $100,000 (first 3 YAs, YA2020 onwards SUTE)",
            "source_guide": "iras_corporate_income_tax_basics_page",
            "effective_from": date(2019, 1, 1).isoformat(),
            "effective_to": None,
        },
        {
            "year_of_assessment": 2020,
            "rate_category": "startup_exemption_second_tier",
            "rate_value": 0.50,
            "threshold_amount": 100000.00,
            "threshold_currency": "SGD",
            "applicable_to": "qualifying_startups",
            "description": "50% exemption on next $100,000 (first 3 YAs, YA2020 onwards SUTE)",
            "source_guide": "iras_corporate_income_tax_basics_page",
            "effective_from": date(2019, 1, 1).isoformat(),
            "effective_to": None,
        },
    ]

    # Domestic withholding tax rates on payments to non-residents
    rates += [
        {
            "year_of_assessment": 2024,
            "rate_category": "withholding_tax_interest",
            "rate_value": 0.15,
            "threshold_amount": None,
            "threshold_currency": "SGD",
            "applicable_to": "non_resident_companies",
            "description": "Final WHT on interest, commission, fee in connection with loan or indebtedness",
            "source_guide": "iras_withholding_tax_overview_page",
            "effective_from": date(2004, 1, 1).isoformat(),
            "effective_to": None,
        },
        {
            "year_of_assessment": 2024,
            "rate_category": "withholding_tax_royalty",
            "rate_value": 0.10,
            "threshold_amount": None,
            "threshold_currency": "SGD",
            "applicable_to": "non_resident_companies",
            "description": "Final WHT on royalties or other lump-sum payments for use of movable property",
            "source_guide": "iras_withholding_tax_overview_page",
            "effective_from": date(2004, 1, 1).isoformat(),
            "effective_to": None,
        },
        {
            "year_of_assessment": 2024,
            "rate_category": "withholding_tax_technical_assistance",
            "rate_value": 0.17,
            "threshold_amount": None,
            "threshold_currency": "SGD",
            "applicable_to": "non_resident_companies",
            "description": "WHT on technical/management/service fees for services rendered in Singapore (at prevailing CIT rate)",
            "source_guide": "iras_withholding_tax_overview_page",
            "effective_from": date(2010, 1, 1).isoformat(),
            "effective_to": None,
        },
        {
            "year_of_assessment": 2024,
            "rate_category": "withholding_tax_director_fees",
            "rate_value": 0.24,
            "threshold_amount": None,
            "threshold_currency": "SGD",
            "applicable_to": "non_resident_individuals",
            "description": "WHT on director's remuneration paid to non-resident directors",
            "source_guide": "iras_withholding_tax_overview_page",
            "effective_from": date(2023, 1, 1).isoformat(),
            "effective_to": None,
        },
        {
            "year_of_assessment": 2024,
            "rate_category": "withholding_tax_reit_distribution",
            "rate_value": 0.10,
            "threshold_amount": None,
            "threshold_currency": "SGD",
            "applicable_to": "non_resident_non_individual_unitholders",
            "description": "Reduced final WHT on REIT distributions (concession extended to 31 Dec 2030 in Budget 2025)",
            "source_guide": "iras_withholding_tax_overview_page",
            "effective_from": date(2005, 2, 18).isoformat(),
            "effective_to": date(2030, 12, 31).isoformat(),
        },
    ]

    # Corporate income tax rebates (Budget-announced, time-bound)
    rates += [
        {
            "year_of_assessment": 2024,
            "rate_category": "cit_rebate",
            "rate_value": 0.50,
            "threshold_amount": 40000.00,
            "threshold_currency": "SGD",
            "applicable_to": "all_companies",
            "description": "50% CIT rebate, capped at $40,000 per company (Budget 2024 measure)",
            "source_guide": "iras_corporate_income_tax_basics_page",
            "effective_from": date(2024, 1, 1).isoformat(),
            "effective_to": date(2024, 12, 31).isoformat(),
        },
        {
            "year_of_assessment": 2025,
            "rate_category": "cit_rebate",
            "rate_value": 0.50,
            "threshold_amount": 40000.00,
            "threshold_currency": "SGD",
            "applicable_to": "all_companies",
            "description": "50% CIT rebate, capped at $40,000 (Budget 2025 measure)",
            "source_guide": "iras_corporate_income_tax_basics_page",
            "effective_from": date(2025, 1, 1).isoformat(),
            "effective_to": date(2025, 12, 31).isoformat(),
        },
    ]

    return rates


# ===========================================================================
# double_tax_agreements — most commonly referenced treaty partners
# ===========================================================================
# Tiered rates flattened to the LOWER tier with a note. If a treaty has no
# comprehensive DTA (e.g. US), all three rate columns are NULL.

def build_dtas() -> list[dict]:
    return [
        {"treaty_partner": "Australia", "treaty_partner_iso": "AUS",
         "signed_date": "1969-02-11", "in_force_date": "1969-12-22",
         "dividend_wht_rate": 0.0, "interest_wht_rate": 0.10, "royalty_wht_rate": 0.10,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Original 1969 treaty; protocol 2009. Dividends exempt under SG one-tier system."},

        {"treaty_partner": "Belgium", "treaty_partner_iso": "BEL",
         "signed_date": "2006-11-06", "in_force_date": "2008-11-27",
         "dividend_wht_rate": 0.05, "interest_wht_rate": 0.05, "royalty_wht_rate": 0.05,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividend rate 5% if beneficial owner is a company holding >=10%."},

        {"treaty_partner": "Canada", "treaty_partner_iso": "CAN",
         "signed_date": "1976-03-06", "in_force_date": "1977-09-23",
         "dividend_wht_rate": 0.15, "interest_wht_rate": 0.15, "royalty_wht_rate": 0.15,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Older treaty; some terms superseded by protocol."},

        {"treaty_partner": "China", "treaty_partner_iso": "CHN",
         "signed_date": "2007-07-11", "in_force_date": "2007-09-18",
         "dividend_wht_rate": 0.05, "interest_wht_rate": 0.07, "royalty_wht_rate": 0.06,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividend rate 5% if beneficial owner is a company holding >=25%; otherwise 10%."},

        {"treaty_partner": "France", "treaty_partner_iso": "FRA",
         "signed_date": "2015-01-15", "in_force_date": "2016-06-01",
         "dividend_wht_rate": 0.05, "interest_wht_rate": 0.10, "royalty_wht_rate": 0.0,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividend rate 5% if beneficial owner is a company holding >=10%; otherwise 15%."},

        {"treaty_partner": "Germany", "treaty_partner_iso": "DEU",
         "signed_date": "2004-06-28", "in_force_date": "2006-12-12",
         "dividend_wht_rate": 0.05, "interest_wht_rate": 0.08, "royalty_wht_rate": 0.08,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividend rate 5% if beneficial owner is a company holding >=10%; otherwise 15%."},

        {"treaty_partner": "Hong Kong SAR", "treaty_partner_iso": "HKG",
         "signed_date": "1997-03-24", "in_force_date": "1997-12-30",
         "dividend_wht_rate": 0.0, "interest_wht_rate": 0.0, "royalty_wht_rate": 0.05,
         "treaty_type": "limited_dta", "is_active": True,
         "notes": "Limited DTA covering shipping/air transport; broader DTA negotiated separately."},

        {"treaty_partner": "India", "treaty_partner_iso": "IND",
         "signed_date": "1994-01-24", "in_force_date": "1994-08-27",
         "dividend_wht_rate": 0.10, "interest_wht_rate": 0.15, "royalty_wht_rate": 0.10,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividend rate 10% if beneficial owner is a company holding >=25%; otherwise 15%. Protocol 2016 ended capital gains exemption."},

        {"treaty_partner": "Indonesia", "treaty_partner_iso": "IDN",
         "signed_date": "2020-02-04", "in_force_date": "2021-07-23",
         "dividend_wht_rate": 0.10, "interest_wht_rate": 0.10, "royalty_wht_rate": 0.10,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Revised treaty replacing 1990 agreement. Dividend rate 10% if beneficial owner is a company holding >=25%; otherwise 15%."},

        {"treaty_partner": "Ireland", "treaty_partner_iso": "IRL",
         "signed_date": "2010-10-28", "in_force_date": "2011-04-08",
         "dividend_wht_rate": 0.0, "interest_wht_rate": 0.05, "royalty_wht_rate": 0.05,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividends exempt under SG one-tier system."},

        {"treaty_partner": "Japan", "treaty_partner_iso": "JPN",
         "signed_date": "1994-04-09", "in_force_date": "1995-04-28",
         "dividend_wht_rate": 0.05, "interest_wht_rate": 0.10, "royalty_wht_rate": 0.10,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividend rate 5% if beneficial owner is a company holding >=25%; otherwise 15%."},

        {"treaty_partner": "Korea, Republic of", "treaty_partner_iso": "KOR",
         "signed_date": "2019-05-13", "in_force_date": "2020-01-01",
         "dividend_wht_rate": 0.10, "interest_wht_rate": 0.10, "royalty_wht_rate": 0.05,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Revised DTA effective 1 Jan 2020. Dividend rate 10% if beneficial owner is a company holding >=25%; otherwise 15%."},

        {"treaty_partner": "Luxembourg", "treaty_partner_iso": "LUX",
         "signed_date": "2013-10-09", "in_force_date": "2015-12-28",
         "dividend_wht_rate": 0.0, "interest_wht_rate": 0.0, "royalty_wht_rate": 0.07,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Holding company friendly; favoured by structuring intermediaries."},

        {"treaty_partner": "Malaysia", "treaty_partner_iso": "MYS",
         "signed_date": "2004-10-05", "in_force_date": "2006-02-13",
         "dividend_wht_rate": 0.05, "interest_wht_rate": 0.10, "royalty_wht_rate": 0.08,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividend rate 5% if beneficial owner is a company holding >=25%; otherwise 10%."},

        {"treaty_partner": "Netherlands", "treaty_partner_iso": "NLD",
         "signed_date": "1971-02-19", "in_force_date": "1971-09-03",
         "dividend_wht_rate": 0.0, "interest_wht_rate": 0.10, "royalty_wht_rate": 0.0,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Long-standing treaty heavily used for European holding structures."},

        {"treaty_partner": "New Zealand", "treaty_partner_iso": "NZL",
         "signed_date": "2009-08-21", "in_force_date": "2010-08-12",
         "dividend_wht_rate": 0.05, "interest_wht_rate": 0.10, "royalty_wht_rate": 0.05,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividend rate 5% if beneficial owner is a company holding >=10%; otherwise 15%."},

        {"treaty_partner": "Philippines", "treaty_partner_iso": "PHL",
         "signed_date": "1977-08-01", "in_force_date": "1977-11-18",
         "dividend_wht_rate": 0.15, "interest_wht_rate": 0.15, "royalty_wht_rate": 0.25,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Older treaty with relatively high rates."},

        {"treaty_partner": "Switzerland", "treaty_partner_iso": "CHE",
         "signed_date": "2011-02-24", "in_force_date": "2012-08-01",
         "dividend_wht_rate": 0.05, "interest_wht_rate": 0.05, "royalty_wht_rate": 0.05,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividend rate 5% if beneficial owner is a company holding >=10%; otherwise 15%."},

        {"treaty_partner": "Thailand", "treaty_partner_iso": "THA",
         "signed_date": "2015-06-11", "in_force_date": "2017-02-15",
         "dividend_wht_rate": 0.10, "interest_wht_rate": 0.10, "royalty_wht_rate": 0.05,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Revised treaty replacing 1975 agreement."},

        {"treaty_partner": "United Arab Emirates", "treaty_partner_iso": "ARE",
         "signed_date": "1995-12-01", "in_force_date": "1996-08-30",
         "dividend_wht_rate": 0.0, "interest_wht_rate": 0.07, "royalty_wht_rate": 0.05,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividends exempt under SG one-tier system."},

        {"treaty_partner": "United Kingdom", "treaty_partner_iso": "GBR",
         "signed_date": "1997-02-12", "in_force_date": "1997-12-26",
         "dividend_wht_rate": 0.0, "interest_wht_rate": 0.05, "royalty_wht_rate": 0.08,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividends exempt under SG one-tier system. Heavily used by UK-headquartered MNEs."},

        {"treaty_partner": "United States of America", "treaty_partner_iso": "USA",
         "signed_date": None, "in_force_date": None,
         "dividend_wht_rate": None, "interest_wht_rate": None, "royalty_wht_rate": None,
         "treaty_type": "limited_dta", "is_active": True,
         "notes": "No comprehensive DTA. Limited shipping/air-transport agreement only. US payments use domestic WHT rates."},

        {"treaty_partner": "Vietnam", "treaty_partner_iso": "VNM",
         "signed_date": "1994-03-02", "in_force_date": "1994-09-09",
         "dividend_wht_rate": 0.05, "interest_wht_rate": 0.10, "royalty_wht_rate": 0.05,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Tiered dividend rate: 5% if holding >=50%; 7% if >=25%; 12.5% otherwise. Lowest tier recorded."},

        {"treaty_partner": "South Africa", "treaty_partner_iso": "ZAF",
         "signed_date": "2015-11-23", "in_force_date": "2016-12-16",
         "dividend_wht_rate": 0.05, "interest_wht_rate": 0.075, "royalty_wht_rate": 0.05,
         "treaty_type": "comprehensive_dta", "is_active": True,
         "notes": "Dividend rate 5% if beneficial owner is a company holding >=10%; otherwise 10%."},
    ]


# ===========================================================================
# tax_instruments — derived from the corpus manifest
# ===========================================================================

def build_instruments_from_manifest(manifest: dict) -> list[dict]:
    """One row per document in the corpus manifest."""
    instruments = []
    for category_docs in manifest["documents"].values():
        for doc in category_docs:
            instruments.append({
                "instrument_id": doc["filename"].replace(".pdf", ""),
                "instrument_type": doc["instrument_type"],
                "title": doc["title"],
                "short_name": doc.get("short_name"),
                "publication_date": doc.get("publication_date"),
                "last_revised_date": doc.get("last_revised_date"),
                "topic_tags": doc.get("topic_tags", []),
                "applicable_to": doc.get("applicable_to", []),
                "status": doc.get("status", "in_force"),
                "superseded_by": None,
                "pdf_filename": doc["filename"],
                "source_url": doc["url"],
                "description": doc.get("notes", ""),
            })
    return instruments


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def save_json(records: list[dict], filename: str, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)
    print(f"  Wrote {len(records):4d} records -> {filepath}")


def save_sql_inserts(records: list[dict], table_name: str, output_dir: Path):
    """Emit INSERT statements for psql ingestion."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / f"seed_{table_name}.sql"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"-- Seed data for {table_name}\n")
        f.write(f"-- Generated by seed_iras_data.py at {datetime.now().isoformat()}\n")
        f.write(f"-- {len(records)} records\n\n")
        if not records:
            return
        cols = list(records[0].keys())
        cols_str = ", ".join(cols)
        for r in records:
            vals = []
            for c in cols:
                v = r.get(c)
                if v is None:
                    vals.append("NULL")
                elif isinstance(v, bool):
                    vals.append("TRUE" if v else "FALSE")
                elif isinstance(v, (int, float)):
                    vals.append(str(v))
                elif isinstance(v, list):
                    # PostgreSQL array literal
                    items = ", ".join(f'"{x}"' for x in v)
                    vals.append(f"'{{{items}}}'")
                else:
                    s = str(v).replace("'", "''")
                    vals.append(f"'{s}'")
            vals_str = ", ".join(vals)
            f.write(f"INSERT INTO {table_name} ({cols_str}) VALUES ({vals_str});\n")
    print(f"  Wrote SQL inserts -> {filepath}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Seed IRAS structured data")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                        help="Path to corpus manifest JSON")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="Output directory for seed JSON/SQL files")
    args = parser.parse_args()

    print("=" * 60)
    print("IRAS Structured Data Seed")
    print("=" * 60)

    # Build records
    print("\n[1/3] Building tax_rates...")
    tax_rates = build_tax_rates()

    print("[2/3] Building double_tax_agreements...")
    dtas = build_dtas()

    print("[3/3] Building tax_instruments from manifest...")
    if not args.manifest.exists():
        print(f"  ERROR: manifest not found at {args.manifest}", file=sys.stderr)
        sys.exit(1)
    with open(args.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    instruments = build_instruments_from_manifest(manifest)

    # Save JSON (consumed by ingestion_adapter.py)
    print("\nWriting JSON seed files...")
    save_json(tax_rates, "tax_rates.json", args.output_dir)
    save_json(instruments, "tax_instruments.json", args.output_dir)
    save_json(dtas, "double_tax_agreements.json", args.output_dir)

    # Save SQL (for direct psql ingestion)
    print("\nWriting SQL insert files...")
    save_sql_inserts(tax_rates, "tax_rates", args.output_dir)
    save_sql_inserts(instruments, "tax_instruments", args.output_dir)
    save_sql_inserts(dtas, "double_tax_agreements", args.output_dir)

    # Summary
    print("\n" + "=" * 60)
    print("SEED SUMMARY")
    print("=" * 60)
    print(f"  tax_rates:             {len(tax_rates):4d} records")
    print(f"  tax_instruments:       {len(instruments):4d} records")
    print(f"  double_tax_agreements: {len(dtas):4d} records")
    print(f"  Total:                 {len(tax_rates) + len(instruments) + len(dtas):4d} records")
    print("=" * 60)


if __name__ == "__main__":
    main()
