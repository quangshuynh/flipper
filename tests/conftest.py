import pytest

import main
import web.security as web_security


@pytest.fixture(autouse=True)
def isolated_web_security_environment(monkeypatch):
    """Keep a developer's real .env sign-in settings out of every test.

    Tests run in local mode without a password unless they configure security explicitly.
    """
    monkeypatch.setattr(main, "load_dotenv", lambda *args, **kwargs: None)
    for name in web_security.SECURITY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(web_security, "login_throttle", web_security.LoginThrottle())
