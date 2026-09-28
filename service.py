"""Orchestration: triage -> recall -> agent -> on-call resolution -> retain. Plus post-mortems, replay, metrics."""
from __future__ import annotations

import asyncio
import logging
import statistics
from datetime import datetime
from typing import Any

from . import agent, comms
from .memory import MemoryService
from .store import Store

log = logging.getLogger(__name__)

WEEKS = [(1, 7, "Week 1 (1-7 Sep)"), (8, 14, "Week 2 (8-14 Sep)"),
         (15, 21, "Week 3 (15-21 Sep)"), (22, 30, "Week 4 (22-30 Sep)")]


def simulated_mttr(truth: dict[str, Any] | None, agreed: bool, needs_human: bool) -> int | None:
    """Time to resolve: fast when the agent's remembered fix was right and trusted, slow from scratch."""
    if not truth:
        return None
    if agreed and not needs_human:
        return truth["assisted_mttr"]
    if agreed:
        return round((truth["base_mttr"] + truth["assisted_mttr"]) / 2)
    return truth["base_mttr"]


class WarRoom:
    """The incident response workflow over a Store and a MemoryService."""

    def __init__(self, store: Store, memory: MemoryService):
        self.store = store
        self.memory = memory
        self.replay_status: dict[str, Any] = {"running": False, "done": 0, "total": 0,
                                              "current": None, "error": None, "through_day": None}
        self._replay_task: asyncio.Task | None = None

    # ---- board -----------------------------------------------------------------
    def board(self) -> list[dict[str, Any]]:
        """Summary row per incident for the incident board."""
        rows = []
        for inc in self.store.incidents:
            tri = self.store.triage_for(inc["id"])
            sug = self.store.state["suggestions"].get(inc["id"])
            res = self.store.state["resolutions"].get(inc["id"])
            svc = self.store.services[inc["service_id"]]
            rows.append({
                "id": inc["id"], "day": inc["day"], "started_at": inc["started_at"],
                "service_id": svc["id"], "service_name": svc["name"], "tier": svc["tier"],
                "alert": inc["alert"]["name"], "summary": inc["alert"]["summary"],
                "severity": (res or sug or {}).get("severity") or tri["severity"],
                "signals": [s["code"] for s in tri["signals"]],
                "suggestion": {k: sug[k] for k in ("action", "confidence", "needs_human", "repeat")} if sug else None,
                "resolution": {k: res[k] for k in ("action", "resolved_by", "mttr_min")} if res else None,
            })
        return rows

    # ---- suggest / resolve --------------------------------------------------------
    async def suggest(self, incident_id: str) -> dict[str, Any]:
        """Recall service + pattern memories and ask the agent for a remediation."""
        ctx = self.store.context(incident_id)
        inc, svc, tri = ctx["incident"], ctx["service"], ctx["triage"]
        svc_mem, pat_mem = await asyncio.gather(
            self.memory.recall_service_history(svc, inc, tri["signals"]),
            self.memory.recall_pattern_precedent(svc, tri["signals"]),
        )
        # never let an incident cite its own resolution (e.g. "Ask again" after resolving)
        own = lambda m: f"incident:{incident_id}" in m.tags or incident_id in m.text  # noqa: E731
        svc_mem = [m for m in svc_mem if not own(m)]
        pat_mem = [m for m in pat_mem if not own(m)]
        sug = await agent.suggest(inc, svc, tri, svc_mem, pat_mem, memory_enabled=self.memory.enabled)
        s = sug.to_dict()
        s["recalled"] = [{**m.to_dict(), "label": f"S{i}"} for i, m in enumerate(svc_mem, 1)]
        s["pattern"] = [{**m.to_dict(), "label": f"P{i}"} for i, m in enumerate(pat_mem, 1)]
        s["status_update"] = comms.status_update(inc, svc, tri, s)
        s["created_at"] = datetime.now().isoformat(timespec="seconds")
        self.store.record_suggestion(incident_id, s)
        return s

    async def resolve(self, incident_id: str, action: str, note: str, cause: str | None = None,
                      resolved_by: str = "on-call") -> dict[str, Any]:
        """Record how the on-call engineer resolved the incident and retain it in memory."""
        if action not in agent.ACTIONS:
            raise ValueError(f"action must be one of {agent.ACTIONS}")
        ctx = self.store.context(incident_id)
        inc, svc, tri, sug = ctx["incident"], ctx["service"], ctx["triage"], ctx["suggestion"]
        cause = _slug(cause) if cause else None
        severity = sug["severity"] if sug else tri["severity"]
        agreed = bool(sug and sug["action"] == action)
        needs_human = sug["needs_human"] if sug else True
        mttr = simulated_mttr(self.store.ground_truth.get(incident_id), agreed, needs_human)
        memory_text = await self.memory.retain_resolution(inc, svc, tri["signals"], severity, action, note,
                                                          cause, mttr)
        resolution = {
            "action": action, "note": note, "cause": cause, "resolved_by": resolved_by, "severity": severity,
            "decided_at": datetime.now().isoformat(timespec="seconds"),
            "suggested_action": sug["action"] if sug else None,
            "suggested_confidence": sug["confidence"] if sug else None,
            "needs_human": needs_human,
            "memory_enabled": sug["memory_enabled"] if sug else self.memory.enabled,
            "agreed": agreed,
            "repeat_flagged": bool(sug and sug.get("repeat")),
            "mttr_min": mttr,
            "retained_memory": memory_text,
        }
        resolution["status_update"] = comms.status_update(inc, svc, tri, sug, resolution)
        self.store.record_resolution(incident_id, resolution)
        return resolution

    async def postmortem(self, incident_id: str) -> str:
        """Draft (and cache) a blameless post-mortem for a resolved incident."""
        ctx = self.store.context(incident_id)
        res = ctx["resolution"]
        if not res:
            raise ValueError("resolve the incident before writing its post-mortem")
        inc, svc = ctx["incident"], ctx["service"]
        history = await self.memory.backend.recall(
            f"Past incidents on {svc['name']} ({svc['id']}) with root cause {res.get('cause') or ''} "
            f"or alert {inc['alert']['name']}; open action items", tags=[f"service:{svc['id']}"], limit=6)
        history = [m for m in history if incident_id not in m.text and f"incident:{incident_id}" not in m.tags]
        text = await comms.generate_postmortem(inc, svc, ctx["triage"], ctx["suggestion"], res, history)
        self.store.record_postmortem(incident_id, text)
        return text

    # ---- seed + replay --------------------------------------------------------------
    async def seed_history(self) -> int:
        """Replay August's resolved incidents into memory."""
        for h in self.store.history:
            await self.memory.retain_resolution(h, self.store.services[h["service_id"]], h["signals"], h["severity"],
                                                h["action"], h["note"], h["cause"], h["mttr_min"])
        return len(self.store.history)

    def start_replay(self, through_day: int) -> dict[str, Any]:
        """Kick off a background replay of unresolved incidents up to ``through_day``."""
        if self.replay_status["running"]:
            return self.replay_status
        todo = [i for i in self.store.incidents
                if i["day"] <= through_day and i["id"] not in self.store.state["resolutions"]]
        self.replay_status = {"running": True, "done": 0, "total": len(todo), "current": None,
                              "error": None, "through_day": through_day}
        self._replay_task = asyncio.create_task(self._replay(todo))
        return self.replay_status

    async def _replay(self, todo: list[dict]) -> None:
        try:
            for inc in todo:
                self.replay_status["current"] = inc["id"]
                await self.suggest(inc["id"])
                t = self.store.ground_truth[inc["id"]]
                await self.resolve(inc["id"], t["expected_action"], t["note"], t["cause"], resolved_by="replay-on-call")
                self.replay_status["done"] += 1
        except Exception as e:  # surface to UI instead of dying silently
            log.exception("replay failed")
            self.replay_status["error"] = f"{type(e).__name__}: {e}"
        finally:
            self.replay_status["running"] = False
            self.replay_status["current"] = None

    # ---- metrics ----------------------------------------------------------------------
    def metrics(self) -> dict[str, Any]:
        """Learning curve: pages to humans, agreement and MTTR per week."""
        res = self.store.state["resolutions"]
        gt = self.store.ground_truth
        weeks = []
        for lo, hi, label in WEEKS:
            rows = [(i, res[i["id"]]) for i in self.store.incidents if lo <= i["day"] <= hi and i["id"] in res]
            weeks.append({"label": label, **_summary(rows, gt)})
        all_rows = [(self.store.by_id[k], r) for k, r in res.items()]
        return {"weeks": weeks, "totals": {"incidents": len(self.store.incidents), **_summary(all_rows, gt)}}


def _summary(rows: list[tuple[dict, dict]], gt: dict[str, dict]) -> dict[str, Any]:
    n = len(rows)
    human = sum(1 for _, r in rows if r["needs_human"])
    mttrs = [r["mttr_min"] for _, r in rows if r["mttr_min"] is not None]
    base = [gt[i["id"]]["base_mttr"] for i, r in rows if i["id"] in gt and r["mttr_min"] is not None]
    return {
        "processed": n, "human_needed": human, "auto_handled": n - human,
        "human_rate": round(human / n, 3) if n else None,
        "agreement": round(sum(r["agreed"] for _, r in rows) / n, 3) if n else None,
        "mttr_avg": round(sum(mttrs) / len(mttrs), 1) if mttrs else None,
        # median = the typical incident; the mean is dragged up by rare multi-hour SEV1s
        "mttr_median": round(statistics.median(mttrs), 1) if mttrs else None,
        "mttr_baseline": round(sum(base) / len(base), 1) if base else None,
        "minutes_saved": sum(base) - sum(mttrs),
        "repeats_flagged": sum(1 for _, r in rows if r.get("repeat_flagged")),
        "sev1": sum(1 for _, r in rows if r["severity"] == "SEV1"),
    }


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in text.strip().lower())
    return "-".join(p for p in out.split("-") if p)[:60]
