# Sprinto Outreach Agent: Project Brief (for building a component map)

**What it is:** an AI-assisted prospecting and cold-outreach agent for Sprinto (a compliance-automation SaaS). It finds
B2B SaaS companies that fit Sprinto's ideal customer profile (ICP), researches them with cited evidence, finds the right
contact, drafts a personalised email, and sends it only after a human approves the exact version. It then tracks
replies in Gmail. Code decides qualification; the LLM only extracts quotes and polishes wording.

- **Live dashboard:** https://sprinto-outreach-agent-three.vercel.app. Demo login: username `professor`, password `sprinto-demo-2026` (also shown on the login page).
- **Code:** private GitHub repo `kadimicherlalahari/sprinto-outreach-agent`. Python 3.9, FastAPI, ~12k lines, 154 tests, 376-case eval suite.
- **Hard safety rule:** emails go **only to the operator's own inbox** (`OPERATOR_INBOX`), never to a prospect. Enforced in code at the last moment before sending.

---

## 1. Pipeline (stages and hand-offs)

```
[Sources] → 1 Discover → 2 Shortlist → 3 Research loop → 4 Qualify (score) → 5 Find contact (POC)
          → 6 Draft email → 7 Human review (edit / approve / reject) → 8 Send (final gate) → 9 Track replies
          → 10 Feedback & reporting
```

| # | Stage | What happens | Output |
|---|---|---|---|
| 1 | Discover | Reads news RSS feeds, Tavily news search (restricted to credible tier-2 sites), portfolio pages, or operator-supplied domains. De-duplicates by domain. | Candidate companies |
| 2 | Shortlist | Rejects platforms/social sites and competitors; reads the lead article's funding stage; never guesses a missing website. | shortlisted / uncertain / rejected |
| 3 | Research loop | Fetches the company's own pages (home, about, pricing, security/trust, careers, press, team), sitemap, job boards, then a news search if funding is unknown. Extracts quotes (rules + Gemini) and validates each one. Bounded by page / round / search / model-call / time / cost budgets. | Evidence rows with source, tier, dates, flags |
| 4 | Qualify | Deterministic ICP scoring with a threshold. Each criterion is verified / inferred / not_found / unknown / conflict. Records score range, stop reason, and whether more evidence could change the result. | qualified / uncertain / rejected |
| 5 | Find contact | Hunter domain search filtered by target role, confidence ≥ 70, own-domain email, verification; confirms the role on the company's team page. Manual Apollo entry as fallback. Never invents people or emails. | POC (verified / name_only / conflict) |
| 6 | Draft | Template from verified evidence: one cited hook (recent funding ≤ 6 months, compliance in progress, …), a labelled-as-guess pain point, value statement, low-pressure question. Optional Gemini rewrite, rejected if it adds facts or numbers. | Draft (version 1) |
| 7 | Review | Operator edits, approves or rejects. Approval is bound to the exact content hash and the current inbox. Any edit clears approval. Every revision and its diff are kept. | Approved version |
| 8 | Send | Final gate re-checks everything; Gmail API send; at-most-once lock; outcomes: accepted_by_gmail / failed / uncertain. | Send record + Gmail IDs |
| 9 | Track replies | Gmail history sync (cron or button; Pub/Sub push optional): match replies to the right thread, ignore the operator's own mail, classify, detect bounces / meeting invites, apply opt-outs. | Reply events + categories |
| 10 | Feedback & report | Reviewer feedback (opinions, never facts), wording suggestions, source demotion, pilot report, kill switches, cost ledger. | Report, controls |

## 2. Components (modules)

| Component | File | Responsibility | Talks to |
|---|---|---|---|
| CLI | `sprinto_agent/__main__.py` | run, resume, retry, approve, send, gmail-auth/sync, costs, campaign, report, feedback, poc-rerun… | everything |
| Web dashboard | `web.py`, `web_extra.py` | FastAPI server-rendered UI; login gate; pages below | Store, review, pipeline, gmail |
| Auth | `auth.py` | Password/demo login, signed session cookie, fail-closed on Vercel without a password | web |
| Config | `config.py`, `config/icp.yaml`, `config/sources.yaml`, `config/costs.yaml` | Settings from env; ICP rules; source tiers; cost defaults | all |
| Pipeline orchestrator | `pipeline.py` | Run lock, stages, resume/retry, metering, contact lookup, drafting hand-off | discovery, research, contacts, drafting, costs |
| Discovery | `discovery.py` | Feeds, search, portfolios, operator domains; dedupe; stage-1 shortlist | fetch, search, evidence |
| Fetcher | `fetch.py` | Polite HTTP: robots.txt, never-fetch list (LinkedIn etc.), per-host delay, retries, 3 MB cap, redirect check, JS-page detection | websites |
| Research loop | `research.py` | Picks pages per open question, budgets, stop decision, evidence flags | fetch, search, llm, evidence, qualify |
| Evidence validation | `evidence.py` | Quote must be in page; source tier; identity / name-collision check; own vs customer compliance; injection filter; PII redaction | — |
| Qualification | `qualify.py` | Criterion status, scoring, decision, could_change, Budget, stop rules | config ICP |
| Search client | `search.py` | Tavily API (leads only) | Tavily |
| LLM client | `llm.py` | Gemini REST: extraction prompt (page = untrusted data), usage tokens | Gemini |
| Contacts | `contacts.py` | Hunter client, eligibility filter, role verification, preferences, manual contact | Hunter |
| Drafting | `drafting.py` | Hook choice, template, LLM polish guard | llm |
| Review & send | `review.py` | Edit/approve/reject, send gate, recipient guard, Gmail/SMTP transport, outcome handling | gmail, safety, feedback |
| Gmail | `gmail.py` | OAuth (installed app), send, profile, watch, history, threads; FakeGmail for tests | Gmail API |
| Mailbox sync | `gmail_sync.py` | Watch renewal, history sync, recovery sync, push handling, thread matching, reply classification, opt-out, reply_state (waiting / no_reply) | gmail, safety |
| Safety | `safety.py` | Suppression list, global/campaign pause, incidents, resume rules, control log | Store |
| Costs | `costs.py` | Cost caps, spend ledger, reservations before every paid call, metered client wrappers | Store |
| Feedback | `feedback.py` | Feedback capture + offline outbox, draft revisions/diffs/categories, unsupported-claim check, style guidance, source demotion | Store |
| Reporting | `report.py` | 30-day pilot report, stage counts/denominators, test vs prospect split, performance pause | Store |
| Dashboard read model | `dashboard.py` | Snapshot reads (one Neon round trip), stage views, "Needs you" queue | Store |
| Store | `store.py` | Neon Postgres (psycopg, autocommit, reconnect, pipelined reads, transactions); SQLite for mock/tests | Neon |
| Versions | `versions.py` | Prompt / ICP / policy fingerprints recorded with runs and feedback | — |
| Vercel entry | `app.py`, `vercel.json` | Serverless dashboard + daily `/cron/gmail-sync` | Vercel |
| Eval harness | `evals/` | 376 cases across 48 decisions, rubric judge + calibration, report | the app |

**Dashboard pages:** Overview (pipeline funnel, per-stage cards, "Needs you" queue, run launcher), Pipeline,
Company (decision, ICP criteria, evidence, contact, feedback), Review queue, Draft (editor, revisions/diffs, approve,
send), Outreach (Gmail results, replies, sync status), Pilot report, Controls (kill switch, incidents, cost limits,
pilot thresholds, wording suggestions, source quality), Runs, Settings, plus `/login`, `/healthz`, `/cron/gmail-sync`, `/gmail/push`.

## 3. External services

| Service | Used for | Where it runs |
|---|---|---|
| Neon Postgres | All persistent data | both |
| Tavily | News search (leads only; pages always fetched before a claim is used) | local runs |
| Google Gemini (`gemini-3.5-flash-lite`) | Quote extraction from fetched pages; optional wording pass | local runs |
| Hunter.io | Contact lookup (domain search) | local runs |
| Gmail API (OAuth, scopes `gmail.send` + `gmail.readonly`) | Send to operator inbox; read replies in tracked threads | both |
| Vercel | Hosts the dashboard; daily cron for Gmail sync | cloud |
| GitHub | Private repo; push deploys to Vercel | cloud |

Research runs take minutes, longer than Vercel functions allow, so they run from the operator's machine (`python -m sprinto_agent run`). The website is for review, sending, tracking and reporting.

## 4. Data model (Neon tables, 4 migrations)

- **Pipeline:** `research_runs` (status, lock, heartbeat, versions, campaign) → `companies` (stage-1 status, qualification, score + range, stop reason/code, could_change, research_stage, budget, error kind) → `evidence` (criterion, value, verbatim quote, source URL, tier, type, confidence, published/retrieved dates, flags) → `pocs` (name, title, email, source, verification, status) → `drafts` (original/current text, version, content hash, approved hash + recipient, parts, segment) → `draft_revisions`, `review_decisions` → `sends` (status, mode, transport, Gmail message/thread IDs, delivery_status, reply_status, is_test, follow-up due).
- **Mailbox:** `gmail_mailbox` (cursor, watch state, last sync), `gmail_notifications`, `gmail_events` (raw facts), `reply_classifications` (interpretation, kept separate), `gmail_sync_runs`.
- **Control and learning:** `feedback`, `style_guidance`, `suppressions`, `app_flags`, `control_events`, `incidents`, `campaigns`, `cost_settings` + history, `cost_ledger`, `pilot_reports`, `fetch_log` (audit), `run_log`.
- **Current live data:** 8 runs, 57 company rows, 531 evidence rows, 3 contacts, 6 drafts (2 waiting for review), 2 sends, 4 Gmail events.

## 5. Decision points (where the system judges something)

The eval suite covers 48 decisions (D01–D40 system decisions, plus C1–C8 from the class Sprinto tables):

- **Run control:** one active run; stale-run recovery; resume from the last completed stage; skip opted-out, duplicate or over-limit candidates.
- **Discovery:** dedupe; shortlist; fetch policy (scheme, never-fetch, robots, HTTP status, redirects, size cap); source tiers.
- **Research:** accept / downgrade / reject quotes; criterion status; funding recency (366 days; draft hook ≤ 183 days); score vs threshold 60; budget/stop rules; unreadable site → conclude nothing, retry later.
- **ICP rules (class spec):**
  - **Must-haves:** B2B SaaS, and funding stage Series A–C. Seed, Series D+ and public companies are rejected.
  - **Disqualifiers:** an already-held SOC 2 or ISO 27001 certification, or being a competitor.
  - **Size:** a soft 50–200 band. 40–300 is accepted; a verified size outside that is rejected.
  - **Triggers (weights):** compliance in progress 30, enterprise selling 25, compliance hiring 20, recent funding 20, GDPR exposure 15, sensitive data 10. At least 1 must be verified.
- **Contact:** eligibility filter; role verification; preference re-run; manual fallback.
- **Draft and review:** hook choice; model rewrite guard; edit clears approval; approval bound to version and recipient; unsupported-claim check; edit categories (typo vs meaningful, including punctuation that changes tone); style guidance scope (newest accepted rule wins).
- **Sending:** final gate (pause, campaign pause, performance pause, suppression, approval hash, recipient, dry-run/live, Gmail connected); recipient guard; at-most-once lock; outcome classification.
- **Mailbox:**
  - **Event types:** own message, reply, bounce or meeting invite.
  - **Reply categories:** positive, negative, question, ambiguous (routed to a person), out of office, opt-out (suppresses the contact), bounce, other.
  - **No reply:** shown once the follow-up window (3 days) passes.
- **Controls:** incidents auto-pause sending; resuming needs a named person, a reason and no open incidents; performance pause needs a minimum sample; cost caps; redaction of secrets and PII; untrusted links rendered inert.

## 6. Safety and reliability controls (summary)

- **Recipient:** only the operator's inbox; checked at build time and again at the final gate. A wrong recipient opens an incident and pauses sending.
- **Human approval:** for one exact version; any edit clears it; approval is bound to the current inbox.
- **At-most-once sending:** a unique database lock is taken before Gmail is called. An uncertain outcome is never retried automatically; the operator resolves it, with an optional Gmail lookup to help.
- **Kill switches:** global, campaign and performance pauses, persisted in Neon and audited.
- **Cost limits** (`config/costs.yaml`): $0.25 per company, $20 per month.
  - **Prices:** Tavily $0.008 per search; Gemini estimated at $2.50 per 1M tokens; Hunter's price is unknown, so it's capped at 40 lookups a month.
  - **Enforcement:** cost is reserved before each call, inside a database lock, so parallel calls can't overspend. Hitting the monthly cap pauses billable work until a person resumes it.
- **Untrusted content:** pages, search results, model output and emails are treated as data. They can't change approvals, recipients, scores, opt-outs, caps or kill switches.
- **Evidence:** a quote must appear verbatim in the fetched page. The app checks the company's identity, flags conflicts, and never treats a failed fetch as absence of evidence.
- **Secrets:** API keys, tokens, database URLs and emails are redacted from logs.
- **Isolation and recovery:** one company's failure doesn't stop the run; resume skips finished work; feedback written during an outage is queued locally and replayed; the Gmail cursor only advances after events are saved.

## 7. Evaluation and test results

- **Unit/integration tests:** `pytest -q` → **154 passed** (offline; sockets blocked).
- **Eval harness:** `python -m evals.harness` → **376 cases, 376 passed, 0 Critical failures** (120 Critical, 187 High, 69 Low; 67+ boundary cases at below/at/above every cap and threshold).
  - Each case is written as Input | Expected | Assertion | Severity.
  - Hard assertions are checked by code. Soft ones use a rubric judge, calibrated against 22 labelled examples (20/22 agree; disagreements logged).
  - Any Critical failure blocks live use.
  - Self-tests: breaking one expected value is caught as a Critical failure; running a case twice gives the same verdict and side effects.
- **Class spec cases (C1–C8):** every row of the class tables plus the missing partitions. Decisions 7–8 (call scheduling, meeting booking, CRM) are **not built** and are listed as such.
- **Bugs found by the evals and fixed:**
  - A redirect into LinkedIn bypassed the never-fetch list.
  - Google OAuth tokens leaked into logs.
  - A float rounding error refused calls landing exactly on a cost cap.
  - Companies with only inferred signals showed "can't change", so research stopped too early.
  - JavaScript-only page detection missed single-quoted markup.
  - One oversized chunk could pass the size cap.
  - Typo-only edits were counted as tone, and conflicting wording rules were applied in random order.
  - "Remove us" wasn't recognised as an opt-out.
  - There was no ambiguous or no-reply status.
  - Punctuation that changes tone was treated as a typo.

## 8. Real-world results so far

- **Real runs:** 5 on Neon (≈ $1.18 of the $20 monthly cap; 5 Hunter lookups).
- **Qualified:** Clueso (90), Parakeet Health (85), AuxoAI (80), Simile (75). Namespace and Momentic qualified under the earlier rules but are "already certified (SOC 2)" under the class rules.
- **Drafts waiting for review:** Clueso (Prajwal Prakash, CTO) and Parakeet Health (Aaron Lee, CTO). AuxoAI and Namespace need a manual Apollo contact (Hunter's people there aren't target roles).
- **Live Gmail tests:** two real sends to the operator's inbox, accepted by Gmail; a real reply from another account was matched to the right thread and classified.

## 9. Known limits

- Research runs locally, not on Vercel.
- Mailbox sync is daily by cron on Vercel's Hobby plan, or on demand via "Sync Gmail now". Real-time Pub/Sub push needs a topic set up.
- Reply categories and the unsupported-claim check are rule-based heuristics.
- Calibration labels were written by Claude and need human review.
- Playwright isn't installed, so JavaScript-only sites are recorded as unreadable rather than rendered.
- Call scheduling, meeting booking and CRM updates (class Decisions 7–8) aren't built.
- Sending to real prospects isn't built, by design.
