"""Reviewed redacted export (DOCX/PDF) and export QA routes.

The "Preview TXT / Preview DOCX (lower assurance)" route that used to live
here is deleted, not disabled. It served an ungated `<name>_redacted.txt`
headed "PRIVACY-REVIEWED REDACTED EXPORT" whose body, before any review
decision, was the complete unredacted source -- a file that reads as a
deliverable and is not one. Reverting it to its pre-resolver behaviour was
considered and rejected: that body redacted everything DETECTED, so it lied
in the other direction whenever a reviewer dismissed a finding, telling the
lawyer something was removed that the shipped file still contained. There is
no honest and useful version of a rebuilt, non-layout-preserving artifact
named like the deliverable, so it is gone. The review canvas
(`GET /api/document/<id>/redaction-plan`) is where a lawyer sees the
post-decision text; `redacted-export` below is the only download, and the
matter batch archive builds each file through the same `build_reviewed_*`
functions it uses.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from app import (
    approved_findings_for_document,
    anchored_redaction_inputs,
    audit_and_commit,
    audited_error,
    document_source_text,
    get_db,
    get_exportable_docx,
    get_pdf_document,
    latest_redacted_pdf_artifact,
    pdf_export_blockers,
    record_document_artifact,
    redaction_targets_for_document,
    require_roles,
    resolve_document_path,
    retained_sample_count_for_document,
    reviewed_pdf_regions,
    save_redacted_pdf_artifact_file,
    sensitive_samples_for_document,
    stored_replacement,
)
from legal_analyzer.docx_quality import analyze_docx_export_quality
from legal_analyzer.docx_redactor import DETECTOR_SOURCES, normalize_style, redact_docx
from legal_analyzer.export_scan import leak_parts, leaked_item_count, scan_docx, scan_pdf
from legal_analyzer.pdf_redactor import analyze_pdf_redaction_quality, redact_pdf
from legal_analyzer.redaction_policy import review_replacement, reviewer_default_replacement

export_bp = Blueprint("export", __name__)


@dataclass(frozen=True)
class ReviewedExport:
    """One document's reviewed redacted file, built behind its export gate."""

    data: bytes
    download_name: str
    mimetype: str


DOCX_MIMETYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def export_stem(filename: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in Path(filename).stem)[:120]


def refuse_leaking_export(doc_id: int, action: str, leaks: list[dict]):
    """The audited 409 for a built file the output scan found approved items in.

    The message and the audit row name parts and counts only: a leak never
    carries its sample, and neither may anything written about it.
    """
    parts = leak_parts(leaks)
    items = leaked_item_count(leaks)
    unreadable = sum(1 for leak in leaks if leak["finding_id"] is None)
    shown = ", ".join(parts[:5]) + (", …" if len(parts) > 5 else "")
    if items:
        message = f"Export refused: {items} approved item(s) still appear in the file ({shown})."
    else:
        message = f"Export refused: {unreadable} part(s) of the file could not be read to check ({shown})."
    return audited_error(
        get_db(),
        message,
        409,
        action,
        doc_id,
        metadata={
            "reason": "output_scan",
            "item_count": items,
            "leak_count": len(leaks),
            "unreadable_part_count": unreadable,
            "parts": parts[:25],
        },
    )


def written_replacements(findings, redaction_style: str) -> set[str]:
    """The replacements this export writes for these findings that the system generated.

    The mask, the reviewer default, and a detector or OCR finding's own
    placeholder (or its removed-passage form). A replacement the reviewer typed
    is never one of them, whatever its shape: it is free text, and it can carry
    the very name it replaces.
    """
    if normalize_style(redaction_style) == "mask":
        return {"[REDACTED]"}
    written = set()
    for row in findings:
        generated = {"[REDACTED]", reviewer_default_replacement(row["category"])}
        if row["source"] in DETECTOR_SOURCES and row["placeholder"]:
            generated |= {row["placeholder"], review_replacement(row["placeholder"])}
        replacement = stored_replacement(row)
        if replacement in generated:
            written.add(replacement)
    return written


def verification_status(report: dict) -> str:
    """The finding_evidence stamp a QA report earns: verified only on a clean pass.

    One rule for DOCX and PDF. Each format used to write its own condition, and
    the DOCX one stamped "redacted_verified" on a report whose own
    overall_status was "fail".
    """
    return "redacted_verified" if report.get("overall_status") == "pass" else "qa_warning"


def build_reviewed_pdf(doc_id: int):
    """The reviewed PDF, or the refusal the export endpoint returns for it.

    The single download and the matter batch export both come through here, so
    a batch cannot build a file the document's own gate would refuse -- or one
    the output scan finds an approved item in.
    """
    conn = get_db()
    doc = get_pdf_document(conn, doc_id, "export.redacted_pdf")
    if not isinstance(doc, sqlite3.Row):
        return doc
    blockers = pdf_export_blockers(conn, doc)
    if blockers:
        return audited_error(
            conn,
            "Reviewed PDF export is blocked until PDF redaction review gates pass.",
            409,
            "export.redacted_pdf",
            doc_id,
            metadata={"blocker_count": len(blockers), "blockers": blockers},
        )
    regions = reviewed_pdf_regions(conn, doc_id)
    targets = redaction_targets_for_document(conn, doc_id)
    approved_findings = approved_findings_for_document(conn, doc_id)
    conn.close()

    source_path = resolve_document_path(doc["filepath"])
    if not source_path.exists():
        return jsonify({"error": f"Source PDF was not found: {source_path}"}), 404
    safe_stem = export_stem(doc["filename"]) or "document"
    data = redact_pdf(source_path, regions, targets)
    leaks = scan_pdf(data, approved_findings)
    if leaks:
        return refuse_leaking_export(doc_id, "export.redacted_pdf", leaks)
    saved_path = save_redacted_pdf_artifact_file(doc_id, safe_stem, data)
    artifact_conn = get_db()
    audit_and_commit(
        artifact_conn, "export.redacted_pdf", document_id=doc_id, metadata={"region_count": len(regions), "style": "black_box"}
    )
    record_document_artifact(
        artifact_conn,
        doc_id,
        "redacted_pdf",
        "Reviewed PDF redaction",
        file_path=str(saved_path),
        metadata={"region_count": len(regions), "byte_size": len(data)},
        export_style="black_box",
    )
    artifact_conn.commit()
    artifact_conn.close()
    return ReviewedExport(data, f"{safe_stem}_redacted.pdf", "application/pdf")


def build_reviewed_docx(doc_id: int, redaction_style: str):
    """The reviewed DOCX, or the refusal the export endpoint returns for it."""
    conn = get_db()
    doc = get_exportable_docx(conn, doc_id, "export.redacted_docx")
    if not isinstance(doc, sqlite3.Row):
        return doc
    targets = redaction_targets_for_document(conn, doc_id)
    anchored, retained = anchored_redaction_inputs(conn, doc_id, document_source_text(conn, doc_id, doc)[0])
    approved_findings = approved_findings_for_document(conn, doc_id)
    conn.close()

    source_path = resolve_document_path(doc["filepath"])
    if not source_path.exists():
        return jsonify({"error": f"Source DOCX was not found: {source_path}"}), 404

    safe_stem = export_stem(doc["filename"])
    data = redact_docx(source_path, anchored, style=redaction_style, retained=retained)
    leaks = scan_docx(data, approved_findings, written_replacements(approved_findings, redaction_style))
    if leaks:
        return refuse_leaking_export(doc_id, "export.redacted_docx", leaks)
    style_suffix = "" if redaction_style == "placeholder" else "_mask"
    artifact_conn = get_db()
    audit_and_commit(
        artifact_conn,
        "export.redacted_docx",
        document_id=doc_id,
        metadata={"target_count": len(targets), "style": redaction_style},
    )
    record_document_artifact(
        artifact_conn,
        doc_id,
        "redacted_docx",
        "Reviewed DOCX redaction",
        metadata={"target_count": len(targets), "byte_size": len(data)},
        export_style=redaction_style,
    )
    artifact_conn.commit()
    artifact_conn.close()
    return ReviewedExport(data, f"{safe_stem}_redacted{style_suffix}.docx", DOCX_MIMETYPE)


def send_reviewed_export(result):
    if not isinstance(result, ReviewedExport):
        return result
    return send_file(
        BytesIO(result.data),
        as_attachment=True,
        download_name=result.download_name,
        mimetype=result.mimetype,
    )


@export_bp.route("/api/document/<int:doc_id>/redacted-export")
@require_roles("reviewer", "admin")
def api_document_redacted_export(doc_id: int):
    export_format = request.args.get("format", "docx").lower()
    redaction_style = request.args.get("style", "placeholder").lower()
    if export_format == "pdf":
        if redaction_style not in {"black_box", "placeholder"}:
            conn = get_db()
            return audited_error(
                conn,
                "Unsupported PDF redaction style. Use black_box.",
                400,
                "export.redacted_pdf",
                doc_id,
                metadata={"style": redaction_style},
            )
        return send_reviewed_export(build_reviewed_pdf(doc_id))

    if export_format != "docx":
        conn = get_db()
        return audited_error(conn, "Actual redacted export supports reviewed DOCX and PDF only.", 415, "export.redacted_docx", doc_id)
    if redaction_style not in {"placeholder", "mask"}:
        conn = get_db()
        return audited_error(
            conn,
            "Unsupported DOCX redaction style. Use placeholder or mask.",
            400,
            "export.redacted_docx",
            doc_id,
            metadata={"style": redaction_style},
        )
    return send_reviewed_export(build_reviewed_docx(doc_id, redaction_style))


@export_bp.route("/api/document/<int:doc_id>/redacted-export/qa")
@require_roles("reviewer", "admin")
def api_document_redacted_export_qa(doc_id: int):
    export_format = request.args.get("format", "docx").lower()
    redaction_style = request.args.get("style", "placeholder").lower()
    if export_format == "pdf":
        if redaction_style not in {"black_box", "placeholder"}:
            conn = get_db()
            return audited_error(
                conn,
                "Unsupported PDF redaction style. Use black_box.",
                400,
                "export.redacted_pdf_qa",
                doc_id,
                metadata={"style": redaction_style},
            )
        conn = get_db()
        doc = get_pdf_document(conn, doc_id, "export.redacted_pdf_qa")
        if not isinstance(doc, sqlite3.Row):
            return doc
        blockers = pdf_export_blockers(conn, doc)
        if blockers:
            return audited_error(
                conn,
                "PDF export QA is blocked until PDF redaction review gates pass.",
                409,
                "export.redacted_pdf_qa",
                doc_id,
                metadata={"blocker_count": len(blockers), "blockers": blockers},
            )
        regions = reviewed_pdf_regions(conn, doc_id)
        region_state = {
            key: conn.execute(
                "SELECT COUNT(*) FROM pdf_redaction_regions WHERE document_id = ? AND review_status = ?",
                (doc_id, key),
            ).fetchone()[0]
            for key in ("pending", "approved", "rejected")
        }
        artifact = latest_redacted_pdf_artifact(conn, doc_id)
        # Same argument as the DOCX QA below: leakage measured only against the
        # approved regions cannot see a finding no region covers, so a second
        # occurrence of the same identifier elsewhere in the PDF was never
        # searched for at all. Retained/dismissed findings are excluded by
        # sensitive_samples_for_document, since for those the text remaining is
        # the expected outcome.
        pdf_sensitive_samples = sensitive_samples_for_document(conn, doc_id)
        pdf_retained_count = retained_sample_count_for_document(conn, doc_id)
        targets = redaction_targets_for_document(conn, doc_id)
        approved_findings = approved_findings_for_document(conn, doc_id)
        audit_and_commit(conn, "export.redacted_pdf_qa", document_id=doc_id, metadata={"region_count": len(regions), "style": "black_box"})
        conn.close()

        source_path = resolve_document_path(doc["filepath"])
        if not source_path.exists():
            return jsonify({"error": f"Source PDF was not found: {source_path}"}), 404
        artifact_path = Path(artifact["file_path"]) if artifact else None
        if artifact_path and artifact_path.exists():
            data = artifact_path.read_bytes()
            qa_target = "saved_artifact"
        else:
            data = redact_pdf(source_path, regions, targets)
            qa_target = "freshly_generated"
        report = analyze_pdf_redaction_quality(
            source_path,
            data,
            regions,
            region_state=region_state,
            qa_target=qa_target,
            sensitive_samples=pdf_sensitive_samples,
            retained_count=pdf_retained_count,
            output_leaks=scan_pdf(data, approved_findings),
        )
        report["document_id"] = doc_id
        artifact_conn = get_db()
        artifact_conn.execute(
            """
            UPDATE finding_evidence
            SET verification_status = ?
            WHERE document_id = ?
            """,
            (verification_status(report), doc_id),
        )
        record_document_artifact(
            artifact_conn,
            doc_id,
            "pdf_qa",
            "PDF export QA",
            metadata={
                "overall_status": report["overall_status"],
                "leakage_count": report["leakage_count"],
                "retained_count": report["retained_count"],
                "region_count": report["region_count"],
                "qa_target": report["qa_target"],
                "annotation_count": report["annotation_count"],
            },
            export_style="black_box",
            qa_status=report["overall_status"],
        )
        artifact_conn.commit()
        artifact_conn.close()
        return jsonify(report)

    if export_format != "docx":
        conn = get_db()
        return audited_error(conn, "Reviewed export QA supports DOCX and PDF only.", 415, "export.redacted_docx_qa", doc_id)
    if redaction_style not in {"placeholder", "mask"}:
        conn = get_db()
        return audited_error(
            conn,
            "Unsupported DOCX redaction style. Use placeholder or mask.",
            400,
            "export.redacted_docx_qa",
            doc_id,
            metadata={"style": redaction_style},
        )

    conn = get_db()
    doc = get_exportable_docx(conn, doc_id, "export.redacted_docx_qa")
    if not isinstance(doc, sqlite3.Row):
        return doc
    targets = redaction_targets_for_document(conn, doc_id)
    # Leakage is measured against every finding whose text remaining would be
    # unexpected, not just the approved targets: a finding that was never made a
    # target is exactly the one the redactor cannot have removed. Findings the
    # reviewer deliberately retained are excluded from that set and counted
    # separately, so the report states them instead of reading as a clean pass.
    sensitive_samples = sensitive_samples_for_document(conn, doc_id)
    retained_count = retained_sample_count_for_document(conn, doc_id)
    approved_findings = approved_findings_for_document(conn, doc_id)
    anchored, retained = anchored_redaction_inputs(conn, doc_id, document_source_text(conn, doc_id, doc)[0])
    audit_and_commit(
        conn,
        "export.redacted_docx_qa",
        document_id=doc_id,
        metadata={"target_count": len(targets), "style": redaction_style},
    )
    conn.close()

    source_path = resolve_document_path(doc["filepath"])
    if not source_path.exists():
        return jsonify({"error": f"Source DOCX was not found: {source_path}"}), 404

    data = redact_docx(source_path, anchored, style=redaction_style, retained=retained)
    report = analyze_docx_export_quality(
        source_path,
        data,
        targets,
        sensitive_samples=sensitive_samples,
        retained_count=retained_count,
        output_leaks=scan_docx(data, approved_findings, written_replacements(approved_findings, redaction_style)),
    )
    report["style"] = redaction_style
    report["format"] = "docx"
    report["document_id"] = doc_id
    artifact_conn = get_db()
    artifact_conn.execute(
        """
        UPDATE finding_evidence
        SET verification_status = ?
        WHERE document_id = ?
        """,
        (verification_status(report), doc_id),
    )
    record_document_artifact(
        artifact_conn,
        doc_id,
        "docx_qa",
        "DOCX export QA",
        metadata={
            "overall_status": report["overall_status"],
            "leakage_count": report["leakage_count"],
            "retained_count": report["retained_count"],
            "target_count": report.get("target_count", len(targets)),
        },
        export_style=redaction_style,
        qa_status=report["overall_status"],
    )
    artifact_conn.commit()
    artifact_conn.close()
    return jsonify(report)
