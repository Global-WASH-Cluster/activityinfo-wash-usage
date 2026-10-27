(function () {
  "use strict";
  const STALE_DAYS = 3;
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => (n == null ? "–" : n.toLocaleString("en-GB"));
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const day = (iso) => (iso ? new Date(iso + (iso.length === 10 ? "T00:00:00Z" : "")).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }) : "–");
  const chip = (s) => `<span class="chip ${esc(s.split(" ")[0])}">${esc(s)}</span>`;
  const pct = (v) => (v == null ? "–" : `${Math.round(v)}%`);

  function spark(series) {
    const pts = series.filter((p) => p.rows != null);
    if (pts.length < 2) return "";
    const w = 84, h = 22, pad = 3;
    const max = Math.max(...pts.map((p) => p.rows), 1);
    const x = (i) => pad + (i * (w - 2 * pad)) / (pts.length - 1);
    const y = (v) => h - pad - (v / max) * (h - 2 * pad);
    const d = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.rows).toFixed(1)}`).join("");
    const last = pts[pts.length - 1];
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Rows over time: ${pts.map((p) => fmt(p.rows)).join(", ")}">` +
      `<path d="${d}" fill="none" stroke="var(--accent)" stroke-width="1.6" stroke-linejoin="round"/>` +
      `<circle cx="${x(pts.length - 1).toFixed(1)}" cy="${y(last.rows).toFixed(1)}" r="2.4" fill="var(--accent)"/></svg>`;
  }

  function render(snap, history) {
    const gen = new Date(snap.generated_at);
    $("asof").textContent = `Data as of ${day(snap.date)} · refreshed daily from ActivityInfo · ${snap.modules} country modules`;
    const ageDays = (Date.now() - gen.getTime()) / 86400000;
    if (ageDays > STALE_DAYS) {
      $("stale").hidden = false;
      $("stale").textContent = `This snapshot is ${Math.floor(ageDays)} days old. The daily refresh has not run since ${day(snap.date)}; figures may be out of date.`;
    } else if (snap.failed_modules && snap.failed_modules.length) {
      $("stale").hidden = false;
      $("stale").textContent = `The last refresh could not read ${snap.failed_modules.join(", ")}; those rows repeat the previous snapshot.`;
    }

    $("takeaways").innerHTML = snap.takeaways.map((t) => `<li>${esc(t)}</li>`).join("");

    const C = snap.countries;
    const reporting = C.filter((c) => c.rows > 0).sort((a, b) => b.rows - a.rows);
    const prevDate = snap.previous_date;
    const prevSnap = prevDate && history.find((h) => h.date === prevDate);
    const prevRows = (c) => (prevSnap && prevSnap.countries[c.country] ? prevSnap.countries[c.country].rows : null);

    // growth bars
    const max = Math.max(...reporting.map((c) => Math.max(c.rows, prevRows(c) || 0)), 1);
    $("bars-legend").innerHTML = (prevSnap ? `<span><i style="background:var(--sep)"></i>${day(prevDate)}</span>` : "") +
      `<span><i style="background:var(--accent)"></i>${day(snap.date)}</span>`;
    $("bars").innerHTML = reporting.map((c) => {
      const p = prevRows(c);
      return `<div>${esc(c.country)}</div><div class="track" role="img" aria-label="${esc(c.country)}: ${p != null ? fmt(p) + " to " : ""}${fmt(c.rows)} rows">` +
        `<span class="now" style="width:${Math.max((c.rows / max) * 100, 0.4)}%"></span>` +
        (p != null ? `<span class="was" style="width:${Math.max((p / max) * 100, 0.4)}%"></span>` : "") +
        `</div><div class="val">${p != null ? fmt(p) + " → " : ""}<b>${fmt(c.rows)}</b></div>`;
    }).join("");

    // stage table: reporting countries individually, empty ones grouped by stage
    $("chg-hd").textContent = prevDate ? `Since ${day(prevDate)}` : "Change";
    const series = (name) => history.map((h) => ({ rows: h.countries[name] ? h.countries[name].rows : null }));
    let rows = reporting.slice().sort((a, b) => STAGE_RANK(a.stage) - STAGE_RANK(b.stage) || b.rows - a.rows).map((c) =>
      `<tr><td>${chip(c.stage)}</td><td>${esc(c.country)}${c.stale ? " *" : ""}</td><td class="num">${fmt(c.rows)}</td>` +
      `<td class="num">${c.rows_change == null ? "–" : (c.rows_change >= 0 ? "+" : "") + fmt(c.rows_change)}</td>` +
      `<td class="spark">${spark(series(c.country))}</td><td class="num">${fmt(c.orgs)}</td><td class="num">${fmt(c.periods)}</td><td class="nowrap">${day(c.last_entry)}</td></tr>`).join("");
    const empty = {};
    C.filter((c) => !c.rows).forEach((c) => (empty[c.stage] = empty[c.stage] || []).push(c));
    Object.keys(empty).sort((a, b) => STAGE_RANK(a) - STAGE_RANK(b)).forEach((s) => {
      const list = empty[s].map((c) => c.country + (s === "Log frame only" && !c.indicators_targeted ? " (no targets)" : "")).join(", ");
      rows += `<tr><td>${chip(s)}</td><td>${esc(list)}</td><td class="num">0</td><td class="num">–</td><td></td><td class="num">–</td><td class="num">–</td><td>–</td></tr>`;
    });
    $("stages").innerHTML = rows;

    // admin ladder
    let h = `<div class="hd name">Country</div>` + [1, 2, 3, 4, 5].map((l) => `<div class="hd">Admin ${l}</div>`).join("");
    reporting.forEach((c) => {
      h += `<div class="name">${esc(c.country)}</div>`;
      for (let l = 1; l <= 5; l++) {
        const d = c.reported_levels.includes(l) ? `<span class="dot ${l === c.main_level ? "main" : "rep"}"></span>` : "";
        h += `<div>${l === c.required_admin_main ? `<span class="req" title="Required level">${d}</span>` : d}</div>`;
      }
    });
    $("ladder").innerHTML = h;

    // indicators
    $("indicators").innerHTML = C.slice().sort((a, b) => b.rows - a.rows || b.indicators_loaded - a.indicators_loaded).map((c) =>
      `<tr><td>${esc(c.country)}</td><td class="num">${fmt(c.indicators_loaded)}</td>` +
      `<td class="num">${c.placeholder_targets ? "placeholder" : fmt(c.indicators_targeted)}</td>` +
      `<td class="num">${c.rows ? fmt(c.targeted_reported) : "–"}</td>` +
      `<td class="num">${c.rows ? pct(c.agesex_pct) : "–"}</td><td class="num">${c.rows ? pct(c.disability_pct) : "–"}</td></tr>`).join("");
  }

  const ORDER = ["Established", "Active", "Piloting", "Configured", "Log frame only", "Not set up"];
  function STAGE_RANK(s) { const i = ORDER.indexOf(s); return i < 0 ? 99 : i; }

  Promise.all(["data/latest.json", "data/history.json"].map((u) =>
    fetch(u, { cache: "no-cache" }).then((r) => { if (!r.ok) throw new Error(`${u}: HTTP ${r.status}`); return r.json(); })))
    .then(([snap, history]) => render(snap, history))
    .catch((e) => {
      $("asof").textContent = "The snapshot could not be loaded.";
      $("takeaways").innerHTML = `<li class="error">Could not load the data files (${esc(e.message)}). Reload the page; if it persists, the refresh job may have failed.</li>`;
    });
})();
