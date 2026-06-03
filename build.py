#!/usr/bin/env python3
"""
Ablo Studio Marketing OS - site generator.

Reads the curated strategy (content.json) and merges in LIVE data:
  - PostHog experiments  (REST, key from ~/.claude/.env)
  - Meta campaign metrics (read from the ablo-ads-autopilot local state)
  - the autopilot's funnel intelligence (insights.json)

Writes data.js  ->  window.ABLO_OS = {...}, which index.html renders.

Design goals: stdlib only, and every live source degrades gracefully. If a
pull fails we log it and fall back to the curated content, so the site never
breaks. Run weekly by the launchd routine (see refresh.sh).
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTENT = HERE / "content.json"
# OUT and HISTORY go to OS_DATA_DIR when set (a writable Railway volume in the
# cloud); otherwise next to the repo as before. Keeps local behaviour identical.
_DATA = Path(os.environ.get("OS_DATA_DIR", HERE))
OUT = _DATA / "data.js"
HISTORY = _DATA / "history.jsonl"  # append-only daily time-series (one row per UTC day)

# The ablo-ads-autopilot keeps fresh Meta state here (refreshed every 6h).
AUTOPILOT = Path(
    "/Users/alejo/Documents/Claude/Brain/projects/ablo/Ablo Studio/autopilot/state"
)
ENV_FILE = Path.home() / ".claude" / ".env"

log = lambda m: print(f"[build] {m}", file=sys.stderr)


def tidy(s):
    """Light cleanup of machine-generated copy: no double-hyphen dashes, no em dashes."""
    if not s:
        return s
    return s.replace(" -- ", ", ").replace("--", ", ").replace(" — ", ", ").replace("—", ", ")


def load_env(path):
    """Parse `export KEY='value'` / `KEY=value` lines without sourcing a shell."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line[7:].strip() if line.startswith("export ") else line
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip("'").strip('"')
    return env


# ---------------------------------------------------------------- PostHog ----
def fetch_experiments(env):
    """Return a list of live experiments, or [] on any failure."""
    key = env.get("POSTHOG_PERSONAL_API_KEY")
    pid = env.get("POSTHOG_PROJECT_ID", "419152")
    if not key:
        log("no POSTHOG_PERSONAL_API_KEY; skipping live experiments")
        return []
    host = env.get("POSTHOG_HOST", "")
    region = "eu" if "eu" in host.lower() else "us"
    base = f"https://{region}.posthog.com"
    url = f"{base}/api/projects/{pid}/experiments/?limit=50"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        log(f"PostHog fetch failed ({e}); falling back to curated experiments")
        return []

    out = []
    for ex in payload.get("results", []):
        if ex.get("archived") or ex.get("deleted"):
            continue
        start, end = ex.get("start_date"), ex.get("end_date")
        if end:
            status = "Complete"
        elif start:
            status = "Running"
        else:
            status = "Draft"
        desc = (ex.get("description") or "").strip()
        m = re.search(r"[Pp]rimary metric[:\s]+([A-Za-z0-9_ ]+)", desc)
        metric = m.group(1).strip().rstrip(".") if m else _first_metric_name(ex)
        out.append(
            {
                "id": f"PH-{ex.get('id')}",
                "name": ex.get("name", "Untitled experiment"),
                "status": status,
                "flag": ex.get("feature_flag_key") or "",
                "hypothesis": desc,
                "metric": metric,
                "started": _short_date(start),
                "url": f"{base}/project/{pid}/experiments/{ex.get('id')}",
            }
        )
    log(f"PostHog: {len(out)} experiment(s) live")
    return out


def _first_metric_name(ex):
    for field in ("metrics", "metrics_secondary"):
        arr = ex.get(field) or []
        if arr and isinstance(arr, list) and isinstance(arr[0], dict):
            return arr[0].get("name") or ""
    return ""


def _short_date(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %-d, %Y")
    except ValueError:
        return iso[:10]


# ------------------------------------------------------------------- Meta ----
def fetch_meta():
    """Pull lifetime numbers + delivery health + funnel intelligence from the autopilot."""
    meta = {
        "spend": None,
        "signups": None,
        "cpl": None,
        "status": "Unknown",
        "asOf": "",
        "deliveryFlag": "",
        "funnelHeadline": "",
        "funnelSuggestions": [],
    }

    latest = AUTOPILOT / "LATEST.md"
    if latest.exists():
        txt = latest.read_text()
        m = re.search(
            r"Lifetime:\s*\$([\d,.]+)\s*spend,\s*([\d,]+)\s*signups,\s*CPL\s*\$([\d.]+)",
            txt,
        )
        if m:
            meta["spend"] = f"${m.group(1)}"
            meta["signups"] = int(m.group(2).replace(",", ""))
            meta["cpl"] = f"${m.group(3)}"
            log(f"Meta: lifetime {meta['spend']} / {meta['signups']} signups / {meta['cpl']} CPL")

    cycle = AUTOPILOT / "last-cycle.json"
    if cycle.exists():
        try:
            c = json.loads(cycle.read_text())
            meta["asOf"] = _short_date(c.get("written_at", ""))
            flags = (c.get("plan") or {}).get("flags") or []
            delivery = [f for f in flags if f.get("kind") == "DELIVERY_HEALTH"]
            if delivery:
                meta["status"] = "Delivery paused"
                meta["deliveryFlag"] = delivery[0].get("reason", "")
            else:
                meta["status"] = "Live"
        except (ValueError, KeyError) as e:
            log(f"last-cycle.json parse issue: {e}")

    ins = AUTOPILOT / "insights.json"
    if ins.exists():
        try:
            data = json.loads(ins.read_text())
            meta["funnelHeadline"] = tidy(data.get("headline", ""))
            for s in (data.get("suggestions") or [])[:4]:
                meta["funnelSuggestions"].append(
                    {
                        "step": s.get("step", ""),
                        "severity": s.get("severity", ""),
                        "title": tidy(s.get("title", "")),
                        "evidence": tidy(s.get("evidence", "")),
                    }
                )
            log(f"Meta: funnel intelligence + {len(meta['funnelSuggestions'])} suggestion(s)")
        except (ValueError, KeyError) as e:
            log(f"insights.json parse issue: {e}")

    return meta


# --------------------------------------------------------------- PostHog HQL --
def _hogql(env, query):
    """Run a HogQL query, return result rows (list of lists). None on failure."""
    key = env.get("POSTHOG_PERSONAL_API_KEY")
    pid = env.get("POSTHOG_PROJECT_ID", "419152")
    if not key:
        return None
    host = env.get("POSTHOG_HOST", "")
    region = "eu" if "eu" in host.lower() else "us"
    url = f"https://{region}.posthog.com/api/projects/{pid}/query/"
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": query}}).encode()
    try:
        req = urllib.request.Request(
            url, data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode()).get("results", [])
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        log(f"HogQL failed ({e})")
        return None


# Canonical happy-path stage -> events. Keys MUST match funnelCurated stages.
FUNNEL_STAGES = [
    ("land",     ["$pageview"]),
    ("engage",   ["cta_clicked", "book_call_clicked", "surprise_me_clicked"]),
    ("intent",   ["signup_modal_opened"]),
    ("signup",   ["signup_completed"]),
    ("studio",   ["studio_entered"]),
    ("model",    ["model_generated"]),
    ("import",   ["product_imported", "product_url_submitted", "product_scrape_succeeded"]),
    ("tryon",    ["tryon_completed"]),
    ("download", ["result_downloaded", "results_downloaded_all"]),
    ("pricing",  ["pricing_plan_clicked"]),
    ("checkout", ["checkout_started"]),
]


def fetch_funnel(env, base):
    """Overlay live per-stage reach (4 windows) + the same-user activation
    spine onto the curated funnel block. Returns the curated base unchanged
    if PostHog is unreachable."""
    all_events = sorted({e for _, evs in FUNNEL_STAGES for e in evs})
    ev_list = ", ".join(f"'{e}'" for e in all_events)
    cases = []
    for key, evs in FUNNEL_STAGES:
        cond = (f"event = '{evs[0]}'" if len(evs) == 1
                else "event IN (" + ", ".join(f"'{e}'" for e in evs) + ")")
        cases.append(f"{cond}, '{key}'")
    multi = "multiIf(" + ", ".join(cases) + ", 'other')"
    reach_q = f"""
        SELECT stage,
          count(DISTINCT if(timestamp >= now() - INTERVAL 7 DAY, person_id, NULL)) AS d7,
          count(DISTINCT if(timestamp >= now() - INTERVAL 30 DAY, person_id, NULL)) AS d30,
          count(DISTINCT if(timestamp >= now() - INTERVAL 90 DAY, person_id, NULL)) AS d90,
          count(DISTINCT person_id) AS dall
        FROM (
          SELECT person_id, timestamp, {multi} AS stage
          FROM events
          WHERE timestamp >= now() - INTERVAL 365 DAY AND event IN ({ev_list})
        )
        WHERE stage != 'other'
        GROUP BY stage
    """.strip()
    rows = _hogql(env, reach_q)
    if not rows:
        return base

    counts = {r[0]: {"d7": int(r[1]), "d30": int(r[2]), "d90": int(r[3]), "all": int(r[4])}
              for r in rows if r and r[0]}

    spine_q = """
        SELECT countIf(s>0) a, countIf(s>0 AND en>0) b, countIf(s>0 AND mo>0) c,
               countIf(s>0 AND im>0) d, countIf(s>0 AND ty>0) e, countIf(s>0 AND dl>0) f,
               countIf(s>0 AND pr>0) g, countIf(s>0 AND ch>0) h
        FROM (
          SELECT person_id,
            maxIf(1, event='signup_completed') s, maxIf(1, event='studio_entered') en,
            maxIf(1, event='model_generated') mo,
            maxIf(1, event IN ('product_imported','product_url_submitted')) im,
            maxIf(1, event='tryon_completed') ty,
            maxIf(1, event IN ('result_downloaded','results_downloaded_all')) dl,
            maxIf(1, event='pricing_plan_clicked') pr, maxIf(1, event='checkout_started') ch
          FROM events WHERE timestamp >= now() - INTERVAL 365 DAY GROUP BY person_id
        )
    """.strip()
    spine_rows = _hogql(env, spine_q)

    import copy
    funnel = copy.deepcopy(base)
    funnel["source"] = "PostHog · live HogQL"
    funnel["updated"] = datetime.now(timezone.utc).strftime("%B %-d, %Y")
    for stage in funnel.get("stages", []):
        c = counts.get(stage["key"])
        if c:
            stage["counts"] = c

    if spine_rows and spine_rows[0]:
        v = [int(x) for x in spine_rows[0]]
        denom = v[0] or 1
        steps = funnel.get("spine", {}).get("steps", [])
        for i, step in enumerate(steps):
            if i < len(v):
                step["count"] = v[i]
                step["pct"] = round(v[i] / denom * 100)
        funnel["spine"]["denominator"] = v[0]
    log(f"PostHog funnel: {len(counts)} stages live")
    return funnel


# ------------------------------------------------------------------- Klaviyo --
KLAVIYO_REV = "2024-10-15"
TRYON_METRIC = "T3S8Cw"  # 'Try-on Completed' — the aha, used as flow conversion metric


def _klaviyo(key, path, method="GET", body=None):
    url = f"https://a.klaviyo.com/api/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Klaviyo-API-Key {key}",
        "revision": KLAVIYO_REV, "accept": "application/json",
        "content-type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode())


def fetch_lifecycle(env, base):
    """Overlay live Klaviyo state (flow statuses, the live Ablo flow's
    messages, prepared lifecycle templates) onto the curated lifecycle block.
    Analysis fields (opportunities, note, message 'read') stay curated."""
    key = env.get("KLAVIYO_API_KEY_ABLO")
    if not key:
        return base
    import copy
    life = copy.deepcopy(base)
    try:
        flows = _klaviyo(key, "flows/?fields%5Bflow%5D=name,status,trigger_type,created&page%5Bsize%5D=50").get("data", [])
    except Exception as e:
        log(f"Klaviyo flows failed ({e})")
        return base

    live_ablo, draft_ablo, other = [], [], []
    for f in flows:
        a = f.get("attributes", {})
        name, status = a.get("name", ""), a.get("status", "")
        low = name.lower()
        if any(x in low for x in ("clawoop", "launchpad", "christmas", "essential flow")):
            other.append(name)
        elif status == "live" and "ablo" in low:
            entry = {"flow": name, "id": f.get("id"), "trigger": a.get("triggerType", ""),
                     "since": _short_date(a.get("created", "")), "status": "live"}
            # Pre-seed messages/read/agg from the curated match so the card
            # always renders even if the live report overlay misses.
            cm = next((x for x in base.get("liveFlows", []) if x.get("id") == entry["id"]), None)
            if cm:
                entry["messages"] = cm.get("messages", [])
                entry["read"] = cm.get("read", "")
                entry["agg"] = cm.get("agg", {})
            live_ablo.append(entry)
        elif status == "draft" and ("ablo" in low or "welcome series" in low):
            draft_ablo.append({"name": name, "trigger": a.get("triggerType", ""), "status": "draft",
                               "note": "Built but never turned on."})

    # Per-message performance for each live Ablo flow (best-effort).
    for lf in live_ablo:
        try:
            report = _klaviyo(key, "flow-values-reports/", "POST", {
                "data": {"type": "flow-values-report", "attributes": {
                    "statistics": ["recipients", "open_rate", "click_rate", "conversions", "conversion_uniques", "unsubscribes"],
                    "timeframe": {"key": "last_90_days"},
                    "conversion_metric_id": TRYON_METRIC,
                    "filter": f"equals(flow_id,\"{lf['id']}\")",
                    "group_by": ["flow_id", "flow_message_id", "flow_message_name"],
                }}})
            results = report.get("data", {}).get("attributes", {}).get("results", [])
            agg = report.get("data", {}).get("attributes", {}).get("flow_aggregation", [])
            msgs, seen = [], {}
            for r in results:
                g, s = r.get("groupings", {}), r.get("statistics", {})
                nm = g.get("flow_message_name", "Message")
                if s.get("recipients", 0) < 2:
                    continue  # skip stray test variations
                seen[nm] = seen.get(nm, 0) + 1
                msgs.append({"name": nm, "timing": "—",
                             "recipients": int(s.get("recipients", 0)),
                             "open": round(s.get("open_rate", 0) * 100, 1),
                             "click": round(s.get("click_rate", 0) * 100, 1),
                             "conv": round(s.get("conversion_rate", 0) * 100, 1),
                             "unsub": int(s.get("unsubscribes", 0))})
            if msgs:
                # carry timing + read from curated message order where possible
                curated_msgs = next((x["messages"] for x in base.get("liveFlows", [])
                                     if x.get("id") == lf["id"]), [])
                for i, m in enumerate(msgs):
                    if i < len(curated_msgs):
                        m["timing"] = curated_msgs[i].get("timing", "—")
                lf["messages"] = msgs
                lf["read"] = next((x.get("read", "") for x in base.get("liveFlows", [])
                                   if x.get("id") == lf["id"]), "")
            if agg:
                s = agg[0].get("statistics", {})
                lf["agg"] = {"recipients": int(s.get("recipients", 0)),
                             "open": round(s.get("open_rate", 0) * 100, 1),
                             "click": round(s.get("click_rate", 0) * 100, 1),
                             "conv": int(s.get("conversions", 0)),
                             "convUniques": int(s.get("conversion_uniques", 0)),
                             "convLabel": "Try-on completed"}
        except Exception as e:
            log(f"Klaviyo flow report failed for {lf['id']} ({e}); keeping curated messages")
            cm = next((x for x in base.get("liveFlows", []) if x.get("id") == lf["id"]), None)
            if cm:
                lf["messages"], lf["read"], lf["agg"] = cm.get("messages", []), cm.get("read", ""), cm.get("agg", {})

    # Prepared (built, unwired) lifecycle templates.
    prepared = []
    try:
        tpls = _klaviyo(key, "templates/?fields%5Btemplate%5D=name,updated&page%5Bsize%5D=10").get("data", [])
        for t in tpls:
            a = t.get("attributes", {})
            nm = a.get("name", "")
            if nm.startswith("[Ablo Lifecycle]"):
                grp = "Activate series" if "Activate" in nm else "AHA series" if "AHA" in nm else "Lifecycle"
                maps = "signup → model gap" if grp == "Activate series" else "model → try-on gap" if grp == "AHA series" else ""
                prepared.append({"name": nm, "group": grp, "maps": maps,
                                 "updated": _short_date(a.get("updated", ""))})
    except Exception as e:
        log(f"Klaviyo templates failed ({e})")

    if live_ablo:
        life["liveFlows"] = live_ablo
    # Only let live override the curated prepared list when it is at least as
    # complete (the templates endpoint paginates without sort, so a short page
    # can under-count). Curated stays authoritative otherwise.
    if len(prepared) >= len(base.get("prepared", [])):
        life["prepared"] = prepared
    if draft_ablo:
        life["draftFlows"] = draft_ablo
    if other:
        life["otherProduct"] = sorted(set(other))
    life["source"] = "Klaviyo · live API"
    life["updated"] = datetime.now(timezone.utc).strftime("%B %-d, %Y")
    log(f"Klaviyo: {len(live_ablo)} live flow(s), {len(prepared)} prepared template(s)")
    return life


# ------------------------------------------------------------------ ClickUp --
ABLO_STUDIO_LIST = "901415977874"  # ClickUp · Space Runners (Ablo) · "Ablo Studio" list


def fetch_clickup(env):
    """Live task feed from the Ablo Studio ClickUp list (source of truth for
    action items). Read-only. None on failure."""
    key = env.get("CLICKUP_TOKEN_ABLO")
    if not key:
        return None
    url = (f"https://api.clickup.com/api/v2/list/{ABLO_STUDIO_LIST}/task"
           "?archived=false&include_closed=false&subtasks=false&order_by=due_date")
    try:
        req = urllib.request.Request(url, headers={"Authorization": key})
        with urllib.request.urlopen(req, timeout=30) as r:
            tasks = json.loads(r.read().decode()).get("tasks", [])
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        log(f"ClickUp fetch failed ({e})")
        return None

    counts, rows = {}, []
    for t in tasks:
        st = (t.get("status") or {}).get("status", "unknown")
        counts[st] = counts.get(st, 0) + 1
        due = t.get("due_date")
        rows.append({
            "name": t.get("name", ""),
            "status": st,
            "color": (t.get("status") or {}).get("color", ""),
            "type": (t.get("status") or {}).get("type", ""),
            "url": t.get("url", ""),
            "due": _short_date(datetime.fromtimestamp(int(due) / 1000, timezone.utc).isoformat()) if due else "",
            "assignee": (t.get("assignees") or [{}])[0].get("username", "").strip() if t.get("assignees") else "",
        })
    # surface in-progress first, then to-do, capped — the live execution layer
    order = {"in progress": 0, "to do": 1}
    active = [r for r in rows if r["type"] != "closed" and r["type"] != "done"]
    active.sort(key=lambda r: (order.get(r["status"], 2), r["due"] or "9"))
    log(f"ClickUp: {len(tasks)} task(s) · {counts}")
    return {"source": "ClickUp · live", "updated": datetime.now(timezone.utc).strftime("%B %-d, %Y"),
            "listUrl": f"https://app.clickup.com/9003194404/v/li/{ABLO_STUDIO_LIST}",
            "counts": counts, "open": active[:12], "total": len(tasks)}


# -------------------------------------------------------------- Instagram ----
IG_ACCOUNT = "17841404306089983"  # @ablo.ai business account


def fetch_instagram(env):
    """Live Instagram organic stats via the Meta Graph API (the ads token has
    account-read scope). Post-level engagement + publishing need an IG token
    with instagram_content_publish — currently expired. None on failure."""
    token = env.get("META_ADS_TOKEN")
    if not token:
        return None
    url = (f"https://graph.facebook.com/v21.0/{IG_ACCOUNT}"
           f"?fields=username,followers_count,media_count&access_token={token}")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.loads(r.read().decode())
        if "error" in d:
            return None
        log(f"Instagram: @{d.get('username')} {d.get('followers_count')} followers")
        return {"username": d.get("username"), "followers": d.get("followers_count"),
                "posts": d.get("media_count"), "source": "Meta Graph · live",
                "canPost": False, "postNote": "Posting + post-level engagement need an IG token with instagram_content_publish scope (the META_IG_TOKEN is expired, refresh it to enable agent posting)."}
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        log(f"Instagram fetch failed ({e})")
        return None


# ----------------------------------------------------------------- channels --
CHANNEL_Q = """
SELECT coalesce(nullIf(properties.utm_source, ''), '(direct)') AS source,
  count(DISTINCT person_id) AS users,
  count(DISTINCT if(event = 'signup_completed', person_id, NULL)) AS signups,
  count(DISTINCT if(event = 'tryon_completed', person_id, NULL)) AS tryons,
  count(DISTINCT if(event = 'checkout_started', person_id, NULL)) AS checkouts
FROM events
WHERE timestamp >= toDateTime('2026-05-01 00:00:00')
GROUP BY source
""".strip()


def _channel_name(src):
    s = (src or "").lower()
    if "meta" in s or s in ("fb", "facebook"):
        return "Meta Ads"
    if "linkedin" in s:
        return "LinkedIn"
    if s in ("ig", "instagram"):
        return "Instagram (organic)"
    if "google" in s or "adwords" in s:
        return "Google"
    if "email" in s or "klaviyo" in s:
        return "Email"
    if "direct" in s:
        return "Direct / untagged"
    return (src or "Other").title()


def fetch_channel_attribution(env):
    """Live per-channel acquisition from PostHog UTM stamps, tied through to
    signups, try-ons and checkouts. None on failure."""
    rows = _hogql(env, CHANNEL_Q)
    if not rows:
        return None
    agg = {}
    for r in rows:
        if not r:
            continue
        name = _channel_name(r[0])
        a = agg.setdefault(name, {"channel": name, "users": 0, "signups": 0, "tryons": 0, "checkouts": 0})
        a["users"] += int(r[1]); a["signups"] += int(r[2]); a["tryons"] += int(r[3]); a["checkouts"] += int(r[4])
    chans = sorted(agg.values(), key=lambda x: x["signups"], reverse=True)
    total_su = sum(c["signups"] for c in chans) or 1
    for c in chans:
        c["signupShare"] = round(c["signups"] / total_su * 100)
    top = chans[0] if chans else None
    insight = ""
    if top and top["signupShare"] >= 40:
        insight = (f"{top['signupShare']}% of signups come from {top['channel']}"
                   + (" — acquisition is dominated by untagged / organic traffic, not paid. "
                      "Tag founder posts and referral links with UTMs to see what is really working, "
                      "and weigh whether paid is earning its share."
                      if top["channel"].startswith("Direct") else "."))
    log(f"PostHog channels: {len(chans)} source(s), top {top['channel'] if top else '-'}")
    return {"attribution": chans, "insight": insight,
            "updated": datetime.now(timezone.utc).strftime("%B %-d, %Y"), "source": "PostHog UTM · live"}


# ------------------------------------------------------------------ history --
# Daily distinct-user reach per stage, reconstructed in full from the PostHog
# event log on every run (self-healing — no drift, no dedupe needed). Non-
# reconstructable fields (Meta cost, email rates, cumulative rates) are
# persisted forward from today in history.jsonl.
DAILY_Q = """
SELECT toString(toDate(timestamp)) AS d,
  count(DISTINCT if(event = '$pageview', person_id, NULL)) AS landed,
  count(DISTINCT if(event IN ('cta_clicked','book_call_clicked','surprise_me_clicked'), person_id, NULL)) AS engaged,
  count(DISTINCT if(event = 'signup_modal_opened', person_id, NULL)) AS modal,
  count(DISTINCT if(event = 'signup_completed', person_id, NULL)) AS signups,
  count(DISTINCT if(event = 'model_generated', person_id, NULL)) AS models,
  count(DISTINCT if(event IN ('product_imported','product_url_submitted'), person_id, NULL)) AS imports,
  count(DISTINCT if(event = 'tryon_completed', person_id, NULL)) AS tryons,
  count(DISTINCT if(event IN ('result_downloaded','results_downloaded_all'), person_id, NULL)) AS downloads,
  count(DISTINCT if(event = 'checkout_started', person_id, NULL)) AS checkouts
FROM events
WHERE timestamp >= toDateTime('2026-05-19 00:00:00')
GROUP BY d ORDER BY d
""".strip()

# Fields that cannot be recomputed from the event log — persisted day by day.
PERSIST_KEYS = ["spend_lifetime", "cpl", "signups_meta", "email_open", "email_click",
                "email_recipients", "aha_rate", "activation_rate", "payment_rate",
                "paying_customers", "ig_followers"]
PH_COLS = ["landed", "engaged", "modal", "signups", "models", "imports", "tryons", "downloads", "checkouts"]


def _money(s):
    try:
        return round(float(str(s).replace("$", "").replace(",", "")), 2)
    except (ValueError, TypeError, AttributeError):
        return None


def snapshot_history(env, funnel, meta_live, lifecycle, instagram=None):
    """Upsert today's row and rewrite history.jsonl. Returns the last 120 days
    for embedding. PostHog reach is recomputed in full; Meta/Klaviyo/rate
    fields persist forward."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1) PostHog daily reach (full history, authoritative)
    rows_ph = _hogql(env, DAILY_Q) or []
    ph = {}
    for r in rows_ph:
        if r and r[0]:
            ph[r[0]] = {PH_COLS[i]: int(r[i + 1]) for i in range(len(PH_COLS))}

    # 2) read previously persisted (non-reconstructable) fields
    persisted = {}
    if HISTORY.exists():
        for line in HISTORY.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            d = row.get("date")
            if d:
                persisted[d] = {k: row[k] for k in PERSIST_KEYS if row.get(k) is not None}

    # 3) today's persisted fields from the live pulls
    rates = {}
    for s in (funnel.get("spine", {}) or {}).get("steps", []):
        lab = (s.get("label", "") or "").lower()
        if s.get("aha"):
            rates["aha_rate"] = s.get("pct")
        elif s.get("payment"):
            rates["payment_rate"] = s.get("pct")
        elif "model" in lab:
            rates["activation_rate"] = s.get("pct")
    agg = ((lifecycle.get("liveFlows") or [{}])[0] or {}).get("agg", {})
    today_fields = {
        "spend_lifetime": _money(meta_live.get("spend")),
        "cpl": _money(meta_live.get("cpl")),
        "signups_meta": meta_live.get("signups"),
        "email_open": agg.get("open"),
        "email_click": agg.get("click"),
        "email_recipients": agg.get("recipients"),
        "paying_customers": 0,
        "ig_followers": (instagram or {}).get("followers"),
        **rates,
    }
    persisted[today] = {k: v for k, v in today_fields.items() if v is not None}

    # 4) merge across the union of dates and rewrite
    dates = sorted(set(ph) | set(persisted))
    rows = []
    for d in dates:
        row = {"date": d}
        row.update(ph.get(d, {}))
        row.update(persisted.get(d, {}))
        rows.append(row)
    HISTORY.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    log(f"history: {len(rows)} day(s) ({dates[0] if dates else '-'} → {today})")
    return {"rows": rows[-120:], "updated": today, "phLive": bool(rows_ph)}


# ---------------------------------------------------- before/after ship ------
def fetch_signup_experiment(env, base):
    """Before/after read on the 'Google primary' signup-modal ship (2026-05-27).
    Full rollout, not a PostHog A/B, so we measure signup-modal completion before
    vs after the ship date. Returns base with a live signal, or base on failure."""
    SHIP = "2026-05-27"
    q = (
        "SELECT "
        "count(DISTINCT if(event='signup_modal_opened' AND ts <  toDateTime('%(s)s'), pid, NULL)) AS ob, "
        "count(DISTINCT if(event='signup_completed'    AND ts <  toDateTime('%(s)s'), pid, NULL)) AS sb, "
        "count(DISTINCT if(event='signup_modal_opened' AND ts >= toDateTime('%(s)s'), pid, NULL)) AS oa, "
        "count(DISTINCT if(event='signup_completed'    AND ts >= toDateTime('%(s)s'), pid, NULL)) AS sa "
        "FROM (SELECT person_id AS pid, timestamp AS ts, event FROM events "
        "WHERE event IN ('signup_modal_opened','signup_completed') AND timestamp >= toDateTime('2026-05-18'))"
    ) % {"s": SHIP}
    rows = _hogql(env, q)
    out = dict(base)
    if not rows or not rows[0]:
        return out
    try:
        ob, sb, oa, sa = [int(x) for x in rows[0]]
    except (ValueError, TypeError):
        return out
    before = (sb / ob * 100) if ob else 0.0
    after = (sa / oa * 100) if oa else 0.0
    delta = after - before
    note = " Low sample while delivery is paused, directional only." if (ob + oa) < 80 else ""
    out["signal"] = (
        f"Modal completion before May 27: {before:.0f}% ({sb}/{ob}). "
        f"After: {after:.0f}% ({sa}/{oa}). Change: {delta:+.0f} pts.{note}"
    )
    log(f"signup before/after: {before:.0f}% -> {after:.0f}% ({delta:+.0f} pts)")
    return out


# ------------------------------------------------------------------ build ----
def build():
    content = json.loads(CONTENT.read_text())
    live_experiments = fetch_experiments(load_env(ENV_FILE))
    meta_live = fetch_meta()

    # Tokens: process environment as the base (Railway env vars / the cloud
    # token store), with ~/.claude/.env overlaid when present (local machine).
    env = {**os.environ, **load_env(ENV_FILE)}
    posthog_live = bool(live_experiments)
    if posthog_live:
        experiments = live_experiments
    else:
        # Graceful fallback: accurate last-known experiments from content.json
        # (auto-upgrades to live once the PostHog key gets experiment:read scope).
        experiments = content.get("experimentsCurated", {}).get("liveFallback", [])

    # Tracked before/after ship (not a PostHog A/B): the Google-primary signup change.
    sx = content.get("experimentsCurated", {}).get("signupExperiment")
    if sx:
        experiments = list(experiments) + [fetch_signup_experiment(env, sx)]

    # Live product funnel (PostHog) and lifecycle (Klaviyo), overlaid on the
    # curated fallbacks. Each degrades to its curated block on failure.
    funnel = fetch_funnel(env, content.get("funnelCurated", {}))
    lifecycle = fetch_lifecycle(env, content.get("lifecycleCurated", {}))
    funnel_live = funnel.get("source", "").startswith("PostHog · live")
    klaviyo_live = lifecycle.get("source", "").startswith("Klaviyo · live")

    # Live channel attribution from PostHog UTMs (ties each source through to
    # signup / try-on / checkout). None on failure.
    channels_live = fetch_channel_attribution(env)

    # ClickUp task feed (source of truth for action items) + IG organic stats.
    clickup = fetch_clickup(env)
    instagram = fetch_instagram(env)

    # Daily time-series — snapshot today and rewrite history.jsonl.
    history = snapshot_history(env, funnel, meta_live, lifecycle, instagram)

    # Activation = same-user signup -> try-on (the aha), straight from the funnel.
    activation = "~47%"
    try:
        spine = funnel.get("spine", {}).get("steps", [])
        aha = next((s for s in spine if s.get("aha")), None)
        if aha:
            activation = f"~{aha['pct']}%"
    except (KeyError, TypeError):
        pass

    signups = meta_live["signups"]
    kpis = [
        {"label": "Paying customers", "value": "0 / 5", "sub": "the brag · CAC < $300", "tone": "accent"},
        {"label": "Lifetime signups", "value": str(signups) if signups is not None else "30", "sub": "all-time, paid", "tone": "default"},
        {"label": "Cost per signup", "value": meta_live["cpl"] or "$21.04", "sub": "target ≤ $20", "tone": "default"},
        {"label": "Activation", "value": activation, "sub": "signup → try-on · target ≥ 50%", "tone": "default"},
        {"label": "Paid spend", "value": meta_live["spend"] or "$631", "sub": "validation budget · $200/wk", "tone": "default"},
        {"label": "Live experiments", "value": str(len(experiments)) if experiments else "1", "sub": "running in PostHog", "tone": "default"},
    ]

    now = datetime.now(timezone.utc)
    content["meta"]["updated"] = now.strftime("%B %-d, %Y")
    content["meta"]["updatedISO"] = now.isoformat()
    # Self-improving Command Center: stamp it reviewed on every run so the action
    # queue always reflects "checked today". Deeper status re-ranking (resolve
    # done items, surface new leaks) is done by the marketing-os-refresh agent
    # skill, which reasons over the live funnel/campaigns/experiments/lifecycle.
    if "commandCenter" in content:
        content["commandCenter"]["updated"] = content["meta"]["updated"]
    content["live"] = {
        "kpis": kpis,
        "experiments": experiments,
        "meta": meta_live,
        "funnel": funnel,
        "lifecycle": lifecycle,
        "channels": channels_live,
        "clickup": clickup,
        "instagram": instagram,
        "history": history,
        "refreshedSources": {
            "posthog": posthog_live,
            "meta": signups is not None,
            "funnel": funnel_live,
            "klaviyo": klaviyo_live,
            "channels": channels_live is not None,
            "clickup": clickup is not None,
            "instagram": instagram is not None,
            "history": history.get("phLive", False),
        },
    }

    banner = (
        "/* AUTO-GENERATED by build.py. Do not edit by hand. */\n"
        "/* Curated strategy lives in content.json; live data refreshes weekly. */\n"
    )
    OUT.write_text(banner + "window.ABLO_OS = " + json.dumps(content, ensure_ascii=False, indent=2) + ";\n")
    log(f"wrote {OUT.name} · updated {content['meta']['updated']}")
    log(f"posthog={'live' if posthog_live else 'cached'} meta={'live' if signups is not None else 'cached'}")


if __name__ == "__main__":
    build()
