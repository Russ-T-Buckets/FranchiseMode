"""
insert_missing_players.py

Looks up missing players by name in Yahoo Fantasy API, then inserts them
into baseball.players with their yahoo_player_id.

Run after backfill_fwa_from_sheet.py to fill gaps in weeks 1-5.
"""

import os
import re
import time
import uuid
import requests
import xml.etree.ElementTree as ET
from requests_oauthlib import OAuth1

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONSUMER_KEY    = os.environ["YAHOO_CONSUMER_KEY"]
CONSUMER_SECRET = os.environ["YAHOO_CONSUMER_SECRET"]
ACCESS_TOKEN    = os.environ["YAHOO_ACCESS_TOKEN"]
ACCESS_SECRET   = os.environ["YAHOO_REFRESH_TOKEN"]   # stored as REFRESH_TOKEN in secrets

SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_SERVICE_KEY"]

BASEBALL_HEADERS = {
    "apikey":         SUPABASE_KEY,
    "Authorization":  f"Bearer {SUPABASE_KEY}",
    "Content-Type":   "application/json",
    "Accept-Profile": "baseball",
    "Content-Profile":"baseball",
    "Prefer":         "resolution=merge-duplicates",
}

YAHOO_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
GAME_KEY   = "422"   # MLB game key (works across seasons for player lookup)

# ---------------------------------------------------------------------------
# Players to look up — sheet name -> (first_name, last_name) for DB insert
# ---------------------------------------------------------------------------
MISSING_PLAYERS = [
    ("Aaron",     "Civale"),
    ("Andrew",    "Painter"),
    ("Andrew",    "Vaughn"),
    ("Augustin",  "Ramirez"),
    ("Brayan",    "Bello"),
    ("Brendon",   "Little"),
    ("Brenton",   "Doyle"),
    ("Bryson",    "Stott"),
    ("Cade",      "Horton"),
    ("Cole",      "Sands"),
    ("Colt",      "Keith"),
    ("Daniel",    "Schneeman"),
    ("Garrett",   "Cleavinger"),
    ("Graham",    "Ashcraft"),
    ("Heliot",    "Ramos"),
    ("Isaac",     "Paredes"),
    ("Jack",      "Leiter"),
    ("Jeffrey",   "Springs"),
    ("Jeremiah",  "Estrada"),
    ("Jeremiah",  "Jackson"),
    ("Jordan",    "Beck"),
    ("Jose",      "Estrada"),
    ("Juan",      "Morillo"),
    ("Kerry",     "Carpenter"),
    ("Kodai",     "Senga"),
    ("Lake",      "Bachar"),
    ("Marcel",    "Dubon"),
    ("Marcelo",   "Mayer"),
    ("Nolan",     "Schanuel"),
    ("Owen",      "Cassie"),
    ("Rhett",     "Lowder"),
    ("Robert",    "Garcia"),
    ("Shane",     "Baz"),
    ("Spencer",   "Torkelson"),
    ("Steven",    "Kwan"),
    ("Tyler",     "Holton"),
    ("Will",      "Vest"),
    ("Yainer",    "Diaz"),
]

# ---------------------------------------------------------------------------
# Yahoo OAuth
# ---------------------------------------------------------------------------
def yahoo_auth():
    return OAuth1(CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_SECRET)


def yahoo_search_player(first, last, auth):
    """Search Yahoo for a player by name, return (yahoo_player_id, full_name) or None."""
    query = f"{first} {last}"
    url   = f"{YAHOO_BASE}/players;game_keys=mlb;search={requests.utils.quote(query)}"
    r = requests.get(url, auth=auth)
    if not r.ok:
        print(f"  Yahoo error {r.status_code} for '{query}': {r.text[:100]}")
        return None

    ns  = {"y": "http://fantasysports.yahooapis.com/fantasy/v2/base.rng"}
    root = ET.fromstring(r.text)

    players = root.findall(".//y:player", ns)
    for player in players:
        pid_el    = player.find("y:player_id", ns)
        fname_el  = player.find(".//y:first", ns)
        lname_el  = player.find(".//y:last", ns)
        if pid_el is None:
            continue
        f = (fname_el.text or "").strip() if fname_el is not None else ""
        l = (lname_el.text or "").strip() if lname_el is not None else ""
        # Match on last name and first initial
        if l.lower() == last.lower() and f.lower().startswith(first[0].lower()):
            return pid_el.text, f"{f} {l}"

    return None


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------
def get_existing_yahoo_ids():
    """Return set of yahoo_player_ids already in DB."""
    url = f"{SUPABASE_URL}/rest/v1/players"
    r   = requests.get(url, headers=BASEBALL_HEADERS,
                       params={"select": "yahoo_player_id", "limit": 5000})
    r.raise_for_status()
    return {p["yahoo_player_id"] for p in r.json() if p["yahoo_player_id"]}


def insert_players(rows):
    url = f"{SUPABASE_URL}/rest/v1/players"
    r   = requests.post(url, headers=BASEBALL_HEADERS, json=rows)
    if not r.ok:
        print(f"  Insert error: {r.status_code} {r.text[:200]}")
        r.raise_for_status()
    return len(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    auth             = yahoo_auth()
    existing_ids     = get_existing_yahoo_ids()
    print(f"Existing yahoo_player_ids in DB: {len(existing_ids)}")

    to_insert  = []
    not_found  = []

    for first, last in MISSING_PLAYERS:
        result = yahoo_search_player(first, last, auth)
        time.sleep(0.3)   # be polite to Yahoo API

        if result is None:
            print(f"  NOT FOUND in Yahoo: {first} {last}")
            not_found.append(f"{first} {last}")
            continue

        yahoo_id, full_name = result
        if yahoo_id in existing_ids:
            print(f"  Already in DB (different name?): {full_name} (id={yahoo_id})")
            continue

        print(f"  Found: {full_name} -> yahoo_player_id={yahoo_id}")
        name_parts = full_name.split(" ", 1)
        to_insert.append({
            "id":              str(uuid.uuid4()),
            "yahoo_player_id": yahoo_id,
            "first_name":      name_parts[0],
            "last_name":       name_parts[1] if len(name_parts) > 1 else "",
        })

    print(f"\n{len(to_insert)} players to insert, {len(not_found)} not found in Yahoo")

    if to_insert:
        inserted = insert_players(to_insert)
        print(f"✓ Inserted {inserted} players into baseball.players")
        print("\nInserted:")
        for p in to_insert:
            print(f"  {p['first_name']} {p['last_name']} (yahoo_id={p['yahoo_player_id']})")

    if not_found:
        print(f"\nNot found in Yahoo ({len(not_found)}):")
        for n in not_found:
            print(f"  {n}")


if __name__ == "__main__":
    main()
