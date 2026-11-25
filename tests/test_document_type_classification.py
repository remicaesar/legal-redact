"""The practice samples are filed under the document type they are.

Before the enforcement and notary categories existed, the classifier's substring
rules filed the icra file as an NDA ("-nda" ends ordinary Turkish words) and the
notary notice and the danışmanlık contract as shareholders' agreements ("nüsha"
contains "sha"). The studio shows that label to the reviewer.

Categories are seed data rather than schema, so the second half checks that a
database seeded by an older checkout -- one without the new categories -- still
files these documents correctly through both ingestion paths.
"""

from __future__ import annotations

import shutil
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import tests.env_setup  # noqa: F401  -- must precede "import app"

import app as app_module
import classify
from db.init_db import seed_categories
from legal_analyzer.classifier import classify_document
from legal_analyzer.extraction import extract_text

ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = ROOT / "samples" / "documents"

EXPECTED = {
    "turkish_enforcement_file.docx": ("enforcement", "payment_order"),
    "turkish_notary_notice.docx": ("notary", "notice"),
    "turkish_contract.docx": ("contract_law", "consulting_advisory"),
}


def stale_database(path: Path) -> None:
    """A database as an older checkout's db/init_db.py left it."""
    conn = sqlite3.connect(path)
    conn.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    seed_categories(conn)
    conn.execute(
        "DELETE FROM subcategories WHERE category_id IN "
        "(SELECT id FROM categories WHERE name IN ('enforcement', 'notary'))"
    )
    conn.execute("DELETE FROM categories WHERE name IN ('enforcement', 'notary')")
    conn.commit()
    conn.close()


def filed_as(conn: sqlite3.Connection, document_id: int) -> tuple[str, str | None]:
    return tuple(conn.execute(
        """
        SELECT c.name, s.name FROM documents d
        JOIN categories c ON c.id = d.category_id
        LEFT JOIN subcategories s ON s.id = d.subcategory_id
        WHERE d.id = ?
        """,
        (document_id,),
    ).fetchone())


class PracticeSampleClassificationTests(unittest.TestCase):
    def test_each_practice_sample_is_filed_under_its_document_type(self):
        for name, expected in EXPECTED.items():
            with self.subTest(name=name):
                text, _warning = extract_text(SAMPLES_DIR / name)
                result = classify_document(name, text)
                self.assertEqual((result["category"], result["subcategory"]), expected)


class OlderDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "legal_documents.db"
        stale_database(self.db_path)

    def test_classify_scan_files_new_types_in_an_older_database(self):
        stock = self.tmp_path / "stock"
        stock.mkdir()
        for name in EXPECTED:
            shutil.copyfile(SAMPLES_DIR / name, stock / name)
        with patch.object(classify, "DB_PATH", self.db_path):
            report = classify.scan_and_classify(stock, [])
        self.assertEqual(report["errors"], [])
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT id, filename FROM documents").fetchall()
            self.assertEqual({filename: filed_as(conn, doc_id) for doc_id, filename in rows}, EXPECTED)
        finally:
            conn.close()

    def test_app_upload_files_new_types_in_an_older_database(self):
        original = app_module.DB_PATH
        self.addCleanup(setattr, app_module, "DB_PATH", original)
        self.addCleanup(app_module.READY_DB_PATHS.discard, self.db_path.resolve())
        app_module.DB_PATH = self.db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())
        conn = app_module.get_db()
        try:
            for name, expected in EXPECTED.items():
                with self.subTest(name=name), app_module.app.test_request_context():
                    document_id = app_module.save_uploaded_document(SAMPLES_DIR / name, name, conn)
                    self.assertEqual(filed_as(conn, document_id), expected)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
