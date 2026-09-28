"""Tests for deterministic triage: signals, SEV classification, escalation triggers, roles."""
from __future__ import annotations

from app.triage import (SEV_MATRIX, assign_roles, check_incident, classify_severity, next_update_due, triage,
                        worst)
from conftest import AUTH, CHECKOUT, ORDERS_DB, make_incident


def codes(inc, svc=CHECKOUT, prior=None):
    return {s.code for s in check_incident(inc, svc, prior)}


def test_deploy_within_window_is_correlated_other_services_ignored():
    inc = make_incident(recent_changes=[
        {"type": "deploy", "service": "checkout-api", "ref": "v2.41.0", "at": "2026-09-02T11:05"},
        {"type": "deploy", "service": "search-service", "ref": "v1", "at": "2026-09-02T11:10"},
    ])
    sigs = check_incident(inc, CHECKOUT)
    dep = [s for s in sigs if s.code == "DEPLOY_CORRELATED"]
    assert len(dep) == 1 and dep[0].details["minutes_before"] == 15 and dep[0].details["ref"] == "v2.41.0"


def test_old_or_future_changes_are_not_correlated():
    inc = make_incident(recent_changes=[
        {"type": "deploy", "service": "checkout-api", "ref": "old", "at": "2026-09-02T09:00"},
        {"type": "flag", "service": "checkout-api", "ref": "later", "at": "2026-09-02T11:30"},
    ])
    assert not codes(inc) & {"DEPLOY_CORRELATED", "FLAG_CHANGE"}


def test_flag_and_config_changes_have_their_own_signals():
    inc = make_incident(recent_changes=[
        {"type": "flag", "service": "checkout-api", "ref": "x -> 25%", "at": "2026-09-02T11:00"},
        {"type": "config", "service": "checkout-api", "ref": "cdn purge", "at": "2026-09-02T11:15"},
    ])
    assert {"FLAG_CHANGE", "CONFIG_CHANGE"} <= codes(inc)


def test_degraded_dependency_flagged_operational_not():
    ok = make_incident(dependencies=[{"name": "NPCI UPI", "status": "operational"}])
    bad = make_incident(dependencies=[{"name": "NPCI UPI", "status": "degraded"}])
    assert "DEPENDENCY_DEGRADED" not in codes(ok)
    assert "DEPENDENCY_DEGRADED" in codes(bad)


def test_error_latency_saturation_and_burn_thresholds():
    inc = make_incident(metrics={"error_rate": 0.05, "p99_ms": 900, "saturation": 0.9, "burn_rate": 20})
    c = codes(inc)
    assert {"ERROR_RATE", "LATENCY_SLO", "SATURATION", "ERROR_BUDGET_BURN"} <= c
    quiet = make_incident(metrics={"error_rate": 0.01, "p99_ms": 400, "saturation": 0.84, "burn_rate": 5.9})
    assert not codes(quiet) & {"ERROR_RATE", "LATENCY_SLO", "SATURATION", "ERROR_BUDGET_BURN"}


def test_burn_rate_page_vs_ticket_severity():
    page = [s for s in check_incident(make_incident(metrics={"burn_rate": 14.4}), CHECKOUT) if s.code == "ERROR_BUDGET_BURN"]
    ticket = [s for s in check_incident(make_incident(metrics={"burn_rate": 7}), CHECKOUT) if s.code == "ERROR_BUDGET_BURN"]
    assert page[0].severity == "high" and ticket[0].severity == "low"


def test_scheduled_job_window_daily_and_weekday():
    backup = make_incident(service_id="orders-db", started_at="2026-09-02T02:20")
    assert "SCHEDULED_JOB" in codes(backup, ORDERS_DB)
    midday = make_incident(service_id="orders-db", started_at="2026-09-02T14:05")
    assert "SCHEDULED_JOB" not in codes(midday, ORDERS_DB)
    sunday = make_incident(service_id="auth-service", started_at="2026-09-06T04:03")   # a Sunday
    monday = make_incident(service_id="auth-service", started_at="2026-09-07T04:03")
    assert "SCHEDULED_JOB" in codes(sunday, AUTH) and "SCHEDULED_JOB" not in codes(monday, AUTH)


def test_duplicate_alert_same_service_and_alert_within_window():
    parent = make_incident(id="INC-0916-01", started_at="2026-09-16T11:20")
    dup = make_incident(id="INC-0916-02", started_at="2026-09-16T11:32")
    later = make_incident(id="INC-0916-03", started_at="2026-09-16T12:30")
    other_alert = make_incident(id="INC-0916-04", started_at="2026-09-16T11:25",
                                alert={"name": "Other", "source": "x", "summary": "y"})
    sig = [s for s in check_incident(dup, CHECKOUT, [parent]) if s.code == "DUPLICATE_ALERT"]
    assert sig and sig[0].details == {"parent": "INC-0916-01", "gap_min": 12}
    assert "DUPLICATE_ALERT" not in codes(later, prior=[parent])
    assert "DUPLICATE_ALERT" not in codes(other_alert, prior=[parent])


def test_no_user_impact_signal():
    assert "NO_USER_IMPACT" in codes(make_incident())
    assert "NO_USER_IMPACT" not in codes(make_incident(metrics={"affected_users_pct": 2}))
    assert "NO_USER_IMPACT" not in codes(make_incident(customer_reported=True))


def sev(inc, svc=CHECKOUT):
    return classify_severity(inc, svc, check_incident(inc, svc))[0]


def test_severity_matrix():
    assert sev(make_incident()) == "SEV4"
    assert sev(make_incident(metrics={"affected_users_pct": 5})) == "SEV3"
    assert sev(make_incident(metrics={"affected_users_pct": 30})) == "SEV2"
    assert sev(make_incident(metrics={"affected_users_pct": 95})) == "SEV1"
    assert sev(make_incident(metrics={"error_rate": 0.3, "affected_users_pct": 5})) == "SEV2"  # tier-1 key feature down


def test_data_integrity_is_always_sev1():
    inc = make_incident(data_integrity={"mismatch_count": 3, "amount_inr": 900})
    s, reasons = classify_severity(inc, CHECKOUT, check_incident(inc, CHECKOUT))
    assert s == "SEV1" and "data integrity" in reasons[0].lower()


def test_customer_reported_on_tier1_is_minimum_sev2():
    assert sev(make_incident(customer_reported=True, metrics={"affected_users_pct": 3})) == "SEV2"
    tier2 = {**CHECKOUT, "tier": 2}
    assert sev(make_incident(customer_reported=True, metrics={"affected_users_pct": 3}), tier2) == "SEV3"


def test_roles_sev1_2_get_dedicated_ic_and_comms():
    inc = make_incident()
    big = assign_roles(inc, CHECKOUT, "SEV2")
    assert set(big) == {"incident_commander", "tech_lead", "communications_lead", "scribe"}
    assert big["incident_commander"] not in CHECKOUT["oncall"] and big["tech_lead"] in CHECKOUT["oncall"]
    small = assign_roles(inc, CHECKOUT, "SEV4")
    assert small["incident_commander"] == small["tech_lead"] in CHECKOUT["oncall"]


def test_triage_bundle_and_update_cadence():
    t = triage(make_incident(metrics={"affected_users_pct": 30}), CHECKOUT)
    assert t["severity"] == "SEV2" and t["sev_policy"] == SEV_MATRIX["SEV2"]
    assert next_update_due("2026-09-02T11:20", "SEV1") == "2026-09-02T11:35"
    assert next_update_due("2026-09-02T11:20", "SEV2") == "2026-09-02T11:50"


def test_worst():
    assert worst("SEV3", "SEV1") == "SEV1" and worst("SEV2", "SEV4") == "SEV2"
