"""
DriftWatch -- rule-based monitoring + continuous-check engine for a
decisions/outcomes pipeline (the shared schema a fleet of small services
writes to: id, system, subject, ts, features, output, joined with an
outcomes table on ts/outcome).

Implements four core checks -- row count, freshness, null rate, duplicate
count -- each with a per-system baseline and a justified threshold, not a
single global number. Alerting categories implemented: rule-based
programmatic triggers (explicit threshold-and-action rules below) plus
continuous monitoring (the same checks, meant to be run on a schedule
rather than once).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class SystemProfile:
    """Per-system baseline + thresholds. Each system gets its own profile
    rather than one global number -- a continuous-traffic system (ED triage)
    and a periodic-batch system (a nightly eval run) need different
    freshness SLAs, and treating them the same produces constant false
    pages on the slow one or missed outages on the fast one."""
    name: str
    baseline_per_day: float
    freshness_sla_hours: float
    row_count_drop_floor_pct: float = 0.20  # alert if 24h count < this * trailing baseline
    duplicate_subject_ts_tolerance_pct: float = 0.01


@dataclass
class Alert:
    metric: str
    severity: str  # "page" | "ticket"
    system: str
    message: str
    routes_to: str
    first_diagnostic_action: str


ROUTING = {
    "row_count": ("per-system on-call engineer",
                   "Check whether inbound requests are even reaching the API layer "
                   "(upstream request-log count) before assuming demand dropped -- a "
                   "broken write path and a real demand lull look identical from the "
                   "decisions table alone."),
    "freshness": ("per-system on-call engineer",
                   "Check the service process's own liveness before investigating "
                   "anything data-side -- a crashed process is the fastest-to-confirm cause."),
    "null_rate": ("shared kernel owner",
                   "Diff the calling code's most recent change against the last known-good "
                   "kernel version to find which call site stopped passing a required field."),
    "duplicate_id": ("shared kernel owner",
                      "Check whether something bypassed the kernel's own insert path -- a "
                      "duplicate primary key should be structurally impossible."),
    "duplicate_subject_ts": ("shared kernel owner",
                              "Separate a real resubmission (expected) from a double-write bug "
                              "by checking whether an idempotency key is missing on the "
                              "resubmission path."),
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def load_rows(db_path: str) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT id, system, subject, ts FROM decisions"
    ).fetchall()]
    con.close()
    return rows


def check_row_count(rows: list[dict], profile: SystemProfile, now: datetime) -> list[Alert]:
    """Deliberately a drop-only threshold, no upper bound (Section 6.1's Prime
    Day caution): a volume spike is success, not a quality problem."""
    window_start = now.timestamp() - 24 * 3600
    count_24h = sum(1 for r in rows if (_parse_ts(r["ts"]) or now).timestamp() >= window_start)
    floor = profile.baseline_per_day * profile.row_count_drop_floor_pct
    if count_24h < floor:
        return [Alert("row_count", "page", profile.name,
                       f"24h decision count ({count_24h}) is below {profile.row_count_drop_floor_pct:.0%} "
                       f"of the trailing baseline ({profile.baseline_per_day:.1f}/day, floor={floor:.1f})",
                       *ROUTING["row_count"])]
    return []


def check_freshness(rows: list[dict], profile: SystemProfile, now: datetime) -> list[Alert]:
    timestamps = [t for t in (_parse_ts(r["ts"]) for r in rows) if t is not None]
    if not timestamps:
        age_hours = float("inf")
    else:
        age_hours = (now - max(timestamps)).total_seconds() / 3600
    if age_hours > profile.freshness_sla_hours:
        return [Alert("freshness", "page", profile.name,
                       f"no new decision row for {age_hours:.1f}h, exceeding this system's "
                       f"{profile.freshness_sla_hours:.1f}h SLA",
                       *ROUTING["freshness"])]
    return []


def check_null_rate(rows: list[dict], profile: SystemProfile) -> list[Alert]:
    """Zero-tolerance: these columns are written by the same code path every
    time regardless of volume, so a null isn't a volume artifact, it's a
    new code defect."""
    bad = [r for r in rows if not r.get("subject") or not str(r["subject"]).strip()
           or not r.get("ts") or not r.get("system")]
    if bad:
        return [Alert("null_rate", "page", profile.name,
                       f"{len(bad)}/{len(rows)} rows have a null/empty required field "
                       "(subject/ts/system)",
                       *ROUTING["null_rate"])]
    return []


def check_duplicates(rows: list[dict], profile: SystemProfile) -> list[Alert]:
    alerts = []
    ids = [r["id"] for r in rows]
    dup_ids = len(ids) - len(set(ids))
    if dup_ids > 0:
        alerts.append(Alert("duplicate_id", "page", profile.name,
                             f"{dup_ids} duplicate primary-key id(s) found -- should be "
                             "structurally impossible",
                             *ROUTING["duplicate_id"]))
    pairs = [(r["subject"], r["ts"]) for r in rows]
    dup_pairs = len(pairs) - len(set(pairs))
    tolerance = profile.duplicate_subject_ts_tolerance_pct * max(len(rows), 1)
    if dup_pairs > tolerance:
        alerts.append(Alert("duplicate_subject_ts", "ticket", profile.name,
                             f"{dup_pairs} duplicate (subject, ts) pairs exceed the "
                             f"{profile.duplicate_subject_ts_tolerance_pct:.0%} tolerance band "
                             f"({tolerance:.2f} rows) -- may be legitimate resubmissions, needs review",
                             *ROUTING["duplicate_subject_ts"]))
    return alerts


def run_all_checks(db_path: str, profile: SystemProfile, now: datetime | None = None) -> list[Alert]:
    now = now or _now_utc()
    rows = load_rows(db_path)
    alerts: list[Alert] = []
    alerts += check_row_count(rows, profile, now)
    alerts += check_freshness(rows, profile, now)
    alerts += check_null_rate(rows, profile)
    alerts += check_duplicates(rows, profile)
    return alerts
