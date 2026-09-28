"""Shared fixtures: a tiny service catalogue and an incident factory (no data files needed)."""
from __future__ import annotations

import copy
from typing import Any

import pytest

CHECKOUT = {
    "id": "checkout-api", "name": "Checkout API", "tier": 1, "team": "Checkout", "channel": "#inc-checkout-api",
    "oncall": ["A", "B", "C"], "slo": {"availability": 99.95, "p99_ms": 400, "error_rate": 0.01},
    "description": "Cart-to-payment orchestration", "dependencies": [], "external": [], "jobs": [],
}
ORDERS_DB = {
    "id": "orders-db", "name": "Orders DB", "tier": 1, "team": "Data Platform", "channel": "#inc-orders-db",
    "oncall": ["D", "E"], "slo": {"availability": 99.95, "p99_ms": 50, "error_rate": 0.001},
    "description": "Postgres", "dependencies": [], "external": [],
    "jobs": [{"name": "nightly pg_basebackup", "start": "02:00", "minutes": 50, "days": "daily"}],
}
AUTH = {
    "id": "auth-service", "name": "Auth", "tier": 1, "team": "Identity", "channel": "#inc-auth",
    "oncall": ["F", "G"], "slo": {"availability": 99.95, "p99_ms": 300, "error_rate": 0.01},
    "description": "Login", "dependencies": [], "external": [],
    "jobs": [{"name": "JWKS rotation", "start": "04:00", "minutes": 10, "days": ["Sun"]}],
}


def make_incident(**over: Any) -> dict[str, Any]:
    """A quiet checkout-api incident; override any field (``metrics`` is merged)."""
    inc: dict[str, Any] = {
        "id": "INC-0902-01", "service_id": "checkout-api", "started_at": "2026-09-02T11:20", "day": 2,
        "alert": {"name": "CheckoutHigh5xxRate", "source": "Prometheus", "summary": "5xx 6%"},
        "metrics": {"error_rate": 0.0, "p99_ms": 200, "saturation": 0.5, "burn_rate": 0.0, "affected_users_pct": 0.0},
        "recent_changes": [], "dependencies": [], "customer_reported": False, "support_tickets": 0,
        "data_integrity": None, "logs": [],
    }
    metrics = over.pop("metrics", {})
    inc.update(copy.deepcopy(over))
    inc["metrics"].update(metrics)
    return inc


@pytest.fixture
def incident():
    return make_incident
