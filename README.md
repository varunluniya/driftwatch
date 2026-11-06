# DriftWatch

A rule-based monitoring and alerting engine for a decisions/outcomes
pipeline — the kind a fleet of small, always-writing services shares: one
`decisions` table (id, system, subject, ts, features, output), continuously
appended to, not a nightly batch load.

DriftWatch runs four checks against that table, each with a per-system
baseline and a deliberately justified threshold rather than one global
number:

- **Row count** — a drop-only threshold. A volume spike is never flagged
  (it's success, not a defect); a sustained drop below the trailing
  baseline pages the on-call engineer.
- **Freshness** — each system gets its own SLA tied to its real traffic
  shape. A continuous, high-volume system gets a tight SLA (hours); a
  periodic batch/eval job gets a loose one — applying one system's SLA to
  the other would either miss real outages or false-page constantly.
- **Null rate** — zero-tolerance on the fields a shared write path is
  supposed to guarantee. Any null here points at a new code defect, not a
  volume artifact.
- **Duplicate count** — a duplicate primary-key id is always a bug
  (structurally supposed to be impossible). A duplicate `(subject, ts)`
  pair gets a small tolerance band instead of zero tolerance, because a
  legitimate resubmission can look identical to a double-write bug from
  the data alone — the two have to be told apart, not both rejected.

Every alert carries a severity, which team it routes to, and a first
diagnostic action — not just "something's wrong."

## Honest disclosure

This isn't a hypothetical demo dataset. `cli.py`'s first run points
DriftWatch at the real, live SQLite databases of eight already-shipped
services (when run from alongside their checkouts) and reports them
clean — because they currently are. The second run injects a deliberately
broken fixture database (stale, with a null field and a duplicate pair)
to prove every alert type actually fires when its specific condition is
really present, not just that the monitor says "all clear" by default.

## Run it

```
python3 cli.py
python3 -m pytest -q
```

## Files

- `monitor.py` — the four checks, the per-system profile, and the routing
  table.
- `cli.py` — the two-run demo described above.
- `tests/test_monitor.py` — 11 tests, including the no-upper-bound-on-a-
  spike case and the within-tolerance-vs-beyond-tolerance duplicate case.
