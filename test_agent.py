"""Tests for the agent's validation, guardrails and safe fallback (no network)."""
from __future__ import annotations

import asyncio

import pytest

from app import agent, llm
from app.memory import MemoryItem
from app.triage import triage
from conftest import CHECKOUT, make_incident


def mem(mid: str, action: str, service: str = "checkout-api", text: str = "past incident") -> MemoryItem:
    return MemoryItem(id=mid, text=text, tags=[f"service:{service}", f"action:{action}"])


def result(action: str, cited=(), severity="SEV4", confidence=0.9) -> dict:
    return {"action": action, "severity": severity, "confidence": confidence, "reason": "r", "hypothesis": "h",
            "runbook": [], "repeat": False, "repeat_note": "", "cited": list(cited)}


def tri(**over) -> dict:
    return triage(make_incident(**over), CHECKOUT)


DEPLOY = [{"type": "deploy", "service": "checkout-api", "ref": "v2", "at": "2026-09-02T11:05"}]


# ---- validator ----------------------------------------------------------------------
def test_validator_normalises_and_maps_labels():
    s1 = mem("m1", "rollback")
    v = agent.make_validator({"S1": s1})
    out = v({"action": "Roll-back".replace("-", ""), "severity": "sev2", "confidence": 85, "reason": "x",
             "cited_memories": ["[S1]", "S1", "S9"], "runbook": "undo it"})
    assert out["action"] == "rollback" and out["severity"] == "SEV2" and out["confidence"] == 0.85
    assert out["cited"] == [("S1", s1)] and out["runbook"] == ["undo it"]


@pytest.mark.parametrize("bad", [{"action": "reboot", "reason": "x"}, {"action": "scale", "reason": ""},
                                 {"action": "scale", "reason": "x", "confidence": "high"}])
def test_validator_rejects_bad_output(bad):
    with pytest.raises(ValueError):
        agent.make_validator({})(bad)


# ---- guardrails ------------------------------------------------------------------------
def test_rollback_with_correlated_deploy_and_precedent_passes():
    t = tri(recent_changes=DEPLOY, metrics={"error_rate": 0.06, "affected_users_pct": 20})
    out, g = agent.apply_guardrails(result("rollback", [("P1", mem("m", "rollback", "payments-gateway"))]), t)
    assert g is None and out["action"] == "rollback" and out["severity"] == "SEV3"


def test_rollback_without_change_escalates():
    t = tri(metrics={"error_rate": 0.06, "affected_users_pct": 20})
    out, g = agent.apply_guardrails(result("rollback", [("S1", mem("m", "rollback"))]), t)
    assert (out["action"], g) == ("escalate", "nothing-to-roll-back") and out["confidence"] <= 0.5


def test_vendor_blamed_while_vendor_green_escalates():
    t = tri(dependencies=[{"name": "NPCI UPI", "status": "operational"}], metrics={"error_rate": 0.42})
    out, g = agent.apply_guardrails(result("vendor", [("S1", mem("m", "vendor"))]), t)
    assert (out["action"], g) == ("escalate", "vendor-is-green")


def test_vendor_with_degraded_dependency_and_precedent_passes():
    t = tri(dependencies=[{"name": "NPCI UPI", "status": "degraded"}], metrics={"error_rate": 0.07})
    out, g = agent.apply_guardrails(result("vendor", [("S1", mem("m", "vendor"))]), t)
    assert g is None and out["action"] == "vendor"


def test_data_integrity_forces_sev1_escalation():
    t = tri(data_integrity={"mismatch_count": 312, "amount_inr": 4870500})
    out, g = agent.apply_guardrails(result("monitor", [("S1", mem("m", "monitor"))]), t)
    assert (out["action"], out["severity"], g) == ("escalate", "SEV1", "data-integrity-sev1")


def test_duplicate_is_always_merged():
    parent = make_incident(id="INC-P", started_at="2026-09-02T11:00")
    t = triage(make_incident(started_at="2026-09-02T11:12"), CHECKOUT, [parent])
    out, g = agent.apply_guardrails(result("rollback", [("S1", mem("m", "rollback"))]), t)
    assert (out["action"], g) == ("merge", "duplicate-merged") and "INC-P" in out["reason"]
    out2, _ = agent.apply_guardrails(result("merge"), tri())
    assert out2["action"] == "escalate"


def test_suppress_needs_no_impact_and_same_service_precedent():
    quiet = tri()
    other_service = [("P1", mem("m", "suppress", "orders-db"))]
    out, g = agent.apply_guardrails(result("suppress", other_service), quiet)
    assert (out["action"], g) == ("escalate", "precedent-required")
    out, g = agent.apply_guardrails(result("suppress", [("S1", mem("m", "suppress"))]), quiet)
    assert g is None and out["action"] == "suppress"
    loud = tri(metrics={"affected_users_pct": 4})
    out, g = agent.apply_guardrails(result("suppress", [("S1", mem("m", "suppress"))], severity="SEV3"), loud)
    assert (out["action"], g) == ("escalate", "impact-not-noise")


def test_action_needs_precedent_with_the_same_action():
    t = tri(metrics={"saturation": 0.95, "affected_users_pct": 3})
    out, g = agent.apply_guardrails(result("scale", [("S1", mem("m", "restart"))]), t)
    assert (out["action"], g) == ("escalate", "precedent-required")
    out, g = agent.apply_guardrails(result("scale", []), t)
    assert out["action"] == "escalate"
    out, g = agent.apply_guardrails(result("scale", [("S1", mem("m", "scale"))]), t)
    assert g is None


def test_text_fallback_when_memory_has_no_action_tag():
    t = tri(metrics={"saturation": 0.95, "affected_users_pct": 3})
    m = MemoryItem(id="x", text="The on-call engineer SCALED UP capacity in 6 min.", tags=[])
    out, g = agent.apply_guardrails(result("scale", [("P1", m)]), t)
    assert g is None


def test_severity_never_downgraded_but_can_be_upgraded():
    t = tri(metrics={"affected_users_pct": 30})  # SEV2 by the matrix
    out, _ = agent.apply_guardrails(result("escalate", severity="SEV4"), t)
    assert out["severity"] == "SEV2"
    out, _ = agent.apply_guardrails(result("escalate", severity="SEV1"), t)
    assert out["severity"] == "SEV1"


def test_needs_human():
    base = dict(reason="r", confidence=0.9, severity="SEV3")
    assert agent.Suggestion(action="escalate", **base).needs_human
    assert not agent.Suggestion(action="scale", **base).needs_human
    assert agent.Suggestion(action="scale", **{**base, "confidence": 0.6}).needs_human
    assert agent.Suggestion(action="scale", **{**base, "severity": "SEV1"}).needs_human


# ---- end to end with a fake LLM --------------------------------------------------------
def test_llm_failure_falls_back_to_escalate(monkeypatch):
    async def boom(*a, **k):
        raise llm.LLMUnavailable("down")

    monkeypatch.setattr(llm, "complete_json", boom)
    t = tri(metrics={"affected_users_pct": 30})
    s = asyncio.run(agent.suggest(make_incident(), CHECKOUT, t, [], []))
    assert s.action == "escalate" and s.guardrail == "safe-fallback" and s.severity == "SEV2" and s.needs_human


def test_suggest_happy_path_with_fake_llm(monkeypatch):
    s1 = mem("m1", "rollback", text="Rolled back checkout-api v2.40.0; canary misses 5xx")

    async def fake(messages, validate):
        assert "[S1]" in messages[1]["content"] and "DEPLOY_CORRELATED" in messages[1]["content"]
        return validate({"action": "rollback", "severity": "SEV3", "confidence": 0.9, "reason": "same as S1",
                         "hypothesis": "bad deploy", "runbook": ["kubectl rollout undo"], "repeat": True,
                         "repeat_note": "2nd time", "cited_memories": ["S1"]}), "fake-model"

    monkeypatch.setattr(llm, "complete_json", fake)
    t = tri(recent_changes=DEPLOY, metrics={"error_rate": 0.06, "affected_users_pct": 20})
    s = asyncio.run(agent.suggest(make_incident(recent_changes=DEPLOY), CHECKOUT, t, [s1], []))
    assert (s.action, s.model, s.repeat, s.needs_human) == ("rollback", "fake-model", True, False)
    assert s.cited_memories[0]["label"] == "S1"
