"""What to DO with a classified span -- the policy layer.

Detection (``privacy``) says where something is; classification
(``legal_semantics``) says what it is; this module says what should happen to
it under the task the document is being prepared for. The transformation
itself -- replacing approved text in the canvas and the exported file -- stays
in ``redaction_plan`` / ``docx_redactor`` / ``pdf_redactor``, and it still runs
only on findings a person approved. A policy here is a RECOMMENDATION plus the
replacement text an approval will use; it never decides a finding itself, so
no profile can bypass human review.

Before this module the policy was spread across three places and could not be
changed without changing detection: each ``DetectionRule`` carried a fixed
recommended action, ``replacement_for_finding`` (written twice, in ``app.py``
and ``classify.py``) turned a CRITICAL finding whose action mentioned "review"
into ``[X_n_REDACTED]``, and ``build_redacted_preview`` repeated that test. The
replacement is now decided once, here, and every caller reads it.

Treatments:

* PSEUDONYMIZE -- replace with a typed placeholder that is the same for every
  mention of the same entity (``[PERSON_1]``, ``[COMPANY_1]``).
* REDACT       -- replace with a typed placeholder; no linkage is needed but
  the type stays readable (``[NATIONAL_ID_1]``, ``[IBAN_1]``).
* GENERALIZE   -- replace with a placeholder for the KIND of value
  (``[ADDRESS_1]``). Real generalisation (keeping the city of an address) is
  not implemented; this is the same mechanism as PSEUDONYMIZE and says so.
* PRESERVE     -- keep the exact text. For a legal provision that is the only
  treatment there is: provisions are never findings, so nothing replaces them.
  For a reported value (an amount, a deadline) it is the recommendation to
  RETAIN the finding; approving it still replaces only the value.
* REVIEW       -- sensitive context a person must judge (health data,
  criminal allegations, privileged communication). Approving it removes that
  passage as ``[X_n_REDACTED]``.

The identifier treatments are the same in every profile. That is the privacy
floor, and ``tests/test_redaction_policy.py`` fails if any profile maps a
direct identifier to PRESERVE.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import MappingProxyType

from legal_analyzer import legal_semantics as ls

PSEUDONYMIZE = "pseudonymize"
REDACT = "redact"
GENERALIZE = "generalize"
PRESERVE = "preserve"
REVIEW = "review"

TREATMENTS = (PSEUDONYMIZE, REDACT, GENERALIZE, PRESERVE, REVIEW)

STRICT_PRIVACY = "STRICT_PRIVACY"
LEGAL_CONTRACT_REVIEW = "LEGAL_CONTRACT_REVIEW"

# Treatments that hold in every profile. Identifiers first: these are the
# floor, and a profile may only ever be STRICTER than them.
_IDENTIFIER_TREATMENTS = {
    "PERSON_NAME": PSEUDONYMIZE,
    "COMPANY_NAME": PSEUDONYMIZE,
    "LAW_FIRM": PSEUDONYMIZE,
    "CASE_NUMBER": PSEUDONYMIZE,
    "BAR_REGISTRATION_NUMBER": PSEUDONYMIZE,
    "NATIONAL_ID": REDACT,
    "PASSPORT_NUMBER": REDACT,
    "PHONE_NUMBER": REDACT,
    "EMAIL_ADDRESS": REDACT,
    "IBAN": REDACT,
    "BANK_ACCOUNT": REDACT,
    "TAX_NUMBER": REDACT,
    "MERSIS_NUMBER": REDACT,
    "TRADE_REGISTRY_NUMBER": REDACT,
    "SOCIAL_MEDIA_HANDLE": REDACT,
    "VEHICLE_PLATE": REDACT,
    "LAND_PARCEL": REDACT,
    "REVIEWER_MARKED_TEXT": REDACT,
    "BIRTH_DATE": REDACT,
    "STREET_ADDRESS": GENERALIZE,
}

# A specific court or office is pseudonymised under its TYPE, so the reader
# still knows a tax office from a court after the name is gone.
_INSTITUTION_TREATMENTS = {kind: PSEUDONYMIZE for kind in ls.INSTITUTION_TYPES}

_SENSITIVE_TREATMENTS = {kind: REVIEW for kind in ls.SENSITIVE_CONTEXT_TYPES}

# Legally operative language is kept in every profile. It is the substance a
# downstream reviewer -- human or model -- is being asked to analyse, and it
# carries no identity of its own; the identifiers inside it are separate
# findings with their own treatment.
_PROVISION_TREATMENTS = {kind: PRESERVE for kind in ls.LEGAL_PROVISION_TYPES}

_REASONS = {
    "AMOUNT": "the amount is part of the legal obligation it appears in",
    "PENALTY_AMOUNT": "the penalty amount is needed to assess the contractual penalty and the exposure under it",
    "LIABILITY_CAP": "the cap is needed to assess the liability exposure",
    "DATE": "the date may set a limitation period, a deadline or the contract's chronology",
    "SIGNATURE_DATE": "the signature date starts the contract's term and its survival period",
    "DEADLINE": "the deadline is needed to compute the obligation or procedural step it governs",
    "PARTY_ROLE": "the party role is the legal relationship itself and identifies no one on its own",
    "ATTORNEY_CLIENT_COMMUNICATION": "the passage reads as a communication between counsel and client",
    "ATTORNEY_WORK_PRODUCT": "the passage reads as counsel's work product",
    "LEGAL_ADVICE": "the passage reads as legal advice or a legal opinion",
    "LITIGATION_STRATEGY": "the passage reads as litigation or defence strategy",
    "ATTORNEY_CLIENT_PRIVILEGE": "the passage carries an attorney-client privilege marker",
    "SPECIAL_CATEGORY_HEALTH_DATA": "health data is special-category personal data",
    "CRIMINAL_ALLEGATION": "data about alleged offences is special-category personal data",
}


@dataclass(frozen=True)
class Profile:
    """A named task profile: the treatment for every semantic type."""

    name: str
    description: str
    treatments: MappingProxyType = field(repr=False)
    preserve_amounts: bool = False

    def treatment(self, semantic_type: str) -> str:
        # An unrecognised type gets a person's judgement, never PRESERVE:
        # silence fails toward review, the way an unrecognised review status
        # fails toward "unresolved" in the release gate.
        return self.treatments.get(semantic_type, REVIEW)

    def summary(self) -> dict:
        return {
            "profile": self.name,
            "description": self.description,
            "preserve_amounts": self.preserve_amounts,
        }


def _profile(name: str, description: str, contextual: dict[str, str], preserve_amounts: bool = False) -> Profile:
    treatments = {
        **_PROVISION_TREATMENTS,
        **_SENSITIVE_TREATMENTS,
        **_INSTITUTION_TREATMENTS,
        **contextual,
        # Last, so no contextual entry can override the floor.
        **_IDENTIFIER_TREATMENTS,
    }
    return Profile(name, description, MappingProxyType(treatments), preserve_amounts)


def build_profile(name: str = "", preserve_amounts: bool | None = None) -> Profile:
    """The profile called ``name`` (default: the configured one).

    ``preserve_amounts`` switches LEGAL_CONTRACT_REVIEW between recommending
    that an amount be replaced by a typed placeholder (the default) and
    recommending that it be kept exactly. It has no effect on STRICT_PRIVACY.
    An unknown name raises: a typo in the configuration must not silently
    select some other policy.
    """
    name = (name or configured_profile_name()).strip().upper()
    if preserve_amounts is None:
        preserve_amounts = configured_preserve_amounts()
    if name == STRICT_PRIVACY:
        return _profile(
            STRICT_PRIVACY,
            "Strict privacy: every reported value is replaced; legal provisions stay readable.",
            {
                **{kind: PSEUDONYMIZE for kind in ("AMOUNT", "PENALTY_AMOUNT", "LIABILITY_CAP")},
                **{kind: PSEUDONYMIZE for kind in ("DATE", "SIGNATURE_DATE", "DEADLINE")},
                "PARTY_ROLE": PSEUDONYMIZE,
            },
        )
    if name == LEGAL_CONTRACT_REVIEW:
        amounts = PRESERVE if preserve_amounts else PSEUDONYMIZE
        return _profile(
            LEGAL_CONTRACT_REVIEW,
            (
                "Legal contract review: identifiers are pseudonymised or removed, legally operative "
                "clauses stay readable, deadlines and party roles are kept, and "
                + ("amounts are kept." if preserve_amounts else "only the amount itself is replaced.")
            ),
            {
                **{kind: amounts for kind in ("AMOUNT", "PENALTY_AMOUNT", "LIABILITY_CAP")},
                "DATE": PSEUDONYMIZE,
                "SIGNATURE_DATE": PSEUDONYMIZE,
                "DEADLINE": PRESERVE,
                "PARTY_ROLE": PRESERVE,
            },
            preserve_amounts=preserve_amounts,
        )
    raise ValueError(f"Unknown redaction profile {name!r}; expected one of {', '.join(PROFILE_NAMES)}.")


PROFILE_NAMES = (LEGAL_CONTRACT_REVIEW, STRICT_PRIVACY)
DEFAULT_PROFILE = LEGAL_CONTRACT_REVIEW
PROFILE_ENV = "LEGAL_ANALYZER_REDACTION_PROFILE"
PRESERVE_AMOUNTS_ENV = "LEGAL_ANALYZER_PRESERVE_AMOUNTS"


def configured_profile_name() -> str:
    return os.environ.get(PROFILE_ENV, "").strip() or DEFAULT_PROFILE


def configured_preserve_amounts() -> bool:
    return os.environ.get(PRESERVE_AMOUNTS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def semantic_type_of(finding: dict) -> str:
    """The finding's semantic type: set at detection, else read off its category."""
    if finding.get("semantic_type"):
        return finding["semantic_type"]
    category = finding.get("category") or ""
    return (
        ls.IDENTITY_TYPES_BY_CATEGORY.get(category)
        or ls.SENSITIVE_TYPES_BY_CATEGORY.get(category)
        or {"party_role": "PARTY_ROLE", "money_amount": "AMOUNT", "date": "DATE"}.get(category)
        or (ls.ADMINISTRATIVE_AUTHORITY if category == "court_or_authority" else "")
    )


def _label(semantic_type: str) -> str:
    return semantic_type.replace("_", " ").lower()


def decide(finding: dict, profile: Profile) -> dict:
    """The policy decision for one finding: treatment, replacement, recommendation.

    ``placeholder`` is the typed pseudonym detection assigned; the replacement
    an approval writes is derived from it here and nowhere else.
    """
    semantic_type = semantic_type_of(finding)
    treatment = profile.treatment(semantic_type)
    placeholder = finding.get("placeholder") or "[REDACTED]"
    if treatment == REVIEW:
        replacement = review_replacement(placeholder)
    else:
        replacement = placeholder
    reason = _REASONS.get(semantic_type, "")
    if treatment == PSEUDONYMIZE:
        action = f"Pseudonymize: replace with {placeholder}; every mention of the same {_label(semantic_type)} gets the same placeholder."
    elif treatment == REDACT:
        action = f"Remove: replace with {placeholder}."
    elif treatment == GENERALIZE:
        action = f"Generalize: replace with {placeholder}."
    elif treatment == PRESERVE:
        action = (
            f"Keep for legal analysis ({profile.name}): {reason}. Retain it to keep the exact text, "
            f"or approve to replace only this value with {placeholder}."
        )
    else:
        action = (
            f"Flag for human legal review: {reason or 'the passage may be sensitive'}. Approve to replace "
            f"this passage with {replacement}, or dismiss it if it is not {_label(semantic_type)}."
        )
    return {"semantic_type": semantic_type, "treatment": treatment, "replacement": replacement, "recommended_action": action}


def apply(findings: list[dict], profile: Profile) -> list[dict]:
    """Attach the policy decision to every finding, in place, and return them."""
    for finding in findings:
        finding.update(decide(finding, profile))
    return findings


def replacement_for_finding(finding: dict) -> str:
    """The text an APPROVED finding is replaced with. The one definition.

    A finding the policy layer decided carries its replacement. One that
    predates it -- a stored risk map, a hand-built row -- falls back to the
    rule the codebase used before: a CRITICAL finding whose recommended action
    asks for review is removed as ``[X_n_REDACTED]``, anything else takes its
    placeholder.
    """
    if finding.get("replacement"):
        return finding["replacement"]
    if finding.get("risk") == "CRITICAL" and "review" in (finding.get("recommended_action") or "").lower():
        return review_replacement(finding.get("placeholder"))
    return finding.get("placeholder") or "[REDACTED]"


def review_replacement(placeholder: str | None) -> str:
    """The removed-passage form of a placeholder: ``[PERSON_1]`` -> ``[PERSON_1_REDACTED]``."""
    return f"[{(placeholder or '[REDACTED]').strip('[]')}_REDACTED]"


def reviewer_default_replacement(category: str) -> str:
    """What a reviewer-added finding is replaced with when the reviewer types nothing."""
    return f"[{category.upper()}_REDACTED]"
