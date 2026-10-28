# Web authentication and multiple users

The reusable API is `nl_sgtk.WebAuthenticator`. It is a backend library,
not a web server or a replacement for a host application's authorization.
It uses the same SGTK browser approval protocol as desktop login, with a
separate session registry for each application. It never uses the backend
machine's saved artist or script API credentials.

## What is stored where

| Location | Stored data |
| --- | --- |
| Browser | A random app session ID in an HttpOnly, Secure cookie |
| Backend SQLite | SHA-256 app session ID hash, verified HumanUser ID/login/name, expiry/activity times, encrypted Flow credential |
| Backend secret storage | Fernet encryption key, separate from the database |
| Request worker memory | The decrypted Flow token and this request's own API client |

The browser never receives the Flow token. Only the application cookie is
sent automatically with requests. The backend hashes it to look up the
matching record, verifies that the encrypted credential belongs to that
record/site/application, then verifies it against Flow on every request.
There is no global current web user and no client-selected login name.

Hashing and encryption solve different problems. A stolen database cannot
be used to recover the random cookie or read the Flow credential without
the encryption key. A hash cannot be used to authenticate to Flow; its real
token must be available in the backend process while calling Flow.
Someone controlling that process, its service account, or both the database
and encryption key can access usable credentials. No Python library can
promise protection from arbitrary code running inside its own process.

## Setup

Install `nl_sgtk[web]`. Generate a Fernet key once with
`cryptography.fernet.Fernet.generate_key()` and put it in a backend secret
manager or a protected environment variable, never frontend code, Git,
the session SQLite file or logs. Keep its value stable after restarting
the app; changing it requires affected browsers to sign in again.

```python
import os
from nl_sgtk import WebAuthenticator

auth = WebAuthenticator(
    base_url="https://your-site.shotgrid.autodesk.com",
    product="ComfyUI",
    encryption_key=os.environ["NL_WEB_AUTH_KEY"].encode("ascii"),
    database="/protected/app-state/web_sessions.sqlite3",
    session_lifetime=8 * 3600,
    idle_timeout=30 * 60,
)
```

Use an app-owned local database directory with OS permissions for its
service account. Do not put this database on an SMB share. Multiple backend
processes can share the same local SQLite file and encryption key; sites
and product names isolate registry keys. `:memory:` is useful for tests,
but a restart or a different process cannot see those sessions.

The optional encryption dependency is imported only when constructing the
web authenticator. Desktop SGTK authentication stays compatible.

## Login and request lifecycle

1. A browser requests login using a same-origin, CSRF-protected endpoint.
   Issue a fresh random temporary cookie and store only its hash as the
   pending-login lookup key. Do not identify the browser by a username,
   ComfyUI `client_id`, URL parameter, or a predictable pending-request ID.
2. Start a bounded backend worker calling `auth.login(deliver_url, ...)`.
   Its callback delivers the Flow-provided approval URL only through that
   browser's pending-login channel. A browser polling the status must prove
   possession of that pending cookie. The process blocks and polls Flow;
   it does not listen for a localhost redirect or open the server's browser.
3. The frontend opens the approval tab. On approval the backend verifies
   the HumanUser, encrypts its Flow credential, and returns a `WebSession`
   to that worker. Close the returned API client after the login response.
4. Deliver `grant.browser_token` only as a cookie in a response to the
   browser that owns the pending login. Delete/consume the pending login;
   clear its temporary cookie. It must not be claimable by a different
   browser. An abandoned/cancelled worker must not attach late credentials.
5. Every protected request passes its own cookie to `auth.authenticate()`.
   Use only the returned `session.sg` and `session.user` for that request.
   Do not call `sgtk_login()` as a fallback, even when no cookie is present.
6. Missing/rejected sessions yield HTTP 401. That requesting frontend starts
   another approval flow. An explicit `browser_open_callback` can instead
   make `authenticate()` perform one blocking approval attempt. A network
   timeout is reported as a network error, without opening another tab.
7. Logout calls `auth.logout(cookie)` and expires the cookie. It does not
   revoke the upstream Flow session or sign the browser out of Flow itself.

Use a persistent cookie with a Max-Age no greater than the absolute session
lifetime. Session IDs rotate on new approvals. The cookie settings are:

```python
# Example aiohttp response; use your framework's equivalent.
response.set_cookie(
    "__Host-nl_session", grant.browser_token,
    httponly=True, secure=True, samesite="Strict", path="/",
    max_age=8 * 3600,
)
```

The `__Host-` prefix requires HTTPS, `Secure`, `Path=/`, and no Domain.
Use a distinct hostname per app; cookies do not isolate services by port.
Keep the frontend and API on the same origin. Use TLS, Origin checks and
CSRF protection on state-changing requests; do not enable wildcard CORS
with credentials. Never copy the cookie, Flow token or full WebSession into
localStorage, workflow JSON, filenames, PNG metadata, URLs, logs, or public
queue/history responses. `repr(WebSession)` hides its credential fields.
The result is not a dataclass and has no instance dictionary, so ordinary
JSON/dataclass conversion fails instead of serializing credentials.
Only return its explicitly non-secret `user` dictionary to the frontend.

For an asynchronous backend, run blocking Flow work in a worker:

```python
import asyncio
from nl_sgtk import WebAuthenticationRequired

async def protected_handler(request):
    cookie = request.cookies.get("__Host-nl_session")
    try:
        session = await asyncio.to_thread(auth.authenticate, cookie)
    except WebAuthenticationRequired:
        # Return 401 {"error": "authentication_required"} to this browser.
        ...
    else:
        try:
            # Pass session.sg explicitly to every nl_sgtk helper/provider.
            return session.user
        finally:
            session.close()
```

These are integration fragments; an application still needs its own
pending-login routes, CSRF, rate limiting, cookies, permissions and UI.
Use `keep_waiting_callback` to cancel abandoned requests and limit active
login workers. Callbacks run in the calling worker and must not access Qt.
The global `NL_SGTK_ALLOW_INTERACTIVE=0` policy prohibits new approvals,
but an existing valid web session can still be verified and used.

## ComfyUI: requests, websockets and jobs

ComfyUI's current user manager selects a profile using the client-provided
`comfy-user` header. Its websocket ID and prompt `client_id` are also
client-controlled. These values route/profile requests; they are not proof
of a Flow identity. Do not use them to select stored Flow credentials.

An integration in `ComfyUI-NL_Nodes` must:

- Authenticate protected HTTP requests using the cookie and attach the
  verified identity to request-local state. Protect the entire set of
  routes exposing private workflows, assets, history and user files, not
  only the sign-in endpoint. An authentication library alone cannot make
  ComfyUI's ordinary shared endpoints private.
- Authenticate websocket handshakes and bind each connection to the
  verified session. A supplied websocket ID must not replace or subscribe
  to a connection owned by another user. Recheck expiry/revocation for
  long-lived sockets and owner-check all event routing.
- At prompt submission, record the owner HumanUser ID from that request
  in trusted backend job state. Ignore any user ID, login or auth fields
  embedded in workflow/client JSON. A profile selected in the frontend
  cannot change the authenticated owner.
- At execution, use job-specific credentials and call
  `auth.authenticate(cookie, expected_user_id=job.owner_id)`. Keep any
  transient cookie in private in-memory job state, outside serializable
  workflow/queue data. Never use whichever user signed in most recently.
  Each request/job gets a separate API client; do not share one mutable
  Shotgun connection between parallel workers.
- If a job loses its original session, pause its Flow-dependent actions
  and ask the original user to reauthenticate. Bind the replacement only
  after checking the same HumanUser ID. Do not run the job as another user,
  fall back to a script account, or replay tracker writes automatically.
- Check ownership on history, downloads, cancellation and publication.
  Authentication identifies a user; it does not grant access to other
  users' jobs, even if they know a prompt ID.

For durable unattended jobs, a host adapter needs a private server-owned
job-to-session binding and explicit policy for delegated access. Do not
serialize browser bearer cookies to persistent queue/history storage.
That facility is outside this API; existing in-flight API calls cannot be
retroactively revoked by deleting a local session row.

Trusted custom nodes run inside the same backend process and can access
its memory and service account. This design isolates ordinary remote
users. Hostile/untrusted custom-node code requires separate processes or
containers and OS-level isolation.

This change adds the nl_sgtk API only. It does not install ComfyUI
middleware, change existing NL nodes, or deploy Crew Planning authentication.

## Verification and sources

Tests use fake Flow responses and never request a real login. They cover
per-user isolation, parallel approvals, no default-user/script fallback,
hash-only cookie storage, encrypted credentials, tampering/ciphertext swap
rejection, renewal, cancellation, logout races, per-app isolation, restart,
and the explicit owner check for queued work.

Sources checked 2026-10-28:

- [Pinned SGTK approval protocol](https://github.com/shotgunsoftware/tk-core/blob/v0.23.6/python/tank/authentication/app_session_launcher.py)
- [ShotGrid token authentication](https://developers.shotgridsoftware.com/python-api/reference.html#shotgun_api3.shotgun.Shotgun)
- [ComfyUI user manager](https://github.com/comfyanonymous/ComfyUI/blob/master/app/user_manager.py)
- [ComfyUI server and prompt routing](https://github.com/comfyanonymous/ComfyUI/blob/master/server.py)
- [OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [Fernet authenticated encryption](https://cryptography.io/en/latest/fernet/)
