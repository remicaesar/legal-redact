# -*- coding: utf-8 -*-
"""The held-out measurement's own guards.

`accuracy_eval.py` is an instrument, and the defect it was built to correct --
a scorer that cannot report a miss -- is one an instrument can have about itself.
So these tests do not check that the detector is good; they check that the
scorer would SAY SO if it were bad. Every guard here was watched failing with
the thing it guards deliberately broken; the pairs are recorded in the PR.

Nothing here asserts a recall or precision floor. `gold/` + `accuracy_audit.py`
is the ratchet and CI still gates on it; this corpus is a measurement, and
gating it at a floor would quietly turn it into a second ratchet.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import tests.env_setup  # noqa: F401  -- sets LEGAL_ANALYZER_SECRET_KEY before app-touching imports

import accuracy_eval
from accuracy_audit import label_matches_finding
from accuracy_eval import Span, covers, diagnose_unlocatable, locate, score_spans
from legal_analyzer.extraction import extract_text

PROJECT_DIR = Path(__file__).resolve().parents[1]
EVAL_DIR = PROJECT_DIR / "eval"


def load_generator():
    """Import eval/generate_corpus.py by path.

    Not a package import: `eval` is also a Python builtin's name, and a top-level
    module called `eval` is a trap waiting for whoever next writes `import eval`.
    The directory stays plain data plus a script.
    """
    spec = importlib.util.spec_from_file_location("eval_generate_corpus", EVAL_DIR / "generate_corpus.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScorerSeesAMissTests(unittest.TestCase):
    """The scorer must be able to report a miss, a stray and a half-covered entity."""

    def setUp(self):
        # "Mehmet Yılmaz" at 0-13 and a national id at 20-31, say.
        self.gold = [
            Span("natural_person_name", 0, 13),
            Span("turkish_national_id", 20, 31),
        ]
        self.clean = [
            Span("natural_person_name", 0, 13),
            Span("turkish_national_id", 20, 31),
        ]

    def test_a_clean_run_scores_perfectly(self):
        scores = score_spans(self.gold, self.clean)
        self.assertEqual(scores["natural_person_name"].recall, 1.0)
        self.assertEqual(scores["turkish_national_id"].recall, 1.0)
        self.assertEqual(scores["natural_person_name"].precision, 1.0)

    def test_a_dropped_entity_lowers_recall(self):
        """The guard the whole file exists for: a missing finding must show up."""
        findings = [span for span in self.clean if span.category != "natural_person_name"]
        scores = score_spans(self.gold, findings)
        self.assertEqual(scores["natural_person_name"].detected, 0)
        self.assertEqual(scores["natural_person_name"].recall, 0.0)
        self.assertEqual(scores["turkish_national_id"].recall, 1.0)

    def test_a_finding_at_the_wrong_offset_is_not_credited(self):
        """Right category, right text, wrong place: a miss AND a false positive.

        This is precisely where `accuracy_audit.label_matches_finding` differs, so
        the audit's own matcher is exercised alongside to keep the difference
        visible. If someone ever "simplifies" this scorer back to substring
        matching, this test is what stops it.
        """
        misplaced = [Span("natural_person_name", 400, 413)]
        scores = score_spans([self.gold[0]], misplaced)
        self.assertEqual(scores["natural_person_name"].detected, 0)
        self.assertEqual(scores["natural_person_name"].false_positives, 1)

        # The audit's matcher, given the same situation, calls it a hit.
        self.assertTrue(
            label_matches_finding(
                {"category": "natural_person_name", "text": "Mehmet Yılmaz"},
                {"category": "natural_person_name", "sample": "Mehmet Yılmaz"},
            ),
            "the audit matcher is offset-blind; that is why this module does not reuse it",
        )

    def test_a_partial_span_is_detected_but_not_fully_covered(self):
        """`ANKARA 7. ASLİYE HUKUK MAHKEMESİ` matched from `7.` onwards leaves the city."""
        gold = [Span("court_or_authority", 0, 32)]
        partial = [Span("court_or_authority", 7, 32)]
        scores = score_spans(gold, partial)
        self.assertEqual(scores["court_or_authority"].recall, 1.0)
        self.assertEqual(scores["court_or_authority"].full_span_recall, 0.0)

    def test_two_findings_together_fully_cover_one_entity(self):
        """Coverage is the union: a name split across two findings is still redactable."""
        gold = [Span("natural_person_name", 0, 20)]
        halves = [Span("natural_person_name", 0, 11), Span("natural_person_name", 11, 20)]
        scores = score_spans(gold, halves)
        self.assertEqual(scores["natural_person_name"].fully_covered, 1)

    def test_a_gap_inside_the_union_is_a_partial(self):
        self.assertTrue(covers([(0, 11), (11, 20)], 0, 20))
        self.assertFalse(covers([(0, 10), (11, 20)], 0, 20))
        self.assertFalse(covers([], 0, 5))

    def test_one_over_wide_finding_over_two_entities_is_one_true_positive(self):
        """An over-reaching span must not buy precision twice."""
        gold = [Span("company_name", 0, 10), Span("company_name", 12, 22)]
        sweep = [Span("company_name", 0, 22)]
        scores = score_spans(gold, sweep)
        self.assertEqual(scores["company_name"].detected, 2)
        self.assertEqual(scores["company_name"].findings, 1)
        self.assertEqual(scores["company_name"].false_positives, 0)

    def test_a_different_category_at_the_same_offsets_is_not_a_match(self):
        """A VKN reported as a phone number is a miss and a stray, not a hit."""
        gold = [Span("tax_number", 0, 10)]
        wrong_category = [Span("phone_number", 0, 10)]
        scores = score_spans(gold, wrong_category)
        self.assertEqual(scores["tax_number"].detected, 0)
        self.assertEqual(scores["phone_number"].false_positives, 1)

    def test_clause_rules_are_reported_separately_not_as_false_positives(self):
        """party_role has no gold label anywhere; counting it as an error says nothing."""
        self.assertIn("party_role", accuracy_eval.UNSCORED_CATEGORIES)
        scores = score_spans([], [Span("party_role", 0, 6)])
        self.assertNotIn("party_role", scores)


class LabelRealignmentTests(unittest.TestCase):
    """Corpus offsets index the .txt source; DOCX and PDF have to be found afresh."""

    def test_a_unique_occurrence_is_found(self):
        text = "Davacı: Mehmet Yılmaz\nTCKN: 12345678950\n"
        self.assertEqual(locate("Mehmet Yılmaz", text, 0.3), text.index("Mehmet Yılmaz"))

    def test_a_repeated_name_anchors_to_the_nearer_mention(self):
        text = "Mehmet Yılmaz" + ("x" * 200) + "Mehmet Yılmaz"
        self.assertEqual(locate("Mehmet Yılmaz", text, 0.0), 0)
        self.assertEqual(locate("Mehmet Yılmaz", text, 1.0), 213)

    def test_a_missing_entity_returns_none(self):
        self.assertIsNone(locate("Mehmet Yılmaz", "nothing of the sort here", 0.5))

    def test_a_page_footer_inside_a_name_is_reported_as_interleaved(self):
        """The PDF page-furniture failure, pinned as a diagnosis.

        A running footer digit landing between two words of a name is genuinely in
        the extracted stream. The entity is unmatchable as one span -- so it cannot
        be approved and cannot be redacted -- while the document still produces
        findings and looks reviewed. This asserts the INSTRUMENT names that case
        rather than silently folding it into recall; it does not assert the
        detector handles it, because it does not.
        """
        extracted = "Duruşmaya katılan tanıklardan Sayın Mehmet\n\n1\nYıldırımhan beyanda bulunmuştur."
        self.assertIsNone(locate("Mehmet Yıldırımhan", extracted, 0.5))
        self.assertEqual(diagnose_unlocatable("Mehmet Yıldırımhan", extracted), "interleaved")

    def test_a_line_break_inside_a_name_is_reported_as_wrapped(self):
        extracted = "...tanıklardan Sayın Mehmet\nYıldırımhan beyanda bulunmuştur."
        self.assertIsNone(locate("Mehmet Yıldırımhan", extracted, 0.5))
        self.assertEqual(diagnose_unlocatable("Mehmet Yıldırımhan", extracted), "wrapped")

    def test_text_the_extractor_lost_is_reported_as_absent(self):
        self.assertEqual(diagnose_unlocatable("Mehmet Yıldırımhan", "an unrelated page"), "absent")


class CorpusIntegrityTests(unittest.TestCase):
    """The ruler must be the ruler the generator describes, in both directions."""

    @classmethod
    def setUpClass(cls):
        cls.generator = load_generator()
        cls.manifest = json.loads((EVAL_DIR / "labels.json").read_text(encoding="utf-8"))

    def test_the_committed_corpus_matches_the_generator(self):
        manifest, texts = self.generator.build()
        problems = self.generator.check(manifest, texts)
        self.assertEqual(
            problems,
            [],
            "eval/ is out of date with eval/generate_corpus.py; "
            "regenerate with python3 eval/generate_corpus.py",
        )

    def test_the_generator_is_deterministic(self):
        self.assertEqual(self.generator.build(), self.generator.build())

    def test_every_label_slices_out_of_its_own_document(self):
        for doc in self.manifest["documents"]:
            text = (EVAL_DIR / "corpus" / doc["path"]).read_text(encoding="utf-8")
            for label in doc["labels"]:
                with self.subTest(doc=doc["id"], label=label["text"]):
                    self.assertEqual(text[label["start"]:label["end"]], label["text"])

    def test_no_label_crosses_a_line_break(self):
        """The same invariant tests/test_redaction_parity.py holds for findings.

        A span carrying a newline can never be matched against a DOCX part, whose
        text is the concatenation of its runs, so a gold label that crossed one
        would be demanding something the deliverable cannot deliver.
        """
        for doc in self.manifest["documents"]:
            for label in doc["labels"]:
                self.assertNotIn("\n", label["text"], f"{doc['id']}: {label['text']!r}")

    def test_the_corpus_covers_both_languages_at_a_size_worth_quoting(self):
        langs = {doc["lang"] for doc in self.manifest["documents"]}
        self.assertEqual(langs, {"tr", "en"})
        spans = sum(len(doc["labels"]) for doc in self.manifest["documents"])
        self.assertGreaterEqual(len(self.manifest["documents"]), 40)
        self.assertGreaterEqual(spans, 400)

    def test_the_corpus_is_not_the_gold_set(self):
        """Held-out means held out: no eval document may come from gold/'s fixtures."""
        gold = json.loads((PROJECT_DIR / "gold" / "gold_labels.example.json").read_text(encoding="utf-8"))
        gold_names = {Path(doc["path"]).name for doc in gold["documents"]}
        eval_names = {Path(doc["path"]).name for doc in self.manifest["documents"]}
        self.assertEqual(gold_names & eval_names, set())


class RenderedFormatTests(unittest.TestCase):
    """The rendered formats must carry the corpus text, or the arm measures itself."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((EVAL_DIR / "labels.json").read_text(encoding="utf-8"))
        cls.doc = next(d for d in cls.manifest["documents"] if d["id"] == "tr_petition_1")
        cls.text = (EVAL_DIR / "corpus" / cls.doc["path"]).read_text(encoding="utf-8")

    def test_docx_rendering_preserves_every_labelled_entity(self):
        """Every MENTION, not merely every string.

        `assertIn` is not enough here: a party is named in the heading and again
        in the signature block, so a renderer that silently dropped the tail of
        the document still satisfied it. Counting occurrences is what catches
        that, and it is also the property realignment depends on -- `locate()`
        picks between repeated mentions by position, which needs them all present.
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.docx"
            accuracy_eval.render_docx(self.text, path)
            extracted, warning = extract_text(path)
        self.assertIsNone(warning)
        for label in self.doc["labels"]:
            with self.subTest(label=label["text"]):
                self.assertEqual(
                    extracted.count(label["text"]),
                    self.text.count(label["text"]),
                    f"{label['text']!r} does not appear the same number of times after rendering",
                )

    def test_pdf_rendering_keeps_turkish_characters(self):
        """insert_text() encodes Latin-1 and turns every ş/ğ/İ into a replacement.

        The PDF arm would then be measuring a broken renderer rather than the
        detector, and it would look like a catastrophic accuracy result.
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.pdf"
            accuracy_eval.render_pdf("İstanbul Şişli 3. Asliye — Çağrı Öz, Ilgaz Gündoğmuş", path)
            extracted, _ = extract_text(path)
        for token in ("İstanbul", "Şişli", "Çağrı", "Gündoğmuş"):
            self.assertIn(token, extracted)

    @unittest.skipUnless(shutil.which("pdftotext"), "poppler not installed")
    def test_the_poppler_arm_is_the_product_branch_it_claims_to_be(self):
        """accuracy_eval runs the two PDF branches by name instead of by PATH.

        That is only honest while each one still matches what the product does.
        On a machine WITH poppler, extract_text picks the poppler branch, so the
        two must agree character for character; if extraction.extract_pdf_text
        changes and this module does not, this is what says so.
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.pdf"
            accuracy_eval.render_pdf(self.text, path)
            product, _ = extract_text(path)
            evaluated = accuracy_eval.extract_pdf_with("poppler", path)
        self.assertEqual(product, evaluated)

    def test_the_two_pdf_branches_are_both_measured(self):
        """One row would make the published page depend on the runner's PATH.

        The first CI run of this tool failed exactly that way: the laptop had
        poppler, the runner did not, and --check-docs called the page stale.
        """
        self.assertEqual(accuracy_eval.PDF_FORMATS, ("pdf-poppler", "pdf-pypdf"))
        for fmt in accuracy_eval.PDF_FORMATS:
            self.assertIn(fmt, accuracy_eval.ALL_FORMATS)

    def test_the_pdf_carries_a_running_page_number(self):
        """No footer, no page furniture, and the PDF arm stops testing anything."""
        long_text = "\n".join(f"Satır {n}: dosya kapsamında inceleme yapılmıştır." for n in range(90))
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.pdf"
            accuracy_eval.render_pdf(long_text, path)
            import pymupdf

            with pymupdf.open(path) as document:
                self.assertGreater(document.page_count, 1)
                footers = [page.get_text().strip().splitlines()[-1].strip() for page in document]
        self.assertEqual(footers[:2], ["1", "2"])


class ReportAndDocsTests(unittest.TestCase):
    """The published numbers must come from a run, and must not be able to rot."""

    def test_a_txt_only_run_produces_the_documented_shape(self):
        """Cheap end-to-end run. CI does the full three-format one with --check-docs."""
        with TemporaryDirectory() as tmp:
            report = accuracy_eval.run_eval(EVAL_DIR / "labels.json", ("txt",), Path(tmp))
        self.assertEqual(report["corpus"]["documents"], 40)
        self.assertIn("txt|tr", report["headline"])
        self.assertIn("txt|en", report["headline"])
        for bucket in report["headline"].values():
            self.assertIsNotNone(bucket["recall"])
            self.assertIsNotNone(bucket["full_span_recall"])
            self.assertLessEqual(bucket["fully_covered"], bucket["detected"])
            self.assertLessEqual(bucket["detected"], bucket["gold"])

    def test_the_accuracy_page_changes_when_a_number_changes(self):
        """--check-docs has to be able to fail, or the committed page is decoration."""
        with TemporaryDirectory() as tmp:
            report = accuracy_eval.run_eval(EVAL_DIR / "labels.json", ("txt",), Path(tmp))
        before = accuracy_eval.render_docs(report)
        moved = json.loads(json.dumps(report))
        key = next(iter(moved["headline"]))
        moved["headline"][key]["recall"] = round((moved["headline"][key]["recall"] or 0) / 2, 4)
        self.assertNotEqual(before, accuracy_eval.render_docs(moved))

    def test_the_committed_accuracy_page_is_generated(self):
        page = accuracy_eval.DOCS_PATH.read_text(encoding="utf-8")
        self.assertTrue(page.startswith("<!-- GENERATED by accuracy_eval.py --write-docs"))
        self.assertIn("held-out", page)
        # It must say which of the two tools it came from; conflating them is the
        # whole mistake this work exists to correct.
        self.assertIn("regression-ratchet", page)

    def test_the_regression_ratchet_is_still_wired_and_still_separate(self):
        """Adding a measurement must not have removed a guard."""
        workflow = (PROJECT_DIR / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("accuracy_audit.py --labels gold/gold_labels.example.json", workflow)
        self.assertTrue((PROJECT_DIR / "gold" / "gold_labels.example.json").exists())
        # The assertion itself, not merely the word: the token appears in the
        # lookup as well as in the check, so asserting the token alone stays
        # green with the gate gutted.
        self.assertIn('metrics.get("false_negative_count", -1) != 0', workflow)
        for name in ("false_low_count", "risk_shortfall_count"):
            self.assertIn(f'"{name}"', workflow)
        # And the measurement is wired beside it, not instead of it.
        self.assertIn("eval/generate_corpus.py --check", workflow)
        self.assertIn("accuracy_eval.py --check-docs", workflow)


if __name__ == "__main__":
    unittest.main()
