"""Public landing page.

The only unauthenticated page in the app besides /login. It reads nothing from
the database and renders no document data, so it is safe to serve anonymously.

The page is built, not written here: landing/ holds its React + visx source,
and `cd landing && npm run build` writes templates/landing.html (the markup,
prerendered) and static/landing/ (the script and styles that bring it to life).
Node is needed only to rebuild it, never to run the app. See landing/README.md.

Its copy lives in landing/src/copy.js, each line next to what substantiates it.
No accuracy figure appears on the page, on purpose: the gold set is 25 synthetic
documents written by the people who wrote the rules, so recall 1.0 against it is
a regression guard, not a property of the detector on real filings, and a bare
number on an unauthenticated page is what gets quoted back without its
qualifier. The demo's findings, placeholders and gate states are not typed
either: landing/scripts/gen_demo.py produces them by running this app on the
bundled synthetic petition, and tests/test_landing.py fails when they drift.
"""

from __future__ import annotations

from flask import Blueprint, render_template

landing_bp = Blueprint("landing", __name__)


@landing_bp.route("/welcome")
def welcome():
    return render_template("landing.html")
