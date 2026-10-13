-- Candidate extraction for stage 2. Weather is strictly as-of lap start.
-- Race-control filters and final exclusions belong to stage 2.
SELECT l.session_key,l.driver_number,d.full_name,l.lap_number,
 l.date_start,l.lap_duration,l.is_pit_out_lap,
 t.stint_number,t.compound,
 t.tyre_age_at_start+l.lap_number-t.lap_start AS tyre_age_before_lap,
 w.date AS weather_observed_at,w.air_temperature,w.track_temperature,
 w.rainfall
FROM f1.laps l
JOIN f1.drivers d USING(session_key,driver_number)
LEFT JOIN f1.stints t ON t.session_key=l.session_key AND t.driver_number=l.driver_number
 AND l.lap_number BETWEEN t.lap_start AND t.lap_end
LEFT JOIN LATERAL (
 SELECT * FROM f1.weather w
 WHERE w.session_key=l.session_key AND w.date<=l.date_start
 ORDER BY w.date DESC LIMIT 1
) w ON true
ORDER BY l.session_key,l.driver_number,l.lap_number;
