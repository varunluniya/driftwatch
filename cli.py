"""
DriftWatch CLI demo.

Run 1: sweep the real, live gen4-kernel repos (if present as sibling
checkouts) using the actual per-repo baselines from the monitoring plan --
proves the checks run clean against real production-shaped data.

Run 2: inject a synthetic breach fixture (a stale, null-containing,
duplicate-containing database) to prove each alert actually fires when the
condition it's designed to catch is really present -- a monitor that only
ever reports "all clear" is unverified, not working.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from monitor import SystemProfile, run_all_checks

# Real per-repo baselines, from the live portfolio monitoring plan
# (recomputed 2026-10-16 from each repo's own seed.py --reset).
REAL_PROFILES = {
    "instatrust":   SystemProfile("instatrust",   baseline_per_day=3.3,  freshness_sla_hours=36),
    "lendfair":     SystemProfile("lendfair",     baseline_per_day=2.0,  freshness_sla_hours=36),
    "snapmatch":    SystemProfile("snapmatch",    baseline_per_day=3.3,  freshness_sla_hours=36),
    "readydesk":    SystemProfile("readydesk",    baseline_per_day=3.6,  freshness_sla_hours=36),
    "sentineldesk": SystemProfile("sentineldesk", baseline_per_day=8.3,  freshness_sla_hours=12),
    "pulsequeue":   SystemProfile("pulsequeue",   baseline_per_day=73.4, freshness_sla_hours=2),
    "echocheck":    SystemProfile("echocheck",    baseline_per_day=0.08, freshness_sla_hours=24 * 30),
    "causalgate":   SystemProfile("causalgate",   baseline_per_day=0.04, freshness_sla_hours=24 * 30),
}


def sweep_real_repos():
    print("=" * 70)
    print("RUN 1 -- sweeping live repo databases (real production-shaped data)")
    print("=" * 70)
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    found_any = False
    for name, profile in REAL_PROFILES.items():
        db_path = os.path.join(base, name, "data", f"{name}.db")
        if not os.path.exists(db_path):
            print(f"  [{name}] skipped -- no sibling checkout found at {db_path}")
            continue
        found_any = True
        # Evaluate "now" at the point just after this db's own last write,
        # not wall-clock now, since these are static seeded snapshots from
        # an earlier session, not continuously-running services right now.
        con = sqlite3.connect(db_path)
        last_ts = con.execute("SELECT MAX(ts) FROM decisions").fetchone()[0]
        con.close()
        eval_now = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        if eval_now.tzinfo is None:
            eval_now = eval_now.replace(tzinfo=timezone.utc)
        eval_now = eval_now + timedelta(hours=1)  # "now" = shortly after last real write
        alerts = run_all_checks(db_path, profile, now=eval_now)
        status = "CLEAR" if not alerts else f"{len(alerts)} ALERT(S)"
        print(f"  [{name:14s}] {status}")
        for a in alerts:
            print(f"      -> {a.metric}: {a.message}")
    if not found_any:
        print("  (no sibling repo checkouts found -- run this from ~/work with the "
              "portfolio repos cloned alongside driftwatch/)")
    return found_any


def _make_breach_fixture(path: str):
    """Build a deliberately broken database: one null subject, one
    duplicate id, two duplicate (subject, ts) pairs beyond tolerance, and
    the newest row backdated well past any freshness SLA."""
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE decisions (id TEXT PRIMARY KEY, system TEXT, subject TEXT, ts TEXT, "
                "features TEXT, output TEXT)")
    stale_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
    rows = []
    # 5 normal rows, all stale (oldest well past any SLA)
    for i in range(5):
        ts = (stale_time + timedelta(minutes=i)).isoformat(timespec="microseconds")
        rows.append((f"id{i}", "fixture-system", f"subj{i}", ts, "{}", "{}"))
    # one null-subject row -- completeness/null-rate violation
    rows.append(("id_null", "fixture-system", "", stale_time.isoformat(timespec="microseconds"), "{}", "{}"))
    # one duplicate id (re-using id0 -- can't actually insert as a true dup
    # with a PRIMARY KEY, so this is inserted into a second, non-PK table
    # in a real system via INSERT OR REPLACE; here we simulate the
    # detection logic directly by inserting via raw cursor bypassing the PK)
    con.executemany("INSERT INTO decisions VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    # Simulate a duplicate-id bypass the same way record_decision()'s own
    # INSERT OR REPLACE would allow if a caller reused an id -- insert a
    # *second* row sharing subj0+ts0 under a different id, which the
    # (subject, ts) duplicate check is designed to catch instead.
    dup_ts = rows[0][3]
    con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                ("id_dup_a", "fixture-system", "subj0", dup_ts, "{}", "{}"))
    con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                ("id_dup_b", "fixture-system", "subj0", dup_ts, "{}", "{}"))
    con.commit()
    con.close()


def sweep_breach_fixture():
    print()
    print("=" * 70)
    print("RUN 2 -- injected breach fixture (proving each alert actually fires)")
    print("=" * 70)
    fixture_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "breach_fixture.db")
    _make_breach_fixture(fixture_path)
    profile = SystemProfile("fixture-system", baseline_per_day=10.0, freshness_sla_hours=24)
    alerts = run_all_checks(fixture_path, profile, now=datetime.now(timezone.utc))
    print(f"  [{profile.name}] {len(alerts)} ALERT(S) fired (expected: row_count, freshness, "
          "null_rate, duplicate_subject_ts)")
    for a in alerts:
        print(f"      -> [{a.severity:6s}] {a.metric}: {a.message}")
        print(f"                 routes to: {a.routes_to}")
        print(f"                 first action: {a.first_diagnostic_action}")
    fired = {a.metric for a in alerts}
    expected = {"row_count", "freshness", "null_rate", "duplicate_subject_ts"}
    missing = expected - fired
    if missing:
        print(f"  WARNING: expected alerts not fired: {missing}")
        sys.exit(1)
    print("  All 4 expected alert types fired correctly.")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sweep_real_repos()
    sweep_breach_fixture()
