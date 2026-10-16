#!/usr/bin/env python3
"""
Fantasy Edge player data builder.

Downloads free public NFL data from nflverse (https://github.com/nflverse/nflverse-data), then computes for every
QB / RB / WR / TE / K and every team defense:
  * last season's fantasy points (regular season, PPR) and games played
  * this season's weekly fantasy points with the opponent for each game
  * a transparent projection for the next game (formula below)
  * real defense-vs-position matchup ranks for all 32 teams (points allowed per game to each position)
It also back-tests the projection formula on past seasons so the site can show how accurate it really is.
For the Odds tab it embeds the real money lines, point spreads and totals from the nflverse schedule file (PD.odds), plus two
numbers measured from 2018-2025: how far final margins land from the spread, and how often favorites at each price won.
The schedule file is re-downloaded on every run because the lines and scores change daily.
It also embeds the stadium for each game (PD.stad, for the weather page) and the official NFL injury report (PD.inj), and the site
calculates standings from the real final scores. The schedule and injury files are re-downloaded on every run.

Usage:
  python3 build_player_data.py                      # writes player_data.json next to this script
  python3 build_player_data.py --embed fantasy-edge.html   # also patches the data into the site

Requires: python3 (3.9+), pandas, numpy, internet access to github.com (Windows also needs: pip install tzdata).
Run it again after each week's games to refresh.
Nothing here is invented: if a number cannot be computed from the downloaded data it is left out.

PROJECTION FORMULA (weighted average of real game scores):
    prior   : pseudo-games at the position's typical points per game (median of players with 4+ games):
              1 for QB/RB/WR/TE, 20 for kickers and defenses (back-tested; those are mostly luck)
    last yr : each game from last season counts 0.5
    this yr : each game counts 1.0, and the 3 most recent games count 2.0
    projection = sum(weight x points) / sum(weight)
Parameters were chosen by back-testing on 2024, 2025 and early 2026 (see backtest() below).
"""
import argparse, collections, datetime, json, os, re, sys, unicodedata, urllib.request, zoneinfo
import numpy as np
import pandas as pd

ET = zoneinfo.ZoneInfo("America/New_York")  # nflverse lists kickoff times in Eastern time, wherever the game is played
ODDS_FROM, ODDS_TO = 2018, 2025             # past seasons used to measure how far results land from the spread and how reliable the lines were

BASE = "https://github.com/nflverse/nflverse-data/releases/download"
OFF = ("QB", "RB", "WR", "TE")
# Projection parameters (chosen by back-test; flat optimum, so these are deliberately simple round numbers).
W_LAST_YEAR, RECENT_BOOST, N_RECENT = 0.5, 1.0, 3
# Weight of the "typical player at this position" pseudo-game. Back-tests: 1 is best for QB/RB/WR/TE (each tested alone);
# kickers and team defenses are mostly week-to-week luck, so they are pulled much harder toward the position average (about 20).
PRIOR_W = {"K": 20.0, "DEF": 20.0}
PRIOR_W_DEFAULT = 1.0
K_PRIOR_GAMES = 6          # shrink this season's points-allowed toward last season's, as if 6 extra games
TEAM_FIX = {"JAX": "JAC"}  # the site uses JAC for Jacksonville


def nz(s):
    """Lower-case letters and digits only, accents folded (Estimé -> estime), matching the site's own name key."""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def tm(t):
    return TEAM_FIX.get(t, t)


# ------------------------------------------------------------------ scoring helpers (pure functions, unit tested)
def kicker_points(fg_0_39, fg_40_49, fg_50_up, pat_made):
    """Fantasy Edge kicker scoring: FG 3 (0-39 yd), 4 (40-49), 5 (50+), PAT 1. No penalties for misses."""
    return 3 * fg_0_39 + 4 * fg_40_49 + 5 * fg_50_up + pat_made


def points_allowed_tier(pa):
    """Common D/ST points-allowed scoring: 0 -> 10, 1-6 -> 7, 7-13 -> 4, 14-20 -> 1, 21-27 -> 0, 28-34 -> -1, 35+ -> -4."""
    if pa == 0: return 10
    if pa <= 6: return 7
    if pa <= 13: return 4
    if pa <= 20: return 1
    if pa <= 27: return 0
    if pa <= 34: return -1
    return -4


def dst_points(sacks, ints, fum_rec, tds, safeties, blocks, pts_allowed):
    """D/ST: sack 1, INT 2, fumble recovery 2, defensive/return TD 6, safety 2, blocked kick 2, plus points-allowed tier."""
    return sacks + 2 * ints + 2 * fum_rec + 6 * tds + 2 * safeties + 2 * blocks + points_allowed_tier(pts_allowed)


def prior_w(pos):
    return PRIOR_W.get(pos, PRIOR_W_DEFAULT)


def project(prior_ppg, games_last_year, ppg_last_year, this_year_pts, pw=PRIOR_W_DEFAULT):
    """Weighted average of real game scores. Returns (projection, effective_games) or (None, 0) with no data at all."""
    if not games_last_year and not this_year_pts:
        return None, 0.0
    num, den = pw * prior_ppg, pw
    if games_last_year:
        num += W_LAST_YEAR * games_last_year * ppg_last_year
        den += W_LAST_YEAR * games_last_year
    n = len(this_year_pts)
    for i, p in enumerate(this_year_pts):
        w = 1.0 + (RECENT_BOOST if i >= n - N_RECENT else 0.0)
        num += w * p
        den += w
    return num / den, den - pw


def confidence(n_this_year, games_last_year):
    eff = n_this_year + 0.5 * games_last_year
    return "high" if (eff >= 8 and n_this_year >= 3) else "medium" if eff >= 4 else "low"


def rank_desc(values):
    """{key: value} -> {key: rank}, rank 1 = largest value."""
    order = sorted(values, key=lambda k: -values[k])
    return {k: i + 1 for i, k in enumerate(order)}


def blend_allowed(sum_now, n_now, avg_last):
    """Points allowed per game this season, shrunk toward last season (K_PRIOR_GAMES pseudo-games)."""
    return (sum_now + K_PRIOR_GAMES * avg_last) / (n_now + K_PRIOR_GAMES)


# ------------------------------------------------------------------ betting lines (pure functions, unit tested)
def implied_prob(ml):
    """Chance of winning that American odds imply, sportsbook margin included: -155 -> 0.608, +130 -> 0.435, +-100 -> 0.5."""
    ml = float(ml)
    return -ml / (-ml + 100.0) if ml < 0 else 100.0 / (ml + 100.0)


def valid_ml(ml):
    """A real American price: a number whose size is at least 100 (anything else is missing or garbage)."""
    return ml is not None and not pd.isna(ml) and abs(float(ml)) >= 100


def no_vig(ml_away, ml_home):
    """(away, home) chance to win with the sportsbook's margin taken out: each side's implied chance divided by the two added up.
    None when either price is missing or not a real American price."""
    if not (valid_ml(ml_away) and valid_ml(ml_home)): return None
    a, h = implied_prob(ml_away), implied_prob(ml_home)
    return a / (a + h), h / (a + h)


def kickoff_ms(gameday, gametime):
    """Kickoff as UTC epoch milliseconds (None when the schedule has no time yet). gametime is Eastern time."""
    if not isinstance(gameday, str) or not isinstance(gametime, str): return None
    dt = datetime.datetime.fromisoformat(f"{gameday}T{gametime}").replace(tzinfo=ET)
    return int(dt.timestamp() * 1000)


CAL_EDGES = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 1.01]


def calibration(away_ml, home_ml, home_score, away_score):
    """How often did the favorite at each price actually win? Returns [[low, high, games, win_rate], ...] for favorites'
    no-vig chance in 5-point buckets (the last bucket is 85% and up). A tie counts as half a win."""
    am, hm = np.asarray(away_ml, float), np.asarray(home_ml, float)
    a = np.where(am < 0, -am / (-am + 100), 100 / (am + 100))
    h = np.where(hm < 0, -hm / (-hm + 100), 100 / (hm + 100))
    ph = h / (a + h)
    hs, as_ = np.asarray(home_score, float), np.asarray(away_score, float)
    home_won = np.where(hs > as_, 1.0, np.where(hs < as_, 0.0, 0.5))
    fav_won = np.where(ph >= 0.5, home_won, 1 - home_won)
    pf = np.maximum(ph, 1 - ph)
    out = []
    for lo, hi in zip(CAL_EDGES[:-1], CAL_EDGES[1:]):
        m = (pf >= lo) & (pf < hi)
        if m.sum(): out.append([lo, min(hi, 1.0), int(m.sum()), round(float(fav_won[m].mean()), 3)])
    return out


def odds_block(games, season, got):
    """Real betting lines for `season`, plus two things measured from past seasons: how far final margins land from the
    point spread (sd, in points) and how often favorites at each price won (cal).
    Game fields: id, w week, a/h away/home team, k kickoff (UTC ms), am/hm money lines, sp spread (positive = home favored),
    ao/ho spread prices, tl total points line, ov/un over/under prices, sa/sh final scores, nt 1 = neutral site.
    Anything the file does not have is left out (never guessed)."""
    reg = games[games.game_type == "REG"]
    cur = reg[reg.season == season]
    priced = cur.dropna(subset=["home_moneyline", "away_moneyline"])
    last = int(priced.week.max()) if len(priced) else 0
    gl = []
    for r in cur[cur.week <= last].sort_values(["week", "gameday", "gametime", "game_id"]).itertuples():
        g = {"id": r.game_id, "w": int(r.week), "a": tm(r.away_team), "h": tm(r.home_team)}
        k = kickoff_ms(r.gameday, r.gametime)
        if k: g["k"] = k
        if valid_ml(r.away_moneyline) and valid_ml(r.home_moneyline):
            g["am"], g["hm"] = int(r.away_moneyline), int(r.home_moneyline)
        for key, col in (("sp", "spread_line"), ("tl", "total_line")):
            if not pd.isna(getattr(r, col)): g[key] = float(getattr(r, col))
        for key, col in (("ao", "away_spread_odds"), ("ho", "home_spread_odds"), ("ov", "over_odds"), ("un", "under_odds")):
            if not pd.isna(getattr(r, col)): g[key] = int(getattr(r, col))
        if not pd.isna(r.home_score) and not pd.isna(r.away_score):
            g["sa"], g["sh"] = int(r.away_score), int(r.home_score)
        if r.location == "Neutral": g["nt"] = 1
        sn = getattr(r, "stadium", None)
        if isinstance(sn, str) and sn: g["sn"] = sn
        gl.append(g)
    past = reg[(reg.season >= ODDS_FROM) & (reg.season <= ODDS_TO)]
    sp = past.dropna(subset=["home_score", "away_score", "spread_line"])
    mp = past.dropna(subset=["home_score", "away_score", "home_moneyline", "away_moneyline"])
    sd = float((sp.home_score - sp.away_score - sp.spread_line).std()) if len(sp) > 30 else None
    return {"got": got, "from": ODDS_FROM, "to": ODDS_TO, "sd": None if sd is None else round(sd, 2), "sdN": int(len(sp)),
            "cal": calibration(mp.away_moneyline, mp.home_moneyline, mp.home_score, mp.away_score), "calN": int(len(mp)), "g": gl}


# ------------------------------------------------------------------ stadiums (weather) and injuries
# Stadium locations are fixed facts, to about 1 km: enough for a city-level forecast. covered = 1 means a dome or a roof, where the
# weather does not matter. Names are the ones nflverse uses in games.csv. A stadium that is not in this table gets no forecast
# (the site says "Data unavailable"); it is never guessed.
STADIUMS = {
    "M&T Bank Stadium": (39.278, -76.623, 0), "Highmark Stadium": (42.774, -78.787, 0), "Bank of America Stadium": (35.226, -80.853, 0),
    "Soldier Field": (41.862, -87.617, 0), "Paycor Stadium": (39.095, -84.516, 0), "Huntington Bank Field": (41.5061, -81.6995, 0),
    "Empower Field at Mile High": (39.7439, -105.0201, 0), "Lambeau Field": (44.5013, -88.0622, 0), "Hard Rock Stadium": (25.958, -80.239, 0),
    "Gillette Stadium": (42.0909, -71.2643, 0), "MetLife Stadium": (40.8135, -74.0745, 0), "Lincoln Financial Field": (39.9008, -75.1675, 0),
    "Acrisure Stadium": (40.447, -80.016, 0), "Lumen Field": (47.595, -122.332, 0), "Levi's Stadium": (37.403, -121.97, 0),
    "Raymond James Stadium": (27.9759, -82.5033, 0), "Nissan Stadium": (36.166, -86.771, 0), "Northwest Stadium": (38.908, -76.864, 0),
    "GEHA Field at Arrowhead Stadium": (39.0489, -94.4839, 0), "EverBank Stadium": (30.324, -81.637, 0),
    "State Farm Stadium": (33.528, -112.263, 1), "Mercedes-Benz Stadium": (33.755, -84.401, 1), "AT&T Stadium": (32.748, -97.093, 1),
    "Reliant Stadium": (29.685, -95.411, 1), "Lucas Oil Stadium": (39.760, -86.164, 1), "Ford Field": (42.340, -83.046, 1),
    "SoFi Stadium": (33.953, -118.339, 1), "Allegiant Stadium": (36.091, -115.184, 1), "U.S. Bank Stadium": (44.974, -93.258, 1),
    "Caesars Superdome": (29.951, -90.081, 1),
    # neutral sites
    "Tottenham Hotspur Stadium": (51.6043, -0.0662, 0), "Wembley Stadium": (51.556, -0.280, 0), "Melbourne Cricket Ground": (-37.820, 144.983, 0),
    "Estadio Banorte": (19.303, -99.150, 0), "Maracana Stadium": (-22.912, -43.230, 0), "Bernabeu": (40.453, -3.688, 0),
    "FC Bayern Munich Stadium": (48.219, 11.625, 1), "Stade de France": (48.924, 2.360, 1),
}


def stadium_block(odds):
    """{stadium name: [lat, lon, covered]} for the stadiums used by the games in the odds block, plus the names we do not know."""
    names = sorted({g["sn"] for g in odds["g"] if "sn" in g})
    return {n: list(STADIUMS[n]) for n in names if n in STADIUMS}


INJ_RANK = {"Out": 0, "Doubtful": 1, "Questionable": 2, "DNP": 3, "Limited": 4}


def injuries_block(inj, got, season):
    """Official NFL injury reports (nflverse, injuries_<season>.csv) for the latest week in the file: QB/RB/WR/TE/K who are not
    practicing fully. status = the game-day report (Out / Doubtful / Questionable) when there is one, otherwise the latest practice
    status (DNP = did not practice, Limited). Rows: [name, team, position, status, injury text]."""
    if inj is None or not len(inj): return None
    inj = inj[(inj.season == season) & (inj.game_type == "REG")]
    if not len(inj): return None
    wk = int(inj.week.max())
    rows = []
    for r in inj[inj.week == wk].itertuples():
        if r.position not in ("QB", "RB", "WR", "TE", "K"): continue
        st = r.report_status if isinstance(r.report_status, str) and r.report_status in INJ_RANK else None
        if st is None:
            ps = r.practice_status if isinstance(r.practice_status, str) else ""
            st = "DNP" if ps.startswith("Did Not") else "Limited" if ps.startswith("Limited") else None
        if st is None: continue
        parts = [x for x in (r.report_primary_injury, r.report_secondary_injury) if isinstance(x, str) and x]
        if not parts:
            parts = [x for x in (r.practice_primary_injury, r.practice_secondary_injury) if isinstance(x, str) and x]
        text = "; ".join(parts)
        if text.lower().startswith("not injury related"): text = "Rest day (not injury related)"
        rows.append([r.full_name, tm(r.team), r.position, st, text or "Injury not listed"])
    rows.sort(key=lambda x: (INJ_RANK[x[3]], x[1], x[0]))
    teams = sorted({tm(t) for t in inj[inj.week == wk].team})
    return {"w": wk, "got": got, "teams": len(teams), "l": rows}


# ------------------------------------------------------------------ download + load
def fetch(url, path, force=False):
    if os.path.exists(path) and os.path.getsize(path) > 1000 and not (FORCE or force):
        return
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-edge-builder"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()                      # read it all first, so a failed download can never wipe the cached copy
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    print("downloaded", os.path.basename(path), os.path.getsize(path) // 1024, "KB")


WEEK_COLS = ["player_id", "player_display_name", "position", "season_type", "week", "team", "opponent_team", "game_id",
             "fantasy_points_ppr", "fg_made_0_19", "fg_made_20_29", "fg_made_30_39", "fg_made_40_49", "fg_made_50_59",
             "fg_made_60_", "pat_made"]


def load_week(path, roster_pos=None):
    df = pd.read_csv(path, usecols=lambda c: c in WEEK_COLS, low_memory=False)
    df = df[df.season_type == "REG"].copy()
    df["position"] = df.position.replace("FB", "RB")
    if roster_pos:  # two-way players (e.g. a CB the roster lists as WR): use the roster position when the game-log one is not offensive
        fix = df.player_id.map(roster_pos)
        m = ~df.position.isin(OFF + ("K",)) & fix.notna()
        df.loc[m, "position"] = fix[m]
    for c in WEEK_COLS[9:]:
        if c not in df: df[c] = 0
    df[WEEK_COLS[9:]] = df[WEEK_COLS[9:]].fillna(0)
    k = df.position == "K"
    df.loc[k, "fantasy_points_ppr"] = [kicker_points(a + b + c, d, e + f, g) for a, b, c, d, e, f, g in
                                      zip(df.fg_made_0_19[k], df.fg_made_20_29[k], df.fg_made_30_39[k], df.fg_made_40_49[k],
                                          df.fg_made_50_59[k], df.fg_made_60_[k], df.pat_made[k])]
    df = df.rename(columns={"fantasy_points_ppr": "pts"})
    return df[df.position.isin(OFF + ("K",))]


# ------------------------------------------------------------------ main computation
def build(season, cache):
    f = lambda n: os.path.join(cache, n)
    for y in (season, season - 1):
        fetch(f"{BASE}/stats_player/stats_player_week_{y}.csv", f(f"week_{y}.csv"))
        fetch(f"{BASE}/stats_team/stats_team_week_{y}.csv", f(f"team_{y}.csv"))
    fetch(f"{BASE}/rosters/roster_{season}.csv", f(f"roster_{season}.csv"))
    try:  # betting lines and final scores change every day, so the schedule file is always downloaded fresh
        fetch(f"{BASE}/schedules/games.csv", f("games.csv"), force=True)
    except Exception as e:
        if not os.path.exists(f("games.csv")): raise
        print("could not refresh games.csv, using the cached copy:", e)
    got = datetime.datetime.fromtimestamp(os.path.getmtime(f("games.csv")), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inj_blk = None
    try:  # official injury reports; also always fresh. If it cannot be downloaded the site just shows no automatic injury list.
        fetch(f"{BASE}/injuries/injuries_{season}.csv", f(f"injuries_{season}.csv"), force=True)
    except Exception as e:
        print("could not refresh the injury file:", e)
    if os.path.exists(f(f"injuries_{season}.csv")):
        igot = datetime.datetime.fromtimestamp(os.path.getmtime(f(f"injuries_{season}.csv")), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        inj_blk = injuries_block(pd.read_csv(f(f"injuries_{season}.csv"), low_memory=False), igot, season)
    ros = pd.read_csv(f(f"roster_{season}.csv"), low_memory=False)
    rmap = {r.gsis_id: r.position for r in ros.drop_duplicates("gsis_id").itertuples() if r.position in OFF + ("K",) and isinstance(r.gsis_id, str)}
    cur, prev = load_week(f(f"week_{season}.csv"), rmap), load_week(f(f"week_{season - 1}.csv"), rmap)
    games = pd.read_csv(f("games.csv"), low_memory=False)
    g_now, g_prev = games[games.season == season].copy(), games[games.season == season - 1].copy()

    # ---- schedule: each team's next unplayed game, last fully played week
    unplayed = g_now[g_now.home_score.isna() | g_now.away_score.isna()].sort_values(["week", "gameday", "gametime"])
    teams = sorted({tm(t) for t in pd.concat([g_now.home_team, g_now.away_team])})
    wk = int(unplayed.week.min()) if len(unplayed) else int(g_now.week.max())
    nxt = {}
    # a team's matchup is its game in the current week even if that game is already final (a Thursday game), so the whole week
    # stays consistent; a team on a bye gets its next unplayed game
    for src in (g_now[g_now.week == wk].sort_values(["gameday", "gametime"]), unplayed):
        for r in src.itertuples():
            for team, opp, home in ((r.away_team, r.home_team, False), (r.home_team, r.away_team, True)):
                nxt.setdefault(tm(team), [int(r.week), tm(opp), 1 if home else 0])
    thru = wk - 1   # every game before this week is final (games already final this week are also included in the logs)
    playing = {tm(t) for t in pd.concat([g_now[g_now.week == wk].home_team, g_now[g_now.week == wk].away_team])}
    bye = sorted(t for t in teams if t not in playing)
    opp_next = {t: (nxt[t] if t in nxt else None) for t in teams}

    # ---- positional priors: median points per game of players with 4+ games last season
    pg = prev.groupby(["player_id", "position"]).pts.agg(["size", "mean"]).reset_index()
    prior = {p: float(pg[(pg.position == p) & (pg["size"] >= 4)]["mean"].median()) for p in OFF + ("K",)}

    # ---- defense vs position (points allowed per game to QB/RB/WR/TE), blended with last season
    def allowed(df):
        by = df[df.position.isin(OFF)].groupby(["opponent_team", "position", "week"]).pts.sum().reset_index()
        weeks = df.groupby("opponent_team").week.unique()
        return by, weeks
    a_now, w_now = allowed(cur)
    a_prev, w_prev = allowed(prev)
    dvp, lg = {}, {}
    for pos in OFF:
        vals, det = {}, {}
        for t in teams:
            tt = "JAX" if t == "JAC" else t
            n_now = len(w_now.get(tt, []))
            s_now = float(a_now[(a_now.opponent_team == tt) & (a_now.position == pos)].pts.sum())
            n_prev = len(w_prev.get(tt, []))
            avg_prev = float(a_prev[(a_prev.opponent_team == tt) & (a_prev.position == pos)].pts.sum()) / n_prev if n_prev else None
            if avg_prev is None:  # no last-season games for this franchise: use the league average
                avg_prev = float(a_prev[a_prev.position == pos].pts.mean())
            vals[t] = blend_allowed(s_now, n_now, avg_prev)
            det[t] = (s_now / n_now if n_now else None, n_now, avg_prev)
        rk = rank_desc(vals)
        lg[pos] = round(float(np.mean(list(vals.values()))), 1)
        dvp[pos] = {t: [rk[t], round(vals[t], 1), None if det[t][0] is None else round(det[t][0], 1), det[t][1], round(det[t][2], 1)] for t in teams}

    # ---- team defense (D/ST) and offense scoring, from team stats + scores
    def team_rows(season_, gdf, team_csv):
        t = pd.read_csv(team_csv, low_memory=False)
        t = t[t.season_type == "REG"].fillna(0).copy()
        sc = {}
        for r in gdf.dropna(subset=["home_score"]).itertuples():
            sc[(r.game_id, r.home_team)] = (r.home_score, r.away_score)
            sc[(r.game_id, r.away_team)] = (r.away_score, r.home_score)
        out = []
        for r in t.itertuples():
            k = (r.game_id, r.team)
            if k not in sc: continue
            scored, allowed_ = sc[k]
            blocks = (getattr(r, "def_punt_blocks", 0) or 0) + (getattr(r, "def_pat_blocks", 0) or 0) + (getattr(r, "def_fg_blocks", 0) or 0)
            pts = dst_points(r.def_sacks, r.def_interceptions, r.fumble_recovery_opp, (r.def_tds or 0) + (r.special_teams_tds or 0),
                             r.def_safeties, blocks, allowed_)
            away = r.game_id.split("_")[2] == r.team
            out.append((tm(r.team), int(r.week), tm(r.opponent_team), away, float(pts), float(scored)))
        return out
    d_now = team_rows(season, g_now, f(f"team_{season}.csv"))
    d_prev = team_rows(season - 1, g_prev, f(f"team_{season - 1}.csv"))
    dst, off_pa = {}, {}
    prior["DEF"] = float(np.median([np.mean([x[4] for x in d_prev if x[0] == t]) for t in teams if any(x[0] == t for x in d_prev)] or [7.0]))
    for t in teams:
        ln = sorted([x for x in d_now if x[0] == t], key=lambda x: x[1])
        lp_ = [x for x in d_prev if x[0] == t]
        g25 = len(lp_)
        dst[t] = {"g5": g25, "p5": round(sum(x[4] for x in lp_), 1),
                  "w": [[x[1], ("@" if x[3] else "") + x[2], round(x[4], 1)] for x in ln]}
        pr_, eff = project(prior["DEF"], g25, (sum(x[4] for x in lp_) / g25) if g25 else 0, [x[4] for x in ln], prior_w("DEF"))
        if pr_ is not None: dst[t]["pr"] = round(pr_, 1); dst[t]["cf"] = confidence(len(ln), g25)
        # opponent-offense scoring (for the D/ST matchup): points the team itself scores per game
        s_now = sum(x[5] for x in ln); n_now = len(ln)
        avg_prev = (sum(x[5] for x in lp_) / g25) if g25 else float(np.mean([x[5] for x in d_prev]) if d_prev else 21.0)
        off_pa[t] = blend_allowed(s_now, n_now, avg_prev)
    off_rank = {t: r for t, r in rank_desc({t: -v for t, v in off_pa.items()}).items()}  # rank 1 = lowest-scoring offense
    off = {t: [off_rank[t], round(off_pa[t], 1)] for t in teams}

    # ---- matchup rank for each team's next game, keyed "TEAM|POS" (rank 1 = best matchup for that team's position group)
    mx = {}
    for t, nx in opp_next.items():
        if not nx or t in bye: continue
        for pos in OFF: mx[f"{t}|{pos}"] = dvp[pos][nx[1]][0]
        mx[f"{t}|DEF"] = off_rank[nx[1]]

    # ---- per-player records
    cur_by = {pid: g.sort_values("week") for pid, g in cur.groupby("player_id")}
    prev_by = {pid: g for pid, g in prev.groupby("player_id")}
    rpos = ros.drop_duplicates("gsis_id").set_index("gsis_id")
    names = {}
    for df in (prev, cur):
        for r in df.drop_duplicates("player_id", keep="last").itertuples(): names[r.player_id] = r.player_display_name
    players, key_count = {}, collections.Counter()
    for pid in set(cur_by) | set(prev_by):
        c, p = cur_by.get(pid), prev_by.get(pid)
        last = (c if c is not None else p).iloc[-1]
        pos = last.position
        team = tm(last.team)
        status = None
        if pid in rpos.index:
            rr = rpos.loc[pid]
            status = rr.status if isinstance(rr.status, str) else None
            if rr.position in OFF + ("K",): pos = rr.position
            if isinstance(rr.team, str): team = tm(rr.team)
        if c is None and status == "CUT": continue
        if c is None and pid not in rpos.index: continue  # no longer in the league
        g25 = 0 if p is None else len(p)
        m25 = 0.0 if not g25 else float(p.pts.mean())
        log = []
        if c is not None:
            for r in c.itertuples():
                away = str(r.game_id).split("_")[2] == r.team
                log.append([int(r.week), ("@" if away else "") + tm(r.opponent_team), round(float(r.pts), 1)])
        proj, _ = project(prior.get(pos, prior["WR"]), g25, m25, [x[2] for x in log], prior_w(pos))
        rec = {"n": names[pid], "t": team, "p": pos, "g5": g25, "p5": round(float(p.pts.sum()), 1) if g25 else 0, "w": log}
        if proj is not None:
            rec["pr"] = round(proj, 1); rec["cf"] = confidence(len(log), g25)
        if status: rec["rs"] = status
        k = nz(rec["n"])
        key_count[k] += 1
        players[k if key_count[k] == 1 else k + pos.lower()] = rec

    # ---- how accurate is the formula? (back-test on the three most recent completed/ongoing seasons)
    bt = backtest(season, cache, f)

    od = odds_block(games, season, got)
    return {"asof": datetime.date.today().isoformat(), "season": season, "wk": wk, "thru": thru,
            "src": "nflverse (github.com/nflverse/nflverse-data). Fantasy points are PPR; kicker and D/ST scoring are Fantasy Edge's own (see model).",
            "model": {"prior": {k: round(v, 1) for k, v in prior.items()}, "priorW": {"default": PRIOR_W_DEFAULT, **PRIOR_W}, "wLast": W_LAST_YEAR,
                      "recent": 1 + RECENT_BOOST, "nRecent": N_RECENT, "kPrior": K_PRIOR_GAMES},
            "bt": bt, "nx": opp_next, "bye": bye, "mx": mx, "dvp": dvp, "lg": lg, "off": off, "dst": dst, "pl": players,
            "odds": od, "stad": stadium_block(od), "inj": inj_blk}


# ------------------------------------------------------------------ back-test
def _bt_predict(hist, prev_g, prev_m, prior_ppg, week):
    gl = [p for (wk, p) in hist if wk < week]
    return project(prior_ppg, prev_g, prev_m, gl)[0]


def backtest(season, cache, f):
    out = {}
    try:
        for y in (season - 3, season - 2):
            fetch(f"{BASE}/stats_player/stats_player_week_{y}.csv", f(f"week_{y}.csv"))
        data = {y: load_week(f(f"week_{y}.csv")) for y in (season - 3, season - 2, season - 1, season)}
    except Exception as e:  # network/data problem: report nothing rather than guess
        print("backtest skipped:", e)
        return None
    for y, lo, hi in ((season - 2, 4, 17), (season - 1, 4, 17), (season, 3, 99)):
        cur, prev = data[y][data[y].position.isin(OFF)], data[y - 1][data[y - 1].position.isin(OFF)]
        pg = prev.groupby("player_id").agg(g=("pts", "size"), m=("pts", "mean"), pos=("position", "last"))
        prior = {p: float(pg[(pg.pos == p) & (pg.g >= 4)].m.median()) for p in OFF}
        hist = collections.defaultdict(list)
        for r in cur.sort_values("week").itertuples(): hist[r.player_id].append((r.week, r.pts))
        errs, base_last, base_now = [], [], []
        for r in cur[(cur.week >= lo) & (cur.week <= hi)].itertuples():
            g, m = (int(pg.at[r.player_id, "g"]), float(pg.at[r.player_id, "m"])) if r.player_id in pg.index else (0, 0.0)
            pr = _bt_predict(hist[r.player_id], g, m, prior[r.position], r.week)
            if pr is None: pr = prior[r.position]  # debut: only the positional baseline is known
            errs.append(pr - r.pts)
            if g: base_last.append(m - r.pts)
            gl = [p for (wk, p) in hist[r.player_id] if wk < r.week]
            if gl: base_now.append(float(np.mean(gl)) - r.pts)
        if not errs: continue
        rm = lambda e: round(float(np.sqrt(np.mean(np.square(e)))), 2)
        out[str(y)] = {"n": len(errs), "rmse": rm(errs), "mae": round(float(np.mean(np.abs(errs))), 2),
                       "last": rm(base_last) if base_last else None, "now": rm(base_now) if base_now else None}
    return out or None


def embed(html_path, data):
    s = open(html_path, encoding="utf-8").read()
    a, b = s.find("/*PD:START*/"), s.find("/*PD:END*/")
    if a < 0 or b < 0:
        sys.exit("markers /*PD:START*/ and /*PD:END*/ not found in " + html_path)
    blob = "const PD=" + json.dumps(data, separators=(",", ":"), ensure_ascii=False) + ";"
    s = s[:a] + "/*PD:START*/" + blob + s[b:]
    open(html_path, "w", encoding="utf-8").write(s)
    print("embedded", len(blob) // 1024, "KB into", html_path)


FORCE = False
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=datetime.date.today().year)
    ap.add_argument("--cache", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "nflverse_cache"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "player_data.json"))
    ap.add_argument("--embed", help="path to fantasy-edge.html (or the app's index.html) to patch")
    ap.add_argument("--refresh", action="store_true", help="re-download even if cached")
    a = ap.parse_args()
    FORCE = a.refresh
    os.makedirs(a.cache, exist_ok=True)
    data = build(a.season, a.cache)
    json.dump(data, open(a.out, "w"), separators=(",", ":"), ensure_ascii=False)
    print(f"wrote {a.out}: {len(data['pl'])} players, week {data['wk']}, through week {data['thru']}")
    if a.embed: embed(a.embed, data)
