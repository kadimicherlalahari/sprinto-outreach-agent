-- 004_feedback_gmail_costs: feedback loops, draft revision history, Gmail API outcome tracking,
-- kill switches + incidents, cost caps + spend ledger, campaigns/pilot and stored pilot reports.
-- Additive only: no existing rows are changed except defaults on new columns. Portable Postgres/SQLite.

-- ---------- campaigns / pilot ----------
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,                       -- active | paused
    paused_reason TEXT,
    pilot_start TEXT,                           -- ISO date set by the operator; NULL = pilot not started
    pilot_days INTEGER NOT NULL DEFAULT 30,
    thresholds_json TEXT,                       -- operator-set performance pause thresholds (no defaults)
    min_sample INTEGER,                         -- operator-set minimum sample before thresholds apply
    performance_paused BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
INSERT INTO campaigns (id, name, status, pilot_days, created_at, updated_at)
SELECT 'pilot', 'Pilot', 'active', 30, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
WHERE NOT EXISTS (SELECT 1 FROM campaigns WHERE id = 'pilot');

ALTER TABLE research_runs ADD COLUMN campaign_id TEXT;            -- NULL is treated as 'pilot'
ALTER TABLE research_runs ADD COLUMN prompt_version TEXT;
ALTER TABLE research_runs ADD COLUMN policy_version TEXT;

-- ---------- feedback (reviewer opinions; never facts) ----------
CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    kind TEXT NOT NULL,                         -- discovery | qualification | research | poc | draft | outcome
    verdict TEXT NOT NULL,                      -- e.g. relevant/irrelevant, should_qualify, wrong_company, unsuitable, approve
    reason TEXT,
    details_json TEXT,                          -- structured extras (source/query, preferred titles, evidence id, ...)
    reviewer TEXT NOT NULL,
    run_id TEXT,
    company_id TEXT,
    poc_id TEXT,
    draft_id TEXT,
    evidence_id TEXT,
    send_id TEXT,
    prompt_version TEXT,
    icp_version TEXT,
    policy_version TEXT,
    idem_key TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_feedback_idem ON feedback(idem_key);
CREATE INDEX IF NOT EXISTS idx_feedback_company ON feedback(company_id);

-- ---------- draft revisions (first generated, every edit, approved final, sent final) ----------
CREATE TABLE IF NOT EXISTS draft_revisions (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,                         -- generated | edit | approved | sent
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    author TEXT NOT NULL,
    reason TEXT,
    diff_json TEXT,                             -- field-level + unified text diff vs previous revision
    categories_json TEXT,                       -- subject | hook | pain_point | factual_claim | tone | cta
    created_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_draft_revision ON draft_revisions(draft_id, revision);
ALTER TABLE drafts ADD COLUMN parts_json TEXT;                    -- hook / pain / value / cta sentences at generation
ALTER TABLE drafts ADD COLUMN segment TEXT;                       -- cohort for scoped style guidance

CREATE TABLE IF NOT EXISTS style_guidance (
    id TEXT PRIMARY KEY,
    scope_role TEXT NOT NULL,                   -- POC role label or '*'
    scope_segment TEXT NOT NULL,                -- company segment or '*'
    rule TEXT NOT NULL,                         -- wording-only rule (never a factual claim)
    detail TEXT,
    evidence_json TEXT,                         -- revision ids it came from
    status TEXT NOT NULL,                       -- proposed | accepted | dismissed (operator decides)
    created_at TIMESTAMPTZ NOT NULL,
    decided_at TIMESTAMPTZ,
    decided_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_style_rule ON style_guidance(scope_role, scope_segment, rule);

-- ---------- POC selection ----------
ALTER TABLE pocs ADD COLUMN status TEXT NOT NULL DEFAULT 'active';   -- active | rejected
ALTER TABLE pocs ADD COLUMN lookup_prefs_json TEXT;                   -- preferences used for this lookup

-- ---------- sends: Gmail API outcome ----------
ALTER TABLE sends ADD COLUMN transport TEXT;                      -- gmail_api | smtp | none (dry-run)
ALTER TABLE sends ADD COLUMN gmail_message_id TEXT;
ALTER TABLE sends ADD COLUMN gmail_thread_id TEXT;
ALTER TABLE sends ADD COLUMN rfc822_message_id TEXT;
ALTER TABLE sends ADD COLUMN delivery_status TEXT;                -- accepted_by_gmail | bounced | unknown | not_sent
ALTER TABLE sends ADD COLUMN is_test BOOLEAN NOT NULL DEFAULT TRUE; -- own-inbox sends are tests/demos
ALTER TABLE sends ADD COLUMN campaign_id TEXT;
ALTER TABLE sends ADD COLUMN meeting_status TEXT;                 -- NULL | invite_received (calendar invitation seen)

CREATE TABLE IF NOT EXISTS gmail_mailbox (
    id TEXT PRIMARY KEY,                        -- 'primary'
    email TEXT,
    history_id TEXT,                            -- last fully processed history id
    watch_expiration TEXT,
    watch_status TEXT,                          -- active | failed | not_configured
    watch_error TEXT,
    last_sync_at TEXT,
    last_sync_status TEXT,                      -- ok | partial | failed
    last_sync_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS gmail_notifications (
    id TEXT PRIMARY KEY,
    pubsub_message_id TEXT NOT NULL,
    history_id TEXT,
    received_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL                        -- received | processed | failed
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_gmail_notification ON gmail_notifications(pubsub_message_id);

CREATE TABLE IF NOT EXISTS gmail_events (       -- raw mailbox facts (untrusted content, redacted)
    id TEXT PRIMARY KEY,
    gmail_message_id TEXT NOT NULL,
    thread_id TEXT,
    history_id TEXT,
    internal_date TEXT,
    label_ids TEXT,
    from_addr TEXT,
    subject TEXT,
    snippet TEXT,
    headers_json TEXT,
    event_type TEXT NOT NULL,                   -- reply | bounce | own_sent | meeting_invite | unmatched
    send_id TEXT,
    draft_id TEXT,
    is_test BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_gmail_event_msg ON gmail_events(gmail_message_id);

CREATE TABLE IF NOT EXISTS reply_classifications (  -- AI/rule interpretation, kept apart from raw events
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    category TEXT NOT NULL,                     -- positive | negative | question | out_of_office | opt_out | bounce | other
    method TEXT NOT NULL,                       -- rules | model
    confidence TEXT,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reply_class ON reply_classifications(event_id, method);

CREATE TABLE IF NOT EXISTS gmail_sync_runs (
    id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL,                      -- push | poll | recovery | manual
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,                       -- running | ok | partial | failed
    start_history_id TEXT,
    end_history_id TEXT,
    messages_seen INTEGER NOT NULL DEFAULT 0,
    events_added INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

-- ---------- kill switches + incidents ----------
CREATE TABLE IF NOT EXISTS control_events (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,                        -- global | campaign | performance | billable
    scope_id TEXT,
    action TEXT NOT NULL,                       -- pause | resume
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,                        -- human | system
    actor_name TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                         -- wrong_recipient | unauthorized_send | opt_out_ignored | unsupported_claim | uncertain_send
    status TEXT NOT NULL,                       -- open | resolved
    draft_id TEXT,
    send_id TEXT,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    resolved_by TEXT,
    resolution TEXT
);

-- ---------- cost caps + ledger ----------
CREATE TABLE IF NOT EXISTS cost_settings (
    name TEXT PRIMARY KEY,                      -- caps | prices | unit_limits
    value_json TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS cost_settings_history (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL,
    changed_by TEXT
);
CREATE TABLE IF NOT EXISTS cost_ledger (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    period_key TEXT NOT NULL,                   -- e.g. 2026-11 for a calendar month
    run_id TEXT,
    company_id TEXT,
    provider TEXT NOT NULL,                     -- tavily | gemini | hunter
    operation TEXT NOT NULL,
    model TEXT,
    units DOUBLE PRECISION,
    unit_kind TEXT,                             -- requests | tokens
    est_cost DOUBLE PRECISION,                  -- NULL when the price is unknown (never shown as zero)
    actual_cost DOUBLE PRECISION,               -- only if a provider reports billed cost (none do today)
    currency TEXT,
    cost_basis TEXT NOT NULL,                   -- estimated | actual | unknown_price
    status TEXT NOT NULL,                       -- reserved | settled | released
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_cost_period ON cost_ledger(period_key);
CREATE INDEX IF NOT EXISTS idx_cost_company ON cost_ledger(company_id);

-- ---------- pilot reports ----------
CREATE TABLE IF NOT EXISTS pilot_reports (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    kind TEXT NOT NULL,                         -- preliminary | final
    as_of TEXT NOT NULL,                        -- date the report covers up to
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pilot_report ON pilot_reports(campaign_id, kind, as_of);

-- existing sends were all to the operator's own inbox (enforced in code since 001): mark them test
UPDATE sends SET delivery_status = 'not_sent' WHERE status = 'dry_run' AND delivery_status IS NULL;
UPDATE sends SET transport = 'smtp' WHERE mode = 'live' AND transport IS NULL;
UPDATE sends SET delivery_status = 'accepted_by_gmail' WHERE status = 'sent' AND delivery_status IS NULL;
UPDATE sends SET delivery_status = 'unknown' WHERE status IN ('sending', 'uncertain') AND delivery_status IS NULL;
