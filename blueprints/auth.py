"""Login, logout, and session identity routes."""

from __future__ import annotations

import secrets

from flask import Blueprint, g, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app import (
    audit_event,
    bump_session_version,
    clear_login_attempts,
    clear_password_change_attempts,
    get_db,
    issue_csrf_token,
    login_lockout_seconds,
    new_password_problem,
    password_change_lockout_seconds,
    record_failed_login,
    record_failed_password_change,
    require_roles,
    session_version,
)

auth_bp = Blueprint("auth", __name__)

LOGIN_PAGE = """
    {%- from "partials/brand.html" import brand_icon, wordmark, favicon_link -%}
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Legal Redact — Sign in</title>
        {{ favicon_link() }}
        <link rel="stylesheet" href="/static/workstation.css">
        <style>
            /* The sign-in sheet in the product's design language: a white
               sheet on the warm canvas, the lockup set large, the secondary
               ink for everything that is not the task. */
            * { box-sizing: border-box; }
            body {
                margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
                background: var(--color-canvas); color: var(--color-text);
                font-family: var(--font-ui); -webkit-font-smoothing: antialiased;
                --focus-ring: 0 0 0 3px rgba(126, 52, 61, 0.26);
            }
            html body .card {
                width: 100%; max-width: 400px; margin: 24px; padding: 36px 36px 28px;
                background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-sm);
                box-shadow: var(--shadow-sheet);
            }
            .brand { display: flex; align-items: center; gap: 12px; margin-bottom: 28px; }
            .brand .name { margin: 0; font-size: 24px; font-weight: 500; line-height: 1; }
            .brand .sub { font-size: var(--text-sm); color: var(--color-text-secondary); margin-top: 6px; }
            label { display: block; font-size: var(--text-sm); font-weight: 600; color: var(--color-text-secondary); margin-bottom: 7px; }
            input {
                display: block; width: 100%; margin-bottom: 18px;
            }
            button {
                width: 100%; min-height: 44px; border-color: var(--color-accent); background: var(--color-accent); color: #fff; margin-top: 6px;
            }
            button:hover { border-color: var(--color-accent-hover); background: var(--color-accent-hover); color: #fff; }
            .error {
                background: var(--color-danger-soft); color: var(--color-danger); border: 1px solid #e3b5b2;
                border-radius: var(--radius-sm); padding: 10px 12px; font-size: var(--text-sm); margin-bottom: 18px;
            }
            .foot {
                display: flex; align-items: baseline; justify-content: center; gap: 7px;
                margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--color-border);
                font-size: 12px; color: var(--color-text-secondary);
            }
            .foot::before {
                width: 6px; height: 6px; border-radius: 50%; background: var(--color-success); content: "";
                transform: translateY(-1px);
            }
            @media (max-width: 480px) { html body .card { margin: 16px; padding: 28px 22px 22px; } }
        </style>
    </head>
    <body>
        <div class="card">
            <div class="brand">
                {{ brand_icon() }}
                <div>
                    <h1 class="name">{{ wordmark() }}</h1>
                    <div class="sub">Local privacy workstation</div>
                </div>
            </div>
            {% if throttled %}
            <div class="error">Too many failed sign-in attempts. Wait a few minutes and try again.</div>
            {% elif error %}<div class="error">Invalid username or password.</div>{% endif %}
            <form method="post">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <label for="username">Username</label>
                <input id="username" name="username" autocomplete="username" autofocus>
                <label for="password">Password</label>
                <input id="password" name="password" type="password" autocomplete="current-password">
                <button class="primary" type="submit">Sign in</button>
            </form>
            <div class="foot">Local only · data stays on this device</div>
        </div>
    </body>
    </html>
    """


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        # Minting the token here is what lets the form below post it back; a
        # POST that arrives without a prior GET has no session token to match.
        issue_csrf_token()
        return render_template_string(LOGIN_PAGE, error=False, throttled=False)

    payload = request.get_json(silent=True) or request.form
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    remote_addr = request.remote_addr or ""
    conn = get_db()

    # Throttle before touching the password hash: check_password_hash is
    # deliberately slow, so an unthrottled endpoint is both a guessing oracle
    # and a way to spend the server's CPU.
    locked_for = login_lockout_seconds(conn, username, remote_addr)
    if locked_for > 0:
        audit_event(conn, "login", result="throttled", metadata={"username": username})
        conn.commit()
        conn.close()
        retry_after = str(locked_for)
        if request.is_json:
            response = jsonify({"error": "Too many failed sign-in attempts. Try again later."})
            response.headers["Retry-After"] = retry_after
            return response, 429
        return render_template_string(LOGIN_PAGE, error=True, throttled=True), 429, {"Retry-After": retry_after}

    user = conn.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        record_failed_login(conn, username, remote_addr)
        audit_event(conn, "login", result="failed", metadata={"username": username})
        conn.commit()
        conn.close()
        if request.is_json:
            return jsonify({"error": "Invalid credentials"}), 401
        return render_template_string(LOGIN_PAGE, error=True), 401

    clear_login_attempts(conn, username, remote_addr)
    session.clear()
    # session.clear() drops the pre-login CSRF token; mint a fresh one rather
    # than carrying the anonymous session's token into an authenticated one.
    issue_csrf_token()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]
    session["session_version"] = session_version(conn, user["id"])
    session["session_nonce"] = secrets.token_urlsafe(16)
    g.current_user = {"id": user["id"], "username": user["username"], "role": user["role"], "active": user["active"]}
    conn.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (user["id"],))
    audit_event(conn, "login", result="success")
    conn.commit()
    conn.close()
    if request.is_json:
        return jsonify({"ok": True, "user": {"username": user["username"], "role": user["role"]}})
    return redirect(url_for("documents.index"))


@auth_bp.route("/logout", methods=["POST"])
@require_roles("viewer", "reviewer", "admin")
def logout():
    # POST only: a GET logout can be triggered by any third-party page that
    # gets the browser to fetch the URL (an <img> tag is enough). Every UI call
    # site already posts.
    conn = get_db()
    audit_event(conn, "logout")
    conn.commit()
    conn.close()
    session.clear()
    return jsonify({"ok": True})


@auth_bp.route("/api/me")
def api_me():
    user = getattr(g, "current_user", None)
    if not user:
        return jsonify({"authenticated": False}), 401
    return jsonify({"authenticated": True, "user": {"username": user["username"], "role": user["role"]}})


@auth_bp.route("/api/account/password", methods=["POST"])
@require_roles("viewer", "reviewer", "admin")
def change_own_password():
    """Change the signed-in user's own password; there is no way to name another account.

    A wrong current password is counted per session (user and the nonce minted
    at sign-in) in password_change_attempts, not in login_attempts: counting it
    as a failed login would let a mistyped form lock the owner out of signing
    in, and counting it per user would let a stolen session lock the owner out
    of the change that signs it out. Once the login throttle's per-pair cap is
    reached the session gets 429 without the hash being checked. A successful change clears that counter and
    signs out every other session of this user; the session that made the
    change is moved to the new version and stays signed in.
    """
    payload = request.get_json(silent=True)
    fields = ("current_password", "new_password", "confirm_password")
    values = [payload.get(field) for field in fields] if isinstance(payload, dict) else []
    if len(values) != len(fields) or not all(isinstance(value, str) for value in values):
        return jsonify({"error": "The current password and both new passwords must be text."}), 400
    current_password, new_password, confirm_password = values
    user_id = g.current_user["id"]
    session_nonce = session.get("session_nonce", "")
    conn = get_db()

    locked_for = password_change_lockout_seconds(conn, user_id, session_nonce)
    if locked_for > 0:
        audit_event(conn, "password_change", result="throttled")
        conn.commit()
        conn.close()
        response = jsonify({"error": "Too many wrong current passwords. Try again later."})
        response.headers["Retry-After"] = str(locked_for)
        return response, 429

    row = conn.execute("SELECT password_hash FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], current_password):
        record_failed_password_change(conn, user_id, session_nonce)
        audit_event(conn, "password_change", result="failed", metadata={"reason": "current_password"})
        conn.commit()
        conn.close()
        return jsonify({"error": "Current password is incorrect."}), 403

    problem = new_password_problem(new_password, confirm_password)
    if problem:
        audit_event(conn, "password_change", result="blocked", metadata={"reason": problem})
        conn.commit()
        conn.close()
        return jsonify({"error": problem}), 400

    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_password), user_id))
    clear_password_change_attempts(conn, user_id)
    session["session_version"] = bump_session_version(conn, user_id)
    audit_event(conn, "password_change", result="success")
    conn.commit()
    conn.close()
    return jsonify({"ok": True})
