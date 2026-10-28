"""Build the public ActivityInfo WASH usage snapshot for the live page.

Runs ai_analyze over every HPC Country Module, reduces each result to a
whitelisted public record (no database ids, grants or per-indicator detail),
and writes:
  data/latest.enc.json               current snapshot + generated takeaways
  data/snapshots/YYYY-MM-DD.enc.json  dated copy
  data/history.enc.json               per-date, per-country rows / orgs / stage
All three are encrypted with SNAPSHOT_PASSWORD (see vault.py): the repo and
Pages site are public, so nothing is written in clear.

Token: ACTIVITYINFO_TOKEN env var (repo secret in CI). Never written anywhere.

Usage:
  build_snapshot.py                       live run against the API
  build_snapshot.py --from-metrics DIR --date YYYY-MM-DD
                                          seed from saved */metrics.json (no API)
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import vault
from rules import STAGE_ORDER, placeholder_targets, stage, target_unit_coverage

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PREFIX = "HPC.tools Country Module:"
MAX_FAILURES = 2


def pct(a, b):
    return round(100 * a / b, 1) if b else None


def public_record(m: dict) -> dict:
    """Field-by-field reduction of one module's metrics. Anything not named
    here never reaches the public page."""
    pm, cp = m["partner_monitoring"], m["cluster_planning"]
    rows = pm["disagg_rows_wash"]
    deep = {k: v for k, v in pm["geo"]["rows_by_deepest_admin"].items() if k != "none"}
    main = max(deep, key=deep.get) if deep else None
    t_lvl, cov = target_unit_coverage(m)
    req = pm["compliance"]["required_admin_levels"]
    return {
        "country": m["country"],
        "stage": stage(m),
        "rows": rows,
        "orgs": pm["distinct_reporting_orgs"],
        "projects_reporting": pm["distinct_projects"],
        "projects_configured": m["projects"]["wash_tagged"],
        "indicators_loaded": m["indicators"]["loaded_wash"],
        "indicators_targeted": cp["indicators_with_target"],
        "indicators_reported": pm["distinct_indicators_reported"],
        "targeted_reported": pm["indicators_reported_with_target"],
        "placeholder_targets": placeholder_targets(m),
        "periods": len(pm["periods"]),
        "latest_period": pm["latest_period"],
        "last_entry": pm["entry"]["last_edit"],
        "rows_30d": pm["entry"]["rows_edited_30d"],
        "required_admin": sorted(int(k) for k in req),
        "required_admin_main": (int(max(req, key=req.get)) if req else None),
        "reported_levels": sorted(int(k[-1]) for k, v in pm["geo"]["admin"].items()
                                  if v["units"]),
        "main_level": int(main[-1]) if main else None,
        "rows_below_admin2_pct": pct(sum(v for k, v in deep.items()
                                         if int(k[-1]) > 2), rows),
        "target_level": t_lvl,
        "target_units_covered_pct": pct(cov, 1) if cov is not None else None,
        "sites_used": any(pm["geo"]["sites"].values()),
        "agesex_pct": pct(pm["rows_with_agesex"], rows),
        "disability_pct": pct(pm["rows_with_disability"], rows),
        "cluster_rows": m["cluster_monitoring"]["disagg_rows_wash"],
    }


def names(xs):
    xs = list(xs)
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def fmt(n):
    return f"{n:,}"


def day(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d.day} {d.strftime('%b %Y')}"


def takeaways(countries: list[dict], prev: dict | None) -> list[str]:
    """Fixed sentence templates over the data, so the summary never goes stale."""
    out = []
    total = sum(c["rows"] for c in countries)
    with_data = [c for c in countries if c["rows"]]
    without = len(countries) - len(with_data)
    out.append(f"{fmt(total)} WASH partner 3W rows across {len(with_data)} modules; "
               f"{without} of {len(countries)} modules still have no partner data.")

    by = {s: [c["country"] for c in countries if c["stage"] == s] for s in STAGE_ORDER}
    parts = []
    for s, label in [("Established", "established"), ("Active", "active")]:
        if by[s]:
            parts.append(f"{names(by[s])} {'is' if len(by[s]) == 1 else 'are'} {label}")
    if by["Piloting"]:
        parts.append(f"{len(by['Piloting'])} countries are piloting")
    if by["Configured"]:
        parts.append(f"{len(by['Configured'])} are configured with no 3W data")
    if by["Log frame only"]:
        parts.append(f"{names(by['Log frame only'])} has a log frame but no targets")
    out.append("Usage stage: " + "; ".join(parts) + ".")

    deep = [c for c in with_data if c["rows"] >= 100 and (c["main_level"] or 0) > 2]
    if deep:
        detail = [f"{c['country']} ({c['rows_below_admin2_pct']:.0f}% of rows below Admin 2"
                  + (", although its models ask for Admin 2" if c["required_admin_main"] == 2
                     else "") + ")" for c in deep]
        out.append("Countries reporting at volume go below Admin 2: " + "; ".join(detail) + ".")

    recent = [c["country"] for c in with_data if c["rows_30d"]]
    out.append(f"{len(recent)} of {len(with_data)} reporting modules had 3W entries "
               f"in the last 30 days.")

    ph = [c["country"] for c in countries if c["placeholder_targets"]]
    if ph:
        out.append(f"Cluster targets in {names(ph)} are placeholders (every WASH "
                   f"target has the same value), so reach against target is not shown.")
    return out


LATEST = DATA / "latest.enc.json"
HISTORY = DATA / "history.enc.json"


def write_snapshot(countries: list[dict], date: str, generated_at: str,
                   failures: list[str]) -> None:
    countries.sort(key=lambda c: (STAGE_ORDER.index(c["stage"]), -c["rows"], c["country"]))
    history = vault.read(HISTORY, [])
    history = [h for h in history if h["date"] != date]
    prev = max((h for h in history if h["date"] < date), key=lambda h: h["date"],
               default=None)
    prev_rows = prev["countries"] if prev else {}
    for c in countries:
        p = prev_rows.get(c["country"])
        c["rows_change"] = c["rows"] - p["rows"] if p else None

    snap = {
        "date": date,
        "generated_at": generated_at,
        "previous_date": prev["date"] if prev else None,
        "modules": len(countries),
        "total_rows": sum(c["rows"] for c in countries),
        "failed_modules": failures,
        "takeaways": takeaways(countries, prev),
        "countries": countries,
    }
    history.append({
        "date": date,
        "total_rows": snap["total_rows"],
        "countries": {c["country"]: {"rows": c["rows"], "orgs": c["orgs"],
                                     "stage": c["stage"]} for c in countries},
    })
    history.sort(key=lambda h: h["date"])

    vault.write(LATEST, snap)
    vault.write(DATA / "snapshots" / f"{date}.enc.json", snap)
    vault.write(HISTORY, history)
    print(f"wrote {date}: {len(countries)} modules, {snap['total_rows']} WASH rows, "
          f"prev={snap['previous_date']}, failed={failures}")


def live() -> None:
    from ai_analyze import analyze
    from ai_client import Client

    c = Client()
    dbs = c.databases()
    items = dbs if isinstance(dbs, list) else dbs.get("databases", dbs)
    modules = sorted((d["label"], d["databaseId"]) for d in items
                     if d["label"].startswith(PREFIX))
    prev = {x["country"]: x for x in vault.read(LATEST, {}).get("countries", [])}
    countries, failures = [], []
    for label, db_id in modules:
        try:
            m = analyze(c, db_id)
            if m["query_errors"]:
                raise RuntimeError(f"{len(m['query_errors'])} query errors")
            countries.append(public_record(m))
            print(f"  ok   {m['country']}")
        except Exception as e:  # noqa: BLE001  (report and carry forward)
            name = label.split(":")[-1].strip().rsplit(" ", 1)[0]
            failures.append(name)
            print(f"  FAIL {name}: {type(e).__name__}")
            if name in prev:
                countries.append({**prev[name], "stale": True})
        time.sleep(0.3)
    if len(failures) > MAX_FAILURES:
        sys.exit(f"{len(failures)} modules failed ({failures}); keeping last snapshot")
    now = datetime.now(timezone.utc)
    write_snapshot(countries, now.date().isoformat(),
                   now.isoformat(timespec="minutes"), failures)


def seed(metrics_dir: Path, date: str) -> None:
    """Seed from saved metrics.json files. Older files (7 Sep) predate the
    geo/entry fields, so they only feed history, not a full snapshot."""
    ms = [json.loads(p.read_text()) for p in sorted(metrics_dir.glob("*/metrics.json"))]
    if all("entry" in m["partner_monitoring"] for m in ms):
        write_snapshot([public_record(m) for m in ms], date, f"{date}T00:00+00:00", [])
        return
    history = [h for h in vault.read(HISTORY, []) if h["date"] != date]
    history.append({
        "date": date,
        "total_rows": sum(m["partner_monitoring"]["disagg_rows_wash"] for m in ms),
        "countries": {m["country"]: {"rows": m["partner_monitoring"]["disagg_rows_wash"],
                                     "orgs": m["partner_monitoring"]["distinct_reporting_orgs"],
                                     "stage": None} for m in ms},
    })
    history.sort(key=lambda h: h["date"])
    vault.write(HISTORY, history)
    print(f"seeded history only for {date} ({len(ms)} modules)")


if __name__ == "__main__":
    if "--from-metrics" in sys.argv:
        seed(Path(sys.argv[sys.argv.index("--from-metrics") + 1]),
             sys.argv[sys.argv.index("--date") + 1])
    else:
        live()
