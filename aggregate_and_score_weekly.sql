-- DRAFT -- NOT YET APPLIED. For review only.
--
-- Aggregates baseball.player_daily_stats (started-slot days only, within one
-- season/week) into baseball.player_weekly_stats_v2, reconstructing ERA/WHIP/K9
-- from the same day's IP rather than averaging daily rates. Then scores FER
-- using the already-live calc_hitter_fer / calc_sp_fer / calc_rp_fer /
-- fer_band_label functions. Never touches locked rows.
--
-- Mid-week pickups/trades are scored normally, based on however many started
-- days they actually have that week for that team_id -- no minimum day count.
-- FER's own PA/IP thresholds (14 PA, 5.0 IP for SP) already handle small
-- samples sensibly; no separate "full week" eligibility filter needed.

CREATE OR REPLACE FUNCTION baseball.aggregate_and_score_weekly(
    p_season_id uuid,
    p_week_number int
) RETURNS SETOF baseball.player_weekly_stats_v2
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH daily AS (
        SELECT
            pds.player_id, pds.team_id, pds.season_id, pds.week_number,
            pds.h, pds.ab, pds.r, pds.hr, pds.rbi, pds.sb, pds.pa_est,
            pds.ip, pds.qs, pds.sv,
            -- back out raw pitching components from that day's rate stats
            CASE WHEN pds.ip > 0 AND pds.era IS NOT NULL THEN pds.era * pds.ip / 9 END AS er_day,
            CASE WHEN pds.ip > 0 AND pds.whip IS NOT NULL THEN pds.whip * pds.ip END AS baserunners_day,
            CASE WHEN pds.ip > 0 AND pds.k_per_9 IS NOT NULL THEN pds.k_per_9 * pds.ip / 9 END AS k_day
        FROM baseball.player_daily_stats pds
        WHERE pds.season_id = p_season_id
          AND pds.week_number = p_week_number
          AND pds.selected_position = ANY (ARRAY['C','1B','2B','3B','SS','OF','UT','SP','RP','P'])
    ),
    agg AS (
        SELECT
            player_id, team_id, season_id, week_number,
            SUM(h) AS h, SUM(ab) AS ab, SUM(r) AS r, SUM(hr) AS hr, SUM(rbi) AS rbi, SUM(sb) AS sb,
            SUM(pa_est) AS pa_est,
            -- weekly OBP reconstructed from real weekly PA/hit totals, not averaged
            CASE WHEN SUM(pa_est) > 0
                 THEN (SUM(h) + (SUM(pa_est) - SUM(ab))) / SUM(pa_est)
            END AS obp,
            SUM(ip) AS ip, SUM(qs) AS qs, SUM(sv) AS sv,
            CASE WHEN SUM(ip) > 0 THEN 9 * SUM(er_day) / SUM(ip) END AS era,
            CASE WHEN SUM(ip) > 0 THEN SUM(baserunners_day) / SUM(ip) END AS whip,
            CASE WHEN SUM(ip) > 0 THEN 9 * SUM(k_day) / SUM(ip) END AS k_per_9
        FROM daily
        GROUP BY player_id, team_id, season_id, week_number
    ),
    scored AS (
        SELECT
            a.*,
            CASE
                WHEN COALESCE(a.pa_est, 0) >= 14
                    THEN baseball.calc_hitter_fer(a.r, a.hr, a.rbi, a.sb, a.obp, a.pa_est)
                WHEN COALESCE(a.ip, 0) >= 5.0 AND COALESCE(a.ab, 0) = 0 AND a.qs IS NOT NULL
                    THEN baseball.calc_sp_fer(a.qs, a.era, a.whip, a.k_per_9, a.ip)
                WHEN COALESCE(a.ip, 0) > 0 AND COALESCE(a.ab, 0) = 0
                    THEN baseball.calc_rp_fer(a.sv, a.era, a.whip, a.k_per_9)
                ELSE NULL
            END AS fer_score
        FROM agg a
    )
    INSERT INTO baseball.player_weekly_stats_v2 (
        player_id, team_id, season_id, week_number,
        h, ab, r, hr, rbi, sb, obp, pa_est, ip, qs, sv, era, whip, k_per_9,
        fer, fer_grade, is_locked
    )
    SELECT
        s.player_id, s.team_id, s.season_id, s.week_number,
        s.h, s.ab, s.r, s.hr, s.rbi, s.sb, s.obp, s.pa_est, s.ip, s.qs, s.sv, s.era, s.whip, s.k_per_9,
        s.fer_score,
        baseball.fer_band_label(s.fer_score),
        false
    FROM scored s
    ON CONFLICT (player_id, season_id, week_number) DO UPDATE SET
        h = EXCLUDED.h, ab = EXCLUDED.ab, r = EXCLUDED.r, hr = EXCLUDED.hr,
        rbi = EXCLUDED.rbi, sb = EXCLUDED.sb, obp = EXCLUDED.obp, pa_est = EXCLUDED.pa_est,
        ip = EXCLUDED.ip, qs = EXCLUDED.qs, sv = EXCLUDED.sv,
        era = EXCLUDED.era, whip = EXCLUDED.whip, k_per_9 = EXCLUDED.k_per_9,
        fer = EXCLUDED.fer, fer_grade = EXCLUDED.fer_grade
    WHERE baseball.player_weekly_stats_v2.is_locked = false
    RETURNING *;
END;
$$;
