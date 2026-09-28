"""Memory layer: a thin wrapper over Hindsight (with a local stand-in for offline dev).

One bank (``warroom-sre``). Every incident resolution is retained as a natural-language memory
tagged ``service:<id>``, ``signal:<code>``, ``action:<action>``, ``cause:<slug>`` and ``incident:<id>``.

Before each recommendation we recall at two levels:
  * service memory: how *this* service's past incidents were resolved (its failure modes, root
    causes and the post-mortem action items that are still open),
  * pattern memory: how incidents with the *same signals* were resolved on other services.
Service reliability profiles and org-wide trend reviews come from reflect.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .config import get_settings

log = logging.getLogger(__name__)

BANK_MISSION = (
    "You are the institutional memory of the SRE / incident response team at PaySetu, an Indian UPI and "
    "payments platform. You remember every production incident: the service, alert, severity, the signals "
    "(deploys, flag or config changes, degraded vendors such as NPCI or SMS providers, saturation, scheduled "
    "jobs), the remediation that worked (rollback, restart, scale, failover, feature flag, vendor escalation, "
    "monitor, suppress noisy alert, escalate to a human incident commander), time to resolve, root causes, and "
    "post-mortem action items and whether they were completed. You are blameless: you describe what the "
    "system lacked, never who made a mistake."
)

PROFILE_QUERY = (
    "Build a reliability profile of service {name} ({service_id}). Cover: (1) its recurring failure modes with "
    "typical magnitudes and times of day, (2) which remediation worked for each and how long it took, "
    "(3) repeat root causes and post-mortem action items that are still open, (4) alerts that are noisy or "
    "known false positives, (5) concrete runbook guidance for the next on-call engineer. Be concise, bullet points."
)

ORG_QUERY = (
    "Across all services, run a quarterly-style incident review: which failure modes repeat, which post-mortem "
    "action items keep not getting done (repeat root causes), which alerts are noisy and should be tuned, which "
    "services need reliability investment, and which third-party dependencies hurt us most. Give an incident "
    "commander's briefing in bullet points."
)

ACTION_VERB = {
    "rollback": "ROLLED BACK the correlated change",
    "restart": "did a ROLLING RESTART",
    "scale": "SCALED UP capacity",
    "failover": "FAILED OVER to the secondary",
    "feature_flag": "flipped a FEATURE FLAG / kill switch",
    "vendor": "treated it as a VENDOR issue (ticket + status page, waited it out)",
    "monitor": "MONITORED it (self-healing, no action)",
    "suppress": "ACKED it as a known NOISY ALERT (no user impact)",
    "merge": "MERGED it into the already-open parent incident (duplicate alert)",
    "escalate": "ESCALATED to a human incident commander / war room",
}

# Signals that say *why* something broke; generic symptoms (error rate, latency) match too broadly.
DISCRIMINATING = ("DATA_INTEGRITY", "DUPLICATE_ALERT", "DEPLOY_CORRELATED", "FLAG_CHANGE", "CONFIG_CHANGE",
                  "DEPENDENCY_DEGRADED", "SATURATION", "SCHEDULED_JOB")


@dataclass
class MemoryItem:
    """A single recalled memory."""

    id: str
    text: str
    tags: list[str] = field(default_factory=list)
    timestamp: str | None = None
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def tag(self, prefix: str) -> str | None:
        """Value of the first ``prefix:<value>`` tag, e.g. ``tag('service')``."""
        return next((t.split(":", 1)[1] for t in self.tags if t.startswith(prefix + ":")), None)


class MemoryBackend(Protocol):
    """What WarRoom needs from a memory store."""

    name: str

    async def setup(self) -> None: ...
    async def retain(self, content: str, tags: list[str], metadata: dict[str, str],
                     timestamp: datetime, document_id: str) -> None: ...
    async def recall(self, query: str, tags: list[str], limit: int) -> list[MemoryItem]: ...
    async def reflect(self, query: str, tags: list[str]) -> str: ...
    async def reset(self) -> None: ...


# ---------------------------------------------------------------------------
# Hindsight (Vectorize) backend
# ---------------------------------------------------------------------------
class HindsightBackend:
    """Hindsight Cloud / self-hosted via the official ``hindsight-client`` SDK."""

    name = "hindsight"

    def __init__(self, base_url: str, api_key: str | None, bank_id: str):
        from hindsight_client import Hindsight

        self.client = Hindsight(base_url=base_url, api_key=api_key, timeout=120)
        self.bank_id = bank_id

    async def setup(self) -> None:
        await self.client.acreate_bank(
            bank_id=self.bank_id, name="WarRoom SRE", mission=BANK_MISSION,
            retain_mission="Extract the service, alert, severity, each signal with its exact numbers, the "
                           "remediation used, time to resolve, the root cause and any open action items.",
        )

    @staticmethod
    async def _retry(fn, *args, **kwargs):
        """Retry transient network failures (DNS, resets, timeouts) with backoff."""
        for attempt in range(4):
            try:
                return await fn(*args, **kwargs)
            except (OSError, asyncio.TimeoutError) as e:  # aiohttp connection errors subclass OSError
                if attempt == 3:
                    raise
                log.warning("Hindsight transient error (%s), retry %d", type(e).__name__, attempt + 1)
                await asyncio.sleep(2 ** (attempt + 1))

    async def retain(self, content, tags, metadata, timestamp, document_id) -> None:
        await self._retry(
            self.client.aretain, bank_id=self.bank_id, content=content, timestamp=timestamp,
            context="incident resolution", document_id=document_id, metadata=metadata, tags=tags,
        )

    async def recall(self, query, tags, limit) -> list[MemoryItem]:
        resp = await self._retry(
            self.client.arecall, bank_id=self.bank_id, query=query, tags=tags or None,
            tags_match="any_strict" if tags else "any", max_tokens=2048, budget="mid",
        )
        items = [MemoryItem(id=r.id, text=r.text, tags=r.tags or [],
                            timestamp=r.occurred_start or r.mentioned_at) for r in resp.results]
        return items[:limit]

    async def reflect(self, query, tags) -> str:
        resp = await self._retry(
            self.client.areflect, bank_id=self.bank_id, query=query, tags=tags or None,
            tags_match="any_strict" if tags else "any", budget="mid",
        )
        return resp.text

    async def reset(self) -> None:
        try:
            await self.client.banks.delete_bank(self.bank_id)
        except Exception as e:  # bank may not exist yet
            log.info("delete_bank: %s", e)
        await self.setup()


# ---------------------------------------------------------------------------
# Local stand-in (JSON file + lexical scoring). Same interface, no network.
# ---------------------------------------------------------------------------
_WORD = re.compile(r"[a-z0-9_%.\-]+")
_STOP = {"the", "a", "an", "of", "on", "and", "to", "for", "is", "in", "was", "with", "by", "as",
         "at", "it", "this", "that", "from", "incident", "alert", "service"}


def _tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if t not in _STOP]


class LocalBackend:
    """Offline memory used when HINDSIGHT_BASE_URL is not configured.

    Retain appends to a JSON file; recall is tag-filtered TF-IDF-style overlap with a
    recency tiebreak; reflect summarises the tagged memories with the Groq LLM.
    """

    name = "local"

    def __init__(self, path):
        self.path = path
        self.items: list[dict[str, Any]] = []

    async def setup(self) -> None:
        if self.path.exists():
            self.items = json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.items, indent=1, ensure_ascii=False), encoding="utf-8")

    async def retain(self, content, tags, metadata, timestamp, document_id) -> None:
        self.items = [m for m in self.items if m["document_id"] != document_id]
        self.items.append({"id": uuid.uuid4().hex[:12], "text": content, "tags": tags,
                           "metadata": metadata, "timestamp": timestamp.isoformat(),
                           "document_id": document_id})
        self._save()

    def _tagged(self, tags: list[str]) -> list[dict[str, Any]]:
        return [m for m in self.items if set(m["tags"]) & set(tags)] if tags else list(self.items)

    async def recall(self, query, tags, limit) -> list[MemoryItem]:
        pool = self._tagged(tags)
        if not pool:
            return []
        q = set(_tokens(query))
        df: dict[str, int] = {}
        for m in pool:
            for t in set(_tokens(m["text"]) + [x.lower() for x in m["tags"]]):
                df[t] = df.get(t, 0) + 1
        scored = []
        for m in pool:
            toks = set(_tokens(m["text"])) | {t.lower() for t in m["tags"]}
            score = sum(math.log(1 + len(pool) / df.get(t, 1)) for t in q & toks)
            scored.append((score, m["timestamp"], m))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [MemoryItem(id=m["id"], text=m["text"], tags=m["tags"], timestamp=m["timestamp"],
                           score=round(s, 3)) for s, _, m in scored[:limit]]

    async def reflect(self, query, tags) -> str:
        from . import llm

        pool = sorted(self._tagged(tags), key=lambda m: m["timestamp"])
        if not pool:
            return "No memories yet."
        facts = "\n".join(f"- {m['text']}" for m in pool[-60:])
        try:
            return await llm.complete_text([
                {"role": "system", "content": BANK_MISSION + " Answer only from the memories given."},
                {"role": "user", "content": f"Memories:\n{facts}\n\n{query}"},
            ])
        except Exception as e:
            log.warning("local reflect LLM failed: %s", e)
            return "LLM unavailable; raw memories:\n" + facts

    async def reset(self) -> None:
        self.items = []
        self._save()


# ---------------------------------------------------------------------------
# Public API used by the rest of the app
# ---------------------------------------------------------------------------
def _signal_line(signals: list[dict[str, Any]]) -> str:
    return "; ".join(f"{s['code']}: {s['message']}" for s in signals) or "none"


class MemoryService:
    """Owns the backend and the global MEMORY ON/OFF switch."""

    def __init__(self, backend: MemoryBackend):
        self.backend = backend
        self.enabled = True

    async def setup(self) -> None:
        await self.backend.setup()

    async def retain_resolution(
        self, incident: dict[str, Any], service: dict[str, Any], signals: list[dict[str, Any]],
        severity: str, action: str, note: str, cause: str | None = None, mttr_min: int | None = None,
    ) -> str:
        """Store how an incident was resolved. Always runs, even when recall is toggled off."""
        text = format_resolution_memory(incident, service, signals, severity, action, note, cause, mttr_min)
        tags = ([f"service:{service['id']}", f"action:{action}", f"severity:{severity}",
                 f"incident:{incident['id']}"]
                + ([f"cause:{cause}"] if cause else [])
                + sorted({f"signal:{s['code']}" for s in signals}))
        await self.backend.retain(
            content=text, tags=tags, timestamp=datetime.fromisoformat(incident["started_at"]),
            document_id=f"resolution-{incident['id']}",
            metadata={"incident_id": incident["id"], "service_id": service["id"], "action": action},
        )
        return text

    async def recall_service_history(
        self, service: dict[str, Any], incident: dict[str, Any], signals: list[dict[str, Any]], limit: int = 8
    ) -> list[MemoryItem]:
        """Past incidents on this service relevant to this alert ([] when memory is off)."""
        if not self.enabled:
            return []
        query = (f"How were past {incident['alert']['name']} incidents on {service['name']} ({service['id']}) "
                 f"resolved, what was the root cause and time to resolve? Current signals: {_signal_line(signals)}")
        return await self.backend.recall(query, tags=[f"service:{service['id']}"], limit=limit)

    async def recall_pattern_precedent(
        self, service: dict[str, Any], signals: list[dict[str, Any]], limit: int = 3
    ) -> list[MemoryItem]:
        """How incidents with the same causal signals were resolved on *other* services."""
        codes = [s["code"] for s in signals if s["code"] in DISCRIMINATING]
        if not self.enabled or not codes:
            return []
        query = ("How are incidents with these signals normally resolved: "
                 + _signal_line([s for s in signals if s["code"] in codes]) + "?")
        items = await self.backend.recall(query, tags=sorted({f"signal:{c}" for c in codes}), limit=limit * 4)
        return [m for m in items if m.tag("service") != service["id"] and f"({service['id']})" not in m.text][:limit]

    async def build_service_profile(self, service: dict[str, Any]) -> str:
        """Synthesise a service reliability profile from all its memories via reflect."""
        return await self.backend.reflect(PROFILE_QUERY.format(name=service["name"], service_id=service["id"]),
                                          tags=[f"service:{service['id']}"])

    async def org_insights(self) -> str:
        """Reflect over the whole bank: repeat incidents, noisy alerts, reliability investment."""
        return await self.backend.reflect(ORG_QUERY, tags=[])

    async def list_service_memories(self, service: dict[str, Any], limit: int = 30) -> list[MemoryItem]:
        """Everything remembered about a service (ignores the toggle; used by the profile page)."""
        return await self.backend.recall(f"All incidents and resolutions for {service['name']} ({service['id']})",
                                         tags=[f"service:{service['id']}"], limit=limit)

    async def reset(self) -> None:
        """Wipe the bank."""
        await self.backend.reset()


def format_resolution_memory(
    incident: dict[str, Any], service: dict[str, Any], signals: list[dict[str, Any]], severity: str,
    action: str, note: str, cause: str | None = None, mttr_min: int | None = None,
) -> str:
    """Render a resolution as a self-contained natural-language memory (numbers kept exact)."""
    when = datetime.fromisoformat(incident["started_at"])
    m = incident["metrics"]
    impact = f"{m.get('affected_users_pct', 0):g}% of users affected"
    if m.get("failed_txn_per_min"):
        impact += f", {m['failed_txn_per_min']:,} failed transactions/min"
    took = f" in {mttr_min} min" if mttr_min else ""
    return (f"On {when:%Y-%m-%d} at {when:%H:%M} IST ({when:%A}), incident {incident['id']} hit "
            f"{service['name']} ({service['id']}, tier {service['tier']}, team {service['team']}). "
            f"Alert {incident['alert']['name']}: {incident['alert']['summary']}. Severity {severity}; {impact}. "
            f"Signals: {_signal_line(signals)}. The on-call engineer {ACTION_VERB[action]}{took}."
            + (f" Root cause: {cause}." if cause else "")
            + f" Engineer note: {note.strip() or 'none'}")


def create_memory_service(bank_id: str | None = None) -> MemoryService:
    """Hindsight when HINDSIGHT_BASE_URL is set, otherwise the local stand-in.

    ``bank_id`` defaults to the main (trained) bank; the story's fresh brain passes its own.
    """
    s = get_settings()
    bank = bank_id or s.bank_id
    if s.hindsight_base_url:
        backend: MemoryBackend = HindsightBackend(s.hindsight_base_url, s.hindsight_api_key, bank)
    else:
        log.warning("HINDSIGHT_BASE_URL not set: using local memory stand-in")
        name = "local_memory.json" if bank == s.bank_id else f"local_memory-{bank}.json"
        backend = LocalBackend(s.var_dir / name)
    return MemoryService(backend)
