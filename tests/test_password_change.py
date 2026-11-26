"""A signed-in user changes their own password (POST /api/account/password).

Before this, only `bootstrap_admin_from_env` ever wrote `users.password_hash`,
so an admin created with the old published placeholder kept it unless someone
ran a one-line hash update against the database. Every password here is
invented for the test.
"""

import os
import sqlite3
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory

from flask.testing import FlaskClient
from werkzeug.security import check_password_hash, generate_password_hash

import app as app_module

CURRENT = "synthetic-current-passphrase"
NEW = "synthetic-replacement-passphrase"
PLACEHOLDER = "change-this-local-password"
ENDPOINT = "/api/account/password"


class SignedInFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / "db").mkdir()
        self.db_path = Path(self.tmp.name) / "db" / "legal_documents.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        conn.executemany(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            [
                ("synthetic-admin", generate_password_hash(CURRENT), "admin"),
                ("synthetic-reviewer", generate_password_hash(CURRENT), "reviewer"),
                ("synthetic-viewer", generate_password_hash(CURRENT), "viewer"),
            ],
        )
        conn.commit()
        conn.close()

        self.original_db_path = app_module.DB_PATH
        app_module.DB_PATH = self.db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())
        self.addCleanup(self.restore)

    def restore(self) -> None:
        app_module.DB_PATH = self.original_db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())

    def signed_in(self, username: str = "synthetic-admin", password: str = CURRENT):
        client = app_module.app.test_client()
        response = client.post("/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return client

    def change(self, client, current=CURRENT, new=NEW, confirm=None):
        return client.post(
            ENDPOINT,
            json={"current_password": current, "new_password": new, "confirm_password": new if confirm is None else confirm},
        )

    def stored_hash(self, username: str = "synthetic-admin") -> str:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()[0]

    def audit_rows(self) -> list[tuple[str, str, str]]:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT result, metadata, actor_username FROM audit_log WHERE action = 'password_change' ORDER BY id"
            ).fetchall()

    def assertRefused(self, response, status: int, phrase: str) -> None:
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        self.assertIn(phrase, response.get_json()["error"])
        self.assertTrue(check_password_hash(self.stored_hash(), CURRENT), "a refused change must not touch the hash")


class PasswordChangeTests(SignedInFixture):
    def test_wrong_current_password_is_refused(self) -> None:
        client = self.signed_in()
        self.assertRefused(self.change(client, current="synthetic-wrong-passphrase"), 403, "Current password")

    def test_mismatched_confirmation_is_refused(self) -> None:
        client = self.signed_in()
        self.assertRefused(self.change(client, confirm=NEW + "-typo"), 400, "do not match")

    def test_too_short_is_refused(self) -> None:
        client = self.signed_in()
        short = "x" * (app_module.PASSWORD_MIN_LENGTH - 1)
        self.assertRefused(self.change(client, new=short), 400, f"at least {app_module.PASSWORD_MIN_LENGTH} characters")

    def test_the_minimum_length_itself_is_accepted(self) -> None:
        client = self.signed_in()
        exact = "y" * app_module.PASSWORD_MIN_LENGTH
        self.assertEqual(self.change(client, new=exact).status_code, 200)
        self.assertTrue(check_password_hash(self.stored_hash(), exact))

    def test_the_known_placeholder_is_refused(self) -> None:
        client = self.signed_in()
        for value in (PLACEHOLDER, PLACEHOLDER.upper(), "my-changeme-passphrase-2026"):
            with self.subTest(value=value):
                self.assertRefused(self.change(client, new=value), 400, "placeholder")

    def test_success_keeps_the_session_and_the_new_password_signs_in(self) -> None:
        client = self.signed_in()
        response = self.change(client)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json(), {"ok": True})

        # The session that made the change is still signed in.
        self.assertEqual(client.get("/api/me").status_code, 200)

        fresh = app_module.app.test_client()
        old = fresh.post("/login", json={"username": "synthetic-admin", "password": CURRENT})
        self.assertEqual(old.status_code, 401)
        self.signed_in(password=NEW)

    def test_any_role_changes_only_its_own_password(self) -> None:
        client = self.signed_in("synthetic-viewer")
        self.assertEqual(self.change(client).status_code, 200)
        self.assertTrue(check_password_hash(self.stored_hash("synthetic-viewer"), NEW))
        self.assertTrue(check_password_hash(self.stored_hash("synthetic-admin"), CURRENT))

    def test_anonymous_request_is_refused(self) -> None:
        response = self.change(app_module.app.test_client())
        self.assertEqual(response.status_code, 401)
        self.assertTrue(check_password_hash(self.stored_hash(), CURRENT))

    def test_request_without_csrf_token_is_refused(self) -> None:
        client = self.signed_in()
        with client.session_transaction() as sess:
            session_cookie_state = dict(sess)
        raw = FlaskClient(app_module.app)
        with raw.session_transaction() as sess:
            sess.update(session_cookie_state)
        response = raw.post(ENDPOINT, json={"current_password": CURRENT, "new_password": NEW, "confirm_password": NEW})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(check_password_hash(self.stored_hash(), CURRENT))

    def test_a_wrong_current_password_does_not_touch_login_throttling(self) -> None:
        client = self.signed_in()
        pair_cap, _addr_cap, _window = app_module.login_throttle_limits()
        for _ in range(pair_cap + 1):
            self.change(client, current="synthetic-wrong-passphrase")
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0], 0)
        self.signed_in()

    def test_non_string_fields_are_refused_without_a_500(self) -> None:
        client = self.signed_in()
        bodies = {
            "numeric current": {"current_password": 123, "new_password": NEW, "confirm_password": NEW},
            "numeric new": {"current_password": CURRENT, "new_password": 123456789012345678, "confirm_password": NEW},
            "list confirm": {"current_password": CURRENT, "new_password": NEW, "confirm_password": [NEW]},
            "null current": {"current_password": None, "new_password": NEW, "confirm_password": NEW},
            "missing confirm": {"current_password": CURRENT, "new_password": NEW},
            "array body": [CURRENT, NEW, NEW],
        }
        for label, body in bodies.items():
            with self.subTest(body=label):
                self.assertRefused(client.post(ENDPOINT, json=body), 400, "must be text")

    def test_other_sessions_are_signed_out_and_this_one_is_kept(self) -> None:
        changing = self.signed_in()
        other = self.signed_in()
        viewer = self.signed_in("synthetic-viewer")
        self.assertEqual(other.get("/api/me").status_code, 200)

        self.assertEqual(self.change(changing).status_code, 200)

        self.assertEqual(other.get("/api/me").status_code, 401)
        self.assertEqual(changing.get("/api/me").status_code, 200)
        self.assertEqual(viewer.get("/api/me").status_code, 200, "another user's session is not touched")
        # The kept session can still act, including changing the password again.
        self.assertEqual(self.change(changing, current=NEW, new=NEW + "-again").status_code, 200)
        self.assertEqual(changing.get("/api/me").status_code, 200)
        # A session signed in after the change is current.
        self.assertEqual(self.signed_in(password=NEW + "-again").get("/api/me").status_code, 200)

    def test_wrong_current_passwords_are_throttled_per_session(self) -> None:
        client = self.signed_in()
        pair_cap, _addr_cap, window = app_module.login_throttle_limits()
        for _ in range(pair_cap):
            self.assertEqual(self.change(client, current="synthetic-wrong-passphrase").status_code, 403)

        throttled = self.change(client)
        self.assertEqual(throttled.status_code, 429, throttled.get_data(as_text=True))
        self.assertTrue(0 < int(throttled.headers["Retry-After"]) <= window)
        self.assertTrue(check_password_hash(self.stored_hash(), CURRENT), "the right password is not checked while throttled")

        # Another session of the same user is not throttled by these guesses, nor is another user.
        self.assertEqual(self.change(self.signed_in("synthetic-viewer")).status_code, 200)
        self.assertEqual(self.change(self.signed_in()).status_code, 200)
        # Sign-in itself is untouched by the change form's counter.
        self.signed_in(password=NEW)

    def test_an_attacker_session_cannot_stop_the_owner_evicting_it(self) -> None:
        attacker = self.signed_in()
        owner = self.signed_in()
        pair_cap, _addr_cap, _window = app_module.login_throttle_limits()
        for _ in range(pair_cap):
            self.change(attacker, current="synthetic-wrong-passphrase")
        self.assertEqual(self.change(attacker).status_code, 429)

        self.assertEqual(self.change(owner).status_code, 200)
        self.assertEqual(attacker.get("/api/me").status_code, 401)
        self.assertEqual(owner.get("/api/me").status_code, 200)

    def test_a_session_without_a_nonce_does_not_escape_the_throttle(self) -> None:
        legacy = self.signed_in()
        with legacy.session_transaction() as sess:
            sess.pop("session_nonce", None)
        pair_cap, _addr_cap, _window = app_module.login_throttle_limits()
        for _ in range(pair_cap):
            self.assertEqual(self.change(legacy, current="synthetic-wrong-passphrase").status_code, 403)
        self.assertEqual(self.change(legacy).status_code, 429)
        self.assertTrue(check_password_hash(self.stored_hash(), CURRENT))

    def test_the_reset_command_signs_out_existing_sessions(self) -> None:
        client = self.signed_in()
        command = app_module.ADMIN_PASSWORD_RESET_COMMAND.replace("python3", sys.executable, 1)
        env = dict(os.environ, LEGAL_ANALYZER_ADMIN_USERNAME="synthetic-admin")
        for _ in range(2):
            result = subprocess.run(command, shell=True, cwd=self.tmp.name, env=env, input=NEW + "\n",
                                    capture_output=True, text=True, timeout=60, start_new_session=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(client.get("/api/me").status_code, 401)
            client = self.signed_in(password=NEW)
        self.assertEqual(client.get("/api/me").status_code, 200)

    def test_a_successful_change_clears_the_counter(self) -> None:
        client = self.signed_in()
        pair_cap, _addr_cap, _window = app_module.login_throttle_limits()
        for _ in range(pair_cap - 1):
            self.change(client, current="synthetic-wrong-passphrase")
        self.assertEqual(self.change(client).status_code, 200)
        for _ in range(pair_cap - 1):
            self.assertEqual(self.change(client, current="synthetic-wrong-passphrase").status_code, 403)
        self.assertEqual(self.change(client, current=NEW, new=NEW + "-again").status_code, 200)

    def test_the_audit_log_never_records_a_password(self) -> None:
        client = self.signed_in()
        self.change(client, current="synthetic-wrong-passphrase")
        self.change(client, new=PLACEHOLDER)
        self.change(client)
        rows = self.audit_rows()
        self.assertEqual([result for result, _metadata, _actor in rows], ["failed", "blocked", "success"])
        self.assertEqual({actor for _result, _metadata, actor in rows}, {"synthetic-admin"})
        for _result, metadata, _actor in rows:
            for secret in (CURRENT, NEW, PLACEHOLDER, "synthetic-wrong-passphrase"):
                self.assertNotIn(secret, metadata)


class SettingsOffersThePasswordChangeTests(SignedInFixture):
    """Every role reaches the form on the rendered /settings page."""

    def form_on_settings(self, username: str) -> tuple[dict, dict[str, dict], str]:
        client = self.signed_in(username)
        response = client.get("/settings")
        self.assertEqual(response.status_code, 200, f"{username} could not open /settings")
        html = response.get_data(as_text=True)
        parser = FormParser()
        parser.feed(html)
        forms = [form for form in parser.forms if form.get("data-endpoint") == ENDPOINT]
        self.assertEqual(len(forms), 1, f"/settings for {username} renders {len(forms)} password forms")
        return forms[0], parser.inputs, html

    def test_every_role_gets_the_rendered_form(self) -> None:
        for username in ("synthetic-admin", "synthetic-reviewer", "synthetic-viewer"):
            with self.subTest(username=username):
                _form, inputs, _html = self.form_on_settings(username)
                self.assertEqual(set(inputs), {"current_password", "new_password", "confirm_password"})
                self.assertEqual(inputs["current_password"].get("autocomplete"), "current-password")
                for name in ("new_password", "confirm_password"):
                    self.assertEqual(inputs[name].get("type"), "password")
                    self.assertEqual(inputs[name].get("autocomplete"), "new-password")
                    self.assertEqual(inputs[name].get("minlength"), str(app_module.PASSWORD_MIN_LENGTH))

    def test_the_admin_facts_stay_admin_only(self) -> None:
        _form, _inputs, admin_html = self.form_on_settings("synthetic-admin")
        self.assertIn("Access Model", admin_html)
        for username in ("synthetic-reviewer", "synthetic-viewer"):
            with self.subTest(username=username):
                _form, _inputs, html = self.form_on_settings(username)
                self.assertNotIn("Access Model", html)
                self.assertNotIn("Current Format Support", html)


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict] = []
        self.inputs: dict[str, dict] = {}
        self._in_password_form = False

    def handle_starttag(self, tag, attrs) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self.forms.append(attributes)
            self._in_password_form = attributes.get("data-endpoint") == ENDPOINT
        elif tag == "input" and self._in_password_form and attributes.get("name"):
            self.inputs[attributes["name"]] = attributes

    def handle_endtag(self, tag) -> None:
        if tag == "form":
            self._in_password_form = False


class StartupPointsAtSettingsTests(unittest.TestCase):
    def test_the_placeholder_refusal_points_at_settings_and_keeps_the_fallback(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"LEGAL_ANALYZER_ADMIN_PASSWORD": PLACEHOLDER}, clear=True):
            with self.assertRaises(SystemExit) as raised:
                app_module.check_admin_password_setting()
        message = str(raised.exception)
        self.assertIn("Settings", message)
        self.assertNotIn("no in-app password change", message)
        self.assertIn(app_module.ADMIN_PASSWORD_RESET_COMMAND, message)
        self.assertIn("forgotten", message)


if __name__ == "__main__":
    unittest.main()
