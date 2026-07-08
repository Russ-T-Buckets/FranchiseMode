"""
============================================================
FRANCHISE MODE - Master Pipeline Script (v9)
============================================================
Full rewrite against the `baseball` schema. The `pipeline` schema
(roster_snapshots, player_stats_daily, weekly_metric_snapshots) is
gone -- do not reference it.

Four modes, one script:

  slot_stats   -- daily, 06:00 UTC (2AM Eastern). Pulls YESTERDAY's
                  full roster (every player, every slot) + YESTERDAY's
                  true single-day box scores. Writes baseball.player_daily_stats
                  (whole roster, stats nulled for non-starters) and
                  baseball.mlb_stats (roster-agnostic, every player).
                  On Mondays this mode ALSO runs weekly_lock for the
                  week that just closed (combined run, see weekly_lock
                  docstring below).

  waiver       -- daily, 14:00 UTC (10AM Eastern). Pulls today's
                  add/drop transactions, writes baseball.transactions,
                  updates mlb_stats.current_fantasy_team_id.

  weekly_lock  -- invoked from slot_stats on Mondays only. Pulls
                  scoreboard/team totals for the week that just ended
                  (folded-in populate_team_weekly_stats logic), then
                  calls a SQL function to aggregate player_daily_stats
                  into player_weekly_stats_v2 + score FER, then computes
                  FWA in Python using data already in memory (no re-read
                  from the DB), writes fwa_* back, and locks the week.

  backfill     -- manual CLI only. `--season YYYY --weeks A-B`. Same as
                  the old standalone populate_team_weekly_stats.py --
                  team totals + matchups for arbitrary historical weeks.
                  Does NOT touch player_daily_stats/mlb_stats (that
                  backfill is a separate, already-tracked backlog item).

Credentials: oauth2.json in the same folder as this script (local dev)
             OR YAHOO_CONSUMER_KEY / YAHOO_CONSUMER_SECRET /
             YAHOO_ACCESS_TOKEN / YAHOO_REFRESH_TOKEN env vars (CI)
Supabase:    SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_KEY) env vars
============================================================
"""

import os
import sys
import json
import base64
import time
import argparse
import requests
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta


# ============================================================
# SECTION 0: RETRY WRAPPERS
# ============================================================

def _retry(method, url, retries=5, backoff=2, **kwargs):
    kwargs.setdefault("timeout", 30)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = method(url, **kwargs)
            if r.status_code in (502, 503, 504):
                last_err = Exception(f"HTTP {r.status_code}: {r.text[:200]}")
                if attempt < retries:
                    wait = backoff * attempt
                    print(f"  [Retry] Attempt {attempt} got HTTP {r.status_code}, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                return r  # exhausted retries, return final response so caller's error message is accurate
            return r
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = backoff * attempt
                print(f"  [Retry] Attempt {attempt} failed ({type(e).__name__}), retrying in {wait}s...")
                time.sleep(wait)
    raise last_err

def requests_get_with_retry(url, **kwargs):    return _retry(requests.get, url, **kwargs)
def requests_post_with_retry(url, **kwargs):   return _retry(requests.post, url, **kwargs)
def requests_patch_with_retry(url, **kwargs):  return _retry(requests.patch, url, **kwargs)
def requests_delete_with_retry(url, **kwargs): return _retry(requests.delete, url, **kwargs)


# ============================================================
# SECTION 1: CONFIG
# ============================================================

CREDS_FILE   = os.path.join(os.path.dirname(__file__), "oauth2.json")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://seqvzektwxxypdcqgtve.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")

YAHOO_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

# Yahoo issues a new league_key every season for the same league.
SEASON_LEAGUE_KEYS = {
    2022: "412.l.71654",
    2023: "422.l.47778",
    2024: "431.l.78645",
    2025: "458.l.72231",
    2026: "469.l.76761",
}
SEASON_YEAR = int(os.environ.get("FRANCHISE_SEASON_YEAR", "2026"))
LEAGUE_KEY  = SEASON_LEAGUE_KEYS[SEASON_YEAR]

# "Started" allow-list. A player's stats only count toward player_daily_stats
# and (eventually) player_weekly_stats_v2 if selected_position is in this set
# on that day. Everyone else (BN, IL, IL10, IL60, NA, ...) still gets a row
# in player_daily_stats -- selected_position recorded, stats left null.
STARTER_ALLOW_LIST = {"C", "1B", "2B", "3B", "SS", "OF", "UT", "SP", "RP", "P"}

STAT_IDS = {
    "h_ab":    "60",
    "r":       "7",
    "hr":      "12",
    "rbi":     "13",
    "sb":      "16",
    "obp":     "4",
    "ip":      "50",
    "sv":      "32",
    "era":     "26",
    "whip":    "27",
    "k_per_9": "57",
    "qs":      "83",
}

# Yahoo stat display_name (lowercased) -> our column name, for the JSON-based
# scoreboard/settings endpoints used by the weekly team-totals pull.
STAT_NAME_ALIASES = {
    "r": "r", "runs": "r",
    "hr": "hr", "home runs": "hr",
    "rbi": "rbi", "rbis": "rbi",
    "sb": "sb", "stolen bases": "sb",
    "obp": "obp", "on-base percentage": "obp", "on base percentage": "obp",
    "sv": "sv", "saves": "sv",
    "qs": "qs", "quality starts": "qs",
    "era": "era", "earned run average": "era",
    "whip": "whip",
    "k/9": "k9", "k9": "k9", "strikeouts per 9 innings": "k9", "strikeouts per nine innings": "k9",
    "ip": "ip", "innings pitched": "ip",
}

OBP_TOLERANCE = 0.005
MIN_PA = 14


# ============================================================
# SECTION 2: TOKEN MANAGEMENT
# ============================================================

def load_creds():
    if os.environ.get("YAHOO_CONSUMER_KEY"):
        return {
            "consumer_key":    os.environ["YAHOO_CONSUMER_KEY"],
            "consumer_secret": os.environ["YAHOO_CONSUMER_SECRET"],
            "access_token":    os.environ.get("YAHOO_ACCESS_TOKEN", ""),
            "refresh_token":   os.environ["YAHOO_REFRESH_TOKEN"],
            "token_time":      0
        }
    with open(CREDS_FILE) as f:
        return json.load(f)

def save_creds(creds):
    if os.environ.get("YAHOO_CONSUMER_KEY"):
        return  # no persistent file in CI
    with open(CREDS_FILE, "w") as f:
        json.dump(creds, f, indent=4)

def refresh_token():
    print("[Token] Refreshing Yahoo OAuth token...")
    creds = load_creds()
    encoded = base64.b64encode(f"{creds['consumer_key']}:{creds['consumer_secret']}".encode()).decode()
    r = requests_post_with_retry(
        "https://api.login.yahoo.com/oauth2/get_token",
        headers={"Authorization": f"Basic {encoded}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]}
    )
    if r.status_code != 200:
        raise Exception(f"[Token] Refresh failed: {r.text}")
    tokens = r.json()
    creds["access_token"]  = tokens["access_token"]
    creds["refresh_token"] = tokens["refresh_token"]
    creds["token_time"]    = time.time()
    save_creds(creds)
    print("[Token] Refreshed successfully.")
    return creds["access_token"]


# ============================================================
# SECTION 3: YAHOO API HELPERS (XML -- roster + single-day stats)
# ============================================================

def strip_namespaces(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}")[1]
    return root

def yahoo_get(access_token, endpoint, retries=3, backoff=2):
    url = f"{YAHOO_BASE}/{endpoint}"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests_get_with_retry(url, headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/xml"
            })
            if r.status_code != 200:
                raise Exception(f"[Yahoo] {r.status_code}: {r.text[:500]}")
            return strip_namespaces(ET.fromstring(r.text))
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = backoff * attempt
                print(f"  [Yahoo] Attempt {attempt} failed, retrying in {wait}s: {type(e).__name__}")
                time.sleep(wait)
    raise Exception(f"[Yahoo] All {retries} attempts failed for {endpoint}: {last_err}")


# ============================================================
# SECTION 4: YAHOO API HELPERS (JSON -- scoreboard/settings/teams)
# used by the folded-in weekly team-totals pull.
# ============================================================

def yahoo_get_json(access_token, path, retries=3, backoff=2):
    url = f"{YAHOO_BASE}/{path}?format=json"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests_get_with_retry(url, headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json"
            })
            if r.status_code != 200:
                raise Exception(f"[Yahoo JSON] {r.status_code}: {r.text[:500]}")
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = backoff * attempt
                print(f"  [Yahoo JSON] Attempt {attempt} failed, retrying in {wait}s: {type(e).__name__}")
                time.sleep(wait)
    raise Exception(f"[Yahoo JSON] All {retries} attempts failed for {path}: {last_err}")


# ============================================================
# SECTION 5: SUPABASE HELPERS
# ============================================================

def sb_headers(schema="baseball", prefer=None):
    h = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Accept-Profile":  schema,
        "Content-Profile": schema,
    }
    if prefer:
        h["Prefer"] = prefer
    return h

def sb_select(table, filters="", schema="baseball"):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if filters:
        url += f"?{filters}"
    r = requests_get_with_retry(url, headers=sb_headers(schema))
    if r.status_code != 200:
        raise Exception(f"[Supabase] Select failed on {schema}.{table}: {r.text[:300]}")
    return r.json()

# Conflict column map -- which columns Supabase should use for upsert per table.
UPSERT_CONFLICT = {
    "player_daily_stats":     "player_id,stat_date",
    "mlb_stats":               "player_id,stat_date",
    "player_weekly_stats_v2":  "player_id,season_id,week_number",
    "matchups":                "season_id,week_number,home_team_id,away_team_id",
    "team_weekly_stats":       "season_id,team_id,week_number",
    "transactions":            "team_id,player_id,transaction_type,transaction_date",
}

def sb_upsert(table, rows, schema="baseball", batch_size=200):
    if not rows:
        return
    conflict_cols = UPSERT_CONFLICT.get(table, "")
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if conflict_cols:
        url += f"?on_conflict={conflict_cols}"
    prefer = "resolution=merge-duplicates,return=minimal"
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        r = requests_post_with_retry(url, headers=sb_headers(schema, prefer), json=batch)
        if r.status_code not in (200, 201, 204):
            print(f"[Supabase] Warning on {schema}.{table}: {r.status_code} {r.text[:300]}")

def sb_patch_by_id(table, row_id, fields, schema="baseball"):
    url = f"{SUPABASE_URL}/rest/v1/{table}?id=eq.{row_id}"
    r = requests_patch_with_retry(url, headers=sb_headers(schema), json=fields)
    if r.status_code not in (200, 201, 204):
        print(f"[Supabase] Warning patching {schema}.{table} id={row_id}: {r.status_code} {r.text[:200]}")

def sb_rpc(function_name, params, schema="baseball"):
    """Call a Postgres function via PostgREST RPC and get its result set back
    directly in the response -- avoids a separate SELECT round trip."""
    url = f"{SUPABASE_URL}/rest/v1/rpc/{function_name}"
    r = requests_post_with_retry(url, headers=sb_headers(schema), json=params)
    if r.status_code not in (200, 201):
        raise Exception(f"[Supabase] RPC {function_name} failed: {r.status_code} {r.text[:500]}")
    return r.json()


# ============================================================
# SECTION 6: PLAYER REGISTRY
# ============================================================

_player_uuid_cache = {}

def ensure_player_exists(yahoo_player_id, first_name, last_name):
    row = {"yahoo_player_id": str(yahoo_player_id), "first_name": first_name, "last_name": last_name}
    r = requests_post_with_retry(
        f"{SUPABASE_URL}/rest/v1/players",
        headers=sb_headers("baseball", "resolution=ignore-duplicates"),
        json=row
    )
    if r.status_code not in (200, 201, 409):
        print(f"[Players] Warning: {first_name} {last_name} ({yahoo_player_id}): {r.status_code}")

def get_player_uuid(yahoo_player_id):
    if yahoo_player_id in _player_uuid_cache:
        return _player_uuid_cache[yahoo_player_id]
    rows = sb_select("players", f"yahoo_player_id=eq.{yahoo_player_id}&select=id")
    uuid = rows[0]["id"] if rows else None
    _player_uuid_cache[yahoo_player_id] = uuid
    return uuid


# ============================================================
# SECTION 7: SEASON / TEAM RESOLUTION (shared by all modes)
# ============================================================

def resolve_season_id(season_year=SEASON_YEAR):
    rows = sb_select("seasons", f"year=eq.{season_year}&select=id,year")
    if not rows:
        raise RuntimeError(f"No baseball.seasons row found for year={season_year}. Create it first.")
    return rows[0]["id"]

def get_teams_map(access_token, season_id):
    """Returns (yahoo_team_key -> team_uuid), (yahoo_team_key -> current_name)."""
    print("[Teams] Building teams map...")
    root = yahoo_get(access_token, f"league/{LEAGUE_KEY}/teams")

    sb_teams = sb_select("teams", f"season_id=eq.{season_id}")
    sb_by_yahoo_id = {t["yahoo_team_id"]: t["id"] for t in sb_teams if t.get("yahoo_team_id")}
    sb_by_name = {t["team_name"].strip().lower(): t["id"] for t in sb_teams if not t.get("yahoo_team_id")}

    teams_map = {}
    yahoo_key_to_name = {}
    name_updates = []
    for team_el in root.iter("team"):
        yahoo_key = team_el.findtext("team_key")
        name = (team_el.findtext("name") or "").strip()
        yahoo_key_to_name[yahoo_key] = name
        if yahoo_key in sb_by_yahoo_id:
            teams_map[yahoo_key] = sb_by_yahoo_id[yahoo_key]
        else:
            match = sb_by_name.get(name.lower())
            if match:
                teams_map[yahoo_key] = match
                name_updates.append({"id": match, "yahoo_team_id": yahoo_key})
            else:
                print(f"  [Teams] WARNING: '{yahoo_key}' (\"{name}\") not found in Supabase by yahoo_team_id or name. "
                      f"Not guessing -- fix manually.")

    for upd in name_updates:
        team_id = upd.pop("id")
        sb_patch_by_id("teams", team_id, upd)
    if name_updates:
        print(f"[Teams] Persisted yahoo_team_id for {len(name_updates)} newly-matched team(s).")

    print(f"[Teams] Mapped {len(teams_map)}/12 teams.")
    return teams_map, yahoo_key_to_name

def get_current_week(access_token):
    root = yahoo_get(access_token, f"league/{LEAGUE_KEY}")
    week = root.findtext(".//current_week")
    return int(week) if week else None


# ============================================================
# SECTION 8: PA / IP HELPERS
# ============================================================

def ip_to_decimal(ip_val):
    """Yahoo IP notation: 3.1 -> 3.333, 3.2 -> 3.667"""
    try:
        ip = float(ip_val)
        whole = int(ip)
        frac = round(ip - whole, 1)
        if frac == 0.1: return whole + 1/3
        if frac == 0.2: return whole + 2/3
        return float(whole)
    except Exception:
        return None

def derive_pa_and_bb_hbp(h, ab, obp, player_name="unknown"):
    if h is None or ab is None or obp is None: return None, None
    if ab == 0: return None, None
    if obp >= 1.0:
        print(f"  [PA Derive] {player_name}: OBP=1.000, skipping")
        return None, None
    bb_hbp = (obp * ab - h) / (1 - obp)
    pa = ab + bb_hbp
    if pa > 0:
        derived_obp = (h + bb_hbp) / pa
        if abs(derived_obp - obp) > OBP_TOLERANCE:
            print(f"  [PA Derive] Warning: {player_name} reported={obp:.3f} derived={derived_obp:.3f}")
    return round(pa, 1), round(bb_hbp, 1)

def parse_stat(stats_dict, stat_id):
    val = stats_dict.get(str(stat_id))
    if val in (None, "", "-", "N/A"): return None
    try: return float(val)
    except Exception: return None


# ============================================================
# SECTION 9: MODE - slot_stats
# Pulls YESTERDAY's roster (full, every slot) + YESTERDAY's true
# single-day box scores. Runs post game-lock, so "yesterday" is the
# day whose games actually finished.
# ============================================================

def run_slot_stats(access_token, season_id, teams_map, target_date, week_number):
    print(f"[SlotStats] Target date: {target_date.isoformat()} (week {week_number})")

    daily_rows = {}   # player_uuid -> row dict, for player_daily_stats
    mlb_rows = {}      # player_uuid -> row dict, for mlb_stats
    team_by_player = {}  # player_uuid -> team_uuid, for waiver-mode ownership seeding

    for yahoo_team_key, team_uuid in teams_map.items():
        root = yahoo_get(access_token, f"team/{yahoo_team_key}/roster;date={target_date.isoformat()}")
        for player_el in root.iter("player"):
            yahoo_pid = player_el.findtext(".//player_id")
            first_name = player_el.findtext(".//first") or ""
            last_name = player_el.findtext(".//last") or ""
            selected_pos = player_el.findtext(".//selected_position/position") or None

            ensure_player_exists(yahoo_pid, first_name, last_name)
            player_uuid = get_player_uuid(yahoo_pid)
            if not player_uuid:
                continue

            team_by_player[player_uuid] = team_uuid
            daily_rows[player_uuid] = {
                "player_id":         player_uuid,
                "team_id":           team_uuid,
                "season_id":         season_id,
                "week_number":       week_number,
                "stat_date":         target_date.isoformat(),
                "selected_position": selected_pos,
                # stats filled in below only if selected_pos is in the allow-list;
                # otherwise these stay null -- explicit, not omitted, so a bench/IL
                # day is visible in the audit trail rather than just absent.
                "h": None, "ab": None, "r": None, "hr": None, "rbi": None, "sb": None,
                "obp": None, "pa_est": None, "ip": None, "qs": None, "sv": None,
                "era": None, "whip": None, "k_per_9": None,
            }
        time.sleep(0.2)

    print(f"[SlotStats] {len(daily_rows)} rostered players across {len(teams_map)} teams.")

    # Pull true single-day box scores for the same date, for every player who
    # is either rostered (any slot) or currently a free agent with mlb_stats
    # history (roster-agnostic universe). For now: everyone rostered, plus a
    # sweep of all league players via the stats;type=date endpoint so FAs are
    # captured in mlb_stats too.
    HITTER_COUNT, SP_COUNT, RP_COUNT = 300, 150, 150
    pulls = [("B", HITTER_COUNT), ("SP", SP_COUNT), ("RP", RP_COUNT)]

    for player_type, total_count in pulls:
        start = 0
        while start < total_count:
            batch_size = min(25, total_count - start)
            endpoint = (
                f"league/{LEAGUE_KEY}/players"
                f";player_type={player_type}"
                f";start={start}"
                f";count={batch_size}"
                f"/stats;type=date;date={target_date.isoformat()}"
            )
            try:
                root = yahoo_get(access_token, endpoint)
            except Exception as e:
                print(f"  [SlotStats] Error pulling {player_type} start={start}: {e}")
                break

            player_els = list(root.iter("player"))
            if not player_els:
                break

            for player_el in player_els:
                yahoo_pid = player_el.findtext(".//player_id")
                first_name = player_el.findtext(".//first") or ""
                last_name = player_el.findtext(".//last") or ""
                full_name = f"{first_name} {last_name}".strip()

                ensure_player_exists(yahoo_pid, first_name, last_name)
                player_uuid = get_player_uuid(yahoo_pid)
                if not player_uuid:
                    continue

                stats_dict = {}
                for stat_el in player_el.iter("stat"):
                    sid = stat_el.findtext("stat_id")
                    val = stat_el.findtext("value")
                    if sid:
                        stats_dict[sid] = val

                h_ab_raw = stats_dict.get(STAT_IDS["h_ab"], "")
                if "/" in str(h_ab_raw):
                    parts = str(h_ab_raw).split("/")
                    try: h, ab = float(parts[0]), float(parts[1])
                    except Exception: h, ab = None, None
                else:
                    h, ab = None, None

                r    = parse_stat(stats_dict, STAT_IDS["r"])
                hr   = parse_stat(stats_dict, STAT_IDS["hr"])
                rbi  = parse_stat(stats_dict, STAT_IDS["rbi"])
                sb   = parse_stat(stats_dict, STAT_IDS["sb"])
                obp  = parse_stat(stats_dict, STAT_IDS["obp"])
                ip   = parse_stat(stats_dict, STAT_IDS["ip"])
                sv   = parse_stat(stats_dict, STAT_IDS["sv"])
                era  = parse_stat(stats_dict, STAT_IDS["era"])
                whip = parse_stat(stats_dict, STAT_IDS["whip"])
                k9   = parse_stat(stats_dict, STAT_IDS["k_per_9"])
                qs   = parse_stat(stats_dict, STAT_IDS["qs"])

                pa_est, _ = derive_pa_and_bb_hbp(h, ab, obp, full_name)
                ip_dec = ip_to_decimal(ip) if ip is not None else None
                if not ip_dec:                 # None or 0.0 IP -> rate stats undefined
                    era = whip = k9 = None

                stat_values = {
                    "h": h, "ab": ab, "r": r, "hr": hr, "rbi": rbi, "sb": sb,
                    "obp": obp, "pa_est": pa_est, "ip": ip_dec, "qs": qs, "sv": sv,
                    "era": era, "whip": whip, "k_per_9": k9,
                }

                # mlb_stats: roster-agnostic, everyone gets a row regardless of slot.
                team_uuid = team_by_player.get(player_uuid)  # None if free agent
                mlb_rows[player_uuid] = {
                    "player_id": player_uuid,
                    "current_fantasy_team_id": team_uuid,
                    "stat_date": target_date.isoformat(),
                    **stat_values,
                }

                # player_daily_stats: only fill stat values if this player
                # was rostered AND in a starter slot that day.
                row = daily_rows.get(player_uuid)
                if row and row["selected_position"] in STARTER_ALLOW_LIST:
                    row.update(stat_values)

            start += batch_size
            time.sleep(0.5)

    sb_upsert("player_daily_stats", list(daily_rows.values()))
    sb_upsert("mlb_stats", list(mlb_rows.values()))
    print(f"[SlotStats] Wrote {len(daily_rows)} player_daily_stats rows, {len(mlb_rows)} mlb_stats rows.")
    return team_by_player


# ============================================================
# SECTION 10: MODE - waiver
# ============================================================

def run_waiver(access_token, season_id, teams_map, target_date):
    print(f"[Waiver] Pulling transactions for {target_date.isoformat()}...")
    root = yahoo_get(access_token, f"league/{LEAGUE_KEY}/transactions;type=waiver,add,drop")

    team_uuid_by_key = teams_map
    rows = []
    ownership_updates = []  # (player_uuid, new_team_uuid_or_None)

    for txn_el in root.iter("transaction"):
        txn_date_ts = txn_el.findtext("timestamp")
        if not txn_date_ts:
            continue
        txn_date = datetime.utcfromtimestamp(int(txn_date_ts)).date()
        if txn_date != target_date:
            continue

        for p_el in txn_el.iter("player"):
            yahoo_pid = p_el.findtext(".//player_id")
            player_uuid = get_player_uuid(yahoo_pid)
            if not player_uuid:
                continue

            txn_data = p_el.find(".//transaction_data")
            if txn_data is None:
                continue
            move_type = txn_data.findtext("type")  # "add" or "drop"
            dest_key = txn_data.findtext("destination_team_key")
            source_key = txn_data.findtext("source_team_key")

            if move_type == "add":
                team_uuid = team_uuid_by_key.get(dest_key)
                faab = txn_el.findtext("faab_bid")
                rows.append({
                    "season_id": season_id, "team_id": team_uuid, "player_id": player_uuid,
                    "transaction_type": "add", "transaction_date": txn_date.isoformat(),
                    "faab_bid": int(faab) if faab else None,
                })
                ownership_updates.append((player_uuid, team_uuid))
            elif move_type == "drop":
                team_uuid = team_uuid_by_key.get(source_key)
                rows.append({
                    "season_id": season_id, "team_id": team_uuid, "player_id": player_uuid,
                    "transaction_type": "drop", "transaction_date": txn_date.isoformat(),
                    "faab_bid": None,
                })
                ownership_updates.append((player_uuid, None))

    sb_upsert("transactions", rows)
    print(f"[Waiver] Wrote {len(rows)} transaction rows.")

    for player_uuid, new_team_uuid in ownership_updates:
        url = f"{SUPABASE_URL}/rest/v1/mlb_stats?player_id=eq.{player_uuid}&stat_date=eq.{target_date.isoformat()}"
        r = requests_patch_with_retry(url, headers=sb_headers("baseball"),
                                       json={"current_fantasy_team_id": new_team_uuid})
        if r.status_code not in (200, 201, 204):
            print(f"  [Waiver] Warning updating ownership for {player_uuid}: {r.status_code}")
    print(f"[Waiver] Updated ownership on {len(ownership_updates)} mlb_stats rows.")


# ============================================================
# SECTION 11: TEAM WEEKLY STATS + MATCHUPS (folded-in from
# populate_team_weekly_stats.py). Used by both weekly_lock (current
# week only) and backfill mode (arbitrary historical weeks).
# ============================================================

def resolve_league_settings(access_token, league_key):
    data = yahoo_get_json(access_token, f"league/{league_key}/settings")
    settings = data["fantasy_content"]["league"][1]["settings"][0]
    stat_categories = settings["stat_categories"]["stats"]

    stat_map = {}
    for s in stat_categories:
        stat = s["stat"]
        sid = str(stat.get("stat_id"))
        label = (stat.get("display_name") or stat.get("name") or "").strip().lower()
        col = STAT_NAME_ALIASES.get(label)
        if col:
            stat_map[sid] = col

    playoff_start_week = settings.get("playoff_start_week")
    return stat_map, int(playoff_start_week) if playoff_start_week is not None else None

def parse_matchups_json(scoreboard_json, week, stat_map):
    matchups = []
    try:
        scoreboard = scoreboard_json["fantasy_content"]["league"][1]["scoreboard"]
        matchup_container = scoreboard["0"]["matchups"]
        count = int(matchup_container["count"])

        for i in range(count):
            m = matchup_container[str(i)]["matchup"]
            teams_block = m["0"]["teams"]
            team_count = int(teams_block["count"])
            teams_in_matchup = []

            for j in range(team_count):
                t = teams_block[str(j)]["team"]
                team_info = t[0]
                team_key = next((x["team_key"] for x in team_info if isinstance(x, dict) and "team_key" in x), None)
                stats_block = t[1].get("team_stats", {}).get("stats", [])
                stats = {}
                for s in stats_block:
                    stat = s["stat"]
                    sid = str(stat["stat_id"])
                    col = stat_map.get(sid)
                    if col:
                        val = stat.get("value")
                        stats[col] = None if val in (None, "", "-") else val
                teams_in_matchup.append({"team_key": team_key, "stats": stats})

            if len(teams_in_matchup) == 2:
                matchups.append({
                    "week": week,
                    "team_a": teams_in_matchup[0], "team_b": teams_in_matchup[1],
                    "team_a_wins": m["0"].get("stat_winners_count_team1"),
                    "team_b_wins": m["0"].get("stat_winners_count_team2"),
                })
    except (KeyError, IndexError, TypeError) as e:
        print(f"  ! Could not parse week {week} scoreboard: {e}")
    return matchups

def pull_team_weekly_stats(access_token, season_id, teams_map, stat_map, week, week_type):
    """Returns (matchup_rows, stat_rows, team_stats_by_key, matchup_pairs_by_key)
    -- the in-memory dicts are what weekly_lock reuses for FWA, avoiding a
    second read from the DB."""
    sb_data = yahoo_get_json(access_token, f"league/{LEAGUE_KEY}/scoreboard;week={week}")
    matchups = parse_matchups_json(sb_data, week, stat_map)

    matchup_rows, stat_rows = [], []
    team_stats_by_key = {}
    matchup_pairs_by_key = {}

    for m in matchups:
        team_a_id = teams_map.get(m["team_a"]["team_key"])
        team_b_id = teams_map.get(m["team_b"]["team_key"])
        if not team_a_id or not team_b_id:
            print(f"  ! Skipping unmapped matchup in week {week}: "
                  f"{m['team_a']['team_key']} vs {m['team_b']['team_key']}")
            continue

        matchup_rows.append({
            "season_id": season_id, "week_number": week, "week_type": week_type,
            "home_team_id": team_a_id, "away_team_id": team_b_id,
            "home_wins": m["team_a_wins"], "away_wins": m["team_b_wins"], "ties": 0,
        })
        matchup_pairs_by_key[m["team_a"]["team_key"]] = m["team_b"]["team_key"]
        matchup_pairs_by_key[m["team_b"]["team_key"]] = m["team_a"]["team_key"]

        for team_key, team_id, side in [(m["team_a"]["team_key"], team_a_id, "team_a"),
                                         (m["team_b"]["team_key"], team_b_id, "team_b")]:
            s = m[side]["stats"]
            row = {
                "season_id": season_id, "team_id": team_id, "week_number": week,
                "r": s.get("r"), "hr": s.get("hr"), "rbi": s.get("rbi"), "sb": s.get("sb"),
                "obp": s.get("obp"), "sv": s.get("sv"), "qs": s.get("qs"),
                "era": s.get("era"), "whip": s.get("whip"), "k9": s.get("k9"),
                "ip": s.get("ip"), "h": None, "ab": None, "pa": None,
                "updated_at": datetime.utcnow().isoformat(),
            }
            stat_rows.append(row)
            team_stats_by_key[team_key] = s

    if matchup_rows:
        sb_upsert("matchups", matchup_rows)
        sb_upsert("team_weekly_stats", stat_rows)
        print(f"[TeamWeekly] Week {week}: wrote {len(matchup_rows)} matchups, {len(stat_rows)} team-week rows.")
    else:
        print(f"[TeamWeekly] Week {week}: no matchups parsed -- nothing written.")

    return matchup_rows, stat_rows, team_stats_by_key, matchup_pairs_by_key


# ============================================================
# SECTION 12: FWA CALCULATION (unchanged formula, ported as-is)
# Requires matchup context. Only calculated for rostered players.
# ============================================================

def calculate_fwa(player_stats, all_roster_players, my_team_stats, opp_stats, is_pitcher):
    fwa = 0.0
    hitters  = [p for p in all_roster_players if not p.get("is_pitcher")]
    pitchers = [p for p in all_roster_players if p.get("is_pitcher")]

    if not is_pitcher:
        for cat in ["r", "hr", "rbi", "sb"]:
            my_total = sum(float(p.get(cat) or 0) for p in hitters)
            opp_val  = float(opp_stats.get(cat) or 0)
            p_val    = float(player_stats.get(cat) or 0)
            n        = max(1, len([p for p in hitters if float(p.get("ab") or 0) > 0]))
            if my_total > opp_val and p_val > 0:
                fwa += p_val / my_total if my_total > 0 else 0
            elif my_total == opp_val:
                if my_total > 0 and p_val > 0: fwa += 0.5 * p_val / my_total
                elif my_total == 0: fwa += 0.5 / n

        my_obp  = float(my_team_stats.get("obp") or 0)
        opp_obp = float(opp_stats.get("obp") or 0)
        p_obp   = float(player_stats.get("obp") or 0)
        p_pa    = float(player_stats.get("pa_est") or 0)
        if my_obp > opp_obp and p_obp > opp_obp and p_pa > 0:
            qual_pa = sum(float(p.get("pa_est") or 0) for p in hitters if float(p.get("obp") or 0) > opp_obp)
            fwa += p_pa / qual_pa if qual_pa > 0 else 0
        elif my_obp == opp_obp and p_obp >= opp_obp and p_pa > 0:
            qual_pa = sum(float(p.get("pa_est") or 0) for p in hitters if float(p.get("obp") or 0) >= opp_obp)
            fwa += 0.5 * p_pa / qual_pa if qual_pa > 0 else 0
    else:
        my_qs  = sum(float(p.get("qs") or 0) for p in pitchers)
        opp_qs = float(opp_stats.get("qs") or 0)
        p_qs   = float(player_stats.get("qs") or 0)
        n_pit  = max(1, len([p for p in pitchers if float(p.get("ip") or 0) > 0]))
        if my_qs > opp_qs and p_qs > 0:
            fwa += p_qs / my_qs if my_qs > 0 else 0
        elif my_qs == opp_qs:
            if my_qs > 0 and p_qs > 0: fwa += 0.5 * p_qs / my_qs
            elif my_qs == 0: fwa += 0.5 / n_pit

        my_sv  = sum(float(p.get("sv") or 0) for p in pitchers)
        opp_sv = float(opp_stats.get("sv") or 0)
        p_sv   = float(player_stats.get("sv") or 0)
        if my_sv > opp_sv and p_sv > 0:
            fwa += p_sv / my_sv if my_sv > 0 else 0
        elif my_sv == opp_sv:
            if my_sv > 0 and p_sv > 0: fwa += 0.5 * p_sv / my_sv

        my_era  = float(my_team_stats.get("era") or 0)
        opp_era = float(opp_stats.get("era") or 0)
        p_era   = float(player_stats.get("era") or 0)
        p_ip    = float(player_stats.get("ip") or 0)
        if my_era < opp_era and p_era < opp_era and p_ip > 0:
            rs = (opp_era - p_era) * p_ip / 9
            qual_rs = sum((opp_era - float(p.get("era") or 0)) * float(p.get("ip") or 0) / 9
                          for p in pitchers if float(p.get("era") or 0) < opp_era and float(p.get("ip") or 0) > 0)
            fwa += rs / qual_rs if qual_rs > 0 else 0
        elif my_era == opp_era and p_era <= opp_era and p_ip > 0:
            qual_ip = sum(float(p.get("ip") or 0) for p in pitchers if float(p.get("era") or 0) <= opp_era)
            fwa += 0.5 * p_ip / qual_ip if qual_ip > 0 else 0

        my_whip  = float(my_team_stats.get("whip") or 0)
        opp_whip = float(opp_stats.get("whip") or 0)
        p_whip   = float(player_stats.get("whip") or 0)
        if my_whip < opp_whip and p_whip < opp_whip and p_ip > 0:
            brs = (opp_whip - p_whip) * p_ip
            qual_brs = sum((opp_whip - float(p.get("whip") or 0)) * float(p.get("ip") or 0)
                           for p in pitchers if float(p.get("whip") or 0) < opp_whip and float(p.get("ip") or 0) > 0)
            fwa += brs / qual_brs if qual_brs > 0 else 0
        elif my_whip == opp_whip and p_whip <= opp_whip and p_ip > 0:
            qual_ip = sum(float(p.get("ip") or 0) for p in pitchers if float(p.get("whip") or 0) <= opp_whip)
            fwa += 0.5 * p_ip / qual_ip if qual_ip > 0 else 0

        my_k9  = float(my_team_stats.get("k9") or 0)
        opp_k9 = float(opp_stats.get("k9") or 0)
        p_k9   = float(player_stats.get("k_per_9") or 0)
        if my_k9 > opp_k9 and p_k9 > opp_k9 and p_ip > 0:
            ka = (p_k9/9 - opp_k9/9) * p_ip
            qual_ka = sum((float(p.get("k_per_9") or 0)/9 - opp_k9/9) * float(p.get("ip") or 0)
                          for p in pitchers if float(p.get("k_per_9") or 0) > opp_k9 and float(p.get("ip") or 0) > 0)
            fwa += ka / qual_ka if qual_ka > 0 else 0
        elif my_k9 == opp_k9 and p_k9 >= opp_k9 and p_ip > 0:
            qual_ip = sum(float(p.get("ip") or 0) for p in pitchers if float(p.get("k_per_9") or 0) >= opp_k9)
            fwa += 0.5 * p_ip / qual_ip if qual_ip > 0 else 0

    return round(fwa, 3)


# ============================================================
# SECTION 13: MODE - weekly_lock
# NOTE: step 3 (SQL aggregate + FER) calls baseball.aggregate_and_score_weekly()
# via RPC. That function does not exist yet -- see the note sent alongside this
# script. This mode will raise until that function is created.
# ============================================================

def run_weekly_lock(access_token, season_id, teams_map, closed_week):
    print(f"[WeeklyLock] Locking week {closed_week}...")

    # Step 1: team totals + matchups for the week that just closed.
    stat_map, playoff_start_week = resolve_league_settings(access_token, LEAGUE_KEY)
    week_type = "playoff" if (playoff_start_week is not None and closed_week >= playoff_start_week) else "regular"
    _, _, team_stats_by_key, matchup_pairs_by_key = pull_team_weekly_stats(
        access_token, season_id, teams_map, stat_map, closed_week, week_type
    )

    # Step 2/3: SQL aggregates player_daily_stats -> player_weekly_stats_v2 + FER,
    # returns the written rows directly via RPC (no re-SELECT).
    weekly_player_rows = sb_rpc("aggregate_and_score_weekly", {
        "p_season_id": season_id, "p_week_number": closed_week
    })
    print(f"[WeeklyLock] SQL aggregation returned {len(weekly_player_rows)} player-week rows.")

    # Step 4: FWA in Python, using team_stats_by_key/matchup_pairs_by_key already
    # in memory from step 1, plus weekly_player_rows already in memory from step 3.
    team_uuid_to_key = {v: k for k, v in teams_map.items()}
    roster_by_team = {}
    for row in weekly_player_rows:
        yahoo_key = team_uuid_to_key.get(row["team_id"])
        if not yahoo_key:
            continue
        roster_by_team.setdefault(yahoo_key, []).append({
            **row, "is_pitcher": row.get("ip") is not None and float(row.get("ip") or 0) > 0
                                  and float(row.get("ab") or 0) == 0
        })

    fwa_updates = []
    for row in weekly_player_rows:
        yahoo_key = team_uuid_to_key.get(row["team_id"])
        if not yahoo_key:
            continue
        opp_key = matchup_pairs_by_key.get(yahoo_key)
        if not opp_key:
            continue
        my_ts = team_stats_by_key.get(yahoo_key, {})
        opp_ts = team_stats_by_key.get(opp_key, {})
        roster = roster_by_team.get(yahoo_key, [])
        is_pitcher = float(row.get("ip") or 0) > 0 and float(row.get("ab") or 0) == 0
        fwa = calculate_fwa(row, roster, my_ts, opp_ts, is_pitcher)
        fwa_updates.append({"id": row["id"], "fwa_total": fwa})

    for upd in fwa_updates:
        sb_patch_by_id("player_weekly_stats_v2", upd["id"], {"fwa_total": upd["fwa_total"]})
    print(f"[WeeklyLock] Wrote fwa_total for {len(fwa_updates)} rows.")

    # Step 5: lock.
    for row in weekly_player_rows:
        sb_patch_by_id("player_weekly_stats_v2", row["id"], {"is_locked": True})
    print(f"[WeeklyLock] Locked week {closed_week}: {len(weekly_player_rows)} rows.")


# ============================================================
# SECTION 14: MODE - backfill (manual CLI)
# ============================================================

def run_backfill(access_token, season_year, weeks):
    season_id = resolve_season_id(season_year)
    league_key = SEASON_LEAGUE_KEYS[season_year]
    teams_map, _ = get_teams_map(access_token, season_id)
    stat_map, playoff_start_week = resolve_league_settings(access_token, league_key)

    for week in weeks:
        week_type = "playoff" if (playoff_start_week is not None and week >= playoff_start_week) else "regular"
        pull_team_weekly_stats(access_token, season_id, teams_map, stat_map, week, week_type)
        time.sleep(1)


# ============================================================
# SECTION 15: MAIN ORCHESTRATOR
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["slot_stats", "waiver", "backfill"], required=True)
    parser.add_argument("--season", type=int, default=SEASON_YEAR)
    parser.add_argument("--weeks", default=None, help="e.g. '1-12' -- only for --mode backfill")
    args = parser.parse_args()

    print("=" * 60)
    print(f"FRANCHISE MODE PIPELINE v9 * mode={args.mode} * {datetime.utcnow().isoformat()}Z")
    print("=" * 60)

    access_token = refresh_token()

    if args.mode == "backfill":
        if not args.weeks:
            print("FATAL: --mode backfill requires --weeks, e.g. --weeks 1-12")
            sys.exit(1)
        start, end = args.weeks.split("-") if "-" in args.weeks else (args.weeks, args.weeks)
        weeks = list(range(int(start), int(end) + 1))
        run_backfill(access_token, args.season, weeks)
        return

    season_id = resolve_season_id(SEASON_YEAR)
    teams_map, _ = get_teams_map(access_token, season_id)
    today = date.today()
    week_number = get_current_week(access_token)

    if args.mode == "slot_stats":
        # Post game-lock run: yesterday's games are the ones that finished.
        target_date = today - timedelta(days=1)
        run_slot_stats(access_token, season_id, teams_map, target_date, week_number)

        # Combined Monday run: also close out the week that just ended.
        if today.weekday() == 0:  # Monday
            closed_week = week_number - 1
            if closed_week >= 1:
                run_weekly_lock(access_token, season_id, teams_map, closed_week)
            else:
                print("[Main] Monday but no prior week to lock (week 1 in progress).")

    elif args.mode == "waiver":
        run_waiver(access_token, season_id, teams_map, today)

    print("=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("=" * 60)
        print(f"PIPELINE FAILED: {e}")
        print("=" * 60)
        raise
