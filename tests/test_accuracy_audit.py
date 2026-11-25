import json
import re
import unittest
from pathlib import Path

from accuracy_audit import DEFAULT_LABELS, audit_document, normalize, risk_shortfall, run_audit
from legal_analyzer.privacy_common import DIRECT_IDENTIFIER_CATEGORIES

PROJECT_DIR = Path(__file__).resolve().parent.parent


class RiskShortfallTests(unittest.TestCase):
    """risk_shortfall is the metric that closes the gap false_low cannot see:
    false_low only fires when actual_risk is exactly "Low", so a document
    expected High that computes Medium is invisible to it and to every other
    metric in run_audit. See accuracy_audit.risk_shortfall.
    """

    def test_downgrade_trips_shortfall(self):
        self.assertTrue(risk_shortfall("High", "Medium"))
        self.assertTrue(risk_shortfall("High", "Low"))
        self.assertTrue(risk_shortfall("Medium", "Low"))

    def test_over_reporting_does_not_trip_shortfall(self):
        # Expected Medium, computed High fails safe and must never be flagged.
        self.assertFalse(risk_shortfall("Medium", "High"))
        self.assertFalse(risk_shortfall("Low", "High"))
        self.assertFalse(risk_shortfall("Low", "Medium"))

    def test_matching_expectation_does_not_trip_shortfall(self):
        self.assertFalse(risk_shortfall("High", "High"))
        self.assertFalse(risk_shortfall("Medium", "Medium"))

    def test_missing_expected_risk_does_not_trip_shortfall(self):
        self.assertFalse(risk_shortfall(None, "Low"))
        self.assertFalse(risk_shortfall(None, "High"))

    def test_unknown_actual_risk_does_not_trip_shortfall(self):
        # actual_risk == "Unknown" means extraction was gated (Partial/Failed) and
        # blocks external LLM use outright -- it is not a claim of lower risk, so
        # it must never register as a shortfall regardless of what was expected.
        self.assertFalse(risk_shortfall("High", "Unknown"))
        self.assertFalse(risk_shortfall("Medium", "Unknown"))


class AuditDocumentRiskShortfallTests(unittest.TestCase):
    """Exercises risk_shortfall through audit_document() against a real fixture,
    proving the new metric fires independently of false_low on a document whose
    engine-computed risk genuinely is Medium.
    """

    def test_expected_high_actual_medium_trips_shortfall_not_false_low(self):
        # bare_names_expert_report currently computes Medium in the real gold set
        # (contextual-signal terms alone clear the Medium threshold). Overriding
        # only the expectation here simulates "gold expects High, engine reports
        # Medium" -- exactly the case false_low cannot see, without touching the
        # real gold labels file.
        item = {
            "id": "bare_names_expert_report_shortfall_probe",
            "path": "../tests/fixtures/bare_names_expert_report.txt",
            "expected_residual_risk": "High",
            "labels": [],
        }
        result = audit_document(item, DEFAULT_LABELS)

        self.assertEqual(result["actual_residual_risk"], "Medium")
        self.assertTrue(result["risk_shortfall"], "expected High vs actual Medium must trip risk_shortfall")
        self.assertFalse(result["false_low"], "false_low must stay False since actual_risk is not Low")

    def test_expected_medium_actual_high_does_not_trip_shortfall(self):
        # Sanity check in the opposite direction: over-reporting is not a shortfall.
        item = {
            "id": "commercial_contract_over_report_probe",
            "path": "../tests/fixtures/commercial_contract.txt",
            "expected_residual_risk": "Medium",
            "labels": [],
        }
        result = audit_document(item, DEFAULT_LABELS)

        self.assertEqual(result["actual_residual_risk"], "High")
        self.assertFalse(result["risk_shortfall"])
        self.assertFalse(result["false_low"])


class GoldSetRiskShortfallRegressionTests(unittest.TestCase):
    """After the lease_agreement gold-label correction (Task 2), the full gold
    set must report risk_shortfall_count == 0 alongside the pre-existing
    false_low_count == 0.
    """

    def test_gold_set_has_no_risk_shortfall(self):
        report = run_audit(DEFAULT_LABELS)

        self.assertEqual(report["metrics"]["false_low_count"], 0)
        self.assertEqual(report["metrics"]["risk_shortfall_count"], 0)
        self.assertEqual(report["metrics"]["recall"], 1.0)
        self.assertEqual(report["metrics"]["false_negative_count"], 0)


class GoldLabelGapTests(unittest.TestCase):
    """The gold set's precision must measure the rules, not missing labels.

    A finding no label matches scores as a false positive whatever the rule did,
    so a category the set never labels reports every match as noise, and a label
    wider than its entity silently credits any fragment inside it. See "Labelling
    gaps closed" in gold/README.md.
    """

    CONTEXT_CATEGORIES = {"criminal_allegation", "health_data", "privileged_or_confidential"}

    # Direct-identifier findings that are rule errors rather than missing labels;
    # each is explained in gold/README.md. Anything else unlabelled is a gap.
    ACCEPTED_DIRECT_IDENTIFIER_FALSE_POSITIVES = {
        ("tr_listed_given_names", "natural_person_name", "Barış Görüşmesi"),
        ("company_span_clauses", "natural_person_name", "Kuzey Yapı Anonim"),
        ("company_span_clauses", "natural_person_name", "Yıldız LTD"),
    }

    @classmethod
    def setUpClass(cls):
        cls.spec = json.loads(DEFAULT_LABELS.read_text(encoding="utf-8"))
        cls.report = run_audit(DEFAULT_LABELS)

    def required_labels(self, category):
        return [
            label["text"]
            for document in self.spec["documents"]
            for label in document["labels"]
            if label["category"] == category and label.get("required", True)
        ]

    def test_no_context_label_embeds_a_person_name(self):
        """A label holding a name credits a bare role-word fragment as the allegation."""
        for document in self.spec["documents"]:
            names = [normalize(label["text"]) for label in document["labels"] if label["category"] == "natural_person_name"]
            for label in document["labels"]:
                if label["category"] not in self.CONTEXT_CATEGORIES:
                    continue
                with self.subTest(document=document["id"], label=label["text"]):
                    self.assertFalse([name for name in names if name in normalize(label["text"])])

    def test_every_party_role_finding_is_labelled(self):
        unlabelled = [
            (result["id"], finding["sample"])
            for result in self.report["documents"]
            for finding in result["false_positives"]
            if finding["category"] == "party_role"
        ]
        self.assertEqual(unlabelled, [])

    def test_every_detected_direct_identifier_is_labelled(self):
        unlabelled = {
            (result["id"], finding["category"], finding["sample"])
            for result in self.report["documents"]
            for finding in result["false_positives"]
            if finding["category"] in DIRECT_IDENTIFIER_CATEGORIES
        }
        self.assertEqual(unlabelled, self.ACCEPTED_DIRECT_IDENTIFIER_FALSE_POSITIVES)

    def test_company_labels_include_title_case_lowercase_and_cedilla_free_spellings(self):
        companies = self.required_labels("company_name")
        self.assertTrue([text for text in companies if text != text.upper() and text != text.lower()], "title case")
        self.assertTrue([text for text in companies if text == text.lower()], "lowercase")
        self.assertTrue(
            [text for text in companies if re.search(r"\b(?:Sti|STI|A\.S)\.?$", text)],
            "cedilla-free legal form",
        )

    def test_a_turkish_national_id_is_labelled(self):
        self.assertTrue(self.required_labels("turkish_national_id"))


if __name__ == "__main__":
    unittest.main()
