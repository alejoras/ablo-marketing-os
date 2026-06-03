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
breaks. Run daily by the launchd routine (see refresh.sh).
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
OUT = HERE / "data.js"
HISTORY = HERE / "history.jsonl"  # append-only daily time-series (one row per UTC day)

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
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
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
                # The autopilot grades delivery: "high" = broken right now,
                # "medium"/"low" = a past stall that has since resumed (the flag
                # is informational, not a current pause). Only the high case is
                # an actual pause -- otherwise the campaign is live and the flag
                # reason is shown as a recovery note, not a "paused" alarm.
                d0 = delivery[0]
                reason = d0.get("reason", "")
                meta["deliveryFlag"] = reason
                # A "high" flag can lag reality: if the reason text says delivery
                # has resumed, the campaign is live again even before the next
                # cycle re-grades it. Treat that as Live, not paused.
                resumed = "resume" in reason.lower()
                meta["status"] = "Delivery paused" if (d0.get("severity") == "high" and not resumed) else "Live"
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
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
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


def fetch_magiclink(env):
    """Distinct persons who requested an email magic link, and how many of
    those made it into the app (login OR signup completed). This is the live
    measure behind the 'email path is leaky' claim in the Overview's Current
    Focus. Returns {'req': int, 'in': int} or None on failure (curated
    fallbacks in content.json then keep the narrative intact)."""
    q = """
        SELECT countIf(req>0) AS requested,
               countIf(req>0 AND (login>0 OR su>0)) AS in_app
        FROM (
          SELECT person_id,
            maxIf(1, event='magic_link_requested') req,
            maxIf(1, event='login_completed') login,
            maxIf(1, event='signup_completed') su
          FROM events WHERE timestamp >= now() - INTERVAL 365 DAY
          GROUP BY person_id
        )
    """.strip()
    rows = _hogql(env, q)
    if not rows or not rows[0]:
        return None
    try:
        req, in_app = int(rows[0][0]), int(rows[0][1])
    except (ValueError, TypeError, IndexError):
        return None
    log(f"PostHog magic-link: {req} requested, {in_app} made it in")
    return {"req": req, "in": in_app}


# Fill {{key|fallback}} placeholders from a vars dict. Missing or None values
# fall back to the inline literal, so curated prose survives a failed live pull.
_TPL_RE = re.compile(r"\{\{(\w+)\|([^}]*)\}\}")


def apply_template_vars(text, variables):
    def repl(m):
        v = variables.get(m.group(1))
        return str(v) if v is not None else m.group(2)
    return _TPL_RE.sub(repl, text)


def bind_current_focus(content, funnel, magiclink):
    """Bind the live funnel figures into the Overview's curated Current Focus
    so it can never silently drift from the Funnel tab. Reads the same `funnel`
    object the site renders (which itself falls back to the curated block), plus
    the magic-link conversion. Pure string substitution over {{key|fallback}}."""
    def stage_all(key):
        for s in funnel.get("stages", []):
            if s.get("key") == key:
                return (s.get("counts") or {}).get("all")
        return None

    studio, model = stage_all("studio"), stage_all("model")
    pct = round((studio - model) / studio * 100) if studio and model is not None else None
    variables = {
        "intent": stage_all("intent"),
        "signup": stage_all("signup"),
        "studio": studio,
        "model": model,
        "studioNoModelPct": pct,
        "mlReq": magiclink.get("req") if magiclink else None,
        "mlIn": magiclink.get("in") if magiclink else None,
    }
    cf = content.get("overview", {}).get("currentFocus")
    if isinstance(cf, list):
        content["overview"]["currentFocus"] = [apply_template_vars(s, variables) for s in cf]


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
    # include_closed=true so the queue reconciler can see completed tasks (a card
    # whose work is done in ClickUp should auto-flag, even though the visible feed
    # below only lists the still-open tasks).
    url = (f"https://api.clickup.com/api/v2/list/{ABLO_STUDIO_LIST}/task"
           "?archived=false&include_closed=true&subtasks=false&order_by=due_date")
    try:
        req = urllib.request.Request(url, headers={"Authorization": key})
        with urllib.request.urlopen(req, timeout=30) as r:
            tasks = json.loads(r.read().decode()).get("tasks", [])
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
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
    # `all` is the full compact pool (open + closed) used only by the reconciler;
    # it is popped before the clickup block is embedded in data.js.
    return {"source": "ClickUp · live", "updated": datetime.now(timezone.utc).strftime("%B %-d, %Y"),
            "listUrl": f"https://app.clickup.com/9003194404/v/li/{ABLO_STUDIO_LIST}",
            "counts": counts, "open": active[:12], "total": len(tasks),
            "all": [{"name": r["name"], "status": r["status"], "type": r["type"], "url": r["url"]} for r in rows]}


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
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        log(f"Instagram fetch failed ({e})")
        return None


# ------------------------------------------------------------- conversions ---
def fetch_paying(env):
    """Distinct Studio self-serve paying customers, from the PostHog purchase
    event. That event only fires inside Studio, so it is inherently scoped to
    self-serve, no Stripe charge filtering needed. None on failure (KPI then
    falls back to its curated value)."""
    try:
        rows = _hogql(env, "SELECT count(DISTINCT person_id) FROM events WHERE event = 'purchase_completed'")
    except Exception as e:  # noqa: BLE001 -- never let a slow/failed pull crash the build
        log(f"Conversions fetch failed ({e}); KPI falls back to curated")
        return None
    if not rows:
        return None
    try:
        paying = int(rows[0][0])
    except (IndexError, TypeError, ValueError):
        return None
    log(f"Conversions: {paying} paying customer(s) via PostHog purchase_completed")
    return paying


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


def snapshot_history(env, funnel, meta_live, lifecycle, instagram=None, paying=None):
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
        # Real paying-customer count (PostHog purchase_completed). None on a failed
        # pull -> dropped below so the prior day's value persists (never overwrite
        # a real count with a placeholder 0).
        "paying_customers": paying,
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


# Spend from finished ad flights. The autopilot's spend_lifetime is the CURRENT
# flight only and resets to ~0 when a new flight starts, so a day where it drops
# vs the prior day marks a closed flight; the prior day's value was that flight's
# final spend. This auto-accumulates closed flights from history -- no manual
# bumping ever. PRE_HISTORY covers flights that closed before history.jsonl began
# (a fixed historical fact that never changes).
CLOSED_FLIGHTS_PRE_HISTORY = 631.32


def _closed_flights_spend(history_rows):
    total = CLOSED_FLIGHTS_PRE_HISTORY
    prev = None
    for r in history_rows:
        s = r.get("spend_lifetime")
        if s is None:
            continue
        try:
            s = float(s)
        except (TypeError, ValueError):
            continue
        if prev is not None and s < prev - 0.01:  # spend dropped = new flight = prior flight closed
            total += prev
        prev = s
    return total


# ----------------------------------------------------------------- learning --
# Self-improvement memory. state/lessons.jsonl is an append-only, git-tracked
# ledger the marketing-os-refresh agent writes: falsifiable `prediction` records
# and durable `lesson` records. build.py reads it back so every run starts with
# what past runs learned, computes the agent's calibration (were its bets right?),
# and flags predictions whose horizon has elapsed and need resolving. This is the
# deterministic surfacing half of the predict→observe→score→learn loop; the agent
# does the judging. Degrades to an empty structure if the ledger is missing.
LESSONS = HERE / "state" / "lessons.jsonl"

_VERDICT_PTS = {"hit": 1.0, "partial": 0.5, "miss": 0.0}


def load_learning(today=None):
    out = {
        "lessons": [],
        "openPredictions": [],
        "dueForReview": [],
        "calibration": {"n": 0, "hits": 0, "hitRate": None},
        "counts": {"lessons": 0, "predictions": 0, "resolved": 0, "open": 0},
    }
    if not LESSONS.exists():
        return out
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lessons, preds = [], []
    for line in LESSONS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            log(f"lessons.jsonl: skipped unparseable line")
            continue
        kind = rec.get("type")
        if kind == "lesson":
            lessons.append(rec)
        elif kind == "prediction":
            preds.append(rec)

    lessons.sort(key=lambda r: r.get("date", ""), reverse=True)
    open_preds = [p for p in preds if p.get("status") == "open"]
    resolved = [p for p in preds if p.get("status") == "resolved"]

    pts = [_VERDICT_PTS[p["verdict"]] for p in resolved if p.get("verdict") in _VERDICT_PTS]
    n = len(pts)
    hits = sum(1 for p in resolved if p.get("verdict") == "hit")

    out["lessons"] = lessons[:12]
    out["openPredictions"] = sorted(open_preds, key=lambda p: p.get("due", "9999"))
    # A bet whose horizon has elapsed but is still open: the agent must resolve it.
    out["dueForReview"] = [p for p in open_preds if p.get("due", "9999") <= today]
    out["calibration"] = {
        "n": n,
        "hits": hits,
        "hitRate": round(sum(pts) / n, 2) if n else None,
    }
    out["counts"] = {
        "lessons": len(lessons),
        "predictions": len(preds),
        "resolved": len(resolved),
        "open": len(open_preds),
    }
    return out


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
    # Numeric result so the queue reconciler can use it as a deterministic
    # done-signal (the ship is live the moment delta is measurable and positive).
    out["delta"] = round(delta, 1)
    out["before"] = round(before, 1)
    out["after"] = round(after, 1)
    out["shipped"] = bool(oa) and delta > 0
    log(f"signup before/after: {before:.0f}% -> {after:.0f}% ({delta:+.0f} pts)")
    return out


# -------------------------------------------------------------- reconcile ----
_DONE_TYPES = {"done", "closed"}


def _best_clickup_match(keywords, pool):
    """Return the ClickUp task whose name best matches the keyword list, or None.
    A task matches when its lower-cased name contains a keyword phrase; ties break
    toward more keyword hits, then toward a done/closed task (resolution wins)."""
    best, best_score = None, 0
    for t in pool:
        name = (t.get("name") or "").lower()
        hits = sum(1 for k in keywords if k.lower() in name)
        if not hits:
            continue
        score = hits * 2 + (1 if t.get("type") in _DONE_TYPES else 0)
        if score > best_score:
            best, best_score = t, score
    return best


def reconcile_queue(content, meta_live, experiments, clickup):
    """Cross-check each curated Command Center item against the live signals this
    build already gathered, and attach a `live` overlay so the dashboard self-
    corrects when the hand-written status drifts from reality.

    Deterministic and non-destructive: it never rewrites the curated item, it only
    annotates it. Items opt in with an optional `verify` block; items without one
    are left untouched. This is the always-on guardrail between the slower
    LLM-driven marketing-os-refresh reasoning passes.

    verify = {
      "signal":  "signupModalShipped" | "metaDeliveryLive",   # named live signals
      "clickup": ["keyword", ...],                             # match a ClickUp task
      "doneWhen": "signal" | "clickup"                         # what flips it to done
    }
    """
    cc = content.get("commandCenter") or {}
    items = cc.get("items") or []
    pool = (clickup or {}).get("all") or []

    # Named deterministic signals, each -> (is_done, evidence string) or None.
    sx = next((e for e in experiments
               if isinstance(e, dict) and isinstance(e.get("delta"), (int, float))
               and ("signup" in (e.get("name", "").lower()) or "google" in (e.get("name", "").lower()))),
              None)
    signals = {}
    if sx is not None:
        signals["signupModalShipped"] = (
            bool(sx.get("shipped")),
            f"experiment: modal completion {sx.get('before')}% -> {sx.get('after')}% ({sx.get('delta'):+g} pts)",
        )
    if meta_live.get("status"):
        live = meta_live["status"] == "Live"
        signals["metaDeliveryLive"] = (live, f"autopilot: delivery {meta_live['status'].lower()}")

    flagged = 0
    for it in items:
        v = it.get("verify")
        if not v:
            continue
        evidence, ct = [], None
        done_when = v.get("doneWhen")  # which channel is allowed to flip "done"
        sig_done = ct_done = False

        sig = v.get("signal")
        if sig and sig in signals:
            sig_done, sig_ev = signals[sig]
            evidence.append(sig_ev)

        kws = v.get("clickup")
        if kws:
            ct = _best_clickup_match(kws, pool)
            if ct:
                evidence.append(f"ClickUp: “{ct['name']}” is {ct['status']}")
                ct_done = ct.get("type") in _DONE_TYPES

        # Only the channel named by doneWhen may flip the card to done. With no
        # doneWhen the signals are evidence-only (watch items never auto-complete).
        done_signal = (sig_done if done_when == "signal"
                       else ct_done if done_when == "clickup"
                       else False)

        # How the human wrote it.
        written_done = bool(it.get("done")) or bool(re.search(r"shipp|done|complete|live again|resolved", it.get("status", ""), re.I))
        verdict = "done" if (done_signal or written_done) else "active"
        # Disagreement: live says done but the card doesn't (the bug that bit us),
        # or the card claims done with no live evidence to back it.
        disagree = (done_signal and not written_done) or (written_done and not done_signal and bool(evidence))

        it["live"] = {
            "verdict": verdict,
            "doneSignal": done_signal,
            "evidence": evidence,
            "disagree": disagree,
            "clickup": ({"name": ct["name"], "status": ct["status"], "url": ct.get("url", "")} if ct else None),
        }
        if disagree:
            flagged += 1

    log(f"reconcile: {sum(1 for it in items if it.get('verify'))} verified item(s), {flagged} disagreement(s)")


# ------------------------------------------------------------------ build ----
def build():
    content = json.loads(CONTENT.read_text())
    live_experiments = fetch_experiments(load_env(ENV_FILE))
    meta_live = fetch_meta()

    # Tokens: process environment as the base (shell / launchd env vars), with
    # ~/.claude/.env overlaid when present (the local machine's source of truth).
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

    # Keep the Overview's Current Focus numbers bound to the live funnel so the
    # narrative and the Funnel tab can never disagree (the coherence rule).
    bind_current_focus(content, funnel, fetch_magiclink(env))

    # Live channel attribution from PostHog UTMs (ties each source through to
    # signup / try-on / checkout). None on failure.
    channels_live = fetch_channel_attribution(env)

    # ClickUp task feed (source of truth for action items) + IG organic stats.
    clickup = fetch_clickup(env)
    instagram = fetch_instagram(env)
    paying = fetch_paying(env)

    # Daily time-series — snapshot today and rewrite history.jsonl.
    history = snapshot_history(env, funnel, meta_live, lifecycle, instagram, paying=paying)

    # Self-improvement memory — read the agent's own ledger back into the OS so
    # every run starts with what past runs learned (and what bets are due to score).
    learning = load_learning()

    # Activation = same-user signup -> try-on (the aha), straight from the funnel.
    # Neutral placeholder if the funnel pull fails -- never show a stale number.
    activation = "—"
    try:
        spine = funnel.get("spine", {}).get("steps", [])
        aha = next((s for s in spine if s.get("aha")), None)
        if aha:
            activation = f"~{aha['pct']}%"
    except (KeyError, TypeError):
        pass

    # Paid spend sub-line: the autopilot reports only the CURRENT flight's
    # lifetime (it resets when a new campaign starts), so anchor all-time with the
    # closed-flight total (auto-derived from history, no manual bumping) + the live
    # flight. Spend is context here, not a hero metric: CPL + weekly burn are what
    # the call should act on.
    CLOSED_FLIGHTS_SPEND = _closed_flights_spend(history.get("rows", []))

    def _spend_sub(spend_str):
        try:
            cur = float(str(spend_str).replace("$", "").replace(",", "")) if spend_str else 0.0
        except ValueError:
            cur = 0.0
        all_time = CLOSED_FLIGHTS_SPEND + cur
        return f"this flight · ~${all_time:,.0f} all-time"

    # Lifetime signups = ALL sources (Meta + LinkedIn + organic + direct), taken
    # from the PostHog "Signed up" stage all-window count, not Meta-attributed only.
    signups = meta_live["signups"]  # Meta-attributed (kept for CPL math)
    total_signups = None
    try:
        sig_stage = next((s for s in funnel.get("stages", []) if s.get("key") == "signup"), None)
        if sig_stage:
            total_signups = sig_stage.get("counts", {}).get("all")
    except (KeyError, TypeError):
        pass
    signups_value = str(total_signups) if total_signups is not None else (str(signups) if signups is not None else "—")
    kpis = [
        # Paying customers from the PostHog purchase event (Studio-scoped by nature).
        {"label": "Paying customers",
         "value": f"{paying} / 5" if paying is not None else "0 / 5",
         "sub": "the brag · CAC < $300", "tone": "accent"},
        {"label": "Lifetime signups", "value": signups_value, "sub": "all-time, all sources", "tone": "default"},
        {"label": "Cost per signup", "value": meta_live["cpl"] or "—", "sub": "paid · target ≤ $20", "tone": "default"},
        {"label": "Activation", "value": activation, "sub": "signup → try-on · target ≥ 50%", "tone": "default"},
        {"label": "Paid spend", "value": meta_live["spend"] or "$0", "sub": _spend_sub(meta_live["spend"]), "tone": "default"},
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
        # Always-on deterministic guardrail: annotate each curated item with the
        # live verdict so the queue self-corrects when its hand-written status
        # drifts (the "stale priority" gap). Runs every build, no LLM needed.
        reconcile_queue(content, meta_live, experiments, clickup)
    if clickup:
        clickup.pop("all", None)  # reconciler-only pool; don't embed it in data.js
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
        "learning": learning,
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
        "/* Curated strategy lives in content.json; live data refreshes daily. */\n"
    )
    OUT.write_text(banner + "window.ABLO_OS = " + json.dumps(content, ensure_ascii=False, indent=2) + ";\n")
    log(f"wrote {OUT.name} · updated {content['meta']['updated']}")
    log(f"posthog={'live' if posthog_live else 'cached'} meta={'live' if signups is not None else 'cached'}")
    cal = learning["calibration"]
    log(
        f"learning: {learning['counts']['lessons']} lessons · "
        f"calibration {cal['hitRate'] if cal['hitRate'] is not None else 'n/a'} (n={cal['n']}) · "
        f"{len(learning['dueForReview'])} prediction(s) due for review"
    )


if __name__ == "__main__":
    build()
