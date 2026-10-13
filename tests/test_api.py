from unittest.mock import Mock

import pytest
import requests

from f1_pipeline.api import OpenF1Client, digest


def response(code=200, rows=None):
    r = Mock(
        status_code=code, headers={}, url="https://api.openf1.org/v1/laps?session_key=1"
    )
    r.json.return_value = [] if rows is None else rows
    if 400 <= code < 500:
        r.raise_for_status.side_effect = requests.HTTPError(str(code))
    return r


def test_retry_429_and_capture_provenance():
    session = Mock()
    session.get.side_effect = [response(429), response(rows=[{"session_key": 1}])]
    sleep = Mock()
    client = OpenF1Client(session=session, sleep=sleep, min_interval=0, attempts=2)
    b = client.fetch("laps", session_key=1)
    assert b.attempts == 2 and b.params == {"session_key": 1}
    assert b.sha256 == digest(b.rows) and b.fetched_at.utcoffset().total_seconds() == 0
    assert session.get.call_args.kwargs["timeout"] == (10, 60)


def test_timeout_has_bounded_retries():
    session = Mock()
    session.get.side_effect = requests.Timeout()
    with pytest.raises(RuntimeError, match="exhausted"):
        OpenF1Client(session=session, sleep=Mock(), attempts=3).fetch("weather")
    assert session.get.call_count == 3


def test_permanent_404_fails_without_retry():
    session = Mock()
    session.get.return_value = response(404)
    with pytest.raises(requests.HTTPError):
        OpenF1Client(session=session, sleep=Mock()).fetch("sessions")
    assert session.get.call_count == 1


@pytest.mark.parametrize("rows", [{"error": "not an array"}, [1, 2]])
def test_schema_error_is_not_silently_ingested(rows):
    session = Mock()
    session.get.return_value = response(rows=rows)
    with pytest.raises(ValueError, match="schema"):
        OpenF1Client(session=session, sleep=Mock()).fetch("laps")


def test_hash_ignores_dict_order_but_detects_content_change():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})
