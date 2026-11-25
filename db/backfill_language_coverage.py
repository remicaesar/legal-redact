#!/usr/bin/env python3
"""Give already-indexed documents the language answer the release gate needs.

A privacy profile stored before the document's language became a gate condition
carries no `language_coverage` key. `/api/document/<id>` computes the release
state on read and so refuses such a document correctly, but the dashboard list
serves the stored columns -- an English document approved under the old gate
went on listing as "Ready - download the reviewed redacted version". This pass
settles those rows.

It reads each document's language and settles what the language decides --
coverage, the release state and the residual risk, since a document the rules
cannot read has no measured level. Findings, finding review statuses,
`redaction_completed`, `human_review_approved` and `review_status` are left
exactly as the reviewer left them. A document whose source file is gone, or
which yields no text, is left untouched: the gate reads a missing answer as
uncovered and refuses it either way, and leaving the record alone is what lets
a later run settle it once the path is repaired. Re-running is a no-op for
every document that got an answer, and a fresh attempt for every one that did
not -- so run it again after fixing paths or mounts.

It is also the only path that recovers the language answer for a document whose
text is reviewer-accepted OCR, so an already-indexed Turkish scan reads as
uncovered until this runs.

This is deliberately not a `db/migrations/*.sql` file: a migration may not
reference any `documents` column beyond id/filename/filepath (see "Database
migrations" in AGENTS.md), and this one has to read and rewrite
`privacy_profile`.

Like benchmark.py it imports `app`, so LEGAL_ANALYZER_SECRET_KEY must be set.

    python3 db/backfill_language_coverage.py --status
    python3 db/backfill_language_coverage.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import DB_PATH, backfill_language_coverage  # noqa: E402


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def pending_count(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT privacy_profile FROM documents WHERE privacy_profile IS NOT NULL AND privacy_profile != ''"
    ).fetchall()
    pending = 0
    for row in rows:
        profile = json.loads(row["privacy_profile"] or "{}")
        if profile and not profile.get("language_coverage"):
            pending += 1
    return pending


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"No database at {args.db}.")

    conn = connect(args.db)
    if args.status:
        print(f"{pending_count(conn)} document(s) have no language answer.")
        conn.close()
        return

    summary = backfill_language_coverage(conn)
    conn.commit()
    conn.close()
    print(
        f"Examined {summary['examined']} document(s) without a language answer; "
        f"settled {summary['settled']}."
    )
    if summary["unreadable"]:
        print(
            f"{summary['unreadable']} had no readable text and were left unanswered; "
            "they stay refused. Repair the paths or mounts and run this again to settle them."
        )


if __name__ == "__main__":
    main()
