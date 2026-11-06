import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from monitor import SystemProfile, check_duplicates, check_freshness, check_null_rate, check_row_count


def _rows(n, start, system="sys", gap_minutes=60, subject_prefix="s"):
    out = []
    for i in range(n):
        ts = (start + timedelta(minutes=i * gap_minutes)).isoformat(timespec="microseconds")
        out.append({"id": f"id{i}", "system": system, "subject": f"{subject_prefix}{i}", "ts": ts})
    return out


def test_row_count_clear_when_above_floor():
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    profile = SystemProfile("sys", baseline_per_day=10, freshness_sla_hours=24)
    rows = _rows(10, now - timedelta(hours=20), gap_minutes=60)
    assert check_row_count(rows, profile, now) == []


def test_row_count_fires_on_sharp_drop():
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    profile = SystemProfile("sys", baseline_per_day=100, freshness_sla_hours=24)
    rows = _rows(1, now - timedelta(hours=1))
    alerts = check_row_count(rows, profile, now)
    assert len(alerts) == 1 and alerts[0].metric == "row_count"


def test_row_count_no_upper_bound_on_spike():
    """A volume spike must never alert -- it's success, not a defect."""
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    profile = SystemProfile("sys", baseline_per_day=10, freshness_sla_hours=24)
    rows = _rows(500, now - timedelta(hours=1), gap_minutes=0.1)
    assert check_row_count(rows, profile, now) == []


def test_freshness_clear_when_recent():
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    profile = SystemProfile("sys", baseline_per_day=10, freshness_sla_hours=24)
    rows = _rows(3, now - timedelta(hours=2))
    assert check_freshness(rows, profile, now) == []


def test_freshness_fires_when_stale():
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    profile = SystemProfile("sys", baseline_per_day=10, freshness_sla_hours=24)
    rows = _rows(3, now - timedelta(hours=100), gap_minutes=1)
    alerts = check_freshness(rows, profile, now)
    assert len(alerts) == 1 and alerts[0].metric == "freshness"


def test_freshness_fires_when_no_rows_at_all():
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    profile = SystemProfile("sys", baseline_per_day=10, freshness_sla_hours=24)
    assert len(check_freshness([], profile, now)) == 1


def test_null_rate_zero_tolerance():
    profile = SystemProfile("sys", baseline_per_day=10, freshness_sla_hours=24)
    clean = [{"id": "a", "system": "s", "subject": "x", "ts": "2026-01-01T00:00:00.000000+00:00"}]
    assert check_null_rate(clean, profile) == []
    dirty = [{"id": "a", "system": "s", "subject": "", "ts": "2026-01-01T00:00:00.000000+00:00"}]
    alerts = check_null_rate(dirty, profile)
    assert len(alerts) == 1 and alerts[0].metric == "null_rate"


def test_duplicate_id_always_flagged():
    profile = SystemProfile("sys", baseline_per_day=10, freshness_sla_hours=24)
    rows = [{"id": "dup", "system": "s", "subject": "a", "ts": "t1"},
            {"id": "dup", "system": "s", "subject": "b", "ts": "t2"}]
    alerts = check_duplicates(rows, profile)
    assert any(a.metric == "duplicate_id" for a in alerts)


def test_duplicate_subject_ts_within_tolerance_is_not_flagged():
    """One resubmission among 1000 rows is within the 1% tolerance band --
    must not page, per the uniqueness-vs-accuracy independence lesson:
    a duplicate isn't automatically a bug."""
    profile = SystemProfile("sys", baseline_per_day=10, freshness_sla_hours=24,
                             duplicate_subject_ts_tolerance_pct=0.01)
    rows = [{"id": f"id{i}", "system": "s", "subject": f"subj{i}", "ts": f"t{i}"} for i in range(998)]
    rows.append({"id": "dup_a", "system": "s", "subject": "subjX", "ts": "tX"})
    rows.append({"id": "dup_b", "system": "s", "subject": "subjX", "ts": "tX"})
    alerts = check_duplicates(rows, profile)
    assert not any(a.metric == "duplicate_subject_ts" for a in alerts)


def test_duplicate_subject_ts_beyond_tolerance_is_flagged():
    profile = SystemProfile("sys", baseline_per_day=10, freshness_sla_hours=24,
                             duplicate_subject_ts_tolerance_pct=0.01)
    rows = [{"id": "a", "system": "s", "subject": "x", "ts": "t1"},
            {"id": "b", "system": "s", "subject": "x", "ts": "t1"}]
    alerts = check_duplicates(rows, profile)
    assert any(a.metric == "duplicate_subject_ts" for a in alerts)


def test_run_all_checks_against_a_real_sqlite_db(tmp_path):
    db_path = str(tmp_path / "t.db")
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE decisions (id TEXT PRIMARY KEY, system TEXT, subject TEXT, ts TEXT)")
    now = datetime.now(timezone.utc)
    con.execute("INSERT INTO decisions VALUES (?,?,?,?)",
                ("id1", "s", "subj1", now.isoformat(timespec="microseconds")))
    con.commit()
    con.close()
    from monitor import run_all_checks
    profile = SystemProfile("s", baseline_per_day=1, freshness_sla_hours=24)
    assert run_all_checks(db_path, profile, now=now) == []
