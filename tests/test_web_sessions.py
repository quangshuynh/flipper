import base64
import json

import pytest

from web import sessions
from web.sessions import (
    ABSOLUTE_LIFETIME_SECONDS,
    IDLE_LIFETIME_SECONDS,
    REFRESH_INTERVAL_SECONDS,
    Session,
    SessionCodec,
)

SECRET = "Zt4vQ9pLr2Xw8Kd3Nf6Hj1Ms5Bc7Ga0Ey-Ui_Oo4Pp2Rr6Tt8"
HASH = "flipper-scrypt:v1:n=16384,r=8,p=1:c2FsdHNhbHRzYWx0c2FsdA:" + "k" * 43
START = 1_800_000_000


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def codec(clock):
    return SessionCodec(SECRET, HASH, clock=clock)


def _payload(value):
    body = value.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _sign(codec, payload):
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    signed = f"v1.{body}"
    return f"{signed}.{sessions._b64encode(codec._signature(signed))}"


def test_valid_session_round_trips(codec):
    session = codec.issue()
    assert codec.decode(codec.encode(session)) == session
    assert session.issued_at == session.last_seen == START


def test_cookie_payload_is_versioned_and_minimal(codec):
    value = codec.encode(codec.issue())
    assert value.startswith("v1.")
    payload = _payload(value)
    assert set(payload) == {"sid", "iat", "seen"}
    assert SECRET not in value and HASH not in value
    assert "password" not in json.dumps(payload)


def test_session_ids_are_random(codec):
    assert codec.issue().session_id != codec.issue().session_id


@pytest.mark.parametrize("part", [1, 2])
def test_tampering_any_part_is_rejected(codec, part):
    parts = codec.encode(codec.issue()).split(".")
    parts[part] = parts[part][:-2] + ("AA" if parts[part][-2:] != "AA" else "BB")
    assert codec.decode(".".join(parts)) is None


def test_modified_but_resigned_elsewhere_payload_is_rejected(codec):
    forged = SessionCodec("x" * 64, HASH, clock=codec._clock)
    assert codec.decode(forged.encode(Session("a" * 22, START, START))) is None


def test_wrong_secret_and_secret_rotation_invalidate_existing_cookies(codec, clock):
    value = codec.encode(codec.issue())
    rotated = SessionCodec(SECRET[::-1], HASH, clock=clock)
    assert rotated.decode(value) is None


def test_changing_the_password_hash_invalidates_existing_cookies(codec, clock):
    value = codec.encode(codec.issue())
    assert SessionCodec(SECRET, HASH.replace("k" * 43, "j" * 43), clock=clock).decode(value) is None


def test_absolute_expiry_holds_even_for_active_sessions(codec, clock):
    session = codec.issue()
    clock.now = START + ABSOLUTE_LIFETIME_SECONDS - 1
    active = Session(session.session_id, session.issued_at, clock.now)
    assert codec.decode(codec.encode(active)) == active
    clock.now = START + ABSOLUTE_LIFETIME_SECONDS
    assert codec.decode(codec.encode(active)) is None


def test_idle_expiry(codec, clock):
    value = codec.encode(codec.issue())
    clock.now = START + IDLE_LIFETIME_SECONDS - 1
    assert codec.decode(value) is not None
    clock.now = START + IDLE_LIFETIME_SECONDS
    assert codec.decode(value) is None


def test_future_timestamps_beyond_skew_are_rejected(codec):
    later = START + sessions.CLOCK_SKEW_SECONDS + 1
    assert codec.decode(codec.encode(Session("a" * 22, later, later))) is None
    assert codec.decode(codec.encode(Session("a" * 22, START + 10, START))) is None


@pytest.mark.parametrize(
    "value",
    [None, "", "v1", "v1.a", "v1.a.b.c", "v2.a.b", "v1.@@@.@@@", "v1.é.x", "v1." + "a" * 600],
)
def test_malformed_cookies_are_rejected(codec, value):
    assert codec.decode(value) is None


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"sid": "a" * 22, "iat": START},
        {"sid": "a" * 22, "iat": START, "seen": START, "admin": True},
        {"sid": "short", "iat": START, "seen": START},
        {"sid": "a" * 22, "iat": str(START), "seen": START},
        {"sid": "a" * 22, "iat": True, "seen": START},
        {"sid": "a" * 22, "iat": START * 1.0, "seen": START},
    ],
)
def test_signed_but_malformed_payloads_are_rejected(codec, payload):
    assert codec.decode(_sign(codec, payload)) is None


def test_signed_non_json_payload_is_rejected(codec):
    signed = "v1." + sessions._b64encode(b"\xff\xfe")
    assert codec.decode(f"{signed}.{sessions._b64encode(codec._signature(signed))}") is None


def test_refresh_touch_and_max_age(codec, clock):
    session = codec.issue()
    assert codec.max_age(session) == IDLE_LIFETIME_SECONDS
    assert not codec.needs_refresh(session)
    clock.now = START + REFRESH_INTERVAL_SECONDS
    assert codec.needs_refresh(session)
    touched = codec.touch(session)
    assert touched.last_seen == clock.now and touched.issued_at == START
    clock.now = START + ABSOLUTE_LIFETIME_SECONDS - 100
    late = Session(session.session_id, START, clock.now)
    assert codec.max_age(late) == 100
