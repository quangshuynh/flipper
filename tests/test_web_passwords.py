import base64
import unicodedata
from getpass import GetPassWarning

import pytest

import main as cli
import web.passwords as passwords
import web.security as web_security
from web.passwords import (
    PasswordHashError,
    PasswordPolicyError,
    hash_password,
    parse_password_hash,
    verify_password,
)

PASSWORD = "correct horse battery staple"
FAST = {"n": 2**14}


@pytest.fixture(scope="module")
def encoded():
    return hash_password(PASSWORD, **FAST)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


# Hash format and verification


def test_hash_is_versioned_self_describing_and_contains_no_plaintext(encoded):
    scheme, version, parameters, salt, key = encoded.split(":")
    assert (scheme, version, parameters) == ("flipper-scrypt", "v1", "n=16384,r=8,p=1")
    assert len(base64.urlsafe_b64decode(salt + "==")) == passwords.SALT_BYTES
    assert len(base64.urlsafe_b64decode(key + "=")) == passwords.KEY_BYTES
    assert PASSWORD not in encoded
    assert not set(encoded) & set("$#'\" \t")


def test_default_parameters_follow_the_scrypt_baseline():
    assert (passwords.DEFAULT_N, passwords.DEFAULT_R, passwords.DEFAULT_P) == (2**17, 8, 1)


def test_verification_accepts_only_the_exact_password(encoded):
    stored = parse_password_hash(encoded)
    assert verify_password(PASSWORD, stored) is True
    for attempt in ("", "correct horse battery stapl", PASSWORD + " ", PASSWORD.upper()):
        assert verify_password(attempt, stored) is False


def test_salts_are_unique_for_the_same_password():
    first = hash_password(PASSWORD, **FAST)
    second = hash_password(PASSWORD, **FAST)
    assert first != second
    assert first.split(":")[3] != second.split(":")[3]
    assert verify_password(PASSWORD, parse_password_hash(second))


def test_unicode_is_normalized_so_every_keyboard_verifies():
    composed = "café password 2026"
    stored = parse_password_hash(hash_password(composed, **FAST))
    assert verify_password(unicodedata.normalize("NFD", composed), stored)


def test_verification_uses_constant_time_comparison(monkeypatch, encoded):
    calls = []
    real = passwords.hmac.compare_digest

    def spy(left, right):
        calls.append((len(left), len(right)))
        return real(left, right)

    monkeypatch.setattr(passwords.hmac, "compare_digest", spy)
    verify_password("wrong password value", parse_password_hash(encoded))
    assert calls == [(passwords.KEY_BYTES, passwords.KEY_BYTES)]


def test_overlong_or_non_string_input_is_rejected_without_key_derivation(monkeypatch, encoded):
    stored = parse_password_hash(encoded)
    monkeypatch.setattr(passwords, "_derive", lambda *args: pytest.fail("derived"))
    assert verify_password("x" * (passwords.MAX_PASSWORD_LENGTH + 1), stored) is False
    assert verify_password(None, stored) is False


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-hash",
        "flipper-scrypt:v1:n=16384,r=8,p=1:c2FsdA",
        "flipper-scrypt:v1:n=16384,r=8:AAAAAAAAAAAAAAAAAAAAAA:" + "A" * 43,
        "flipper-scrypt:v1:n=16384,r=8,p=1:!!!:" + "A" * 43,
        "flipper-scrypt:v1:n=16384,r=8,p=1:AAAAAAAAAAAAAAAAAAAAAA:AAAA",
        "flipper-scrypt:v1:n=16384,r=8,p=1:AAAA:" + "A" * 43,
        "flipper-scrypt:v1:n=16384,r=8,p=1:AAAAAAAAAAAAAAAAAAAAAB:" + "A" * 43,
        " flipper-scrypt:v1:n=16384,r=8,p=1:AAAAAAAAAAAAAAAAAAAAAA:" + "A" * 42 + "\n x",
    ],
)
def test_malformed_hashes_are_rejected_without_echoing_them(value):
    with pytest.raises(PasswordHashError) as raised:
        parse_password_hash(value)
    assert str(raised.value) == "password hash is malformed"


def test_unsupported_version_and_algorithm_are_distinguished(encoded):
    with pytest.raises(PasswordHashError, match="version is unsupported"):
        parse_password_hash(encoded.replace(":v1:", ":v2:"))
    with pytest.raises(PasswordHashError, match="algorithm is unsupported"):
        parse_password_hash(encoded.replace("flipper-scrypt", "flipper-md5"))


@pytest.mark.parametrize(
    "parameters",
    [
        "n=1024,r=8,p=1",
        "n=16383,r=8,p=1",
        "n=2097152,r=8,p=1",
        "n=16384,r=0,p=1",
        "n=16384,r=8,p=9",
    ],
)
def test_out_of_bounds_parameters_are_refused(parameters):
    value = f"flipper-scrypt:v1:{parameters}:{_b64(b's' * 16)}:{_b64(b'k' * 32)}"
    with pytest.raises(PasswordHashError, match="parameters are unsupported"):
        parse_password_hash(value)


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("", "must not be empty"),
        ("            ", "must not be empty"),
        ("short pass", "at least 12"),
        ("aaaaaaaaaaaaaaaa", "different characters"),
        ("x" * 1025 + "abcdef", "at most 1024"),
    ],
)
def test_policy_rejects_obviously_accidental_passwords(password, message):
    with pytest.raises(PasswordPolicyError, match=message):
        hash_password(password, **FAST)


def test_parsed_hash_repr_never_shows_salt_or_key(encoded):
    shown = repr(parse_password_hash(encoded))
    assert encoded.split(":")[3] not in shown and encoded.split(":")[4] not in shown


# CLI generation


def _prompts(monkeypatch, *answers):
    queue = list(answers)
    prompts = []

    def fake_getpass(prompt):
        prompts.append(prompt)
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(cli, "getpass", fake_getpass)
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: None)
    return prompts


def test_cli_hash_password_prompts_twice_and_prints_only_the_hash(monkeypatch, capsys):
    prompts = _prompts(monkeypatch, PASSWORD, PASSWORD)
    monkeypatch.setattr(cli, "hash_password", lambda value: hash_password(value, **FAST))

    assert cli.main(["auth", "hash-password"]) == 0

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert verify_password(PASSWORD, parse_password_hash(lines[0]))
    assert PASSWORD not in captured.out + captured.err
    assert len(prompts) == 2 and all("password" in prompt.lower() for prompt in prompts)


def test_cli_hash_uses_default_strong_parameters(monkeypatch, capsys):
    _prompts(monkeypatch, PASSWORD, PASSWORD)
    assert cli.main(["auth", "hash-password"]) == 0
    assert ":n=131072,r=8,p=1:" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ((PASSWORD, PASSWORD + "!"), "passwords do not match"),
        (("", ""), "must not be empty"),
        (("tooshort", "tooshort"), "at least 12"),
        ((GetPassWarning("echo"),), "interactive terminal is required"),
        ((EOFError(),), "cancelled"),
    ],
)
def test_cli_hash_password_failures_print_nothing_to_stdout(monkeypatch, capsys, answers, message):
    _prompts(monkeypatch, *answers)

    assert cli.main(["auth", "hash-password"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert PASSWORD not in captured.err


def test_cli_never_accepts_a_plaintext_password_argument(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["auth", "hash-password", "--password", PASSWORD])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["auth", "hash-password", PASSWORD])


def test_cli_session_secret_is_random_and_acceptable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: None)
    values = []
    for _ in range(2):
        assert cli.main(["auth", "session-secret"]) == 0
        values.append(capsys.readouterr().out.strip())
    assert values[0] != values[1]
    for value in values:
        assert len(value) == 64
        web_security._check_session_secret(value)
