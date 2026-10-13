-- Explicit model input contract, one row per driver lap; no retrospective flags.
SELECT session_key,year,driver_number,lap_number,driver_name,lap_start_at,
       compound,tyre_age_before_lap,air_temperature,track_temperature,
       weather_at,weather_age_seconds,previous_lap_duration,
       previous_lap_number,previous_lap_end_at,lap_duration
FROM analytics.training_laps ORDER BY session_key,driver_number,lap_number;
