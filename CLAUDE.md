# WarRoom: Incident Response Commander with Hindsight memory

Hackathon project (HackwithHyderabad 3.0). Must use Hindsight (Vectorize) as the memory layer.
Judging: Innovation 30%, Hindsight memory use 25%, Technical 20%, UX/demo 15%, Impact 10%.
Persona/methodology spec: `C:\Users\avskd\Downloads\incident-response-commander.md`.

## What it does
An incident commander agent for "PaySetu", a fictional Indian UPI/payments platform. For every
production alert: deterministic triage (SEV1-SEV4 matrix + escalation triggers, deploy/flag
correlation, dependency status, saturation, error-budget burn, scheduled-job overlap, data integrity,
duplicate alerts), explicit role assignment (IC / Comms / Tech lead / Scribe), then an LLM recommends
a remediation (rollback / restart / scale / failover / feature_flag / vendor / monitor / suppress /
merge / escalate) with runbook steps, grounded in Hindsight memories of how on-call engineers
resolved past incidents. It flags repeat incidents whose post-mortem action items were never done,
drafts blameless post-mortems, and learns from every resolution.

## Stack
- Python venv in `.venv`, FastAPI on port 8020; React + Vite on port 5190 (`.claude/launch.json`)
- Groq `openai/gpt-oss-120b`, fallbacks `qwen/qwen3.8-27b`, `openai/gpt-oss-20b`
- `hindsight-client`, bank `warroom-sre`; empty `HINDSIGHT_BASE_URL` = local stand-in in `var/local_memory.json`
- Memories tagged `service:<id>`, `signal:<code>`, `action:<action>`, `cause:<slug>`, `incident:<id>`

## Conventions
- Type hints, small modules, docstrings on public functions; tests in `tests/` (run `pytest`)
- Regenerate data with `python scripts/generate_data.py`; it validates signals vs ground truth
- Severity is deterministic (`app/triage.py`); the LLM may upgrade it, never downgrade it
- Guardrails in `app/agent.py` must keep: data integrity => SEV1 + escalate, duplicate alert => merge,
  vendor needs a degraded dependency, rollback needs a correlated change, suppress needs a precedent
  from THIS service, every other auto action needs a cited precedent with that action; any failure => escalate
- Post-mortems are blameless: systems and guardrails, never people
- Never commit `.env`
