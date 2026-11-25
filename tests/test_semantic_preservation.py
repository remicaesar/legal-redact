"""The second dimension's scorer, attacked directly.

legal_semantic_preservation is what says a sanitized document is still usable,
so it has to be able to say it is NOT: these tests feed it the exact rendering
the old privilege rule produced on the NDA and require it to score that low
while the privacy dimension scores it perfect -- the combination the measurement
exists to expose.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from legal_analyzer.semantic_preservation import (
    Rendering,
    entity_removed,
    legal_semantic_preservation,
    passage_characters_removed,
    render,
)

NDA = (Path(__file__).parent / "fixtures" / "nda" / "mock_nda_tr.txt").read_text(encoding="utf-8")
DEFINITION = (
    "Gizli bilgi; ticari sırlar, müşteri listeleri, fiyatlandırma bilgileri, kaynak kodları, teknik çizimler "
    "ve tarafların yazılı olarak gizli olduğunu belirttiği her türlü bilgi ve belgedir."
)


def finding(category: str, text: str, sample: str, placeholder: str, risk: str = "HIGH", action: str = "Replace") -> dict:
    start = text.index(sample)
    return {
        "category": category,
        "sample": sample,
        "placeholder": placeholder,
        "risk": risk,
        "recommended_action": action,
        "start": start,
        "end": start + len(sample),
    }


class SemanticScoreTests(unittest.TestCase):
    def test_an_untouched_document_keeps_every_term(self) -> None:
        score = legal_semantic_preservation(NDA, NDA)
        self.assertEqual(score.kept, score.terms)
        self.assertGreater(score.terms, 30)

    def test_the_old_privilege_rendering_scores_low_while_privacy_scores_perfect(self) -> None:
        """Every identifier caught AND the definition deleted is a poor document."""
        name = "Selin Arıkan"
        findings = [
            finding("natural_person_name", NDA, name, "[PERSON_1]"),
            finding(
                "privileged_or_confidential", NDA, DEFINITION, "[PRIVILEGED_CONTENT_3]",
                risk="CRITICAL", action="Flag for human legal review",
            ),
        ]
        rendering = render(NDA, findings)
        self.assertIn("[PRIVILEGED_CONTENT_3_REDACTED]", rendering.text)
        start = NDA.index(name)
        self.assertTrue(entity_removed(rendering, start, start + len(name)))

        score = legal_semantic_preservation(NDA, rendering.text)
        self.assertLess(score.kept, score.terms)
        lost = {kind: counts["terms"] - counts["kept"] for kind, counts in score.by_kind.items()}
        self.assertGreater(lost["CONTRACTUAL_CONFIDENTIALITY"], 0, lost)
        self.assertGreater(passage_characters_removed(rendering, {"privileged_or_confidential"}), len(DEFINITION) - 5)

    def test_one_deleted_occurrence_out_of_three_is_visible(self) -> None:
        source = "Cezai şart A. Cezai şart B. Cezai şart C."
        rendered = "Cezai şart A. [X_1_REDACTED] Cezai şart C."
        score = legal_semantic_preservation(source, rendered)
        self.assertEqual(score.by_kind["CONTRACTUAL_PENALTY"], {"terms": 3, "kept": 2})

    def test_destroyed_obligation_words_are_seen(self) -> None:
        """The markers that make a clause an obligation are counted terms: a pass
        that replaced them scored a perfect 1.000 when only topical terms were."""
        source = "Taraf bedeli ödeyecektir ve bilgiyi koruyacaktır; aksi halde sorumlu olacaktır."
        rendered = "Taraf bedeli [COMPANY_1] ve bilgiyi [COMPANY_1]; aksi halde sorumlu [COMPANY_1]."
        score = legal_semantic_preservation(source, rendered)
        self.assertEqual(score.by_kind["CONTRACTUAL_OBLIGATION"]["kept"], 0)
        self.assertLess(score.kept, score.terms)

    def test_a_term_the_rendering_adds_is_not_credit(self) -> None:
        score = legal_semantic_preservation("Bir cezai şart vardır.", "Bir cezai şart vardır. Cezai şart.")
        self.assertEqual((score.kept, score.terms), (score.terms, score.terms))
        self.assertEqual(score.by_kind["CONTRACTUAL_PENALTY"], {"terms": 1, "kept": 1})


class EntityRemovedTests(unittest.TestCase):
    def test_partial_coverage_is_not_removal(self) -> None:
        rendering = Rendering(text="", redacted_spans=((0, 5, "x"), (7, 10, "x")))
        self.assertTrue(entity_removed(rendering, 0, 5))
        self.assertFalse(entity_removed(rendering, 0, 8))
        self.assertTrue(entity_removed(Rendering("", ((0, 5, "x"), (5, 10, "y"))), 2, 9))
        self.assertFalse(entity_removed(Rendering("", ()), 0, 1))


if __name__ == "__main__":
    unittest.main()
