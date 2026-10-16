# Fantasy Edge: installable app package

Files: `index.html` (the site), `manifest.webmanifest`, `sw.js` (offline support), `icons/`.

## Put it online free (pick one)
- **Netlify:** go to app.netlify.com/drop and drag this whole folder onto the page. You get an https link.
- **GitHub Pages:** create a repo, upload these files, then Settings > Pages > deploy from the main branch.
- **Vercel / Cloudflare Pages:** create a project and upload the folder (no build step needed).
It must be served over https (all of the above do this) for install and offline to work.

## Install on a phone
- **iPhone (Safari):** open your link > Share > Add to Home Screen.
- **Android (Chrome):** open your link > menu > Install app (or Add to Home screen).

## Notes
- It opens full screen with the red FE icon and works offline after the first visit.
- Your roster and loaded team lists are saved in that browser/app only.
- The screenshot import button only works inside Claude, so it will be hidden on your own site.
- After you change `index.html`, bump `CACHE = "fantasy-edge-v1"` in `sw.js` (to v2, v3...) so phones pick up the update.
- Push notifications need the Supabase + OneSignal backend and an Add-to-Home-Screen install on iPhone (iOS 16.4+).

## Player data: where the numbers come from, and how to refresh them
Every player's 2025 and 2026 fantasy points, the opponent for each game, the defense-vs-position matchup ranks, current
teams/roster status and the projections come from **nflverse**, a free public NFL dataset
(https://github.com/nflverse/nflverse-data). The numbers are baked into `index.html` between the `/*PD:START*/` and
`/*PD:END*/` markers, so the site works with no server. They are only as fresh as the last run. After each week's games:

    pip install pandas numpy          # Windows also needs: pip install tzdata
    python3 build_player_data.py --embed index.html       # downloads about 35 MB, rewrites the data block in index.html
    python3 -W ignore -m unittest test_build_player_data   # optional: checks the scoring and projection math

Then re-upload `index.html` and bump the cache name in `sw.js` (for example `fantasy-edge-v37`) so phones pick up the new copy.
Also update the hand-entered pieces that are not part of this script: injury report (`INJS`), news links, weather kickoffs.

**Projection model** (all weights were chosen by back-testing on 2024, 2025 and early 2026, about 10,000 player-games):
a weighted average of real game scores. Each 2026 game counts 1 (the 3 most recent count 2), each 2025 game counts 0.5, plus
one pseudo-game at the position's typical points per game (20 for kickers and defenses, which are mostly luck).
Typical miss: about 6.1 to 6.2 points per game, versus 6.5 to 7.0 using only last season's average. Matchups move the
**Value** number by up to 15% but are not part of the projected points, because in back-tests they improved accuracy only slightly.
If you load Sleeper's projections (Rosters app), those replace the model's number.

Scoring: PPR for QB/RB/WR/TE. Kickers: FG 3 (0 to 39 yards), 4 (40 to 49), 5 (50+), extra point 1. Defenses: sack 1,
interception 2, fumble recovery 2, touchdown 6, safety 2, blocked kick 2, plus points allowed (0 = 10, 1 to 6 = 7, 7 to 13 = 4,
14 to 20 = 1, 21 to 27 = 0, 28 to 34 = -1, 35+ = -4).

Known limits: no depth-chart, snap-count or game-script information; rookies and practice-squad players with no NFL games
show "Data unavailable"; if a player changes teams the stats follow him but the old team's matchup history does not.

## Dashboard apps and the Odds tab
- **Dashboard** is an app launcher. Tap an icon (Rankings, Projections, Start / Sit, Trade Analyzer, Injuries, Rosters, Players,
  News, Weather, Results) and it opens full screen. The **Back** button at the top right returns to the launcher, and so does the
  phone's own Back gesture. Old links such as `#players` still open the right app.
- **Odds** tab: every game of the week with the money line, point spread, total and each team's chance to win. Tap a game for
  its page: the chance-to-win bar, a line chart of how the money lines rated both teams in every game this season (filled dot =
  won, hollow = lost; tap a dot for details), a curve of likely final margins built from the point spread, and the full lines.
  Weeks 1 to 7 are listed; finished games show the score and flag upsets.

**Where the odds come from.** The money lines, spreads and totals are the betting columns of the nflverse schedule file
(`away_moneyline`, `home_moneyline`, `spread_line`, `total_line`, ...), embedded in `index.html` as `PD.odds` by
`build_player_data.py`. nflverse does not say which sportsbook they come from or whether they are opening or closing lines.
They are a **snapshot, not a live feed**: the page shows when they were downloaded, real lines move until kickoff, and
games without a posted line show "Data unavailable". The schedule file is re-downloaded on every run of the script, so run
`python3 build_player_data.py --embed index.html` as often as you like (it is free) to refresh lines and final scores.

**How the percentages work.** Chance to win = the money line turned into a probability, then divided by the two teams' total so
they add up to 100% (that removes the sportsbook's built-in margin, about 4% in 2026). The margin curve is centered on the
point spread and is as wide as real results have been: 2,127 games from 2018 to 2025 landed with a standard deviation of
12.78 points around the spread. The curve and the money line usually land within a few points of each other (the game page shows both and the gap). As a reality check,
from 2018 to 2025 favorites the lines priced at 55-60% won 60% of the time, 70-75% won 75%, 85%+ won 91%.
Everything on the Odds tab is information only, not betting advice.

## Standings, weather and injuries are automatic now
- **Standings, top 10, conference tables, best team of the week** are calculated in the page from the real final scores (`PD.odds`). The old typed-in tables are gone. The calculation matched the typed records for all 32 teams.
- **Weather** lists the current week's games and their stadiums from the schedule file. Stadium locations are in `STADIUMS` in `build_player_data.py` (fixed facts; an unknown stadium shows "Data unavailable"). Forecasts still load on your device from Open-Meteo.
- **Injuries** come from the NFL's official injury report (nflverse `injuries_<season>.csv`, `PD.inj`). Typed-in news notes (`INJS` in `index.html`) are kept only for 3 days after their date and the more serious status wins.
- **Still typed in by hand:** the news links.
