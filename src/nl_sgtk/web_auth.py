"""Isolated web sessions with hashed app IDs and encrypted Flow credentials.

The web adapter owns cookies, CSRF, pending approval delivery, and jobs.
This module never selects a machine's default Toolkit user or script key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple, Union
from urllib.parse import urlsplit

from . import nl_sgtk as _api


class WebAuthenticationRequired(_api.TrackerAuthenticationError):
    """This browser must approve a personal Flow session again."""

    def __init__(self, reason: str) -> None:
        super().__init__("Flow sign-in is required for this browser.")
        self.reason = reason


class WebIdentityMismatchError(_api.TrackerAuthenticationError):
    """A request or approval belongs to a different HumanUser."""


@dataclass(frozen=True)
class WebIdentity:
    """Identity supplied by Flow, never by workflow or request metadata."""

    id: int
    login: str
    name: str

    def as_user(self) -> Dict[str, Any]:
        """Return the existing nl_sgtk HumanUser dictionary shape."""
        return {
            "type": "HumanUser", "id": self.id, "name": self.name,
            "_login": self.login.split("@")[0],
        }


class WebSession:
    """One request's client, intentionally not a serializable dataclass."""

    __slots__ = ("_identity", "_sg", "_browser_token", "_renewed")

    def __init__(
        self, identity: WebIdentity, sg: Any, browser_token: str,
        renewed: bool = False,
    ) -> None:
        self._identity = identity
        self._sg = sg
        self._browser_token = browser_token
        self._renewed = renewed

    def __repr__(self) -> str:
        return (f"WebSession(identity={self.identity!r}, "
                f"renewed={self.renewed!r})")

    @property
    def identity(self) -> WebIdentity:
        """Return the immutable identity verified by Flow."""
        return self._identity

    @property
    def sg(self) -> Any:
        """Return this request's private, caller-owned API client."""
        return self._sg

    @property
    def browser_token(self) -> str:
        """Return the opaque app cookie for an adapter's Set-Cookie header."""
        return self._browser_token

    @property
    def renewed(self) -> bool:
        """Indicate that the adapter must replace its browser cookie."""
        return self._renewed

    @property
    def user(self) -> Dict[str, Any]:
        """Return a fresh, non-secret user dictionary."""
        return self.identity.as_user()

    def close(self) -> None:
        """Close this request's caller-owned API connection."""
        self.sg.close()

    def __enter__(self) -> WebSession:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class _HideAuthDebug(logging.Filter):
    """SGTK versions can log raw session tokens at DEBUG during approval."""

    def __init__(self) -> None:
        super().__init__()
        self.thread_id = threading.get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        return (record.thread != self.thread_id
                or record.levelno > logging.DEBUG)


@contextmanager
def _private_approval_logs() -> Iterator[None]:
    logger = _api.sgtk.authentication.app_session_launcher.logger
    guard = _HideAuthDebug()
    logger.addFilter(guard)
    try:
        yield
    finally:
        logger.removeFilter(guard)


def _site(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("The Flow site must be an HTTPS origin.")
    # Accessing port also rejects malformed port specifications.
    parsed.port
    return "https://" + parsed.netloc.lower()


class WebAuthenticator:
    """Resolve opaque browser cookies to individual Flow connections.

    Each app owns an instance (or instances sharing its database). Only
    app-cookie hashes, Flow identity, encrypted credentials and timestamps
    are persisted. The encryption key must be kept outside the database.
    Raw Flow tokens are only decrypted while handling this user's request.
    """

    def __init__(
        self,
        base_url: str = _api.SHOTGRID_URL,
        *,
        encryption_key: bytes,
        product: str = "NL Web App",
        database: Union[str, Path] = ":memory:",
        session_lifetime: float = 28800,
        idle_timeout: float = 1800,
        http_proxy: Optional[str] = None,
    ) -> None:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise ImportError(
                "Web authentication requires the nl_sgtk[web] extra.") from exc
        self._cipher = Fernet(encryption_key)
        self.base_url = _site(base_url)
        if not isinstance(product, str) or not product.strip():
            raise ValueError("A nonempty application product is required.")
        for value in (session_lifetime, idle_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(
                    "Session timeouts must be finite and positive.")
        self.product = product
        self.session_lifetime = session_lifetime
        self.idle_timeout = idle_timeout
        self.http_proxy = http_proxy
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(database), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS nl_web_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    login TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    credential BLOB NOT NULL
                )
            """)

    def _key(self, browser_token: Optional[str]) -> Optional[str]:
        if (not isinstance(browser_token, str) or not browser_token
                or len(browser_token) > 4096):
            return None
        scope = "nl_sgtk.web.v1\0" + self.base_url + "\0" + self.product
        return hashlib.sha256(
            (scope + "\0" + browser_token).encode("utf-8"),
        ).hexdigest()

    def _record(self, key: Optional[str]) -> Optional[sqlite3.Row]:
        if key is None:
            return None
        with self._lock:
            return self._db.execute(
                "SELECT * FROM nl_web_sessions WHERE token_hash = ?", (key,),
            ).fetchone()

    def _delete(self, key: Optional[str]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM nl_web_sessions WHERE token_hash = ?", (key,),
            )

    def _credential(self, key: str, record: sqlite3.Row) -> str:
        from cryptography.fernet import InvalidToken

        try:
            payload = json.loads(self._cipher.decrypt(record["credential"]))
            if (payload["key"] != key or payload["site"] != self.base_url
                    or payload["product"] != self.product
                    or payload["user_id"] != record["user_id"]
                    or payload["login"] != record["login"]
                    or not isinstance(payload["token"], str)
                    or not payload["token"]):
                raise ValueError("Credential context mismatch")
            return payload["token"]
        except (InvalidToken, ValueError, KeyError, TypeError):
            self._delete(key)
            raise WebAuthenticationRequired("invalid") from None

    def _verify(
        self, token: str, login: str, expected_id: Optional[int] = None,
    ) -> Tuple[Any, WebIdentity]:
        client = _api.shotgun.Shotgun(
            self.base_url, session_token=token, connect=False,
            http_proxy=self.http_proxy,
        )
        try:
            timeout = client.config.timeout_secs
            attempts = client.config.max_rpc_attempts
            try:
                client.config.timeout_secs = 10
                client.config.max_rpc_attempts = 1
                user = client.find_one(
                    "HumanUser", [["login", "is", login]],
                    ["name", "login"],
                )
            finally:
                client.config.timeout_secs = timeout
                client.config.max_rpc_attempts = attempts
            if (not isinstance(user, dict) or user.get("type") != "HumanUser"
                    or type(user.get("id")) is not int or user["id"] <= 0
                    or not isinstance(user.get("login"), str)
                    or user["login"].casefold() != login.casefold()):
                raise _api.TrackerAuthenticationError(
                    "Flow did not verify the approved browser identity.")
            if expected_id is not None and user["id"] != expected_id:
                raise WebIdentityMismatchError(
                    "This Flow session belongs to another user.")
            return client, WebIdentity(
                user["id"], user["login"], user.get("name") or login,
            )
        except Exception:
            with suppress(Exception):
                client.close()
            raise

    def login(
        self,
        browser_open_callback: Callable[[str], Any],
        *,
        keep_waiting_callback: Optional[Callable[[], bool]] = None,
        expected_user_id: Optional[int] = None,
    ) -> WebSession:
        """Approve one new session in the requesting user's browser.

        Blocking and worker-safe with non-Qt callbacks. The web adapter must
        bind URL delivery and cancellation to that browser's pending login.
        No credentials are cached in Toolkit or selected from environment.
        """
        if not _api._interactive_allowed(True):
            raise _api.TrackerAuthenticationError(
                "Interactive login is disabled")
        if not callable(browser_open_callback):
            raise TypeError("A browser URL delivery callback is required.")
        keep_waiting = keep_waiting_callback
        if keep_waiting is None:
            keep_waiting = lambda: True
        if not callable(keep_waiting):
            raise TypeError("keep_waiting_callback must be callable.")

        def ensure_waiting() -> None:
            if not keep_waiting():
                raise WebAuthenticationRequired("cancelled")

        def offer_url(url: str) -> bool:
            ensure_waiting()
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname
                    or parsed.username or parsed.password):
                raise _api.TrackerAuthenticationError(
                    "Flow returned an invalid approval URL.")
            return browser_open_callback(url) is not False

        ensure_waiting()
        with _private_approval_logs():
            host, login, token, _ = (
                _api.sgtk.authentication.app_session_launcher.process(
                    self.base_url,
                    product=self.product,
                    http_proxy=self.http_proxy,
                    browser_open_callback=offer_url,
                    keep_waiting_callback=keep_waiting,
                )
            )
        ensure_waiting()
        if (_site(host) != self.base_url or not isinstance(token, str)
                or not token
                or not isinstance(login, str) or not login):
            raise _api.TrackerAuthenticationError(
                "Flow returned incomplete browser credentials.")
        try:
            client, identity = self._verify(token, login, expected_user_id)
        except _api.shotgun.AuthenticationFault as exc:
            raise _api.TrackerSessionExpiredError(
                "The newly approved Flow session was rejected.") from exc
        try:
            ensure_waiting()
            now = time.time()
            browser_token = secrets.token_urlsafe(32)
            key = self._key(browser_token)
            credential = self._cipher.encrypt(json.dumps({
                "key": key, "site": self.base_url, "product": self.product,
                "user_id": identity.id, "login": identity.login,
                "token": token,
            }).encode("utf-8"))
            with self._lock, self._db:
                self._db.execute(
                    "DELETE FROM nl_web_sessions "
                    "WHERE expires_at <= ?", (now,),
                )
                self._db.execute(
                    "INSERT INTO nl_web_sessions "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, identity.id, identity.login, identity.name, now,
                     now + self.session_lifetime, now, credential),
                )
            return WebSession(identity, client, browser_token, renewed=True)
        except Exception:
            with suppress(Exception):
                client.close()
            raise

    def authenticate(
        self,
        browser_token: Optional[str],
        *,
        browser_open_callback: Optional[Callable[[str], Any]] = None,
        keep_waiting_callback: Optional[Callable[[], bool]] = None,
        expected_user_id: Optional[int] = None,
    ) -> WebSession:
        """Resolve this browser's app cookie and verify Flow on every call.

        Missing/unknown/expired sessions require approval. When a callback
        is supplied, request approval exactly once. Transport errors do not
        cause reauthentication. An expected user can pin queued work to its
        original owner; a different account is rejected, never substituted.
        """
        key = self._key(browser_token)
        record = self._record(key)
        reason = "missing" if not browser_token else "unknown"
        if record:
            if (expected_user_id is not None
                    and record["user_id"] != expected_user_id):
                raise WebIdentityMismatchError(
                    "This browser is not the owner of the requested work.")
            # Renewal must keep the same identity. Intentional account
            # switching uses logout() followed by a separate login().
            expected_user_id = record["user_id"]
            now = time.time()
            if (record["expires_at"] <= now
                    or record["last_seen"] + self.idle_timeout <= now):
                self._delete(key)
                reason = "expired"
            else:
                try:
                    token = self._credential(key, record)
                    client, identity = self._verify(
                        token, record["login"], record["user_id"],
                    )
                except WebAuthenticationRequired as exc:
                    reason = exc.reason
                except _api.shotgun.AuthenticationFault:
                    self._delete(key)
                    reason = "expired"
                except WebIdentityMismatchError:
                    raise
                except _api.TrackerAuthenticationError:
                    self._delete(key)
                    reason = "invalid"
                else:
                    # A logout during verification must not resurrect it.
                    now = time.time()
                    try:
                        with self._lock, self._db:
                            changed = self._db.execute(
                                "UPDATE nl_web_sessions SET last_seen = ? "
                                "WHERE token_hash = ? AND user_id = ? "
                                "AND expires_at > ? AND last_seen > ?",
                                (now, key, identity.id, now,
                                 now - self.idle_timeout),
                            ).rowcount
                        if changed:
                            return WebSession(identity, client, browser_token)
                    except Exception:
                        with suppress(Exception):
                            client.close()
                        raise
                    client.close()
                    reason = "revoked"
        if browser_open_callback is None:
            raise WebAuthenticationRequired(reason)
        return self.login(
            browser_open_callback,
            keep_waiting_callback=keep_waiting_callback,
            expected_user_id=expected_user_id,
        )

    def logout(self, browser_token: Optional[str]) -> None:
        """Revoke this app session; the adapter must also clear its cookie."""
        self._delete(self._key(browser_token))

    def close(self) -> None:
        """Close the app's session store; clients stay caller-owned."""
        with self._lock:
            self._db.close()

    def __enter__(self) -> WebAuthenticator:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
