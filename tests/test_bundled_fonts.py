"""The brand fonts ship with the app, and nothing fetches a font from the network.

The design tokens name Newsreader, Hanken Grotesk and IBM Plex Mono, and the
Legal [Redact] lockup is drawn in Newsreader and, for "[Redact]", Courier
Prime -- the Courier-style mono of the chosen 1c logo. Without an
@font-face and a bundled file, a machine that does not have them installed
renders the wordmark in Georgia and system monospace, which is not the logo.
The product is local-first, so the fix is self-hosting: no Google Fonts link,
no CDN.

These tests read the stylesheet and the files it points at, and every page as
its route renders it (tests/rendered_pages.py), so a page that stops loading
workstation.css loses the fonts visibly here rather than silently in a browser.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import tests.env_setup  # noqa: F401  -- must precede "import app"

import app as app_module
from tests import rendered_pages

STATIC = Path("static")
STYLESHEET = STATIC / "workstation.css"
FONT_DIR = STATIC / "fonts"
WOFF2_MAGIC = b"wOF2"

COMMENT = re.compile(r"/\*.*?\*/", re.S)
VAR = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*))?\)")
# A font or stylesheet host that is not this app.
REMOTE_FONT = re.compile(r"fonts\.googleapis\.com|fonts\.gstatic\.com|use\.typekit|fonts\.bunny", re.I)
EXTERNAL_STYLESHEET = re.compile(r"<link[^>]+rel=\"stylesheet\"[^>]+href=\"(?:https?:)?//", re.I)
WORDMARK_SUBJECT = re.compile(r"\.wordmark__redact(?![\w-])[^\s>+~]*$")
TURKISH = "ğĞşŞıİçÇöÖüÜ"

# family -> styles that must be declared.
REQUIRED = {
    "Newsreader": {"normal", "italic"},
    "Hanken Grotesk": {"normal"},
    "IBM Plex Mono": {"normal"},
    "Courier Prime": {"normal"},
}


def declarations(body: str) -> dict[str, str]:
    props = {}
    for decl in body.split(";"):
        if ":" in decl:
            key, value = decl.split(":", 1)
            value = re.sub(r"\s*!important\s*$", "", value.strip(), flags=re.I)
            props[key.strip().lower()] = " ".join(value.split())
    return props


def css_rules() -> list[tuple[str, dict[str, str]]]:
    """(prelude, declarations) for every rule, in source order, @media flattened."""
    text = COMMENT.sub("", STYLESHEET.read_text(encoding="utf-8"))
    rules, preludes, buf = [], [], ""
    for char in text:
        if char == "{":
            preludes.append(" ".join(buf.split()))
            buf = ""
        elif char == "}":
            prelude = preludes.pop()
            if not prelude.startswith("@") or prelude.lower() == "@font-face":
                rules.append((prelude, declarations(buf)))
            buf = ""
        else:
            buf += char
    return rules


def style_rules() -> list[tuple[list[str], dict[str, str]]]:
    return [
        ([selector.strip() for selector in prelude.split(",")], decls)
        for prelude, decls in css_rules()
        if not prelude.startswith("@")
    ]


def font_faces() -> list[dict[str, str]]:
    return [decls for prelude, decls in css_rules() if prelude.lower() == "@font-face"]


def custom_properties() -> dict[str, str]:
    props: dict[str, str] = {}
    for selectors, decls in style_rules():
        if set(selectors) <= {":root", "html:root"}:
            props.update({key: value for key, value in decls.items() if key.startswith("--")})
    return props


def resolve(value: str, props: dict[str, str]) -> str:
    for _ in range(10):
        value, count = VAR.subn(lambda m: props.get(m.group(1), m.group(2) or ""), value)
        if not count:
            return value
    raise AssertionError(f"unresolvable var() chain: {value}")


def primary_family(value: str, props: dict[str, str] | None = None) -> str:
    resolved = resolve(value, props if props is not None else custom_properties())
    return " ".join(resolved.split(",")[0].strip().strip("\"'").split())


def family(face: dict[str, str]) -> str:
    return primary_family(face["font-family"], {})


def source_path(face: dict[str, str]) -> Path:
    url = re.fullmatch(r"url\(\"([^\"]+)\"\) format\(\"woff2\"\)", face["src"])
    assert url, face["src"]
    return STYLESHEET.parent / url.group(1)


class FontFaceDeclarationsTests(unittest.TestCase):
    def test_every_token_family_has_a_font_face(self) -> None:
        props = custom_properties()
        tokens = {primary_family(value, props) for key, value in props.items() if key.startswith("--font-")}
        self.assertEqual(tokens, set(REQUIRED))
        declared = {family(face) for face in font_faces()}
        self.assertEqual(declared, set(REQUIRED))

    def test_required_styles_are_declared(self) -> None:
        for name, styles in REQUIRED.items():
            with self.subTest(family=name):
                declared = {face["font-style"] for face in font_faces() if family(face) == name}
                self.assertEqual(declared, styles)

    def test_every_face_swaps_and_is_split_into_latin_and_latin_ext(self) -> None:
        subsets: dict[tuple[str, str, str], set[str]] = {}
        for face in font_faces():
            with self.subTest(face=face["src"]):
                self.assertEqual(face["font-display"], "swap")
                self.assertIn("unicode-range", face)
                subset = "latin-ext" if source_path(face).stem.endswith("latin-ext") else "latin"
                key = (family(face), face["font-style"], face["font-weight"])
                subsets.setdefault(key, set()).add(subset)
        for key, found in subsets.items():
            with self.subTest(face=key):
                self.assertEqual(found, {"latin", "latin-ext"})

    def test_mono_weights_used_by_the_css_are_bundled(self) -> None:
        """IBM Plex Mono is static: a weight without a file is synthesised."""
        bundled = {face["font-weight"] for face in font_faces() if family(face) == "IBM Plex Mono"}
        self.assertEqual(bundled, {"400", "500", "600"})

    def test_courier_prime_draws_the_wordmark_and_nothing_else(self) -> None:
        """The 1c logo's "[Redact]" is Courier-style; mono UI text stays Plex."""
        props = custom_properties()
        wordmark = [
            decls for selectors, decls in style_rules() if any(WORDMARK_SUBJECT.search(s) for s in selectors)
        ]
        families = [decls["font-family"] for decls in wordmark if "font-family" in decls]
        self.assertTrue(families)
        self.assertEqual(primary_family(families[-1], props), "Courier Prime")
        users = {
            selector
            for selectors, decls in style_rules()
            if "font-family" in decls and primary_family(decls["font-family"], props) == "Courier Prime"
            for selector in selectors
        }
        self.assertEqual(users, {".wordmark__redact"})
        self.assertEqual(primary_family(props["--font-mono"], props), "IBM Plex Mono")
        bundled = {face["font-weight"] for face in font_faces() if family(face) == "Courier Prime"}
        self.assertEqual(bundled, {"400"})
        weights = [decls["font-weight"] for decls in wordmark if "font-weight" in decls]
        self.assertTrue(weights)
        self.assertIn(weights[-1], bundled)


class FontFilesTests(unittest.TestCase):
    def test_every_declared_file_exists_and_is_woff2(self) -> None:
        for face in font_faces():
            path = source_path(face)
            with self.subTest(path=str(path)):
                self.assertTrue(path.is_file(), path)
                self.assertEqual(path.read_bytes()[:4], WOFF2_MAGIC)

    def test_no_bundled_font_file_is_unreferenced(self) -> None:
        referenced = {source_path(face).resolve() for face in font_faces()}
        on_disk = {path.resolve() for path in FONT_DIR.rglob("*.woff2")}
        self.assertEqual(on_disk, referenced)

    def test_each_family_carries_its_ofl_licence(self) -> None:
        for directory in {source_path(face).parent for face in font_faces()}:
            with self.subTest(directory=str(directory)):
                licence = (directory / "OFL.txt").read_text(encoding="utf-8")
                self.assertIn("SIL Open Font License", licence)

    def test_turkish_letters_are_covered_by_every_face(self) -> None:
        try:
            from fontTools.ttLib import TTFont
        except ImportError:
            self.skipTest("fontTools is not installed")
        groups: dict[tuple[str, str, str], set[int]] = {}
        for face in font_faces():
            key = (family(face), face["font-style"], face["font-weight"])
            groups.setdefault(key, set()).update(TTFont(source_path(face)).getBestCmap())
        for key, cmap in groups.items():
            with self.subTest(face=key):
                self.assertEqual([c for c in TURKISH if ord(c) not in cmap], [])

    def test_the_app_serves_the_fonts_as_woff2(self) -> None:
        client = app_module.app.test_client()
        for face in font_faces():
            url = "/" + source_path(face).as_posix()
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "font/woff2")
                response.close()


class NoRemoteFontsTests(unittest.TestCase):
    def test_no_template_or_static_asset_references_a_font_host(self) -> None:
        sources = [*Path("templates").rglob("*.html"), *STATIC.glob("*.css"), *STATIC.glob("*.js")]
        for path in sources:
            with self.subTest(path=str(path)):
                self.assertIsNone(REMOTE_FONT.search(path.read_text(encoding="utf-8")))

    def test_every_rendered_page_loads_the_bundled_fonts_and_no_remote_stylesheet(self) -> None:
        pages = {name: page.html for name, page in rendered_pages.render_every_page("admin").items()}
        pages["login"] = app_module.app.test_client().get("/login").get_data(as_text=True)
        for name, html in pages.items():
            with self.subTest(page=name):
                self.assertRegex(html, r"<link rel=\"stylesheet\" href=\"/static/workstation\.css\">")
                self.assertIsNone(REMOTE_FONT.search(html))
                self.assertIsNone(EXTERNAL_STYLESHEET.search(html))


if __name__ == "__main__":
    unittest.main()
