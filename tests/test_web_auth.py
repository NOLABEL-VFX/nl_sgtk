"""Verify web identity isolation without browser, cache, or Flow access."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock
import logging
import threading

import pytest

Fernet = pytest.importorskip("cryptography.fernet").Fernet

import nl_sgtk
from nl_sgtk import nl_sgtk as api
from nl_sgtk import web_auth


@pytest.fixture
def flow(monkeypatch):
    state = SimpleNamespace(
        host="https://flow.invalid", tokens={}, errors={}, clients=[],
        approval=("alice", "flow-secret-alice"), callbacks=[], barrier=None,
        verify_hook=None,
    )
    state.users = {
        "alice": {"type": "HumanUser", "id": 11, "name": "Alice",
                  "login": "alice"},
        "bob": {"type": "HumanUser", "id": 22, "name": "Bob",
                "login": "bob"},
    }
    monkeypatch.delenv("NL_SGTK_ALLOW_INTERACTIVE", raising=False)
    # Any use of machine credentials, global caches, or profile writes fails.
    for name in ("ensure_sgtk_user", "sgtk_login", "_script_credentials",
                 "invalidate_session_cache", "update_user_cache"):
        monkeypatch.setattr(api, name, Mock(side_effect=AssertionError(name)))
    cache = api.sgtk.authentication.session_cache
    for name in ("get_session_data", "cache_session_data", "get_current_user",
                 "set_current_user", "get_current_host", "set_current_host"):
        monkeypatch.setattr(
            cache, name, Mock(side_effect=AssertionError(name)),
        )

    class Client:
        def __init__(self, host, *, session_token, connect, http_proxy):
            assert host == state.host
            assert connect is False
            self.token = session_token
            self.proxy = http_proxy
            self.closed = False
            self.calls = 0
            self.config = SimpleNamespace(timeout_secs=30, max_rpc_attempts=3)
            state.clients.append(self)

        def find_one(self, entity, filters, fields):
            assert entity == "HumanUser"
            assert self.config.timeout_secs == 10
            assert self.config.max_rpc_attempts == 1
            self.calls += 1
            if state.verify_hook:
                state.verify_hook()
            if self.token in state.errors:
                raise state.errors[self.token]
            login = state.tokens[self.token]
            if filters != [["login", "is", login]]:
                return None
            return dict(state.users[login])

        def close(self):
            self.closed = True

    def approve(host, *, product, http_proxy, browser_open_callback,
                keep_waiting_callback):
        assert host == state.host
        login, token = state.approval
        if state.barrier:
            login, token = state.barrier()
        url = host + "/approve/" + login
        assert browser_open_callback(url)
        state.callbacks.append(url)
        assert keep_waiting_callback()
        state.tokens[token] = login
        api.sgtk.authentication.app_session_launcher.logger.debug(
            "Session token: %s", token,
        )
        return host, login, token, None

    monkeypatch.setattr(api.shotgun, "Shotgun", Client)
    state.approve = Mock(side_effect=approve)
    monkeypatch.setattr(
        api.sgtk.authentication.app_session_launcher, "process", state.approve,
    )
    return state


@pytest.fixture
def auth(flow, tmp_path):
    manager = nl_sgtk.WebAuthenticator(
        flow.host, encryption_key=Fernet.generate_key(),
        database=tmp_path / "sessions.sqlite3", product="ComfyUI test",
        http_proxy="proxy.invalid:3128",
    )
    yield manager
    manager.close()


def signed_in(auth, flow, login="alice"):
    flow.approval = login, "flow-secret-" + login
    session = auth.login(lambda url: None)
    session.close()
    return session


def test_persistence_contains_hashes_and_ciphertext_not_credentials(
    auth, flow, tmp_path,
):
    session = signed_in(auth, flow)
    database = tmp_path / "sessions.sqlite3"
    data = database.read_bytes()
    assert b"flow-secret-alice" not in data
    assert session.browser_token.encode() not in data
    assert session.browser_token != "flow-secret-alice"
    row = auth._record(auth._key(session.browser_token))
    assert row["token_hash"] == auth._key(session.browser_token)
    assert row["login"] == "alice"
    assert b"flow-secret-alice" not in row["credential"]
    assert "flow-secret" not in repr(session)
    assert session.browser_token not in repr(session)
    assert "token" not in session.user
    assert session.user == {
        "type": "HumanUser", "id": 11, "name": "Alice", "_login": "alice",
    }


def test_accidental_session_serialization_does_not_expose_credentials(
    auth, flow,
):
    from dataclasses import asdict
    import json

    session = signed_in(auth, flow)
    with pytest.raises(TypeError):
        asdict(session)
    with pytest.raises(TypeError):
        vars(session)
    with pytest.raises(TypeError):
        json.dumps(session)


def test_valid_cookie_revalidates_without_machine_login(auth, flow):
    original = signed_in(auth, flow)
    flow.approve.reset_mock()
    with auth.authenticate(original.browser_token) as first:
        assert first.identity.id == 11
        assert first.renewed is False
        assert first.sg.proxy == "proxy.invalid:3128"
        assert first.sg.config.timeout_secs == 30
        assert first.sg.config.max_rpc_attempts == 3
    with auth.authenticate(original.browser_token) as second:
        assert second.sg is not first.sg
        assert second.sg.calls == 1
    assert first.sg.closed and second.sg.closed
    flow.approve.assert_not_called()


def test_two_users_do_not_mix_connections_identities_or_logout(auth, flow):
    alice = signed_in(auth, flow, "alice")
    bob = signed_in(auth, flow, "bob")
    assert alice.browser_token != bob.browser_token
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(auth.authenticate, alice.browser_token)
        second = workers.submit(auth.authenticate, bob.browser_token)
        a, b = first.result(), second.result()
    try:
        assert (a.identity.id, a.sg.token) == (11, "flow-secret-alice")
        assert (b.identity.id, b.sg.token) == (22, "flow-secret-bob")
        auth.logout(alice.browser_token)
        with pytest.raises(nl_sgtk.WebAuthenticationRequired):
            auth.authenticate(alice.browser_token)
        with auth.authenticate(bob.browser_token) as again:
            assert again.identity.id == 22
    finally:
        a.close()
        b.close()


def test_parallel_approvals_have_separate_callbacks_and_no_global_lock(
    auth, flow,
):
    barrier = threading.Barrier(2)
    local = threading.local()
    deliveries = {}

    def approved_identity():
        barrier.wait(timeout=5)
        return local.login, "flow-secret-" + local.login

    def worker(login):
        local.login = login
        deliver = lambda url: deliveries.setdefault(login, url)
        with auth.login(deliver) as grant:
            return grant.identity.id, grant.browser_token

    flow.barrier = approved_identity
    with ThreadPoolExecutor(max_workers=2) as workers:
        a = workers.submit(worker, "alice")
        b = workers.submit(worker, "bob")
        alice, bob = a.result(timeout=10), b.result(timeout=10)
    assert alice[0] == 11 and bob[0] == 22
    assert alice[1] != bob[1]
    assert deliveries["alice"].endswith("/alice")
    assert deliveries["bob"].endswith("/bob")


@pytest.mark.parametrize("token", [None, "", "made-up-cookie", "x" * 5000])
def test_missing_unknown_and_malformed_cookies_fail_closed(auth, flow, token):
    with pytest.raises(nl_sgtk.WebAuthenticationRequired):
        auth.authenticate(token)
    assert flow.clients == []
    flow.approve.assert_not_called()


def test_raw_flow_token_and_database_hash_cannot_act_as_app_cookie(auth, flow):
    session = signed_in(auth, flow)
    for impostor in ("flow-secret-alice", auth._key(session.browser_token)):
        with pytest.raises(nl_sgtk.WebAuthenticationRequired):
            auth.authenticate(impostor)


def test_rejected_flow_session_requires_reauth_and_rotates_app_cookie(
    auth, flow,
):
    session = signed_in(auth, flow)
    flow.errors["flow-secret-alice"] = api.shotgun.AuthenticationFault(
        "expired")
    flow.approval = "alice", "flow-refreshed-alice"
    flow.approve.reset_mock()
    with auth.authenticate(
        session.browser_token, browser_open_callback=lambda url: True,
    ) as renewed:
        assert renewed.renewed
        assert renewed.browser_token != session.browser_token
        assert renewed.sg.token == "flow-refreshed-alice"
    flow.approve.assert_called_once()
    with pytest.raises(nl_sgtk.WebAuthenticationRequired):
        auth.authenticate(session.browser_token)


def test_reauthentication_cannot_silently_switch_users(auth, flow):
    session = signed_in(auth, flow)
    flow.errors["flow-secret-alice"] = api.shotgun.AuthenticationFault(
        "expired")
    flow.approval = "bob", "flow-secret-bob"
    with pytest.raises(nl_sgtk.WebIdentityMismatchError):
        auth.authenticate(
            session.browser_token, browser_open_callback=lambda url: True,
        )
    assert flow.clients[-1].closed


def test_queued_work_rejects_another_user_even_with_a_valid_cookie(auth, flow):
    bob = signed_in(auth, flow, "bob")
    count = len(flow.clients)
    with pytest.raises(nl_sgtk.WebIdentityMismatchError):
        auth.authenticate(bob.browser_token, expected_user_id=11)
    assert len(flow.clients) == count


def test_transport_failure_preserves_session_without_approval(auth, flow):
    session = signed_in(auth, flow)
    flow.approve.reset_mock()
    flow.errors["flow-secret-alice"] = TimeoutError("network unavailable")
    with pytest.raises(TimeoutError):
        auth.authenticate(
            session.browser_token, browser_open_callback=lambda url: True,
        )
    assert auth._record(auth._key(session.browser_token))
    flow.approve.assert_not_called()


def test_new_approval_rejection_never_starts_a_second_login(auth, flow):
    flow.errors["flow-secret-alice"] = api.shotgun.AuthenticationFault(
        "expired")
    with pytest.raises(api.TrackerSessionExpiredError):
        auth.authenticate(None, browser_open_callback=lambda url: True)
    flow.approve.assert_called_once()


def test_logout_during_verification_cannot_resurrect_session(auth, flow):
    session = signed_in(auth, flow)
    flow.verify_hook = lambda: auth.logout(session.browser_token)
    with pytest.raises(nl_sgtk.WebAuthenticationRequired) as caught:
        auth.authenticate(session.browser_token)
    assert caught.value.reason == "revoked"
    assert flow.clients[-1].closed


@pytest.mark.parametrize("column", ["expires_at", "last_seen"])
def test_absolute_and_idle_expiry_require_new_approval(auth, flow, column):
    session = signed_in(auth, flow)
    with auth._db:
        # Column names are test constants, never user input.
        auth._db.execute("UPDATE nl_web_sessions SET " + column + " = 0")
    with pytest.raises(nl_sgtk.WebAuthenticationRequired) as caught:
        auth.authenticate(session.browser_token)
    assert caught.value.reason == "expired"


def test_persistent_session_survives_backend_restart_with_same_key(
    flow, tmp_path,
):
    key = Fernet.generate_key()
    path = tmp_path / "persistent.sqlite3"
    with nl_sgtk.WebAuthenticator(
        flow.host, encryption_key=key, database=path,
    ) as first:
        session = signed_in(first, flow)
    with nl_sgtk.WebAuthenticator(
        flow.host, encryption_key=key, database=path,
    ) as second:
        with second.authenticate(session.browser_token) as restored:
            assert restored.identity.id == 11


def test_wrong_encryption_key_fails_closed(flow, tmp_path):
    path = tmp_path / "persistent.sqlite3"
    with nl_sgtk.WebAuthenticator(
        flow.host, encryption_key=Fernet.generate_key(), database=path,
    ) as first:
        session = signed_in(first, flow)
    with nl_sgtk.WebAuthenticator(
        flow.host, encryption_key=Fernet.generate_key(), database=path,
    ) as second:
        with pytest.raises(nl_sgtk.WebAuthenticationRequired) as caught:
            second.authenticate(session.browser_token)
        assert caught.value.reason == "invalid"


def test_database_identity_tampering_cannot_reassign_session(auth, flow):
    session = signed_in(auth, flow)
    with auth._db:
        auth._db.execute(
            "UPDATE nl_web_sessions SET user_id = 22, login = 'bob'",
        )
    with pytest.raises(nl_sgtk.WebAuthenticationRequired) as caught:
        auth.authenticate(session.browser_token)
    assert caught.value.reason == "invalid"


def test_ciphertext_cannot_be_swapped_between_sessions(auth, flow):
    alice = signed_in(auth, flow, "alice")
    bob = signed_in(auth, flow, "bob")
    alice_key = auth._key(alice.browser_token)
    bob_key = auth._key(bob.browser_token)
    alice_cipher = auth._record(alice_key)["credential"]
    with auth._db:
        auth._db.execute(
            "UPDATE nl_web_sessions SET credential = ? WHERE token_hash = ?",
            (alice_cipher, bob_key),
        )
    with pytest.raises(nl_sgtk.WebAuthenticationRequired):
        auth.authenticate(bob.browser_token)


def test_product_scopes_do_not_share_browser_sessions(auth, flow, tmp_path):
    session = signed_in(auth, flow)
    with nl_sgtk.WebAuthenticator(
        flow.host, encryption_key=Fernet.generate_key(),
        database=tmp_path / "sessions.sqlite3", product="Another web app",
    ) as other:
        with pytest.raises(nl_sgtk.WebAuthenticationRequired):
            other.authenticate(session.browser_token)


def test_cancellation_after_approval_does_not_persist_credentials(auth, flow):
    checks = iter([True, True, True, False])
    with pytest.raises(nl_sgtk.WebAuthenticationRequired):
        auth.login(
            lambda url: True, keep_waiting_callback=lambda: next(checks),
        )
    count = auth._db.execute(
        "SELECT COUNT(*) FROM nl_web_sessions",
    ).fetchone()[0]
    assert count == 0


def test_cancellation_during_verification_discards_result(auth, flow):
    cancelled = threading.Event()
    flow.verify_hook = cancelled.set
    with pytest.raises(nl_sgtk.WebAuthenticationRequired):
        auth.login(
            lambda url: True,
            keep_waiting_callback=lambda: not cancelled.is_set(),
        )
    assert flow.clients[-1].closed
    count = auth._db.execute(
        "SELECT COUNT(*) FROM nl_web_sessions",
    ).fetchone()[0]
    assert count == 0


def test_auth_debug_logs_never_contain_flow_token(auth, flow, caplog):
    with caplog.at_level(logging.DEBUG):
        signed_in(auth, flow)
    assert "flow-secret-alice" not in caplog.text


def test_global_noninteractive_policy_allows_reuse_but_not_approval(
    auth, flow, monkeypatch,
):
    session = signed_in(auth, flow)
    monkeypatch.setenv("NL_SGTK_ALLOW_INTERACTIVE", "0")
    with auth.authenticate(session.browser_token) as restored:
        assert restored.identity.id == 11
    with pytest.raises(api.TrackerAuthenticationError, match="disabled"):
        auth.login(lambda url: True)


@pytest.mark.parametrize("url", ["http://flow.invalid", "https://user@host",
                                 "https://host/path", "https://host?key=1"])
def test_invalid_flow_origins_are_rejected(url):
    with pytest.raises(ValueError):
        nl_sgtk.WebAuthenticator(url, encryption_key=Fernet.generate_key())


def test_web_extra_is_loaded_only_when_constructing_authenticator(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def without_crypto(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("Optional dependency is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_crypto)
    assert callable(nl_sgtk.sgtk_login)
    with pytest.raises(ImportError, match="nl_sgtk\\[web\\]"):
        nl_sgtk.WebAuthenticator(encryption_key=b"unused")
