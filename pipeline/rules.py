"""Usage-stage rules for the WASH Country Module review.

Shared by build_status_workbook.py and the live page pipeline
(Global-WASH-Cluster/activityinfo-wash-usage, pipeline/rules.py): keep the two
copies identical.
"""
from __future__ import annotations

# Usage-stage thresholds. One place, echoed in the Column guide.
RULES = {
    "piloting_min_rows": 1,
    "active_min_orgs": 5,
    "active_min_periods": 2,
    "active_min_rows": 100,
    "active_recent_days": 60,
    "established_min_orgs": 10,
    "established_min_share_targeted_reported": 0.5,
    "established_min_target_unit_coverage": 0.25,
}
STAGE_ORDER = ["Established", "Active", "Piloting", "Configured",
               "Log frame only", "Not set up"]


def deepest(geo: dict):
    lv = [int(k[-1]) for k, v in geo["admin"].items() if v["units"]]
    return max(lv) if lv else None


def target_unit_coverage(m: dict):
    """Share of the admin units that carry cluster targets (at the deepest
    target level) which also appear in partner monitoring at that level."""
    t = deepest(m["cluster_planning"]["geo"])
    if not t:
        return None, None
    tg = m["cluster_planning"]["geo"]["admin"][f"admin{t}"]["units"]
    rp = m["partner_monitoring"]["geo"]["admin"].get(f"admin{t}", {}).get("units", 0)
    return t, (rp / tg if tg else None)


def placeholder_targets(m: dict) -> bool:
    """All WASH targets identical (e.g. South Sudan: 47 indicators, all 34) =
    placeholder values, not a real plan."""
    t = [x["cluster_target"] for x in m["wash_indicator_detail"]
         if x["cluster_target"] is not None]
    return len(t) >= 5 and len(set(t)) == 1


def stage(m: dict) -> str:
    ind, cp, pm = m["indicators"], m["cluster_planning"], m["partner_monitoring"]
    if ind["loaded_wash"] == 0:
        return "Not set up"
    if pm["disagg_rows_wash"] < RULES["piloting_min_rows"]:
        return "Configured" if cp["indicators_with_target"] else "Log frame only"
    recent = pm["entry"][f"rows_edited_{RULES['active_recent_days']}d"]
    active = (pm["distinct_reporting_orgs"] >= RULES["active_min_orgs"]
              and len(pm["periods"]) >= RULES["active_min_periods"]
              and pm["disagg_rows_wash"] >= RULES["active_min_rows"]
              and recent > 0)
    if not active:
        return "Piloting"
    share = (pm["indicators_reported_with_target"] / cp["indicators_with_target"]
             if cp["indicators_with_target"] else 0)
    _, cov = target_unit_coverage(m)
    if (pm["distinct_reporting_orgs"] >= RULES["established_min_orgs"]
            and share >= RULES["established_min_share_targeted_reported"]
            and (cov is None or cov >= RULES["established_min_target_unit_coverage"])):
        return "Established"
    return "Active"
