"""LLM incident commander: triaged alert + recalled memories -> remediation recommendation.

The LLM proposes; deterministic guardrails dispose:
  * any data integrity concern is SEV1 and always escalated to a human IC,
  * a duplicate alert is always merged into its parent (never a second war room),
  * "vendor" needs a dependency that is actually degraded (don't blame a green vendor),
  * "rollback" needs a deploy/flag/config change correlated with the alert,
  * "suppress" needs no user impact and a precedent from THIS service,
  * every other automatic action needs a cited precedent where that action was used,
  * severity can be upgraded by the model, never downgraded below the SEV matrix,
  * any failure (API, JSON, validation) falls back to ``escalate`` (page a human).
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import llm
from .memory import MemoryItem
from .triage import (CONFIG_CHANGE, DATA_INTEGRITY, DEPENDENCY_DEGRADED, DEPLOY_CORRELATED, DUPLICATE_ALERT,
                     FLAG_CHANGE, NO_USER_IMPACT, SEVERITIES, sev_rank, worst)

log = logging.getLogger(__name__)

ACTIONS = ("rollback", "restart", "scale", "failover", "feature_flag", "vendor", "monitor", "suppress",
           "merge", "escalate")
HUMAN_REVIEW_CONFIDENCE = 0.7

SYSTEM_PROMPT = """You are WarRoom, the incident commander agent for PaySetu, an Indian UPI/payments platform.
A production alert has been triaged deterministically: severity (SEV matrix), signals (correlated deploys/flags/
config changes, degraded vendors, saturation, error-budget burn, scheduled jobs, data integrity, duplicates).
You also get memories of how on-call engineers resolved incidents before:
  SERVICE memories [S1..]: past incidents on THIS service (its failure modes, fixes, root causes, open action items).
  PATTERN memories [P1..]: incidents with the same signals on OTHER services (standard SRE practice).

Recommend ONE remediation (fix the bleeding first, root cause later):
- "rollback": roll back the deploy/flag/config change that correlates with the alert.
- "restart": rolling restart / recycle the component (leaks, pool exhaustion, heap pressure).
- "scale": add capacity (replicas, shards, consumers) for a known load pattern.
- "failover": switch to the secondary provider / replica / region.
- "feature_flag": flip a kill switch or fallback flag.
- "vendor": a third party is degraded: vendor ticket + status page, mitigate, wait with monitoring.
- "monitor": known self-healing transient; watch 15-30 min, no action.
- "suppress": known noisy alert with NO user impact; ack and file an alert-tuning ticket.
- "merge": duplicate of an already-open incident.
- "escalate": page the human incident commander and open a war room. Use when there is no relevant precedent,
  precedents conflict, you are unsure, the impact is severe and novel, or when a KNOWN pattern looks different
  this time: magnitude far beyond what memories show (e.g. 42% failures when history shows 6-9%), or the usual
  trigger is absent (vendor status green, scheduled job not running). A familiar alert with a missing trigger is
  a NEW failure mode: escalate, do not repeat the old fix. With no memories at all you should escalate.

Only recommend a non-escalate action if a cited memory shows that SAME action fixed a matching incident.
Confidence (0 to 1) reflects precedent strength: one matching memory ~0.7, two or more consistent 0.85-0.95.
Also say if this is a REPEAT: SERVICE memories show the same root cause before (especially with an action item
still open). Be blameless: talk about systems and missing guardrails, never people.
Reply with ONLY a JSON object:
{"action": "<one of the actions>", "severity": "SEV1|SEV2|SEV3|SEV4", "confidence": 0.0,
 "hypothesis": "<most likely cause, 1 sentence>", "reason": "<=2 sentences, cite numbers",
 "runbook": ["<=5 short imperative steps"], "repeat": false, "repeat_note": "<occurrence count + open action item, or empty>",
 "cited_memories": ["S1", "P2", ...]}"""


@dataclass
class Suggestion:
    """The agent's recommendation for one incident."""

    action: str
    severity: str
    confidence: float
    reason: str
    hypothesis: str = ""
    runbook: list[str] = field(default_factory=list)
    repeat: bool = False
    repeat_note: str = ""
    cited_memories: list[dict[str, Any]] = field(default_factory=list)
    recalled_count: int = 0
    memory_enabled: bool = True
    model: str | None = None
    guardrail: str | None = None
    latency_ms: int = 0

    @property
    def needs_human(self) -> bool:
        """True when a person must take command: escalated, low confidence, or SEV1."""
        return self.action == "escalate" or self.confidence < HUMAN_REVIEW_CONFIDENCE or self.severity == "SEV1"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["needs_human"] = self.needs_human
        return d


def label_memories(service_mem: list[MemoryItem], pattern_mem: list[MemoryItem]) -> dict[str, MemoryItem]:
    """S1.. for this service's memories, P1.. for pattern memories."""
    labels = {f"S{i}": m for i, m in enumerate(service_mem, 1)}
    labels.update({f"P{i}": m for i, m in enumerate(pattern_mem, 1)})
    return labels


def build_user_message(
    incident: dict[str, Any], service: dict[str, Any], triage: dict[str, Any],
    service_mem: list[MemoryItem], pattern_mem: list[MemoryItem],
) -> str:
    """Compact, model-friendly description of the incident."""
    m = incident["metrics"]
    metrics = ", ".join(f"{k}={v}" for k, v in m.items())
    deps = ", ".join(f"{d['name']}: {d['status']}" for d in incident.get("dependencies", [])) or "none"
    jobs = ", ".join(f"{j['name']} at {j['start']}" for j in service.get("jobs", [])) or "none"
    lines = [
        f"Service: {service['name']} ({service['id']}), tier {service['tier']}, team {service['team']}. "
        f"SLO p99 {service['slo']['p99_ms']} ms, error rate {service['slo']['error_rate']:.1%}.",
        f"Scheduled jobs on this service: {jobs}",
        f"Incident {incident['id']} started {incident['started_at']} IST: alert {incident['alert']['name']} "
        f"({incident['alert']['source']}): {incident['alert']['summary']}",
        f"Metrics: {metrics}",
        f"Vendor/dependency status now: {deps}",
        f"Rule severity: {triage['severity']} ({'; '.join(triage['severity_reasons'])})",
        "Logs: " + " | ".join(incident.get("logs", [])),
        "",
        "Signals:",
    ]
    lines += [f"- {s['code']} ({s['severity']}): {s['message']}" for s in triage["signals"]] or ["- none"]
    lines += ["", "SERVICE memories (this service):"]
    lines += [f"[S{i}] {mm.text}" for i, mm in enumerate(service_mem, 1)] or ["(none)"]
    lines += ["", "PATTERN memories (other services, same signals):"]
    lines += [f"[P{i}] {mm.text}" for i, mm in enumerate(pattern_mem, 1)] or ["(none)"]
    return "\n".join(lines)


def make_validator(labels: dict[str, MemoryItem]):
    """Validate/normalise the model's JSON; map S/P labels back to memory objects."""
    by_id = {m.id: m for m in labels.values()}

    def validate(obj: dict[str, Any]) -> dict[str, Any]:
        action = str(obj.get("action", "")).strip().lower().replace("-", "_").replace(" ", "_")
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}, got {action!r}")
        severity = str(obj.get("severity", "")).strip().upper().replace(" ", "")
        if severity not in SEVERITIES:
            severity = "SEV4"  # no opinion: the rule severity wins in the guardrails
        try:
            confidence = float(obj.get("confidence", 0))
        except (TypeError, ValueError):
            raise ValueError("confidence must be a number")
        if confidence > 1:  # model answered in percent
            confidence /= 100
        reason = str(obj.get("reason", "")).strip()
        if not reason:
            raise ValueError("reason is required")
        runbook = obj.get("runbook") or []
        if isinstance(runbook, str):
            runbook = [runbook]
        cited = obj.get("cited_memories") or []
        if isinstance(cited, str):
            cited = [cited]
        resolved: list[tuple[str, MemoryItem]] = []
        for c in cited:
            key = str(c).strip().strip("[]").upper()
            m = labels.get(key) or by_id.get(str(c))
            if m and all(m is not r for _, r in resolved):
                label = key if key in labels else next(k for k, v in labels.items() if v is m)
                resolved.append((label, m))
        return {"action": action, "severity": severity, "confidence": max(0.0, min(1.0, confidence)),
                "reason": reason, "hypothesis": str(obj.get("hypothesis", "")).strip(),
                "runbook": [str(s).strip() for s in runbook if str(s).strip()][:6],
                "repeat": bool(obj.get("repeat")), "repeat_note": str(obj.get("repeat_note") or "").strip(),
                "cited": resolved}

    return validate


ACTION_WORDS = {
    "rollback": ("rolled back", "rollback", "roll back"), "restart": ("restart",),
    "scale": ("scaled", "scale up", "scaling"), "failover": ("failed over", "failover", "fail over"),
    "feature_flag": ("feature flag", "kill switch", "flag "), "vendor": ("vendor",),
    "monitor": ("monitored", "watched", "self-heal"), "suppress": ("noisy alert", "known noise", "acked"),
    "merge": ("merged", "duplicate"), "escalate": ("escalated",),
}


def _used_action(m: MemoryItem, action: str) -> bool:
    """Did this memory's incident get resolved with ``action``? (tags first, text as a fallback)."""
    tag = m.tag("action")
    if tag:
        return tag == action
    text = m.text.lower()
    return any(w in text for w in ACTION_WORDS[action])


def _escalate(result: dict[str, Any], why: str, note: str) -> tuple[dict[str, Any], str]:
    return {**result, "action": "escalate", "confidence": min(result["confidence"], 0.5),
            "reason": why + " " + result["reason"]}, note


def apply_guardrails(
    result: dict[str, Any], triage: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Deterministic safety net over the LLM output. Returns (result, guardrail_note)."""
    codes = {s["code"] for s in triage["signals"]}
    result = {**result, "severity": worst(triage["severity"], result["severity"])}
    action = result["action"]

    if DATA_INTEGRITY in codes:
        if action == "escalate" and result["severity"] == "SEV1":
            return result, None
        return {**result, "action": "escalate", "severity": "SEV1", "confidence": max(result["confidence"], 0.9),
                "reason": "Data integrity concern: immediate SEV1, human IC and finance paged. " + result["reason"]}, \
            "data-integrity-sev1"
    if DUPLICATE_ALERT in codes:
        if action == "merge":
            return result, None
        parent = next(s["details"].get("parent") for s in triage["signals"] if s["code"] == DUPLICATE_ALERT)
        return {**result, "action": "merge", "confidence": max(result["confidence"], 0.9),
                "reason": f"Duplicate of open incident {parent}: merged, no second war room. " + result["reason"]}, \
            "duplicate-merged"
    if action == "merge":
        return _escalate(result, "Nothing to merge into (no duplicate signal).", "no-parent-incident")
    if action == "vendor" and DEPENDENCY_DEGRADED not in codes:
        return _escalate(result, "Every vendor/dependency reports operational, so this is not a vendor issue.",
                         "vendor-is-green")
    if action == "rollback" and not codes & {DEPLOY_CORRELATED, FLAG_CHANGE, CONFIG_CHANGE}:
        return _escalate(result, "No deploy, flag or config change correlates with the alert, so there is nothing "
                                 "to roll back.", "nothing-to-roll-back")
    if action == "suppress" and (NO_USER_IMPACT not in codes or sev_rank(result["severity"]) < 4):
        return _escalate(result, "Users are affected, so this alert cannot be suppressed as noise.",
                         "impact-not-noise")
    if action != "escalate":
        cited = result["cited"]
        pool = [m for label, m in cited if label.startswith("S")] if action == "suppress" \
            else [m for _, m in cited]
        if not any(_used_action(m, action) for m in pool):
            where = "from this service " if action == "suppress" else ""
            return _escalate(result, f"No cited precedent {where}where '{action}' resolved a matching incident, "
                                     "so a human IC must decide.", "precedent-required")
    return result, None


def fallback(reason: str, severity: str, recalled: int, memory_enabled: bool) -> Suggestion:
    """Safe default when the LLM cannot be used: page a human."""
    return Suggestion(action="escalate", severity=severity, confidence=0.0, reason=reason, recalled_count=recalled,
                      memory_enabled=memory_enabled, guardrail="safe-fallback")


async def suggest(
    incident: dict[str, Any], service: dict[str, Any], triage: dict[str, Any],
    service_mem: list[MemoryItem], pattern_mem: list[MemoryItem], memory_enabled: bool = True,
) -> Suggestion:
    """Recommend a remediation (or escalation) for a triaged incident."""
    started = time.perf_counter()
    labels = label_memories(service_mem, pattern_mem)
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_message(incident, service, triage,
                                                                   service_mem, pattern_mem)}]
        raw, model = await llm.complete_json(messages, make_validator(labels))
    except Exception as e:
        log.warning("agent fell back to escalate: %s", e)
        s = fallback(f"Agent unavailable ({type(e).__name__}); paging the human incident commander.",
                     triage["severity"], len(labels), memory_enabled)
        s.latency_ms = int((time.perf_counter() - started) * 1000)
        return s

    result, guardrail = apply_guardrails(raw, triage)
    return Suggestion(
        action=result["action"], severity=result["severity"], confidence=round(result["confidence"], 2),
        reason=result["reason"], hypothesis=result["hypothesis"], runbook=result["runbook"],
        repeat=result["repeat"] and bool(service_mem), repeat_note=result["repeat_note"],
        cited_memories=[{**m.to_dict(), "label": label} for label, m in result["cited"]],
        recalled_count=len(labels), memory_enabled=memory_enabled, model=model, guardrail=guardrail,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
