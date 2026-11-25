"""Startup and hardening configuration guards.

These cover the things that are set once at import time and are therefore easy
to regress silently: the session secret (which used to have a published default
that made an admin session forgeable), the session cookie flags, the upload
size cap, and the single shared HTML-escaping helper the templates must use
instead of five local copies that did not escape quotes.
"""

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import tests.env_setup  # noqa: F401  -- sets LEGAL_ANALYZER_SECRET_KEY before app is imported
import app as app_module

PROJECT_DIR = Path(__file__).resolve().parents[1]

# What .env.example used to set the session key to. Following that file's own
# "copy to .env" instruction produced a key anyone could read on GitHub.
OLD_ENV_EXAMPLE_SECRET_KEY = 'generate-with: python3 -c "import secrets; print(secrets.token_hex(32))"'


def import_app_with_secret_key(value: str, **extra_env: str) -> subprocess.CompletedProcess:
    """Import app in a fresh interpreter with only this key; a local .env cannot interfere."""
    code = (
        "import sys, types\n"
        "sys.modules['dotenv'] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)\n"
        "import app\n"
    )
    env = {key: val for key, val in os.environ.items() if key != "LEGAL_ANALYZER_SECRET_KEY"}
    env.pop("LEGAL_ANALYZER_ADMIN_PASSWORD", None)
    env["LEGAL_ANALYZER_SECRET_KEY"] = value
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT_DIR, env=env, capture_output=True, text=True, timeout=120
    )
TEMPLATES_WITH_ESCAPE_HTML = ["index.html", "matters.html", "matter.html", "studio.html", "audit.html"]


class SecretKeyTests(unittest.TestCase):
    def test_missing_secret_key_refuses_to_start(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as raised:
                app_module.resolve_secret_key()

        message = str(raised.exception)
        self.assertIn("LEGAL_ANALYZER_SECRET_KEY", message)
        self.assertIn("secrets.token_hex(32)", message)

    def test_blank_secret_key_refuses_to_start(self):
        with mock.patch.dict(os.environ, {"LEGAL_ANALYZER_SECRET_KEY": "   "}, clear=True):
            with self.assertRaises(SystemExit):
                app_module.resolve_secret_key()

    def test_configured_secret_key_is_used(self):
        key = "9f2c" * 16  # 64 characters, the length secrets.token_hex(32) prints
        with mock.patch.dict(os.environ, {"LEGAL_ANALYZER_SECRET_KEY": key}, clear=True):
            self.assertEqual(app_module.resolve_secret_key(), key)

    def test_a_key_shorter_than_32_characters_refuses_to_start(self):
        for key in ("abc123", "x" * 31):
            with self.subTest(length=len(key)):
                with mock.patch.dict(os.environ, {"LEGAL_ANALYZER_SECRET_KEY": key}, clear=True):
                    with self.assertRaises(SystemExit) as raised:
                        app_module.resolve_secret_key()
                self.assertIn("at least 32 characters", str(raised.exception))
                self.assertIn("secrets.token_hex(32)", str(raised.exception))

    def test_a_known_placeholder_refuses_to_start(self):
        for key in (OLD_ENV_EXAMPLE_SECRET_KEY, "change-me-to-a-long-random-secret-key-value"):
            with self.subTest(key=key):
                with mock.patch.dict(os.environ, {"LEGAL_ANALYZER_SECRET_KEY": key}, clear=True):
                    with self.assertRaises(SystemExit) as raised:
                        app_module.resolve_secret_key()
                self.assertIn("placeholder", str(raised.exception))

    def test_booting_with_the_old_env_example_value_exits(self):
        """The value .env.example shipped until v1.0: copying the file booted an app
        whose session-signing key was published on GitHub."""
        result = import_app_with_secret_key(OLD_ENV_EXAMPLE_SECRET_KEY)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("placeholder", result.stderr)
        self.assertIn("secrets.token_hex(32)", result.stderr)

    def test_env_example_ships_no_secret_key_value(self):
        from dotenv import dotenv_values

        example = PROJECT_DIR / ".env.example"
        self.assertEqual(dotenv_values(example).get("LEGAL_ANALYZER_SECRET_KEY"), "")
        # And copying the file as-is still does not boot.
        self.assertNotEqual(import_app_with_secret_key("").returncode, 0)

    def test_importing_app_without_a_secret_key_exits_nonzero(self):
        # The guard has to fire at import, not at first request: a process that
        # got as far as serving traffic has already signed cookies. dotenv is
        # stubbed out so a developer's local .env cannot satisfy the check.
        code = (
            "import sys, types\n"
            "sys.modules['dotenv'] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)\n"
            "import app\n"
        )
        env = {key: value for key, value in os.environ.items() if key != "LEGAL_ANALYZER_SECRET_KEY"}
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("LEGAL_ANALYZER_SECRET_KEY", result.stderr)
        self.assertIn("secrets.token_hex(32)", result.stderr)


class AdminPasswordTests(unittest.TestCase):
    """The first admin's password: .env.example and the README once handed everyone the same one."""

    OLD_PLACEHOLDER = "change-this-local-password"

    def test_a_placeholder_admin_password_refuses_to_start(self):
        result = import_app_with_secret_key(
            "admin-password-test-key-0123456789abcdef", LEGAL_ANALYZER_ADMIN_PASSWORD=self.OLD_PLACEHOLDER
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("LEGAL_ANALYZER_ADMIN_PASSWORD", result.stderr)
        self.assertIn("placeholder", result.stderr)

    def test_placeholders_are_refused_and_real_passwords_are_not(self):
        for value in (self.OLD_PLACEHOLDER, "Change-Me-Please", "replace-me"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"LEGAL_ANALYZER_ADMIN_PASSWORD": value}, clear=True):
                    with self.assertRaises(SystemExit):
                        app_module.check_admin_password_setting()
        for value in ("", "a synthetic chosen passphrase"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"LEGAL_ANALYZER_ADMIN_PASSWORD": value}, clear=True):
                    app_module.check_admin_password_setting()

    def run_reset_command(self, folder: Path, username: str) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items() if key != "LEGAL_ANALYZER_ADMIN_USERNAME"}
        if username:
            env["LEGAL_ANALYZER_ADMIN_USERNAME"] = username
        command = app_module.ADMIN_PASSWORD_RESET_COMMAND.replace("python3", sys.executable, 1)
        return subprocess.run(command, shell=True, cwd=folder, env=env, input="a synthetic new passphrase\n",
                              capture_output=True, text=True, timeout=60, start_new_session=True)

    def test_the_reset_command_sets_the_configured_admin_and_refuses_an_unknown_one(self):
        import sqlite3
        import tempfile
        from werkzeug.security import check_password_hash

        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "db").mkdir()
            database = Path(folder) / "db" / "legal_documents.db"
            with sqlite3.connect(database) as conn:
                conn.execute("CREATE TABLE users (username TEXT, password_hash TEXT)")
                conn.execute("INSERT INTO users VALUES ('synthetic-admin', 'old-hash')")
            unknown = self.run_reset_command(Path(folder), "")
            self.assertNotEqual(unknown.returncode, 0, unknown.stdout)
            self.assertIn("No account named admin", unknown.stderr)
            changed = self.run_reset_command(Path(folder), "synthetic-admin")
            self.assertEqual(changed.returncode, 0, changed.stderr)
            self.assertIn("1 account updated", changed.stdout)
            with sqlite3.connect(database) as conn:
                stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
            self.assertTrue(check_password_hash(stored, "a synthetic new passphrase"))

    def test_env_example_ships_no_admin_password(self):
        from dotenv import dotenv_values

        self.assertEqual(dotenv_values(PROJECT_DIR / ".env.example").get("LEGAL_ANALYZER_ADMIN_PASSWORD"), "")


class StartupOutputTests(unittest.TestCase):
    def test_starting_the_app_prints_no_pymupdf_deprecation(self):
        # PyMuPDF prints "The `fitz` API is deprecated" on every `import fitz`;
        # a first run should not open with a warning about the app's own code.
        result = import_app_with_secret_key("startup-output-test-key-0123456789abcdef")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fitz", result.stdout + result.stderr)


class LocalServerPortTests(unittest.TestCase):
    """`python3 app.py` port: on macOS, AirPlay Receiver also listens on 5000."""

    def test_default_is_5000(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(app_module.local_server_port(), 5000)

    def test_legal_analyzer_port_sets_it(self):
        with mock.patch.dict(os.environ, {"LEGAL_ANALYZER_PORT": " 5056 "}, clear=True):
            self.assertEqual(app_module.local_server_port(), 5056)

    def test_an_unusable_port_refuses_to_start(self):
        for value in ("http", "0", "65536", "-1", "50.5"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"LEGAL_ANALYZER_PORT": value}, clear=True):
                    with self.assertRaises(SystemExit) as raised:
                        app_module.local_server_port()
                self.assertIn("LEGAL_ANALYZER_PORT", str(raised.exception))


class HardeningConfigTests(unittest.TestCase):
    def test_session_cookies_are_httponly_and_samesite_strict(self):
        self.assertTrue(app_module.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app_module.app.config["SESSION_COOKIE_SAMESITE"], "Strict")

    def test_request_bodies_are_capped_at_the_extractor_ceiling(self):
        # Werkzeug rejects past MAX_CONTENT_LENGTH and blueprints/documents.py
        # audits against MAX_UPLOAD_BYTES; both must be the number the ZIP
        # extractor is willing to read back, or an upload can claim more disk
        # than extraction will ever process.
        from legal_analyzer.extraction import ZIP_MAX_TOTAL_BYTES

        self.assertEqual(app_module.app.config["MAX_CONTENT_LENGTH"], app_module.MAX_UPLOAD_BYTES)
        self.assertEqual(app_module.MAX_UPLOAD_BYTES, ZIP_MAX_TOTAL_BYTES)

    def test_logout_rejects_get(self):
        rules = [rule for rule in app_module.app.url_map.iter_rules() if str(rule) == "/logout"]
        self.assertEqual(len(rules), 1)
        self.assertNotIn("GET", rules[0].methods)
        self.assertIn("POST", rules[0].methods)


class SharedEscapeHelperTests(unittest.TestCase):
    escape_js = PROJECT_DIR / "static" / "escape.js"

    def test_templates_include_the_shared_helper_and_define_no_local_copy(self):
        for name in TEMPLATES_WITH_ESCAPE_HTML:
            with self.subTest(template=name):
                source = (PROJECT_DIR / "templates" / name).read_text(encoding="utf-8")
                self.assertIn("filename='escape.js'", source)
                self.assertNotIn("function escapeHtml", source)
                self.assertNotIn("function escapeAttr", source)

    def test_escape_js_escapes_quotes_as_well_as_angle_brackets(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; escape.js behaviour not exercised")
        # The helper is interpolated into double-quoted attributes fed by stored
        # filenames and reviewer replacement text, so quotes matter as much as
        # angle brackets. The old text-node implementation escaped neither.
        script = (
            f"{self.escape_js.read_text(encoding='utf-8')}\n"
            "const value = `x\" onerror='alert(1)' <b>&`;\n"
            "console.log(escapeHtml(value));\n"
            "console.log(escapeAttr(value));\n"
        )
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

        escaped, attr_escaped = result.stdout.strip().splitlines()
        self.assertEqual(escaped, "x&quot; onerror=&#39;alert(1)&#39; &lt;b&gt;&amp;")
        self.assertEqual(attr_escaped, escaped)
        self.assertNotIn('"', escaped)
        self.assertNotIn("'", escaped)


if __name__ == "__main__":
    unittest.main()
