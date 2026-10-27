"""Score one HPC Country Module against the WASH status template.

Label-based so the same run works on any country module: resolves forms by
their template label, finds the WASH coordination entity (coded WSH in every
module seen), pulls each form with a short column list and filters to WASH
client-side. One unreadable form yields a partial result, not a crash.

Usage: ai_analyze.py <databaseId> <outdir>
Writes <outdir>/metrics.json and prints it.
`analyze(client, db_id)` is importable for the roll-out driver.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

from ai_client import Client

# template labels the analysis reads. CORE forms carry signal we depend on;
# absence of an OPTIONAL form (mostly the barely-used *_Totals forms) is normal.
CORE_FORMS = [
    "1.1_Coordination_Entities",
    "1.2_Logframe_Entities",
    "1.2A_Logframe_Relationships",
    "1.3_Indicators",
    "2.2_Projects",
    "3.1_Indicator_Planning_Totals",
    "3.1A_Indicator_Planning_Disaggregation",
    "5.1A_Project_Indicator_Monitoring_Disaggregation",
    "6.1_Indicator_Monitoring_Totals",
    "6.1A_Indicator_Monitoring_Disaggregation",
]
OPTIONAL_FORMS = [
    "1.4_Caseloads",
    "4.1_Project_Indicator_Planning_Totals",
    "4.1A_Project_Indicator_Planning_Disaggregation",
    "5.1_Project_Indicator_Monitoring_Totals",
]
FORMS = CORE_FORMS + OPTIONAL_FORMS

# numeric prefixes of the standard template forms (anything else = a country add-on)
STD_PREFIXES = {
    "1.1", "1.1A", "1.2", "1.2A", "1.3", "1.4", "1.5",
    "2.1", "2.1A", "2.2", "2.2A",
    "3.1", "3.1A", "3.2", "3.2A", "3.3",
    "4.1", "4.1A", "4.2", "4.2A", "4.3",
    "5.1", "5.1A", "5.2", "5.2A",
    "6.1", "6.1A", "6.2", "6.2A",
}

WASH_CODES = {"WSH", "WASH", "EHA", "ASH"}
WASH_NAME_HINTS = ("WASH", "AGUA", "SANEAMIENTO", "EAU", "ASSAINISSEMENT",
                   "WATER AND SANITATION", "WATER, ")


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def positive(v):
    n = num(v)
    return n is not None and n > 0


def resolve_name(name, reflabel, ccode):
    """NAME (English) is blank in French/Spanish modules; REFLABEL is
    "CCODE - <name in the module's language>" and is always populated."""
    if name and name.strip():
        return name
    reflabel, ccode = reflabel or "", ccode or ""
    if ccode and reflabel.startswith(f"{ccode} - "):
        return reflabel[len(ccode) + 3:]
    return reflabel


def admin_level(code: str):
    """DISAG_ADMIN3 -> 3; site fields -> None."""
    m = re.fullmatch(r"DISAG_ADMIN(\d)", code)
    return int(m.group(1)) if m else None


def required_admin(dm_name):
    """Required admin level encoded in the disaggregation-model name, e.g.
    'ADM2-PPL' -> 2, 'Admin3 reqd, Admin4/5 opt' -> 3, 'Location only: Admin2' -> 2.
    The first admin token is the required one; later ones are optional."""
    m = re.search(r"AD(?:MIN|M)\s?(\d)", (dm_name or "").upper())
    return int(m.group(1)) if m else None


def requires_agesex(dm_name):
    d = dm_name or ""
    return "Age/Sex" in d or bool(re.search(r"(^|-)AS(-|$|\))|\(AS", d))


def iso_date(ts):
    return (datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
            if ts else None)


class Table:
    def __init__(self, rows):
        self.rows = list(rows)

    def __len__(self):
        return len(self.rows)

    def where(self, pred):
        return Table([r for r in self.rows if pred(r)])

    def distinct(self, k):
        return {r.get(k) for r in self.rows if r.get(k) not in (None, "")}


def analyze(c: Client, db_id: str) -> dict:
    db = c.database(db_id)
    resources = db.get("resources", [])
    label2id = {r["label"]: r["id"] for r in resources if r.get("type") == "FORM"}
    all_labels = {r["label"] for r in resources}
    missing_core = [lbl for lbl in CORE_FORMS if lbl not in label2id]
    missing_optional = [lbl for lbl in OPTIONAL_FORMS if lbl not in label2id]
    errors: list[str] = []

    def pull(label: str, cols: dict[str, str]) -> Table:
        fid = label2id.get(label)
        if not fid:
            return Table([])
        try:
            d = c.query(fid, cols)
        except (requests.HTTPError, requests.ReadTimeout) as e:
            errors.append(f"{label}: {type(e).__name__} {getattr(e, 'response', '')}")
            return Table([])
        n = d.get("rows", 0)
        C = d.get("columns", {})
        return Table(
            {k: C.get(k, {}).get("values", [None] * n)[i] for k in cols}
            for i in range(n)
        )

    def geo_fields(label: str) -> list[str]:
        """DISAG_ADMIN* / DISAG_SITE* codes present on a form. The set varies by
        module (Myanmar skips Admin 2; site fields differ), so read the schema."""
        fid = label2id.get(label)
        if not fid:
            return []
        try:
            els = c.form_schema(fid).get("elements", [])
        except requests.HTTPError as e:
            errors.append(f"{label} schema: {e}")
            return []
        return [e["code"] for e in els
                if (e.get("code") or "").startswith(("DISAG_ADMIN", "DISAG_SITE"))
                and e.get("type") == "reference"]

    # total units per admin level configured in the module (coverage denominator)
    admin_units_total = {}
    for lbl, fid in label2id.items():
        mm = re.fullmatch(r"Operation_Location_Admin(\d)", lbl)
        if mm and mm.group(1) != "0":
            try:
                admin_units_total[f"admin{mm.group(1)}"] = c.count(fid)
            except requests.HTTPError as e:
                errors.append(f"{lbl}: {e}")

    def geo_profile(t: Table, fields: list[str]) -> dict:
        """Rows, distinct units and % coverage per admin level, plus site use and
        the deepest admin level each row carries."""
        out = {"admin": {}, "sites": {}}
        for f in fields:
            lvl = admin_level(f)
            used = [r[f] for r in t.rows if r.get(f)]
            if lvl:
                key = f"admin{lvl}"
                tot = admin_units_total.get(key)
                units = len(set(used))
                out["admin"][key] = {
                    "rows": len(used), "units": units, "units_total": tot,
                    "coverage_pct": round(100 * units / tot, 1) if tot else None,
                }
            else:
                out["sites"][f.replace("DISAG_", "")] = len(used)
        deepest = Counter()
        for r in t.rows:
            lv = max((admin_level(f) for f in fields
                      if admin_level(f) and r.get(f)), default=None)
            deepest[f"admin{lv}" if lv else "none"] += 1
        out["rows_by_deepest_admin"] = dict(deepest)
        return out

    # our token's read scope: are planning / monitoring / project forms VIEW-filtered?
    scoped = sorted({
        next((r["label"] for r in resources if r["id"] == g["resourceId"]), g["resourceId"])
        for g in db.get("grants", [])
        for op in g.get("operations", [])
        if op.get("operation") == "VIEW" and op.get("filter")
    })

    # period label -> start date, for "latest period reported"
    periods = {r["n"]: (r["s"] or "")
               for r in pull("Operation_Time_Periods", {"n": "NAME", "s": "STARTDATE"}).rows
               if r["n"]}

    def latest(labels):
        got = [p for p in labels if p in periods]
        return max(got, key=lambda p: periods[p]) if got else None

    # --- WASH coordination entity ---
    ce = pull("1.1_Coordination_Entities",
              {"id": "_id", "name": "NAME", "code": "REFCODE", "cdl": "CDL.NAME"})
    clusters = ce.where(lambda r: r.get("cdl") == "Cluster")
    wash = next((r for r in ce.rows if (r.get("code") or "").upper() in WASH_CODES), None)
    if not wash:
        wash = next((r for r in ce.rows
                     if any(h in (r.get("name") or "").upper() for h in WASH_NAME_HINTS)),
                    None)
    if not wash:
        raise SystemExit(
            f"No WASH coordination entity in {db['label']}. "
            f"Entities: {[(r.get('code'), r.get('name')) for r in ce.rows]}")
    WSH = wash["id"]

    notes: list[str] = []

    def wash_filter(t: Table, key: str, glob_key: str | None = None) -> Table:
        f = t.where(lambda r: r.get(key) == WSH)
        if len(t) and len(f) == len(t):
            msg = f"{key}: all {len(t)} rows resolve to WASH"
            if glob_key:
                gw = sum(1 for r in t.rows
                         if (r.get(glob_key) or "").startswith("GSCWSH"))
                msg += f"; global-sector WASH on {gw}/{len(t)}"
            notes.append(msg)
        return f

    m: dict = {
        "database_id": db_id,
        "database_label": db["label"],
        "country": (db["label"].split(":")[-1].strip().rsplit(" ", 1)[0]
                    if ":" in db["label"] else db["label"]),
        "access_scope": "cluster-scoped" if scoped else "unfiltered",
        "view_filtered_forms": scoped,
        "missing_core_forms": missing_core,
        "missing_optional_forms": missing_optional,
        "extra_numbered_forms": sorted(
            f for f in all_labels
            if f[:2] in ("1.", "2.", "3.", "4.", "5.", "6.")
            and f.split("_")[0] not in STD_PREFIXES),
        "admin2_configured": "Operation_Location_Admin2" in all_labels,
        "admin_units_total": admin_units_total,
        "read_date": datetime.now(timezone.utc).date().isoformat(),
        "wash_entity": {"id": WSH, "name": wash["name"], "code": wash["code"]},
        "n_clusters_visible": len(clusters),
    }

    # --- 1.2 Logframe entities (WASH) ---
    lfe = pull("1.2_Logframe_Entities",
               {"id": "_id", "cde": "CDE", "level": "LFL.NAME", "name": "NAME"})
    wlfe = wash_filter(lfe, "cde")
    m["logframe"] = {
        "total_all_clusters": len(lfe),
        "wash_total": len(wlfe),
        "wash_by_level": dict(Counter(r["level"] for r in wlfe.rows)),
    }

    # --- 1.2A Logframe Relationships: parent chain by composite code, for
    # walking each indicator's activity up to its strategic objective ---
    rel = pull("1.2A_Logframe_Relationships",
               {"child": "CHILD_LFE.CCODE", "pcode": "PARENT_LFE.CCODE",
                "pname": "PARENT_LFE.NAME", "preflabel": "PARENT_LFE.REFLABEL",
                "plevel": "PARENT_LFE.LFL.NAME"})
    parent_of = {
        r["child"]: {"ccode": r["pcode"],
                      "name": resolve_name(r["pname"], r["preflabel"], r["pcode"]),
                      "level": r["plevel"]}
        for r in rel.rows if r.get("child")
    }

    def strategic_objective(ccode):
        """Walk the parent chain from an activity's ccode up to its SO."""
        seen = set()
        cur = ccode
        for _ in range(6):
            if not cur or cur in seen:
                return None, None
            seen.add(cur)
            p = parent_of.get(cur)
            if not p:
                return None, None
            if p["level"] == "Strategic Objective":
                return p["ccode"], p["name"]
            cur = p["ccode"]
        return None, None

    # --- 1.3 Indicators (WASH) ---
    ind = pull("1.3_Indicators",
               {"id": "_id", "cde": "CDE", "name": "NAME", "ccode": "CCODE",
                "reflabel": "REFLABEL", "unit": "UNIT.NAME", "dm": "DM.NAME",
                "mm": "MM.NAME", "pm": "PM.NAME", "glob": "SEG_GLOBCDE_MAP",
                "lfe_ccode": "LFE.CCODE", "lfe_name": "LFE.NAME",
                "lfe_reflabel": "LFE.REFLABEL", "lfe_level": "LFE.LFL.NAME"})
    # NAME is the English name and is blank in French/Spanish modules; REFLABEL
    # is "CCODE - <name in the module's language>" and is always populated.
    for r in ind.rows:
        r["name"] = resolve_name(r["name"], r["reflabel"], r["ccode"])
        r["lfe_name"] = resolve_name(r["lfe_name"], r["lfe_reflabel"], r["lfe_ccode"])
    wind = wash_filter(ind, "cde", "glob")
    m["wash_entity"]["confirmed_by_global_sector"] = bool(
        wind.rows) and all((r["glob"] or "").startswith("GSCWSH") for r in wind.rows)
    m["indicators"] = {
        "loaded_all_clusters": len(ind),
        "loaded_wash": len(wind),
        "wash_units": dict(Counter(r["unit"] for r in wind.rows)),
        "wash_with_disagg_model": sum(1 for r in wind.rows if r["dm"]),
        "wash_with_metric_model": sum(1 for r in wind.rows if r["mm"]),
        "wash_with_period_model": sum(1 for r in wind.rows if r["pm"]),
    }
    ind_ccode = {r["id"]: r["ccode"] for r in wind.rows}
    ind_name = {r["id"]: r["name"] for r in wind.rows}
    ind_unit = {r["id"]: r["unit"] for r in wind.rows}
    ind_dm = {r["id"]: r["dm"] for r in wind.rows}
    # activity = the indicator's own logframe entity, if it sits at activity
    # level; strategic objective = walked up the 1.2A parent chain from there.
    ind_activity = {}
    ind_so = {}
    for r in wind.rows:
        act_ccode = r["lfe_ccode"] if "Activity" in (r["lfe_level"] or "") else None
        act_name = r["lfe_name"] if act_ccode else None
        ind_activity[r["id"]] = (act_ccode, act_name)
        ind_so[r["id"]] = strategic_objective(r["lfe_ccode"])

    # --- 1.4 Caseloads (WASH) ---
    csl = pull("1.4_Caseloads", {"id": "_id", "cde": "CDE", "name": "NAME"})
    m["caseloads"] = {"wash_total": len(wash_filter(csl, "cde"))}

    # --- 2.2 Projects tagged WASH ---
    proj = pull("2.2_Projects", {"id": "_id", "cdes": "CDE_CCODES"})
    wproj = proj.where(lambda r: "WSH" in (r.get("cdes") or ""))
    m["projects"] = {"total": len(proj), "wash_tagged": len(wproj)}

    # --- 3.1 / 3.1A cluster planning (targets) ---
    w31 = wash_filter(pull("3.1_Indicator_Planning_Totals",
                           {"ind": "IND", "cde": "IND.CDE", "tgt": "AMOUNT_TGI"}), "cde")
    g31a = geo_fields("3.1A_Indicator_Planning_Disaggregation")
    w31a = wash_filter(pull("3.1A_Indicator_Planning_Disaggregation",
                            {"ind": "IND", "cde": "IND.CDE", "tgt": "AMOUNT_TGI",
                             "agesex": "DISAG_AGE_SEX", "pop": "DISAG_POPTYPE",
                             "disab": "DISAG_DISAB", **{f: f for f in g31a}}), "cde")
    tgt_by_ind = {r["ind"]: r["tgt"] for r in w31.rows if positive(r["tgt"])}
    geo31 = geo_profile(w31a, g31a)
    m["cluster_planning"] = {
        "totals_rows_wash": len(w31),
        "indicators_with_target": len(tgt_by_ind),
        "disagg_rows_wash": len(w31a),
        "geo_fields_available": g31a,
        "geo": geo31,
        "disagg_agesex_used": sum(1 for r in w31a.rows if r["agesex"]),
        "disagg_poptype_used": sum(1 for r in w31a.rows if r["pop"]),
        "disagg_disability_used": sum(1 for r in w31a.rows if r["disab"]),
    }

    # --- 4.1 / 4.1A partner planning ---
    part_plan = sum(
        len(pull(lbl, {"cde": "IND.CDE"}).where(lambda r: r.get("cde") == WSH))
        for lbl in ("4.1_Project_Indicator_Planning_Totals",
                    "4.1A_Project_Indicator_Planning_Disaggregation")
    )
    m["partner_planning"] = {"wash_rows_total": part_plan}

    # --- 5.1 / 5.1A partner monitoring (the 3W) ---
    mon_tot = pull("5.1_Project_Indicator_Monitoring_Totals",
                   {"ind": "IND", "cde": "IND.CDE"})
    g51 = geo_fields("5.1A_Project_Indicator_Monitoring_Disaggregation")
    m51 = pull("5.1A_Project_Indicator_Monitoring_Disaggregation",
               {"proj": "PROJECT", "ind": "IND", "cde": "IND.CDE", "indname": "IND.NAME",
                "iporg": "SEG_IPORG_MAN.NAME", "leadorg": "PROJECT.LEADORG.NAME",
                "period": "SEG_PERIOD_MAN.NAME", "edited": "_lastEditTime",
                "mea": "AMOUNT_MEA", "cme": "AMOUNT_CME", "tgt": "AMOUNT_XJTG",
                "agesex": "DISAG_AGE_SEX", "pop": "DISAG_POPTYPE", "disab": "DISAG_DISAB",
                **{f: f for f in g51}})
    w51 = wash_filter(m51, "cde")
    for r in w51.rows:
        r["org"] = r.get("iporg") or r.get("leadorg")
        r["deepest"] = max((admin_level(f) for f in g51
                            if admin_level(f) and r.get(f)), default=None)
    geo51 = geo_profile(w51, g51)

    # entry activity: _lastEditTime is the last edit, not creation, so a re-edited
    # row moves forward; still the best available signal of when data went in.
    now = datetime.now(timezone.utc).timestamp()
    edits = [num(r["edited"]) for r in w51.rows if num(r["edited"])]
    entry = {
        "first_edit": iso_date(min(edits)) if edits else None,
        "last_edit": iso_date(max(edits)) if edits else None,
        "rows_edited_30d": sum(1 for t in edits if now - t <= 30 * 86400),
        "rows_edited_60d": sum(1 for t in edits if now - t <= 60 * 86400),
        "rows_edited_90d": sum(1 for t in edits if now - t <= 90 * 86400),
        "rows_by_edit_month": dict(sorted(Counter(
            iso_date(t)[:7] for t in edits).items())),
        "distinct_edit_days": len({iso_date(t) for t in edits}),
    }
    m["partner_monitoring"] = {
        "totals_rows_wash": len(mon_tot.where(lambda r: r.get("cde") == WSH)),
        "disagg_rows_all_clusters": len(m51),
        "disagg_rows_wash": len(w51),
        "disagg_rows_indicator_unresolved": len(
            m51.where(lambda r: r.get("ind") and not r.get("indname"))),
        "distinct_projects": len(w51.distinct("proj")),
        "distinct_implementing_partners": len(w51.distinct("iporg")),
        "distinct_reporting_orgs": len(w51.distinct("org")),
        "distinct_indicators_reported": len(w51.distinct("ind")),
        "indicators_reported_with_target": len(w51.distinct("ind") & set(tgt_by_ind)),
        "periods": sorted(w51.distinct("period"), key=lambda p: periods.get(p, p)),
        "latest_period": latest(w51.distinct("period")),
        "rows_by_period": dict(sorted(Counter(r["period"] for r in w51.rows
                                              if r["period"]).items(),
                                      key=lambda kv: periods.get(kv[0], kv[0]))),
        "rows_with_periodic_measure": sum(1 for r in w51.rows if positive(r["mea"])),
        "rows_with_cumulative_measure": sum(1 for r in w51.rows if positive(r["cme"])),
        "geo_fields_available": g51,
        "geo": geo51,
        "entry": entry,
        "rows_with_agesex": sum(1 for r in w51.rows if r["agesex"]),
        "rows_with_poptype": sum(1 for r in w51.rows if r["pop"]),
        "rows_with_disability": sum(1 for r in w51.rows if r["disab"]),
    }

    # --- 6.1 / 6.1A cluster monitoring ---
    w61 = pull("6.1_Indicator_Monitoring_Totals",
               {"ind": "IND", "cde": "IND.CDE"}).where(lambda r: r.get("cde") == WSH)
    g61a = geo_fields("6.1A_Indicator_Monitoring_Disaggregation")
    c61a = pull("6.1A_Indicator_Monitoring_Disaggregation",
                {"ind": "IND", "cde": "IND.CDE", "period": "SEG_PERIOD_MAN.NAME",
                 "mea": "AMOUNT_MEA", "cme": "AMOUNT_CME", **{f: f for f in g61a}})
    w61a = wash_filter(c61a, "cde")
    m["cluster_monitoring"] = {
        "totals_rows_wash": len(w61),
        "disagg_rows_wash": len(w61a),
        "geo_fields_available": g61a,
        "geo": geo_profile(w61a, g61a),
        "periods": sorted(w61a.distinct("period"), key=lambda p: periods.get(p, p)),
        "latest_period": latest(w61a.distinct("period")),
        "rows_with_periodic_measure": sum(1 for r in w61a.rows if positive(r["mea"])),
        "rows_with_cumulative_measure": sum(1 for r in w61a.rows if positive(r["cme"])),
    }

    # --- per-indicator detail (for the workbook detail sheet) ---
    rows_51 = Counter(r["ind"] for r in w51.rows)
    rows_61 = Counter(r["ind"] for r in w61a.rows)

    def cumulative_by_indicator(table: Table) -> dict:
        """Sum the cumulative measure per indicator, restricted to that
        indicator's latest reporting period (cumulative is year-to-date, so
        summing across periods would double-count; summing the disaggregation
        cells within one period is valid)."""
        by_ind: dict = {}
        for r in table.rows:
            by_ind.setdefault(r["ind"], []).append(r)
        out = {}
        for iid, rs in by_ind.items():
            lp = latest({r["period"] for r in rs if r["period"]})
            sel = [r for r in rs if r["period"] == lp] if lp else rs
            out[iid] = sum(num(r["cme"]) or 0 for r in sel)
        return out

    partner_cume = cumulative_by_indicator(w51)
    cluster_cume = cumulative_by_indicator(w61a)

    def cumulative_by_project(table: Table) -> dict:
        """Partner stream variant: each project's own latest period, summed
        across projects, so a partner that has not yet reported the module's
        latest period still counts with its last year-to-date figure."""
        by: dict = {}
        for r in table.rows:
            by.setdefault((r["ind"], r["proj"]), []).append(r)
        out: Counter = Counter()
        for (iid, _), rs in by.items():
            lp = latest({r["period"] for r in rs if r["period"]})
            sel = [r for r in rs if r["period"] == lp] if lp else rs
            out[iid] += sum(num(r["cme"]) or 0 for r in sel)
        return dict(out)

    partner_cume_proj = cumulative_by_project(w51)
    by_ind51: dict = {}
    for r in w51.rows:
        by_ind51.setdefault(r["ind"], []).append(r)

    detail = []
    for iid in sorted(ind_ccode, key=lambda i: ind_ccode[i] or ""):
        act_ccode, act_name = ind_activity[iid]
        so_ccode, so_name = ind_so[iid]
        detail.append({
            "activity_code": act_ccode,
            "activity_name": act_name,
            "so_code": so_ccode,
            "so_name": so_name,
            "ccode": ind_ccode[iid],
            "unit": ind_unit[iid],
            "required_disaggregation": ind_dm[iid],
            "cluster_target": tgt_by_ind.get(iid),
            "required_admin": required_admin(ind_dm[iid]),
            "requires_agesex": requires_agesex(ind_dm[iid]),
            "rows_5_1A": rows_51.get(iid, 0),
            "orgs_5_1A": len({r["org"] for r in by_ind51.get(iid, []) if r["org"]}),
            "deepest_admin_5_1A": max((r["deepest"] for r in by_ind51.get(iid, [])
                                       if r["deepest"]), default=None),
            "rows_at_required_admin": (sum(
                1 for r in by_ind51.get(iid, [])
                if r["deepest"] and r["deepest"] >= required_admin(ind_dm[iid]))
                if required_admin(ind_dm[iid]) else None),
            "rows_with_agesex": sum(1 for r in by_ind51.get(iid, []) if r["agesex"]),
            "partner_cumulative": partner_cume.get(iid, 0) or None,
            "partner_cumulative_by_project": partner_cume_proj.get(iid, 0) or None,
            "rows_6_1A": rows_61.get(iid, 0),
            "cluster_cumulative": cluster_cume.get(iid, 0) or None,
            "name": ind_name[iid],
        })
    m["wash_indicator_detail"] = detail

    # disaggregation compliance, over rows of indicators whose model asks for it
    req = [x for x in detail if x["required_admin"] and x["rows_5_1A"]]
    agx = [x for x in detail if x["requires_agesex"] and x["rows_5_1A"]]
    m["partner_monitoring"]["compliance"] = {
        "rows_needing_admin": sum(x["rows_5_1A"] for x in req),
        "rows_at_required_admin": sum(x["rows_at_required_admin"] for x in req),
        "rows_needing_agesex": sum(x["rows_5_1A"] for x in agx),
        "rows_with_agesex_where_required": sum(x["rows_with_agesex"] for x in agx),
        "required_admin_levels": dict(Counter(
            x["required_admin"] for x in detail if x["required_admin"])),
    }

    m["filter_notes"] = notes
    m["query_errors"] = errors
    return m


def main() -> None:
    db_id, outdir = sys.argv[1], Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)
    m = analyze(Client(), db_id)
    (outdir / "metrics.json").write_text(json.dumps(m, indent=2))
    print(json.dumps(m, indent=2))


if __name__ == "__main__":
    main()
