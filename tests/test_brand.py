"""The product is Legal Redact, and every page carries the one lockup.

The lockup is defined once, in templates/partials/brand.html, and rendered by
the workstation sidebar, the landing page and the sign-in page. These tests read
what the routes ship rather than the template source, so a page that stops
extending the base or hand-rolls its own mark is caught.

Everything here is synthetic.
"""

from __future__ import annotations

import re
import unittest

import tests.env_setup  # noqa: F401  -- must precede "import app"

import app as app_module
from tests import rendered_pages

OLD_NAME = re.compile(r"redact[\s_-]?ai", re.IGNORECASE)
FAVICON_LINK = '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">'
ICON = re.compile(r'<span class="brand-icon" aria-hidden="true">.*?>L</span>')
WORDMARK = '<span class="wordmark__redact"><span aria-hidden="true">[</span>Redact<span aria-hidden="true">]</span></span>'


class EveryPageCarriesTheLockupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pages = rendered_pages.render_every_page("admin")
        cls.login = app_module.app.test_client().get("/login").get_data(as_text=True)

    def all_html(self) -> dict[str, str]:
        return {**{name: page.html for name, page in self.pages.items()}, "login": self.login}

    def test_every_page_template_was_rendered(self) -> None:
        self.assertEqual(rendered_pages.unrendered_templates(self.pages), set())

    def test_no_page_names_the_old_product(self) -> None:
        for name, html in self.all_html().items():
            with self.subTest(page=name):
                self.assertIsNone(OLD_NAME.search(html), OLD_NAME.search(html))

    def test_every_page_title_names_legal_redact(self) -> None:
        for name, html in self.all_html().items():
            with self.subTest(page=name):
                title = re.search(r"<title>(.*?)</title>", html, re.S)
                self.assertIsNotNone(title)
                self.assertIn("Legal Redact", title.group(1))

    def test_every_page_links_the_favicon(self) -> None:
        for name, html in self.all_html().items():
            with self.subTest(page=name):
                self.assertEqual(html.count(FAVICON_LINK), 1)

    def test_every_page_shows_the_bracket_icon_and_wordmark(self) -> None:
        for name, html in self.all_html().items():
            with self.subTest(page=name):
                self.assertRegex(html, ICON)
                self.assertIn(WORDMARK, html)

    def test_the_sidebar_lockup_is_icon_wordmark_and_subtitle(self) -> None:
        sidebar = re.search(r'<div class="brand-block">(.*?)</div>\s*</div>', self.pages["index.html"].html, re.S)
        self.assertIsNotNone(sidebar)
        block = sidebar.group(1)
        self.assertRegex(block, ICON)
        self.assertIn('<div class="brand-title">', block)
        self.assertIn(WORDMARK, block)
        self.assertIn('<div class="brand-subtitle">Local privacy workstation', block)


class FaviconTests(unittest.TestCase):
    def test_favicon_is_served_as_svg(self) -> None:
        response = app_module.app.test_client().get("/static/favicon.svg")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/svg+xml")
            svg = response.get_data(as_text=True)
        finally:
            response.close()
        # The brackets alone on the ink tile: no letter, no text node.
        self.assertIn('fill="#1c1a15"', svg)
        self.assertIn('fill="#f5f2eb"', svg)
        self.assertNotIn("<text", svg)


if __name__ == "__main__":
    unittest.main()
