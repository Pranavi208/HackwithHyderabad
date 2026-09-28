"""Communications: stakeholder status updates (per severity cadence) and blameless post-mortems."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from . import llm
from .memory import BANK_MISSION, MemoryItem
from .triage import SEV_MATRIX, next_update_due

log = logging.getLogger(__name__)

ACTION_PLAIN = {
    "rollback": "rolling back the recent change", "restart": "performing a rolling restart",
    "scale": "adding capacity", "failover": "failing over to the secondary provider",
    "feature_flag": "switching the affected feature to its fallback", "vendor": "working with the third-party provider",
    "monitor": "monitoring a known transient issue", "suppress": "confirming there is no customer impact",
    "merge": "tracking this under the already-open incident", "escalate": "the incident team is investigating",
}


def status_update(incident: dict[str, Any], service: dict[str, Any], triage: dict[str, Any],
                  suggestion: dict[str, Any] | None, decision: dict[str, Any] | None = None) -> dict[str, str]:
    """Fill the stakeholder template for the current state. Returns {subject, body}."""
    sev = (suggestion or {}).get("severity") or triage["severity"]
    m = incident["metrics"]
    impact = f"{m.get('affected_users_pct', 0):g}% of users"
    if m.get("failed_txn_per_min"):
        impact += f" (~{m['failed_txn_per_min']:,} failed transactions per minute)"
    if decision:
        took = decision.get("mttr_min")
        return {
            "subject": f"[RESOLVED] {service['name']} — {incident['alert']['summary']}",
            "body": (f"**Resolution**: {ACTION_PLAIN[decision['action']].capitalize()}. {decision['note']}\n"
                     f"**Duration**: {incident['started_at'][11:]} IST, resolved in {took} min\n"
                     f"**Impact Summary**: {impact} affected at peak.\n"
                     f"**Follow-up**: Blameless post-mortem within 48 hours; action items tracked in {service['channel']}."),
        }
    cadence = SEV_MATRIX[sev]["cadence_min"]
    doing = ACTION_PLAIN[suggestion["action"]] if suggestion else "the on-call engineer is investigating"
    hyp = (suggestion or {}).get("hypothesis") or "Root cause not yet confirmed."
    return {
        "subject": f"[{sev}] {service['name']} — {incident['alert']['summary']}",
        "body": (f"**Status**: {'Identified' if suggestion and suggestion['action'] != 'escalate' else 'Investigating'}\n"
                 f"**Impact**: {impact} affected; {service['description'].lower()} degraded.\n"
                 f"**Current Understanding**: {hyp}\n"
                 f"**Actions Taken**: Incident declared {sev}, roles assigned; {doing}.\n"
                 f"**Next Update**: in {cadence} min (by {next_update_due(incident['started_at'], sev)[11:]} IST)."),
    }


POSTMORTEM_PROMPT = """Write a BLAMELESS post-mortem in Markdown using exactly this structure:
# Post-Mortem: <title>
**Date** / **Severity** / **Duration** / **Author**: WarRoom agent (draft) / **Status**: Draft
## Executive Summary (2-3 sentences)
## Impact (users affected, failed transactions, SLO budget note, support tickets)
## Timeline (IST) — a Markdown table built ONLY from the timeline events given
## Root Cause Analysis — ### What happened, ### Contributing Factors (Immediate / Underlying / Systemic), ### 5 Whys
## What Went Well
## What Went Poorly
## Action Items — Markdown table: ID | Action | Owner | Priority | Due Date | Status
## Lessons Learned
Rules: frame findings as what the SYSTEM lacked (guardrails, alerts, tests, runbooks), never what a person did.
If past incidents show the same root cause, say how many times it has happened and mark any still-open action
item as carried over with status "Overdue (repeat)". Owners are teams (e.g. @checkout-team). Use only facts given."""


def build_timeline(incident: dict[str, Any], suggestion: dict[str, Any] | None,
                   decision: dict[str, Any]) -> list[tuple[str, str]]:
    """Ordered (HH:MM, event) pairs reconstructed from the incident record (the scribe's log)."""
    start = datetime.fromisoformat(incident["started_at"])
    ev: list[tuple[datetime, str]] = []
    for ch in incident.get("recent_changes", []):
        if ch["service"] == incident["service_id"]:
            ev.append((datetime.fromisoformat(ch["at"]), f"{ch['type'].title()} {ch['ref']} ({ch.get('team')})"))
    ev.append((start, f"Alert fires: {incident['alert']['name']} — {incident['alert']['summary']}"))
    ev.append((start + timedelta(minutes=1), "WarRoom triage: severity classified, roles assigned"))
    if suggestion:
        ev.append((start + timedelta(minutes=2),
                   f"Agent recommends {suggestion['action']} ({round(suggestion['confidence'] * 100)}% confidence)"
                   + (" — paged human IC" if suggestion.get("needs_human") else "")))
    mttr = decision.get("mttr_min") or 0
    ev.append((start + timedelta(minutes=max(3, mttr - 5)), f"Mitigation: {ACTION_PLAIN[decision['action']]}"))
    ev.append((start + timedelta(minutes=max(4, mttr)), "Metrics back within SLO; incident resolved"))
    ev.append((start + timedelta(minutes=max(4, mttr) + 15), "All-clear sent to stakeholders"))
    return [(t.strftime("%H:%M"), e) for t, e in sorted(ev, key=lambda x: x[0])]


async def generate_postmortem(
    incident: dict[str, Any], service: dict[str, Any], triage: dict[str, Any],
    suggestion: dict[str, Any] | None, decision: dict[str, Any], history: list[MemoryItem],
) -> str:
    """Draft a blameless post-mortem from the incident, its resolution and related past incidents."""
    timeline = build_timeline(incident, suggestion, decision)
    due = (datetime.fromisoformat(decision["decided_at"]) + timedelta(days=14)).date().isoformat()
    facts = [
        f"Incident {incident['id']} on {service['name']} ({service['id']}), team {service['team']}, "
        f"started {incident['started_at']} IST. Alert: {incident['alert']['summary']}.",
        f"Severity: {(suggestion or {}).get('severity') or triage['severity']}. Metrics: "
        + ", ".join(f"{k}={v}" for k, v in incident["metrics"].items()),
        f"Support tickets: {incident.get('support_tickets', 0)}.",
        "Signals: " + "; ".join(s["message"] for s in triage["signals"]),
        "Logs: " + " | ".join(incident.get("logs", [])),
        f"Resolution: {decision['action']} in {decision.get('mttr_min')} min. Root cause given by the engineer: "
        f"{decision.get('cause') or 'not stated'}. Engineer note: {decision['note']}",
        f"Agent hypothesis: {(suggestion or {}).get('hypothesis', '')}",
        f"Default action item due date: {due}.",
        "Timeline events:\n" + "\n".join(f"{t} | {e}" for t, e in timeline),
        "Related past incidents from memory:\n" + ("\n".join(f"- {m.text}" for m in history) or "(none)"),
    ]
    try:
        return await llm.complete_text([
            {"role": "system", "content": BANK_MISSION + "\n\n" + POSTMORTEM_PROMPT},
            {"role": "user", "content": "\n".join(facts)},
        ], max_tokens=1800)
    except Exception as e:
        log.warning("post-mortem LLM failed: %s", e)
        rows = "\n".join(f"| {t} | {e} |" for t, e in timeline)
        return (f"# Post-Mortem: {incident['alert']['summary']}\n\n**Severity**: {triage['severity']} · "
                f"**Status**: Draft (LLM unavailable)\n\n## Timeline (IST)\n| Time | Event |\n|---|---|\n{rows}\n\n"
                f"## Resolution\n{decision['note']}\n")
