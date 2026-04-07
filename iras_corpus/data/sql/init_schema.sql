-- =============================================================================
-- IRAS Corporate Tax Compliance — SQL Schema
-- =============================================================================
-- Project 2, Phase 1 (IRAS swap): Database schema for structured tax rate,
-- instrument, and DTA data. Designed per the IRAS spec Source B.
--
-- Three tables, structurally analogous to the MAS schema:
--   1. tax_rates             — primary SQL query target (analogue: enforcement_actions)
--   2. tax_instruments       — metadata for routing decisions (analogue: regulatory_instruments)
--   3. double_tax_agreements — cross-referencing reference data (analogue: regulated_entities)
--
-- The structural shape match with the MAS schema is intentional: it's part of
-- validating that the pipeline doesn't depend on MAS-specific column names or
-- relationships. The agent sees this schema in its system prompt and generates
-- SQL directly — no separate text-to-SQL step.
--
-- Usage:
--   psql -d iras_compliance -f init_schema.sql
-- =============================================================================

DROP TABLE IF EXISTS tax_rates CASCADE;
DROP TABLE IF EXISTS tax_instruments CASCADE;
DROP TABLE IF EXISTS double_tax_agreements CASCADE;

-- ---------------------------------------------------------------------------
-- Table 1: tax_rates (primary structured query target)
-- ---------------------------------------------------------------------------
-- Time-series factual data the agent queries directly. Most "what was the
-- WHT rate on X in YA Y" or "when did the SUTE cap change" queries land here.

CREATE TABLE tax_rates (
    id                  SERIAL PRIMARY KEY,
    year_of_assessment  INTEGER NOT NULL,
    rate_category       TEXT NOT NULL                 -- Classification of rate
                        CHECK (rate_category IN (
                            'corporate_income_tax',
                            'cit_rebate',
                            'partial_exemption_first_tier',
                            'partial_exemption_second_tier',
                            'startup_exemption_first_tier',
                            'startup_exemption_second_tier',
                            'withholding_tax_interest',
                            'withholding_tax_royalty',
                            'withholding_tax_technical_assistance',
                            'withholding_tax_director_fees',
                            'withholding_tax_reit_distribution',
                            'other'
                        )),
    rate_value          DECIMAL(6, 4),                -- e.g. 0.17 for 17%
    threshold_amount    DECIMAL(15, 2),               -- NULL if rate is flat
    threshold_currency  TEXT DEFAULT 'SGD',
    applicable_to       TEXT,                         -- "all_companies", "qualifying_startups", etc.
    description         TEXT,                         -- Brief narrative description of the rate
    source_guide        TEXT,                         -- Reference to the e-Tax Guide that documents it
    effective_from      DATE,
    effective_to        DATE,                         -- NULL if still current
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

-- Indexes for common query patterns
CREATE INDEX idx_tax_rates_ya ON tax_rates (year_of_assessment);
CREATE INDEX idx_tax_rates_category ON tax_rates (rate_category);
CREATE INDEX idx_tax_rates_applicable ON tax_rates (applicable_to);
CREATE INDEX idx_tax_rates_effective_from ON tax_rates (effective_from);

-- ---------------------------------------------------------------------------
-- Table 2: tax_instruments (metadata for routing decisions)
-- ---------------------------------------------------------------------------
-- Enables the agent to check what's in the corpus before deciding tools.
-- Maps 1:1 with vector store documents.

CREATE TABLE tax_instruments (
    id                  SERIAL PRIMARY KEY,
    instrument_id       TEXT UNIQUE NOT NULL,         -- e.g., "etg_transfer_pricing_guidelines"
    instrument_type     TEXT NOT NULL
                        CHECK (instrument_type IN (
                            'e_tax_guide',
                            'circular',
                            'ruling',
                            'income_tax_act_section'
                        )),
    title               TEXT NOT NULL,
    short_name          TEXT,                         -- Human-friendly short name
    publication_date    DATE,
    last_revised_date   DATE,
    topic_tags          TEXT[],                       -- {"corporate_tax", "transfer_pricing", "withholding_tax", "dta", "anti_avoidance", "tax_incentive", "substance_requirements", "tax_administration"}
    applicable_to       TEXT[],                       -- {"resident_companies", "non_resident_companies", "holding_companies"}
    status              TEXT NOT NULL
                        CHECK (status IN (
                            'in_force',
                            'superseded',
                            'draft',
                            'historical'
                        )),
    superseded_by       TEXT,                         -- instrument_id of replacement
    pdf_filename        TEXT,                         -- Maps to vector store document
    source_url          TEXT,
    description         TEXT,                         -- Brief summary
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_instruments_type ON tax_instruments (instrument_type);
CREATE INDEX idx_instruments_status ON tax_instruments (status);
CREATE INDEX idx_instruments_topics ON tax_instruments USING GIN (topic_tags);
CREATE INDEX idx_instruments_applicable ON tax_instruments USING GIN (applicable_to);

-- ---------------------------------------------------------------------------
-- Table 3: double_tax_agreements (cross-referencing reference data)
-- ---------------------------------------------------------------------------
-- Enables "list X by Y" queries — "which DTAs cap dividend WHT at 5% or below",
-- "what's the interest WHT rate under the Singapore-Germany treaty", etc.
-- Source: IRAS DTA list page (iras.gov.sg/taxes/international-tax/list-of-dtas).
--
-- IMPORTANT: Tiered DTAs (e.g. China dividends 5%/10% by holding %) are
-- flattened to the LOWER tier with a note in the `notes` column. This matches
-- the MAS convention of flattening categorical nested data into a single row.
-- Some treaties have NULL rates (e.g. limited-DTA with US) — these columns
-- intentionally allow NULL.

CREATE TABLE double_tax_agreements (
    id                  SERIAL PRIMARY KEY,
    treaty_partner      TEXT NOT NULL,                -- Country name
    treaty_partner_iso  TEXT,                         -- ISO 3166 country code
    signed_date         DATE,
    in_force_date       DATE,
    dividend_wht_rate   DECIMAL(6, 4),                -- Reduced rate under treaty (NULL if N/A)
    interest_wht_rate   DECIMAL(6, 4),
    royalty_wht_rate    DECIMAL(6, 4),
    treaty_type         TEXT
                        CHECK (treaty_type IN (
                            'comprehensive_dta',
                            'limited_dta',
                            'tiea'
                        )),
    is_active           BOOLEAN DEFAULT TRUE,
    notes               TEXT,
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_dta_partner ON double_tax_agreements (treaty_partner);
CREATE INDEX idx_dta_iso ON double_tax_agreements (treaty_partner_iso);
CREATE INDEX idx_dta_active ON double_tax_agreements (is_active);
CREATE INDEX idx_dta_type ON double_tax_agreements (treaty_type);

-- ---------------------------------------------------------------------------
-- Useful views for common agent query patterns
-- ---------------------------------------------------------------------------

-- View: Currently effective tax rates (most recent rate per category)
CREATE OR REPLACE VIEW current_tax_rates AS
SELECT DISTINCT ON (rate_category, applicable_to)
    rate_category,
    applicable_to,
    rate_value,
    threshold_amount,
    threshold_currency,
    description,
    effective_from,
    effective_to,
    source_guide
FROM tax_rates
WHERE effective_to IS NULL OR effective_to >= CURRENT_DATE
ORDER BY rate_category, applicable_to, effective_from DESC;

-- View: Active comprehensive DTAs with all three reduced rates populated
CREATE OR REPLACE VIEW active_comprehensive_dtas AS
SELECT
    treaty_partner,
    treaty_partner_iso,
    in_force_date,
    dividend_wht_rate,
    interest_wht_rate,
    royalty_wht_rate,
    notes
FROM double_tax_agreements
WHERE is_active = TRUE
  AND treaty_type = 'comprehensive_dta'
ORDER BY treaty_partner;

-- View: Tax instruments currently in force
CREATE OR REPLACE VIEW active_instruments AS
SELECT
    instrument_id,
    instrument_type,
    title,
    short_name,
    publication_date,
    last_revised_date,
    topic_tags,
    applicable_to
FROM tax_instruments
WHERE status = 'in_force'
ORDER BY instrument_type, last_revised_date DESC NULLS LAST;

-- ---------------------------------------------------------------------------
-- Expected row counts (validation targets):
--   tax_rates:             ~30-50 records
--   tax_instruments:       12-15 records (1:1 with vector store documents)
--   double_tax_agreements: ~25 records (most commonly referenced partners)
-- ---------------------------------------------------------------------------

COMMENT ON TABLE tax_rates IS 'Time-series tax rate data: CIT, exemption tiers, withholding tax rates, rebates. Primary SQL query target.';
COMMENT ON TABLE tax_instruments IS 'Registry of IRAS e-Tax Guides and other instruments. Maps to vector store documents for routing decisions.';
COMMENT ON TABLE double_tax_agreements IS 'Singapore DTAs with reduced WHT rates. Used for cross-referencing tax queries against treaty terms.';
