"""
============================================================
FRANCHISE MODE · FWA VALIDATION HARNESS
============================================================
Purpose: prove the automated FWA can match your Monday hand-calc
BEFORE we let the daily pipeline replace the spreadsheet.

What it does, for one week and one team:
  1. Pulls each rostered player's OFFICIAL weekly line via the
     week-scoped roster endpoint (the same numbers you key in by
     hand), with started/bench flags.
  2. CHECKSUM DIAGNOSTIC — sums the started players' counting stats
     and compares to the official scoreboard team totals. This is
     the test that tells us whether this source reconstructs your
     team totals (i.e. whether it's lineup-aware enough to trust).
  3. Runs the LOCKED calculate_fwa() from the pipeline (imported,
     not re-implemented — zero drift) and prints per-player FWA.
  4. If you supply a hand-calc CSV, diffs against it player-by-player.

Run it the SAME place you run the pipeline (repo folder with
oauth2.json present, or with the Yahoo env vars set).

    python validate_fwa.py --week 5
    python validate_fwa.py --week 5 --handcalc week5_fwa.csv

handcalc CSV format (header required):
    player_name,fwa
    Jazz Chisholm,1.42
    Shohei Ohtani,0.88
============================================================
"""

import os
import csv
import sys
import argparse

# Import the locked pieces straight from the pipeline so this harness
# can NEVER disagree with production over the calc or the auth.
from franchise_pipeline_v8 import (
    refresh_token,
    yahoo_get,
    pull_scoreboard,
    calculate_fwa,
    derive_pa_and_bb_hbp,
    ip_to_decimal,
    STAT_IDS,
    LEAGUE_KEY,
    SEASON_YEAR,
)

# Your team. Confirm this is right (team 12, 2026 league).
MY_TEAM_KEY = f"{LEAGUE_KEY}.t.12"

# selected_position values that mean the player was NOT in the active lineup
BENCH_SLOTS = {"BN", "IL", "IL+", "IL10", "IL60", "NA", "DL"}


# ------------------------------------------------------------
# Week-scoped roster pull — the candidate replacement source.
# Returns one dict per rostered player, shaped exactly the way
# calculate_fwa() expects, plus 'started' / 'name' for diagnostics.
# ------------------------------------------------------------
def pull_weekly_roster_stats(access_token, team_key, week_number):
    endpoint = (
        f"team/{team_key}/roster;week={week_number}"
        f"/players/stats;type=week;week={week_number}"
    )
    root = yahoo_get(access_token, endpoint)

    players = []
    for player_el in root.iter("player"):
        first = player_el.findtext(".//first") or ""
        last  = player_el.findtext(".//last") or ""
        name  = f"{first} {last}".strip()

        # position_type: 'P' pitcher, 'B' batter — authoritative classifier
        ptype = (player_el.findtext(".//position_type") or "").strip().upper()
        is_pitcher = (ptype == "P")

        # lineup slot for THIS week — bench/IL means it didn't count
        sel = player_el.findtext(".//selected_position/position") \
              or player_el.findtext(".//selected_position//position") or ""
        sel = sel.strip().upper()
        started = sel not in BENCH_SLOTS and sel != ""

        # parse the weekly stat line (same stat IDs as the pipeline)
        stats = {}
        for stat_el in player_el.iter("stat"):
            sid = stat_el.findtext("stat_id")
            val = stat_el.findtext("value")
            if sid:
                stats[sid] = val

        def num(key):
            v = stats.get(STAT_IDS[key])
            if v in (None, "", "-", "N/A"):
                return None
            try:
                return float(v)
            except ValueError:
                return None

        # H/AB is a combined "h/ab" string under stat 60
        h = ab = None
        hab = stats.get(STAT_IDS["h_ab"], "")
        if "/" in str(hab):
            try:
                h, ab = [float(x) for x in str(hab).split("/")]
            except ValueError:
                h = ab = None

        obp = num("obp")
        ip_raw = num("ip")
        pa_est, bb_hbp_est = derive_pa_and_bb_hbp(h, ab, obp, name)

        players.append({
            "name":              name,
            "is_pitcher":        is_pitcher,
            "started":           started,
            "selected_position": sel,
            "h":   h, "ab": ab,
            "r":   num("r"), "hr": num("hr"), "rbi": num("rbi"), "sb": num("sb"),
            "obp": obp, "pa_est": pa_est, "bb_hbp_est": bb_hbp_est,
            "ip":  ip_to_decimal(ip_raw) if ip_raw else None,
            "qs":  num("qs"), "sv": num("sv"),
            "era": num("era"), "whip": num("whip"), "k_per_9": num("k_per_9"),
        })
    return players


def fmt(v, width=7, dp=2):
    return f"{v:>{width}.{dp}f}" if isinstance(v, (int, float)) else f"{'-':>{width}}"


def run(week, handcalc_path):
    print("=" * 64)
    print(f"FWA VALIDATION · week {week} · {MY_TEAM_KEY} · {SEASON_YEAR}")
    print("=" * 64)

    token = refresh_token()

    # 1. Official totals + matchup from the scoreboard (authoritative)
    matchup_pairs, team_stats = pull_scoreboard(token, week)
    opp_key = matchup_pairs.get(MY_TEAM_KEY)
    if not opp_key:
        print(f"!! No opponent found for {MY_TEAM_KEY} in week {week} scoreboard.")
        print(f"   Teams in scoreboard: {list(team_stats.keys())}")
        sys.exit(1)
    my_ts  = team_stats.get(MY_TEAM_KEY, {})
    opp_ts = team_stats.get(opp_key, {})
    print(f"Opponent: {opp_key}")

    # 2. Candidate per-player source
    roster = pull_weekly_roster_stats(token, MY_TEAM_KEY, week)
    started = [p for p in roster if p["started"]]
    print(f"Roster pulled: {len(roster)} players, {len(started)} started.\n")

    # 3. CHECKSUM DIAGNOSTIC — do started counting stats == team totals?
    print("-" * 64)
    print("CHECKSUM: started-player sums vs official team totals")
    print("-" * 64)
    hit_cats = [("r", "R"), ("hr", "HR"), ("rbi", "RBI"), ("sb", "SB")]
    pit_cats = [("qs", "QS"), ("sv", "SV")]
    ties = True
    for pkey, tkey in hit_cats + pit_cats:
        summed = sum(float(p.get(pkey) or 0) for p in started)
        official = float(my_ts.get(tkey, 0))
        delta = summed - official
        flag = "OK " if abs(delta) < 0.5 else ">>>"
        if abs(delta) >= 0.5:
            ties = False
        print(f"  {flag} {tkey:<4} summed={summed:>7.1f}  official={official:>7.1f}  delta={delta:>+7.1f}")
    print()
    if ties:
        print("  ==> Counting stats tie out. This source reconstructs your team.")
    else:
        print("  ==> MISMATCH. Likely cause: daily lineups (player active only")
        print("      part of the week) — type=week gives full-week stats, not the")
        print("      started-days subset. If so we sum daily pulls instead. Tell me")
        print("      which categories drifted and we adjust the source, not the calc.")
    print()

    # 4. Run the LOCKED calc per started player
    print("-" * 64)
    print("FWA (locked calculate_fwa, imported from pipeline)")
    print("-" * 64)
    results = []
    for p in started:
        position = "SP" if p["is_pitcher"] else "B"  # SP/RP share the pitcher branch
        fwa = calculate_fwa(p, started, my_ts, opp_ts, position)
        results.append((p["name"], p["selected_position"], fwa))
    results.sort(key=lambda x: x[2], reverse=True)

    total = 0.0
    for name, slot, fwa in results:
        total += fwa
        print(f"  {name:<24} {slot:<4} {fmt(fwa)}")
    print("-" * 64)
    print(f"  {'TEAM TOTAL FWA':<29} {fmt(total)}")
    print("  (should ≈ your actual category wins this week)\n")

    # 5. Optional hand-calc comparison
    if handcalc_path:
        if not os.path.exists(handcalc_path):
            print(f"!! handcalc file not found: {handcalc_path}")
            return
        hand = {}
        with open(handcalc_path, newline="") as f:
            for row in csv.DictReader(f):
                hand[row["player_name"].strip()] = float(row["fwa"])
        print("-" * 64)
        print("HAND-CALC DIFF (auto - hand)")
        print("-" * 64)
        auto = {name: fwa for name, _, fwa in results}
        worst = 0.0
        for name in sorted(set(auto) | set(hand)):
            a = auto.get(name)
            h = hand.get(name)
            if a is None:
                print(f"  {name:<24} only in HAND ({h})")
                continue
            if h is None:
                print(f"  {name:<24} only in AUTO ({a})")
                continue
            d = a - h
            worst = max(worst, abs(d))
            flag = "OK " if abs(d) < 0.01 else ">>>"
            print(f"  {flag} {name:<24} auto={fmt(a)}  hand={fmt(h)}  diff={d:>+7.3f}")
        print("-" * 64)
        print(f"  Worst per-player diff: {worst:.3f}")
        print(f"  Total diff: {sum(auto.values()) - sum(hand.values()):+.3f}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--handcalc", type=str, default=None,
                    help="optional CSV: player_name,fwa")
    args = ap.parse_args()
    run(args.week, args.handcalc)
