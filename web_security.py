"""Request-security helpers with no tie to the Flask app object.

The CSRF token helpers and the login throttle, moved out of ``app.py``
unchanged. ``app.py`` re-exports every name here, so blueprints keep
importing them from ``app``. The pieces bound to the app object stay there:
the ``csrf_protect`` before-request hook and the Jinja ``csrf_token`` global.
This module must not import ``app`` -- see the ``sys.modules`` guard at the top
of ``app.py``.
"""

from __future__ import annotations

import os
import secrets
import sqlite3

from flask import request, session


CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
# Methods that must not change state, so they carry no token requirement. A
# handler that mutates on GET would sit outside this protection entirely.
CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
FORM_MIMETYPES = frozenset({"application/x-www-form-urlencoded", "multipart/form-data"})


def issue_csrf_token() -> str:
    """Return this session's CSRF token, minting one on first use.

    Registered as a Jinja global so templates can emit it, and called on login
    so the token rotates when the session's privilege level changes.
    """
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def submitted_csrf_token() -> str:
    """The token the caller presented, from the header or a form field."""
    header = (request.headers.get(CSRF_HEADER) or "").strip()
    if header:
        return header
    # Only touch request.form for form-encoded bodies. Reading it on a JSON
    # request is harmless but pointless, and the API is JSON everywhere except
    # the login form.
    if request.mimetype in FORM_MIMETYPES:
        return (request.form.get(CSRF_FIELD) or "").strip()
    return ""


def login_throttle_limits() -> tuple[int, int, int]:
    """(per-pair cap, per-address cap, window seconds), all env-overridable."""
    return (
        int(os.environ.get("LEGAL_ANALYZER_LOGIN_MAX_ATTEMPTS", "5")),
        int(os.environ.get("LEGAL_ANALYZER_LOGIN_MAX_ATTEMPTS_PER_ADDR", "20")),
        int(os.environ.get("LEGAL_ANALYZER_LOGIN_WINDOW_SECONDS", "900")),
    )


def record_failed_login(conn: sqlite3.Connection, username: str, remote_addr: str) -> None:
    conn.execute(
        "INSERT INTO login_attempts (username, remote_addr) VALUES (?, ?)",
        (username, remote_addr or ""),
    )


def clear_login_attempts(conn: sqlite3.Connection, username: str, remote_addr: str) -> None:
    """Drop a caller's failures once they prove they know the password."""
    conn.execute(
        "DELETE FROM login_attempts WHERE username = ? AND remote_addr = ?",
        (username, remote_addr or ""),
    )


def login_lockout_seconds(conn: sqlite3.Connection, username: str, remote_addr: str) -> int:
    """Seconds until this caller may try again; 0 when they are not locked out.

    Counted per (username, address) rather than per username alone, so someone
    who knows a username cannot lock its owner out from somewhere else. The
    per-address cap is the second half of that trade: it stops one address
    spraying one attempt each across many usernames and never tripping the
    pair limit.

    remote_addr is whatever the WSGI layer reports. Behind a reverse proxy that
    is the proxy unless it is configured to pass the client through, in which
    case every caller shares one bucket -- size the per-address cap for that.
    """
    pair_cap, addr_cap, window = login_throttle_limits()
    offset = f"-{window} seconds"
    conn.execute("DELETE FROM login_attempts WHERE attempted_at < datetime('now', ?)", (offset,))

    remaining = 0
    for sql, params, cap in (
        (
            "SELECT COUNT(*), MIN(attempted_at) FROM login_attempts "
            "WHERE username = ? AND remote_addr = ? AND attempted_at >= datetime('now', ?)",
            (username, remote_addr or "", offset),
            pair_cap,
        ),
        (
            "SELECT COUNT(*), MIN(attempted_at) FROM login_attempts "
            "WHERE remote_addr = ? AND attempted_at >= datetime('now', ?)",
            (remote_addr or "", offset),
            addr_cap,
        ),
    ):
        count, first_attempt = conn.execute(sql, params).fetchone()
        if count < cap or not first_attempt:
            continue
        elapsed = conn.execute(
            "SELECT CAST(strftime('%s','now') AS INTEGER) - CAST(strftime('%s', ?) AS INTEGER)",
            (first_attempt,),
        ).fetchone()[0]
        remaining = max(remaining, window - int(elapsed))

    return max(remaining, 0)
