-- One row per race; no joins that multiply lap rows.
SELECT s.session_key,s.year,s.circuit_short_name,s.date_start,
 (SELECT count(*) FROM f1.drivers d WHERE d.session_key=s.session_key) AS drivers,
 (SELECT count(*) FROM f1.laps l WHERE l.session_key=s.session_key) AS laps,
 (SELECT count(*) FROM f1.stints t WHERE t.session_key=s.session_key) AS stints,
 (SELECT count(*) FROM f1.weather w WHERE w.session_key=s.session_key) AS weather,
 (SELECT count(*) FROM f1.race_control r WHERE r.session_key=s.session_key) AS race_control,
 (SELECT count(*) FROM f1.laps l WHERE l.session_key=s.session_key AND l.lap_duration IS NULL) AS null_duration,
 (SELECT count(*) FROM f1.laps l WHERE l.session_key=s.session_key AND l.date_start IS NULL) AS null_start,
 (SELECT count(*) FROM f1.laps l WHERE l.session_key=s.session_key AND l.is_pit_out_lap) AS pit_out,
 (SELECT count(*) FROM f1.weather w WHERE w.session_key=s.session_key AND w.rainfall=1) AS rain_samples,
 (SELECT count(*) FROM f1.weather w WHERE w.session_key=s.session_key AND w.rainfall IS NULL) AS rain_unknown
FROM f1.sessions s ORDER BY s.date_start;
