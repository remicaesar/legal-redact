"""Document review-state and privacy-finding review routes."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from flask import Blueprint, jsonify, request

from app import (
    audit_and_commit,
    audited_error,
    count_undecided_findings,
    get_db,
    has_unsafe_html_chars,
    ocr_blocks_redaction,
    record_document_artifact,
    refresh_document_state,
    require_roles,
    review_gate_counts,
    revoke_review_approval,
)
from blueprints.documents import api_document_detail
from legal_analyzer.privacy import (
    CATEGORY_EXPLANATIONS,
    FINDING_REVIEW_STATUSES,
    RISK_ORDER,
    refresh_release_state,
)
from legal_analyzer.redaction_policy import reviewer_default_replacement

review_bp = Blueprint("review", __name__)

# What a reviewer's finding-level action writes to privacy_findings.review_status.
#
# 'reject' is kept as an alias for 'dismissed' so existing API clients do not
# break. It maps to the permissive decision deliberately: before the split,
# 'rejected' was overwhelmingly used to clear false positives, and mapping a
# bare 'reject' to the blocking 'retained' would have silently changed what
# every existing caller's request means. Choosing to keep a real identifier is
# now an explicit action a caller has to name.
FINDING_ACTION_STATUSES = {
    "approve": "approved",
    "dismiss": "dismissed",
    "retain": "retained",
    "reject": "dismissed",
    "pending": "pending",
}

# replacement_text is reviewer free text that is written into the redacted
# export and rendered back into the studio UI, including inside HTML
# attributes. Reject the characters that break out of an attribute rather than
# silently rewriting text that ends up in a legal document.
UNSAFE_REPLACEMENT_TEXT_ERROR = "Replacement text must not contain quotes or angle brackets."


def count_pending_pdf_boxes(conn: sqlite3.Connection, doc: sqlite3.Row) -> int:
    if doc["file_extension"] != ".pdf":
        return 0
    return conn.execute(
        "SELECT COUNT(*) FROM pdf_redaction_regions WHERE document_id = ? AND review_status = 'pending'",
        (doc["id"],),
    ).fetchone()[0]


@review_bp.route("/api/document/<int:doc_id>/review", methods=["POST"])
@require_roles("reviewer", "admin")
def api_document_review(doc_id: int):
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    # 'enable_auto_mode'/'disable_auto_mode' used to be here. The enable arm set
    # a stored flag the release gate and the reviewed-DOCX export accepted in
    # place of a human approval, with no UI, no indicator and no audit surface.
    # They are rejected as unsupported rather than silently ignored: a caller
    # that still sends one must be told nothing happened, not handed a 200 and a
    # document that looks reviewed.
    if action not in {"mark_redacted", "approve", "reject", "needs_ocr", "reset"}:
        return jsonify({"error": "Unsupported review action"}), 400

    conn = get_db()
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not doc:
        conn.close()
        return jsonify({"error": "Document not found"}), 404

    redaction_completed = bool(doc["redaction_completed"])
    human_review_approved = bool(doc["human_review_approved"])
    review_status = doc["review_status"] or "pending_review"
    ocr_status = doc["ocr_status"] or "not_required"

    if action == "mark_redacted":
        if ocr_blocks_redaction(ocr_status):
            return audited_error(
                conn,
                "OCR output must be accepted or not required before marking redaction complete.",
                409,
                f"document_review.{action}",
                doc_id,
            )
        # Fails closed the same way as review_gate_counts() in app.py: a
        # finding only counts as reviewed once it has a real terminal status,
        # or redaction_completed could flip to True and unblock export with a
        # finding nobody decided.
        unreviewed = count_undecided_findings(conn, doc_id)
        if unreviewed:
            return audited_error(
                conn,
                "Every finding must be decided — approved, dismissed as not sensitive, or retained "
                "unredacted — before marking redaction complete.",
                409,
                f"document_review.{action}",
                doc_id,
                {"unreviewed_findings": unreviewed},
            )
        pending_pdf_regions = count_pending_pdf_boxes(conn, doc)
        if pending_pdf_regions:
            return audited_error(
                conn,
                "All PDF redaction boxes must be approved or rejected before marking redaction complete.",
                409,
                f"document_review.{action}",
                doc_id,
                {"pending_pdf_regions": pending_pdf_regions},
            )
        redaction_completed = True
        review_status = "redaction_complete"
    elif action == "approve":
        # The sign-off attests a decided document, so it comes last. The
        # studio only offers Approve after "Mark redaction complete", but the
        # API took it at any time -- and an approval given before the
        # decisions then unlocked export once they were made. Findings are
        # re-read rather than trusting redaction_completed, which a finding
        # reverted to pending afterwards does not clear.
        undecided = count_undecided_findings(conn, doc_id)
        if undecided:
            return audited_error(
                conn,
                "Every finding must be decided — approved, dismissed as not sensitive, or retained "
                "unredacted — before the review can be approved.",
                409,
                f"document_review.{action}",
                doc_id,
                {"unreviewed_findings": undecided},
            )
        pending_pdf_regions = count_pending_pdf_boxes(conn, doc)
        if pending_pdf_regions:
            return audited_error(
                conn,
                "All PDF redaction boxes must be approved or rejected before the review can be approved.",
                409,
                f"document_review.{action}",
                doc_id,
                {"pending_pdf_regions": pending_pdf_regions},
            )
        if not redaction_completed:
            return audited_error(
                conn,
                "Mark redaction complete before approving the review.",
                409,
                f"document_review.{action}",
                doc_id,
            )
        human_review_approved = True
        review_status = "reviewed"
    elif action == "reject":
        human_review_approved = False
        review_status = "rejected"
    elif action == "needs_ocr":
        human_review_approved = False
        redaction_completed = False
        review_status = "needs_ocr"
        ocr_status = "queued"
    elif action == "reset":
        redaction_completed = False
        human_review_approved = False
        review_status = "needs_ocr" if doc["extraction_status"] in {"Partial", "Failed"} else "pending_review"
        ocr_status = "queued" if doc["extraction_status"] in {"Partial", "Failed"} else "not_required"

    profile = refresh_release_state(
        json.loads(doc["privacy_profile"] or "{}"),
        redaction_completed=redaction_completed,
        human_review_approved=human_review_approved,
        ocr_status=ocr_status,
        **review_gate_counts(conn, doc_id),
    )
    gate_allowed = bool(profile["external_llm_gate"]["allowed"])
    if action == "approve":
        review_status = "approved_for_external_llm" if gate_allowed else "reviewed_blocked"

    conn.execute(
        """
        UPDATE documents
        SET privacy_profile = ?,
            language = ?,
            external_llm_readiness = ?,
            human_review_required = ?,
            redaction_status = ?,
            redaction_completed = ?,
            human_review_approved = ?,
            review_status = ?,
            ocr_status = ?,
            reviewed_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            json.dumps(profile, ensure_ascii=False),
            profile["external_llm_gate"]["language_coverage"]["language"],
            profile["external_llm_readiness"],
            1 if profile["human_review_required"] else 0,
            profile["redaction_status"],
            1 if redaction_completed else 0,
            1 if human_review_approved else 0,
            review_status,
            ocr_status,
            doc_id,
        ),
    )
    if action == "mark_redacted":
        finding_counts = conn.execute(
            """
            SELECT
                SUM(CASE WHEN review_status = 'approved' THEN 1 ELSE 0 END) AS approved_count,
                SUM(CASE WHEN review_status = 'dismissed' THEN 1 ELSE 0 END) AS dismissed_count,
                SUM(CASE WHEN review_status = 'retained' THEN 1 ELSE 0 END) AS retained_count,
                SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN risk = 'CRITICAL' THEN 1 ELSE 0 END) AS critical_count
            FROM privacy_findings
            WHERE document_id = ?
            """,
            (doc_id,),
        ).fetchone()
        record_document_artifact(
            conn,
            doc_id,
            "review_snapshot",
            "Reviewed finding snapshot",
            metadata={
                "review_status": review_status,
                "approved_findings": finding_counts["approved_count"] or 0,
                "dismissed_findings": finding_counts["dismissed_count"] or 0,
                # The dangerous decision, on the permanent artifact timeline: a
                # nonzero count here means the export carries identifiers a
                # reviewer chose to leave in.
                "retained_findings": finding_counts["retained_count"] or 0,
                "pending_findings": finding_counts["pending_count"] or 0,
                "critical_findings": finding_counts["critical_count"] or 0,
            },
        )
    audit_and_commit(conn, f"document_review.{action}", document_id=doc_id, metadata={"review_status": review_status})
    conn.close()
    return api_document_detail(doc_id)


@review_bp.route("/api/finding/<int:finding_id>/review", methods=["POST"])
@require_roles("reviewer", "admin")
def api_finding_review(finding_id: int):
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    if action not in set(FINDING_ACTION_STATUSES) | {"update"}:
        return jsonify({"error": "Unsupported finding action"}), 400

    conn = get_db()
    finding = conn.execute("SELECT * FROM privacy_findings WHERE id = ?", (finding_id,)).fetchone()
    if not finding:
        conn.close()
        return jsonify({"error": "Finding not found"}), 404

    previous_status = finding["review_status"]
    review_status = previous_status or "pending"
    replacement_text = payload.get("replacement_text", finding["replacement_text"])
    reviewer_note = payload.get("reviewer_note", finding["reviewer_note"])
    if replacement_text and has_unsafe_html_chars(str(replacement_text)):
        conn.close()
        return jsonify({"error": UNSAFE_REPLACEMENT_TEXT_ERROR}), 400
    if action in FINDING_ACTION_STATUSES:
        review_status = FINDING_ACTION_STATUSES[action]
    elif action == "update":
        requested_status = payload.get("review_status", review_status)
        if requested_status not in FINDING_REVIEW_STATUSES:
            conn.close()
            return jsonify({
                "error": f"Unsupported review_status '{requested_status}'. Must be one of: "
                         f"{', '.join(sorted(FINDING_REVIEW_STATUSES))}.",
            }), 400
        review_status = requested_status

    conn.execute(
        """
        UPDATE privacy_findings
        SET review_status = ?, replacement_text = ?, reviewer_note = ?
        WHERE id = ?
        """,
        (review_status, replacement_text, reviewer_note, finding_id),
    )
    conn.execute(
        """
        UPDATE pseudonym_mappings
        SET replacement_text = ?
        WHERE finding_id = ?
        """,
        (replacement_text, finding_id),
    )
    # A changed decision is not what the sign-off approved; restating the same
    # one is no change and leaves it standing.
    decision_changed = (
        review_status != previous_status or replacement_text != finding["replacement_text"]
    )
    sign_off_cleared = decision_changed and revoke_review_approval(conn, finding["document_id"])
    refresh_document_state(conn, finding["document_id"])
    audit_and_commit(
        conn,
        f"finding_review.{action}",
        document_id=finding["document_id"],
        metadata={
            "finding_id": finding_id,
            "review_status": review_status,
            "category": finding["category"],
            "sign_off_cleared": sign_off_cleared,
        },
    )
    conn.close()
    return jsonify({"ok": True, "finding_id": finding_id, "review_status": review_status})


@review_bp.route("/api/document/<int:doc_id>/findings/review-batch", methods=["POST"])
@require_roles("reviewer", "admin")
def api_finding_batch_review(doc_id: int):
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    if action not in FINDING_ACTION_STATUSES:
        return jsonify({"error": "Unsupported batch finding action"}), 400

    conn = get_db()
    doc = conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not doc:
        conn.close()
        return jsonify({"error": "Document not found"}), 404

    categories = [str(item) for item in payload.get("categories", []) if item]
    risks = [str(item).upper() for item in payload.get("risks", []) if item]
    only_pending = bool(payload.get("only_pending", True))
    finding_ids = [int(item) for item in payload.get("finding_ids", []) if str(item).isdigit()]

    query = "SELECT id FROM privacy_findings WHERE document_id = ?"
    params: list[object] = [doc_id]
    if finding_ids:
        query += f" AND id IN ({','.join('?' for _ in finding_ids)})"
        params.extend(finding_ids)
    if categories:
        query += f" AND category IN ({','.join('?' for _ in categories)})"
        params.extend(categories)
    if risks:
        query += f" AND risk IN ({','.join('?' for _ in risks)})"
        params.extend(risks)
    if only_pending:
        query += " AND review_status = 'pending'"

    ids = [row["id"] for row in conn.execute(query, params).fetchall()]
    if not ids:
        audit_and_commit(
            conn,
            f"finding_batch.{action}",
            document_id=doc_id,
            result="noop",
            metadata={"categories": categories, "risks": risks, "only_pending": only_pending},
        )
        conn.close()
        return jsonify({"ok": True, "updated": 0})

    placeholders = ",".join("?" for _ in ids)
    review_status = FINDING_ACTION_STATUSES[action]
    changed = conn.execute(
        f"""
        UPDATE privacy_findings SET review_status = ?
        WHERE document_id = ? AND id IN ({placeholders}) AND review_status IS NOT ?
        """,
        (review_status, doc_id, *ids, review_status),
    ).rowcount
    sign_off_cleared = bool(changed) and revoke_review_approval(conn, doc_id)
    refresh_document_state(conn, doc_id)
    audit_and_commit(
        conn,
        f"finding_batch.{action}",
        document_id=doc_id,
        metadata={
            "updated": len(ids),
            # Recorded so a batch 'retain' — the one decision that leaves real
            # identifiers in the export — is traceable in the audit log, and so
            # the 'reject' alias's resolved status is visible rather than implied.
            "review_status": review_status,
            "categories": categories,
            "risks": risks,
            "only_pending": only_pending,
            "sign_off_cleared": sign_off_cleared,
        },
    )
    conn.close()
    return jsonify({"ok": True, "updated": len(ids), "review_status": review_status})


@review_bp.route("/api/document/<int:doc_id>/findings", methods=["POST"])
@require_roles("reviewer", "admin")
def api_add_finding(doc_id: int):
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    category = (payload.get("category") or "manual_sensitive_text").strip()
    replacement_text = (payload.get("replacement_text") or reviewer_default_replacement(category)).strip()
    risk = (payload.get("risk") or "HIGH").strip().upper()
    reviewer_note = payload.get("reviewer_note")
    if not text:
        return jsonify({"error": "Missing exact text for reviewer-added finding"}), 400
    if category not in CATEGORY_EXPLANATIONS:
        return jsonify({
            "error": f"Unsupported category '{category}'. Must be one of: {', '.join(sorted(CATEGORY_EXPLANATIONS))}.",
        }), 400
    if risk not in RISK_ORDER:
        return jsonify({
            "error": f"Unsupported risk level '{risk}'. Must be one of: {', '.join(sorted(RISK_ORDER))}.",
        }), 400
    if has_unsafe_html_chars(replacement_text):
        return jsonify({"error": UNSAFE_REPLACEMENT_TEXT_ERROR}), 400

    conn = get_db()
    doc = conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not doc:
        conn.close()
        return jsonify({"error": "Document not found"}), 404

    cursor = conn.execute(
        """
        INSERT INTO privacy_findings (
            document_id, category, sample, risk, recommended_action, placeholder,
            replacement_text, review_status, reviewer_note, source, fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'added_by_reviewer', ?, 'reviewer', ?)
        """,
        (
            doc_id,
            category,
            text,
            risk,
            "Reviewer-added exact text redaction",
            replacement_text,
            replacement_text,
            reviewer_note,
            hashlib.sha256(f"{doc_id}:{category}:{text}".casefold().encode("utf-8")).hexdigest()[:12],
        ),
    )
    finding_id = cursor.lastrowid
    conn.execute(
        """
        INSERT INTO pseudonym_mappings (
            document_id, finding_id, original_text, replacement_text, category, restricted
        ) VALUES (?, ?, ?, ?, ?, 1)
        """,
        (doc_id, finding_id, text, replacement_text, category),
    )
    sign_off_cleared = revoke_review_approval(conn, doc_id)
    refresh_document_state(conn, doc_id)
    audit_and_commit(
        conn,
        "finding.add_manual",
        document_id=doc_id,
        metadata={"category": category, "risk": risk, "sign_off_cleared": sign_off_cleared},
    )
    conn.close()
    return jsonify({"ok": True, "finding_id": finding_id})
