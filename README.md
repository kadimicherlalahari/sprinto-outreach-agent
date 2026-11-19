# Sprinto prospecting & cold-outreach agent

Finds SaaS companies that fit Sprinto's ICP, researches and qualifies them with cited evidence, finds a
verified point of contact, and drafts a personalised email. **A human approves every email**, and an
approved email is sent **only to your own inbox** (`OPERATOR_INBOX`), never to the prospect.

```
Stage 1  discover + shortlist     discovery.py   (domain list, RSS feeds, portfolio pages, Tavily search)
Stage 2  research + qualify       research.py, evidence.py, qualify.py   (agent loop, scores, stop rules)
         POC lookup + verify      contacts.py    (Hunter.io, or manual Apollo entry)
         draft                    drafting.py
Stage 3  review / send / learn    review.py, web.py   (approve | reject | edit; dry-run default)
Storage                           store.py + migrations/  (Neon Postgres; mock data in a separate SQLite file)
```

## 1. Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill it in; .env is git-ignored
```

| Variable | Needed for | Where to get it |
|---|---|---|
| `NEON_DATABASE_URL` | any real run (required) | neon.tech → project → Connection string (`postgresql://…?sslmode=require`) |
| `TAVILY_API_KEY` | search-based discovery and funding news | tavily.com (free tier) |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | LLM quote extraction + wording polish (optional) | aistudio.google.com |
| `HUNTER_API_KEY` | contact lookup (free: 25 searches/month) | hunter.io → API |
| `SMTP_USER`, `SMTP_APP_PASSWORD` | live sending via Gmail | Google account → 2-Step Verification → App passwords |
| `OPERATOR_INBOX` | any sending (the **only** recipient ever) | your own address |
| `SEND_MODE` | `dry-run` (default) or `live` | |
| `SENDER_NAME` | signature in drafts | |

Check what's missing: `.venv/bin/python -m sprinto_agent check-config`

## 2. Neon setup

1. Create a Neon project, copy the connection string into `NEON_DATABASE_URL`.
2. `.venv/bin/python -m sprinto_agent migrate` applies `migrations/*.sql` (tracked in `schema_migrations`).
3. Tables: `research_runs`, `companies`, `evidence`, `pocs`, `drafts`, `review_decisions`, `sends`, `run_log`.

Without `NEON_DATABASE_URL`, real runs stop with an explicit "nothing was saved" error. No fallback storage is used for real data.

## 3. Commands

```bash
.venv/bin/python -m sprinto_agent serve                 # UI at http://127.0.0.1:8000 (Run button, results, review)
.venv/bin/python -m sprinto_agent run --domains acme.com,beta.io     # CLI run (also uses feeds/search unless --no-feeds/--no-search)
.venv/bin/python -m sprinto_agent drafts                # list drafts
.venv/bin/python -m sprinto_agent show <draft_id>
.venv/bin/python -m sprinto_agent edit <draft_id> --body-file body.txt [--subject "..."]
.venv/bin/python -m sprinto_agent approve <draft_id>
.venv/bin/python -m sprinto_agent reject <draft_id> --note "why"
.venv/bin/python -m sprinto_agent send <draft_id>
.venv/bin/python -m sprinto_agent add-contact <company_id> --first A --last B --title CTO [--email a@co.com] --note "Apollo"
```

**The UI** (`serve`, then open http://127.0.0.1:8000):

| Page | What it's for |
|---|---|
| Overview | What needs you now: drafts to review, companies needing a contact, approved drafts to send, failures. Then the pipeline funnel (discovered, researched, qualified, contact found, drafted, approved, sent, replied) and the latest run, with live progress while a run is going. |
| Pipeline | Every company with its stage, ICP signals, score, contact and next step. Filter by stage and search. |
| Company | Stage tracker, decision and score against the threshold, ICP criteria, quoted evidence (plus rejected quotes and why), contact or the manual Apollo form, drafts. |
| Review queue / Draft | Edit, approve the exact version, reject, send. The side panel shows the hook's source, the contact, and the guessed pain points labeled as guesses. |
| Outreach | Sent and dry-run emails. Reserved stages for reply tracking and follow-ups (not connected yet; `FOLLOW_UP_DAYS`, default 3). |
| Runs / Settings | Run history and logs; which services are connected, ICP rules, sources, sending mode. |

**Mock mode** (`--mock` before the command, e.g. `python -m sprinto_agent --mock serve`): uses invented
companies on reserved `.example` domains, makes **no network calls**, writes only to
`data/mock/MOCK_FAKE_DATA.sqlite3` (every run flagged `is_mock`), shows a red MOCK banner, and refuses live sending.

## 4. Conditions required for an email to be sent (all enforced in `review.py`)

1. The draft status is `approved`.
2. The current content hash equals the approved hash. Any edit creates a new version, clears approval, and returns it to review. Approving a stale screen fails.
3. It hasn't already been sent (each version is sent at most once).
4. `OPERATOR_INBOX` is set. The message has exactly one recipient, that inbox, with no Cc or Bcc. The prospect's address is never used.
5. For a real send: `SEND_MODE=live`, not mock mode, and `SMTP_USER` + `SMTP_APP_PASSWORD` are set. Otherwise it's a **dry run**: the attempt is recorded and nothing is sent.

## 4b. Reliability and safety

**Bounded research.** Per company: `MAX_PAGES_PER_COMPANY`, `MAX_RESEARCH_ROUNDS`, `MAX_SEARCHES_PER_COMPANY`,
`MAX_LLM_CALLS_PER_COMPANY`, `MAX_SECONDS_PER_COMPANY`. Each company stores:
- **Stop code and reason:** `decision_stable`, `budget_pages`, `budget_time`, `no_sources`, `site_blocked`, `poc_quota`…
- **Score range:** the lowest and highest score still possible.
- **Could it change:** whether unresolved evidence could change the result, and which criteria are unresolved.
- **Budget used.**

**Resumable, no duplicates.**
- **One run at a time:** a unique database index allows only one active run, so double clicks and second tabs are refused.
- **Saved progress:** each company records its stage (`pending → researching → researched → contact → done`).
- **Crash recovery:** a run with no heartbeat for `STALE_RUN_MINUTES` becomes `interrupted`. **Resume** continues each company from its last completed stage. Partial evidence from an interrupted stage is replaced, and finished stages are never repeated.
- **No repeat work:** companies with an open draft or a send aren't researched again in later runs.

**Evidence states.**
- **Criterion status:** `verified`, `inferred` (tier 3, or a third party that doesn't link to the company), `not_found` (relevant pages were read and say nothing), `unknown` (couldn't check: blocked, unavailable or JS-only), and `conflict`. A failed fetch is never treated as absence.
- **Per quote:** source URL, retrieval date, publication date, source type, confidence, and flags (`undated`, `stale`, `conflict`, `identity_unconfirmed`, `identity_mismatch`, `injection_suspected`, `pii_redacted`).
- **Identity check:** a third-party article that links to a similarly named company's site is rejected.

**Sending.** All enforced in `review.py`, on top of section 4:
- **Recipient-bound approval:** approval is tied to the exact version *and* to `OPERATOR_INBOX`. If the inbox changes, the draft must be approved again.
- **At most once:** a live send claims a unique database lock before contacting Gmail.
  - If Gmail rejects the message before accepting it, the send is marked `failed` and you can retry.
  - If the connection drops during or after transmission, it's marked `uncertain` and **never retried automatically**. Check Gmail's Sent folder, then mark it "sent" or "not sent" (UI or `resolve-send`).
- **Opt-out list:** suppressed domains and emails are never researched, drafted or sent.
- **Pause switch:** Settings, `pause on`, or `SENDING_PAUSED=1` blocks every send.

**Failures.**
- **Classified:** site blocks, 404s, JS-only pages, API quota, outages, missing credentials and database errors are each recorded with a kind and whether they can be retried.
- **Isolated:** one company's failure never stops the run.
- **Retry:** Retry continues from the last completed stage (company page or `retry`).

**Audit (Neon).** For every run:
- `fetch_log`: every page, search, contact lookup and model call, with its outcome.
- `run_log`: the actions taken.
- Evidence: accepted *and* rejected quotes, each with a reason.
- Decisions: score range, stop reason, edits, approvals, and send results with who resolved them.
- **Redaction:** emails, API keys and database URLs are stripped from logs and error text. Personal emails and phone numbers are stripped from stored quotes.

```bash
.venv/bin/python -m sprinto_agent resume <run_id>
.venv/bin/python -m sprinto_agent retry <company_id>
.venv/bin/python -m sprinto_agent suppress add acme.com --reason "asked not to be contacted"
.venv/bin/python -m sprinto_agent pause on        # off | status
.venv/bin/python -m sprinto_agent resolve-send <send_id> not_sent   # or: sent
```

## 4c. Feedback, Gmail tracking, kill switches, costs and the pilot report

New pages: **Controls** (kill switches, incidents, cost limits, pilot thresholds, wording suggestions, source quality) and
**Pilot report**. Outreach now shows Gmail's answer, bounces and replies. Migration `004_feedback_gmail_costs.sql` adds the tables.

- **Feedback is an opinion, never a fact.** Company page: discovery (relevant/irrelevant + how it was found), ICP decision,
  research quality per evidence row, contact suitable/unsuitable. Stored in `feedback` with run/company/contact/draft ids and the
  prompt, ICP and policy versions. It never changes evidence, scores, the ICP or approvals. Two weak/wrong-company flags on a
  third-party domain stop it being fetched as evidence. Feedback written during a Neon outage is kept in `data/outbox/` and replayed.
- **Contact re-run.** "Unsuitable: look again" rejects the contact, supersedes its drafts and searches Hunter again with your preferred/
  excluded titles, departments and seniority. Only people Hunter returns are used; nobody is guessed.
- **Draft revisions.** The first generated draft, every edit (with a text diff and categories: subject, hook, pain point, factual claim,
  tone, CTA), the approved and the sent version are kept. Any edit clears approval. Edits only produce *proposed* wording rules
  (scoped by role and segment); they are used only after you accept them, and edits that add facts are ignored.
- **Gmail API.** Sends go through the Gmail API to `OPERATOR_INBOX` only. A 2xx response is recorded as `accepted_by_gmail`, never as
  delivered. Replies are matched by thread / Message-ID; your own SENT messages and unrelated mail are never stored. Raw events
  (`gmail_events`) and rule-based categories (`reply_classifications`: positive, negative, question, out of office, opt-out, bounce,
  other) are stored separately. An opt-out reply suppresses that contact at once. Sync runs on Pub/Sub push and every
  `GMAIL_SYNC_MINUTES`; watches are renewed a day before expiry; expired history falls back to re-reading tracked threads. Every send is
  a test/demo (own inbox) and is excluded from prospect metrics.
- **Kill switches.** Global pause, campaign pause and the pilot performance pause are re-checked at the final gate right before
  transmission. A wrong recipient, an unknown send seen in Gmail, an ignored opt-out, an edited claim not backed by evidence, or an
  uncertain send opens an incident and pauses sending. Resuming needs a named person, a reason, and no open incidents.
- **Cost caps.** Set a per-company cap, a monthly cap and, per provider, a unit price or (if unknown) a monthly unit limit. Paid research
  does not start until they are set. Each billable call reserves its estimated cost first (in a locked transaction, so parallel workers
  cannot overshoot). At the company cap research stops with `cost_cap_reached`, findings kept, unresolved facts listed. At the monthly
  cap all billable work pauses until a person resumes it; raising a cap does not resume anything.
- **Pilot report.** Stage-by-stage entered / advanced / did not advance / rate with the denominator named, for a 30-day window from the
  pilot start (preliminary before day 30). Test and prospect numbers are separate; meetings count only from calendar invitations.

```bash
.venv/bin/python -m sprinto_agent costs set --currency USD --per-company 0.50 --monthly 20 --price tavily=0.008 --limit gemini=500 --limit hunter=25
.venv/bin/python -m sprinto_agent gmail-auth            # once, after setting GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET
.venv/bin/python -m sprinto_agent gmail-sync            # --recovery re-reads every tracked thread
.venv/bin/python -m sprinto_agent feedback qualification should_reject --company <id> --reason "too small"
.venv/bin/python -m sprinto_agent poc-rerun <company_id> --preferred-titles "Head of Security" --reason "CTO too senior"
.venv/bin/python -m sprinto_agent campaign configure --max-bounce 0.05 --min-sample 30 --start 2026-12-01
.venv/bin/python -m sprinto_agent incidents             # resolve-incident <id> "what you checked"
.venv/bin/python -m sprinto_agent pause off --reason "incident resolved" --by "Your Name"
.venv/bin/python -m sprinto_agent report [--save] [--json]
```

## 5. ICP (`config/icp.yaml`)

From the case: B2B SaaS, Series A–C, preparing for an audit or under enterprise-customer pressure (SOC 2 / ISO 27001 / GDPR).
Assumptions are labeled `[assumption]` in the file: the 12-month funding window, trigger weights, POC role order, and employee range.
Must-haves: B2B SaaS and Series A–C (verified). Triggers (at least one verified): enterprise selling, compliance in progress,
compliance hiring, GDPR exposure, sensitive data, recent funding. Negatives: already holds both SOC 2 and ISO 27001; competitor (disqualifies).
Thresholds and weights live in the file. The code reads them and never hardcodes companies.

## 6. Sources and collection methods by stage

Credibility tiers are in `config/sources.yaml`:

- **Tier 1:** the company's own domain, plus job boards linked from its careers page. This is primary evidence.
- **Tier 2:** Reuters, Bloomberg, TechCrunch, ET, Mint, Moneycontrol, Inc42, Entrackr, YourStory, Business Standard, YC, PR Newswire, Business Wire, SEC.
- **Tier 3:** Crunchbase, Tracxn, PitchBook. Their claims are recorded as "inferred" only and never verify a must-have.
- **Anything else** is a lead only, never evidence.
- **`never_fetch`:** LinkedIn, search engines, and social sites.

| Stage | Sources | Method |
|---|---|---|
| 1 Discovery | Operator domain list; RSS feeds (TechCrunch startups, Inc42, Entrackr); portfolio pages you add; Tavily | Tavily API, restricted to tier-2 domains. Feeds and pages are fetched directly. The lead article is fetched before any claim is used, and the company domain comes only from a link in the article that matches the company name (never guessed). |
| 2 Research | Homepage, about, pricing, security/trust, customers, careers, press/blog, sitemap, PDFs, JSON-LD; Tavily for funding news | Direct fetch with robots.txt checks, a per-host delay, timeouts, retries and a 3 MB cap. Same-domain link targeting, then the sitemap, then search. Playwright is used only if a page looks JS-rendered and it's installed. |
| 2 Qualify | Validated evidence only | Code checks that each quote appears in the page, the source tier, and the company's identity (third-party pages must name the company). Compliance claims must be about the company itself, not its customers. Funding without a date is never "recent". Conflicts are flagged. Scoring is deterministic, and the stop rule ends research once more evidence can't change the decision, or when the budget or sources run out. The reason is stored. |
| 2 POC | Hunter Domain Search; manual Apollo entry | Keeps only personal, on-domain, confidence ≥ 70, full-name, titled contacts with a target role. The role is verified on company team/about pages, then on credible pages found by search. LinkedIn is never fetched. With no suitable POC, research stops for that company and the reason is recorded. |
| 3 Draft | Verified tier-1/2 evidence | One cited hook, a role pain **labeled as a guess**, a value statement from config, and a low-pressure question. Gemini may polish the wording, but its output is rejected if it adds numbers or drops the facts. |
| 3 Learn | `review_decisions` | Only wording rules (shorter, greeting, dropped closing line), each needing at least 2 edits and citing the review IDs. Edits never become evidence and never change the ICP. |

Gemini is used only to extract quotes from supplied text, which code then validates, and to polish wording. It never decides qualification.
All page content is treated as untrusted data: prompts wrap it as data, and outputs are validated.
Logs redact emails and key-like strings.

## 7. Tests

```bash
.venv/bin/python -m pytest -q
```
The tests run fully offline (a fixture fails any test that opens a socket). They cover ICP scoring and stop rules, quote and source validation,
unknown and conflicting facts, own-vs-customer compliance, company identity, contact filtering and no-guessed emails, approval/edit history,
the own-inbox-only rule, dry-run default, one-failure isolation, and mock/real separation.

## 7b. Evaluation suite (`evals/`)

`python -m evals.harness` runs 331 cases across the 40 decisions listed in `evals/decisions.py`. It uses fake data, mocked
integrations, no network and no real email. Each case is written as Input | Expected Behaviour | Assertion | Severity.
The report (`evals/out/report.md`, `results.json`) gives coverage by decision, partition, boundary (below / at / above every
cap and threshold) and severity. **Any Critical failure blocks live use** and makes the command exit 1, whatever the pass rate.

- Hard assertions are checked by code. Soft assertions (evidence relevance, typo vs meaningful edit) use the rubric judge in
  `evals/judge.py`, calibrated against `evals/calibration.json`; disagreements go to `evals/out/judge_disagreements.jsonl`.
  The calibration labels are reference labels written by Claude and still need the operator's review.
- Self-tests: `--break <ID>` changes one expected value and must make that case fail; `--repeat <ID>` runs a case twice and
  requires the same verdict and side effects. Both are also run by `tests/test_evals.py`.
- The suite covers the documented conditions only, not every real-world case. See "Assumptions" in the report.

Default cost limits live in `config/costs.yaml` (with the price sources and reasoning). They are loaded into Neon only if no
limits are saved there yet; after that, change them in Controls (every change is recorded).

## 8. Limitations

- Gmail API sending, push notifications and history sync were tested only against an in-memory fake Gmail. They have not run
  against a real mailbox yet. An OAuth app left in Google's "Testing" status issues refresh tokens that expire after 7 days.
- Reply categories are rule-based (keywords). Ambiguous replies land in "other" or "question"; check them on the Outreach page.
- Unsupported-claim detection is a heuristic (numbers, frameworks, funding words not present in verified evidence or the generated draft).

- Tavily, Hunter and Gemini have been called live, but their quota and outage error codes (e.g. 429 vs 432) were only exercised with mocks.
- Rule extraction is keyword-based and can miss facts phrased unusually. Gemini extraction helps, but every quote is still verified.
- The YC directory is JS-rendered. Add YC or VC portfolio URLs only where their terms allow it. Playwright isn't installed by default.
- The free Hunter tier (25/month) limits contact lookups. Use manual Apollo entry for the rest.
- Your school Google Workspace account may block app passwords. If so, use a personal Gmail account for `SMTP_USER`.
- The mock UI preview from the desktop app couldn't launch the server due to macOS folder permissions. Start it from a terminal instead.
