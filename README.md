# WarRoom — the incident commander that remembers every outage

**HackwithHyderabad 3.0 · Memory layer: [Hindsight](https://hindsight.vectorize.io) by Vectorize**

At 3 AM the on-call engineer gets paged for `PostgresReplicaLagHigh`. It's the *sixth* time this month.
Someone on the team knows it's the nightly backup and harmless, but that knowledge lives in a Slack
thread from three weeks ago. The next page is a UPI success-rate drop: last time it was an NPCI blip,
so everyone waits… except this time NPCI is green and 42% of payments are failing.

**WarRoom** is an AI incident commander for *PaySetu*, a (fictional) Indian UPI/payments platform. It
triages every alert with a deterministic SEV1–SEV4 framework, assigns roles, and recommends a
remediation grounded in **Hindsight memories of how on-call engineers actually resolved past
incidents**. Every resolution is retained, so recurring failure modes get handled automatically over
time. It also flags *repeat incidents* whose post-mortem action items were never done, drafts blameless
post-mortems, and refuses to reuse an old fix when a familiar alert is actually a new failure mode.

## What it does

| Stage | How |
|---|---|
| **Triage** (deterministic, `app/triage.py`) | Severity matrix (SEV1–4) with escalation triggers: data integrity → SEV1, >25% users → SEV2, customer-reported tier-1 → min SEV2. Signals: correlated deploy / flag / config change (≤60 min), degraded vendor (NPCI, SMS provider, DigiLocker, issuer ACS), saturation ≥85%, error-rate & latency vs SLO, error-budget burn (14.4× page / 6× ticket), scheduled-job overlap, duplicate alert (≤30 min), no user impact |
| **Roles** | Incident Commander, Tech lead, Comms lead, Scribe assigned from on-call rotations *before* debugging starts; SEV1/2 get a dedicated IC and comms lead |
| **Recall** (Hindsight) | *Service memory*: how this service's past incidents were fixed, their root causes and open action items. *Pattern memory*: the same causal signals on other services |
| **Recommend** (LLM, `app/agent.py`) | One of rollback · restart · scale · failover · feature_flag · vendor · monitor · suppress · merge · escalate, with hypothesis, runbook steps, confidence, cited memories and a repeat-incident flag |
| **Guardrails** | Data integrity ⇒ SEV1 + human IC. Duplicate ⇒ merge. *Vendor* only if a vendor is actually degraded. *Rollback* only with a correlated change. *Suppress* only with zero user impact **and** a precedent from this service. Every other automatic fix needs a cited memory where the same fix worked. Severity can be upgraded, never downgraded. Any LLM/JSON failure ⇒ escalate |
| **Communicate** (`app/comms.py`) | Stakeholder update drafted per severity cadence (15/30/120 min), all-clear on resolution, and a **blameless post-mortem** (timeline, impact, 5 whys, action items) that pulls previous occurrences from memory |
| **Learn** | The engineer's resolution + root cause + note is retained to Hindsight. Reflect builds per-service reliability profiles and an org-wide incident review |

## Architecture

```
               ┌──────────────────────── React + Vite (port 5190) ─────────────────────────┐
               │ Incident board · Incident detail (roles, signals, recommendation, memory   │
               │ evidence, stakeholder update, resolve, post-mortem) · Service memory ·     │
               │ Learning curve · global MEMORY ON/OFF                                      │
               └──────────────────────────────────┬─────────────────────────────────────────┘
                                                  │ /api
┌──────────────────────────────────── FastAPI (port 8020) ─────────────────────────────────────┐
│                                                                                              │
│  alert ─▶ triage.py ──▶ memory.py ──recall──▶ ┌───────────────────────────┐                  │
│          signals,        service + pattern    │   Hindsight Cloud         │                  │
│          SEV, roles      memories             │   bank: warroom-sre       │                  │
│                │              │               │   tags: service:, signal:,│                  │
│                ▼              ▼               │   action:, cause:,        │                  │
│             agent.py (Groq gpt-oss-120b)      │   severity:, incident:    │                  │
│             JSON ▶ validator ▶ guardrails     └───────────────────────────┘                  │
│                │                                  ▲ retain         ▲ reflect                 │
│                ▼                                  │                │                         │
│      on-call resolves (action + root cause + note) ┘   service profile / incident review     │
│                │                                                                             │
│                └─▶ comms.py: status update, all-clear, blameless post-mortem                 │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

## How Hindsight memory is used

* **One bank, `warroom-sre`**, created with a mission describing an SRE team's institutional memory
  (blameless, focused on signals, fixes, root causes and action items) and a `retain_mission` telling
  Hindsight to extract service, alert, severity, exact signal numbers, remediation, time to resolve,
  root cause and open action items.
* **`retain`** after every resolution (`MemoryService.retain_resolution`). The memory is a
  self-contained sentence with exact numbers, e.g.
  *"On 2026-09-08 at 15:05 IST (Tuesday), incident INC-0908-02 hit Orders DB (Postgres) (orders-db, tier 1,
  team Data Platform). Alert PgbouncerPoolExhausted… Signals: SATURATION: pgbouncer pool at 98%…;
  SCHEDULED_JOB: finance reporting export… The on-call engineer did a ROLLING RESTART in 12 min. Root
  cause: reporting-job-on-primary. Engineer note: … Action item AI-DATA-7 still NOT done."*
  Tags: `service:orders-db`, `action:restart`, `cause:reporting-job-on-primary`, `signal:SATURATION`,
  `signal:SCHEDULED_JOB`, `severity:SEV3`, `incident:INC-0908-02`. `document_id` is per incident so
  re-resolving updates rather than duplicates. The incident start time is the memory timestamp, so
  Hindsight's temporal reasoning sees real incident chronology.
* **`recall`, two levels, before every recommendation**
  1. *Service memory* (`tags=[service:<id>]`, `any_strict`): what fixed this service before, typical
     magnitudes, open action items → enables auto-handling **and** repeat detection.
  2. *Pattern memory* (`tags=[signal:<causal signals>]`, own service filtered out): e.g. a first-ever
     deploy regression on a new service can cite rollbacks elsewhere as standard practice. Only causal
     signals (deploy, flag, config, vendor, saturation, scheduled job…) are used so generic symptoms
     don't create false precedent.
* **Citations are enforced.** The model must cite `[S1]`/`[P2]` labels; the guardrail checks each cited
  memory's `action:` tag. No cited precedent with the same fix ⇒ escalate to a human. This makes memory
  the *reason* a page is avoided, not decoration.
* **`reflect`** powers the *Service memory* tab: a per-service reliability profile (failure modes,
  what worked, noisy alerts, open action items) and an org-wide incident review ("which post-mortem
  action items keep not getting done?").
* **Memory toggle.** MEMORY OFF makes recall return nothing; with no precedents the guardrails page a
  human for every alert: the generic-runbook-bot baseline for before/after demos. Retain still runs.
* **Fallback.** With `HINDSIGHT_BASE_URL` empty, a local JSON stand-in with the same interface keeps
  the app working offline.

## Dataset (synthetic, `scripts/generate_data.py`)

12 services (checkout-api, upi-switch, payments-gateway, ledger-service, orders-db, session-cache,
auth-service, kafka-events, notification-service, search-service, kyc-service, merchant-dashboard), each
with team, on-call rotation, SLOs, vendors and scheduled jobs. **21 resolved August incidents** (history
to seed) and **58 September incidents** built from 14 recurring failure modes with consistent fixes:

| Service | Recurring pattern | Fix learned |
|---|---|---|
| checkout-api | 5xx after deploy (canary misses 5xx) | rollback |
| checkout-api | flag ramp breaks confirm | feature_flag |
| upi-switch | NPCI/PSP bank degradation, 6–9% failures | vendor |
| payments-gateway | issuer ACS slow | vendor |
| orders-db | replica lag during nightly backup, no impact | suppress |
| orders-db | pgbouncer pool exhausted by reporting job on primary (**repeat**, AI-DATA-7 open) | restart |
| session-cache | Friday 8 PM flash-sale evictions | scale |
| auth-service | Sunday 04:00 JWKS rotation 401 blip | monitor |
| kafka-events | EOD batch consumer lag | scale |
| notification-service | primary SMS provider throttling | failover |
| search-service | reindex heap pressure | restart |
| kyc-service | DigiLocker timeouts | feature_flag |
| ledger-service | EOD recon backlog, 0 mismatches | monitor |
| merchant-dashboard | cold cache after CDN purge | monitor |

**Anomalies that must not be auto-handled:** UPI failures at **42% with NPCI green** (a familiar alert
with a missing trigger → escalate, not "wait for vendor"); Kafka lag of 2.9M **at 14:05 with no EOD
batch** (scaling won't help: poison message); ledger **reconciliation mismatch of ₹48.7 lakh** (data
integrity → SEV1); and a **duplicate** checkout alert re-firing 12 minutes into an incident (→ merge).
The generator validates that every ground-truth fix has the signals it depends on.

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt        # Windows (use .venv/bin/pip elsewhere)
cp .env.example .env                                  # add GROQ_API_KEY, HINDSIGHT_BASE_URL, HINDSIGHT_API_KEY
python scripts/generate_data.py                       # already committed; regenerate if you change patterns
.venv\Scripts\python -m uvicorn app.api:app --port 8020
cd frontend && npm install && npm run dev             # http://localhost:5190
```

Optional: `python scripts/seed_history.py --reset` loads August into Hindsight from the CLI (or use
*Seed August history* in the UI). `pytest` runs the tests (triage rules, guardrails, memory; no network).
`npm run build` lets FastAPI serve the UI at http://localhost:8020 on its own.

## Demo story (5 minutes, built into the app)

The app opens on **Demo story**: eight slide-like chapters with one button each (← → keys navigate).
It uses two "brains", each with its own Hindsight bank and board state:

* **New brain** (`warroom-sre-fresh`): empty; the story teaches it live, so judges watch learning happen.
* **1 month of memory** (`warroom-sre`): taught by replaying all of September beforehand.

| # | Chapter | What the judges see |
|---|---|---|
| ★ | Meet WarRoom | The problem in one paragraph, 4-step how-it-works. *Start* wipes the new brain |
| 1 | Checkout starts failing | Bad deploy, empty memory → "I've never seen anything like this. Wake up a human." |
| 2 | The engineer fixes it | Engineer's one-paragraph note → "✓ Saved to Hindsight memory" (the exact memory shown) |
| 3 | Same alert, a week later | "I've seen this before. **Roll back.** Very sure (95%)", citing last week's incident, plus a *this keeps happening* warning. Last week vs today side by side |
| 4 | A month on the job | Real replay numbers: alerts needing a human **54% → 17%**, typical fix **25 → 6.5 min**, 15 h saved |
| 5 | When the past doesn't apply | UPI at 42% with NPCI healthy: memory says "vendor blip", WarRoom still escalates. Ledger mismatch → SEV1, duplicate → merged |
| 6 | Promises kept | Third pool exhaustion, "action item still pending" → one-click blameless post-mortem |
| 7 | The proof | Memory OFF on the chapter-3 incident → "Not sure (20%), wake a human". Back ON → "Roll back, 95%" |

**Before presenting:** run *Impact → Presenter tools → Replay September* once (≈20 min on free tiers);
it resumes if interrupted. Everything else in the story runs live in seconds.

Other views: **Incidents** (every alert in plain language; opening one asks WarRoom automatically; accept
its call with one click or choose something else; raw telemetry is under *Technical details*),
**Memory** (per-service reliability profiles and an org-wide incident review via Hindsight reflect) and
**Impact** (the learning curve).

## Project layout

```
app/triage.py     deterministic signals, SEV matrix, escalation triggers, roles
app/memory.py     Hindsight wrapper (retain/recall/reflect) + local stand-in
app/agent.py      prompt, JSON validation, guardrails, safe fallback
app/comms.py      stakeholder updates, blameless post-mortems
app/service.py    workflow, replay, metrics (pages to humans, MTTR, repeats)
app/api.py        FastAPI endpoints
scripts/          dataset generator, history seeder
tests/            pytest for triage, guardrails, memory
frontend/         React + Vite UI
```
