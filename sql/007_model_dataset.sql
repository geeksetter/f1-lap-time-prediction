-- Only requested session IDs are read. Train never requests the test session.
-- History: latest three already completed, accepted ordinary dry laps, same driver/session.
SELECT t.session_key,t.year,t.driver_number,t.lap_number,t.driver_name,t.lap_start_at,
       t.compound,t.tyre_age_before_lap,t.air_temperature,t.track_temperature,
       t.weather_at,t.weather_age_seconds,t.lap_duration,
       COALESCE(h.history,'[]'::jsonb) AS correct_history,
       l.lap_duration IS NOT DISTINCT FROM t.lap_duration AS target_matches_source
FROM analytics.training_laps t
JOIN f1.laps l USING(session_key,driver_number,lap_number)
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object(
        'session_key',p.session_key,'driver_number',p.driver_number,
        'lap_number',p.lap_number,'lap_duration',p.lap_duration,
        'end_at',p.lap_start_at+p.lap_duration*interval '1 second'
    ) ORDER BY p.lap_number DESC) AS history
    FROM (
        SELECT p.* FROM analytics.training_laps p
        WHERE p.session_key=t.session_key AND p.driver_number=t.driver_number
          AND p.lap_number<t.lap_number
          AND p.lap_start_at+p.lap_duration*interval '1 second'<=t.lap_start_at
        ORDER BY p.lap_number DESC LIMIT 3
    ) p
) h ON true
WHERE t.session_key=ANY(%s)
ORDER BY t.session_key,t.driver_number,t.lap_number;
