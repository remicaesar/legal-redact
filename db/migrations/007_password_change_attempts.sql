-- Wrong current passwords sent to POST /api/account/password, backing that
-- form's per-session throttle.
--
-- Kept apart from login_attempts on purpose: the caller already holds a
-- session, and counting a mistyped form there would lock the owner out of
-- /login. Keyed by user and by the random nonce each session gets at sign-in,
-- not by address: an attacker's wrong guesses from a stolen session then lock
-- only that session, never the owner's, who can still change the password and
-- so sign the attacker out. A session signed in before the nonce existed counts
-- under the empty nonce, one bucket shared by all such sessions of the user.
-- Same limits and window as the login throttle
-- (login_throttle_limits); rows are deleted once they age out of the window or
-- the user changes the password.

CREATE TABLE IF NOT EXISTS password_change_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_nonce TEXT NOT NULL DEFAULT '',
    attempted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_password_change_attempts_session ON password_change_attempts(user_id, session_nonce, attempted_at);
