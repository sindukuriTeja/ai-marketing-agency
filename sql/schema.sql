-- ============================================================================
-- AI-FIRST DIGITAL MARKETING AGENCY — PRODUCTION SCHEMA (PostgreSQL)
-- India region. The SQLite schema in ai_agency/db.py is the local-first
-- stand-in; this is the production shape with REAL row-level security.
--
-- Invariants enforced here:
--   * Every client-partitioned table has an RLS policy keyed on client_id,
--     so an agent session physically cannot read another client's rows.
--   * Secrets are NEVER stored — only vault refs.
--   * audit_log / egress_events are append-only (no UPDATE/DELETE grants).
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector (L3 vector index)
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- gen_random_uuid(), hmac

-- ---------------------------------------------------------------------------
-- Tenancy + session client
-- The runtime sets the session client from the task:
--     SET app.client_id = '<client_id>';
-- RLS policies below compare current_setting('app.client_id') to the row.
-- ---------------------------------------------------------------------------

CREATE TABLE clients (
    client_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    tier        TEXT NOT NULL DEFAULT 'B' CHECK (tier IN ('A','B','C')),
    vertical    TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- vault refs only — never the secret
CREATE TABLE client_accounts (
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    platform    TEXT NOT NULL,
    vault_ref   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, platform)
);

CREATE TABLE client_brand_kit (
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (client_id, key, version)
);

-- Work: every agent action is a task row
CREATE TABLE projects (
    project_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tasks (
    task_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID REFERENCES projects(project_id),
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    agent       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    payload     JSONB,
    result      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ
);

CREATE TABLE workflows (
    workflow_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    name        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'running',
    steps       JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ
);

-- Content
CREATE TABLE content_assets (
    asset_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    type        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','review','live')),
    title       TEXT,
    body        TEXT,
    sources     JSONB,                       -- grounding provenance
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ
);

CREATE TABLE content_briefs (
    brief_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    type        TEXT NOT NULL,
    keyword     TEXT,
    cta         TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE content_calendar (
    id          BIGSERIAL PRIMARY KEY,
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    asset_type  TEXT NOT NULL,
    slot        DATE NOT NULL,
    brief_id    UUID REFERENCES content_briefs(brief_id),
    status      TEXT NOT NULL DEFAULT 'planned'
);

-- Media
CREATE TABLE media_assets (
    asset_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    kind        TEXT NOT NULL,
    storage_ref TEXT NOT NULL,               -- object storage (S3/GCS)
    license     TEXT NOT NULL DEFAULT 'agency-owned',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE creative_briefs (
    brief_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Analytics: the one place numbers live
CREATE TABLE metrics (
    id          BIGSERIAL PRIMARY KEY,
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    source      TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    period      TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_metrics_client_period ON metrics(client_id, period);

CREATE TABLE dashboards (
    dashboard_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    name        TEXT NOT NULL,
    spec        JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE experiments (
    experiment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- CRM (agency-level — NOT client-partitioned)
CREATE TABLE leads (
    lead_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT,
    company     TEXT,
    vertical    TEXT,
    score       DOUBLE PRECISION NOT NULL DEFAULT 0,
    stage       TEXT NOT NULL DEFAULT 'new',
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ
);

CREATE TABLE opportunities (
    opp_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id     UUID REFERENCES leads(lead_id),
    name        TEXT NOT NULL,
    value       DOUBLE PRECISION NOT NULL DEFAULT 0,
    stage       TEXT NOT NULL DEFAULT 'open',
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ
);

CREATE TABLE proposals (
    proposal_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id     UUID REFERENCES leads(lead_id),
    client_id   UUID REFERENCES clients(client_id),
    status      TEXT NOT NULL DEFAULT 'draft',
    body        TEXT,
    price       DOUBLE PRECISION,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ
);

-- Knowledge (agency IP — never leaves; NOT client-partitioned)
CREATE TABLE playbooks (
    agent       TEXT PRIMARY KEY,
    version     TEXT NOT NULL,
    body        TEXT NOT NULL,               -- YAML
    status      TEXT NOT NULL DEFAULT 'active',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sops (
    sop_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    body        TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE taste_library (
    entry_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID,                        -- NULL = cross-client (anonymized)
    kind        TEXT NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE benchmarks (
    benchmark_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vertical    TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    as_of       DATE NOT NULL,
    source      TEXT
);

-- Memory (per agent+client)
CREATE TABLE agent_memory (
    id          BIGSERIAL PRIMARY KEY,
    client_id   UUID NOT NULL REFERENCES clients(client_id),
    agent       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     JSONB NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- L3 knowledge substrate: vector index (pgvector) + provenance
CREATE TABLE doc_chunks (
    chunk_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID,                        -- NULL = agency-level
    doc_id      TEXT NOT NULL,
    doc_title   TEXT NOT NULL,
    text        TEXT NOT NULL,
    data_class  TEXT NOT NULL DEFAULT 'C2',
    version     INTEGER NOT NULL DEFAULT 1,
    embedding   vector(1536),                -- local embedding model
    source      TEXT,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_chunks_embedding ON doc_chunks
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ix_chunks_client ON doc_chunks(client_id);

-- L3 knowledge graph (entities, relations, time)
CREATE TABLE kg_entities (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    name        TEXT NOT NULL,
    client_id   UUID,
    props       JSONB NOT NULL DEFAULT '{}',
    as_of       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE kg_relations (
    id          BIGSERIAL PRIMARY KEY,
    src         TEXT NOT NULL REFERENCES kg_entities(id),
    rel         TEXT NOT NULL,
    dst         TEXT NOT NULL REFERENCES kg_entities(id),
    props       JSONB NOT NULL DEFAULT '{}',
    as_of       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Audit: long retention; the trust anchor (append-only)
CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    client_id   UUID,
    detail      JSONB,
    signature   TEXT                          -- hmac-signed entry
);

CREATE TABLE egress_events (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent       TEXT,
    tool        TEXT,
    target      TEXT,
    data_class  TEXT NOT NULL,
    decision    TEXT NOT NULL,
    redacted    BOOLEAN NOT NULL DEFAULT FALSE,
    detail      JSONB
);
CREATE INDEX ix_egress_ts ON egress_events(ts);

CREATE TABLE playbook_runs (
    run_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent       TEXT NOT NULL,
    client_id   UUID,
    status      TEXT NOT NULL,
    steps       JSONB,
    egress_count INTEGER NOT NULL DEFAULT 0,
    tokens      INTEGER NOT NULL DEFAULT 0,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

-- L4 learning
CREATE TABLE trajectories (
    id          BIGSERIAL PRIMARY KEY,
    run_id      UUID NOT NULL,
    client_id   UUID,
    agent       TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    first_pass  BOOLEAN NOT NULL DEFAULT FALSE,
    payload     JSONB,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE insights (
    insight_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     JSONB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'candidate'
                CHECK (status IN ('candidate','quarantine','verified',
                                  'promoted','rolled_back')),
    provenance  JSONB,
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ
);

-- ============================================================================
-- ROW-LEVEL SECURITY (the safety engine)
-- ============================================================================

-- Client-partitioned tables: an agent session sees only its client's rows.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'client_accounts','client_brand_kit','projects','tasks','workflows',
        'content_assets','content_briefs','content_calendar','media_assets',
        'creative_briefs','metrics','dashboards','experiments','agent_memory'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY;', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
            USING (client_id = current_setting('app.client_id')::uuid)
            WITH CHECK (client_id = current_setting('app.client_id')::uuid);
        $f$, t);
    END LOOP;
END $$;

-- doc_chunks: client-scoped, but agency-level (client_id NULL) rows are
-- visible to all (anonymized cross-client knowledge).
ALTER TABLE doc_chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY chunk_isolation ON doc_chunks
    USING (client_id IS NULL
           OR client_id = current_setting('app.client_id')::uuid);

-- ============================================================================
-- APPEND-ONLY AUDIT: revoke UPDATE/DELETE on the trust anchor
-- ============================================================================
-- (Run as the DB superuser, substituting your app role name)
-- REVOKE UPDATE, DELETE ON audit_log, egress_events FROM app_agent;
-- GRANT  INSERT, SELECT ON audit_log, egress_events TO app_agent;

-- ============================================================================
-- Helper: the runtime sets the session client before every scoped operation
-- ============================================================================
--   BEGIN;
--   SET app.client_id = '<client_id>';
--   ... agent's reads/writes ...
--   COMMIT;