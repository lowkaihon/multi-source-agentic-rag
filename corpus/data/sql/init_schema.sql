-- =============================================================================
-- MAS Regulatory Compliance — SQL Schema
-- =============================================================================
-- Project 2, Phase 1: Database schema for structured enforcement and
-- regulatory instrument data. Designed per domain spec Section 2 (Source B).
--
-- Three tables:
--   1. enforcement_actions  — primary SQL query target
--   2. regulatory_instruments — metadata for routing decisions
--   3. regulated_entities — cross-referencing enforcement with entity profiles
--
-- Usage:
--   psql -d mas_compliance -f init_schema.sql
-- =============================================================================

-- Drop existing tables (for clean re-initialisation)
DROP TABLE IF EXISTS enforcement_actions CASCADE;
DROP TABLE IF EXISTS regulatory_instruments CASCADE;
DROP TABLE IF EXISTS regulated_entities CASCADE;

-- ---------------------------------------------------------------------------
-- Table 1: enforcement_actions (primary structured query target)
-- ---------------------------------------------------------------------------
-- Most structured queries will filter/aggregate here.
-- Source: OpenSanctions MAS dataset + MAS quarterly enforcement summaries.

CREATE TABLE enforcement_actions (
    id                  SERIAL PRIMARY KEY,
    entity_name         TEXT NOT NULL,               -- "DBS Bank Ltd", "John Doe"
    entity_type         TEXT NOT NULL                 -- "institution", "individual"
                        CHECK (entity_type IN ('institution', 'individual')),
    action_date         DATE NOT NULL,
    action_type         TEXT NOT NULL                 -- Classification of enforcement action
                        CHECK (action_type IN (
                            'composition_penalty',
                            'prohibition_order',
                            'reprimand',
                            'criminal_conviction',
                            'civil_penalty',
                            'warning',
                            'licence_revocation',
                            'licence_suspension',
                            'direction',
                            'other'
                        )),
    violation_category  TEXT NOT NULL                 -- Primary regulatory area breached
                        CHECK (violation_category IN (
                            'aml_cft',
                            'market_abuse',
                            'technology_risk',
                            'business_conduct',
                            'disclosure',
                            'unlicensed_activity',
                            'outsourcing',
                            'fit_and_proper',
                            'other'
                        )),
    penalty_amount      DECIMAL(15, 2),              -- NULL if non-monetary action
    penalty_currency    TEXT DEFAULT 'SGD',
    regulation_breached TEXT,                         -- e.g. "Notice 626", "SFA s197", "FAA s25"
    prohibition_years   INTEGER,                     -- NULL if not a prohibition order
    description         TEXT,                         -- Brief narrative summary
    source_url          TEXT,                         -- Link to MAS enforcement action page
    report_period       TEXT,                         -- e.g. "2022/2023", "2023/2024", "Q3_2025"
    opensanctions_id    TEXT,                         -- OpenSanctions entity ID for traceability
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

-- Indexes for common query patterns
CREATE INDEX idx_enforcement_action_date ON enforcement_actions (action_date);
CREATE INDEX idx_enforcement_entity_type ON enforcement_actions (entity_type);
CREATE INDEX idx_enforcement_action_type ON enforcement_actions (action_type);
CREATE INDEX idx_enforcement_violation ON enforcement_actions (violation_category);
CREATE INDEX idx_enforcement_entity_name ON enforcement_actions (entity_name);
CREATE INDEX idx_enforcement_report_period ON enforcement_actions (report_period);
CREATE INDEX idx_enforcement_regulation ON enforcement_actions (regulation_breached);

-- ---------------------------------------------------------------------------
-- Table 2: regulatory_instruments (metadata for routing decisions)
-- ---------------------------------------------------------------------------
-- Enables the agent to check what's in the corpus before deciding tools.
-- Maps 1:1 with vector store documents.

CREATE TABLE regulatory_instruments (
    id                  SERIAL PRIMARY KEY,
    instrument_id       TEXT UNIQUE NOT NULL,         -- "Notice_626", "TRM_Guidelines_2021"
    instrument_type     TEXT NOT NULL
                        CHECK (instrument_type IN (
                            'notice',
                            'guideline',
                            'circular',
                            'consultation_paper',
                            'information_paper',
                            'enforcement_report'
                        )),
    title               TEXT NOT NULL,
    short_name          TEXT,                         -- Human-friendly short name
    effective_date      DATE,
    last_revised_date   DATE,
    applicable_sectors  TEXT[],                       -- {"banking", "capital_markets", "insurance", "payments"}
    applicable_entities TEXT[],                       -- {"full_bank", "wholesale_bank", "fund_manager", "insurer"}
    topic_tags          TEXT[],                       -- {"aml_cft", "technology_risk", "outsourcing", "ai_governance"}
    status              TEXT NOT NULL
                        CHECK (status IN (
                            'in_force',
                            'superseded',
                            'consultation_open',
                            'consultation_closed',
                            'historical',
                            'draft'
                        )),
    superseded_by       TEXT,                         -- instrument_id of replacement, if superseded
    pdf_filename        TEXT,                         -- Maps to vector store document filename
    source_url          TEXT,
    description         TEXT,                         -- Brief summary of the instrument
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_instruments_type ON regulatory_instruments (instrument_type);
CREATE INDEX idx_instruments_status ON regulatory_instruments (status);
CREATE INDEX idx_instruments_topics ON regulatory_instruments USING GIN (topic_tags);
CREATE INDEX idx_instruments_sectors ON regulatory_instruments USING GIN (applicable_sectors);

-- ---------------------------------------------------------------------------
-- Table 3: regulated_entities (cross-referencing)
-- ---------------------------------------------------------------------------
-- Enables cross-referencing ("show me all enforcement actions against full banks").
-- Source: MAS Financial Institutions Directory.

CREATE TABLE regulated_entities (
    id              SERIAL PRIMARY KEY,
    entity_name     TEXT NOT NULL,
    entity_type     TEXT NOT NULL                     -- Classification of the entity
                    CHECK (entity_type IN (
                        'local_bank',
                        'full_bank',
                        'wholesale_bank',
                        'merchant_bank',
                        'finance_company',
                        'fund_manager',
                        'insurer',
                        'insurance_broker',
                        'capital_markets_intermediary',
                        'payment_service_provider',
                        'trust_company',
                        'financial_adviser',
                        'individual',
                        'other'
                    )),
    sector          TEXT NOT NULL                     -- Broad sector classification
                    CHECK (sector IN (
                        'banking',
                        'capital_markets',
                        'insurance',
                        'payments',
                        'trust',
                        'financial_advisory',
                        'other'
                    )),
    licence_types   TEXT[],                           -- {"full_bank", "capital_markets_services"}
    is_active       BOOLEAN DEFAULT TRUE,
    fi_directory_id TEXT,                             -- ID from MAS FI Directory, if available
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_entities_type ON regulated_entities (entity_type);
CREATE INDEX idx_entities_sector ON regulated_entities (sector);
CREATE INDEX idx_entities_name ON regulated_entities (entity_name);
CREATE INDEX idx_entities_active ON regulated_entities (is_active);

-- ---------------------------------------------------------------------------
-- Useful views for common agent query patterns
-- ---------------------------------------------------------------------------

-- View: Enforcement actions with entity details
CREATE OR REPLACE VIEW enforcement_with_entities AS
SELECT
    ea.id AS action_id,
    ea.entity_name,
    ea.entity_type,
    ea.action_date,
    ea.action_type,
    ea.violation_category,
    ea.penalty_amount,
    ea.penalty_currency,
    ea.regulation_breached,
    ea.prohibition_years,
    ea.description,
    ea.report_period,
    re.sector,
    re.licence_types,
    re.is_active
FROM enforcement_actions ea
LEFT JOIN regulated_entities re ON ea.entity_name = re.entity_name;

-- View: Enforcement summary by year and violation category
CREATE OR REPLACE VIEW enforcement_summary AS
SELECT
    EXTRACT(YEAR FROM action_date) AS year,
    violation_category,
    action_type,
    COUNT(*) AS action_count,
    SUM(penalty_amount) AS total_penalties,
    COUNT(DISTINCT entity_name) AS unique_entities
FROM enforcement_actions
GROUP BY EXTRACT(YEAR FROM action_date), violation_category, action_type
ORDER BY year DESC, action_count DESC;

-- View: Regulatory instruments currently in force
CREATE OR REPLACE VIEW active_instruments AS
SELECT
    instrument_id,
    instrument_type,
    title,
    short_name,
    effective_date,
    last_revised_date,
    applicable_sectors,
    topic_tags
FROM regulatory_instruments
WHERE status = 'in_force'
ORDER BY instrument_type, effective_date DESC;

-- ---------------------------------------------------------------------------
-- Row counts (actual, from Phase 1 ingestion):
--   enforcement_actions:     337 records (OpenSanctions FTM JSON)
--   regulatory_instruments:  32 records (1:1 with vector store documents)
--   regulated_entities:      317 records (OpenSanctions + 17 hand-seeded major FIs)
-- ---------------------------------------------------------------------------

COMMENT ON TABLE enforcement_actions IS 'MAS enforcement actions from OpenSanctions + quarterly summaries. Primary SQL query target.';
COMMENT ON TABLE regulatory_instruments IS 'Registry of MAS regulatory instruments. Maps to vector store documents for routing decisions.';
COMMENT ON TABLE regulated_entities IS 'Regulated financial institutions from MAS FI Directory. Used for cross-referencing enforcement actions.';
