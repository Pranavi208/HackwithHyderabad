"""Generate PaySetu's synthetic incident dataset.

Writes data/services.json, data/history.json (August, resolved), data/incidents.json (September,
open) and data/ground_truth.json (what the on-call engineer actually did, for replays).

Every service has 1-2 recurring failure modes with a consistent fix; a few September incidents are
genuine anomalies that look like a known pattern but must be escalated.

Run:  python scripts/generate_data.py
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.triage import triage  # noqa: E402

DATA = ROOT / "data"
rng = random.Random(20260928)

G = "https://grafana.paysetu.internal/d"


def svc(id, name, tier, team, oncall, p99, err, desc, deps=(), external=(), jobs=(), runbook=()):
    return {"id": id, "name": name, "tier": tier, "team": team, "channel": f"#inc-{id}",
            "oncall": list(oncall), "slo": {"availability": 99.95 if tier == 1 else 99.9, "p99_ms": p99,
                                            "error_rate": err},
            "description": desc, "dependencies": list(deps), "external": list(external), "jobs": list(jobs),
            "dashboards": f"{G}/{id}", "runbooks": list(runbook)}


SERVICES = [
    svc("checkout-api", "Checkout API", 1, "Checkout", ["Arjun Rao", "Meera Iyer", "Kabir Shah", "Divya Nair"],
        400, 0.01, "Cart-to-payment orchestration for 3.2M daily orders",
        deps=["orders-db", "session-cache", "payments-gateway", "upi-switch"],
        runbook=["Rollback: kubectl rollout undo deployment/checkout-api -n production",
                 "Kill switch: flagctl set <flag> off --env prod"]),
    svc("upi-switch", "UPI Switch", 1, "Payments Core", ["Sandeep Gupta", "Pooja Bhat", "Nikhil Das"],
        1500, 0.03, "UPI collect/intent routing to NPCI and partner PSP banks",
        external=["NPCI UPI", "PSP bank: Axis", "PSP bank: Yes Bank"],
        runbook=["Check NPCI status: https://status.npci.internal-mirror", "Post status page update for UPI"]),
    svc("payments-gateway", "Payments Gateway", 1, "Payments Core", ["Pooja Bhat", "Nikhil Das", "Sandeep Gupta"],
        1200, 0.02, "Card and netbanking authorisation, 3-D Secure",
        external=["Visa/Mastercard networks", "Issuer ACS: SBI", "Issuer ACS: ICICI"]),
    svc("ledger-service", "Ledger Service", 1, "Ledger & Recon", ["Revathi S", "Imran Khan", "Gaurav Jain"],
        600, 0.005, "Double-entry ledger and daily settlement reconciliation with partner banks",
        deps=["orders-db", "kafka-events"],
        jobs=[{"name": "EOD settlement reconciliation", "start": "23:30", "minutes": 40, "days": "daily"}]),
    svc("orders-db", "Orders DB (Postgres)", 1, "Data Platform", ["Karthik Subramanian", "Neha Joshi", "Amit Patel"],
        50, 0.001, "Primary Postgres 15 cluster (1 primary, 2 replicas) behind pgbouncer",
        jobs=[{"name": "nightly pg_basebackup", "start": "02:00", "minutes": 50, "days": "daily"},
              {"name": "finance reporting export", "start": "15:00", "minutes": 30, "days": "daily"}],
        runbook=["pgbouncer: systemctl restart pgbouncer (rolling, one node at a time)"]),
    svc("session-cache", "Session Cache (Redis)", 1, "Platform", ["Rahul Verma", "Swati Mishra", "Joseph Thomas"],
        20, 0.005, "Redis cluster for sessions and cart state",
        runbook=["Scale: kubectl scale statefulset/session-cache --replicas=<n>"]),
    svc("auth-service", "Auth Service", 1, "Identity", ["Priya Raman", "Aditya Kumar", "Zoya Ahmed"],
        300, 0.01, "Login, OTP and JWT issuance",
        jobs=[{"name": "JWKS signing-key rotation", "start": "04:00", "minutes": 10, "days": ["Sun"]}]),
    svc("kafka-events", "Kafka Event Bus", 2, "Data Platform", ["Neha Joshi", "Amit Patel", "Karthik Subramanian"],
        200, 0.01, "Kafka cluster; payment, order and ledger event topics",
        jobs=[{"name": "EOD batch settlement publish", "start": "22:45", "minutes": 70, "days": "daily"}]),
    svc("notification-service", "Notification Service", 2, "Engagement", ["Tanvi Shah", "Manoj Pillai", "Ritu Singh"],
        2000, 0.02, "SMS/OTP, email and push notifications",
        external=["SMS provider: MsgBharat (primary)", "SMS provider: TextNova (secondary)"]),
    svc("search-service", "Search Service", 2, "Discovery", ["Varun Nair", "Isha Kapoor", "Deepak Rao"],
        350, 0.01, "Elasticsearch-backed merchant and product search",
        jobs=[{"name": "catalog full reindex", "start": "03:00", "minutes": 45, "days": "daily"}]),
    svc("kyc-service", "KYC Service", 2, "Onboarding", ["Shreya Ghosh", "Mohit Agarwal", "Anjali Menon"],
        3000, 0.02, "Aadhaar eKYC, DigiLocker document fetch, video KYC",
        external=["DigiLocker API", "UIDAI eKYC"]),
    svc("merchant-dashboard", "Merchant Dashboard", 3, "Merchant Experience", ["Kiran Babu", "Sana Mirza"],
        800, 0.02, "Web dashboard for 400k merchants, served via CDN", external=["CDN edge"]),
]
S = {s["id"]: s for s in SERVICES}


# ---------------------------------------------------------------------------
# incident builder
# ---------------------------------------------------------------------------
def at(day: int, hhmm: str, month: int = 9) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, month, day, h, m)


def iso(d: datetime) -> str:
    return d.isoformat(timespec="minutes")


def deps(service_id: str, bad: dict[str, str] | None = None) -> list[dict[str, str]]:
    bad = bad or {}
    return [{"name": n, "status": bad.get(n, "operational")} for n in S[service_id]["external"]]


def base(sid: str, when: datetime, alert: str, source: str, summary: str, metrics: dict[str, Any],
         logs: list[str], changes: list[dict] | None = None, dep_status: dict[str, str] | None = None,
         **extra: Any) -> dict[str, Any]:
    m = {"error_rate": 0.0, "p99_ms": S[sid]["slo"]["p99_ms"] // 2, "saturation": 0.5, "burn_rate": 0.0,
         "affected_users_pct": 0.0, **metrics}
    noise = []
    if rng.random() < 0.4:  # unrelated change elsewhere, to keep correlation honest
        other = rng.choice([s for s in S if s != sid])
        noise.append({"type": "deploy", "service": other, "ref": f"{other} v{rng.randint(1, 9)}.{rng.randint(10, 80)}.0",
                      "at": iso(when - timedelta(minutes=rng.randint(10, 50))), "team": S[other]["team"]})
    return {"service_id": sid, "started_at": iso(when), "region": "ap-south-1 (Mumbai)",
            "alert": {"name": alert, "source": source, "summary": summary}, "metrics": m,
            "recent_changes": (changes or []) + noise, "dependencies": deps(sid, dep_status),
            "customer_reported": False, "support_tickets": 0, "data_integrity": None, "logs": logs, **extra}


def change(kind: str, sid: str, ref: str, when: datetime, mins: int) -> dict[str, Any]:
    return {"type": kind, "service": sid, "ref": ref, "at": iso(when - timedelta(minutes=mins)), "team": S[sid]["team"]}


Truth = dict[str, Any]
Pattern = Callable[[datetime, int], tuple[dict[str, Any], Truth]]


def truth(action: str, cause: str, note: str, base_mttr: int, assisted_mttr: int, pattern: str) -> Truth:
    return {"expected_action": action, "cause": cause, "note": note, "base_mttr": base_mttr,
            "assisted_mttr": assisted_mttr, "pattern": pattern}


# ---- recurring patterns (one or two per service) ----------------------------------
def checkout_deploy(when, n):
    ver, mins, err = f"v2.{41 + n}.0", rng.randint(6, 22), round(rng.uniform(0.045, 0.11), 3)
    users = round(rng.uniform(14, 32), 1)
    inc = base("checkout-api", when, "CheckoutHigh5xxRate", "Prometheus",
               f"5xx ratio {err:.1%} over 5m on checkout-api", {
                   "error_rate": err, "p99_ms": rng.randint(420, 780), "saturation": 0.55,
                   "burn_rate": round(rng.uniform(22, 60), 1), "affected_users_pct": users,
                   "failed_txn_per_min": rng.randint(900, 2400)},
               [f"ERROR checkout-api {ver} NullPointerException in PromoResolver.apply (cart without promo)",
                "WARN envoy upstream_rq_5xx rising on checkout-api pods (new ReplicaSet only)"],
               changes=[change("deploy", "checkout-api", f"checkout-api {ver}", when, mins)])
    return inc, truth("rollback", "bad-deploy-canary-misses-5xx",
                      f"Rolled back checkout-api {ver} (deployed {mins} min before the alert); 5xx back to baseline "
                      f"4 min after rollback. Canary analysis only checks latency, not 5xx ratio. "
                      f"Action item AI-CHK-12 'add 5xx ratio to canary gate' still open.", 38, 9, "checkout_deploy")


def checkout_flag(when, n):
    flag, mins = ["one_click_upi", "emi_offers_v2", "smart_retry"][n % 3], rng.randint(5, 25)
    err = round(rng.uniform(0.02, 0.05), 3)
    inc = base("checkout-api", when, "CheckoutHigh5xxRate", "Prometheus",
               f"5xx ratio {err:.1%} on /v2/checkout/confirm", {
                   "error_rate": err, "p99_ms": rng.randint(350, 520), "burn_rate": round(rng.uniform(12, 30), 1),
                   "affected_users_pct": round(rng.uniform(4, 12), 1), "failed_txn_per_min": rng.randint(200, 700)},
               [f"ERROR FlagEvaluator: {flag} enabled for 25% cohort; handler ConfirmV3 returned 500",
                "INFO no deploys on checkout-api in the last 6h"],
               changes=[change("flag", "checkout-api", f"{flag} -> 25%", when, mins)])
    return inc, truth("feature_flag", "flag-rollout-without-guardrail-metric",
                      f"Turned feature flag {flag} off via kill switch; errors cleared within 2 min. Flag ramps are "
                      f"not tied to an error-rate guardrail (AI-CHK-15 open).", 30, 6, "checkout_flag")


def upi_npci(when, n):
    fail = round(rng.uniform(0.055, 0.095), 3)
    bank = ["PSP bank: Axis", "PSP bank: Yes Bank"][n % 2]
    inc = base("upi-switch", when, "UPISuccessRateDrop", "Datadog",
               f"UPI success rate {1 - fail:.1%} (baseline 99.2%)", {
                   "error_rate": fail, "p99_ms": rng.randint(1700, 2600), "burn_rate": round(rng.uniform(8, 18), 1),
                   "affected_users_pct": round(fail * 100 * 1.4, 1), "failed_txn_per_min": rng.randint(1500, 4200)},
               ["WARN NPCI response U28/U30 (PSP not available) rising", f"INFO failures concentrated on {bank.split(': ')[1]} handles"],
               dep_status={"NPCI UPI": "degraded", bank: "degraded"})
    return inc, truth("vendor", "npci-psp-degradation",
                      f"NPCI/{bank.split(': ')[1]} side degradation (U28/U30 codes). Raised ticket with NPCI, posted "
                      f"status page banner, enabled smart retry. Recovered on its own in ~25 min. Typical failure "
                      f"6-9% in these episodes.", 35, 10, "upi_npci")


def pg_acs(when, n):
    issuer = ["Issuer ACS: SBI", "Issuer ACS: ICICI"][n % 2]
    fail = round(rng.uniform(0.04, 0.08), 3)
    inc = base("payments-gateway", when, "CardAuthTimeouts", "Datadog",
               f"3-D Secure timeouts {fail:.1%} on {issuer.split(': ')[1]} cards", {
                   "error_rate": fail, "p99_ms": rng.randint(4000, 9000), "burn_rate": round(rng.uniform(6, 12), 1),
                   "affected_users_pct": round(rng.uniform(3, 7), 1), "failed_txn_per_min": rng.randint(150, 500)},
               [f"WARN ACS {issuer.split(': ')[1]} p99 8.4s, timeouts after 10s", "INFO other issuers healthy"],
               dep_status={issuer: "degraded"})
    return inc, truth("vendor", "issuer-acs-slow",
                      f"{issuer} slow. Opened ticket with issuer bank, showed 'try UPI' nudge to affected card "
                      f"BINs. Resolved from bank side in ~40 min.", 45, 12, "pg_acs")


def db_backup_lag(when, n):
    lag = rng.randint(90, 240)
    inc = base("orders-db", when, "PostgresReplicaLagHigh", "Prometheus",
               f"replica-2 replication lag {lag}s (threshold 60s)", {
                   "replica_lag_s": lag, "p99_ms": 22, "saturation": 0.62, "saturation_resource": "disk IO"},
               ["INFO pg_basebackup running from replica-2", "INFO reads served by replica-1; replica-2 out of LB pool"])
    return inc, truth("suppress", "backup-io-replica-lag",
                      f"Known noise: nightly pg_basebackup saturates IO on replica-2 and lag hits {lag}s; replica-2 is "
                      f"drained from the read pool during backup so no user impact. Acked. Alert-tuning ticket "
                      f"OBS-44 (silence during backup window) still open.", 20, 2, "db_backup_lag")


def db_pool(when, n):
    sat = round(rng.uniform(0.96, 1.0), 2)
    inc = base("orders-db", when, "PgbouncerPoolExhausted", "Prometheus",
               f"pgbouncer client wait queue {rng.randint(300, 900)}, pool {sat:.0%} used", {
                   "error_rate": round(rng.uniform(0.02, 0.06), 3), "p99_ms": rng.randint(900, 2500),
                   "saturation": sat, "saturation_resource": "pgbouncer pool",
                   "burn_rate": round(rng.uniform(15, 30), 1), "affected_users_pct": round(rng.uniform(8, 20), 1)},
               ["WARN pgbouncer: no more connections allowed (max_client_conn)",
                "INFO long-running queries from role finance_report on primary (SELECT ... FROM orders JOIN ...)"])
    return inc, truth("restart", "reporting-job-on-primary",
                      "Killed the finance reporting export queries on the primary and did a rolling restart of "
                      "pgbouncer; pool recovered in 3 min. Root cause: reporting export reads from the primary "
                      "instead of a replica. Action item AI-DATA-7 'move finance export to replica-1' still NOT done.",
                      50, 12, "db_pool")


def cache_flash(when, n):
    sat = round(rng.uniform(0.9, 0.97), 2)
    inc = base("session-cache", when, "RedisMemoryHigh", "Prometheus",
               f"session-cache used_memory {sat:.0%} of maxmemory, evictions {rng.randint(2, 9)}k/s", {
                   "error_rate": round(rng.uniform(0.006, 0.02), 3), "p99_ms": rng.randint(25, 60),
                   "saturation": sat, "saturation_resource": "Redis memory",
                   "affected_users_pct": round(rng.uniform(2, 6), 1)},
               ["WARN evicted_keys rising; cart sessions dropped", "INFO Friday 8PM flash sale traffic 3.4x"])
    return inc, truth("scale", "flash-sale-capacity",
                      "Friday flash sale traffic. Scaled session-cache from 6 to 9 shards; evictions stopped in 5 min. "
                      "Pre-scaling before sales is on the capacity plan (CAP-9).", 25, 6, "cache_flash")


def auth_jwks(when, n):
    err = round(rng.uniform(0.02, 0.04), 3)
    inc = base("auth-service", when, "Auth401Spike", "Datadog",
               f"401 responses {err:.1%} on token validation", {
                   "error_rate": err, "p99_ms": 140, "affected_users_pct": round(rng.uniform(0.3, 0.8), 1)},
               ["WARN kid not found in JWKS cache; refetching", "INFO signing key rotated at 04:00"])
    return inc, truth("monitor", "jwks-cache-ttl-after-rotation",
                      "Weekly JWKS key rotation; edge caches hold old keys for up to 5 min so some tokens fail "
                      "validation. Self-heals by 04:07; watched it, no action. Few users at 4 AM.", 15, 2, "auth_jwks")


def kafka_eod(when, n):
    lag = rng.randint(180_000, 420_000)
    inc = base("kafka-events", when, "KafkaConsumerLagHigh", "Prometheus",
               f"ledger-consumer lag {lag:,} messages on payments.settled", {
                   "consumer_lag": lag, "p99_ms": 120, "saturation": round(rng.uniform(0.86, 0.93), 2),
                   "saturation_resource": "consumer CPU"},
               ["INFO EOD batch settlement publish started 22:45", "WARN ledger-consumer group 12 consumers, 48 partitions"])
    return inc, truth("scale", "eod-batch-consumer-capacity",
                      f"EOD batch publish; lag {lag:,}. Scaled ledger-consumer from 12 to 24 replicas, lag drained "
                      f"in 18 min. No user impact.", 30, 5, "kafka_eod")


def sms_failover(when, n):
    fail = round(rng.uniform(0.12, 0.25), 3)
    inc = base("notification-service", when, "OTPDeliveryFailures", "Datadog",
               f"SMS OTP delivery failure {fail:.0%} on primary provider", {
                   "error_rate": fail, "p99_ms": rng.randint(4000, 8000),
                   "affected_users_pct": round(rng.uniform(2, 5), 1)},
               ["WARN MsgBharat HTTP 429 Too Many Requests", "INFO TextNova (secondary) healthy"],
               dep_status={"SMS provider: MsgBharat (primary)": "degraded"})
    return inc, truth("failover", "primary-sms-throttling",
                      "Primary SMS provider MsgBharat throttling (429). Failed over OTP traffic to TextNova via "
                      "provider switch; OTP success back to 99% in 3 min. Switch back after vendor confirms.", 25, 5,
                      "sms_failover")


def search_reindex(when, n):
    sat = round(rng.uniform(0.9, 0.97), 2)
    inc = base("search-service", when, "ESHeapPressure", "Prometheus",
               f"es-data-3 JVM heap {sat:.0%}, search p99 {rng.randint(900, 1800)} ms", {
                   "p99_ms": rng.randint(900, 1800), "saturation": sat, "saturation_resource": "JVM heap",
                   "error_rate": round(rng.uniform(0.004, 0.009), 3), "affected_users_pct": round(rng.uniform(0.5, 1.5), 1)},
               ["WARN [gc][old] es-data-3 spent 4.2s in GC", "INFO catalog full reindex running"])
    return inc, truth("restart", "reindex-heap-fragmentation",
                      "Catalog reindex fragments heap on es-data-3. Rolling restart of es-data-3 after shard "
                      "relocation; p99 normal in 8 min. Moving reindex to a dedicated node is backlog (SRCH-21).",
                      35, 8, "search_reindex")


def kyc_digilocker(when, n):
    err = round(rng.uniform(0.3, 0.55), 2)
    inc = base("kyc-service", when, "KYCVendorTimeouts", "Datadog",
               f"DigiLocker fetch timeouts {err:.0%}", {
                   "error_rate": err, "p99_ms": rng.randint(9000, 15000),
                   "affected_users_pct": round(rng.uniform(1, 3), 1)},
               ["WARN DigiLocker /pull-doc 504 gateway timeout", "INFO UIDAI eKYC healthy"],
               dep_status={"DigiLocker API": "degraded"})
    return inc, truth("feature_flag", "digilocker-timeouts",
                      "DigiLocker degraded. Switched flag kyc.docs_fallback=upload so users upload PAN/Aadhaar images "
                      "for manual review; onboarding continued. Flag reverted after DigiLocker recovered.", 40, 7,
                      "kyc_digilocker")


def ledger_recon(when, n):
    inc = base("ledger-service", when, "ReconPendingHigh", "Prometheus",
               f"{rng.randint(1200, 3800)} settlement entries pending reconciliation", {
                   "p99_ms": 300, "saturation": 0.7, "saturation_resource": "recon workers",
                   "recon_pending": rng.randint(1200, 3800)},
               ["INFO EOD settlement file from partner bank arrived 23:34", "INFO recon workers processing, 0 mismatches"])
    return inc, truth("monitor", "eod-recon-backlog",
                      "EOD settlement reconciliation backlog while partner bank files arrive. 0 mismatches; clears "
                      "by 00:10 on its own. Watched, no action.", 20, 3, "ledger_recon")


def cdn_purge(when, n):
    inc = base("merchant-dashboard", when, "Origin504Spike", "CDN logs",
               "Origin 504s 3.1% after cache purge", {
                   "error_rate": 0.031, "p99_ms": 2100, "affected_users_pct": round(rng.uniform(1, 3), 1)},
               ["INFO full cache purge by release pipeline", "WARN origin cold, cache hit ratio 41%"],
               changes=[change("config", "merchant-dashboard", "CDN full purge", when, rng.randint(3, 8))])
    return inc, truth("monitor", "cdn-cold-cache-after-purge",
                      "Cold cache after full CDN purge by the release pipeline; hit ratio recovered and 504s gone in "
                      "~10 min. Watched. Ticket MX-8: purge by path, not full purge.", 20, 4, "cdn_purge")


# ---- anomalies (September only) -------------------------------------------------------
def a_upi_spike(when, n):
    inc = base("upi-switch", when, "UPISuccessRateDrop", "Datadog", "UPI success rate 57.8% (baseline 99.2%)", {
        "error_rate": 0.422, "p99_ms": 3100, "burn_rate": 84.0, "affected_users_pct": 58.0,
        "failed_txn_per_min": 21800},
        ["ERROR HSM sign() timeout after 2000ms", "INFO NPCI response codes normal; failures are internal U16"],
        customer_reported=True, support_tickets=740)
    return inc, truth("escalate", "hsm-connection-leak",
                      "NOT an NPCI blip: NPCI green and failure 42%, far beyond the usual 6-9%. War room: HSM client "
                      "connection leak after a certificate renewal; human IC coordinated failover to the standby HSM. "
                      "New failure mode.", 95, 95, "anomaly_upi")


def a_ledger_mismatch(when, n):
    inc = base("ledger-service", when, "ReconMismatch", "Prometheus",
               "Reconciliation mismatches detected against partner bank settlement", {
                   "p99_ms": 320, "saturation": 0.72, "saturation_resource": "recon workers", "recon_pending": 2100},
               ["ERROR 312 entries: ledger amount != bank settlement amount", "INFO EOD settlement file arrived 23:36"],
               data_integrity={"mismatch_count": 312, "amount_inr": 4_870_500})
    return inc, truth("escalate", "double-posted-refunds",
                      "Real mismatch, not the usual backlog: 312 entries worth INR 48.7 lakh. SEV1, finance and "
                      "VP Eng paged, refunds double-posted by a retry bug. Froze payouts until fixed.", 180, 180,
                      "anomaly_ledger")


def a_kafka_midday(when, n):
    lag = 2_900_000
    inc = base("kafka-events", when, "KafkaConsumerLagHigh", "Prometheus",
               f"ledger-consumer lag {lag:,} messages on payments.settled", {
                   "consumer_lag": lag, "p99_ms": 400, "saturation": 0.91, "saturation_resource": "consumer CPU",
                   "error_rate": 0.02, "affected_users_pct": 3.0},
               ["ERROR ledger-consumer poison message offset 88123311: deserialization failed, retrying",
                "INFO no batch job scheduled at this time"])
    return inc, truth("escalate", "poison-message",
                      "Midday lag of 2.9M with no EOD batch running: not the usual capacity issue; scaling would not "
                      "help. Poison message blocked a partition; human IC coordinated skip + DLQ.", 70, 70,
                      "anomaly_kafka")


def a_duplicate(parent: dict[str, Any], when: datetime) -> tuple[dict[str, Any], Truth]:
    inc = json.loads(json.dumps(parent))
    inc["started_at"] = iso(when)
    inc["alert"]["summary"] = parent["alert"]["summary"] + " (re-fired)"
    inc["logs"] = ["INFO alertmanager re-notified: group_interval elapsed"] + parent["logs"][:1]
    return inc, truth("merge", "alert-refire-during-incident",
                      f"Duplicate of {parent['id']}: same alert re-fired while the rollback was in progress. Merged "
                      f"into the parent incident.", 10, 1, "duplicate")


# ---------------------------------------------------------------------------
PATTERNS: dict[str, tuple[Pattern, list[str]]] = {
    # name: (generator, alert times)
    "checkout_deploy": (checkout_deploy, ["11:20", "14:40", "17:05", "12:30"]),
    "checkout_flag": (checkout_flag, ["13:15", "19:40", "16:20"]),
    "upi_npci": (upi_npci, ["10:05", "18:30", "20:45", "09:40", "13:50"]),
    "pg_acs": (pg_acs, ["12:10", "21:15", "11:45"]),
    "db_backup_lag": (db_backup_lag, ["02:15", "02:25", "02:10", "02:30", "02:20", "02:35"]),
    "db_pool": (db_pool, ["15:10", "15:05", "15:15"]),
    "cache_flash": (cache_flash, ["20:05", "20:10", "20:02", "20:08"]),
    "auth_jwks": (auth_jwks, ["04:02", "04:03", "04:01", "04:04"]),
    "kafka_eod": (kafka_eod, ["23:05", "23:15", "22:55", "23:20", "23:10"]),
    "sms_failover": (sms_failover, ["10:30", "19:10", "08:50", "17:35"]),
    "search_reindex": (search_reindex, ["03:20", "03:15", "03:25", "03:10"]),
    "kyc_digilocker": (kyc_digilocker, ["11:10", "16:40", "12:25"]),
    "ledger_recon": (ledger_recon, ["23:40", "23:45", "23:38", "23:50"]),
    "cdn_purge": (cdn_purge, ["18:05", "15:30"]),
}

# September schedule: pattern -> days (Fridays 4/11/18/25 for flash sales, Sundays 6/13/20/27 for JWKS)
SEPTEMBER = {
    "checkout_deploy": [2, 9, 16, 24], "checkout_flag": [4, 18, 26], "upi_npci": [1, 7, 12, 19, 25],
    "pg_acs": [3, 14, 23], "db_backup_lag": [2, 5, 10, 15, 20, 27], "db_pool": [8, 17, 29],
    "cache_flash": [4, 11, 18, 25], "auth_jwks": [6, 13, 20, 27], "kafka_eod": [3, 10, 16, 23, 30],
    "sms_failover": [5, 14, 21, 28], "search_reindex": [6, 12, 19, 26], "kyc_digilocker": [8, 15, 24],
    "ledger_recon": [1, 11, 22, 30], "cdn_purge": [9, 23],
}
ANOMALIES = [(a_upi_spike, 22, "14:20"), (a_ledger_mismatch, 26, "23:42"), (a_kafka_midday, 21, "14:05")]
DUPLICATE_OF = ("checkout_deploy", 16, 12)  # re-fire 12 min after the 16 Sep deploy incident

# August history (already resolved): Fridays 7/14/21/28, Sundays 2/9/16/23/30
AUGUST = {
    "checkout_deploy": [5, 19], "upi_npci": [4, 13, 26], "db_backup_lag": [3, 12, 24], "db_pool": [11, 25],
    "cache_flash": [14, 28], "auth_jwks": [9, 23], "kafka_eod": [6, 20], "ledger_recon": [10, 27],
    "sms_failover": [18], "pg_acs": [21], "search_reindex": [17],
}


def build(schedule: dict[str, list[int]], month: int) -> list[tuple[dict, Truth]]:
    out = []
    for name, days in schedule.items():
        gen, times = PATTERNS[name]
        for n, day in enumerate(days):
            inc, t = gen(at(day, times[n % len(times)], month), n + (3 if month == 9 else 0))
            out.append((inc, t))
    return out


def assign_ids(items: list[tuple[dict, Truth]], prefix: str) -> None:
    items.sort(key=lambda it: it[0]["started_at"])
    per_day: Counter = Counter()
    for inc, _ in items:
        d = datetime.fromisoformat(inc["started_at"])
        per_day[d.date()] += 1
        inc["id"] = f"{prefix}-{d:%m%d}-{per_day[d.date()]:02d}"
        inc["date"] = d.date().isoformat()
        inc["day"] = d.day


def main() -> None:
    DATA.mkdir(exist_ok=True)

    # ---- August history (resolved) ----
    hist = build(AUGUST, 8)
    assign_ids(hist, "INC")
    history = []
    for i, (inc, t) in enumerate(hist):
        tri = triage(inc, S[inc["service_id"]], [h for h, _ in hist[:i]])
        history.append({**inc, "signals": tri["signals"], "severity": tri["severity"],
                        "action": t["expected_action"], "cause": t["cause"], "note": t["note"],
                        "mttr_min": t["base_mttr"]})

    # ---- September (open) ----
    sept = build(SEPTEMBER, 9)
    for gen, day, hhmm in ANOMALIES:
        sept.append(gen(at(day, hhmm), 0))
    assign_ids(sept, "INC")
    name, day, gap = DUPLICATE_OF
    parent = next(inc for inc, t in sept if t["pattern"] == name and inc["day"] == day)
    sept.append(a_duplicate(parent, datetime.fromisoformat(parent["started_at"]) + timedelta(minutes=gap)))
    for inc, _ in sept:
        inc.pop("id", None)
    assign_ids(sept, "INC")

    incidents = [inc for inc, _ in sept]
    ground = {inc["id"]: t for inc, t in sept}

    # ---- validate: the signals each fix depends on must be present ----
    need = {"rollback": {"DEPLOY_CORRELATED"}, "feature_flag": set(), "vendor": {"DEPENDENCY_DEGRADED"},
            "suppress": {"NO_USER_IMPACT", "SCHEDULED_JOB"}, "merge": {"DUPLICATE_ALERT"},
            "failover": {"DEPENDENCY_DEGRADED"}}
    problems = []
    for i, inc in enumerate(incidents):
        tri = triage(inc, S[inc["service_id"]], incidents[:i])
        codes = {s["code"] for s in tri["signals"]}
        t = ground[inc["id"]]
        missing = need.get(t["expected_action"], set()) - codes
        if missing:
            problems.append(f"{inc['id']} {t['pattern']}: missing {missing}")
        if t["pattern"] != "duplicate" and "DUPLICATE_ALERT" in codes:
            problems.append(f"{inc['id']} {t['pattern']}: unexpected duplicate")
        if t["pattern"] == "anomaly_upi" and "DEPENDENCY_DEGRADED" in codes:
            problems.append("anomaly_upi must have a green NPCI")
        if t["pattern"] == "anomaly_ledger" and tri["severity"] != "SEV1":
            problems.append("ledger mismatch must be SEV1")
        if t["pattern"] == "anomaly_kafka" and "SCHEDULED_JOB" in codes:
            problems.append("midday kafka lag must not overlap the EOD batch")
        if t["pattern"] in ("kafka_eod", "db_backup_lag", "auth_jwks", "search_reindex", "ledger_recon") \
                and "SCHEDULED_JOB" not in codes:
            problems.append(f"{inc['id']} {t['pattern']}: expected SCHEDULED_JOB")
    if problems:
        raise SystemExit("Dataset validation failed:\n  " + "\n  ".join(problems))

    (DATA / "services.json").write_text(json.dumps(SERVICES, indent=1, ensure_ascii=False), encoding="utf-8")
    (DATA / "history.json").write_text(json.dumps(history, indent=1, ensure_ascii=False), encoding="utf-8")
    (DATA / "incidents.json").write_text(json.dumps(incidents, indent=1, ensure_ascii=False), encoding="utf-8")
    (DATA / "ground_truth.json").write_text(json.dumps(ground, indent=1, ensure_ascii=False), encoding="utf-8")

    sev = Counter(triage(inc, S[inc["service_id"]], incidents[:i])["severity"] for i, inc in enumerate(incidents))
    acts = Counter(t["expected_action"] for t in ground.values())
    print(f"{len(SERVICES)} services, {len(history)} August incidents, {len(incidents)} September incidents")
    print("severity:", dict(sorted(sev.items())))
    print("expected actions:", dict(acts.most_common()))


if __name__ == "__main__":
    main()
