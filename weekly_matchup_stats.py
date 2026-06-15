"""
============================================================
FRANCHISE MODE · WEEKLY MATCHUP STATS (started-only source)
============================================================
Replaces the rolling-7d window as the input to FWA.

Why this exists:
  The Franchise XII is daily-managed but decided weekly. A player
  only contributes to the official weekly team total on the days he
  was in an ACTIVE lineup slot. Yahoo's week-scoped player stats
  (type=week) return each player's FULL week regardless of bench
  days, so they do NOT reconstruct the team totals — which is why
  automated FWA never tied out to the hand-calc / 660 conservation.

  This module rebuilds each player's started-only weekly line by
  summing daily stats over active days, and rebuilds the rate stats
  (OBP, ERA, wHIP, K/9) from components so they match official totals.

Returns a list of dicts shaped exactly for calculate_fwa().

Depends on helpers imported from the pipeline so there is zero drift.
============================================================
"""

from datetime import date, datetime, timedelta
import time

from franchise_pipeline_v8 import (
    yahoo_get,
    derive_pa_and_bb_hbp,
    ip_to_decimal,
    STAT_IDS,
    LEAGUE_KEY,
)

# Lineup slots that mean the player was NOT counting that day
BENCH_SLOTS = {"BN", "IL", "IL+", "IL10", "IL15", "IL60", "NA", "DL", "DTD"}


# ------------------------------------------------------------
# Fantasy-week date range
# ------------------------------------------------------------
def get_week_date_range(access_token, week_number):
    """
    Return (start_iso, end_iso) for a fantasy week.
    Primary source: the scoreboard matchup nodes carry week_start/week_end.
    """
    root = yahoo_get(access_token, f"league/{LEAGUE_KEY}/scoreboard;week={week_number}")
    start = root.findtext(".//week_start")
    end   = root.findtext(".//week_end")
    if not (start and end):
        raise RuntimeError(
            f"Could not read week_start/week_end for week {week_number} "
            f"from scoreboard. Got start={start!r} end={end!r}."
        )
    return start, end


def _daterange(start_iso, end_iso, cap_today=True):
    d0 = datetime.strptime(start_iso, "%Y-%m-%d").date()
    d1 = datetime.strptime(end_iso, "%Y-%m-%d").date()
    if cap_today:
        d1 = min(d1, date.today())   # no stats exist for future days
    cur = d0
    while cur <= d1:
        yield cur.isoformat()
        cur += timedelta(days=1)


def _num(stats, key):
    v = stats.get(STAT_IDS[key])
    if v in (None, "", "-", "N/A"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _parse_h_ab(stats):
    raw = stats.get(STAT_IDS["h_ab"], "")
    if "/" in str(raw):
        try:
            h, ab = [float(x) for x in str(raw).split("/")]
            return h, ab
        except ValueError:
            return None, None
    return None, None


# ------------------------------------------------------------
# Main: started-only weekly line per player for ONE team
# ------------------------------------------------------------
def pull_weekly_matchup_stats(access_token, team_key, week_number,
                              week_start=None, week_end=None, sleep=0.3):
    """
    Sum each player's stats over the days they were in an active slot,
    rebuilding rate stats from components. Returns dicts for calculate_fwa().
    """
    if week_start is None or week_end is None:
        week_start, week_end = get_week_date_range(access_token, week_number)

    # accumulator keyed by yahoo player_id
    acc = {}

    for day in _daterange(week_start, week_end):
        endpoint = (
            f"team/{team_key}/roster;date={day}"
            f"/players/stats;type=date;date={day}"
        )
        root = yahoo_get(access_token, endpoint)

        for pel in root.iter("player"):
            pid   = pel.findtext(".//player_id")
            if not pid:
                continue
            sel = (pel.findtext(".//selected_position/position")
                   or pel.findtext(".//selected_position//position") or "").strip().upper()
            if sel == "" or sel in BENCH_SLOTS:
                continue  # benched / IL that day — did not count

            first = pel.findtext(".//first") or ""
            last  = pel.findtext(".//last") or ""
            name  = f"{first} {last}".strip()
            ptype = (pel.findtext(".//position_type") or "").strip().upper()
            is_pitcher = (ptype == "P")

            stats = {}
            for sel_el in pel.iter("stat"):
                sid = sel_el.findtext("stat_id")
                val = sel_el.findtext("value")
                if sid:
                    stats[sid] = val

            a = acc.setdefault(pid, {
                "name": name, "is_pitcher": is_pitcher, "active_days": 0,
                # hitter accumulators
                "r": 0.0, "hr": 0.0, "rbi": 0.0, "sb": 0.0,
                "h": 0.0, "ab": 0.0, "onbase": 0.0, "pa": 0.0,
                # pitcher accumulators (components)
                "ip": 0.0, "qs": 0.0, "sv": 0.0,
                "er": 0.0, "br": 0.0, "k": 0.0,
            })
            a["active_days"] += 1

            if not is_pitcher:
                for c in ("r", "hr", "rbi", "sb"):
                    a[c] += _num(stats, c) or 0.0
                h, ab = _parse_h_ab(stats)
                obp   = _num(stats, "obp")
                if h is not None and ab is not None:
                    a["h"]  += h
                    a["ab"] += ab
                    pa_d, bb_hbp_d = derive_pa_and_bb_hbp(h, ab, obp, name)
                    if pa_d:
                        a["pa"]     += pa_d
                        a["onbase"] += (h + (bb_hbp_d or 0.0))
            else:
                a["qs"] += _num(stats, "qs") or 0.0
                a["sv"] += _num(stats, "sv") or 0.0
                ip_raw = _num(stats, "ip")
                ip_d   = ip_to_decimal(ip_raw) if ip_raw else 0.0
                if ip_d:
                    a["ip"] += ip_d
                    era = _num(stats, "era")
                    whp = _num(stats, "whip")
                    k9  = _num(stats, "k_per_9")
                    # rebuild components from rate * volume
                    if era is not None: a["er"] += era * ip_d / 9.0
                    if whp is not None: a["br"] += whp * ip_d
                    if k9  is not None: a["k"]  += k9  * ip_d / 9.0

        if sleep:
            time.sleep(sleep)

    # finalize: collapse accumulators into calculate_fwa() shape
    players = []
    for pid, a in acc.items():
        if a["is_pitcher"]:
            ip = a["ip"]
            players.append({
                "name": a["name"], "yahoo_pid": pid, "is_pitcher": True,
                "ip": round(ip, 2),
                "qs": a["qs"], "sv": a["sv"],
                "era":     round(9.0 * a["er"] / ip, 3) if ip > 0 else None,
                "whip":    round(a["br"] / ip, 3)       if ip > 0 else None,
                "k_per_9": round(9.0 * a["k"]  / ip, 3) if ip > 0 else None,
                # hitter fields present-but-empty so calc never KeyErrors
                "r": 0.0, "hr": 0.0, "rbi": 0.0, "sb": 0.0,
                "obp": None, "pa_est": 0.0, "ab": 0.0,
                "active_days": a["active_days"],
            })
        else:
            pa = a["pa"]
            players.append({
                "name": a["name"], "yahoo_pid": pid, "is_pitcher": False,
                "r": a["r"], "hr": a["hr"], "rbi": a["rbi"], "sb": a["sb"],
                "h": a["h"], "ab": a["ab"],
                "obp":    round(a["onbase"] / pa, 4) if pa > 0 else None,
                "pa_est": round(pa, 1),
                # pitcher fields present-but-empty
                "ip": 0.0, "qs": 0.0, "sv": 0.0,
                "era": None, "whip": None, "k_per_9": None,
                "active_days": a["active_days"],
            })
    return players


# ------------------------------------------------------------
# Self-check: do started-only sums reconstruct official totals?
# Counting stats must tie exactly; this is the 660-conservation gate.
# ------------------------------------------------------------
def checksum(players, official_team_stats):
    """Return (ok, rows) comparing summed counting stats to official totals."""
    checks = [("r", "R"), ("hr", "HR"), ("rbi", "RBI"), ("sb", "SB"),
              ("qs", "QS"), ("sv", "SV")]
    rows = []
    ok = True
    for pkey, tkey in checks:
        summed   = sum(float(p.get(pkey) or 0) for p in players)
        official = float(official_team_stats.get(tkey, 0))
        delta    = summed - official
        if abs(delta) >= 0.5:
            ok = False
        rows.append((tkey, official, summed, delta))
    return ok, rows


# ------------------------------------------------------------
# Whole-league FWA from the started-only source.
# Returns {player_uuid: fwa} for every owned player, ready to
# drop straight into weekly_metric_snapshots.
# Also returns per-team checksum rows for the conservation log.
# ------------------------------------------------------------
def compute_all_fwa(access_token, teams_map, matchup_pairs, team_stats,
                    week_number, week_start=None, week_end=None):
    from franchise_pipeline_v8 import calculate_fwa, get_player_uuid, yahoo_get as _yg  # noqa

    if week_start is None or week_end is None:
        week_start, week_end = get_week_date_range(access_token, week_number)

    fwa_by_uuid = {}
    checksum_log = {}   # yahoo_team_key -> (ok, rows)

    for yahoo_team_key in teams_map.keys():
        opp_key = matchup_pairs.get(yahoo_team_key)
        if not opp_key:
            continue
        my_ts  = team_stats.get(yahoo_team_key, {})
        opp_ts = team_stats.get(opp_key, {})

        players = pull_weekly_matchup_stats(
            access_token, yahoo_team_key, week_number, week_start, week_end
        )
        checksum_log[yahoo_team_key] = checksum(players, my_ts)

        for p in players:
            position = "P" if p["is_pitcher"] else "B"
            fwa = calculate_fwa(p, players, my_ts, opp_ts, position)
            # map yahoo player back to the DB uuid
            # pull_weekly_matchup_stats keeps name; re-resolve uuid by player_id
            # (we re-tag pid below in the loop that builds players)
            uuid = p.get("player_uuid")
            if uuid is None and p.get("yahoo_pid"):
                uuid = get_player_uuid(p["yahoo_pid"])
            if uuid:
                fwa_by_uuid[uuid] = fwa

    return fwa_by_uuid, checksum_log, (week_start, week_end)
