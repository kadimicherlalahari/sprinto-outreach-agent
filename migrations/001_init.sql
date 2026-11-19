-- 001_init: core schema. Written to run on Postgres (Neon) and on SQLite (mock store only).
-- IDs are UUID strings generated in Python; timestamps are ISO-8601 with timezone; JSON stored as text.

CREATE TABLE IF NOT EXISTS research_runs (
    id TEXT PRIMARY KEY,
    is_mock BOOLEAN NOT NULL,
    status TEXT NOT NULL,                 -- queued | running | completed | failed
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    input_json TEXT NOT NULL,             -- domains / discovery settings
    icp_version TEXT NOT NULL,
    summary_json TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES research_runs(id),
    name TEXT,
    domain TEXT NOT NULL,
    found_via TEXT NOT NULL,              -- why/how it was discovered
    lead_url TEXT,
    stage1_status TEXT NOT NULL,          -- shortlisted | rejected | uncertain
    stage1_reason TEXT NOT NULL,
    qualification TEXT,                   -- qualified | rejected | uncertain | error
    score INTEGER,
    assessment_json TEXT,                 -- per-criterion verified/inferred/unknown/conflict
    stop_reason TEXT,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_companies_run ON companies(run_id);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    criterion TEXT NOT NULL,
    value TEXT NOT NULL,
    status TEXT NOT NULL,                 -- verified | inferred | rejected
    quote TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_tier TEXT NOT NULL,            -- 1 | 2 | 3 | lead
    retrieved_at TIMESTAMPTZ NOT NULL,
    published_at TEXT,
    extractor TEXT NOT NULL,              -- rules | gemini | structured_data
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_company ON evidence(company_id);

CREATE TABLE IF NOT EXISTS pocs (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    full_name TEXT NOT NULL,
    title TEXT NOT NULL,
    email TEXT,                           -- only as returned by Hunter / entered manually; never generated
    role_label TEXT NOT NULL,
    source TEXT NOT NULL,                 -- hunter | manual_apollo
    source_detail_json TEXT NOT NULL,
    verification_status TEXT NOT NULL,    -- verified | unverified
    verification_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    poc_id TEXT NOT NULL REFERENCES pocs(id),
    run_id TEXT NOT NULL REFERENCES research_runs(id),
    original_subject TEXT NOT NULL,
    original_body TEXT NOT NULL,
    current_subject TEXT NOT NULL,
    current_body TEXT NOT NULL,
    current_hash TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,                 -- pending_review | approved | rejected | sent | dry_run_sent
    approved_hash TEXT,
    hook_evidence_id TEXT,
    pain_points_json TEXT NOT NULL,
    guidance_json TEXT NOT NULL,          -- which past reviews shaped wording
    composer TEXT NOT NULL,               -- template | gemini
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS review_decisions (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    run_id TEXT NOT NULL,
    decision TEXT NOT NULL,               -- approve | reject | edit
    reviewer TEXT NOT NULL,
    version INTEGER NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    note TEXT,
    decided_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS sends (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    content_hash TEXT NOT NULL,
    recipient TEXT NOT NULL,
    mode TEXT NOT NULL,                   -- dry-run | live
    status TEXT NOT NULL,                 -- sent | dry_run | failed
    error TEXT,
    sent_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    company_id TEXT,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
