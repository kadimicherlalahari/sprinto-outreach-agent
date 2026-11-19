# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
One operator, Sprinto's Growth Lead or the person running outbound. They start prospecting runs, check
the evidence behind each decision, review and approve each email draft, and send approved drafts to their own inbox.
Single reviewer. No team assignment.

## Product Purpose
An internal prospecting and cold-outreach agent for Sprinto, which sells SOC 2, ISO 27001 and GDPR compliance automation.
It discovers SaaS companies, qualifies them against a configurable ICP using cited evidence, finds and verifies
a point of contact, and drafts a personalised email. Success: the operator trusts every decision because each one traces
to a source, and only spends attention where the agent needs a human.

## Positioning
Evidence-first and human-approved. Every qualification and email hook links to a quoted source. Decisions are made by
code, not the LLM. Nothing is sent without approval of the exact version, and mail goes only to the operator's own inbox.

## Operating Context
Pipeline stages: discovered → researched → qualified → contact found → drafted → approved → sent.
Planned (not built yet): reply tracking → awaiting reply → replied / follow-up due after 3–7 days → follow-up sent.
Runs take one to three minutes. The operator checks in, reviews drafts, and adds manual Apollo contacts when Hunter finds none.

## Capabilities and Constraints
- Python + FastAPI, server-rendered HTML (`sprinto_agent/web.py`), bound to 127.0.0.1. Neon Postgres for real data, a separate SQLite mock store.
- Send mode defaults to dry-run. The only recipient is OPERATOR_INBOX.
- No scraping of LinkedIn or search engines. Never guess names, titles or emails.
- Reply tracking and follow-ups are reserved stages, shown as not connected until built.

## Brand Commitments
User-supplied reference (2026-11-19): a light, lavender and indigo SaaS dashboard with a left sidebar (Overview, Companies, Review drafts,
Sources), a primary "Run prospecting" action, a current-run table, and a review-queue callout. The user invited improvements on it.

## Evidence on Hand
Real runs stored in Neon, such as Momentic (qualified, draft pending) and Simile (qualified, no contact). Do not invent metrics,
customers or reply data. Mock mode uses clearly labeled fake companies on `.example` domains.

## Product Principles
1. Show what needs the operator now before anything else.
2. Every number and decision is clickable down to its evidence.
3. Make safety state (dry-run, recipient, approval) visible at all times.
4. Show placeholders for unbuilt features honestly, never fake activity.
