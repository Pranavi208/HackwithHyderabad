"""Tests for the local memory stand-in and the two-level recall (no network)."""
from __future__ import annotations

import asyncio

from app.memory import LocalBackend, MemoryService, format_resolution_memory
from conftest import CHECKOUT, ORDERS_DB, make_incident

DEPLOY_SIG = {"code": "DEPLOY_CORRELATED", "severity": "high", "message": "Deploy v2 12 min before", "details": {}}
ERR_SIG = {"code": "ERROR_RATE", "severity": "high", "message": "Error rate 6%", "details": {}}


def service(tmp_path) -> MemoryService:
    svc = MemoryService(LocalBackend(tmp_path / "mem.json"))
    asyncio.run(svc.setup())
    return svc


def retain(svc, inc_id, svc_def, action="rollback", signals=(DEPLOY_SIG, ERR_SIG), cause="bad-deploy"):
    inc = make_incident(id=inc_id, service_id=svc_def["id"], metrics={"affected_users_pct": 12})
    return asyncio.run(svc.retain_resolution(inc, svc_def, list(signals), "SEV3", action, "rolled back", cause, 9))


def test_format_memory_keeps_facts():
    inc = make_incident(metrics={"affected_users_pct": 12, "failed_txn_per_min": 1200})
    text = format_resolution_memory(inc, CHECKOUT, [DEPLOY_SIG], "SEV3", "rollback", "undo v2", "bad-deploy", 9)
    assert "ROLLED BACK" in text and "in 9 min" in text and "(checkout-api" in text
    assert "1,200 failed transactions/min" in text and "Root cause: bad-deploy" in text and "undo v2" in text
    assert "Wednesday" in text  # 2 Sep 2026


def test_tags_carry_service_action_cause_and_signals(tmp_path):
    svc = service(tmp_path)
    retain(svc, "INC-1", CHECKOUT)
    tags = set(svc.backend.items[0]["tags"])  # type: ignore[attr-defined]
    assert {"service:checkout-api", "action:rollback", "cause:bad-deploy", "incident:INC-1",
            "signal:DEPLOY_CORRELATED", "signal:ERROR_RATE", "severity:SEV3"} <= tags


def test_service_recall_is_scoped_and_respects_toggle(tmp_path):
    svc = service(tmp_path)
    retain(svc, "INC-1", CHECKOUT)
    retain(svc, "INC-2", ORDERS_DB)
    inc = make_incident()
    got = asyncio.run(svc.recall_service_history(CHECKOUT, inc, [DEPLOY_SIG]))
    assert [m.tag("service") for m in got] == ["checkout-api"]
    svc.enabled = False
    assert asyncio.run(svc.recall_service_history(CHECKOUT, inc, [DEPLOY_SIG])) == []
    assert asyncio.run(svc.recall_pattern_precedent(CHECKOUT, [DEPLOY_SIG])) == []


def test_pattern_recall_excludes_own_service_and_needs_causal_signal(tmp_path):
    svc = service(tmp_path)
    retain(svc, "INC-1", CHECKOUT)
    retain(svc, "INC-2", ORDERS_DB)
    got = asyncio.run(svc.recall_pattern_precedent(CHECKOUT, [DEPLOY_SIG]))
    assert [m.tag("service") for m in got] == ["orders-db"]
    # generic symptoms alone don't pull cross-service precedents
    assert asyncio.run(svc.recall_pattern_precedent(CHECKOUT, [ERR_SIG])) == []


def test_retain_is_idempotent_per_incident_and_persists(tmp_path):
    svc = service(tmp_path)
    for _ in range(2):
        retain(svc, "INC-1", CHECKOUT)
    again = service(tmp_path)
    assert len(again.backend.items) == 1  # type: ignore[attr-defined]
