-- 003_reliability: resumable runs, one-active-run lock, per-company stage + budget + error state,
-- evidence source type/confidence/flags, recipient-bound approval, at-most-once live send,
-- suppression list, runtime flags (send pause), and a per-run source/action audit log.
-- Portable across Postgres (Neon) and SQLite (mock store). NULLs never collide in a UNIQUE index.

ALTER TABLE research_runs ADD COLUMN active_lock INTEGER;                -- 1 while queued/running, else NULL
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_run ON research_runs(active_lock);
ALTER TABLE research_runs ADD COLUMN heartbeat_at TIMESTAMPTZ;
ALTER TABLE research_runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1;

ALTER TABLE companies ADD COLUMN research_stage TEXT NOT NULL DEFAULT 'pending'; -- pending|researching|researched|contact|drafted|done|skipped
ALTER TABLE companies ADD COLUMN stop_code TEXT;
ALTER TABLE companies ADD COLUMN could_change BOOLEAN;                     -- could unresolved evidence change the result?
ALTER TABLE companies ADD COLUMN score_min INTEGER;
ALTER TABLE companies ADD COLUMN score_max INTEGER;
ALTER TABLE companies ADD COLUMN budget_json TEXT;
ALTER TABLE companies ADD COLUMN error_kind TEXT;                          -- blocked|unavailable|js_only|quota|outage|credentials|database|internal
ALTER TABLE companies ADD COLUMN retryable BOOLEAN;
ALTER TABLE companies ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;

ALTER TABLE evidence ADD COLUMN source_type TEXT;                          -- company_site|job_board|publication|third_party_estimate|unlisted
ALTER TABLE evidence ADD COLUMN confidence TEXT;                           -- high|medium|low
ALTER TABLE evidence ADD COLUMN flags_json TEXT;                           -- ["undated","stale","conflict","identity_unconfirmed",...]

ALTER TABLE drafts ADD COLUMN approved_recipient TEXT;                     -- OPERATOR_INBOX at approval time

ALTER TABLE sends ADD COLUMN live_lock TEXT;                               -- draft_id while sending/sent/uncertain, else NULL
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_live_send ON sends(live_lock);
ALTER TABLE sends ADD COLUMN resolved_by TEXT;
ALTER TABLE sends ADD COLUMN resolved_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS suppressions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                                                    -- domain | email
    value TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_suppression ON suppressions(kind, value);

CREATE TABLE IF NOT EXISTS app_flags (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    company_id TEXT,
    kind TEXT NOT NULL,                                                    -- page | search | contact_api | model
    target TEXT NOT NULL,                                                  -- URL or query (never credentials)
    outcome TEXT NOT NULL,                                                 -- ok | blocked | unavailable | not_found | js_only | quota | outage | credentials | skipped_budget
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_run ON fetch_log(run_id);

-- Backfill stages for rows created before this migration (so resume/retry see finished work as finished).
UPDATE companies SET research_stage = 'skipped' WHERE qualification IS NULL;
UPDATE companies SET research_stage = 'done' WHERE qualification IS NOT NULL;
UPDATE companies SET research_stage = 'researched'
  WHERE qualification = 'qualified' AND id NOT IN (SELECT company_id FROM pocs);
UPDATE companies SET error_kind = 'internal', retryable = FALSE WHERE qualification = 'error';
