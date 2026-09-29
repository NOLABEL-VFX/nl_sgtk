import threading
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('kwargs', [{}, {'force': False}])
def test_worker_requires_force_for_interactive_login(monkeypatch, kwargs):
    from nl_sgtk import nl_sgtk as api

    calls = []
    monkeypatch.setattr(
        api.sgtk.authentication.app_session_launcher, 'process',
        lambda *args, **kwargs: calls.append('browser'),
    )
    errors = []

    def worker():
        try:
            api.launch_interactive_login(**kwargs)
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert calls == []
    assert len(errors) == 1
    assert 'Sign in from the main' in errors[0]


@pytest.mark.parametrize(
    'in_worker, kwargs',
    [(False, {}), (False, {'force': True}), (True, {'force': True})],
)
def test_allowed_login_opens_browser_and_caches_session(
    monkeypatch, in_worker, kwargs,
):
    from nl_sgtk import nl_sgtk as api

    host = 'https://shotgrid.invalid'
    login_url = host + '/login'
    session_data = (host, 'artist', 'test-token', {'test': True})
    browser = Mock(return_value=True)

    def process(base_url, *, product, browser_open_callback):
        assert base_url == host
        assert product == 'Non-Qt test host'
        assert browser_open_callback(login_url) is True
        return session_data

    launcher = Mock(side_effect=process)
    monkeypatch.setattr(
        api.sgtk.authentication.app_session_launcher, 'process', launcher,
    )
    cache = Mock()
    for name in ('cache_session_data', 'set_current_host', 'set_current_user'):
        monkeypatch.setattr(
            api.sgtk.authentication.session_cache, name, getattr(cache, name),
        )

    errors = []

    def run_login():
        try:
            assert api.launch_interactive_login(
                host, 'Non-Qt test host', browser, **kwargs,
            ) is None
        except Exception as exc:
            errors.append(exc)

    if in_worker:
        thread = threading.Thread(target=run_login)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    else:
        run_login()

    assert errors == []
    launcher.assert_called_once()
    browser.assert_called_once_with(login_url)
    cache.cache_session_data.assert_called_once_with(*session_data)
    cache.set_current_host.assert_called_once_with(host=host)
    cache.set_current_user.assert_called_once_with(host=host, login='artist')
