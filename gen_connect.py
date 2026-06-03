#!/usr/bin/env python3
"""
Add KPI-laddering to the Command Center, an objectives anchor, and seed the
Content Calendar block. Re-runnable: overwrites the fields it owns.
"""
import json
from collections import OrderedDict
from pathlib import Path

CONTENT = Path(__file__).resolve().parent / "content.json"

# Each action item ladders to a specific lever + KPI. Keyed by rank.
LADDER = {
    "1": "Lever 2 · Conversion → signup-to-paid ≥ 8%",
    "2": "Lever 2 · Conversion → signup-to-paid ≥ 8%",
    "3": "Lever 2 · Activation → signup-to-activation ≥ 50%",
    "4": "Lever 2/3 · Activation-to-paid + ARPU",
    "5": "Measurement · unblocks signup-to-paid (the make-or-break KPI)",
    "6": "Lever 1 · Grow top-of-funnel → CPL ≤ $20",
    "7": "The brag · ARPU ≥ $50, validate willingness to pay",
}

OBJECTIVES = {
    "northStar": "First paying customer plus a repeatable channel at stable CAC.",
    "brag": "5 paying customers · CAC < $300 · ARPU ≥ $50",
    "kpis": [
        {"k": "Paying customers", "v": "0 / 5"},
        {"k": "Signup → paid", "v": "≥ 8%"},
        {"k": "Signup → activation", "v": "≥ 50%"},
        {"k": "CPL", "v": "≤ $20"},
    ],
    "rule": "Every item below earns its place by moving one of these. The agent ranks by goal-impact: anything that does not ladder up to a KPI does not belong in the queue.",
}

CALENDAR = OrderedDict([
    ("updated", "June 3, 2026"),
    ("note", "Seeded scaffold. The source of truth will be ClickUp — a reminder task is queued to wire this to a ClickUp calendar. Until then, edit items here. Instagram publishing by the agent needs an IG token refresh (instagram_content_publish scope)."),
    ("channels", ["Instagram", "LinkedIn", "Email", "Blog", "Campaign"]),
    ("items", [
        {"date": "2026-06-04", "channel": "Instagram", "title": "Kids try-on before/after reel", "status": "planned", "owner": "Marketing"},
        {"date": "2026-06-05", "channel": "LinkedIn",  "title": "Founder post: the photoshoot is becoming optional", "status": "planned", "owner": "Deniz"},
        {"date": "2026-06-06", "channel": "Email",     "title": "Launch AHA series (B1-B3) behavioral flow", "status": "planned", "owner": "Marketing"},
        {"date": "2026-06-09", "channel": "Instagram", "title": "Swim fit on diverse bodies carousel", "status": "planned", "owner": "Marketing"},
        {"date": "2026-06-10", "channel": "Email",     "title": "Launch Activate series (A1-A3) behavioral flow", "status": "planned", "owner": "Marketing"},
        {"date": "2026-06-11", "channel": "LinkedIn",  "title": "Founder post: editorial AI for fashion brands", "status": "planned", "owner": "Won"},
        {"date": "2026-06-13", "channel": "Blog",      "title": "Case study: a kids brand ships a drop without a shoot", "status": "planned", "owner": "Marketing"},
        {"date": "2026-06-16", "channel": "Instagram", "title": "12-pose model showcase", "status": "planned", "owner": "Marketing"},
        {"date": "2026-06-18", "channel": "Campaign",  "title": "Wave 3 paid launch — Kids + Swim", "status": "planned", "owner": "Marketing"},
        {"date": "2026-06-20", "channel": "LinkedIn",  "title": "Founder post: what '73% direct/organic' taught us", "status": "planned", "owner": "Deniz"},
        {"date": "2026-06-23", "channel": "Email",     "title": "Post-aha upgrade nudge (price-ask cohort)", "status": "planned", "owner": "Marketing"},
        {"date": "2026-06-25", "channel": "Instagram", "title": "Surprise Me feature spotlight", "status": "planned", "owner": "Marketing"},
    ]),
])


def main():
    data = json.loads(CONTENT.read_text(), object_pairs_hook=OrderedDict)
    cc = data.get("commandCenter", {})

    cc["intro"] = ("The prioritized action queue, anchored to the goal. Every live funnel leak is tied "
                   "to the one fix that moves it and to the KPI it ladders up to, so priority always "
                   "means goal-impact. This is the surface the daily routine rewrites as it reads the "
                   "funnel, campaigns, experiments and lifecycle and learns which fixes moved which number.")
    cc["objectives"] = OBJECTIVES
    for it in cc.get("items", []):
        it["ladder"] = LADDER.get(it.get("rank", ""), "")
    cc["loop"] = ("The daily routine: read the live funnel, campaigns, experiments and lifecycle, re-rank "
                  "this queue by impact on the KPIs above, update the status of every in-flight item, sync "
                  "against ClickUp (the task source of truth), and write the result back. Over time it "
                  "correlates each shipped fix with the KPI it moved, so the ranking gets smarter on its own.")

    data["commandCenter"] = cc

    # splice contentCalendar after commandCenter
    out = OrderedDict()
    for k, v in data.items():
        if k == "contentCalendar":
            continue
        out[k] = v
        if k == "commandCenter":
            out["contentCalendar"] = CALENDAR
    if "contentCalendar" not in out:
        out["contentCalendar"] = CALENDAR

    CONTENT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print("updated commandCenter (ladder + objectives) and seeded contentCalendar:", len(CALENDAR["items"]), "items")


if __name__ == "__main__":
    main()
