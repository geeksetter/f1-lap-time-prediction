CREATE SCHEMA IF NOT EXISTS f1;

CREATE TABLE IF NOT EXISTS f1.ingestion_runs (
    run_id uuid PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    year integer NOT NULL,
    country_name text NOT NULL,
    status text NOT NULL CHECK (status IN ('running','completed','failed')),
    error text
);
CREATE TABLE IF NOT EXISTS f1.api_batches (
    batch_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES f1.ingestion_runs(run_id),
    endpoint text NOT NULL,
    source_url text NOT NULL,
    query_params jsonb NOT NULL,
    fetched_at timestamptz NOT NULL,
    row_count integer NOT NULL CHECK (row_count >= 0),
    content_sha256 char(64) NOT NULL,
    attempts integer NOT NULL CHECK (attempts > 0),
    response jsonb NOT NULL CHECK (jsonb_typeof(response) = 'array')
);
CREATE INDEX IF NOT EXISTS batches_run_endpoint ON f1.api_batches(run_id, endpoint);

CREATE TABLE IF NOT EXISTS f1.meetings (
    meeting_key integer PRIMARY KEY,
    meeting_name text, year integer NOT NULL,
    circuit_key integer, circuit_short_name text,
    country_name text, location text,
    date_start timestamptz, date_end timestamptz,
    is_cancelled boolean,
    source_batch_id uuid NOT NULL REFERENCES f1.api_batches(batch_id),
    raw_record jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS f1.sessions (
    session_key integer PRIMARY KEY,
    meeting_key integer NOT NULL REFERENCES f1.meetings(meeting_key),
    session_name text, session_type text,
    year integer NOT NULL, circuit_key integer, circuit_short_name text,
    country_name text, date_start timestamptz, date_end timestamptz,
    is_cancelled boolean,
    source_batch_id uuid NOT NULL REFERENCES f1.api_batches(batch_id),
    raw_record jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS f1.drivers (
    session_key integer NOT NULL REFERENCES f1.sessions(session_key),
    driver_number integer NOT NULL,
    full_name text, name_acronym text, team_name text,
    source_batch_id uuid NOT NULL REFERENCES f1.api_batches(batch_id),
    raw_record jsonb NOT NULL,
    PRIMARY KEY(session_key, driver_number)
);
CREATE TABLE IF NOT EXISTS f1.laps (
    session_key integer NOT NULL,
    driver_number integer NOT NULL,
    lap_number integer NOT NULL CHECK(lap_number > 0),
    date_start timestamptz, lap_duration double precision,
    duration_sector_1 double precision, duration_sector_2 double precision,
    duration_sector_3 double precision, is_pit_out_lap boolean,
    source_batch_id uuid NOT NULL REFERENCES f1.api_batches(batch_id),
    raw_record jsonb NOT NULL,
    PRIMARY KEY(session_key, driver_number, lap_number),
    FOREIGN KEY(session_key, driver_number) REFERENCES f1.drivers(session_key,driver_number)
);
CREATE INDEX IF NOT EXISTS laps_session_time ON f1.laps(session_key,date_start);
CREATE TABLE IF NOT EXISTS f1.stints (
    session_key integer NOT NULL,
    driver_number integer NOT NULL,
    stint_number integer NOT NULL CHECK(stint_number > 0),
    lap_start integer, lap_end integer, compound text, tyre_age_at_start integer,
    source_batch_id uuid NOT NULL REFERENCES f1.api_batches(batch_id),
    raw_record jsonb NOT NULL,
    PRIMARY KEY(session_key,driver_number,stint_number),
    FOREIGN KEY(session_key,driver_number) REFERENCES f1.drivers(session_key,driver_number)
);
CREATE INDEX IF NOT EXISTS stints_lap_range ON f1.stints(session_key,driver_number,lap_start,lap_end);
CREATE TABLE IF NOT EXISTS f1.weather (
    session_key integer NOT NULL REFERENCES f1.sessions(session_key),
    date timestamptz NOT NULL,
    air_temperature double precision, track_temperature double precision,
    humidity double precision, pressure double precision,
    rainfall integer, wind_speed double precision, wind_direction integer,
    source_batch_id uuid NOT NULL REFERENCES f1.api_batches(batch_id),
    raw_record jsonb NOT NULL,
    PRIMARY KEY(session_key,date)
);
CREATE TABLE IF NOT EXISTS f1.race_control (
    session_key integer NOT NULL REFERENCES f1.sessions(session_key),
    event_id char(64) NOT NULL,
    date timestamptz NOT NULL,
    driver_number integer, lap_number integer,
    category text, flag text, scope text, sector integer, message text,
    source_batch_id uuid NOT NULL REFERENCES f1.api_batches(batch_id),
    raw_record jsonb NOT NULL,
    PRIMARY KEY(session_key,event_id),
    FOREIGN KEY(session_key,driver_number) REFERENCES f1.drivers(session_key,driver_number)
);
CREATE INDEX IF NOT EXISTS race_control_session_date ON f1.race_control(session_key,date);
