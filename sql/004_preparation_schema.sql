-- Derived data live separately; source tables are never updated by preparation.
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE TABLE IF NOT EXISTS analytics.preparation_runs (
    run_id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    policy_version text NOT NULL,
    input_sha256 char(64) NOT NULL,
    output_sha256 char(64) NOT NULL,
    code_sha256 char(64) NOT NULL,
    configuration jsonb NOT NULL,
    source_batches jsonb NOT NULL,
    raw_count integer NOT NULL,
    accepted_count integer NOT NULL,
    validation jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS analytics.lap_observations (
    session_key integer NOT NULL,
    driver_number integer NOT NULL,
    lap_number integer NOT NULL,
    preparation_run_id uuid NOT NULL REFERENCES analytics.preparation_runs(run_id),
    year integer NOT NULL,
    driver_name text,
    lap_start_at timestamptz,
    lap_end_at timestamptz,
    lap_duration double precision,
    stint_number integer,
    stint_lap_start integer,
    tyre_age_at_stint_start integer,
    compound text,
    tyre_age_before_lap integer,
    weather_at timestamptz,
    weather_age_seconds double precision,
    air_temperature double precision,
    track_temperature double precision,
    rainfall_at_start integer,
    previous_lap_duration double precision,
    previous_lap_number integer,
    previous_lap_end_at timestamptz,
    exclusion_reasons text[] NOT NULL,
    warning_codes text[] NOT NULL,
    race_control_event_ids text[] NOT NULL,
    PRIMARY KEY(session_key,driver_number,lap_number),
    FOREIGN KEY(session_key,driver_number,lap_number)
        REFERENCES f1.laps(session_key,driver_number,lap_number),
    FOREIGN KEY(session_key,driver_number,stint_number)
        REFERENCES f1.stints(session_key,driver_number,stint_number),
    FOREIGN KEY(session_key,weather_at) REFERENCES f1.weather(session_key,date),
    CHECK(weather_at IS NULL OR weather_at <= lap_start_at),
    CHECK(previous_lap_end_at IS NULL OR previous_lap_end_at <= lap_start_at),
    CHECK(previous_lap_number IS NULL OR previous_lap_number < lap_number)
);
CREATE INDEX IF NOT EXISTS observations_reasons
    ON analytics.lap_observations USING gin(exclusion_reasons);
-- Explicit whitelist: retrospective reasons, lap end and same-lap sectors are absent.
CREATE TABLE IF NOT EXISTS analytics.training_laps (
    session_key integer NOT NULL,
    driver_number integer NOT NULL,
    lap_number integer NOT NULL,
    preparation_run_id uuid NOT NULL REFERENCES analytics.preparation_runs(run_id),
    year integer NOT NULL,
    driver_name text NOT NULL,
    lap_start_at timestamptz NOT NULL,
    compound text NOT NULL CHECK(compound IN ('SOFT','MEDIUM','HARD')),
    tyre_age_before_lap integer NOT NULL CHECK(tyre_age_before_lap >= 0),
    air_temperature double precision NOT NULL,
    track_temperature double precision NOT NULL,
    weather_at timestamptz NOT NULL,
    weather_age_seconds double precision NOT NULL CHECK(weather_age_seconds BETWEEN 0 AND 120),
    previous_lap_duration double precision,
    previous_lap_number integer,
    previous_lap_end_at timestamptz,
    lap_duration double precision NOT NULL CHECK(lap_duration > 0 AND lap_duration < 'Infinity'::float8),
    PRIMARY KEY(session_key,driver_number,lap_number),
    FOREIGN KEY(session_key,driver_number,lap_number)
        REFERENCES analytics.lap_observations(session_key,driver_number,lap_number),
    CHECK(weather_at <= lap_start_at),
    CHECK(previous_lap_end_at IS NULL OR previous_lap_end_at <= lap_start_at)
);
CREATE INDEX IF NOT EXISTS training_year_lap ON analytics.training_laps(year,lap_number);
CREATE INDEX IF NOT EXISTS training_compound ON analytics.training_laps(compound);
