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
OUT = HERE / "data.js"

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


# ------------------------------------------------------------------ build ----
def build():
    content = json.loads(CONTENT.read_text())
    live_experiments = fetch_experiments(load_env(ENV_FILE))
    meta_live = fetch_meta()

    posthog_live = bool(live_experiments)
    if posthog_live:
        experiments = live_experiments
    else:
        # Graceful fallback: accurate last-known experiments from content.json
        # (auto-upgrades to live once the PostHog key gets experiment:read scope).
        experiments = content.get("experimentsCurated", {}).get("liveFallback", [])

    signups = meta_live["signups"]
    kpis = [
        {"label": "Paying customers", "value": "0 / 5", "sub": "the brag · CAC < $300", "tone": "accent"},
        {"label": "Lifetime signups", "value": str(signups) if signups is not None else "30", "sub": "all-time, paid", "tone": "default"},
        {"label": "Cost per signup", "value": meta_live["cpl"] or "$21.04", "sub": "target ≤ $20", "tone": "default"},
        {"label": "Activation", "value": "~53%", "sub": "signup → try-on · target ≥ 50%", "tone": "default"},
        {"label": "Paid spend", "value": meta_live["spend"] or "$631", "sub": "validation budget · $200/wk", "tone": "default"},
        {"label": "Live experiments", "value": str(len(experiments)) if experiments else "1", "sub": "running in PostHog", "tone": "default"},
    ]

    now = datetime.now(timezone.utc)
    content["meta"]["updated"] = now.strftime("%B %-d, %Y")
    content["meta"]["updatedISO"] = now.isoformat()
    content["live"] = {
        "kpis": kpis,
        "experiments": experiments,
        "meta": meta_live,
        "refreshedSources": {
            "posthog": posthog_live,
            "meta": signups is not None,
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
