"""
============================================================
FRANCHISE MODE · Master Pipeline Script
============================================================
Runs every morning via GitHub Actions (or manually).

Every day:
  - Refreshes Yahoo OAuth token
  - Pulls roster snapshots for all 12 teams
  - Pulls scoreboard for current week (matchup pairs + team stats)
  - Pulls player stats for full universe (3 windows)
  - Calculates FWA and FER, writes to weekly_metric_snapshots
    with is_locked = False (live, updating numbers)

Every Monday:
  - All of the above, then flips is_locked = True for the
    week that just ended. Locked rows are never touched again.

Credentials: oauth2.json in the same folder as this script
Supabase:    set SUPABASE_URL and SUPABASE_KEY as env vars
============================================================
"""

import os
import json
import base64
import time
import requests
import xml.etree.ElementTree as ET
from datetime import date, timedelta


# ============================================================
# RETRY WRAPPER
# Wraps any requests call with retry + exponential backoff.
# Catches SSL errors, connection errors, and timeouts.
# ============================================================

def requests_get_with_retry(url, retries=3, backoff=2, **kwargs):
    kwargs.setdefault("timeout", 30)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, **kwargs)
            return r
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = backoff * attempt
                print(f"  [Retry] Attempt {attempt} failed ({type(e).__name__}), retrying in {wait}s...")
                time.sleep(wait)
    raise last_err

def requests_post_with_retry(url, retries=3, backoff=2, **kwargs):
    kwargs.setdefault("timeout", 30)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, **kwargs)
            return r
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = backoff * attempt
                print(f"  [Retry] Attempt {attempt} failed ({type(e).__name__}), retrying in {wait}s...")
                time.sleep(wait)
    raise last_err

def requests_delete_with_retry(url, retries=3, backoff=2, **kwargs):
    kwargs.setdefault("timeout", 30)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.delete(url, **kwargs)
            return r
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = backoff * attempt
                print(f"  [Retry] Attempt {attempt} failed ({type(e).__name__}), retrying in {wait}s...")
                time.sleep(wait)
    raise last_err

# ============================================================
# CONFIG
# ============================================================
CREDS_FILE   = os.path.join(os.path.dirname(__file__), "oauth2.json")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

GAME_KEY     = "469"
LEAGUE_KEY   = "469.l.76761"
SEASON_YEAR  = 2026

YAHOO_BASE   = "https://fantasysports.yahooapis.com/fantasy/v2"

WINDOWS = {
    "7d":     "lastweek",
    "30d":    "lastmonth",
    "season": "season"
}

HITTER_COUNT = 300
SP_COUNT     = 150
RP_COUNT     = 150

OBP_TOLERANCE = 0.005

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

# Yahoo stat ID -> category name for scoreboard parsing
CAT_STAT_MAP = {
    "7":"R","12":"HR","13":"RBI","16":"SB","4":"OBP",
    "26":"ERA","27":"wHIP","57":"K9","83":"QS","32":"SV"
}

ERA_BASE  = 3.71   # league average ERA baseline for runs saved
WHIP_BASE = 1.19   # league average wHIP baseline for BR saved


# ============================================================
# SECTION 1: FER LOOKUP TABLES
# Calibrated from 2022-2026 Franchise XII data (n=7,387 weeks).
# Counting stats: ceiling logic for floor band (normal = credit)
# Rate stats: floor logic (you beat exactly who you beat)
# Top tier = 99 (record-setters only)
# All other tops capped at 98
# ============================================================

# -- HITTERS --
R_TABLE   = {0:5, 1:16,2:34,3:56,4:73,5:85,6:92,7:96,8:98,9:98,10:98,11:98,12:99}
HR_TABLE  = {0:38,1:72,2:91,3:97,4:98,5:98,6:98,7:99}
RBI_TABLE = {0:9, 1:24,2:42,3:59,4:73,5:83,6:90,7:94,8:97,9:98,10:98,
             11:98,12:98,13:98,14:98,15:98,16:98,17:99}
SB_TABLE  = {0:63,1:85,2:95,3:98,4:98,5:98,6:98,7:98,8:99}
OBP_BANDS = [
    (0.000,0.050,0),(0.050,0.100,0),(0.100,0.150,1),(0.150,0.200,3),
    (0.200,0.250,8),(0.250,0.300,19),(0.300,0.350,36),(0.350,0.400,55),
    (0.400,0.450,72),(0.450,0.500,86),(0.500,0.550,93),(0.550,0.600,98),
    (0.600,0.722,98),(0.722,999,99)
]
MIN_PA = 14

# -- SP (4 categories, zone-aware) --
# Zone 1: 5.0-7.2 IP | Zone 2: 8.0+ IP
QS_SP     = {0:35,1:94,2:98}
ERA_SP_Z1 = [(0.00,1.50,98),(1.50,2.70,51),(2.70,3.95,30),(3.95,5.11,20),(5.11,6.75,11),(6.75,999,0)]
ERA_SP_Z2 = [(0.00,1.50,98),(1.50,2.70,49),(2.70,3.95,31),(3.95,5.11,19),(5.11,6.75,9),(6.75,999,0)]
WHIP_SP_Z1= [(0.00,0.001,99),(0.001,0.30,98),(0.30,0.70,81),(0.70,1.00,60),
             (1.00,1.19,37),(1.19,1.42,21),(1.42,1.75,9),(1.75,999,0)]
WHIP_SP_Z2= [(0.00,0.001,99),(0.001,0.30,98),(0.30,0.70,76),(0.70,1.00,54),
             (1.00,1.19,36),(1.19,1.42,21),(1.42,1.75,6),(1.75,999,0)]
K9_SP_Z1  = [(0.0,5.0,0),(5.0,7.0,10),(7.0,9.0,25),(9.0,11.0,40),(11.0,13.0,70),
             (13.0,16.0,85),(16.0,23.4,98),(23.4,999,99)]
K9_SP_Z2  = [(0.0,5.0,0),(5.0,7.0,4),(7.0,9.0,22),(9.0,11.0,44),(11.0,13.0,75),
             (13.0,16.0,90),(16.0,23.4,98),(23.4,999,99)]

# -- RP (4 categories) --
SV_TABLE  = {0:35,1:71,2:93,3:98,4:98,5:99}
ERA_RP    = [(0.00,0.01,98),(0.01,3.00,38),(3.00,5.00,22),(5.00,9.00,14),(9.00,999,0)]
WHIP_RP   = [(0.00,0.001,98),(0.001,0.34,80),(0.34,0.51,71),(0.51,0.68,63),(0.68,1.01,39),
             (1.01,1.34,30),(1.34,1.51,23),(1.51,2.01,13),(2.01,3.01,3),(3.01,999,0)]
K9_RP     = [(0,6,0),(6,9,18),(9,12,30),(12,15,57),(15,18,75),(18,21,84),(21,27,94),(27,999,99)]

# -- FER Band Labels --
# Calibrated from v5 distribution: mean=55.4, median=56.5
FER_BANDS = [
    (90, "Elite"),
    (80, "Great"),
    (70, "Above Avg"),
    (55, "Average"),
    (40, "Below Avg"),
    (25, "Poor"),
    (0,  "Awful"),
]


# ============================================================
# SECTION 2: FER CALCULATION HELPERS
# ============================================================

def lookup_table(val, table):
    keys = sorted(table.keys())
    result = table[keys[0]]
    for k in keys:
        if val >= k: result = table[k]
        else: break
    return result

def lookup_band(val, bands):
    for lo, hi, score in bands:
        if val < hi: return score
    return bands[-1][2]

def ip_to_decimal(ip_val):
    """Convert Yahoo IP notation: 3.1 -> 3.333, 3.2 -> 3.667"""
    try:
        ip = float(ip_val)
        whole = int(ip)
        frac  = round(ip - whole, 1)
        if frac == 0.1: return whole + 1/3
        if frac == 0.2: return whole + 2/3
        return float(whole)
    except:
        return 0.0

def calc_hitter_fer(r, hr, rbi, sb, obp, pa):
    if pa is None or pa < MIN_PA: return None
    r_p  = lookup_table(min(int(r or 0), 12), R_TABLE)
    hr_p = lookup_table(min(int(hr or 0), 7),  HR_TABLE)
    rb_p = lookup_table(min(int(rbi or 0), 17),RBI_TABLE)
    sb_p = lookup_table(min(int(sb or 0), 8),  SB_TABLE)
    op_p = lookup_band(obp or 0, OBP_BANDS)
    return round((r_p + hr_p + rb_p + sb_p + op_p) / 5, 1)

def calc_sp_fer(qs, era, whip, k9, ip):
    if ip is None or ip < 5.0: return None
    z2 = ip >= 8.0
    q  = QS_SP.get(min(int(qs or 0), 2), 98)
    e  = lookup_band(era or 0,  [(lo,hi,s2 if z2 else s1)
         for (lo,hi,s1),(lo2,hi2,s2) in zip(ERA_SP_Z1, ERA_SP_Z2)])
    w  = lookup_band(whip or 0, [(lo,hi,s2 if z2 else s1)
         for (lo,hi,s1),(lo2,hi2,s2) in zip(WHIP_SP_Z1, WHIP_SP_Z2)])
    k  = lookup_band(k9 or 0,   [(lo,hi,s2 if z2 else s1)
         for (lo,hi,s1),(lo2,hi2,s2) in zip(K9_SP_Z1,   K9_SP_Z2)])
    return round((q + e + w + k) / 4, 1)

def calc_rp_fer(sv, era, whip, k9):
    s = lookup_table(min(int(sv or 0), 5), SV_TABLE)
    e = lookup_band(era or 0,  ERA_RP)
    w = lookup_band(whip or 0, WHIP_RP)
    k = lookup_band(k9 or 0,   K9_RP)
    return round((s + e + w + k) / 4, 1)

def fer_band_label(score):
    if score is None: return None
    for threshold, label in FER_BANDS:
        if score >= threshold: return label
    return "Awful"


# ============================================================
# SECTION 3: TOKEN MANAGEMENT
# ============================================================

def load_creds():
    # If running in GitHub Actions, build creds from environment variables
    if os.environ.get("YAHOO_CONSUMER_KEY"):
        return {
            "consumer_key":    os.environ["YAHOO_CONSUMER_KEY"],
            "consumer_secret": os.environ["YAHOO_CONSUMER_SECRET"],
            "access_token":    os.environ["YAHOO_ACCESS_TOKEN"],
            "refresh_token":   os.environ["YAHOO_REFRESH_TOKEN"],
            "token_time":      0
        }
    # Otherwise load from local file (running on your laptop)
    with open(CREDS_FILE) as f:
        return json.load(f)

def save_creds(creds):
    # Only save locally — in GitHub Actions there's no persistent file
    if os.environ.get("YAHOO_CONSUMER_KEY"):
        return
    with open(CREDS_FILE, "w") as f:
        json.dump(creds, f, indent=4)

def refresh_token():
    print("[Token] Refreshing Yahoo OAuth token...")
    creds = load_creds()
    encoded = base64.b64encode(
        f"{creds['consumer_key']}:{creds['consumer_secret']}".encode()
    ).decode()
    r = requests_post_with_retry(
        "https://api.login.yahoo.com/oauth2/get_token",
        headers={
            "Authorization": f"Basic {encoded}",
            "Content-Type":  "application/x-www-form-urlencoded"
        },
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
# SECTION 4: YAHOO API HELPERS
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
                "Accept":        "application/xml"
            }, timeout=30)
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
# SECTION 5: SUPABASE HELPERS
# ============================================================
def sb_headers(schema="public"):
    h = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=representation"
    }
    if schema != "public":
        h["Accept-Profile"]  = schema
        h["Content-Profile"] = schema
    return h

def sb_select(table, filters=""):
    schema, tbl = table.split(".") if "." in table else ("public", table)
    url = f"{SUPABASE_URL}/rest/v1/{tbl}"
    if filters: url += f"?{filters}"
    r = requests_get_with_retry(url, headers=sb_headers(schema))
    if r.status_code != 200:
        raise Exception(f"[Supabase] Select failed on {table}: {r.text[:300]}")
    return r.json()

def sb_upsert_by_id(table, rows):
    """Update rows in-place by primary key (id). Used for field-level updates."""
    if not rows: return
    schema, tbl = table.split(".") if "." in table else ("public", table)
    for row in rows:
        row_id = row.pop("id")
        url = f"{SUPABASE_URL}/rest/v1/{tbl}?id=eq.{row_id}"
        r = requests_post_with_retry(
            url,
            headers={**sb_headers(schema), "Prefer": "resolution=merge-duplicates"},
            json=row
        )
        if r.status_code not in (200, 201, 204):
            print(f"[Supabase] Warning updating {table} id={row_id}: {r.status_code} {r.text[:200]}")
        row["id"] = row_id  # restore so caller's dict isn't mutated

# Conflict column map — tells Supabase which columns to use for upsert
UPSERT_CONFLICT = {
    "pipeline.roster_snapshots":        "player_id,snapshot_date",
    "pipeline.player_stats_daily":      "player_id,window_type,pulled_date",
    "pipeline.weekly_metric_snapshots": "player_id,week_number,season_year",
}

def sb_upsert(table, rows):
    if not rows: return
    schema, tbl = table.split(".") if "." in table else ("public", table)
    conflict_cols = UPSERT_CONFLICT.get(table, "")
    url = f"{SUPABASE_URL}/rest/v1/{tbl}"
    if conflict_cols:
        url += f"?on_conflict={conflict_cols}"
    for i in range(0, len(rows), 100):
        batch = rows[i:i+100]
        r = requests_post_with_retry(url, headers=sb_headers(schema), json=batch)
        if r.status_code not in (200, 201):
            print(f"[Supabase] Warning on {table}: {r.status_code} {r.text[:300]}")

def sb_delete_old(table, date_col, days_back):
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    schema, tbl = table.split(".") if "." in table else ("public", table)
    url = f"{SUPABASE_URL}/rest/v1/{tbl}?{date_col}=lt.{cutoff}"
    r = requests_delete_with_retry(url, headers=sb_headers(schema))
    print(f"[Cleanup] {table}: removed rows before {cutoff} (status {r.status_code})")

# ============================================================
# SECTION 6: PLAYER REGISTRY
# ============================================================

def ensure_player_exists(yahoo_player_id, first_name, last_name):
    row = {
        "yahoo_player_id": str(yahoo_player_id),
        "first_name":      first_name,
        "last_name":       last_name
    }
    r = requests_post_with_retry(
        f"{SUPABASE_URL}/rest/v1/players",
        headers=sb_headers("baseball"),
        json=row
    )
    if r.status_code not in (200, 201, 409):
        print(f"[Players] Warning: {first_name} {last_name} ({yahoo_player_id}): {r.status_code}")

def get_player_uuid(yahoo_player_id):
    r = requests_get_with_retry(
        f"{SUPABASE_URL}/rest/v1/players?yahoo_player_id=eq.{yahoo_player_id}&select=id",
        headers=sb_headers("baseball")
    )
    rows = r.json()
    return rows[0]["id"] if rows else None


# ============================================================
# SECTION 7: TEAMS MAP
# ============================================================
def get_teams_map(access_token):
    print("[Teams] Building teams map...")
    root = yahoo_get(access_token, f"league/{LEAGUE_KEY}/teams")

    seasons = sb_select("baseball.seasons", f"year=eq.{SEASON_YEAR}")
    if not seasons:
        raise Exception(f"[Teams] No season row found for {SEASON_YEAR}")
    season_uuid = seasons[0]["id"]

    sb_teams = sb_select("baseball.teams", f"season_id=eq.{season_uuid}")
    sb_by_yahoo_id = {t["yahoo_team_id"]: t["id"] for t in sb_teams}

    teams_map = {}
    yahoo_key_to_name = {}
    for team_el in root.iter("team"):
        yahoo_key = team_el.findtext("team_key")
        name      = (team_el.findtext("name") or "").strip()
        if yahoo_key in sb_by_yahoo_id:
            teams_map[yahoo_key]         = sb_by_yahoo_id[yahoo_key]
            yahoo_key_to_name[yahoo_key] = name
        else:
            print(f"  [Teams] Warning: '{yahoo_key}' ({name}) not found in Supabase")

    # Auto-sync current Yahoo team names back to Supabase
    name_updates = []
    for yahoo_key, team_uuid in teams_map.items():
        current_name = yahoo_key_to_name.get(yahoo_key)
        if current_name:
            name_updates.append({
                "id":        team_uuid,
                "team_name": current_name
            })
    if name_updates:
        sb_upsert_by_id("baseball.teams", name_updates)
        print(f"[Teams] Synced {len(name_updates)} team names.")

    print(f"[Teams] Mapped {len(teams_map)} of 12 teams.")
    return teams_map, yahoo_key_to_name

# ============================================================
# SECTION 8: SCOREBOARD + TEAM STATS
# Pulls matchup pairs and authoritative team category totals.
# Returns:
#   matchup_pairs: {team_key: opp_team_key}
#   team_stats:    {team_key: {R, HR, RBI, SB, OBP, QS, SV, ERA, wHIP, K9, IP}}
# ============================================================

def pull_scoreboard(access_token, week_number):
    print(f"[Scoreboard] Pulling week {week_number} scoreboard...")
    root = yahoo_get(access_token,
        f"league/{LEAGUE_KEY}/scoreboard;week={week_number}")

    matchup_pairs = {}
    team_stats    = {}

    for matchup in root.iter("matchup"):
        pair = []
        for team in matchup.iter("team"):
            tk    = team.findtext("team_key")
            stats = {}
            for stat in team.iter("stat"):
                sid = stat.findtext("stat_id")
                val = stat.findtext("value")
                if sid in CAT_STAT_MAP and val:
                    try: stats[CAT_STAT_MAP[sid]] = float(val)
                    except: pass
            team_stats[tk] = stats
            pair.append(tk)
        if len(pair) == 2:
            matchup_pairs[pair[0]] = pair[1]
            matchup_pairs[pair[1]] = pair[0]

    # Pull authoritative team stats (includes dropped players)
    for team_key in list(team_stats.keys()):
        try:
            troot = yahoo_get(access_token,
                f"team/{team_key}/stats;type=week;week={week_number}")
            for stat in troot.iter("stat"):
                sid = stat.findtext("stat_id")
                val = stat.findtext("value")
                if val and val not in ("-",""):
                    if sid in CAT_STAT_MAP:
                        try: team_stats[team_key][CAT_STAT_MAP[sid]] = float(val)
                        except: pass
                    elif sid == "50":  # IP
                        try: team_stats[team_key]["IP"] = ip_to_decimal(val)
                        except: pass
            time.sleep(0.2)
        except Exception as e:
            print(f"  [Scoreboard] Warning pulling team stats for {team_key}: {e}")

    print(f"[Scoreboard] {len(matchup_pairs)//2} matchups, {len(team_stats)} teams.")
    return matchup_pairs, team_stats


# ============================================================
# SECTION 9: ROSTER SNAPSHOTS
# ============================================================

def pull_roster_snapshots(access_token, today, teams_map):
    print(f"[Rosters] Pulling snapshots for {today}...")
    yesterday = (today - timedelta(days=1)).isoformat()

    existing = sb_select("pipeline.roster_snapshots",
                         f"snapshot_date=eq.{yesterday}")
    yesterday_players = {row["player_id"] for row in existing}

    rows = []
    for yahoo_team_key, team_uuid in teams_map.items():
        root = yahoo_get(access_token, f"team/{yahoo_team_key}/roster")
        for player_el in root.iter("player"):
            yahoo_pid  = player_el.findtext(".//player_id")
            first_name = player_el.findtext(".//first") or ""
            last_name  = player_el.findtext(".//last") or ""
            ensure_player_exists(yahoo_pid, first_name, last_name)
            player_uuid = get_player_uuid(yahoo_pid)
            if not player_uuid: continue
            rows.append({
                "player_id":        player_uuid,
                "team_id":          team_uuid,
                "snapshot_date":    today.isoformat(),
                "acquisition_type": "fa_pickup",
                "first_day":        player_uuid not in yesterday_players
            })
        time.sleep(0.2)

    sb_upsert("pipeline.roster_snapshots", rows)
    print(f"[Rosters] Wrote {len(rows)} roster rows.")
    return {row["player_id"]: row["team_id"] for row in rows}


# ============================================================
# SECTION 10: PA AND BB+HBP DERIVATION
# ============================================================

def derive_pa_and_bb_hbp(h, ab, obp, player_name="unknown"):
    if h is None or ab is None or obp is None: return None, None
    if ab == 0: return None, None
    if obp >= 1.0:
        print(f"  [PA Derive] {player_name}: OBP=1.000, skipping")
        return None, None
    bb_hbp = (obp * ab - h) / (1 - obp)
    pa     = ab + bb_hbp
    if pa > 0:
        derived_obp = (h + bb_hbp) / pa
        if abs(derived_obp - obp) > OBP_TOLERANCE:
            print(f"  [PA Derive] Warning: {player_name} "
                  f"reported={obp:.3f} derived={derived_obp:.3f}")
    return round(pa, 1), round(bb_hbp, 1)


# ============================================================
# SECTION 11: PLAYER STATS PULL
# ============================================================

def parse_stat(stats_dict, stat_id):
    val = stats_dict.get(str(stat_id))
    if val in (None, "", "-", "N/A"): return None
    try: return float(val)
    except: return None

def pull_player_stats(access_token, today):
    print(f"[Stats] Pulling player stats for {today}...")
    all_rows = []

    pulls = [
        ("B",  "R",   HITTER_COUNT, "hitters"),
        ("SP", "INN", SP_COUNT,     "SP"),
        ("RP", "INN", RP_COUNT,     "RP"),
    ]

    for window_label, yahoo_stat_type in WINDOWS.items():
        print(f"  Window: {window_label}")
        for player_type, sort_stat, total_count, label in pulls:
            start = 0
            while start < total_count:
                batch_size = min(25, total_count - start)
                endpoint = (
                    f"league/{LEAGUE_KEY}/players"
                    f";sort={sort_stat}"
                    f";sort_type={yahoo_stat_type}"
                    f";player_type={player_type}"
                    f";start={start}"
                    f";count={batch_size}"
                    f"/stats;type={yahoo_stat_type}"
                )
                try:
                    root = yahoo_get(access_token, endpoint)
                except Exception as e:
                    print(f"  [Stats] Error {label} w={window_label} s={start}: {e}")
                    break

                player_els = list(root.iter("player"))
                if not player_els: break

                for player_el in player_els:
                    yahoo_pid  = player_el.findtext(".//player_id")
                    first_name = player_el.findtext(".//first") or ""
                    last_name  = player_el.findtext(".//last") or ""
                    position   = player_el.findtext(".//display_position") or ""
                    full_name  = f"{first_name} {last_name}".strip()

                    ensure_player_exists(yahoo_pid, first_name, last_name)
                    player_uuid = get_player_uuid(yahoo_pid)
                    if not player_uuid: continue

                    stats_dict = {}
                    for stat_el in player_el.iter("stat"):
                        sid = stat_el.findtext("stat_id")
                        val = stat_el.findtext("value")
                        if sid: stats_dict[sid] = val

                    own_pct = None
                    own_el = player_el.find(".//percent_owned")
                    if own_el is not None:
                        try: own_pct = float(own_el.findtext("value") or 0)
                        except: pass

                    h_ab_raw = stats_dict.get(STAT_IDS["h_ab"], "")
                    if "/" in str(h_ab_raw):
                        parts = str(h_ab_raw).split("/")
                        try: h = float(parts[0]); ab = float(parts[1])
                        except: h, ab = None, None
                    else:
                        h, ab = None, None

                    r   = parse_stat(stats_dict, STAT_IDS["r"])
                    hr  = parse_stat(stats_dict, STAT_IDS["hr"])
                    rbi = parse_stat(stats_dict, STAT_IDS["rbi"])
                    sb  = parse_stat(stats_dict, STAT_IDS["sb"])
                    obp = parse_stat(stats_dict, STAT_IDS["obp"])
                    ip  = parse_stat(stats_dict, STAT_IDS["ip"])
                    sv  = parse_stat(stats_dict, STAT_IDS["sv"])
                    era = parse_stat(stats_dict, STAT_IDS["era"])
                    whip= parse_stat(stats_dict, STAT_IDS["whip"])
                    k9  = parse_stat(stats_dict, STAT_IDS["k_per_9"])
                    qs  = parse_stat(stats_dict, STAT_IDS["qs"])

                    pa_est, bb_hbp_est = derive_pa_and_bb_hbp(h, ab, obp, full_name)

                    # Convert IP to decimal
                    ip_dec = ip_to_decimal(ip) if ip else None

                    all_rows.append({
                        "player_id":     player_uuid,
                        "position":      position,
                        "pulled_date":   today.isoformat(),
                        "window_type":   window_label,
                        "h":             h,
                        "ab":            ab,
                        "r":             r,
                        "hr":            hr,
                        "rbi":           rbi,
                        "sb":            sb,
                        "obp":           obp,
                        "pa_est":        pa_est,
                        "bb_hbp_est":    bb_hbp_est,
                        "ip":            ip_dec,
                        "qs":            qs,
                        "sv":            sv,
                        "era":           era,
                        "k_per_9":       k9,
                        "whip":          whip,
                        "ownership_pct": own_pct
                    })

                start += batch_size
                time.sleep(0.5)

    print(f"[Stats] Writing {len(all_rows)} rows...")
    sb_upsert("pipeline.player_stats_daily", all_rows)
    print(f"[Stats] Done.")
    return all_rows


# ============================================================
# SECTION 12: FWA CALCULATION
# Franchise Wins Added — proportional credit for category wins.
# Requires matchup context (opponent stats + team totals).
# Returns float (sum of fractional wins across categories).
# Only calculated for rostered players — FA always gets None.
# ============================================================

def calculate_fwa(player_stats, all_roster_players, my_team_stats, opp_stats, position):
    """
    player_stats:       dict of this player's individual stat line
    all_roster_players: list of all stat dicts for this team (for proportional calc)
    my_team_stats:      authoritative team totals from Yahoo team endpoint
    opp_stats:          opponent's category totals from scoreboard
    position:           'B' (batter) or 'SP'/'RP' (pitcher)
    """
    fwa = 0.0
    is_pitcher = position in ("SP","RP","P")
    hitters    = [p for p in all_roster_players if p.get("is_pitcher") == False]
    pitchers   = [p for p in all_roster_players if p.get("is_pitcher") == True]

    if not is_pitcher:
        # ── Counting hitting stats ────────────────────────────────────────────
        for cat in ["r","hr","rbi","sb"]:
            my_total = sum(float(p.get(cat) or 0) for p in hitters)
            opp_val  = float(opp_stats.get(cat.upper(), 0))
            p_val    = float(player_stats.get(cat) or 0)
            n        = max(1, len([p for p in hitters if float(p.get("ab") or 0) > 0]))

            if my_total > opp_val and p_val > 0:
                fwa += p_val / my_total if my_total > 0 else 0
            elif my_total == opp_val:
                if my_total > 0 and p_val > 0:
                    fwa += 0.5 * p_val / my_total
                elif my_total == 0:
                    fwa += 0.5 / n

        # ── OBP ──────────────────────────────────────────────────────────────
        my_obp  = float(my_team_stats.get("OBP", 0))
        opp_obp = float(opp_stats.get("OBP", 0))
        p_obp   = float(player_stats.get("obp") or 0)
        p_pa    = float(player_stats.get("pa_est") or 0)

        if my_obp > opp_obp and p_obp > opp_obp and p_pa > 0:
            qual_pa = sum(float(p.get("pa_est") or 0) for p in hitters
                          if float(p.get("obp") or 0) > opp_obp)
            fwa += p_pa / qual_pa if qual_pa > 0 else 0
        elif my_obp == opp_obp and p_obp >= opp_obp and p_pa > 0:
            qual_pa = sum(float(p.get("pa_est") or 0) for p in hitters
                          if float(p.get("obp") or 0) >= opp_obp)
            fwa += 0.5 * p_pa / qual_pa if qual_pa > 0 else 0

    else:
        # ── QS ───────────────────────────────────────────────────────────────
        my_qs  = sum(float(p.get("qs") or 0) for p in pitchers)
        opp_qs = float(opp_stats.get("QS", 0))
        p_qs   = float(player_stats.get("qs") or 0)
        n_pit  = max(1, len([p for p in pitchers if float(p.get("ip") or 0) > 0]))

        if my_qs > opp_qs and p_qs > 0:
            fwa += p_qs / my_qs if my_qs > 0 else 0
        elif my_qs == opp_qs:
            if my_qs > 0 and p_qs > 0: fwa += 0.5 * p_qs / my_qs
            elif my_qs == 0: fwa += 0.5 / n_pit

        # ── SV ───────────────────────────────────────────────────────────────
        my_sv  = sum(float(p.get("sv") or 0) for p in pitchers)
        opp_sv = float(opp_stats.get("SV", 0))
        p_sv   = float(player_stats.get("sv") or 0)

        if my_sv > opp_sv and p_sv > 0:
            fwa += p_sv / my_sv if my_sv > 0 else 0
        elif my_sv == opp_sv:
            if my_sv > 0 and p_sv > 0: fwa += 0.5 * p_sv / my_sv

        # ── ERA (runs saved) ─────────────────────────────────────────────────
        my_era  = float(my_team_stats.get("ERA", 0))
        opp_era = float(opp_stats.get("ERA", 0))
        p_era   = float(player_stats.get("era") or 0)
        p_ip    = float(player_stats.get("ip") or 0)
        team_ip = float(my_team_stats.get("IP", 0))

        if my_era < opp_era and p_era < opp_era and p_ip > 0:
            rs      = (opp_era - p_era) * p_ip / 9
            qual_rs = sum((opp_era - float(p.get("era") or 0)) * float(p.get("ip") or 0) / 9
                          for p in pitchers
                          if float(p.get("era") or 0) < opp_era and float(p.get("ip") or 0) > 0)
            fwa += rs / qual_rs if qual_rs > 0 else 0
        elif my_era == opp_era and p_era <= opp_era and p_ip > 0:
            qual_ip = sum(float(p.get("ip") or 0) for p in pitchers
                          if float(p.get("era") or 0) <= opp_era)
            fwa += 0.5 * p_ip / qual_ip if qual_ip > 0 else 0

        # ── wHIP (BR saved) ──────────────────────────────────────────────────
        my_whip  = float(my_team_stats.get("wHIP", 0))
        opp_whip = float(opp_stats.get("wHIP", 0))
        p_whip   = float(player_stats.get("whip") or 0)

        if my_whip < opp_whip and p_whip < opp_whip and p_ip > 0:
            brs      = (opp_whip - p_whip) * p_ip
            qual_brs = sum((opp_whip - float(p.get("whip") or 0)) * float(p.get("ip") or 0)
                           for p in pitchers
                           if float(p.get("whip") or 0) < opp_whip and float(p.get("ip") or 0) > 0)
            fwa += brs / qual_brs if qual_brs > 0 else 0
        elif my_whip == opp_whip and p_whip <= opp_whip and p_ip > 0:
            qual_ip = sum(float(p.get("ip") or 0) for p in pitchers
                          if float(p.get("whip") or 0) <= opp_whip)
            fwa += 0.5 * p_ip / qual_ip if qual_ip > 0 else 0

        # ── K/9 ──────────────────────────────────────────────────────────────
        my_k9   = float(my_team_stats.get("K9", 0))
        opp_k9  = float(opp_stats.get("K9", 0))
        p_k9    = float(player_stats.get("k_per_9") or 0)

        if my_k9 > opp_k9 and p_k9 > opp_k9 and p_ip > 0:
            ka      = (p_k9/9 - opp_k9/9) * p_ip
            qual_ka = sum((float(p.get("k_per_9") or 0)/9 - opp_k9/9) * float(p.get("ip") or 0)
                          for p in pitchers
                          if float(p.get("k_per_9") or 0) > opp_k9 and float(p.get("ip") or 0) > 0)
            fwa += ka / qual_ka if qual_ka > 0 else 0
        elif my_k9 == opp_k9 and p_k9 >= opp_k9 and p_ip > 0:
            qual_ip = sum(float(p.get("ip") or 0) for p in pitchers
                          if float(p.get("k_per_9") or 0) >= opp_k9)
            fwa += 0.5 * p_ip / qual_ip if qual_ip > 0 else 0

    return round(fwa, 3)


# ============================================================
# SECTION 13: DAILY METRIC WRITE
# ============================================================

def write_daily_metrics(today, rostered_map, teams_map, week_number,
                        stat_rows_7d, matchup_pairs, team_stats):
    print(f"[Metrics] Calculating FWA + FER for week {week_number}...")

    # Get already-locked rows to skip
    locked = sb_select(
        "pipeline.weekly_metric_snapshots",
        f"week_number=eq.{week_number}&season_year=eq.{SEASON_YEAR}&is_locked=eq.true"
    )
    locked_pids = {row["player_id"] for row in locked}

    # Build lookup: player_uuid -> stat row
    stat_by_pid = {row["player_id"]: row for row in stat_rows_7d}

    # Build team_uuid -> yahoo_team_key from teams_map (authoritative source)
    # teams_map is {yahoo_team_key: team_uuid} — invert it
    team_uuid_to_key = {v: k for k, v in teams_map.items()}
    print(f"[Metrics] team_uuid_to_key has {len(team_uuid_to_key)} entries")

    # Group roster players by team for FWA
    team_roster_stats = {}  # yahoo_team_key -> [stat_rows with is_pitcher flag]
    for pid, team_uuid in rostered_map.items():
        yahoo_key = team_uuid_to_key.get(team_uuid)
        if not yahoo_key: continue
        if yahoo_key not in team_roster_stats:
            team_roster_stats[yahoo_key] = []
        stat_row = stat_by_pid.get(pid, {})
        ip_val = float(stat_row.get("ip") or 0)
        ab_val = float(stat_row.get("ab") or 0)
        is_pitcher = ip_val > 0 and ab_val == 0
        stat_row_copy = dict(stat_row)
        stat_row_copy["is_pitcher"] = is_pitcher
        team_roster_stats[yahoo_key].append(stat_row_copy)

    rows = []
    fer_count = fwa_count = 0

    for pid, stat_row in stat_by_pid.items():
        if pid in locked_pids: continue

        is_owned  = pid in rostered_map
        team_uuid = rostered_map.get(pid)
        basis     = "matchup" if is_owned else "mlb_7d"
        position  = stat_row.get("position", "")

        # ── FER ──────────────────────────────────────────────────────────────
        ip_val  = float(stat_row.get("ip") or 0)
        ab_val  = float(stat_row.get("ab") or 0)
        pa_val  = float(stat_row.get("pa_est") or 0)

        is_sp = position in ("SP",) or (ip_val > 0 and "SP" in position)
        is_rp = position in ("RP","CL","SU","MR") or (ip_val > 0 and not is_sp and ab_val == 0)
        is_h  = ab_val > 0 or pa_val >= MIN_PA

        fer = None
        if is_h:
            fer = calc_hitter_fer(
                stat_row.get("r"), stat_row.get("hr"),
                stat_row.get("rbi"), stat_row.get("sb"),
                stat_row.get("obp"), pa_val
            )
        elif is_sp:
            fer = calc_sp_fer(
                stat_row.get("qs"), stat_row.get("era"),
                stat_row.get("whip"), stat_row.get("k_per_9"), ip_val
            )
        elif is_rp:
            fer = calc_rp_fer(
                stat_row.get("sv"), stat_row.get("era"),
                stat_row.get("whip"), stat_row.get("k_per_9")
            )

        if fer is not None: fer_count += 1
        label = fer_band_label(fer)

        # ── FWA ──────────────────────────────────────────────────────────────
        fwa = None
        if is_owned and team_uuid:
            yahoo_key = team_uuid_to_key.get(team_uuid)
            opp_key   = matchup_pairs.get(yahoo_key)
            if yahoo_key and opp_key:
                my_ts   = team_stats.get(yahoo_key, {})
                opp_ts  = team_stats.get(opp_key, {})
                roster  = team_roster_stats.get(yahoo_key, [])
                stat_copy = dict(stat_row)
                stat_copy["is_pitcher"] = is_sp or is_rp
                fwa = calculate_fwa(stat_copy, roster, my_ts, opp_ts, position)
                if fwa is not None: fwa_count += 1

        rows.append({
            "player_id":   pid,
            "team_id":     team_uuid,
            "week_number": week_number,
            "season_year": SEASON_YEAR,
            "fwa":         fwa,
            "fer":         fer,
            "fer_grade":   label,
            "stat_basis":  basis,
            "is_locked":   False
        })

    sb_upsert("pipeline.weekly_metric_snapshots", rows)
    print(f"[Metrics] Wrote {len(rows)} rows — FER: {fer_count}, FWA: {fwa_count}")


# ============================================================
# SECTION 14: MONDAY LOCK
# ============================================================

def run_monday_lock(week_number):
    print(f"[Monday] Locking week {week_number} metrics...")

    # Delete any corrupt rows with null player_id before locking
    schema = "pipeline"
    url = f"{SUPABASE_URL}/rest/v1/weekly_metric_snapshots?player_id=is.null"
    r = requests_delete_with_retry(url, headers=sb_headers(schema))
    if r.status_code in (200, 204):
        print(f"[Monday] Cleaned up null player_id rows.")

    unlocked = sb_select(
        "pipeline.weekly_metric_snapshots",
        f"week_number=eq.{week_number}&season_year=eq.{SEASON_YEAR}&is_locked=eq.false&player_id=not.is.null"
    )
    if not unlocked:
        print(f"[Monday] No unlocked rows for week {week_number}.")
        return
    # Only lock rows that have a valid player_id (skip corrupt rows from bad runs)
    lock_rows = [{"id": row["id"], "is_locked": True}
                 for row in unlocked if row.get("player_id")]
    sb_upsert("pipeline.weekly_metric_snapshots", lock_rows)
    print(f"[Monday] Locked {len(lock_rows)} rows for week {week_number}.")


# ============================================================
# SECTION 15: CLEANUP
# ============================================================

def run_cleanup():
    print("[Cleanup] Running retention cleanup...")
    sb_delete_old("pipeline.roster_snapshots",   "snapshot_date", 30)
    sb_delete_old("pipeline.player_stats_daily", "pulled_date",    7)
    print("[Cleanup] Done.")


# ============================================================
# SECTION 16: MAIN ORCHESTRATOR
# ============================================================

def get_current_week(access_token):
    root = yahoo_get(access_token, f"league/{LEAGUE_KEY}")
    week = root.findtext(".//current_week")
    return int(week) if week else None

def main():
    today     = date.today()
    is_monday = today.weekday() == 0

    print("=" * 60)
    print(f"FRANCHISE MODE PIPELINE · {today.isoformat()}")
    print(f"Monday lock: {'YES' if is_monday else 'no'}")
    print("=" * 60)

    try:
        # 1. Refresh token
        access_token = refresh_token()

        # 2. Current week
        week_number = get_current_week(access_token)
        if not week_number:
            raise Exception("[Main] Could not determine current week")
        print(f"[Main] Current week: {week_number}")

        # 3. Teams map
        teams_map, yahoo_key_to_name = get_teams_map(access_token)

        # 4. Scoreboard — matchup pairs + team stats
        # On Monday, metrics are for the week that just ended (week_number - 1)
        force_week = os.environ.get("FORCE_WEEK")
        metric_week = int(force_week) if force_week else (week_number - 1 if is_monday else week_number)
        matchup_pairs, team_stats = pull_scoreboard(access_token, metric_week)

        # 5. Roster snapshots
        rostered_map = pull_roster_snapshots(access_token, today, teams_map)

        # 6. Player stats — all windows
        stat_rows_all = pull_player_stats(access_token, today)

        # Filter to 7d window for metrics
        stat_rows_7d = [r for r in stat_rows_all if r["window_type"] == "7d"]

        # 7. Daily metric write
        write_daily_metrics(
            today, rostered_map, teams_map, metric_week,
            stat_rows_7d, matchup_pairs, team_stats
        )

        # 8. Monday lock
        if is_monday and metric_week >= 1:
            run_monday_lock(metric_week)

        # 9. Cleanup
        run_cleanup()

        print("=" * 60)
        print("PIPELINE COMPLETE")
        print("=" * 60)

    except Exception as e:
        print("=" * 60)
        print(f"PIPELINE FAILED: {e}")
        print("=" * 60)
        raise

if __name__ == "__main__":
    main()
