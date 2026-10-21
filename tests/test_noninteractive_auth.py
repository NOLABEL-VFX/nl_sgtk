"""No farm authentication path may launch or renew an interactive session."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nl_sgtk import nl_sgtk as api
from nl_sgtk.provider import NlSgtkProvider


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch):
    api._sgtk_login_cached.cache_clear()
    monkeypatch.setattr(api, "_script_credentials", lambda: (None, None))
    monkeypatch.setattr(api, "update_user_cache", lambda *a, **kw: None)
    monkeypatch.setattr(api, "launch_interactive_login",
                        Mock(side_effect=AssertionError("interactive login")))
    yield
    api._sgtk_login_cached.cache_clear()


def test_missing_session_legacy_return_and_strict_provider(monkeypatch):
    monkeypatch.setattr(api.sgtk.authentication, "ShotgunAuthenticator",
                        lambda: SimpleNamespace(get_default_user=lambda: None))
    monkeypatch.setenv("NL_SGTK_ALLOW_INTERACTIVE", "0")
    assert api.sgtk_login() == (None, None)
    with pytest.raises(api.TrackerAuthenticationError, match="disabled"):
        NlSgtkProvider()._connection()
    api.launch_interactive_login.assert_not_called()


def test_expired_session_uses_raw_client_without_toolkit_renewal(monkeypatch):
    wrapper = Mock(side_effect=AssertionError("Toolkit renewal"))
    user = SimpleNamespace(login="artist", create_sg_connection=wrapper,
                           impl=SimpleNamespace(get_session_token=lambda: "fake"))
    monkeypatch.setattr(api.sgtk.authentication, "ShotgunAuthenticator",
                        lambda: SimpleNamespace(get_default_user=lambda: user))
    client = SimpleNamespace(find=Mock(side_effect=api.shotgun.AuthenticationFault(
        "expired")))
    monkeypatch.setattr(api.shotgun, "Shotgun", lambda *a, **kw: client)
    with pytest.raises(api.TrackerAuthenticationError) as caught:
        api.sgtk_login(allow_interactive=False, raise_on_error=True)
    assert isinstance(caught.value.__cause__, api.shotgun.AuthenticationFault)
    wrapper.assert_not_called()
    api.launch_interactive_login.assert_not_called()


def test_failed_script_login_has_no_interactive_fallback(monkeypatch):
    monkeypatch.setattr(api, "_script_credentials", lambda: ("fake", "fake"))
    error = api.shotgun.AuthenticationFault("rejected")
    monkeypatch.setattr(api, "_script_login", Mock(side_effect=error))
    ensure = Mock(side_effect=AssertionError("script fallback"))
    monkeypatch.setattr(api, "ensure_sgtk_user", ensure)
    with pytest.raises(api.TrackerAuthenticationError) as caught:
        api.sgtk_login(allow_interactive=False, raise_on_error=True)
    assert caught.value.__cause__ is error
    ensure.assert_not_called()


def test_cache_separates_interactive_policy(monkeypatch):
    calls = []
    def login(**kwargs):
        calls.append(kwargs["allow_interactive"])
        return SimpleNamespace(), {"id": 1}
    monkeypatch.setattr(api, "_sgtk_login_uncached", login)
    monkeypatch.delenv("NL_SGTK_ALLOW_INTERACTIVE", raising=False)
    api.sgtk_login(allow_interactive=True)
    api.sgtk_login(allow_interactive=False)
    assert calls == [True, False]


def test_artist_can_request_interactive_login(monkeypatch):
    authenticator = SimpleNamespace(get_default_user=Mock(side_effect=[None, "user"]))
    monkeypatch.setattr(api.sgtk.authentication, "ShotgunAuthenticator",
                        lambda: authenticator)
    monkeypatch.setattr(api, "launch_interactive_login", Mock())
    monkeypatch.delenv("NL_SGTK_ALLOW_INTERACTIVE", raising=False)
    assert api.ensure_sgtk_user(allow_interactive=True) == "user"
    api.launch_interactive_login.assert_called_once()
