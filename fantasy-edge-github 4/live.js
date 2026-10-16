// Fantasy Edge live layer. Runs only if config.js has a Supabase URL + anon key; otherwise the LIVE tab says so.
// Nothing here fabricates data: if a table is empty or unreachable it shows "Data unavailable" + last good sync.
(function () {
  const C = window.FE_CONFIG || {}, ON = !!(C.supabaseUrl && C.supabaseAnonKey);
  const S = { sb: null, user: null, rows: { analysis: [], news: [], sync_log: [], weather: [], games: [], players: [] }, err: "", live: false, tz: Intl.DateTimeFormat().resolvedOptions().timeZone, prefs: null, hist: [], league: null, os: "off" };
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  // All times are stored UTC; display converts with an IANA zone, so DST is handled by the browser's tz database.
  const fmt = (iso, o) => { try { return iso ? new Intl.DateTimeFormat("en-US", Object.assign({ timeZone: S.tz, month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }, o)).format(new Date(iso)) : "never"; } catch { return String(iso); } };
  const ago = iso => { if (!iso) return "never"; const m = Math.round((Date.now() - new Date(iso)) / 60000); return m < 1 ? "just now" : m < 90 ? m + " min ago" : Math.round(m / 60) + " h ago"; };
  const load = (src, ok) => new Promise(r => { const s = document.createElement("script"); s.src = src; s.onload = () => r(true); s.onerror = () => r(false); document.head.appendChild(s); });
  const redraw = () => { if (location.hash.slice(1) === "live" && typeof go === "function") go(); };

  async function fetchAll() {
    const sb = S.sb, q = (t, f) => f(sb.from(t).select("*")).then(r => { if (r.error) throw new Error(t + ": " + r.error.message); S.rows[t] = r.data || []; });
    try {
      await Promise.all([
        q("analysis", x => x.order("projection", { ascending: false }).limit(60)),
        q("news", x => x.order("published_at", { ascending: false }).limit(25)),
        q("sync_log", x => x), q("weather", x => x), q("games", x => x.order("kickoff_utc").limit(40)),
        q("players", x => x.not("injury_status", "is", null).limit(60))
      ]);
      S.err = "";
    } catch (e) { S.err = e.message; }
    redraw();
  }
  function subscribe() {
    const ch = S.sb.channel("fe-live");
    ["analysis", "news", "games", "weather", "sync_log", "players"].forEach(t => ch.on("postgres_changes", { event: "*", schema: "public", table: t }, () => { clearTimeout(subscribe.t); subscribe.t = setTimeout(fetchAll, 800); }));
    ch.subscribe(st => { S.live = st === "SUBSCRIBED"; redraw(); });
  }
  async function authInit() {
    const { data } = await S.sb.auth.getSession(); S.user = data.session?.user || null;
    S.sb.auth.onAuthStateChange((_e, s) => { S.user = s?.user || null; afterAuth(); });
    await afterAuth();
  }
  async function afterAuth() {
    if (S.user) {
      const [p, h, r] = await Promise.all([S.sb.from("notification_prefs").select("*").eq("user_id", S.user.id).maybeSingle(), S.sb.from("notifications").select("*").order("created_at", { ascending: false }).limit(20), S.sb.from("user_rosters").select("*").order("imported_at", { ascending: false }).limit(1)]);
      S.prefs = p.data; S.hist = h.data || []; S.league = r.data?.[0] || null;
      if (window.OneSignal && C.oneSignalAppId) { try { await OneSignal.login(S.user.id); } catch { } }
    } else { S.prefs = null; S.hist = []; S.league = null; }
    redraw();
  }
  async function initPush() {
    if (!C.oneSignalAppId) return;
    window.OneSignalDeferred = window.OneSignalDeferred || [];
    if (!await load("https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js")) { S.os = "blocked"; return; }
    OneSignalDeferred.push(async OS => { await OS.init({ appId: C.oneSignalAppId }); S.os = OS.Notifications.permission ? "on" : "off"; if (S.user) await OS.login(S.user.id); redraw(); });
  }

  // ---- actions (called from the LIVE tab buttons) ----
  window.feLive = {
    async magic() { const e = document.getElementById("fe-mail").value.trim(); if (!e) return; const r = await S.sb.auth.signInWithOtp({ email: e, options: { emailRedirectTo: location.href.split("#")[0] } }); alert(r.error ? "Could not send link: " + r.error.message : "Check your email for the sign-in link."); },
    async out() { await S.sb.auth.signOut(); },
    async push() { try { await OneSignal.Notifications.requestPermission(); S.os = OneSignal.Notifications.permission ? "on" : "off"; } catch (e) { alert("Push not available: " + e.message); } redraw(); },
    async pref(k, v) { if (!S.user) return; const r = await S.sb.from("notification_prefs").update({ [k]: v }).eq("user_id", S.user.id); if (r.error) alert(r.error.message); else S.prefs[k] = v; redraw(); },
    async tz(v) { S.tz = v; if (S.user) await S.sb.from("profiles").update({ timezone: v }).eq("id", S.user.id); redraw(); },
    async follow(id) { if (!S.user) return alert("Sign in first."); const r = await S.sb.from("follows").upsert({ user_id: S.user.id, kind: "player", ref: id }); alert(r.error ? r.error.message : "Following. You'll get alerts for this player."); },
    async sleeper() {
      const u = document.getElementById("fe-sl").value.trim(); if (!u) return;
      try {
        const J = async p => { const r = await fetch("https://api.sleeper.app/v1/" + p); if (!r.ok) throw new Error(p + " → HTTP " + r.status); return r.json(); };
        const usr = await J("user/" + encodeURIComponent(u)), leagues = await J(`user/${usr.user_id}/leagues/nfl/${C.season || new Date().getFullYear()}`);
        if (!leagues.length) return alert("No NFL leagues found for that user this season.");
        const L = leagues[0], rosters = await J(`league/${L.league_id}/rosters`), mine = rosters.find(x => x.owner_id === usr.user_id);
        if (!mine) return alert("Could not find your roster in that league.");
        const row = { league_id: L.league_id, league_name: L.name, scoring: L.scoring_settings, roster_positions: L.roster_positions, player_ids: mine.players || [], starters: mine.starters || [] };
        if (S.user) { const r = await S.sb.from("user_rosters").upsert(Object.assign({ user_id: S.user.id }, row)); if (r.error) throw new Error(r.error.message); await S.sb.from("profiles").update({ sleeper_user: u }).eq("id", S.user.id); }
        S.league = Object.assign({ imported_at: new Date().toISOString() }, row); redraw();
      } catch (e) { alert("Sleeper import failed: " + e.message); }
    },
    refresh: () => fetchAll()
  };

  function view() {
    if (!ON) return `<div class="card"><h2>● LIVE — not connected</h2><p>No backend is configured, so nothing on this tab is live. The rest of the app shows its labeled sample/saved data. To connect, deploy <code>fantasy-edge-backend</code> and fill in <code>config.js</code>.</p></div>`;
    const R = S.rows, name = id => (R.players.find(p => p.id === id) || {}).full_name || id;
    const last = R.sync_log.map(s => s.last_success_utc).filter(Boolean).sort().pop();
    const badge = S.live ? `<span style="color:#22c55e">● LIVE</span>` : `<span style="color:#f59e0b">● Not streaming</span>`;
    const tbl = (rows, cols) => `<table style="width:100%"><tr>${cols.map(c => `<th align="left">${c[0]}</th>`).join("")}</tr>${rows.map(r => `<tr>${cols.map(c => `<td>${c[1](r)}</td>`).join("")}</tr>`).join("")}</table>`;
    const unavail = what => `<p style="opacity:.8">Data unavailable${S.err ? " (" + esc(S.err) + ")" : ""} — ${what}. Last successful update: ${fmt(last)}.</p>`;
    const sync = tbl(R.sync_log, [["Source", r => esc(r.source)], ["Last success", r => fmt(r.last_success_utc) + " (" + ago(r.last_success_utc) + ")"], ["Rows", r => r.rows ?? 0], ["Last error", r => esc(r.last_error || "—")]]);
    const an = R.analysis.length ? tbl(R.analysis, [["Player", r => esc(name(r.player_id)) + ` <a href="#live" onclick="feLive.follow('${esc(r.player_id)}');return false">☆</a>`], ["Proj", r => Number(r.projection).toFixed(1)], ["Call", r => esc(r.start_sit || "—")], ["Conf.", r => esc(r.confidence || "—")], ["Why", r => esc(r.explanation || "")], ["Updated", r => fmt(r.updated_at)]]) : unavail("no projections have been computed yet");
    const nw = R.news.length ? R.news.map(n => `<p><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.headline)}</a><br><small>${esc(n.source)} · ${fmt(n.published_at)}</small></p>`).join("") : unavail("no news synced yet");
    const inj = R.players.length ? tbl(R.players, [["Player", r => esc(r.full_name)], ["Team", r => esc(r.team || "")], ["Status", r => `<b style="color:#ef4444">⚠ ${esc(r.injury_status)}</b>`], ["Body", r => esc(r.injury_body_part || "")], ["Updated", r => fmt(r.updated_at)]]) : unavail("no injury data synced yet");
    const gm = R.games.length ? tbl(R.games, [["Game", r => esc(r.away) + " @ " + esc(r.home)], ["Kickoff (your time)", r => fmt(r.kickoff_utc, { weekday: "short" })], ["Weather", r => { const w = R.weather.find(x => x.game_id === r.id); return r.dome ? "Dome" : w ? `${Math.round(w.temp_f)}°F, wind ${Math.round(w.wind_mph)} mph, precip ${Math.round(w.precip_pct)}% <small>(${ago(w.fetched_at)})</small>` : "Data unavailable"; }]]) : unavail("no schedule synced yet");
    const acct = S.user ? `<p>Signed in as ${esc(S.user.email)} <button onclick="feLive.out()">Sign out</button></p>` : `<p><input id="fe-mail" type="email" placeholder="email for magic link"> <button onclick="feLive.magic()">Sign in</button></p>`;
    const P = S.prefs, tog = (k, l) => `<label style="display:block"><input type="checkbox" ${P[k] ? "checked" : ""} onchange="feLive.pref('${k}',this.checked)"> ${l}</label>`;
    const sets = !S.user ? "<p>Sign in to choose alerts.</p>" : !P ? "<p>Data unavailable.</p>" : `${tog("injury_alerts", "Injury alerts")}${tog("lineup_alerts", "Lineup alerts")}${tog("projection_alerts", "Big projection changes")}${tog("daily_summary", "Daily briefing")}${tog("quiet_enabled", "Quiet hours")} <small>${P.quiet_start}:00–${P.quiet_end}:00 in ${esc(S.tz)}</small><br><button onclick="feLive.push()">Push: ${S.os === "on" ? "enabled ✓" : S.os === "blocked" ? "SDK blocked" : "turn on"}</button>`;
    const hist = S.hist.length ? S.hist.map(h => `<p><b>${esc(h.title)}</b> <small>${fmt(h.created_at)} · ${esc(h.status)}</small><br>${esc(h.body)}</p>`).join("") : "<p>No notifications yet.</p>";
    const lg = S.league ? `<p>Imported <b>${esc(S.league.league_name)}</b> (${S.league.player_ids.length} players, ${fmt(S.league.imported_at)}). Start/sit below uses your league's scoring + roster slots.</p>` + (R.analysis.length ? tbl(S.league.player_ids.map(id => R.analysis.find(a => a.player_id === id)).filter(Boolean), [["Player", r => esc(name(r.player_id))], ["Proj", r => Number(r.projection).toFixed(1)], ["Call", r => esc(r.start_sit || "")]]) + `<small>Projections are PPR defaults; league scoring is stored but not yet applied.</small>` : "") : "";
    return `<div class="card"><h2>${badge} &nbsp; Last updated: ${fmt(last)} (${ago(last)}) <button onclick="feLive.refresh()">Refresh</button></h2>
      <p>Times shown in <select onchange="feLive.tz(this.value)">${[S.tz, "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "UTC"].filter((v, i, a) => a.indexOf(v) === i).map(z => `<option ${z === S.tz ? "selected" : ""}>${z}</option>`).join("")}</select>. Stored in UTC; DST handled automatically.</p>${S.err ? `<p style="color:#ef4444">Backend error: ${esc(S.err)}</p>` : ""}</div>
      <div class="card"><h3>Projections (transparent model)</h3>${an}</div><div class="card"><h3>Injuries</h3>${inj}</div><div class="card"><h3>Games &amp; weather</h3>${gm}</div><div class="card"><h3>News (attributed)</h3>${nw}</div>
      <div class="card"><h3>Data sources</h3>${sync}</div><div class="card"><h3>Account &amp; notifications</h3>${acct}${sets}<h4>History</h4>${hist}</div>
      <div class="card"><h3>Import Sleeper league</h3><input id="fe-sl" placeholder="Sleeper username"> <button onclick="feLive.sleeper()">Import</button>${lg}</div>`;
  }

  if (typeof N !== "undefined" && typeof V !== "undefined") { V.live = view; if (ON) N.live = "● LIVE"; if (typeof go === "function") go(); }
  if (!ON) return;
  load("https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4/dist/umd/supabase.min.js").then(async ok => {
    if (!ok || !window.supabase) { S.err = "Could not load the Supabase client"; return redraw(); }
    S.sb = supabase.createClient(C.supabaseUrl, C.supabaseAnonKey);
    await authInit(); await fetchAll(); subscribe(); initPush();
  });
})();
