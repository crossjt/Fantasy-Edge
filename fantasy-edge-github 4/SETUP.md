# Put Fantasy Edge on GitHub so it refreshes itself (free)

You do not need to write code. Everything is already in this folder.

1. Go to github.com and sign in (or click Sign up, it is free).
2. Click the + at the top right, then New repository. Name it fantasy-edge. Choose Public. Click Create repository.
3. On the new page click "uploading an existing file". Unzip this folder first, then drag EVERYTHING inside it into the page.
   The hidden folder named .github must go in too. If your computer hides it, turn on "show hidden files",
   or drag the whole folder contents from a file manager that shows it. Click Commit changes.
4. Click Settings, then Pages (left side). Under Build and deployment choose "Deploy from a branch",
   branch main, folder / (root), then Save. After about a minute your site address appears at the top of that page.
5. Click the Actions tab. If it asks, click "I understand my workflows, enable them".
   Click "Refresh Fantasy Edge data" on the left, then "Run workflow" to test it. A green check means it worked.
6. Open your site address on your phone. To install it as an app: Share > Add to Home Screen (iPhone) or menu > Install app (Android).

After that it refreshes about every 30 minutes by itself. Open the site and pull down to reload to see new data.

## What refreshes and what does not
Refreshes by itself: schedule, final scores, betting lines, standings and rankings (calculated from the real scores), every player's fantasy points,
projections, matchup ranks, rosters, the weather game list (games and stadiums), and the injury list (the NFL's official report, via nflverse).
Does NOT refresh: the news links, and the short injury notes that were typed in from news (those expire on their own after 3 days, so an old note
can never override a newer official report). The news links need updating by hand (ask Claude).

## Honest limits
- Data is only as fresh as the free nflverse files, which update after games and overnight. This is not live to the second. The official injury report only has the days the NFL has published so far this week, and covers the teams that have reported.
- GitHub can run the timer a few minutes late. If nothing changed, nothing is published.
- It has not been run on your GitHub yet. Step 5 is the test. If it shows a red X, click it and send me the message.
- Free GitHub accounts have a monthly limit on automatic run time. This job is small, but if you ever see a limit message, change */30 to */60 in .github/workflows/refresh.yml.
