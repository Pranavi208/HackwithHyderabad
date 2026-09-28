"""Deterministic incident triage: signals, SEV1-SEV4 classification and role assignment.

Nothing here calls an LLM. Severity is never skipped: it drives escalation, the stakeholder
update cadence and who gets paged. The agent may upgrade it, never downgrade it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

# ---- signal codes -------------------------------------------------------------
DATA_INTEGRITY = "DATA_INTEGRITY"
DUPLICATE_ALERT = "DUPLICATE_ALERT"
DEPLOY_CORRELATED = "DEPLOY_CORRELATED"
FLAG_CHANGE = "FLAG_CHANGE"
CONFIG_CHANGE = "CONFIG_CHANGE"
DEPENDENCY_DEGRADED = "DEPENDENCY_DEGRADED"
SATURATION = "SATURATION"
ERROR_RATE = "ERROR_RATE"
LATENCY_SLO = "LATENCY_SLO"
ERROR_BUDGET_BURN = "ERROR_BUDGET_BURN"
SCHEDULED_JOB = "SCHEDULED_JOB"
CUSTOMER_REPORTED = "CUSTOMER_REPORTED"
NO_USER_IMPACT = "NO_USER_IMPACT"

CHANGE_SIGNALS = {"deploy": DEPLOY_CORRELATED, "flag": FLAG_CHANGE, "config": CONFIG_CHANGE}

CHANGE_WINDOW_MIN = 60      # a change this close before the alert is a suspect
DUPLICATE_WINDOW_MIN = 30   # same service + alert again within this window = duplicate
SATURATION_LIMIT = 0.85
PAGE_BURN_RATE = 14.4       # budget gone in ~2h
TICKET_BURN_RATE = 6.0      # budget gone in ~5d

# ---- severity framework (from the incident commander playbook) -------------------
SEV_MATRIX: dict[str, dict[str, Any]] = {
    "SEV1": {"name": "Critical", "criteria": "Full outage, data loss risk, security breach",
             "response": "< 5 min", "cadence_min": 15, "escalation": "VP Eng + CTO immediately"},
    "SEV2": {"name": "Major", "criteria": "Degraded service for >25% users, key feature down",
             "response": "< 15 min", "cadence_min": 30, "escalation": "Eng Manager within 15 min"},
    "SEV3": {"name": "Moderate", "criteria": "Minor feature broken, workaround available",
             "response": "< 1 hour", "cadence_min": 120, "escalation": "Team lead next standup"},
    "SEV4": {"name": "Low", "criteria": "No user impact, tech-debt trigger",
             "response": "Next business day", "cadence_min": 1440, "escalation": "Backlog triage"},
}
SEVERITIES = tuple(SEV_MATRIX)

IC_ROTATION = ["Ananya Krishnan", "Rohit Menon", "Farah Qureshi", "Vikram Reddy"]
COMMS_ROTATION = ["Sneha Kulkarni", "Harsh Vardhan", "Lakshmi Pillai"]


@dataclass
class Signal:
    """One triage finding on an alert."""

    code: str
    severity: str  # low | medium | high
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sev_rank(sev: str) -> int:
    """SEV1 -> 1 ... SEV4 -> 4 (lower is worse)."""
    return int(sev[-1])


def worst(a: str, b: str) -> str:
    """The more severe of two severities."""
    return a if sev_rank(a) <= sev_rank(b) else b


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _in_job_window(when: datetime, job: dict[str, Any]) -> bool:
    days = job.get("days", "daily")
    if days != "daily" and when.strftime("%a") not in days:
        return False
    hh, mm = (int(x) for x in job["start"].split(":"))
    start = hh * 60 + mm
    end = start + job["minutes"] + 15          # effects linger a little after the job
    t = when.hour * 60 + when.minute
    return start - 5 <= t <= end or (end >= 1440 and t <= end - 1440)


def check_incident(
    incident: dict[str, Any], service: dict[str, Any], prior: list[dict[str, Any]] | None = None
) -> list[Signal]:
    """Run every deterministic check on an alert. ``prior`` = incidents that started earlier."""
    out: list[Signal] = []
    started = _ts(incident["started_at"])
    m = incident["metrics"]
    slo = service["slo"]

    di = incident.get("data_integrity")
    if di and di.get("mismatch_count", 0) > 0:
        out.append(Signal(DATA_INTEGRITY, "high",
                          f"{di['mismatch_count']} ledger entries disagree with bank settlement "
                          f"(INR {di.get('amount_inr', 0):,.0f})", dict(di)))

    for p in prior or []:
        if p["service_id"] == incident["service_id"] and p["alert"]["name"] == incident["alert"]["name"]:
            gap = (started - _ts(p["started_at"])).total_seconds() / 60
            if 0 <= gap <= DUPLICATE_WINDOW_MIN:
                out.append(Signal(DUPLICATE_ALERT, "medium",
                                  f"Same alert on {service['id']} already opened as {p['id']} {gap:.0f} min ago",
                                  {"parent": p["id"], "gap_min": round(gap)}))
                break

    seen: set[str] = set()
    for ch in incident.get("recent_changes", []):
        if ch["service"] != incident["service_id"]:
            continue
        code = CHANGE_SIGNALS.get(ch["type"])
        ago = (started - _ts(ch["at"])).total_seconds() / 60
        if code and code not in seen and 0 <= ago <= CHANGE_WINDOW_MIN:
            seen.add(code)
            out.append(Signal(code, "high" if ch["type"] == "deploy" else "medium",
                              f"{ch['type'].title()} {ch['ref']} on {ch['service']} {ago:.0f} min before the alert",
                              {"ref": ch["ref"], "minutes_before": round(ago), "team": ch.get("team")}))

    bad = [d for d in incident.get("dependencies", []) if d["status"] != "operational"]
    if bad:
        out.append(Signal(DEPENDENCY_DEGRADED, "high" if any(d["status"] == "outage" for d in bad) else "medium",
                          "Dependency status: " + ", ".join(f"{d['name']} {d['status']}" for d in bad),
                          {"dependencies": bad}))

    if m.get("saturation", 0) >= SATURATION_LIMIT:
        out.append(Signal(SATURATION, "high" if m["saturation"] >= 0.95 else "medium",
                          f"{m.get('saturation_resource', 'resource')} at {m['saturation']:.0%} "
                          f"(limit {SATURATION_LIMIT:.0%})", {"saturation": m["saturation"]}))

    if m.get("error_rate", 0) > slo["error_rate"]:
        out.append(Signal(ERROR_RATE, "high" if m["error_rate"] >= 10 * slo["error_rate"] else "medium",
                          f"Error rate {m['error_rate']:.1%} vs SLO threshold {slo['error_rate']:.1%}",
                          {"error_rate": m["error_rate"], "threshold": slo["error_rate"]}))

    if m.get("p99_ms", 0) > slo["p99_ms"]:
        out.append(Signal(LATENCY_SLO, "medium",
                          f"p99 latency {m['p99_ms']} ms vs SLO {slo['p99_ms']} ms",
                          {"p99_ms": m["p99_ms"], "slo_ms": slo["p99_ms"]}))

    burn = m.get("burn_rate", 0)
    if burn >= TICKET_BURN_RATE:
        page = burn >= PAGE_BURN_RATE
        out.append(Signal(ERROR_BUDGET_BURN, "high" if page else "low",
                          f"Error budget burning at {burn:.1f}x ({'page' if page else 'ticket'} threshold "
                          f"{PAGE_BURN_RATE if page else TICKET_BURN_RATE}x)", {"burn_rate": burn}))

    jobs = [j for j in service.get("jobs", []) if _in_job_window(started, j)]
    if jobs:
        out.append(Signal(SCHEDULED_JOB, "low",
                          "Overlaps scheduled job: " + ", ".join(f"{j['name']} ({j['start']}, {j['minutes']} min)"
                                                                  for j in jobs),
                          {"jobs": [j["name"] for j in jobs]}))

    if incident.get("customer_reported"):
        out.append(Signal(CUSTOMER_REPORTED, "medium", "Reported by paying customers/merchants via support",
                          {"tickets": incident.get("support_tickets", 0)}))

    if m.get("affected_users_pct", 0) == 0 and not incident.get("customer_reported"):
        out.append(Signal(NO_USER_IMPACT, "low", "No user-facing impact measured", {}))
    return out


def classify_severity(
    incident: dict[str, Any], service: dict[str, Any], signals: list[Signal]
) -> tuple[str, list[str]]:
    """Apply the SEV matrix and auto-upgrade triggers. Returns (severity, reasons)."""
    codes = {s.code for s in signals}
    m = incident["metrics"]
    users = m.get("affected_users_pct", 0)
    err = m.get("error_rate", 0)
    reasons: list[str] = []
    if DATA_INTEGRITY in codes:
        return "SEV1", ["Any data integrity concern is an immediate SEV1"]
    if users >= 90 or err >= 0.9:
        return "SEV1", [f"Full outage: {max(users, err * 100):.0f}% of users/requests failing"]
    if users >= 25:
        sev = "SEV2"
        reasons.append(f"Degraded service for {users:.0f}% of users (>25%)")
    elif service["tier"] == 1 and err >= 0.25:
        sev = "SEV2"
        reasons.append(f"Key tier-1 feature down: {err:.0%} of requests failing")
    elif users > 0:
        sev = "SEV3"
        reasons.append(f"{users:.1f}% of users affected, service still usable")
    else:
        sev = "SEV4"
        reasons.append("No measurable user impact")
    if CUSTOMER_REPORTED in codes and service["tier"] == 1 and sev_rank(sev) > 2:
        sev = "SEV2"
        reasons.append("Customer-reported incident on paying accounts: minimum SEV2")
    return sev, reasons


def assign_roles(incident: dict[str, Any], service: dict[str, Any], severity: str) -> dict[str, str]:
    """Explicit roles before anyone starts debugging. SEV1/2 get a dedicated IC and comms lead."""
    day = _ts(incident["started_at"]).timetuple().tm_yday
    oncall = service["oncall"]
    primary, secondary = oncall[day % len(oncall)], oncall[(day + 1) % len(oncall)]
    if sev_rank(severity) <= 2:
        return {"incident_commander": IC_ROTATION[day % len(IC_ROTATION)], "tech_lead": primary,
                "communications_lead": COMMS_ROTATION[day % len(COMMS_ROTATION)], "scribe": secondary}
    return {"incident_commander": primary, "tech_lead": primary,
            "communications_lead": primary if severity == "SEV4" else secondary, "scribe": "WarRoom agent"}


def triage(
    incident: dict[str, Any], service: dict[str, Any], prior: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Signals + severity + roles in one serialisable dict."""
    signals = check_incident(incident, service, prior)
    sev, reasons = classify_severity(incident, service, signals)
    return {"signals": [s.to_dict() for s in signals], "severity": sev, "severity_reasons": reasons,
            "roles": assign_roles(incident, service, sev), "sev_policy": SEV_MATRIX[sev]}


def next_update_due(started_at: str, severity: str) -> str:
    """When the next stakeholder update is due per the severity cadence."""
    due = _ts(started_at) + timedelta(minutes=SEV_MATRIX[severity]["cadence_min"])
    return due.isoformat(timespec="minutes")
