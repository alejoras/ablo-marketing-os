# Ablo Studio · Marketing OS

The single internal home for Ablo Studio's marketing strategy and live execution.
One page, ten sections, on the real Ablo Studio design system. Built so the team
(and the team's content agents) always work from the same canon.

**Internal and confidential.** `noindex` + `robots.txt` disallow. Do not share the
link publicly.

## What's inside

| Group | Sections |
|---|---|
| Strategy | Overview · Goals & OKRs · ICP & Segments · Positioning · Competition |
| Brand | Messaging & Perceptions · Brand Voice (with a copy-paste voice card for agents) |
| Execution | Experiments (PostHog) · Campaigns (Meta) · Content Strategy |

## How it works (hybrid data)

- **Curated strategy** lives in [`content.json`](content.json). Human-edited. Positioning,
  ICP, messaging, voice, goals, competition. This rarely changes and is never auto-rewritten.
- **Live data** is merged in by [`build.py`](build.py) each week:
  - **Campaigns** + the KPI strip read the `ablo-ads-autopilot` local state (lifetime
    spend / signups / CPL, delivery health, and the auto-generated funnel intelligence).
  - **Experiments** read PostHog. (See "Live PostHog" below — currently a cached snapshot.)
- `build.py` writes [`data.js`](data.js) (`window.ABLO_OS = {...}`), which `index.html` renders.
  Every live source degrades gracefully: if a pull fails, the last good value stays and the
  site never breaks.

```
content.json  ─┐
                ├─► build.py ─► data.js ─► index.html (renders)
live sources  ─┘
```

## Editing the strategy

Edit `content.json`, then run `python3 build.py` and refresh the page. No build tools,
no dependencies (stdlib Python only). The weekly job will also pick up your edits.

## The weekly refresh

`refresh.sh` runs `build.py`, then commits and pushes so GitHub Pages redeploys.
It is scheduled by launchd every **Monday 09:00** via
`~/Library/LaunchAgents/com.alejo.ablo-marketing-os.weekly.plist`.

```bash
# run it now
./refresh.sh
# check / load / unload the schedule
launchctl list | grep ablo-marketing-os
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.alejo.ablo-marketing-os.weekly.plist
launchctl bootout  gui/$(id -u) ~/Library/LaunchAgents/com.alejo.ablo-marketing-os.weekly.plist
```

Logs: `.refresh.log` (build + git output).

## Live PostHog experiments

`build.py` pulls experiments via the PostHog REST API using `POSTHOG_PERSONAL_API_KEY`
from `~/.claude/.env`. That key currently lacks the `experiment:read` and
`feature_flag:read` scopes, so the Experiments tab shows an accurate **cached snapshot**
(labeled as such). To switch to fully live: PostHog → Settings → Personal API keys →
add the `experiment:read` and `feature_flag:read` scopes. No code change needed; the next
refresh will show "live."

## Publishing (GitHub Pages)

Static site, no build step. Files that ship: `index.html`, `data.js`, `assets/`,
`robots.txt`, `.nojekyll`. Repo is private; Pages serves the built site.

```bash
git add -A && git commit -m "update" && git push origin main
```

GitHub Pages caveat: on a private repo the Pages **site URL** is still public unless you
are on GitHub Enterprise with access control. The repo source stays private, the page is
`noindex`, and no personal/HR data is included. For stricter gating, put the page behind
Cloudflare Access or a similar auth proxy.

## Files

| File | Role |
|---|---|
| `index.html` | The app: CSS + render logic (self-contained) |
| `content.json` | Curated strategy (human-edited) |
| `data.js` | Generated. `window.ABLO_OS`. Do not hand-edit. |
| `build.py` | Generator: content.json + live sources → data.js |
| `refresh.sh` | Weekly wrapper (build + commit + push) |
| `assets/` | Logo |

## Source of truth

Curated content is distilled from the marketing strategy spine and the MVC in
`Brain/projects/ablo/Ablo Studio/`. When the strategy there changes materially, update
`content.json` to match. Personal, HR, and non-marketing content is intentionally excluded.
