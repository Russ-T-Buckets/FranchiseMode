"""
backfill_fwa_from_sheet.py

Reads FWA values from the manual spreadsheet (weeks 1-11)
and upserts into pipeline.weekly_metric_snapshots.

Overwrites any existing FWA for these weeks.
FER intentionally excluded.
Week 12 skipped (partial — only More Defiant Jazz filled in).

Upsert key: (player_id, week_number, season_year)
"""

import os
import argparse
import uuid
import requests
from openpyxl import load_workbook

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SEASON_YEAR  = 2026
WEEKS        = list(range(1, 12))   # 1-11; week 12 skipped (partial)

# pipeline schema headers (for weekly_metric_snapshots)
PIPELINE_HEADERS = {
    "apikey":          SUPABASE_KEY,
    "Authorization":   f"Bearer {SUPABASE_KEY}",
    "Content-Type":    "application/json",
    "Accept-Profile":  "pipeline",
    "Content-Profile": "pipeline",
    "Prefer":          "resolution=merge-duplicates",
}

# baseball schema headers (for players)
BASEBALL_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Accept-Profile": "baseball",
}

# ---------------------------------------------------------------------------
# Hardcoded 2026 team UUIDs (stable — queried from DB 2026-06-25)
# Maps DB team_name -> UUID
# ---------------------------------------------------------------------------
TEAM_UUID = {
    "Jackson County OrangTurangs": "5366ee5d-e4af-4396-a823-5c68ff2543b2",
    "Down by the Schoolyard":      "1745e320-90c0-467f-8183-dc328c045596",
    "Sho-Time":                    "823fa185-7aa2-40e3-83f5-0c4661f4453b",
    "More Defiant Jazz":           "c20baac2-8ed3-4fcf-8841-589849b013e9",
    "All Betts are Off":           "dee5c8c0-8562-4cc6-82f5-6d8d97580aad",
    "Ass Cannons":                 "8dd32df8-a4d6-4cc0-90c7-18701a5825c2",
    "Ronald's PlayPlace":          "7fe3b8b6-0466-4ded-a746-c67dc09d638a",
    "Kekambas":                    "dcfbe677-8c59-4ea2-9a04-2e1f3ca12874",
    "My Roman Empire":             "7063e9b9-31d8-4f23-8962-6cf3586b6ef3",
    "I am the Breg-man":           "3c102f76-d71f-4e8b-bbfb-c9a14b382099",
    "Greene Brown and Schlitty":   "49292066-93e3-414a-92e5-e4764f0e4924",
    "Boston Stink Sox":            "c132b84e-edf2-45bc-beb5-592c5a378f09",
}

# Spreadsheet name -> DB team name
TEAM_NAME_MAP = {
    "More Defiant Jazz":         "More Defiant Jazz",
    "All Betts Are Off":         "All Betts are Off",
    "Ass Cannons":               "Ass Cannons",
    "Boston Stink Sox":          "Boston Stink Sox",
    "Down By The Schoolyard":    "Down by the Schoolyard",
    "Greene Brown and Schlitty": "Greene Brown and Schlitty",
    "I am the Breg-Man":         "I am the Breg-man",
    "Kekambas":                  "Kekambas",
    "My Roman Empire":           "My Roman Empire",
    "Ronald's PlayPlace":        "Ronald's PlayPlace",
    "Sho-Time":                  "Sho-Time",
    "honey nuts":                "Jackson County OrangTurangs",
    "Honey Nuts":                "Jackson County OrangTurangs",
}

# Precompute: spreadsheet name -> UUID
SHEET_NAME_TO_ID = {
    sheet: TEAM_UUID[db]
    for sheet, db in TEAM_NAME_MAP.items()
    if db in TEAM_UUID
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sb_get_players():
    """Paginate through baseball.players, return normalized_name -> id."""
    url = f"{SUPABASE_URL}/rest/v1/players"
    all_players = []
    offset = 0
    limit  = 1000
    while True:
        r = requests.get(url, headers=BASEBALL_HEADERS,
                         params={"select": "id,first_name,last_name",
                                 "limit": limit, "offset": offset})
        r.raise_for_status()
        batch = r.json()
        all_players.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return all_players


def sb_upsert(rows, batch_size=200):
    url     = f"{SUPABASE_URL}/rest/v1/weekly_metric_snapshots"
    written = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i+batch_size]
        r = requests.post(url, headers=PIPELINE_HEADERS, json=batch)
        if not r.ok:
            print(f"  ERROR batch {i//batch_size}: {r.status_code} {r.text[:300]}")
            r.raise_for_status()
        written += len(batch)
    return written


def normalize_name(name):
    import unicodedata
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = name.lower().strip()
    for suffix in [" jr.", " jr", " sr.", " sr", " iii", " ii"]:
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()
    return name


def load_players():
    players = sb_get_players()
    name_to_id = {}
    for p in players:
        full = f"{p['first_name']} {p['last_name']}".strip()
        name_to_id[normalize_name(full)] = p["id"]
    return name_to_id


def extract_week(wb, week_num):
    ws       = wb[f"Week {week_num}"]
    all_rows = list(ws.iter_rows(values_only=True))

    header_row_idx = None
    for i, row in enumerate(all_rows):
        strs = [str(c) if c is not None else "" for c in row]
        if "Player" in strs and "FWA" in strs:
            header_row_idx = i
            break

    if header_row_idx is None:
        print(f"  Week {week_num}: no header row found, skipping")
        return []

    header     = all_rows[header_row_idx]
    col_player = col_team = col_fwa = None
    for i, h in enumerate(header):
        if h == "Player": col_player = i
        if h == "Team":   col_team   = i
        if h == "FWA":    col_fwa    = i

    if col_player is None or col_fwa is None:
        print(f"  Week {week_num}: missing Player or FWA column, skipping")
        return []

    out = []
    for row in all_rows[header_row_idx + 1:]:
        player = row[col_player] if col_player < len(row) else None
        team   = row[col_team]   if col_team is not None and col_team < len(row) else None
        fwa    = row[col_fwa]    if col_fwa < len(row) else None

        if not (player and isinstance(player, str) and player.strip()):
            continue
        if not isinstance(fwa, (int, float)):
            continue

        out.append({
            "player_name":     player.strip(),
            "team_sheet_name": team.strip() if isinstance(team, str) else None,
            "fwa":             float(fwa),
        })
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--xlsx",    required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"Team map: {len(SHEET_NAME_TO_ID)} sheet names -> UUIDs")

    print("Loading workbook...")
    wb = load_workbook(args.xlsx, read_only=True, data_only=True)

    print("Loading players from Supabase...")
    norm_to_id = load_players()
    print(f"  {len(norm_to_id)} players loaded")

    grand_total       = 0
    unmatched_players = {}
    unmatched_teams   = {}

    for week_num in WEEKS:
        print(f"\n--- Week {week_num} ---")
        sheet_rows = extract_week(wb, week_num)
        print(f"  {len(sheet_rows)} rows in sheet")

        upsert_rows       = []
        skip_no_player    = 0
        skip_no_team      = 0

        for sr in sheet_rows:
            player_id = norm_to_id.get(normalize_name(sr["player_name"]))
            if player_id is None:
                skip_no_player += 1
                unmatched_players.setdefault(sr["player_name"], []).append(week_num)
                continue

            team_id = SHEET_NAME_TO_ID.get(sr["team_sheet_name"]) if sr["team_sheet_name"] else None
            if team_id is None:
                skip_no_team += 1
                unmatched_teams.setdefault(sr["team_sheet_name"], []).append(week_num)
                continue

            upsert_rows.append({
                "id":          str(uuid.uuid4()),
                "player_id":   player_id,
                "team_id":     team_id,
                "week_number": week_num,
                "season_year": SEASON_YEAR,
                "fwa":         round(sr["fwa"], 6),
                "fer":         None,
                "fer_grade":   None,
                "stat_basis":  "matchup",
                "is_locked":   True,
            })

        total_fwa = sum(r["fwa"] for r in upsert_rows)
        print(f"  {len(upsert_rows)} rows ready  |  FWA checksum: {total_fwa:.4f}")
        print(f"  {skip_no_player} skipped (no player match)  |  {skip_no_team} skipped (no team match)")

        if not args.dry_run and upsert_rows:
            written = sb_upsert(upsert_rows)
            print(f"  ✓ {written} rows written")
            grand_total += written

    print(f"\n{'='*50}")
    if not args.dry_run:
        print(f"DONE — {grand_total} rows written across weeks {WEEKS[0]}-{WEEKS[-1]}")
    else:
        print("DRY RUN — nothing written")

    if unmatched_players:
        print(f"\nUnmatched players ({len(unmatched_players)} unique):")
        for name, weeks in sorted(unmatched_players.items()):
            print(f"  '{name}' — weeks {sorted(set(weeks))}")

    if unmatched_teams:
        print(f"\nUnmatched teams ({len(unmatched_teams)} unique):")
        for name, weeks in sorted(unmatched_teams.items()):
            print(f"  '{name}' — first seen week {min(weeks)}")


if __name__ == "__main__":
    main()
