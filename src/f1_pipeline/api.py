import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

LOG = logging.getLogger(__name__)
ENDPOINTS = {"meetings", "sessions", "drivers", "laps", "stints", "weather", "race_control"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass
class Batch:
    endpoint: str
    params: dict
    url: str
    fetched_at: datetime
    rows: list[dict]
    sha256: str
    attempts: int


class OpenF1Client:
    """Bounded retries, explicit timeouts, rate pacing and validated JSON."""

    def __init__(self, base_url="https://api.openf1.org/v1", timeout=60,
                 attempts=4, min_interval=.6, session=None, sleep=time.sleep):
        if timeout <= 0 or attempts < 1 or min_interval < 0:
            raise ValueError("Invalid timeout, attempts or min_interval")
        self.base_url = base_url.rstrip("/")
        self.timeout, self.attempts, self.min_interval = timeout, attempts, min_interval
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "f1-lap-time-prediction/0.1 educational"})
        self.sleep = sleep

    def close(self):
        self.session.close()

    def fetch(self, endpoint, **params):
        if endpoint not in ENDPOINTS:
            raise ValueError(f"Unsupported endpoint: {endpoint}")
        for attempt in range(1, self.attempts + 1):
            self.sleep(self.min_interval)
            retry_after = None
            try:
                response = self.session.get(f"{self.base_url}/{endpoint}", params=params,
                                            timeout=(10, self.timeout))
                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = response.headers.get("Retry-After")
                    raise requests.ConnectionError(f"Transient HTTP {response.status_code}")
                response.raise_for_status()  # Other 4xx fail immediately.
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == self.attempts:
                    raise RuntimeError(f"OpenF1 {endpoint}: {self.attempts} attempts exhausted") from exc
                delay = min(2 ** (attempt - 1), 30)
                if retry_after and retry_after.isdigit():
                    delay = min(int(retry_after), 60)
                LOG.warning("%s attempt %s/%s; retry in %ss", endpoint, attempt, self.attempts, delay)
                self.sleep(delay)
                continue
            rows = response.json()
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ValueError(f"Unexpected JSON schema from {endpoint}")
            LOG.info("Fetched %s: %s rows", endpoint, len(rows))
            return Batch(endpoint, params, response.url, datetime.now(timezone.utc),
                         rows, digest(rows), attempt)
