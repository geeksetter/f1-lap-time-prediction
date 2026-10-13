-- One candidate per raw lap. A stint collision is counted, never silently selected.
-- Parameters: circuit, years. All times are UTC timestamptz.
SELECT l.session_key,l.driver_number,l.lap_number,s.year,d.full_name AS driver_name,
       l.date_start AS lap_start_at,l.lap_duration,l.is_pit_out_lap,
       CASE WHEN l.lap_duration > 0 AND l.lap_duration < 'Infinity'::float8
            THEN l.date_start + l.lap_duration * interval '1 second' END AS lap_end_at,
       s.date_start AS session_start_at,s.date_end AS session_end_at,
       t.matches AS stint_matches,t.stint_number,t.lap_start AS stint_lap_start,
       t.compound,t.tyre_age_at_start AS tyre_age_at_stint_start,
       t.tyre_age_at_start+l.lap_number-t.lap_start AS tyre_age_before_lap,
       w.date AS weather_at,extract(epoch FROM l.date_start-w.date)::float8 AS weather_age_seconds,
       w.air_temperature,w.track_temperature,w.rainfall AS rainfall_at_start,
       p.lap_duration AS previous_lap_duration,p.lap_number AS previous_lap_number,
       p.date_start+p.lap_duration * interval '1 second' AS previous_lap_end_at,
       EXISTS(SELECT 1 FROM f1.laps n WHERE n.session_key=l.session_key
              AND n.driver_number=l.driver_number AND n.lap_number=l.lap_number+1
              AND n.is_pit_out_lap IS TRUE) AS inferred_pit_in,
       EXISTS(SELECT 1 FROM f1.stints n WHERE n.session_key=l.session_key
              AND n.driver_number=l.driver_number AND n.stint_number>1
              AND n.lap_start=l.lap_number+1) AS before_stint_change
FROM f1.laps l
JOIN f1.sessions s USING(session_key)
JOIN f1.drivers d USING(session_key,driver_number)
LEFT JOIN LATERAL (
    SELECT count(*)::integer AS matches,
           CASE WHEN count(*)=1 THEN min(stint_number) END AS stint_number,
           CASE WHEN count(*)=1 THEN min(lap_start) END AS lap_start,
           CASE WHEN count(*)=1 THEN min(compound) END AS compound,
           CASE WHEN count(*)=1 THEN min(tyre_age_at_start) END AS tyre_age_at_start
    FROM f1.stints t WHERE t.session_key=l.session_key AND t.driver_number=l.driver_number
      AND l.lap_number BETWEEN t.lap_start AND t.lap_end
) t ON true
LEFT JOIN LATERAL (
    SELECT * FROM f1.weather w WHERE w.session_key=l.session_key AND w.date<=l.date_start
    ORDER BY w.date DESC LIMIT 1
) w ON true
LEFT JOIN LATERAL (
    SELECT p.* FROM f1.laps p WHERE p.session_key=l.session_key AND p.driver_number=l.driver_number
      AND p.lap_number<l.lap_number AND p.date_start IS NOT NULL
      AND p.lap_duration>0 AND p.lap_duration<'Infinity'::float8
      AND p.date_start+p.lap_duration*interval '1 second'<=l.date_start
    ORDER BY p.lap_number DESC LIMIT 1
) p ON true
WHERE s.session_name='Race' AND s.circuit_short_name=%s AND s.year=ANY(%s)
  AND s.is_cancelled IS DISTINCT FROM true
ORDER BY l.session_key,l.driver_number,l.lap_number;
