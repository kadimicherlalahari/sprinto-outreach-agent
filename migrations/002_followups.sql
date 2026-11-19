-- 002_followups: reserved fields for reply tracking and follow-ups (feature not connected yet).
-- reply_status stays 'not_tracked' until a reply-checking step exists; nothing writes reply data today.
ALTER TABLE sends ADD COLUMN reply_status TEXT NOT NULL DEFAULT 'not_tracked';  -- not_tracked | awaiting | replied | bounced
ALTER TABLE sends ADD COLUMN reply_checked_at TIMESTAMPTZ;
ALTER TABLE sends ADD COLUMN replied_at TIMESTAMPTZ;
ALTER TABLE sends ADD COLUMN follow_up_due_at TIMESTAMPTZ;                        -- set on live send: sent_at + FOLLOW_UP_DAYS
ALTER TABLE drafts ADD COLUMN follow_up_of TEXT;                                 -- draft id this one follows up (future)
ALTER TABLE drafts ADD COLUMN follow_up_number INTEGER NOT NULL DEFAULT 0;
