"""
populate_team_weekly_stats.py

Pulls weekly scoreboard data from Yahoo Fantasy Sports API and upserts into:
  - baseball.matchups          (who played whom, W/L category counts)
  - baseball.team_weekly_stats (the 10 raw category totals + h/ab/pa/ip per team per week)

Designed to run as a GitHub Action (see populate_team_weekly_stats.yml). No manual
team-ID or stat-ID editing required -- both are resolved automatically at runtime by
matching Yahoo's team names against baseball.teams, and Yahoo's stat display_names
against a known label map.

Supports multiple seasons -- each year has its own Yahoo league_key (Yahoo issues a new
one every season even though it's the same league). Team names/rosters reset each year
too, so team matching is always scoped to (season_id, Yahoo league_key for that year).

Usage:
    python3 populate_team_weekly_stats.py --season 2026 --weeks 1-12
    python3 populate_team_weekly_stats.py --season 2026 --weeks current   # weekly cron
    python3 populate_team_weekly_stats.py --season 2024 --weeks 1-23     # historical backfill

Required secrets/env vars (same Yahoo ones your existing pipeline already has):
    YAHOO_CONSUMER_KEY
    YAHOO_CONSUMER_SECRET
    YAHOO_REFRESH_TOKEN
    SUPABASE_URL
    SUPABASE_SERVICE_KEY   (or SUPABASE_KEY -- publishable key also works today since
                             RLS is currently disabled on these tables)
"""

import os
import sys
import time
import argparse
import requests
from datetime import datetime, date

# Yahoo issues a new league_key every season for the same league. From userMemories /
# prior confirmation in Supabase baseball.seasons.yahoo_league_id:
SEASON_LEAGUE_KEYS = {
    2022: "412.l.71654",
    2023: "422.l.47778",
    2024: "431.l.78645",
    2025: "458.l.72231",
    2026: "469.l.76761",
}

YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://seqvzektwxxypdcqgtve.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")

# Maps Yahoo's stat display_name (lowercased) -> our column name.
# This is matched against whatever print_stat_settings() returns at runtime, so if Yahoo's
# wording differs slightly from what's listed here, add the alias rather than guessing stat_ids.
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
}


# ---------------------------------------------------------------------------
# Yahoo auth + fetch
# ---------------------------------------------------------------------------

def get_yahoo_session():
    consumer_key = os.environ["YAHOO_CONSUMER_KEY"]
    consumer_secret = os.environ["YAHOO_CONSUMER_SECRET"]
    refresh_token = os.environ["YAHOO_REFRESH_TOKEN"]

    resp = requests.post("https://api.login.yahoo.com/oauth2/get_token", data={
        "client_id": consumer_key,
        "client_secret": consumer_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "redirect_uri": "oob",
    })
    resp.raise_for_status()
    access_token = resp.json()["access_token"]

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
    return session


def fetch_json(session, path):
    url = f"{YAHOO_API_BASE}/{path}?format=json"
    resp = session.get(url)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def supabase_get(table, params, schema="baseball"):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept-Profile": schema,
    }
    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def supabase_upsert(table, rows, on_conflict, schema="baseball"):
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
        "Content-Profile": schema,
    }
    resp = requests.post(url, headers=headers, params={"on_conflict": on_conflict}, json=rows)
    if resp.status_code >= 300:
        print(f"  ! Upsert to {schema}.{table} FAILED: {resp.status_code} {resp.text[:500]}")
        return False
    print(f"  \u2713 Upserted {len(rows)} row(s) into {schema}.{table}")
    return True


# ---------------------------------------------------------------------------
# Auto-resolve: season_id, team_key -> team_id, stat_id -> column name
# ---------------------------------------------------------------------------

def resolve_season_id(season_year):
    rows = supabase_get("seasons", {"year": f"eq.{season_year}", "select": "id,year"})
    if not rows:
        raise RuntimeError(f"No baseball.seasons row found for year={season_year}. Create it first.")
    return rows[0]["id"]


def resolve_team_map(session, season_id, league_key):
    """Fetch Yahoo's team list + Supabase's team list for this season.
    Priority: match on existing yahoo_team_id first (stable across renames),
    fall back to matching on team_name for any team that doesn't have one yet.
    Any successful name-match gets its yahoo_team_id written back to Supabase,
    so future runs (and future renames) don't depend on name matching again."""
    yahoo_data = fetch_json(session, f"league/{league_key}/teams")
    teams_block = yahoo_data["fantasy_content"]["league"][1]["teams"]
    count = int(teams_block["count"])

    yahoo_teams = []
    for i in range(count):
        t = teams_block[str(i)]["team"][0]
        team_key = next((x["team_key"] for x in t if isinstance(x, dict) and "team_key" in x), None)
        name = next((x["name"] for x in t if isinstance(x, dict) and "name" in x), None)
        yahoo_teams.append((team_key, name))

    sb_teams = supabase_get("teams", {"season_id": f"eq.{season_id}", "select": "id,team_name,yahoo_team_id"})
    sb_by_yahoo_id = {t["yahoo_team_id"]: t["id"] for t in sb_teams if t.get("yahoo_team_id")}
    sb_by_name = {t["team_name"].strip().lower(): t["id"] for t in sb_teams if not t.get("yahoo_team_id")}

    team_map = {}
    newly_matched = []  # (team_id, yahoo_team_id) pairs to persist back to Supabase
    unmatched = []

    for team_key, name in yahoo_teams:
        if team_key in sb_by_yahoo_id:
            team_map[team_key] = sb_by_yahoo_id[team_key]
            continue
        match = sb_by_name.get((name or "").strip().lower())
        if match:
            team_map[team_key] = match
            newly_matched.append((match, team_key))
        else:
            unmatched.append((team_key, name))

    if newly_matched:
        print(f"  Persisting yahoo_team_id for {len(newly_matched)} newly-matched team(s)...")
        for team_id, team_key in newly_matched:
            url = f"{SUPABASE_URL}/rest/v1/teams"
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Content-Profile": "baseball",
            }
            resp = requests.patch(url, headers=headers, params={"id": f"eq.{team_id}"},
                                   json={"yahoo_team_id": team_key})
            if resp.status_code >= 300:
                print(f"    ! Could not persist yahoo_team_id={team_key} for team_id={team_id}: {resp.text[:200]}")

    if unmatched:
        print("  ! WARNING: could not match these Yahoo teams to baseball.teams by yahoo_team_id OR name:")
        for team_key, name in unmatched:
            print(f"      {team_key}  \"{name}\"  -- this team needs a manual fix. Do NOT guess by")
            print(f"        elimination/position -- confirm the real manager/owner, then either rename")
            print(f"        the team_name in Supabase to match Yahoo, or set yahoo_team_id directly:")
            print(f"        update baseball.teams set yahoo_team_id = '{team_key}' where id = '<correct-uuid>';")

    print(f"  Resolved {len(team_map)}/{len(yahoo_teams)} teams.")
    return team_map


def resolve_league_settings(session, league_key):
    """Fetch this league's real stat_id -> display_name (-> STAT_NAME_ALIASES), AND playoff_start_week,
    so matchups can be tagged 'regular' vs 'playoff' correctly without guessing or hardcoding per year."""
    data = fetch_json(session, f"league/{league_key}/settings")
    settings = data["fantasy_content"]["league"][1]["settings"][0]
    stat_categories = settings["stat_categories"]["stats"]

    stat_map = {}
    unmatched = []
    for s in stat_categories:
        stat = s["stat"]
        sid = str(stat.get("stat_id"))
        label = (stat.get("display_name") or stat.get("name") or "").strip().lower()
        col = STAT_NAME_ALIASES.get(label)
        if col:
            stat_map[sid] = col
        else:
            unmatched.append((sid, stat.get("display_name")))

    print(f"  Resolved {len(stat_map)} stat categories automatically.")
    if unmatched:
        print("  (Unmatched Yahoo stats -- expected, these are categories we don't track, e.g. W, K, AVG):")
        for sid, label in unmatched:
            print(f"      stat_id={sid}  \"{label}\"")

    playoff_start_week = settings.get("playoff_start_week")
    if playoff_start_week is not None:
        playoff_start_week = int(playoff_start_week)
        print(f"  League playoffs start at week {playoff_start_week} (from Yahoo settings).")
    else:
        print("  ! Could not find playoff_start_week in Yahoo settings -- all weeks will be tagged 'regular'.")
        print("    Check baseball.matchups.week_type manually for this season if playoff bucketing matters.")

    return stat_map, playoff_start_week


# ---------------------------------------------------------------------------
# Scoreboard parsing
# ---------------------------------------------------------------------------

def parse_matchups(scoreboard_json, week, stat_map):
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
                team_a_wins = m["0"].get("stat_winners_count_team1")
                team_b_wins = m["0"].get("stat_winners_count_team2")
                matchups.append({
                    "week": week,
                    "team_a": teams_in_matchup[0],
                    "team_b": teams_in_matchup[1],
                    "team_a_wins": team_a_wins,
                    "team_b_wins": team_b_wins,
                })
    except (KeyError, IndexError, TypeError) as e:
        print(f"  ! Could not parse week {week} scoreboard: {e}")
        print("    Yahoo's JSON structure may not match what this script expects.")
        print("    Re-run with --debug-week to dump the raw response for inspection.")
    return matchups


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(season_year, weeks):
    league_key = SEASON_LEAGUE_KEYS.get(season_year)
    if not league_key:
        print(f"FATAL: no Yahoo league_key known for season_year={season_year}.")
        print(f"       Known seasons: {sorted(SEASON_LEAGUE_KEYS.keys())}")
        print(f"       If this is a new season, add it to SEASON_LEAGUE_KEYS at the top of the script.")
        sys.exit(1)

    print(f"=== Season {season_year} (league_key={league_key}) ===")
    print("Resolving season_id, team map, and stat/playoff settings from Yahoo + Supabase...")
    session = get_yahoo_session()
    season_id = resolve_season_id(season_year)
    team_map = resolve_team_map(session, season_id, league_key)
    stat_map, playoff_start_week = resolve_league_settings(session, league_key)

    if not team_map:
        print("FATAL: could not resolve any teams. Check that baseball.teams has rows for this season")
        print("       and that team names match Yahoo's names closely.")
        sys.exit(1)

    any_failure = False

    for week in weeks:
        print(f"\nWeek {week}...")
        sb_data = fetch_json(session, f"league/{league_key}/scoreboard;week={week}")
        matchups = parse_matchups(sb_data, week, stat_map)

        week_type = "playoff" if (playoff_start_week is not None and week >= playoff_start_week) else "regular"

        matchup_rows = []
        stat_rows = []

        for m in matchups:
            team_a_id = team_map.get(m["team_a"]["team_key"])
            team_b_id = team_map.get(m["team_b"]["team_key"])
            if not team_a_id or not team_b_id:
                print(f"  ! Skipping unmapped matchup in week {week}: "
                      f"{m['team_a']['team_key']} vs {m['team_b']['team_key']}")
                continue

            matchup_rows.append({
                "season_id": season_id,
                "week_number": week,
                "week_type": week_type,
                "home_team_id": team_a_id,
                "away_team_id": team_b_id,
                "home_wins": m["team_a_wins"],
                "away_wins": m["team_b_wins"],
                "ties": 0,
            })

            for team_id, side in [(team_a_id, "team_a"), (team_b_id, "team_b")]:
                s = m[side]["stats"]
                stat_rows.append({
                    "season_id": season_id,
                    "team_id": team_id,
                    "week_number": week,
                    "r": s.get("r"), "hr": s.get("hr"), "rbi": s.get("rbi"), "sb": s.get("sb"),
                    "obp": s.get("obp"), "sv": s.get("sv"), "qs": s.get("qs"),
                    "era": s.get("era"), "whip": s.get("whip"), "k9": s.get("k9"),
                    "h": None, "ab": None, "pa": None, "ip": None,  # not in scoreboard payload
                    "updated_at": datetime.utcnow().isoformat(),
                })

        if not matchup_rows:
            print(f"  (no matchups parsed for week {week} -- skipping upsert)")
            continue

        ok1 = supabase_upsert("matchups", matchup_rows, on_conflict="season_id,week_number,home_team_id,away_team_id")
        ok2 = supabase_upsert("team_weekly_stats", stat_rows, on_conflict="season_id,team_id,week_number")
        if not (ok1 and ok2):
            any_failure = True

        time.sleep(1)

    if any_failure:
        print(f"\nSeason {season_year} completed with at least one upsert failure -- check logs above.")
        return False
    print(f"\nSeason {season_year} done.")
    return True


def debug_week(season_year, week):
    league_key = SEASON_LEAGUE_KEYS.get(season_year)
    if not league_key:
        print(f"FATAL: no Yahoo league_key known for season_year={season_year}.")
        sys.exit(1)
    session = get_yahoo_session()
    data = fetch_json(session, f"league/{league_key}/scoreboard;week={week}")
    import json

    print(f"=== Searching for win/loss/score related keys in {season_year} week {week} scoreboard ===\n")

    def walk(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if any(term in kl for term in ["win", "loss", "tie", "status", "stat_winner", "is_winner"]):
                    print(f"  {path}.{k} = {json.dumps(v)[:200]}")
                walk(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")

    walk(data)

    print(f"\n=== First 6000 chars of full raw JSON (for context if the above wasn't enough) ===\n")
    print(json.dumps(data, indent=2)[:6000])


def parse_week_arg(weeks_arg, season_year):
    if weeks_arg == "current":
        league_key = SEASON_LEAGUE_KEYS.get(season_year)
        if not league_key:
            raise RuntimeError(f"No league_key for season_year={season_year}.")
        session = get_yahoo_session()
        data = fetch_json(session, f"league/{league_key}")
        league_info = data["fantasy_content"]["league"][0]
        current_week = next((x["current_week"] for x in league_info if isinstance(x, dict) and "current_week" in x), None)
        if current_week is None:
            raise RuntimeError("Could not determine current_week from Yahoo league resource.")
        return [int(current_week)]
    if "-" in weeks_arg:
        start, end = weeks_arg.split("-")
        return list(range(int(start), int(end) + 1))
    return [int(weeks_arg)]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, default=2026, help="Season year, e.g. 2024. Defaults to 2026.")
    parser.add_argument("--seasons", default=None,
                         help="Comma or range list for multi-season backfill, e.g. '2022-2025' or '2022,2023,2024,2025'. "
                              "Overrides --season if given.")
    parser.add_argument("--weeks", default="current", help="e.g. '1-12', '5', or 'current'. 'current' only valid for a single season.")
    parser.add_argument("--debug-week", type=int, help="dump raw Yahoo JSON for one week and exit (uses --season)")
    args = parser.parse_args()

    if args.debug_week:
        debug_week(args.season, args.debug_week)
        sys.exit(0)

    def expand_seasons(s):
        if "-" in s and "," not in s:
            a, b = s.split("-")
            return list(range(int(a), int(b) + 1))
        return [int(x.strip()) for x in s.split(",")]

    seasons = expand_seasons(args.seasons) if args.seasons else [args.season]

    if args.weeks == "current" and len(seasons) > 1:
        print("FATAL: --weeks current only makes sense for a single season. "
              "For historical backfill, specify an explicit week range, e.g. --weeks 1-23.")
        sys.exit(1)

    overall_ok = True
    for year in seasons:
        weeks = parse_week_arg(args.weeks, year)
        ok = run(year, weeks)
        overall_ok = overall_ok and ok
        if len(seasons) > 1:
            time.sleep(2)  # be polite to Yahoo between seasons

    sys.exit(0 if overall_ok else 1)
