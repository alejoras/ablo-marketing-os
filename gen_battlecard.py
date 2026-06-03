#!/usr/bin/env python3
"""
Parse the competitive battlecard workbook into a `battlecard` block in
content.json. One-shot importer, kept in-repo so the card can be regenerated
if the source xlsx is refreshed.

Source: Brain/projects/ablo/Ablo Studio/competitive-landscape/battlecard-2026-04-29.xlsx
Sheets: Feature Matrix | Positioning & ICP | Pricing & Per-Image | Strengths Weaknesses

Columns in every sheet (after the label col):
  1 Ablo Studio | 2 Botika | 3 Raspberry AI | 4 Browzwear (Lalaland)
  5 The New Black | 6 Fashn.ai | 7 Veeton | 8 Wearview
"""
import json
from collections import OrderedDict
from pathlib import Path

import openpyxl

HERE = Path(__file__).resolve().parent
CONTENT = HERE / "content.json"
XLSX = Path(
    "/Users/alejo/Documents/Claude/Brain/projects/ablo/Ablo Studio/"
    "competitive-landscape/battlecard-2026-04-29.xlsx"
)


def tidy(s):
    """Match build.py house style: no em dashes, no double-hyphen dashes."""
    if s is None:
        return ""
    s = str(s).strip()
    return (
        s.replace(" -- ", ", ")
        .replace("--", ", ")
        .replace(" — ", ", ")
        .replace("—", ", ")
        .replace(" – ", ", ")
        .replace("–", ", ")
    )


def rows_of(ws):
    return [[c for c in row] for row in ws.iter_rows(values_only=True)]


def find(rows, prefix):
    """First row whose label cell (col 0) starts with prefix (case-insensitive)."""
    p = prefix.lower()
    for r in rows:
        lbl = (r[0] or "")
        if str(lbl).strip().lower().startswith(p):
            return r
    return None


def cell(rows, prefix, col):
    r = find(rows, prefix)
    return tidy(r[col]) if r else ""


def main():
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    fm = rows_of(wb["Feature Matrix"])
    po = rows_of(wb["Positioning & ICP"])
    pr = rows_of(wb["Pricing & Per-Image"])
    sw = rows_of(wb["Strengths Weaknesses"])

    # header (names) at index 3, site row at index 4, data from index 5
    # (row 2 is blank in every sheet)
    names = [tidy(x) for x in po[3]]
    sites = [tidy(x) for x in po[4]]

    columns = [names[c] for c in range(1, 9)]
    sitecols = [sites[c] for c in range(1, 9)]

    # ---- per-competitor battle cards (skip col 1 = Ablo, the "us" column) ----
    competitors = []
    for c in range(2, 9):
        comp = OrderedDict()
        comp["name"] = names[c]
        comp["site"] = sites[c]
        comp["tagline"] = cell(po, "Tagline", c)
        comp["promise"] = cell(po, "Core promise", c)
        comp["icp"] = cell(po, "Primary ICP", c)
        comp["differentiation"] = cell(po, "Differentiation claim", c)
        comp["entryTier"] = cell(pr, "Entry tier", c)
        comp["perImage"] = cell(pr, "$/image", c)
        comp["funding"] = cell(fm, "Funding", c)
        comp["reviews"] = cell(fm, "Public reviews", c)
        comp["customers"] = cell(fm, "Marquee customer", c)
        comp["strengths"] = [cell(sw, f"Strength #{i}", c) for i in (1, 2, 3)]
        comp["weaknesses"] = [cell(sw, f"Weakness #{i}", c) for i in (1, 2, 3)]
        comp["objection"] = cell(sw, "Common buyer objection", c)
        wins = []
        for r in sw:
            lbl = str(r[0] or "").lower()
            if lbl.startswith("how ablo wins"):
                v = tidy(r[c])
                if v and v != "-":
                    wins.append(v)
        comp["wins"] = wins
        competitors.append(comp)

    # ---- full feature matrix (grouped) ----
    groups = []
    cur = None
    for r in fm[5:]:  # skip title, caption, header, site rows
        label = str(r[0] or "").strip()
        if not label:
            continue
        rest = [r[i] for i in range(1, 9)]
        is_group = all((x is None or str(x).strip() == "") for x in rest)
        if is_group:
            cur = {"group": tidy(label), "rows": []}
            groups.append(cur)
        else:
            if cur is None:
                cur = {"group": "", "rows": []}
                groups.append(cur)
            cur["rows"].append(
                {"feature": tidy(label), "values": [tidy(r[i]) for i in range(1, 9)]}
            )

    # ---- pricing table ----
    pricing_rows = []
    for r in pr[5:]:
        label = str(r[0] or "").strip()
        if not label:
            continue
        pricing_rows.append(
            {"dim": tidy(label), "values": [tidy(r[i]) for i in range(1, 9)]}
        )

    battlecard = OrderedDict()
    battlecard["intro"] = (
        "Head-to-head against the AI try-on set. For each competitor: their angle, "
        "their soft spots, the objection a buyer will raise, and how Studio wins the room. "
        "Studio is the column everything is measured against."
    )
    battlecard["updated"] = "April 29, 2026"
    battlecard["sources"] = [
        "brand-gtm-brief-2026-04-28.md",
        "competitor-pricing-comparison.md",
        "competitor-research-2026-04-29.md",
    ]
    battlecard["caveat"] = (
        "Cells marked VERIFY are unconfirmed and need a check before going in any "
        "external deck. Pricing sourced from public pages on 2026-04-28/29; some "
        "competitors wall prices behind login. Refresh this card when the workbook updates."
    )
    battlecard["columns"] = columns
    battlecard["sites"] = sitecols
    battlecard["competitors"] = competitors
    battlecard["featureMatrix"] = {"columns": columns, "groups": groups}
    battlecard["pricing"] = {"columns": columns, "rows": pricing_rows}

    # ---- splice into content.json, right after `competition` ----
    data = json.loads(CONTENT.read_text(), object_pairs_hook=OrderedDict)
    out = OrderedDict()
    for k, v in data.items():
        if k == "battlecard":  # drop any prior copy; we re-add it fresh below
            continue
        out[k] = v
        if k == "competition":
            out["battlecard"] = battlecard
    if "battlecard" not in out:
        out["battlecard"] = battlecard

    CONTENT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print(f"battlecard: {len(competitors)} competitors, "
          f"{sum(len(g['rows']) for g in groups)} feature rows, "
          f"{len(pricing_rows)} pricing rows -> content.json")


if __name__ == "__main__":
    main()
