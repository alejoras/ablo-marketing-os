#!/usr/bin/env python3
"""
gen_marketing_call.py -- draft the Wednesday marketing-call agenda from the Marketing OS.

Reads the live Marketing OS data (data.js -> window.ABLO_OS) plus history.jsonl
and prints paste-able, bullet-point sections for Alejo's weekly Ablo Studio
marketing call. It does NOT call any API: run `python3 build.py` first if you
want fresh numbers. The `marketing-call` skill orchestrates that and folds in
ClickUp "done this week" + Alejo's own context.

Output sections (no emojis, no em dashes -- house style):
  SCOREBOARD  -- the numbers, week-over-week, with a one-line "so what"
  SHIPPED LAST WEEK  -- proof of progress (from the Command Center)
  WHAT WE LEARNED  -- the funnel/experiment insight
  FOCUS THIS WEEK  -- top Command Center priorities + content/blog pipeline
  ASKS / DEPENDENCIES  -- decisions for founders, product deps, blockers

Usage:  python3 gen_marketing_call.py
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))


def load_os():
    """Parse window.ABLO_OS out of data.js."""
    path = os.path.join(HERE, "data.js")
    raw = open(path, encoding="utf-8").read()
    raw = raw.split("window.ABLO_OS =", 1)[1].strip().rstrip().rstrip(";")
    return json.loads(raw)


def load_history():
    """Daily funnel rows, oldest -> newest."""
    path = os.path.join(HERE, "history.jsonl")
    rows = []
    if not os.path.exists(path):
        return rows
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    rows.sort(key=lambda r: r.get("date", ""))
    return rows


def wow(rows):
    """Sum trailing-7 days vs the prior-7 days for key funnel metrics."""
    if not rows:
        return {}
    today = date.today()
    cur_lo = today - timedelta(days=7)
    prev_lo = today - timedelta(days=14)
    fields = ["landed", "engaged", "signups", "models", "tryons", "checkouts"]
    cur = {f: 0 for f in fields}
    prev = {f: 0 for f in fields}
    for r in rows:
        try:
            d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        bucket = None
        if cur_lo <= d <= today:
            bucket = cur
        elif prev_lo <= d < cur_lo:
            bucket = prev
        if bucket is not None:
            for f in fields:
                bucket[f] += r.get(f, 0) or 0
    out = {}
    for f in fields:
        delta = cur[f] - prev[f]
        sign = "+" if delta >= 0 else ""
        out[f] = (cur[f], f"{sign}{delta} vs prior 7d")
    return out


def line(s=""):
    print(s)


def section_scoreboard(d, hist):
    live = d.get("live", {})
    kpis = live.get("kpis", [])
    w = wow(hist)
    line("**SCOREBOARD** (last 7 days)")
    for k in kpis:
        label = k.get("label", "")
        val = k.get("value", "")
        sub = k.get("sub", "")
        extra = ""
        # attach week-over-week to the signup-count metric only (not CPL/cost)
        ll = label.lower()
        if "signup" in ll and "cost" not in ll and "cpl" not in ll and "signups" in w:
            extra = f"  ({w['signups'][1]})"
        suffix = f" -- {sub}" if sub else ""
        line(f"- {label}: {val}{suffix}{extra}")
    # explicit funnel WoW the KPIs do not cover
    if w:
        landed = w.get("landed", (0, ""))
        tryons = w.get("tryons", (0, ""))
        line(f"- Traffic: {landed[0]} landed last 7d ({landed[1]})")
        line(f"- Try-ons (AHA): {tryons[0]} last 7d ({tryons[1]})")
    # delivery / so-what
    meta = live.get("meta", {})
    if meta.get("status"):
        flag = meta.get("deliveryFlag", "")
        line(f"- Paid status: {meta['status']}" + (f" -- {flag}" if flag else ""))
    head = (meta.get("funnelHeadline") or "").strip()
    if head:
        first = head.split(". ")[0].strip()
        line("")
        line(f"SO WHAT: {first}.")


def section_action_items(d):
    """Progress on the action items committed at Monday's all-hands.
    Convention: those items carry a due date in the current week. The skill
    fills the live list from ClickUp (tasks due this week, assigned to Alejo);
    any open[] tasks already dated this week are shown here as a head start."""
    line("**THIS WEEK'S ACTION ITEMS** (set Monday, status today)")
    cu = d.get("live", {}).get("clickup", {})
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    shown = 0
    for t in cu.get("open", []):
        due_ms = t.get("due")
        if not due_ms:
            continue
        try:
            due = datetime.fromtimestamp(int(due_ms) / 1000).date()
        except (ValueError, TypeError):
            continue
        if monday <= due <= sunday:
            line(f"- {t.get('name')} -- {t.get('status')}")
            shown += 1
    line("- [skill fills: ClickUp tasks due this week (assigned to Alejo) + status]")
    if shown == 0:
        line("- (none dated this week yet -- give Monday's action items a due date so they land here)")


def section_shipped(d):
    cc = d.get("commandCenter", {})
    items = cc.get("items", [])
    shipped = [
        it for it in items
        if it.get("sev") == "done"
        or "ship" in str(it.get("status", "")).lower()
    ]
    line("**SHIPPED LAST WEEK**")
    if shipped:
        for it in shipped:
            line(f"- {it.get('title')} -- {it.get('status')}")
    else:
        line("- (none flagged shipped in the Command Center)")
    line("- [skill fills: ClickUp tasks completed in the last 7 days]")


# Distinctive funnel tokens used to match an autopilot insight to a shipped
# Command Center fix (so we stop calling an already-fixed leak "the top leak").
_FIX_TOKENS = {"signup", "magic", "magic-link", "modal", "google", "activation",
               "studio", "rageclick", "rage", "checkout", "pricing", "import",
               "url", "scrape", "lifecycle", "email"}


def _shipped_topics(d):
    """Token sets from Command Center items whose fix has shipped/is shipping."""
    out = []
    for it in d.get("commandCenter", {}).get("items", []):
        status = str(it.get("status", "")).lower()
        shipped = it.get("sev") == "done" or any(
            w in status for w in ("ship", "fix", "deploy", "improv", "resolved"))
        if shipped:
            title = str(it.get("title", "")).lower()
            toks = {t for t in _FIX_TOKENS if t in title}
            if toks:
                out.append((it.get("title", ""), it.get("status", ""), toks))
    return out


def section_learned(d):
    live = d.get("live", {})
    meta = live.get("meta", {})
    sugg = meta.get("funnelSuggestions", []) or []
    shipped = _shipped_topics(d)
    line("**WHAT WE LEARNED**")
    if not sugg:
        line("- (no fresh funnel insight this cycle)")
        return
    for s in sugg[:2]:
        title = s.get("title", "")
        ev = (s.get("evidence", "") or "").split(". ")[0].strip()
        text = (title + " " + s.get("step", "")).lower()
        s_toks = {t for t in _FIX_TOKENS if t in text}
        # If this insight maps to a shipped fix, present it as fixed, not as a leak.
        match = next((cc for cc in shipped if s_toks & cc[2]), None)
        if match:
            line(f"- FIX SHIPPED, watching: {title} (Command Center: {match[0]} -- {match[1]})")
        else:
            line(f"- {title}")
        if ev:
            line(f"  Evidence: {ev}.")
        # Rage-click metric caveat: until the autopilot re-runs with the
        # Surprise-Me exclusion, this count can include by-design repeat clicks.
        if "rage" in text or "studio" in text:
            line("  Caveat: rage-click count may still include by-design 'Surprise me' clicks until the next autopilot cycle.")


def section_in_progress(d):
    """Status of everything moving: board counts + active items + content pipeline."""
    cu = d.get("live", {}).get("clickup", {})
    counts = cu.get("counts", {})
    line("**IN PROGRESS / PIPELINE** (Ablo Studio board)")
    if counts:
        order = ["in progress", "review", "to do", "done"]
        parts = [f"{counts[k]} {k}" for k in order if k in counts]
        line(f"- Board: {' · '.join(parts)} ({cu.get('total', '?')} total)")
    # active (in-progress) items -- the real "being worked on now" view
    active = [t for t in cu.get("open", []) if t.get("status") == "in progress"]
    for t in active:
        line(f"- Active: {t.get('name')}")
    if cu.get("listUrl"):
        line(f"- Board: {cu['listUrl']}")
    # content / blog pipeline, next 7 days
    cal = d.get("contentCalendar", {})
    today = date.today()
    horizon = today + timedelta(days=7)
    upcoming = []
    for it in cal.get("items", []):
        try:
            di = datetime.strptime(it.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= di <= horizon:
            upcoming.append((di, it))
    upcoming.sort(key=lambda x: x[0])
    if upcoming:
        line("")
        line("Content / blog pipeline (next 7 days):")
        for di, it in upcoming:
            owner = it.get("owner", "")
            owner_s = f" ({owner})" if owner else ""
            line(f"- {di.isoformat()} · {it.get('channel')}: {it.get('title')} [{it.get('status')}]{owner_s}")


def section_focus(d):
    cc = d.get("commandCenter", {})
    items = [it for it in cc.get("items", []) if it.get("sev") != "done"]
    # rank order, top 3 -- the bets to call out, not the whole board
    items = sorted(items, key=lambda it: str(it.get("rank", "99")))[:3]
    line("**FOCUS THIS WEEK** (the bets, with status)")
    for it in items:
        line(f"- {it.get('title')} -- {it.get('status')}")


def section_asks(d):
    cc = d.get("commandCenter", {})
    items = cc.get("items", [])
    asks = []
    for it in items:
        status = str(it.get("status", "")).lower()
        if "block" in status:
            asks.append(f"- BLOCKER: {it.get('title')} -- {it.get('status')}")
    line("**ASKS / DEPENDENCIES**")
    if asks:
        for a in asks:
            line(a)
    line("- [skill fills: decisions for Deniz/Won, product deps on Jason, enterprise overlap with Michael, Alejo's own asks]")


def section_discussion():
    """Always-present slot for Alejo's own thoughts, questions, and opinions.
    The skill must ask him to fill this before finalizing -- these rarely live
    in ClickUp or the data."""
    line("**DISCUSSION / MY TAKE**")
    line("- [skill asks Alejo: anything on your mind -- questions, opinions, ideas,")
    line("  things not in ClickUp or the data -- that you want on the agenda?]")


def main():
    try:
        d = load_os()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not read data.js ({e}). Run `python3 build.py` first.", file=sys.stderr)
        sys.exit(1)
    hist = load_history()
    meta = d.get("meta", {})
    line(f"Ablo Studio -- marketing call draft  (OS updated {meta.get('updated', '?')})")
    line("=" * 60)
    line()
    section_scoreboard(d, hist)
    line()
    section_action_items(d)
    line()
    section_shipped(d)
    line()
    section_learned(d)
    line()
    section_in_progress(d)
    line()
    section_focus(d)
    line()
    section_asks(d)
    line()
    section_discussion()


if __name__ == "__main__":
    main()
