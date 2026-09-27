"""Production container and Render Blueprint configuration.

These tests read the committed deployment files; they never build an image or contact a host.
CI's container job builds and exercises the real image.
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient

import web.app as web_app
import web.security as web_security
from storage_config import FILE_BACKEND, resolve_storage
from web.app import app
from web.passwords import hash_password
from web.security import Access, classify, resolve_web_security

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
ENTRYPOINT = (ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")
CLI_WRAPPER = (ROOT / "deploy" / "flipper-cli").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

ORIGIN = "https://flipper-app.example.test"
PASSWORD = "synthetic owner passphrase"
SECRET = "Zt4vQ9pLr2Xw8Kd3Nf6Hj1Ms5Bc7Ga0Ey-Ui_Oo4Pp2Rr6Tt8"
SECRET_NAMES = {
    "FLIPPER_PASSWORD_HASH",
    "FLIPPER_SESSION_SECRET",
    "EBAY_SELLER_CLIENT_ID",
    "EBAY_SELLER_CLIENT_SECRET",
    "EBAY_SELLER_RUNAME",
}


def _instruction(name: str) -> str:
    matches = re.findall(rf"^{name} (.+)$", DOCKERFILE, flags=re.MULTILINE)
    assert len(matches) == 1, f"expected exactly one {name}"
    return matches[0]


def _image_env() -> dict[str, str]:
    block = re.search(r"^ENV FLIPPER_WEB_SECURITY_MODE=.*?(?=^\S|\Z)", DOCKERFILE, re.M | re.S)
    assert block is not None
    return dict(token.split("=", 1) for token in shlex.split(block[0][4:].replace("\\\n", " ")))


def _start_command() -> list[str]:
    command = json.loads(_instruction("CMD"))
    assert command[:2] == ["sh", "-c"]
    return shlex.split(command[2])


@pytest.fixture(scope="module")
def blueprint():
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    (service,) = document["services"]
    return service


# Dockerfile and entrypoint


def test_image_uses_ci_python_and_repository_dependencies():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_version = re.search(r'python-version: "(\d+\.\d+)"', ci)[1]
    assert _instruction("FROM") == f"python:{ci_version}-slim-bookworm"
    assert "pip install --no-compile -r requirements.txt" in DOCKERFILE


def test_start_command_is_one_production_uvicorn_process():
    command = _start_command()
    assert command[:3] == ["exec", "uvicorn", "web.app:app"]
    assert command[command.index("--host") + 1] == "0.0.0.0"
    assert command[command.index("--port") + 1] == "$PORT"
    # Forwarded headers are never trusted; security uses FLIPPER_PUBLIC_ORIGIN instead.
    assert "--no-proxy-headers" in command
    assert "--forwarded-allow-ips" not in command
    for development_or_scaling in ("--reload", "--workers", "--proxy-headers"):
        assert development_or_scaling not in command
    assert "--timeout-graceful-shutdown" in command


def test_image_defaults_fail_closed_to_hosted_mode_on_a_persistent_data_dir():
    env = _image_env()
    assert env["FLIPPER_WEB_SECURITY_MODE"] == "hosted"
    assert env["FLIPPER_CREDENTIAL_BACKEND"] == FILE_BACKEND
    data_dir = PurePosixPath(env["FLIPPER_DATA_DIR"])
    assert data_dir.is_absolute() and data_dir.parent != PurePosixPath("/")
    # The image never carries secrets or explicit path overrides that could bypass the data dir.
    for name in SECRET_NAMES | {"FLIPPER_INVENTORY_DB", "FLIPPER_ATTACHMENT_ROOT"}:
        assert name not in DOCKERFILE

    with pytest.raises(web_security.WebSecurityConfigurationError):
        resolve_web_security({"FLIPPER_WEB_SECURITY_MODE": env["FLIPPER_WEB_SECURITY_MODE"]})


def test_container_drops_root_and_refuses_ephemeral_storage():
    assert "useradd --system" in DOCKERFILE
    assert _instruction("ENTRYPOINT") == '["/app/deploy/entrypoint.sh"]'
    for script in (ENTRYPOINT, CLI_WRAPPER):
        assert script.startswith("#!/bin/sh\n")
        assert "\r" not in script
        assert "set -eu" in script and "umask 077" in script
        assert "setpriv --reuid=flipper --regid=flipper --init-groups --inh-caps=-all" in script
    assert 'mountpoint -q "$disk"' in ENTRYPOINT
    assert '"$disk" = "/"' in ENTRYPOINT
    assert ENTRYPOINT.rstrip().endswith('exec "$@"')
    # Render SSH/SFTP (backup transfer) needs these; the image runs no SSH server itself.
    assert "openssh-sftp-server" in DOCKERFILE and "install -d -m 0700 /root/.ssh" in DOCKERFILE
    assert "openssh-server" not in DOCKERFILE and "EXPOSE 22" not in DOCKERFILE


def test_build_context_is_an_allow_list_excluding_data_and_secrets():
    rules = [line for line in DOCKERIGNORE if line and not line.startswith("#")]
    assert rules[0] == "*", "the build context must deny everything by default"
    included = {rule[1:] for rule in rules if rule.startswith("!")}
    forbidden = (".env", "context.md", ".claude", "data/", "tests", "docs", ".git")
    for entry in included:
        assert not entry.startswith(forbidden) or entry == "data/listings.json", entry
    for pattern in ("**/*.db", "**/.env", "**/attachments", "**/credentials", "**/__pycache__"):
        assert pattern in rules
    # Every top-level package the app imports is included.
    packages = {path.parent.name for path in ROOT.glob("*/__init__.py")}
    assert packages <= {entry.rstrip("/") for entry in included}


# Render Blueprint


def test_blueprint_runs_one_docker_instance_with_a_persistent_disk(blueprint):
    assert blueprint["type"] == "web"
    assert blueprint["runtime"] == "docker"
    assert blueprint["dockerfilePath"] == "./Dockerfile"
    assert blueprint["numInstances"] == 1
    assert "scaling" not in blueprint
    assert blueprint["plan"] != "free", "free web services cannot attach a persistent disk"
    assert blueprint["autoDeployTrigger"] in {"checksPass", "off"}
    disk = blueprint["disk"]
    assert disk["sizeGB"] >= 1

    env = {item["key"]: item for item in blueprint["envVars"]}
    assert env["FLIPPER_WEB_SECURITY_MODE"]["value"] == "hosted"
    assert env["FLIPPER_CREDENTIAL_BACKEND"]["value"] == FILE_BACKEND
    data_dir = PurePosixPath(env["FLIPPER_DATA_DIR"]["value"])
    assert data_dir.parent == PurePosixPath(disk["mountPath"])
    assert env["FLIPPER_DATA_DIR"]["value"] == _image_env()["FLIPPER_DATA_DIR"]


def test_blueprint_never_contains_secret_values(blueprint):
    env = {item["key"]: item for item in blueprint["envVars"]}
    for name in SECRET_NAMES | {"FLIPPER_PUBLIC_ORIGIN"}:
        assert env[name] == {"key": name, "sync": False}
    for item in blueprint["envVars"]:
        assert "generateValue" not in item


def test_health_check_path_is_an_existing_non_data_public_route(blueprint):
    path = blueprint["healthCheckPath"]
    assert classify("GET", path) is Access.AUTHENTICATION


@pytest.mark.skipif(os.name != "posix", reason="the Blueprint's POSIX paths are absolute on Linux")
def test_blueprint_paths_resolve_every_store_onto_the_disk(blueprint):
    env = {item["key"]: item.get("value", "") for item in blueprint["envVars"]}
    storage = resolve_storage(env)
    disk = PurePosixPath(blueprint["disk"]["mountPath"])
    assert PurePosixPath(storage.inventory_database.as_posix()).is_relative_to(disk)
    assert storage.attachment_root is None  # beside the database, on the same disk
    assert PurePosixPath(storage.credential_file.as_posix()).is_relative_to(disk)
    assert storage.credential_backend == FILE_BACKEND


# Hosted startup with the production configuration shape


@pytest.fixture
def hosted_env(monkeypatch, tmp_path):
    data_dir = tmp_path / "disk" / "flipper"
    values = {
        "FLIPPER_WEB_SECURITY_MODE": "hosted",
        "FLIPPER_DATA_DIR": str(data_dir),
        "FLIPPER_CREDENTIAL_BACKEND": FILE_BACKEND,
        "FLIPPER_PASSWORD_HASH": hash_password(PASSWORD, n=2**14),
        "FLIPPER_SESSION_SECRET": SECRET,
        "FLIPPER_PUBLIC_ORIGIN": ORIGIN,
    }
    for name in ("FLIPPER_INVENTORY_DB", "FLIPPER_ATTACHMENT_ROOT", "FLIPPER_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(web_app, "_initialized_databases", set())
    monkeypatch.setattr(web_security, "login_throttle", web_security.LoginThrottle())
    return data_dir


def test_hosted_startup_creates_the_database_under_the_data_dir(hosted_env):
    with TestClient(app, base_url=ORIGIN):
        pass
    assert (hosted_env / "flipper_inventory.db").is_file()
    assert web_app._storage().inventory_database == hosted_env / "flipper_inventory.db"


def test_hosted_startup_fails_without_required_secrets(hosted_env, monkeypatch):
    monkeypatch.delenv("FLIPPER_SESSION_SECRET")
    with pytest.raises(web_security.WebSecurityConfigurationError):
        with TestClient(app, base_url=ORIGIN):
            pass
    assert not (hosted_env / "flipper_inventory.db").exists()


def test_health_check_request_succeeds_without_a_session(hosted_env, blueprint):
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get(blueprint["healthCheckPath"])
    assert response.status_code == 200
    assert "Set-Cookie" not in response.headers


def test_smoke_script_passes_against_hosted_app(hosted_env):
    sys.path.insert(0, str(ROOT / "deploy"))
    try:
        import smoke
    finally:
        sys.path.pop(0)
    lines = []
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        smoke.run_smoke(client, ORIGIN, PASSWORD, report=lines.append)
    assert lines and all(line.startswith("ok  ") for line in lines)
    assert not any(PASSWORD in line or SECRET in line for line in lines)
