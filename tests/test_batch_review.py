"""Batch review: one decision per finding across a matter, never a shortcut past the gate.

A matter is reviewed in one pass at /matter/<id>/review. The batch groups every
finding in the matter by category and Turkish-folded text, so a party named in
three documents is decided once. What these tests pin is less the convenience
than the four things that convenience must not cost:

1. A group decision fills in UNDECIDED findings only. It never overwrites a
   decision already made, in the Studio or by a per-document override, and a
   per-document override changes that one document and nothing else.
2. The batch never signs a document off, and never changes one that is. Deciding
   every finding leaves every document unsigned, the batch export refuses, and
   sign-off is the existing one-document /review endpoint with its own
   refusals; once a document is signed off, no group decision, group Undo or
   override reaches its findings.
3. The batch export is each document's own reviewed export, and a document
   whose gate refuses -- unsigned, or reverted to undecided after sign-off --
   is held back from the archive.
4. Deciding a PDF finding does not decide its boxes, and the finding text never
   reaches the audit log.

Every document, name and address here is synthetic; the email domain is RFC
2606's example.com.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf
from defusedxml import ElementTree
from werkzeug.security import generate_password_hash

import tests.env_setup  # noqa: F401  -- sets LEGAL_ANALYZER_SECRET_KEY before app is imported
import app as app_module
from blueprints.batch import finding_group_key
from legal_analyzer.taxonomy import CATEGORIES, SUBCATEGORIES

W_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NAME = "Deniz Kurgusal"
BODY = (
    "İşbu dilekçe davacı vekili Av. Deniz Kurgusal tarafından hazırlanmıştır. Davacı ile davalı "
    "arasındaki uyuşmazlık hakkında mahkemeye sunulan bu belge ilgili kanun hükümlerine göre "
    "düzenlenmiştir ve taraflar arasında imzalanan sözleşme hükümleri geçerlidir. İletişim için "
    "deniz.kurgusal@example.com adresine yazılabilir."
)
DOCUMENTS = {
    "a_dilekce.docx": BODY,
    "b_tanik.docx": BODY + " Tanık Sayın Mert Masalcı dinlenmiştir.",
    # The same person in capitals: one entity, so one decision.
    "c_ozet.docx": BODY + " DAVACI VEKİLİ AV. DENİZ KURGUSAL",
}


def make_docx(path: Path, text: str) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{W_NAMESPACE}"><w:body>'
            f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
            f"</w:body></w:document>",
        )


def make_pdf(path: Path, text: str) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 520, 400), text, fontsize=11)
    doc.save(path)
    doc.close()


def docx_text(data: bytes) -> str:
    with ZipFile(BytesIO(data)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    return "".join(node.text or "" for node in root.iter(f"{{{W_NAMESPACE}}}t"))


class BatchHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        db_path = self.tmp_path / "batch.db"
        previous = (app_module.DB_PATH, app_module.EXPORT_DIR)
        app_module.DB_PATH = db_path
        app_module.EXPORT_DIR = self.tmp_path / "exports"
        app_module.READY_DB_PATHS.discard(db_path.resolve())

        def restore() -> None:
            app_module.DB_PATH, app_module.EXPORT_DIR = previous
            app_module.READY_DB_PATHS.discard(db_path.resolve())

        self.addCleanup(restore)

        conn = sqlite3.connect(db_path)
        conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        for name, name_tr, icon in CATEGORIES:
            conn.execute("INSERT OR IGNORE INTO categories (name, name_tr, icon) VALUES (?, ?, ?)", (name, name_tr, icon))
            category_id = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]
            for sub_name, sub_name_tr in SUBCATEGORIES[name]:
                conn.execute(
                    "INSERT OR IGNORE INTO subcategories (category_id, name, name_tr) VALUES (?, ?, ?)",
                    (category_id, sub_name, sub_name_tr),
                )
        for role in ("reviewer", "viewer"):
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (role, generate_password_hash("synthetic-password"), role),
            )
        self.matter_id = conn.execute("INSERT INTO matters (name) VALUES ('Synthetic batch matter')").lastrowid
        self.other_matter_id = conn.execute("INSERT INTO matters (name) VALUES ('Another synthetic matter')").lastrowid
        conn.commit()
        conn.close()

        self.client = app_module.app.test_client()
        response = self.client.post("/login", json={"username": "reviewer", "password": "synthetic-password"})
        self.assertIn(response.status_code, (200, 302))

        self.doc_ids = {name: self.upload(name, text, self.matter_id) for name, text in DOCUMENTS.items()}
        # The same person in a document of ANOTHER matter, which no decision
        # here may reach.
        self.outside_id = self.upload("outside.docx", BODY, self.other_matter_id)

    def upload(self, name: str, text: str, matter_id: int, maker=make_docx) -> int:
        source = self.tmp_path / name
        maker(source, text)
        with mock.patch.object(app_module, "UPLOAD_DIR", self.tmp_path / "uploads"):
            response = self.client.post(
                "/api/upload",
                data={"file": (BytesIO(source.read_bytes()), name), "matter_id": str(matter_id)},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["id"]

    def batch(self) -> dict:
        response = self.client.get(f"/api/matter/{self.matter_id}/batch")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def group(self, category: str, text: str) -> dict:
        key = finding_group_key(category, text, 0)
        found = [group for group in self.batch()["groups"] if group["key"] == key]
        self.assertEqual(len(found), 1, f"no single {category} group for {text!r}")
        return found[0]

    def decide(self, group: dict, action: str, document_id: int | None = None, status: int = 200) -> dict:
        body = {"group_key": group["key"], "action": action}
        if document_id is not None:
            body["document_id"] = document_id
        response = self.client.post(f"/api/matter/{self.matter_id}/batch/decide", json=body)
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        return response.get_json()

    def decide_everything(self, action: str = "approve") -> None:
        for group in self.batch()["groups"]:
            if group["undecided"]:
                self.decide(group, action)

    def name_statuses(self, doc_id: int) -> list[str]:
        findings = self.client.get(f"/api/document/{doc_id}").get_json()["findings"]
        return [
            finding["review_status"]
            for finding in findings
            if finding["category"] == "natural_person_name" and finding["sample"].upper() in {NAME.upper(), "DENİZ KURGUSAL"}
        ]

    def sign_off(self, doc_id: int, status: int = 200) -> None:
        for action in ("mark_redacted", "approve"):
            response = self.client.post(f"/api/document/{doc_id}/review", json={"action": action})
            self.assertEqual(response.status_code, status, response.get_data(as_text=True))
            if status != 200:
                return

    def document_state(self, doc_id: int) -> dict:
        return next(doc for doc in self.batch()["documents"] if doc["id"] == doc_id)


class OneFindingIsOneGroupAcrossTheMatterTests(BatchHarness):
    def test_a_name_in_three_documents_is_one_group(self) -> None:
        group = self.group("natural_person_name", NAME)
        self.assertEqual(sorted(doc["document_id"] for doc in group["documents"]), sorted(self.doc_ids.values()))

    def test_the_same_name_in_capitals_joins_the_same_group(self) -> None:
        """Turkish case folding: DENİZ KURGUSAL is Deniz Kurgusal."""
        group = self.group("natural_person_name", NAME)
        capitals = next(doc for doc in group["documents"] if doc["document_id"] == self.doc_ids["c_ozet.docx"])
        self.assertEqual(len(capitals["finding_ids"]), 2)
        self.assertEqual(group["occurrences"], 4)

    def test_a_document_in_another_matter_is_not_in_the_batch(self) -> None:
        batch = self.batch()
        self.assertNotIn(self.outside_id, [doc["id"] for doc in batch["documents"]])
        for group in batch["groups"]:
            self.assertNotIn(self.outside_id, [doc["document_id"] for doc in group["documents"]])

    def test_a_name_in_one_document_is_its_own_group(self) -> None:
        group = self.group("natural_person_name", "Mert Masalcı")
        self.assertEqual([doc["document_id"] for doc in group["documents"]], [self.doc_ids["b_tanik.docx"]])

    def test_every_finding_in_the_matter_is_in_exactly_one_group(self) -> None:
        listed = sorted(
            finding_id
            for group in self.batch()["groups"]
            for doc in group["documents"]
            for finding_id in doc["finding_ids"]
        )
        stored = sorted(
            finding["id"]
            for doc_id in self.doc_ids.values()
            for finding in self.client.get(f"/api/document/{doc_id}").get_json()["findings"]
        )
        self.assertEqual(listed, stored)


class AGroupDecisionFillsInOnlyTheUndecidedTests(BatchHarness):
    def test_approving_a_group_decides_it_in_every_document_of_the_matter(self) -> None:
        result = self.decide(self.group("natural_person_name", NAME), "approve")
        self.assertEqual((result["updated"], result["documents"], result["kept"]), (4, 3, 0))
        for doc_id in self.doc_ids.values():
            with self.subTest(doc_id=doc_id):
                statuses = self.name_statuses(doc_id)
                self.assertTrue(statuses)
                self.assertEqual(set(statuses), {"approved"})

    def test_it_does_not_reach_a_document_in_another_matter(self) -> None:
        self.decide(self.group("natural_person_name", NAME), "approve")
        self.assertEqual(set(self.name_statuses(self.outside_id)), {"pending"})

    def test_it_never_overwrites_a_decision_made_in_the_studio(self) -> None:
        b_id = self.doc_ids["b_tanik.docx"]
        finding = next(
            f for f in self.client.get(f"/api/document/{b_id}").get_json()["findings"]
            if f["category"] == "natural_person_name" and f["sample"] == NAME
        )
        response = self.client.post(f"/api/finding/{finding['id']}/review", json={"action": "retain"})
        self.assertEqual(response.status_code, 200)

        result = self.decide(self.group("natural_person_name", NAME), "approve")
        self.assertEqual((result["updated"], result["kept"]), (3, 1))
        self.assertEqual(self.name_statuses(b_id), ["retained"])

    def test_it_never_overwrites_a_per_document_override(self) -> None:
        group = self.group("natural_person_name", NAME)
        b_id = self.doc_ids["b_tanik.docx"]
        self.decide(group, "dismiss", document_id=b_id)
        self.decide(group, "approve")
        self.assertEqual(self.name_statuses(b_id), ["dismissed"])
        self.assertEqual(set(self.name_statuses(self.doc_ids["a_dilekce.docx"])), {"approved"})

    def test_the_override_changes_one_document_and_nothing_else(self) -> None:
        group = self.group("natural_person_name", NAME)
        self.decide(group, "approve")
        c_id = self.doc_ids["c_ozet.docx"]
        result = self.decide(group, "retain", document_id=c_id)
        self.assertEqual((result["updated"], result["documents"]), (2, 1))
        self.assertEqual(set(self.name_statuses(c_id)), {"retained"})
        for name in ("a_dilekce.docx", "b_tanik.docx"):
            with self.subTest(document=name):
                self.assertEqual(set(self.name_statuses(self.doc_ids[name])), {"approved"})

    def test_undo_returns_the_whole_group_to_undecided(self) -> None:
        group = self.group("natural_person_name", NAME)
        self.decide(group, "approve")
        self.decide(group, "pending")
        for doc_id in self.doc_ids.values():
            with self.subTest(doc_id=doc_id):
                self.assertEqual(set(self.name_statuses(doc_id)), {"pending"})

    def test_undo_leaves_a_finding_the_reviewer_typed_in_alone(self) -> None:
        a_id = self.doc_ids["a_dilekce.docx"]
        response = self.client.post(
            f"/api/document/{a_id}/findings",
            json={"text": NAME, "category": "natural_person_name", "replacement_text": "[PERSON_X]"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        group = self.group("natural_person_name", NAME)
        self.decide(group, "approve")
        self.decide(group, "pending")
        self.assertEqual(sorted(self.name_statuses(a_id)), ["added_by_reviewer", "pending"])

    def test_an_unknown_group_or_action_is_refused(self) -> None:
        group = self.group("natural_person_name", NAME)
        self.decide({"key": "0" * 20}, "approve", status=404)
        self.decide(group, "reject", status=400)
        self.decide(group, "approve", document_id=self.outside_id, status=404)
        self.assertEqual(set(self.name_statuses(self.doc_ids["a_dilekce.docx"])), {"pending"})


class TheBatchNeverSignsADocumentOffTests(BatchHarness):
    def test_deciding_every_finding_signs_nothing_off(self) -> None:
        self.decide_everything()
        conn = sqlite3.connect(app_module.DB_PATH)
        rows = conn.execute(
            f"SELECT redaction_completed, human_review_approved FROM documents WHERE id IN ({','.join('?' * len(self.doc_ids))})",
            tuple(self.doc_ids.values()),
        ).fetchall()
        conn.close()
        self.assertEqual(set(rows), {(0, 0)})
        batch = self.batch()
        self.assertEqual({doc["next_step"] for doc in batch["documents"]}, {"sign_off"})
        self.assertEqual(batch["summary"]["export_ready_count"], 0)

    def test_the_export_refuses_until_a_document_is_signed_off(self) -> None:
        self.decide_everything()
        response = self.client.get(f"/api/matter/{self.matter_id}/redacted-export")
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))

    def test_sign_off_is_still_refused_while_a_finding_is_undecided(self) -> None:
        self.decide(self.group("natural_person_name", NAME), "approve")
        a_id = self.doc_ids["a_dilekce.docx"]
        self.assertEqual(self.document_state(a_id)["next_step"], "decide_findings")
        self.sign_off(a_id, status=409)


class TheBatchExportIsEachDocumentsOwnExportTests(BatchHarness):
    def setUp(self) -> None:
        super().setUp()
        self.decide_everything()
        self.a_id = self.doc_ids["a_dilekce.docx"]
        self.sign_off(self.a_id)

    def archive(self, status: int = 200):
        response = self.client.get(f"/api/matter/{self.matter_id}/redacted-export")
        self.assertEqual(response.status_code, status, response.get_data()[:300])
        return response

    def test_only_the_signed_off_document_is_in_the_archive(self) -> None:
        response = self.archive()
        with ZipFile(BytesIO(response.get_data())) as archive:
            self.assertEqual(archive.namelist(), ["a_dilekce_redacted.docx"])
        self.assertEqual(response.headers["X-Exported-Count"], "1")
        self.assertEqual(response.headers["X-Held-Back-Count"], "2")

    def test_the_archived_copy_is_the_single_download(self) -> None:
        with ZipFile(BytesIO(self.archive().get_data())) as archive:
            archived = docx_text(archive.read("a_dilekce_redacted.docx"))
        single = self.client.get(f"/api/document/{self.a_id}/redacted-export?format=docx")
        self.assertEqual(single.status_code, 200)
        self.assertEqual(archived, docx_text(single.get_data()))
        self.assertNotIn(NAME, archived)
        self.assertNotIn("example.com", archived)

    def test_a_document_reverted_after_sign_off_is_held_back(self) -> None:
        """redaction_completed is a latch; the export re-checks the findings themselves."""
        for finding in self.client.get(f"/api/document/{self.a_id}").get_json()["findings"]:
            if finding["category"] == "natural_person_name":
                response = self.client.post(f"/api/finding/{finding['id']}/review", json={"action": "pending"})
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.document_state(self.a_id)["next_step"], "decide_findings")
        self.archive(status=409)

    def test_every_signed_off_document_is_exported(self) -> None:
        for name in ("b_tanik.docx", "c_ozet.docx"):
            self.sign_off(self.doc_ids[name])
        with ZipFile(BytesIO(self.archive().get_data())) as archive:
            names = sorted(archive.namelist())
            texts = [docx_text(archive.read(name)) for name in names]
        self.assertEqual(names, ["a_dilekce_redacted.docx", "b_tanik_redacted.docx", "c_ozet_redacted.docx"])
        for text in texts:
            self.assertNotIn("kurgusal", text.casefold())

    def test_the_builder_refuses_an_unsigned_document_on_its_own(self) -> None:
        """The batch filters on the gate first, but the builder is the guard that cannot be skipped."""
        from blueprints.export import ReviewedExport, build_reviewed_docx

        b_id = self.doc_ids["b_tanik.docx"]
        with app_module.app.test_request_context():
            result = build_reviewed_docx(b_id, "placeholder")
        self.assertNotIsInstance(result, ReviewedExport)
        self.assertEqual(result[1], 409)

    def test_a_held_back_document_is_not_attempted(self) -> None:
        """Holding a document back is not an export attempt, so it logs no refusal against it."""
        self.archive()
        conn = sqlite3.connect(app_module.DB_PATH)
        refused = conn.execute(
            "SELECT document_id FROM audit_log WHERE action = 'export.redacted_docx' AND result = 'blocked'"
        ).fetchall()
        conn.close()
        self.assertEqual(refused, [])

    def test_each_export_is_recorded_on_its_own_document(self) -> None:
        self.archive()
        conn = sqlite3.connect(app_module.DB_PATH)
        artifacts = conn.execute(
            "SELECT document_id FROM document_artifacts WHERE artifact_type = 'redacted_docx'"
        ).fetchall()
        conn.close()
        self.assertEqual(artifacts, [(self.a_id,)])


class ASignedOffDocumentIsNotTheBatchsToChangeTests(BatchHarness):
    def setUp(self) -> None:
        super().setUp()
        self.decide_everything()
        self.a_id = self.doc_ids["a_dilekce.docx"]
        self.sign_off(self.a_id)
        self.others = [self.doc_ids["b_tanik.docx"], self.doc_ids["c_ozet.docx"]]

    def test_undo_then_not_sensitive_leaves_the_signed_off_document_as_approved(self) -> None:
        """The reviewer's sequence: a sign-off must keep covering the decisions it was given."""
        group = self.group("natural_person_name", NAME)
        undo = self.decide(group, "pending")
        self.assertEqual((undo["locked"], undo["locked_documents"]), (1, 1))
        dismiss = self.decide(self.group("natural_person_name", NAME), "dismiss")
        self.assertEqual((dismiss["updated"], dismiss["locked_documents"]), (3, 1))

        self.assertEqual(set(self.name_statuses(self.a_id)), {"approved"})
        for doc_id in self.others:
            with self.subTest(doc_id=doc_id):
                self.assertEqual(set(self.name_statuses(doc_id)), {"dismissed"})
        state = self.document_state(self.a_id)
        self.assertTrue(state["signed_off"])
        self.assertEqual(state["next_step"], "export_ready")

        response = self.client.get(f"/api/matter/{self.matter_id}/redacted-export")
        self.assertEqual(response.status_code, 200, response.get_data()[:300])
        with ZipFile(BytesIO(response.get_data())) as archive:
            archived = docx_text(archive.read("a_dilekce_redacted.docx"))
        self.assertNotIn(NAME, archived)

    def test_group_undo_leaves_the_signed_off_documents_findings_untouched(self) -> None:
        before = self.client.get(f"/api/document/{self.a_id}").get_json()["findings"]
        for group in self.batch()["groups"]:
            self.decide(group, "pending")
        after = self.client.get(f"/api/document/{self.a_id}").get_json()["findings"]
        self.assertEqual(
            [(f["id"], f["review_status"]) for f in after],
            [(f["id"], f["review_status"]) for f in before],
        )
        for doc_id in self.others:
            with self.subTest(doc_id=doc_id):
                self.assertEqual(set(self.name_statuses(doc_id)), {"pending"})

    def test_an_override_on_a_signed_off_document_is_refused(self) -> None:
        group = self.group("natural_person_name", NAME)
        for action in ("dismiss", "retain", "pending"):
            with self.subTest(action=action):
                result = self.decide(group, action, document_id=self.a_id, status=409)
                self.assertIn(f"/studio/{self.a_id}", result["error"])
        self.assertEqual(set(self.name_statuses(self.a_id)), {"approved"})
        self.assertTrue(self.document_state(self.a_id)["signed_off"])

    def test_the_batch_marks_the_signed_off_document_locked(self) -> None:
        group = self.group("natural_person_name", NAME)
        self.assertEqual(group["locked_documents"], 1)
        self.assertEqual(group["occurrences"], 4)
        self.assertEqual(group["open_occurrences"], 3)
        locked = {doc["document_id"]: doc["locked"] for doc in group["documents"]}
        self.assertEqual(locked, {self.a_id: True, **{doc_id: False for doc_id in self.others}})


    def test_a_sign_off_landing_after_the_read_still_stops_the_write(self) -> None:
        """The lock is re-checked by the UPDATE itself, not only by the read before it."""
        import blueprints.batch as batch_module

        real = batch_module.matter_findings

        def stale_read(conn, matter_id):
            return [{**finding, "locked": False} for finding in real(conn, matter_id)]

        group = self.group("natural_person_name", NAME)
        with mock.patch.object(batch_module, "matter_findings", stale_read):
            result = self.decide(group, "pending")
        self.assertEqual((result["updated"], result["documents"]), (3, 2))
        self.assertEqual((result["locked"], result["locked_documents"]), (1, 1))
        self.assertEqual(set(self.name_statuses(self.a_id)), {"approved"})
        conn = sqlite3.connect(app_module.DB_PATH)
        audited = conn.execute(
            "SELECT document_id FROM audit_log WHERE action = 'finding_batch.matter.pending'"
        ).fetchall()
        conn.close()
        self.assertNotIn((self.a_id,), audited)


class AnApprovedButUnredactedDocumentIsLockedNotSignedOffTests(BatchHarness):
    """An approval on record without redaction: locked, but not signed off, and still undecided.

    The approve action now refuses an undecided or unmarked document, so this
    state is written directly -- a database approved before that rule can
    still hold it, and the batch must keep treating it as locked.
    """

    def setUp(self) -> None:
        super().setUp()
        self.b_id = self.doc_ids["b_tanik.docx"]
        response = self.client.post(f"/api/document/{self.b_id}/review", json={"action": "approve"})
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        conn = sqlite3.connect(app_module.DB_PATH)
        conn.execute("UPDATE documents SET human_review_approved = 1 WHERE id = ?", (self.b_id,))
        conn.commit()
        conn.close()

    def test_its_group_entry_is_locked_but_not_signed_off(self) -> None:
        group = self.group("natural_person_name", "Mert Masalcı")
        (entry,) = group["documents"]
        self.assertEqual((entry["locked"], entry["signed_off"]), (True, False))
        self.assertEqual((group["undecided"], group["decision"]), (0, "locked"))

    def test_its_undecided_findings_still_count_as_undecided(self) -> None:
        batch = self.batch()
        state = next(doc for doc in batch["documents"] if doc["id"] == self.b_id)
        self.assertGreater(state["undecided_count"], 0)
        self.assertFalse(state["signed_off"])
        self.assertEqual(
            batch["summary"]["undecided_finding_count"],
            sum(doc["undecided_count"] for doc in batch["documents"]),
        )
        self.decide_everything()
        self.assertEqual(self.batch()["summary"]["undecided_finding_count"], state["undecided_count"])


class APdfBoxIsNotDecidedByTheBatchTests(BatchHarness):
    def setUp(self) -> None:
        super().setUp()
        self.pdf_id = self.upload("d_tutanak.pdf", BODY, self.matter_id, maker=make_pdf)
        response = self.client.post(f"/api/document/{self.pdf_id}/pdf/regions/generate", json={})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def pending_boxes(self) -> int:
        conn = sqlite3.connect(app_module.DB_PATH)
        count = conn.execute(
            "SELECT COUNT(*) FROM pdf_redaction_regions WHERE document_id = ? AND review_status = 'pending'",
            (self.pdf_id,),
        ).fetchone()[0]
        conn.close()
        return count

    def test_deciding_every_finding_leaves_the_boxes_pending(self) -> None:
        boxes = self.pending_boxes()
        self.assertGreater(boxes, 0)
        self.decide_everything()
        self.assertEqual(self.pending_boxes(), boxes)
        state = self.document_state(self.pdf_id)
        self.assertEqual(state["undecided_count"], 0)
        self.assertEqual(state["next_step"], "open_studio")
        self.sign_off(self.pdf_id, status=409)


class AccessAndAuditTests(BatchHarness):
    def test_a_viewer_can_neither_read_decide_nor_export(self) -> None:
        group = self.group("natural_person_name", NAME)
        viewer = app_module.app.test_client()
        viewer.post("/login", json={"username": "viewer", "password": "synthetic-password"})
        for method, url, body in (
            ("get", f"/api/matter/{self.matter_id}/batch", None),
            ("post", f"/api/matter/{self.matter_id}/batch/decide", {"group_key": group["key"], "action": "approve"}),
            ("get", f"/api/matter/{self.matter_id}/redacted-export", None),
            ("get", f"/matter/{self.matter_id}/review", None),
        ):
            with self.subTest(url=url):
                response = getattr(viewer, method)(url, json=body) if body else getattr(viewer, method)(url)
                # An API refuses; a page sends a signed-in user it refuses back to the dashboard.
                self.assertEqual(response.status_code, 403 if url.startswith("/api/") else 302)
        self.assertEqual(set(self.name_statuses(self.doc_ids["a_dilekce.docx"])), {"pending"})

    def test_the_finding_text_never_reaches_the_audit_log(self) -> None:
        group = self.group("natural_person_name", NAME)
        self.decide(group, "approve")
        self.decide(group, "retain", document_id=self.doc_ids["b_tanik.docx"])
        conn = sqlite3.connect(app_module.DB_PATH)
        rows = conn.execute(
            "SELECT action, metadata FROM audit_log WHERE action LIKE 'finding_batch.matter.%'"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 4)
        for action, metadata in rows:
            with self.subTest(action=action):
                self.assertNotIn("kurgusal", metadata.casefold())
                self.assertNotIn(group["key"], metadata)
                self.assertEqual(json.loads(metadata)["category"], "natural_person_name")

    def test_the_matter_page_offers_batch_review_to_a_reviewer_only(self) -> None:
        page = self.client.get(f"/matter/{self.matter_id}").get_data(as_text=True)
        self.assertIn(f'href="/matter/{self.matter_id}/review"', page)
        viewer = app_module.app.test_client()
        viewer.post("/login", json={"username": "viewer", "password": "synthetic-password"})
        page = viewer.get(f"/matter/{self.matter_id}").get_data(as_text=True)
        self.assertNotIn(f'href="/matter/{self.matter_id}/review"', page)


if __name__ == "__main__":
    unittest.main()
