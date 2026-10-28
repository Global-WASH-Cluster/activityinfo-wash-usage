(function () {
  "use strict";
  const STALE_DAYS = 3;
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => (n == null ? "–" : n.toLocaleString("en-GB"));
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const day = (iso) => (iso ? new Date(iso + (iso.length === 10 ? "T00:00:00Z" : "")).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }) : "–");
  const chip = (s) => `<span class="chip ${esc(s.split(" ")[0])}">${esc(s)}</span>`;
  const pct = (v) => (v == null ? "–" : `${Math.round(v)}%`);

  function render(snap) {
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

    // current rows per reporting module
    const max = Math.max(...reporting.map((c) => c.rows), 1);
    $("bars").innerHTML = reporting.map((c) =>
      `<div>${esc(c.country)}</div><div class="track" role="img" aria-label="${esc(c.country)}: ${fmt(c.rows)} rows">` +
      `<span class="now" style="width:${Math.max((c.rows / max) * 100, 0.4)}%"></span></div>` +
      `<div class="val"><b>${fmt(c.rows)}</b></div>`).join("");

    // stage table: reporting countries individually, empty ones grouped by stage
    let rows = reporting.slice().sort((a, b) => STAGE_RANK(a.stage) - STAGE_RANK(b.stage) || b.rows - a.rows).map((c) =>
      `<tr><td>${chip(c.stage)}</td><td>${esc(c.country)}${c.stale ? " *" : ""}</td><td class="num">${fmt(c.rows)}</td>` +
      `<td class="num">${fmt(c.orgs)}</td><td class="num">${fmt(c.periods)}</td><td class="nowrap">${day(c.last_entry)}</td></tr>`).join("");
    const empty = {};
    C.filter((c) => !c.rows).forEach((c) => (empty[c.stage] = empty[c.stage] || []).push(c));
    Object.keys(empty).sort((a, b) => STAGE_RANK(a) - STAGE_RANK(b)).forEach((s) => {
      const list = empty[s].map((c) => c.country + (s === "Log frame only" && !c.indicators_targeted ? " (no targets)" : "")).join(", ");
      rows += `<tr><td>${chip(s)}</td><td>${esc(list)}</td><td class="num">0</td><td class="num">–</td><td class="num">–</td><td>–</td></tr>`;
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

  // --- password gate: data files are AES-256-GCM encrypted (see pipeline/vault.py)
  const b64 = (t) => Uint8Array.from(atob(t), (c) => c.charCodeAt(0));
  async function decrypt(env, password) {
    const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
    const key = await crypto.subtle.deriveKey(
      { name: "PBKDF2", hash: "SHA-256", salt: b64(env.salt), iterations: env.iter },
      base, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
    const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(env.iv) }, key, b64(env.ct));
    return JSON.parse(new TextDecoder().decode(pt));
  }
  let envelope = null;
  async function unlock(password, remember) {
    if (!envelope) {
      const r = await fetch("data/latest.enc.json", { cache: "no-cache" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      envelope = await r.json();
    }
    let snap;
    try { snap = await decrypt(envelope, password); }
    catch (e) { const err = new Error("wrong"); err.wrong = true; throw err; }
    if (remember) { try { sessionStorage.setItem("aiw-pw", password); } catch (e) { /* storage blocked */ } }
    $("gate").hidden = true;
    $("report").hidden = false;
    render(snap);
  }
  const msg = $("gate-msg");
  $("gate").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const btn = $("unlock");
    btn.disabled = true; msg.className = "gate-msg"; msg.textContent = "Checking…";
    unlock($("pw").value, true).catch((e) => {
      msg.className = "gate-msg error";
      msg.textContent = e.wrong ? "That password is not correct. Check it and try again." :
        `The report data could not be loaded (${e.message}). Reload the page and try again.`;
      $("pw").select();
    }).finally(() => { btn.disabled = false; });
  });
  let saved = null;
  try { saved = sessionStorage.getItem("aiw-pw"); } catch (e) { /* storage blocked */ }
  if (saved) unlock(saved, false).catch(() => { try { sessionStorage.removeItem("aiw-pw"); } catch (e) { /* */ } });
})();
