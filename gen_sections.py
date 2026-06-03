#!/usr/bin/env python3
"""
Seed the curated FALLBACK blocks for the funnel, lifecycle, channels and
command-center sections into content.json.

These are real snapshots captured 2026-06-03 from PostHog (product funnel)
and Klaviyo (lifecycle). build.py overlays LIVE data on top of `funnelCurated`
and `lifecycleCurated` on every refresh; `channels` and `commandCenter` are
the analyst/agent-written surfaces and stay curated until the daily routine
rewrites them.

Re-runnable: splices the four keys into content.json after `battlecard`,
replacing any prior copy.
"""
import json
from collections import OrderedDict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTENT = HERE / "content.json"

# Window keys are shared by the curated fallback and the live build.
WINDOWS = ["d7", "d30", "d90", "all"]
WINDOW_LABELS = {"d7": "7 days", "d30": "30 days", "d90": "90 days", "all": "Since launch"}


def W(d7, rest):
    """Build a per-window count dict. 30d/90d == all because all data sits
    inside the last 30 days (launch was 2026-05-20)."""
    return {"d7": d7, "d30": rest, "d90": rest, "all": rest}


FUNNEL = OrderedDict([
    ("updated", "June 3, 2026"),
    ("source", "PostHog · cached snapshot"),
    ("windows", WINDOWS),
    ("windowLabels", WINDOW_LABELS),
    ("note", "Distinct users reaching each milestone, ordered along the happy path. Reach (not strict order), so a later step can exceed an earlier one when users take a side path (try-on via Surprise Me without importing, checkout from pricing without downloading). The activation spine below is the same-user, strictly monotonic view."),
    ("ahaKey", "tryon"),
    ("paymentKey", "checkout"),
    ("stages", [
        {"key": "land",     "label": "Landed",            "sub": "$pageview",          "group": "Acquire",  "counts": W(332, 752)},
        {"key": "engage",   "label": "Engaged",           "sub": "cta_clicked",        "group": "Acquire",  "counts": W(81, 161), "benchmark": "1 in 5 visitors engage"},
        {"key": "intent",   "label": "Opened signup",     "sub": "signup_modal_opened","group": "Acquire",  "counts": W(76, 148)},
        {"key": "signup",   "label": "Signed up",         "sub": "signup_completed",   "group": "Acquire",  "counts": W(34, 64), "benchmark": "8.5% land→signup (target 5%)"},
        {"key": "studio",   "label": "Entered studio",    "sub": "studio_entered",     "group": "Activate", "counts": W(46, 80)},
        {"key": "model",    "label": "Generated a model", "sub": "model_generated",    "group": "Activate", "counts": W(23, 43), "benchmark": "67% signup→model (target 50%)"},
        {"key": "import",   "label": "Imported a product","sub": "product_imported",   "group": "Activate", "counts": W(14, 29)},
        {"key": "tryon",    "label": "Tried on",          "sub": "tryon_completed",    "group": "Aha",      "counts": W(11, 30), "aha": True},
        {"key": "download", "label": "Downloaded result", "sub": "result_downloaded",  "group": "Value",    "counts": W(2, 5)},
        {"key": "pricing",  "label": "Clicked pricing",   "sub": "pricing_plan_clicked","group": "Pay",     "counts": W(1, 8)},
        {"key": "checkout", "label": "Started checkout",  "sub": "checkout_started",   "group": "Pay",      "counts": W(1, 8), "payment": True},
    ]),
    ("spine", {
        "label": "Activation spine",
        "note": "Same user, of everyone who signed up (since launch). Strictly monotonic, this is the cleanest drop story.",
        "denominator": 64,
        "steps": [
            {"label": "Signed up",          "count": 64, "pct": 100},
            {"label": "Entered studio",     "count": 64, "pct": 100},
            {"label": "Generated a model",  "count": 43, "pct": 67},
            {"label": "Imported a product", "count": 29, "pct": 45},
            {"label": "Tried on (aha)",     "count": 30, "pct": 47, "aha": True},
            {"label": "Downloaded result",  "count": 5,  "pct": 8},
            {"label": "Clicked pricing",    "count": 7,  "pct": 11},
            {"label": "Started checkout",   "count": 7,  "pct": 11, "payment": True},
        ],
    }),
    ("drops", [
        {"sev": "med",  "title": "Engagement gap (land → engage)", "rate": "21%",
         "detail": "Only 161 of 752 visitors click any CTA. The hero is not converting the visit into intent.",
         "fix": "Sharpen the hero promise and CTA; lead from the bodies-you-cannot-shoot wedge."},
        {"sev": "high", "title": "Signup-modal leak (open → signup)", "rate": "43%",
         "detail": "148 open the signup modal, only 64 finish. Email magic-link leaked almost everyone (retired); the in-modal drop is the standing lever.",
         "fix": "Google one-click as the primary, full-width button; demote email. Ship mobile-first (71% of opens)."},
        {"sev": "high", "title": "Activation gap (signup → model)", "rate": "67%",
         "detail": "1 in 3 signups never generate a model. The largest rage-click cluster by far sits on /studio (10 users, 30 clicks).",
         "fix": "Find the control that looks clickable but stalls before the first Generate. Coachmarks experiment is live."},
        {"sev": "high", "title": "Value-capture leak (try-on → download)", "rate": "17%",
         "detail": "Only 5 of 30 users who reach the aha export a result. The aha fires but the value never leaves the app.",
         "fix": "Add a post-try-on download / share prompt the moment the image renders."},
        {"sev": "high", "title": "Payment + measurement (try-on → checkout)", "rate": "23%",
         "detail": "7 of 30 aha-reachers start checkout, and 0 purchases are confirmed because no success event fires.",
         "fix": "In-product upgrade prompt at the aha, plus the price-ask test. Instrument purchase_completed."},
    ]),
    ("gaps", [
        "No purchase_completed / subscription_started event is instrumented. checkout_started (8 users) is the deepest tracked step, so true paid conversion and revenue cannot be read from product analytics yet. Instrument it to close the loop.",
        "studio_entered (80) exceeds signup_completed (64) because the studio is reachable by returning and anonymous sessions. The activation spine corrects for this by counting same-user, signup-anchored.",
    ]),
])

LIFECYCLE = OrderedDict([
    ("updated", "June 3, 2026"),
    ("source", "Klaviyo · cached snapshot"),
    ("note", "Klaviyo already receives the product events as metrics (Model Generated, Try-on Completed, Checkout Started), so behavioral triggers are possible today. Only the Added-to-List onboarding flow is live; the behavioral flows that would fix the funnel are unbuilt even though the emails exist."),
    ("liveFlows", [
        {
            "flow": "Ablo Studio — Onboarding", "id": "TG3ii9", "trigger": "Added to List",
            "since": "May 19, 2026", "status": "live",
            "agg": {"recipients": 80, "open": 97.5, "click": 2.5, "conv": 19, "convUniques": 11, "convLabel": "Try-on completed"},
            "messages": [
                {"name": "Welcome Email", "timing": "On signup (Day 0)", "recipients": 53, "open": 96.2, "click": 3.8, "conv": 17.0, "unsub": 1},
                {"name": "Follow up",     "timing": "After a delay (verify in Klaviyo)", "recipients": 26, "open": 100.0, "click": 0.0, "conv": 7.7, "unsub": 0},
            ],
            "read": "Elite open rates (96-100%) but near-zero clicks (2.5%). The emails get seen and do not pull anyone back into the product. Add one clear, single CTA per email tied to the next funnel step.",
        },
    ]),
    ("prepared", [
        {"name": "[Ablo Lifecycle] Activate A1 — one-sentence nudge", "group": "Activate series", "maps": "signup → model gap", "updated": "May 30"},
        {"name": "[Ablo Lifecycle] Activate A2 — kill the blank page",  "group": "Activate series", "maps": "signup → model gap", "updated": "May 30"},
        {"name": "[Ablo Lifecycle] Activate A3 — value reframe",        "group": "Activate series", "maps": "signup → model gap", "updated": "May 30"},
        {"name": "[Ablo Lifecycle] AHA B1 — the missing half",          "group": "AHA series",      "maps": "model → try-on gap", "updated": "May 30"},
        {"name": "[Ablo Lifecycle] AHA B2 — show the outcome",          "group": "AHA series",      "maps": "model → try-on gap", "updated": "May 30"},
        {"name": "[Ablo Lifecycle] AHA B3 — last nudge + offer help",   "group": "AHA series",      "maps": "model → try-on gap", "updated": "May 30"},
    ]),
    ("draftFlows", [
        {"name": "Welcome Series - Standard", "trigger": "Added to List", "status": "draft", "note": "Ablo-relevant, built Apr 2, never turned on."},
    ]),
    ("otherProduct", ["Clawoop Welcome Series", "New Launchpad Subs - Nano Creators", "Christmas Popup Flow", "Essential Flow Recommendation (×3, unconfigured)"]),
    ("opportunities", [
        {"title": "Activate the 'generated a model, never tried on' segment", "trigger": "Model Generated metric", "exit": "Try-on Completed",
         "use": "Wire the AHA B1-B3 templates into a 3-step flow.", "impact": "Targets the model → try-on drop. The emails already exist."},
        {"title": "Rescue the 'signed up, never generated' segment", "trigger": "Added to List, no Model Generated in 24h", "exit": "Model Generated",
         "use": "Wire the Activate A1-A3 templates.", "impact": "Targets the 33% activation gap, the biggest post-signup leak."},
        {"title": "Convert the 'tried on, never checked out' segment", "trigger": "Try-on Completed", "exit": "Checkout Started",
         "use": "New value / upgrade email at the aha moment.", "impact": "Targets the 77% who reach the aha but never start payment."},
    ]),
])

CHANNELS = OrderedDict([
    ("updated", "June 3, 2026"),
    ("intro", "Every acquisition and retention surface in one scorecard, so budget concentration is a decision, not a guess. The open question the OS exists to answer: which one channel plus audience to pour budget into."),
    ("rows", [
        {"name": "Meta / Instagram Ads", "role": "Paid · primary (~80%)", "status": "Paused", "tone": "warn",
         "metrics": [["Lifetime spend", "$631"], ["Signups", "30"], ["CPL", "$21"]],
         "read": "Flight ended May 29 and delivery broke May 30-31 (billing). Kids is the cheapest, healthiest segment; Swim has the best CTR (2.97%). Run by the ads autopilot on a 6h cron."},
        {"name": "LinkedIn (founder-led)", "role": "Organic · secondary (~20%)", "status": "Dormant", "tone": "muted",
         "metrics": [["Vehicle", "Founder posts"], ["US CTR", "12.2%"], ["EU CTR", "3.9-5.5%"]],
         "read": "Wave 1 (Deniz 5/19, Won 5/20) plus ~$300 Thought Leader Ads. US decisively outperforms EU. No standing cadence yet."},
        {"name": "Email / Klaviyo", "role": "Lifecycle · activation", "status": "Live, underbuilt", "tone": "accent",
         "metrics": [["Live flows", "1"], ["Open rate", "97.5%"], ["Click rate", "2.5%"]],
         "read": "One onboarding flow live. 6 lifecycle emails built but wired to nothing. The cheapest untapped lever in the stack."},
        {"name": "Organic / Content", "role": "Owned · stub", "status": "Not started", "tone": "muted",
         "metrics": [["Engine", "None yet"], ["By design", "BD-led plan"]],
         "read": "No content engine by design. The product's own output (before/after, shot-in-minutes) is the latent, infinite fuel when content goes in scope."},
    ]),
    ("decision", "Evidence points to Meta + Kids/Swim as the paid wedge to concentrate on once delivery is restored, with email as the cheapest immediate lever (the activation and aha flows are already built, just unwired). LinkedIn stays founder-led and US-only. Organic is deferred."),
])

COMMAND = OrderedDict([
    ("updated", "June 3, 2026"),
    ("intro", "The prioritized action queue: every live funnel drop tied to the one fix that moves it, who owns it, and where it stands. This is the surface the daily routine rewrites as it reads the funnel, campaigns, experiments and lifecycle and learns which fixes moved which number."),
    ("items", [
        {"rank": "1", "sev": "high", "title": "Wire the prepared lifecycle emails", "owner": "Marketing", "status": "Ready — fastest lever",
         "targets": "signup → model · model → try-on",
         "body": "The Activate (A1-A3) and AHA (B1-B3) templates exist but sit in no flow. Build two behavioral flows in Klaviyo (Added-to-List → Activate, Model Generated → AHA). Zero new copy, ships this week."},
        {"rank": "2", "sev": "high", "title": "Fix the signup-modal leak", "owner": "Product", "status": "Shipping",
         "targets": "open → signup (43%)",
         "body": "Promote Google one-click to the primary, full-width button and demote email. Mobile-first. Tracked by the signup experiment."},
        {"rank": "3", "sev": "high", "title": "Close the activation gap", "owner": "Product", "status": "Investigating",
         "targets": "signup → model (67%)",
         "body": "Pull /studio session replays on the non-activators and find the control that stalls before the first Generate. Rage-click cluster confirmed. Coachmarks experiment live."},
        {"rank": "4", "sev": "high", "title": "Stop the value-capture leak", "owner": "Product / Marketing", "status": "New",
         "targets": "try-on → download (17%)",
         "body": "Only 5 of 30 aha-reachers export a result. Add a download / share prompt the instant the try-on renders, so the value leaves the app."},
        {"rank": "5", "sev": "med", "title": "Instrument purchase_completed", "owner": "Product / Data", "status": "Blocker for revenue truth",
         "targets": "checkout → paid",
         "body": "No paid-conversion event fires, so true signup→paid and revenue cannot be measured. This blocks the make-or-break number the whole strategy hinges on."},
        {"rank": "6", "sev": "med", "title": "Restart Meta delivery, concentrate Kids/Swim", "owner": "Autopilot / Alejo", "status": "Blocked (billing)",
         "targets": "top of funnel",
         "body": "Fix billing in Ads Manager, then let the autopilot reconcentrate spend on the Kids and Swim winners. Two zero-spend days flagged."},
        {"rank": "7", "sev": "med", "title": "Run the price-ask test", "owner": "Alejo", "status": "Queued",
         "targets": "ARPU · willingness to pay",
         "body": "Manually onboard 5 kids and 5 swim founders, watch them hit try-on, then ask what they would pay. Free signups tell us almost nothing about revenue."},
    ]),
    ("loop", "The daily routine: read the live funnel, campaign metrics, experiments and lifecycle, re-rank this queue by leverage against the goal (first paying customer, CAC < $300), update the status of every in-flight item, and write the result back here. Over time it correlates each shipped fix with the drop it was meant to move, so the ranking gets smarter on its own. The agent layer on top reads this surface plus live campaign data and proposes the next action toward the goal."),
    ("sources", [
        {"name": "PostHog (product funnel)", "how": "Live HogQL on each refresh", "tone": "accent"},
        {"name": "Klaviyo (lifecycle)", "how": "Live API on each refresh", "tone": "accent"},
        {"name": "Meta Ads (campaigns)", "how": "Ads autopilot, 6h cron", "tone": "default"},
        {"name": "Experiments", "how": "PostHog experiments API", "tone": "default"},
    ]),
])


def main():
    data = json.loads(CONTENT.read_text(), object_pairs_hook=OrderedDict)
    blocks = OrderedDict([
        ("funnelCurated", FUNNEL),
        ("lifecycleCurated", LIFECYCLE),
        ("channels", CHANNELS),
        ("commandCenter", COMMAND),
    ])
    drop = set(blocks.keys())
    out = OrderedDict()
    for k, v in data.items():
        if k in drop:
            continue  # remove any prior copy
        out[k] = v
        if k == "battlecard":
            for bk, bv in blocks.items():
                out[bk] = bv
    for bk, bv in blocks.items():
        if bk not in out:
            out[bk] = bv
    CONTENT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print("seeded:", ", ".join(blocks.keys()))


if __name__ == "__main__":
    main()
