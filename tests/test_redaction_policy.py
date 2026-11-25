"""The policy layer: what each profile recommends, and the floor no profile can lower.

A profile changes recommendations and nothing else -- every finding still waits
for a person -- but the identifier treatments are the privacy floor, and a
profile that mapped a direct identifier to PRESERVE would turn a recommendation
into a leak the reviewer was told to make. These tests pin that floor for every
profile, and pin that legal provisions are preserved in every profile.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from legal_analyzer import legal_semantics, redaction_policy
from legal_analyzer.privacy import DETECTION_RULES, DIRECT_IDENTIFIER_CATEGORIES, analyze_privacy

ALL_PROFILES = [
    redaction_policy.build_profile(name, preserve_amounts=preserve)
    for name in redaction_policy.PROFILE_NAMES
    for preserve in (False, True)
]


class PrivacyFloorTests(unittest.TestCase):
    def test_no_profile_preserves_a_direct_identifier(self) -> None:
        for profile in ALL_PROFILES:
            for category in sorted(DIRECT_IDENTIFIER_CATEGORIES):
                semantic_type = redaction_policy.semantic_type_of({"category": category})
                with self.subTest(profile=profile.name, preserve_amounts=profile.preserve_amounts, category=category):
                    self.assertIn(
                        profile.treatment(semantic_type),
                        {redaction_policy.PSEUDONYMIZE, redaction_policy.REDACT, redaction_policy.GENERALIZE},
                    )

    def test_identifier_treatments_are_identical_in_every_profile(self) -> None:
        for semantic_type in redaction_policy._IDENTIFIER_TREATMENTS:
            treatments = {profile.treatment(semantic_type) for profile in ALL_PROFILES}
            with self.subTest(semantic_type=semantic_type):
                self.assertEqual(len(treatments), 1, treatments)

    def test_every_identifier_subtype_the_detector_assigns_is_on_the_floor(self) -> None:
        """A subtype missing from the floor would fall through to REVIEW and be
        delivered as a removed passage ("[LAW_FIRM_1_REDACTED]")."""
        for profile in ALL_PROFILES:
            self.assertEqual(profile.treatment("LAW_FIRM"), redaction_policy.PSEUDONYMIZE)

    def test_a_birth_date_is_removed_in_every_profile(self) -> None:
        for profile in ALL_PROFILES:
            self.assertEqual(profile.treatment("BIRTH_DATE"), redaction_policy.REDACT)

    def test_sensitive_context_always_goes_to_a_person(self) -> None:
        for profile in ALL_PROFILES:
            for semantic_type in legal_semantics.SENSITIVE_CONTEXT_TYPES:
                self.assertEqual(profile.treatment(semantic_type), redaction_policy.REVIEW)

    def test_an_unknown_type_gets_review_never_preserve(self) -> None:
        for profile in ALL_PROFILES:
            self.assertEqual(profile.treatment("SOMETHING_NEW"), redaction_policy.REVIEW)

    def test_every_detection_category_has_a_treatment(self) -> None:
        categories = {rule.category for rule in DETECTION_RULES} | {"natural_person_name"}
        for category in sorted(categories):
            semantic_type = redaction_policy.semantic_type_of({"category": category})
            with self.subTest(category=category):
                self.assertTrue(semantic_type)
                for profile in ALL_PROFILES:
                    self.assertIn(profile.treatment(semantic_type), redaction_policy.TREATMENTS)


class ProvisionsArePreservedTests(unittest.TestCase):
    def test_every_provision_type_is_preserved_in_every_profile(self) -> None:
        for profile in ALL_PROFILES:
            for semantic_type in legal_semantics.LEGAL_PROVISION_TYPES:
                with self.subTest(profile=profile.name, semantic_type=semantic_type):
                    self.assertEqual(profile.treatment(semantic_type), redaction_policy.PRESERVE)


class ProfileDifferencesTests(unittest.TestCase):
    def test_legal_contract_review_keeps_deadlines_and_roles_strict_privacy_does_not(self) -> None:
        legal = redaction_policy.build_profile(redaction_policy.LEGAL_CONTRACT_REVIEW, preserve_amounts=False)
        strict = redaction_policy.build_profile(redaction_policy.STRICT_PRIVACY, preserve_amounts=False)
        for semantic_type in ("DEADLINE", "PARTY_ROLE"):
            self.assertEqual(legal.treatment(semantic_type), redaction_policy.PRESERVE)
            self.assertEqual(strict.treatment(semantic_type), redaction_policy.PSEUDONYMIZE)

    def test_the_amount_option_switches_only_amounts(self) -> None:
        replaced = redaction_policy.build_profile(redaction_policy.LEGAL_CONTRACT_REVIEW, preserve_amounts=False)
        kept = redaction_policy.build_profile(redaction_policy.LEGAL_CONTRACT_REVIEW, preserve_amounts=True)
        changed = {
            semantic_type
            for semantic_type in set(replaced.treatments) | set(kept.treatments)
            if replaced.treatment(semantic_type) != kept.treatment(semantic_type)
        }
        self.assertEqual(changed, {"AMOUNT", "PENALTY_AMOUNT", "LIABILITY_CAP"})
        self.assertEqual(kept.treatment("PENALTY_AMOUNT"), redaction_policy.PRESERVE)
        self.assertEqual(replaced.treatment("PENALTY_AMOUNT"), redaction_policy.PSEUDONYMIZE)

    def test_strict_privacy_ignores_the_amount_option(self) -> None:
        strict = redaction_policy.build_profile(redaction_policy.STRICT_PRIVACY, preserve_amounts=True)
        self.assertEqual(strict.treatment("PENALTY_AMOUNT"), redaction_policy.PSEUDONYMIZE)


class ProfileConfigurationTests(unittest.TestCase):
    def test_the_default_is_legal_contract_review_with_amounts_replaced(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(redaction_policy.PROFILE_ENV, None)
            os.environ.pop(redaction_policy.PRESERVE_AMOUNTS_ENV, None)
            profile = redaction_policy.build_profile()
        self.assertEqual(profile.name, redaction_policy.LEGAL_CONTRACT_REVIEW)
        self.assertFalse(profile.preserve_amounts)

    def test_the_environment_selects_the_profile_and_the_amount_option(self) -> None:
        with mock.patch.dict(
            os.environ,
            {redaction_policy.PROFILE_ENV: "strict_privacy", redaction_policy.PRESERVE_AMOUNTS_ENV: "1"},
        ):
            self.assertEqual(redaction_policy.build_profile().name, redaction_policy.STRICT_PRIVACY)
        with mock.patch.dict(os.environ, {redaction_policy.PRESERVE_AMOUNTS_ENV: "true"}):
            self.assertTrue(redaction_policy.build_profile(redaction_policy.LEGAL_CONTRACT_REVIEW).preserve_amounts)

    def test_an_unknown_profile_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            redaction_policy.build_profile("LEGAL_CONTRACT_REVEIW")


class AnalysisAttachesThePolicyTests(unittest.TestCase):
    TEXT = (
        "Kestane Yazılım Anonim Şirketi adına Selin Arıkan (TCKN: 11111111110) imzalamıştır. "
        "İhlal halinde 250.000 TL cezai şart ödenir. Şüpheli hakkında suç duyurusu bulunmaktadır."
    )

    def test_every_finding_carries_a_decision_and_the_profile_is_recorded(self) -> None:
        profile = redaction_policy.build_profile(redaction_policy.LEGAL_CONTRACT_REVIEW, preserve_amounts=True)
        result = analyze_privacy("x.txt", self.TEXT, profile=profile)
        self.assertEqual(result["redaction_policy"]["profile"], redaction_policy.LEGAL_CONTRACT_REVIEW)
        self.assertTrue(result["redaction_policy"]["preserve_amounts"])
        for finding in result["risk_map"]:
            with self.subTest(sample=finding["sample"]):
                self.assertIn(finding["treatment"], redaction_policy.TREATMENTS)
                self.assertTrue(finding["replacement"].startswith("["))
                # The recommendation names exactly what an approval writes.
                self.assertIn(finding["replacement"], finding["recommended_action"])

        by_category = {f["category"]: f for f in result["risk_map"]}
        self.assertEqual(by_category["money_amount"]["treatment"], redaction_policy.PRESERVE)
        self.assertIn("Retain it to keep the exact text", by_category["money_amount"]["recommended_action"])
        self.assertEqual(by_category["turkish_national_id"]["treatment"], redaction_policy.REDACT)
        self.assertEqual(by_category["turkish_national_id"]["replacement"], "[NATIONAL_ID_1]")
        self.assertEqual(by_category["criminal_allegation"]["treatment"], redaction_policy.REVIEW)
        self.assertTrue(by_category["criminal_allegation"]["replacement"].endswith("_REDACTED]"))

    def test_the_stored_replacement_is_the_policys(self) -> None:
        """app.replacement_for_finding is the policy module's, not a second copy."""
        import tests.env_setup  # noqa: F401
        import app as app_module
        import classify

        self.assertIs(app_module.replacement_for_finding, redaction_policy.replacement_for_finding)
        self.assertIs(classify.replacement_for_finding, redaction_policy.replacement_for_finding)


if __name__ == "__main__":
    unittest.main()
