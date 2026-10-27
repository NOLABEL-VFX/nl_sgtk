"""Exercise expired personal sessions without browser, disk or Flow access."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import nl_sgtk
from nl_sgtk import nl_sgtk as api
from nl_sgtk.provider import NlSgtkProvider


@pytest.fixture
def session(monkeypatch):
    """Provide saved and approved tokens with an isolated fake Flow site."""
    api.invalidate_session_cache()
    monkeypatch.delenv("NL_SGTK_ALLOW_INTERACTIVE", raising=False)
    monkeypatch.setattr(api, "_script_credentials", lambda: (None, None))
    monkeypatch.setattr(api, "update_user_cache", Mock())
    state = SimpleNamespace(
        token="old", faults={}, clients=[], queries=[],
        host="https://flow.invalid", login="artist@example.invalid",
    )

    def default_user():
        """Read the current fake cached token without Toolkit renewal."""
        if state.token is None:
            return None
        return SimpleNamespace(
            login=state.login, http_proxy="proxy.invalid:3128",
            impl=SimpleNamespace(get_session_token=lambda: state.token),
            create_sg_connection=Mock(side_effect=AssertionError("Toolkit")),
        )

    monkeypatch.setattr(
        api.sgtk.authentication, "ShotgunAuthenticator",
        lambda: SimpleNamespace(get_default_user=default_user),
    )

    class Client:
        """Record bounded validation and reject selected fake tokens."""

        def __init__(self, token):
            self.token = token
            self.config = SimpleNamespace(
                timeout_secs=30, max_rpc_attempts=3)

        def check(self, stage):
            state.queries.append((self.token, stage,
                                  self.config.timeout_secs,
                                  self.config.max_rpc_attempts))
            error = state.faults.get((self.token, stage))
            if error:
                raise error

        def find(self, *args):
            self.check("projects")
            return [{"type": "Project", "id": 1}]

        def find_one(self, *args):
            self.check("artist")
            return getattr(state, "artist", {
                "type": "HumanUser", "id": 113,
                "name": "Artist", "login": state.login,
            })

    def client(host, *, session_token, connect, http_proxy):
        """Construct a plain client without accessing a remote site."""
        assert host == state.host
        assert connect is False
        assert http_proxy == "proxy.invalid:3128"
        error = state.faults.get((session_token, "constructor"))
        if error:
            raise error
        connection = Client(session_token)
        state.clients.append(connection)
        return connection

    monkeypatch.setattr(api.shotgun, "Shotgun", client)
    state.launcher = Mock(return_value=(
        state.host, state.login, "fresh", None))
    monkeypatch.setattr(api.sgtk.authentication.app_session_launcher,
                        "process", state.launcher)

    def save(host, login, token, metadata):
        """Replace fake saved credentials only after browser approval."""
        state.token = token

    cache = api.sgtk.authentication.session_cache
    state.save = Mock(side_effect=save)
    monkeypatch.setattr(cache, "cache_session_data", state.save)
    monkeypatch.setattr(cache, "set_current_host", Mock())
    monkeypatch.setattr(cache, "set_current_user", Mock())
    monkeypatch.setattr(cache, "get_session_data", lambda *args: {
        "session_token": state.token})
    monkeypatch.setattr(cache, "get_current_host", lambda: state.host)
    monkeypatch.setattr(cache, "get_current_user", lambda *args: state.login)
    yield state
    api.invalidate_session_cache()


@pytest.mark.parametrize("stage", ["constructor", "projects", "artist"])
def test_rejected_session_is_distinct_and_noninteractive(session, stage):
    """Report rejection from every login phase without starting browser UI."""
    error = api.shotgun.AuthenticationFault("expired")
    session.faults["old", stage] = error
    with pytest.raises(api.TrackerSessionExpiredError) as caught:
        api.sgtk_login(session.host, allow_interactive=False,
                       raise_on_error=True)
    assert caught.value.__cause__ is error
    assert isinstance(caught.value, api.TrackerAuthenticationError)
    assert api.sgtk_login(session.host, allow_interactive=False) == (None, None)
    session.launcher.assert_not_called()


@pytest.mark.parametrize("stage", ["constructor", "projects", "artist"])
def test_interactive_login_renews_rejected_token_once(session, stage):
    """Recover rejection during construction, validation or identity lookup."""
    session.faults["old", stage] = api.shotgun.AuthenticationFault("expired")
    connection, artist = api.sgtk_login(session.host, raise_on_error=True)
    assert connection.token == "fresh"
    assert artist == {"type": "HumanUser", "id": 113, "name": "Artist",
                      "_login": "artist"}
    session.launcher.assert_called_once()


def test_missing_session_needs_only_one_approval(session):
    """Approve once when no saved artist exists."""
    session.token = None
    connection, _ = api.sgtk_login(session.host, raise_on_error=True)
    assert connection.token == "fresh"
    session.launcher.assert_called_once()


def test_rejected_approval_does_not_open_another_browser(session):
    """Surface failed approval validation without an authentication loop."""
    for token in ("old", "fresh"):
        session.faults[token, "projects"] = api.shotgun.AuthenticationFault(
            "expired")
    with pytest.raises(api.TrackerSessionExpiredError):
        api.sgtk_login(session.host, raise_on_error=True)
    session.launcher.assert_called_once()


@pytest.mark.parametrize("stage", ["constructor", "projects", "artist"])
def test_network_failure_does_not_request_renewal(session, stage):
    """Leave transport errors distinguishable from session rejection."""
    error = TimeoutError("unreachable")
    session.faults["old", stage] = error
    with pytest.raises(TimeoutError) as caught:
        api.sgtk_login(session.host, raise_on_error=True)
    assert caught.value is error
    session.launcher.assert_not_called()


def test_renewal_is_personal_and_verifies_before_return(session, monkeypatch):
    """Ignore configured script credentials during explicit personal renewal."""
    monkeypatch.setattr(api, "_script_credentials", lambda: ("script", "key"))
    script = Mock(side_effect=AssertionError("script identity"))
    monkeypatch.setattr(api, "_script_login", script)
    connection, artist = nl_sgtk.renew_session(session.host)
    assert connection.token == "fresh"
    assert artist["type"] == "HumanUser"
    assert session.queries == [("fresh", "projects", 10, 1),
                               ("fresh", "artist", 10, 1)]
    assert connection.config.timeout_secs == 30
    assert connection.config.max_rpc_attempts == 3
    script.assert_not_called()


@pytest.mark.parametrize("artist", [
    None, {"type": "ApiUser", "id": 1, "login": "script"},
    {"type": "HumanUser", "id": 113},
])
def test_unverified_artist_never_completes_renewal(session, artist):
    """Require a real HumanUser result before reporting renewal success."""
    session.artist = artist
    with pytest.raises(api.TrackerAuthenticationError, match="HumanUser"):
        api.renew_session(session.host)
    session.launcher.assert_called_once()


def test_partial_cache_write_retires_stale_clients(session, monkeypatch):
    """Reacquire the token even when saving current-user metadata fails."""
    first, _ = api.sgtk_login(session.host, allow_interactive=False)
    cache = api.sgtk.authentication.session_cache
    monkeypatch.setattr(cache, "set_current_user",
                        Mock(side_effect=OSError("cache metadata")))
    with pytest.raises(OSError, match="cache metadata"):
        api.renew_session(session.host)
    second, _ = api.sgtk_login(session.host, allow_interactive=False)
    assert first.token == "old"
    assert second.token == "fresh"


@pytest.mark.parametrize("fault", ["missing", "old-token", "host", "user"])
def test_approval_requires_matching_disk_readback(session, monkeypatch, fault):
    """An older valid session cannot hide an incomplete approval save."""
    cache = api.sgtk.authentication.session_cache
    before, _ = api.sgtk_login(session.host, allow_interactive=False)
    if fault == "missing":
        monkeypatch.setattr(cache, "get_session_data", lambda *args: None)
    elif fault == "old-token":
        session.save.side_effect = None
    elif fault == "host":
        monkeypatch.setattr(cache, "get_current_host", lambda: None)
    else:
        monkeypatch.setattr(cache, "get_current_user", lambda *args: "other")
    with pytest.raises(api.TrackerAuthenticationError, match="read back"):
        api.launch_interactive_login(session.host)
    assert before.token == "old"
    session.launcher.assert_called_once()
    # A failed approval must release the lock for the next explicit attempt.
    assert api._session_renewal_lock.acquire(blocking=False)
    api._session_renewal_lock.release()


def test_approval_retires_cached_login_and_provider_client(session):
    """Replace thread-local provider clients after successful browser login."""
    provider = NlSgtkProvider(allow_interactive=False)
    # Provider uses the configured site; keep all requests inside this fixture.
    original = api.sgtk_login
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(api, "sgtk_login", lambda **kw: original(
            session.host, **kw))
        first, _ = provider._connection()
        assert provider._connection()[0] is first
        api.launch_interactive_login(session.host)
        second, _ = provider._connection()
    assert first.token == "old"
    assert second.token == "fresh"
    assert first is not second


def test_invalidation_preserves_saved_credentials_and_injected_client(session):
    """Retire owned caches without changing caller-owned connections."""
    first, artist = api.sgtk_login(session.host, allow_interactive=False)
    injected = NlSgtkProvider(sg=first, user=artist)
    session.token = "externally-approved"
    api.invalidate_session_cache()
    second, _ = api.sgtk_login(session.host, allow_interactive=False)
    assert second.token == "externally-approved"
    assert injected._connection()[0] is first
    assert session.token == "externally-approved"
    session.save.assert_not_called()
    session.launcher.assert_not_called()


def test_cancelled_renewal_keeps_previous_cached_session(session):
    """Keep a usable cached login when the artist cancels browser approval."""
    api.sgtk_login(session.host, allow_interactive=False)
    before = len(session.clients)
    session.launcher.side_effect = RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        api.renew_session(session.host)
    connection, _ = api.sgtk_login(session.host, allow_interactive=False)
    assert connection.token == "old"
    assert len(session.clients) == before
    session.save.assert_not_called()


def test_concurrent_approval_opens_one_browser(session):
    """Reject another approval while a browser-only worker is waiting."""
    entered, release = threading.Event(), threading.Event()

    def approve(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return session.host, session.login, "fresh", None

    session.launcher.side_effect = approve
    errors = []

    def renew():
        try:
            api.renew_session(session.host, force=True)
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=renew)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(api.TrackerAuthenticationError,
                           match="already in progress"):
            api.renew_session(session.host)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert errors == []
    session.launcher.assert_called_once()


def test_global_noninteractive_policy_blocks_explicit_renewal(
    session, monkeypatch,
):
    """Respect farm policy even when an explicit thread bypass is supplied."""
    monkeypatch.setenv("NL_SGTK_ALLOW_INTERACTIVE", "0")
    with pytest.raises(api.TrackerAuthenticationError, match="disabled"):
        api.renew_session(session.host, force=True)
    session.launcher.assert_not_called()


def test_inflight_old_login_cannot_repopulate_the_current_cache(
    session, monkeypatch,
):
    """Keep results completed after invalidation under their old generation."""
    entered, release = threading.Event(), threading.Event()
    calls = []

    def login(**kwargs):
        token = session.token
        calls.append(token)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return SimpleNamespace(token=token), {"id": 113}

    monkeypatch.setattr(api, "_sgtk_login_uncached", login)
    results, errors = [], []

    def first_login():
        try:
            results.append(api.sgtk_login(
                session.host, allow_interactive=False, raise_on_error=True))
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=first_login)
    worker.start()
    try:
        assert entered.wait(5)
        session.token = "fresh"
        api.invalidate_session_cache()
        current, _ = api.sgtk_login(session.host, allow_interactive=False)
        assert current.token == "fresh"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert errors == []
    assert results[0][0].token == "old"
    current, _ = api.sgtk_login(session.host, allow_interactive=False)
    assert current.token == "fresh"
