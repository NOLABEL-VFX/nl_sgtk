import threading


def test_worker_never_launches_interactive_login(monkeypatch):
    from nl_sgtk import nl_sgtk as api

    calls = []
    monkeypatch.setattr(
        api.sgtk.authentication.app_session_launcher, 'process',
        lambda *args, **kwargs: calls.append('browser'),
    )
    errors = []

    def worker():
        try:
            api.launch_interactive_login()
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert calls == []
    assert len(errors) == 1
    assert 'Sign in from the main' in errors[0]
