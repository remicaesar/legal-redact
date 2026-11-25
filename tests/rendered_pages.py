"""Render every page template through its real route, the way a browser gets it.

The page skeleton and the workstation chrome -- `<html lang>`, the CSRF token
and wrapper, the sidebar -- live once, in templates/base.html. A child
template's source no longer contains any of them, so a test about what a page
*ships* has to read what its route renders, not the file.

`render_every_page()` also records which template each route rendered (Flask's
`template_rendered` signal), and `unrendered_templates()` names every template
file that no route here reached. A new page that nobody adds to `PAGES` is then
a failure rather than a page no test ever looks at.

Everything here is synthetic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import template_rendered
from werkzeug.security import generate_password_hash

import tests.env_setup  # noqa: F401  -- sets LEGAL_ANALYZER_SECRET_KEY before app is imported
import app as app_module

TEMPLATE_DIR = Path("templates")
BASE_TEMPLATE = "base.html"
# The unauthenticated marketing page. It extends the base for the skeleton only
# and carries no workstation chrome.
LANDING_TEMPLATE = "landing.html"

DOCUMENT_ID = 1
MATTER_ID = 1
# route -> the template it must render. The landing page is fetched signed out.
PAGES = {
    "/welcome": LANDING_TEMPLATE,
    "/": "index.html",
    "/matters": "matters.html",
    f"/matter/{MATTER_ID}": "matter.html",
    f"/matter/{MATTER_ID}/review": "matter_review.html",
    "/audit-log": "audit.html",
    "/settings": "settings.html",
    f"/studio/{DOCUMENT_ID}": "studio.html",
}
PASSWORD = "synthetic-password"


class RenderedPage:
    def __init__(self, route: str, template: str, html: str) -> None:
        self.route = route
        self.template = template
        self.html = html


def render_every_page(role: str = "admin") -> dict[str, RenderedPage]:
    """Return {template name: RenderedPage}, each fetched through its route.

    `role` is the signed-in user's role. Routes that role may not open (the
    admin pages, for a reviewer) are skipped rather than rendered as a refusal.
    """
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "pages.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (role, generate_password_hash(PASSWORD), role),
        )
        conn.execute("INSERT INTO matters (id, name) VALUES (?, ?)", (MATTER_ID, "Synthetic matter"))
        conn.execute(
            "INSERT INTO documents (id, filename, filepath, file_extension, file_size) VALUES (?, ?, ?, ?, ?)",
            (DOCUMENT_ID, "synthetic_petition.docx", "/tmp/synthetic_petition.docx", ".docx", 100),
        )
        conn.commit()
        conn.close()

        original_db_path = app_module.DB_PATH
        app_module.DB_PATH = db_path
        app_module.READY_DB_PATHS.discard(db_path.resolve())
        rendered: list[str] = []

        def record(sender, template, context, **extra) -> None:
            rendered.append(template.name)

        template_rendered.connect(record, app_module.app)
        try:
            signed_out = app_module.app.test_client()
            signed_in = app_module.app.test_client()
            response = signed_in.post("/login", data={"username": role, "password": PASSWORD})
            assert response.status_code == 302, f"synthetic {role} could not sign in"
            pages: dict[str, RenderedPage] = {}
            for route, template in PAGES.items():
                client = signed_out if template == LANDING_TEMPLATE else signed_in
                rendered.clear()
                response = client.get(route)
                if response.status_code in (302, 403) and template != LANDING_TEMPLATE:
                    continue
                assert response.status_code == 200, f"{route} returned {response.status_code}"
                assert rendered == [template], f"{route} rendered {rendered}, expected {template}"
                pages[template] = RenderedPage(route, template, response.get_data(as_text=True))
            return pages
        finally:
            template_rendered.disconnect(record, app_module.app)
            app_module.DB_PATH = original_db_path
            app_module.READY_DB_PATHS.discard(db_path.resolve())


def page_templates() -> set[str]:
    """Every template file a route renders: all of them but the base."""
    return {path.name for path in TEMPLATE_DIR.glob("*.html")} - {BASE_TEMPLATE}


def unrendered_templates(pages: dict[str, RenderedPage]) -> set[str]:
    return page_templates() - set(pages)
