"""/welcome is the landing page built from landing/, and it keeps its promises.

The page is React + visx, prebuilt: `cd landing && npm run build` writes the
prerendered markup into templates/landing.html and the script and styles into
static/landing/. These tests read what the route actually serves, so they hold
without Node installed.

What they pin, because each is the kind of thing that ships unnoticed:

* The page is the new one, its content is in the HTML (it reads without
  JavaScript), and the old particles/orbs page is gone.
* It makes no request to any other host: every link, script, stylesheet and
  font it loads is this app's, and the script opens no connection.
* Every link and button goes somewhere real in the app.
* It quotes no accuracy figure. /welcome is the one page anyone can read
  without logging in; the gold set behind the accuracy audit is synthetic
  documents written by the same people who wrote the rules, so recall 1.0
  against it is a regression guard, not a property of the detector, and a bare
  number there travels without that qualifier.
* The figures it does quote are derived, not typed: the release gate's condition
  count and labels, and the demo's findings, placeholders and gate states, which
  landing/scripts/gen_demo.py produces by running this app. A detection change
  that moves them fails here until the demo is regenerated and rebuilt.
* The sample petition's check-digit-valid ID numbers never reach the page: the
  demo shows them masked.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import unittest
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory

import tests.env_setup  # noqa: F401  -- must precede "import app"

import app as app_module
from legal_analyzer import privacy
from legal_analyzer.extraction import extract_text

LANDING = Path("landing")
DEMO_JSON = LANDING / "src" / "demo.json"
PLAN_CASES_JSON = LANDING / "scripts" / "plan_cases.json"
BUNDLE = Path("static/landing")
SAMPLE = Path("samples/documents/turkish_petition.docx")
REBUILD = (
    "regenerate and rebuild: .venv/bin/python landing/scripts/gen_demo.py && "
    "cd landing && npm ci && npm run build"
)

# Words that mark a figure as an accuracy measurement rather than a design property.
ACCURACY_WORDS = re.compile(r"\b(recall|precision|f1)\b", re.IGNORECASE)
PERCENTAGE = re.compile(r"\d\s*%")
# Browser APIs that open a connection from script.
REQUEST_APIS = re.compile(r"\bfetch\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts|new Worker\b")
# URL strings the React/visx bundle carries that are not requests: XML namespaces,
# the error-decoder link React only puts in thrown error messages, and a licence
# comment. A new one is a failure, so it gets looked at rather than shipped.
KNOWN_NON_REQUEST_URLS = {
    "http://www.w3.org/1998/Math/MathML",
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/XML/1998/namespace",
    "https://reactjs.org/docs/error-decoder.html?invariant=",
    "http://jedwatson.github.io/classnames",
}
URL = re.compile(r"(?:https?:)?//[\w.-]+\.[a-z]{2,}[^\s\"'`)<>]*", re.IGNORECASE)
CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")


class _Links(HTMLParser):
    """Every URL-bearing attribute, and every id, in a page."""

    URL_ATTRS = {"href", "src", "srcset", "action", "formaction", "poster", "data", "xlink:href"}

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[tuple[str, str, str]] = []  # (tag, attr, value)
        self.ids: set[str] = set()
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        for name, value in attrs.items():
            if name in self.URL_ATTRS and value is not None:
                self.urls.append((tag, name, value))
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.stylesheets.append(attrs["href"])
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])


def parse(html: str) -> _Links:
    links = _Links()
    links.feed(html)
    return links


def page_text(html: str) -> str:
    """What a reader sees: the markup without tags, scripts or styles."""
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def failing_gate() -> dict:
    """The release gate with every one of its conditions failing at once."""
    return privacy.external_llm_gate_policy(
        residual_risk={"level": "High"},
        findings=[{"category": "natural_person_name", "risk": "CRITICAL"}],
        extraction_status={"status": "Partial", "blocks_external_llm": True},
        redaction_completed=False,
        human_review_approved=False,
        ocr_status="pending",
    )


class WelcomeClientMixin:
    """A signed-out client on a throwaway database.

    /welcome reads nothing from the database, but the app's before_request still
    readies one; point it at a temp file so no test touches db/legal_documents.db.
    """

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        conn.commit()
        conn.close()
        self.original_db_path = app_module.DB_PATH
        app_module.DB_PATH = self.db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())
        self.addCleanup(self.restore)
        self.client = app_module.app.test_client()

    def restore(self) -> None:
        app_module.DB_PATH = self.original_db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())

    def welcome(self) -> str:
        response = self.client.get("/welcome")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def fetch(self, path: str) -> tuple[int, str, bytes]:
        response = self.client.get(path)
        try:
            return response.status_code, response.mimetype, response.get_data()
        finally:
            response.close()


class TheBuiltPageIsServedTests(WelcomeClientMixin, unittest.TestCase):
    def test_welcome_is_served_without_a_session(self) -> None:
        self.welcome()

    def test_the_content_is_in_the_html_without_javascript(self) -> None:
        """Prerendered: the headline, the definition, the demo and its document
        are markup, not something a script has to draw first."""
        text = page_text(self.welcome())
        for line in (
            "Nothing becomes [PERSON_1] until you decide.",
            "A local privacy workstation for legal documents.",
            "Watch it work on a practice petition.",
            "Tamamen kurgusal dava dilekçesi",
            "SENTETİK ÖRNEK / SYNTHETIC SAMPLE",
            "Clearing it for an LLM is a separate question.",
            "What it does not do.",
            "./start.sh",
        ):
            with self.subTest(line=line):
                self.assertIn(line, text)

    def test_the_page_loads_the_built_script_and_styles(self) -> None:
        links = parse(self.welcome())
        self.assertEqual(links.stylesheets, ["/static/workstation.css", "/static/landing/landing.css"])
        self.assertEqual(links.scripts, ["/static/landing/landing.js"])
        # A module script needs a JavaScript type; Python's table names it either way.
        for path, mimetypes in (
            ("/static/landing/landing.js", {"text/javascript", "application/javascript"}),
            ("/static/landing/landing.css", {"text/css"}),
        ):
            with self.subTest(path=path):
                status, served, body = self.fetch(path)
                self.assertEqual(status, 200)
                self.assertIn(served, mimetypes)
                self.assertTrue(body)

    def test_the_old_page_is_gone(self) -> None:
        html = self.welcome()
        for marker in ("landing__orb", "data-particles", "data-rotator", "landing__stats", "/static/landing.js", "/static/landing.css"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, html)
        self.assertFalse(Path("static/landing.js").exists())
        self.assertFalse(Path("static/landing.css").exists())


class NoRemoteRequestTests(WelcomeClientMixin, unittest.TestCase):
    def test_every_link_script_and_stylesheet_is_this_apps(self) -> None:
        for tag, attr, value in parse(self.welcome()).urls:
            with self.subTest(tag=tag, attr=attr, value=value):
                self.assertTrue(
                    value.startswith("#") or (value.startswith("/") and not value.startswith("//")),
                    f"<{tag} {attr}> points off this app: {value}",
                )

    def test_every_static_file_the_page_loads_exists(self) -> None:
        links = parse(self.welcome())
        paths = {value for _tag, _attr, value in links.urls if value.startswith("/static/")}
        paths |= {
            ref if ref.startswith("/") else f"/static/{ref}"
            for sheet in links.stylesheets
            for ref in CSS_URL.findall(self.fetch(sheet)[2].decode("utf-8"))
            if not ref.startswith("data:")
        }
        self.assertIn("/static/favicon.svg", paths)
        for path in sorted(paths):
            with self.subTest(path=path):
                self.assertEqual(self.fetch(path)[0], 200)

    def test_no_stylesheet_imports_or_loads_from_another_host(self) -> None:
        for sheet in parse(self.welcome()).stylesheets:
            css = self.fetch(sheet)[2].decode("utf-8")
            with self.subTest(sheet=sheet):
                self.assertNotIn("@import", css)
                for ref in CSS_URL.findall(css):
                    self.assertNotRegex(ref, r"^(?:[a-z]+:)?//", f"{sheet} loads {ref}")

    def test_the_script_opens_no_connection(self) -> None:
        script = (BUNDLE / "landing.js").read_text(encoding="utf-8")
        self.assertIsNone(REQUEST_APIS.search(script), REQUEST_APIS.search(script))
        self.assertEqual(set(URL.findall(script)) - KNOWN_NON_REQUEST_URLS, set())

    def test_the_page_markup_names_no_other_host(self) -> None:
        html = self.welcome()
        self.assertEqual(set(URL.findall(html)) - KNOWN_NON_REQUEST_URLS, set())


class EveryLinkWorksTests(WelcomeClientMixin, unittest.TestCase):
    def test_every_link_lands_on_a_real_page_or_section(self) -> None:
        links = parse(self.welcome())
        hrefs = {value for tag, attr, value in links.urls if tag == "a" and attr == "href"}
        self.assertTrue(hrefs)
        for href in sorted(hrefs):
            with self.subTest(href=href):
                if href.startswith("#"):
                    self.assertIn(href[1:], links.ids, f"{href} names no element on the page")
                else:
                    self.assertEqual(self.fetch(href)[0], 200, href)

    def test_open_the_workstation_goes_to_the_sign_in_page(self) -> None:
        html = self.welcome()
        with app_module.app.test_request_context():
            login = app_module.url_for("auth.login")
        ctas = re.findall(r'<a class="btn btn--primary[^"]*" href="([^"]+)">Open the workstation</a>', html)
        self.assertGreaterEqual(len(ctas), 2, "the nav and hero CTAs")
        self.assertEqual(set(ctas), {login})
        self.assertIn(f'<a href="{login}">Log in</a>', html)


class TheCopyQuotesOnlyWhatTheCodeDoesTests(WelcomeClientMixin, unittest.TestCase):
    def test_the_page_quotes_no_accuracy_figure(self) -> None:
        text = page_text(self.welcome())
        self.assertIsNone(ACCURACY_WORDS.search(text), ACCURACY_WORDS.search(text))
        self.assertIsNone(PERCENTAGE.search(text), PERCENTAGE.search(text))

    def test_release_condition_count_and_labels_match_the_gate(self) -> None:
        """Fail every condition at once and count what the gate reports; the page
        quotes that number and lists those labels, in the gate's order."""
        gate = failing_gate()
        count = len(gate["failed_conditions"])
        self.assertGreaterEqual(count, 5, gate)  # sanity: everything failed
        labels = [condition["label"] for condition in gate["conditions"]]
        self.assertEqual(len(labels), count)

        demo = json.loads(DEMO_JSON.read_text(encoding="utf-8"))
        self.assertEqual([c["label"] for c in demo["states"]["uploaded"]["gate"]["conditions"]], labels, REBUILD)

        text = page_text(self.welcome())
        self.assertIn(f"only when all {count} of these hold", text)
        positions = [text.find(label) for label in labels]
        self.assertNotIn(-1, positions, labels)
        self.assertEqual(positions, sorted(positions), "the page lists the conditions out of the gate's order")


def load_generator():
    spec = importlib.util.spec_from_file_location("landing_gen_demo", LANDING / "scripts" / "gen_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TheDemoIsTheProductsOutputTests(WelcomeClientMixin, unittest.TestCase):
    """The demo claims to be the real detector on the bundled petition."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.demo, cls.plan_cases = load_generator().generate()

    def test_the_committed_demo_is_what_the_app_produces_today(self) -> None:
        committed = json.loads(DEMO_JSON.read_text(encoding="utf-8"))
        self.assertEqual(committed, self.demo, f"landing/src/demo.json is stale; {REBUILD}")

    def test_the_committed_plan_cases_are_what_the_app_produces_today(self) -> None:
        """scripts/verify_plan.mjs checks the page's plan port against these."""
        committed = json.loads(PLAN_CASES_JSON.read_text(encoding="utf-8"))
        self.assertEqual(committed, self.plan_cases, f"landing/scripts/plan_cases.json is stale; {REBUILD}")

    def test_the_built_page_draws_the_committed_demo(self) -> None:
        """demo.json is compiled into the bundle and the prerendered markup, so a
        regenerated demo that was not rebuilt shows here."""
        text = page_text(self.welcome())
        for finding in self.demo["findings"]:
            with self.subTest(finding=finding["id"]):
                self.assertIn(finding["sample"], text)
        self.assertIn(f"{len(self.demo['findings'])} spans in this petition", text)

    def test_the_plaintiff_is_the_headlines_placeholder(self) -> None:
        people = [f for f in self.demo["findings"] if f["category"] == "natural_person_name"]
        self.assertTrue(people)
        self.assertEqual({f["placeholder"] for f in people}, {"[PERSON_1]"})
        self.assertIn('<span class="hero__chip">[PERSON_1]</span>', self.welcome())


class TheSampleIdsAreMaskedTests(WelcomeClientMixin, unittest.TestCase):
    def sample_ids(self) -> set[str]:
        text, _warning = extract_text(SAMPLE)
        ids = {n for n in re.findall(r"(?<!\d)\d{11}(?!\d)", text) if privacy.valid_turkish_national_id(n)}
        ids |= {n for n in re.findall(r"(?<!\d)\d{10}(?!\d)", text) if privacy.valid_turkish_tax_number(n)}
        return ids

    def test_no_check_digit_valid_id_from_the_sample_reaches_the_page(self) -> None:
        ids = self.sample_ids()
        self.assertEqual(len(ids), 2, "expected the sample's one TCKN and one VKN")
        surfaces = {
            "/welcome": self.welcome(),
            "static/landing/landing.js": (BUNDLE / "landing.js").read_text(encoding="utf-8"),
            "landing/src/demo.json": DEMO_JSON.read_text(encoding="utf-8"),
            "landing/scripts/plan_cases.json": PLAN_CASES_JSON.read_text(encoding="utf-8"),
        }
        for name, body in surfaces.items():
            for number in ids:
                with self.subTest(surface=name):
                    self.assertNotIn(number, body)
        self.assertIn("107••••••48", page_text(surfaces["/welcome"]))


if __name__ == "__main__":
    unittest.main()
