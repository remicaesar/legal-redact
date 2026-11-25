"""Batch review: decide a matter's findings once, across every document in it.

A matter is the folder here. The batch groups every finding in the matter by
what it says -- its category and its text, Turkish-case-folded -- so a party
named in nine documents is one decision rather than nine. Four things it does
NOT do, each of which is the point:

* It never decides a finding nobody looked at. A group decision applies to the
  group's UNDECIDED findings only; a finding already decided, in the Studio or
  by a per-document override here, keeps its decision. The override is the
  same call scoped to one document, and it does replace that document's
  decisions, because naming the document is the reviewer saying so.
* It never signs a document off. Marking redaction complete and approving the
  review stay one document at a time, through the same
  /api/document/<id>/review endpoint the Studio uses, which refuses while any
  finding is undecided, OCR is unresolved or a PDF box is pending.
* It never changes a document that is signed off. Once redaction is marked
  complete or the review approved, that sign-off covers the decisions as they
  stood; the batch skips the document, an override naming it is refused, and
  changing it means reopening it in its own review in the Studio.
* It never decides a PDF box. Deciding a finding does not decide its boxes
  anywhere in the product (`pdf_export_blockers()` refuses on unreviewed boxes
  for a reason), so a PDF with pending boxes is sent to the Studio.

The batch export builds each document through the same function as its own
download (`build_reviewed_docx` / `build_reviewed_pdf`), so a document whose
export gate refuses is held back from the archive rather than built around.
"""

from __future__ import annotations

import hashlib
import sqlite3
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from flask import Blueprint, jsonify, render_template, render_template_string, request, send_file

from app import (
    audit_and_commit,
    audit_event,
    audited_error,
    export_gate_state,
    get_db,
    ocr_blocks_redaction,
    refresh_document_state,
    require_roles,
    template_user_context,
)
from blueprints.export import ReviewedExport, build_reviewed_docx, build_reviewed_pdf
from blueprints.review import FINDING_ACTION_STATUSES
from legal_analyzer.privacy import CATEGORY_EXPLANATIONS, RESOLVED_REVIEW_STATUSES, RISK_ORDER
from legal_analyzer.turkish_names import tr_fold

batch_bp = Blueprint("batch", __name__)

# The four decisions a reviewer can name. The 'reject' alias the per-document
# routes keep for old clients is not offered: this surface has no old clients,
# and every decision it records is spelled out.
BATCH_ACTIONS = ("approve", "dismiss", "retain", "pending")

# A risk the detector did not grade sorts above CRITICAL, so it is never the
# group's quietest member.
UNGRADED_RISK_RANK = max(RISK_ORDER.values()) + 1


def finding_group_key(category: str, sample: str | None, finding_id: int) -> str:
    """Which group a finding belongs to: one entity, however it was cased.

    Hashed so the key can travel in a request without carrying the text. A
    finding with no text cannot be matched to anything and is its own group.
    """
    if not sample or not sample.strip():
        basis = f"finding\0{finding_id}"
    else:
        basis = f"{category}\0{' '.join(tr_fold(sample).split())}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def is_undecided(review_status: str | None) -> bool:
    """Fails closed like review_gate_counts(): only a real terminal status is a decision."""
    return (review_status or "pending") not in RESOLVED_REVIEW_STATUSES


def matter_or_none(conn: sqlite3.Connection, matter_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT id, name FROM matters WHERE id = ?", (matter_id,)).fetchone()


def matter_documents(conn: sqlite3.Connection, matter_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.*
        FROM document_matters dm
        JOIN documents d ON d.id = dm.document_id
        WHERE dm.matter_id = ?
        ORDER BY d.filename, d.id
        """,
        (matter_id,),
    ).fetchall()


def matter_findings(conn: sqlite3.Connection, matter_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            pf.id, pf.document_id, pf.category, pf.sample, pf.risk, pf.review_status, pf.source,
            (SELECT fe.context FROM finding_evidence fe WHERE fe.finding_id = pf.id ORDER BY fe.id LIMIT 1) AS context,
            (COALESCE(d.redaction_completed, 0) OR COALESCE(d.human_review_approved, 0)) AS locked,
            (COALESCE(d.redaction_completed, 0) AND COALESCE(d.human_review_approved, 0)) AS signed_off
        FROM document_matters dm
        JOIN privacy_findings pf ON pf.document_id = dm.document_id
        JOIN documents d ON d.id = pf.document_id
        WHERE dm.matter_id = ?
        ORDER BY pf.document_id, COALESCE(pf.start_offset, 0), pf.id
        """,
        (matter_id,),
    ).fetchall()
    findings = []
    for row in rows:
        finding = dict(row)
        finding["locked"] = bool(row["locked"])
        finding["signed_off"] = bool(row["signed_off"])
        finding["group_key"] = finding_group_key(row["category"], row["sample"], row["id"])
        findings.append(finding)
    return findings


def group_findings(findings: list[dict], filenames: dict[int, str]) -> list[dict]:
    groups: dict[str, dict] = {}
    for finding in findings:
        group = groups.get(finding["group_key"])
        if group is None:
            group = groups[finding["group_key"]] = {
                "key": finding["group_key"],
                "category": finding["category"],
                "category_label": CATEGORY_EXPLANATIONS.get(finding["category"], {}).get("label", finding["category"]),
                "text": finding["sample"] or "",
                "risk": finding["risk"],
                "occurrences": 0,
                "status_counts": {},
                "documents": {},
            }
        group["occurrences"] += 1
        if RISK_ORDER.get(finding["risk"], UNGRADED_RISK_RANK) > RISK_ORDER.get(group["risk"], UNGRADED_RISK_RANK):
            group["risk"] = finding["risk"]
        status = finding["review_status"] or "pending"
        bucket = "undecided" if is_undecided(status) else status
        if not finding["locked"]:
            # A signed-off document is not the batch's to change, so it does
            # not count towards what the group's buttons would act on.
            group["status_counts"][bucket] = group["status_counts"].get(bucket, 0) + 1
        document = group["documents"].setdefault(
            finding["document_id"],
            {
                "document_id": finding["document_id"],
                "filename": filenames.get(finding["document_id"], ""),
                "context": finding["context"] or "",
                "locked": finding["locked"],
                "signed_off": finding["signed_off"],
                "finding_ids": [],
                "statuses": [],
            },
        )
        document["finding_ids"].append(finding["id"])
        document["statuses"].append("undecided" if is_undecided(status) else status)

    result = []
    for group in groups.values():
        documents = list(group["documents"].values())
        for document in documents:
            distinct = set(document["statuses"])
            document["decision"] = distinct.pop() if len(distinct) == 1 else "mixed"
        undecided = group["status_counts"].get("undecided", 0)
        decided = {status for status in group["status_counts"] if status != "undecided"}
        if undecided:
            decision = "undecided" if not decided else "partly_decided"
        elif not decided:
            decision = "locked"
        else:
            decision = decided.pop() if len(decided) == 1 else "mixed"
        result.append({
            **{key: group[key] for key in ("key", "category", "category_label", "text", "risk", "status_counts", "occurrences")},
            "open_occurrences": sum(group["status_counts"].values()),
            "locked_documents": sum(1 for document in documents if document["locked"]),
            "undecided": undecided,
            "decision": decision,
            "documents": documents,
        })
    # Undecided work first, then the riskiest, then the most widespread.
    result.sort(key=lambda g: (
        0 if g["undecided"] else 1,
        -RISK_ORDER.get(g["risk"], UNGRADED_RISK_RANK),
        -len(g["documents"]),
        g["category"],
        tr_fold(g["text"]),
    ))
    return result


def document_batch_state(conn: sqlite3.Connection, doc: sqlite3.Row) -> dict:
    """Where one document stands in the batch, and the one thing it needs next.

    Read off the same server-side conditions the endpoints refuse on --
    export_gate_state() and the checks api_document_review('mark_redacted')
    makes -- never re-derived in the browser.
    """
    doc_id = doc["id"]
    counts = conn.execute(
        f"""
        SELECT
            COUNT(*) AS finding_count,
            SUM(CASE WHEN COALESCE(review_status, 'pending') NOT IN ({','.join('?' for _ in RESOLVED_REVIEW_STATUSES)})
                THEN 1 ELSE 0 END) AS undecided_count
        FROM privacy_findings
        WHERE document_id = ?
        """,
        (*sorted(RESOLVED_REVIEW_STATUSES), doc_id),
    ).fetchone()
    pending_regions = conn.execute(
        "SELECT COUNT(*) FROM pdf_redaction_regions WHERE document_id = ? AND review_status = 'pending'",
        (doc_id,),
    ).fetchone()[0]
    gate = export_gate_state(conn, doc)
    undecided = counts["undecided_count"] or 0
    signed_off = bool(doc["redaction_completed"]) and bool(doc["human_review_approved"])
    if undecided:
        next_step, reason = "decide_findings", f"{undecided} finding(s) still undecided."
    elif ocr_blocks_redaction(doc["ocr_status"]):
        next_step, reason = "open_studio", "OCR output must be reviewed in the Studio first."
    elif pending_regions:
        next_step, reason = "open_studio", f"{pending_regions} PDF redaction box(es) must be reviewed in the Studio."
    elif not signed_off:
        next_step, reason = "sign_off", "Every finding is decided. Sign off to release this document's reviewed copy."
    elif gate["allowed"]:
        next_step, reason = "export_ready", "Signed off. Its reviewed copy is included in the batch export."
    elif not gate["supported"]:
        next_step, reason = "no_export", "Signed off. There is no reviewed export for this file type."
    else:
        next_step, reason = "open_studio", gate["failed_conditions"][0]
    return {
        "id": doc_id,
        "filename": doc["filename"],
        "title": doc["title"],
        "file_extension": doc["file_extension"],
        "finding_count": counts["finding_count"] or 0,
        "undecided_count": undecided,
        "pending_pdf_regions": pending_regions,
        "ocr_status": doc["ocr_status"],
        "review_status": doc["review_status"],
        "redaction_completed": bool(doc["redaction_completed"]),
        "human_review_approved": bool(doc["human_review_approved"]),
        "signed_off": signed_off,
        "export_gate": gate,
        "next_step": next_step,
        "next_step_reason": reason,
    }


@batch_bp.route("/matter/<int:matter_id>/review")
@require_roles("reviewer", "admin")
def matter_review_page(matter_id: int):
    conn = get_db()
    matter = matter_or_none(conn, matter_id)
    conn.close()
    if not matter:
        return render_template_string("<p>Matter not found.</p><p><a href='/matters'>Back to matters</a></p>"), 404
    return render_template(
        "matter_review.html",
        matter_id=matter_id,
        matter_name=matter["name"],
        **template_user_context(),
    )


@batch_bp.route("/api/matter/<int:matter_id>/batch")
@require_roles("reviewer", "admin")
def api_matter_batch(matter_id: int):
    conn = get_db()
    matter = matter_or_none(conn, matter_id)
    if not matter:
        conn.close()
        return jsonify({"error": "Matter not found"}), 404
    documents = [document_batch_state(conn, doc) for doc in matter_documents(conn, matter_id)]
    filenames = {doc["id"]: doc["filename"] for doc in documents}
    groups = group_findings(matter_findings(conn, matter_id), filenames)
    conn.close()
    return jsonify({
        "matter": {"id": matter["id"], "name": matter["name"]},
        "documents": documents,
        "groups": groups,
        "summary": {
            "document_count": len(documents),
            "group_count": len(groups),
            "undecided_group_count": sum(1 for group in groups if group["undecided"]),
            "undecided_finding_count": sum(doc["undecided_count"] for doc in documents),
            "signed_off_count": sum(1 for doc in documents if doc["signed_off"]),
            "export_ready_count": sum(1 for doc in documents if doc["next_step"] == "export_ready"),
        },
    })


@batch_bp.route("/api/matter/<int:matter_id>/batch/decide", methods=["POST"])
@require_roles("reviewer", "admin")
def api_matter_batch_decide(matter_id: int):
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    group_key = str(payload.get("group_key") or "")
    if action not in BATCH_ACTIONS:
        return jsonify({"error": f"Unsupported batch decision. Use one of: {', '.join(BATCH_ACTIONS)}."}), 400
    if not group_key:
        return jsonify({"error": "A finding group is required."}), 400
    document_id = payload.get("document_id")
    if document_id is not None and not str(document_id).isdigit():
        return jsonify({"error": "document_id must be a document id."}), 400
    document_id = int(document_id) if document_id is not None else None

    conn = get_db()
    if not matter_or_none(conn, matter_id):
        conn.close()
        return jsonify({"error": "Matter not found"}), 404
    members = [finding for finding in matter_findings(conn, matter_id) if finding["group_key"] == group_key]
    if document_id is not None:
        members = [finding for finding in members if finding["document_id"] == document_id]
    if not members:
        conn.close()
        return jsonify({"error": "That finding group is not in this matter."}), 404
    if document_id is not None and members[0]["locked"]:
        conn.close()
        return jsonify({
            "error": "This document is signed off, so the batch cannot change its decisions. "
            f"Reopen it in its own review in the Studio (/studio/{document_id}) to change them.",
        }), 409
    locked = [finding for finding in members if finding["locked"]]
    open_members = [finding for finding in members if not finding["locked"]]

    if document_id is not None:
        # The per-document override: the reviewer named this document, so its
        # findings in the group take this decision whatever they held.
        targets = open_members
    elif action == "pending":
        # Undo for the whole group. A finding a reviewer typed in by hand is
        # not the detector's to group, and is left alone.
        targets = [finding for finding in open_members if (finding["review_status"] or "pending") != "added_by_reviewer"]
    else:
        # A group decision fills in the undecided findings and nothing else, so
        # it can never overwrite a decision made one document at a time.
        targets = [finding for finding in open_members if is_undecided(finding["review_status"])]

    review_status = FINDING_ACTION_STATUSES[action]
    by_document: dict[int, list[int]] = {}
    for finding in targets:
        by_document.setdefault(finding["document_id"], []).append(finding["id"])
    updated = 0
    updated_documents = 0
    for doc_id, finding_ids in by_document.items():
        changed = conn.execute(
            f"""
            UPDATE privacy_findings SET review_status = ?
            WHERE document_id = ? AND id IN ({','.join('?' for _ in finding_ids)})
              AND NOT EXISTS (
                SELECT 1 FROM documents d
                WHERE d.id = privacy_findings.document_id
                  AND (COALESCE(d.redaction_completed, 0) OR COALESCE(d.human_review_approved, 0))
              )
            """,
            (review_status, doc_id, *finding_ids),
        ).rowcount
        if not changed:
            locked.extend(finding for finding in targets if finding["document_id"] == doc_id)
            continue
        updated += changed
        updated_documents += 1
        refresh_document_state(conn, doc_id)
        # Counts and the category only: the group's text is the identifier
        # itself, and neither it nor its hash goes into the audit log.
        audit_event(
            conn,
            f"finding_batch.matter.{action}",
            document_id=doc_id,
            metadata={
                "matter_id": matter_id,
                "scope": "document" if document_id is not None else "matter",
                "updated": len(finding_ids),
                "review_status": review_status,
                "category": members[0]["category"],
            },
        )
    if not updated_documents:
        audit_event(
            conn,
            f"finding_batch.matter.{action}",
            result="noop",
            metadata={"matter_id": matter_id, "category": members[0]["category"], "locked": len(locked)},
        )
    # One commit for the whole decision: it lands in every document or in none.
    conn.commit()
    conn.close()
    return jsonify({
        "ok": True,
        "updated": updated,
        "kept": len(open_members) - len(targets),
        "locked": len(locked),
        "locked_documents": len({finding["document_id"] for finding in locked}),
        "documents": updated_documents,
        "review_status": review_status,
    })


@batch_bp.route("/api/matter/<int:matter_id>/redacted-export")
@require_roles("reviewer", "admin")
def api_matter_redacted_export(matter_id: int):
    """Every signed-off document's reviewed copy, in one archive.

    Built document by document through the single-document builders, so the
    archive holds exactly the files each document's own download would give,
    and a document whose gate refuses is left out, not built around.
    """
    redaction_style = request.args.get("style", "placeholder").lower()
    conn = get_db()
    if not matter_or_none(conn, matter_id):
        conn.close()
        return jsonify({"error": "Matter not found"}), 404
    if redaction_style not in {"placeholder", "mask"}:
        return audited_error(
            conn,
            "Unsupported DOCX redaction style. Use placeholder or mask.",
            400,
            "export.matter_batch",
            metadata={"matter_id": matter_id, "style": redaction_style},
        )
    candidates = []
    held_back = 0
    for doc in matter_documents(conn, matter_id):
        gate = export_gate_state(conn, doc)
        if gate["supported"] and gate["allowed"]:
            candidates.append((doc["id"], gate["format"]))
        else:
            held_back += 1
    if not candidates:
        return audited_error(
            conn,
            "No document in this matter has passed its reviewed-export gate yet. "
            "Decide every finding and sign each document off first.",
            409,
            "export.matter_batch",
            metadata={"matter_id": matter_id, "held_back": held_back},
        )
    conn.close()

    archive = BytesIO()
    names: set[str] = set()
    exported = 0
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        for doc_id, export_format in candidates:
            result = build_reviewed_pdf(doc_id) if export_format == "pdf" else build_reviewed_docx(doc_id, redaction_style)
            if not isinstance(result, ReviewedExport):
                held_back += 1
                continue
            name = result.download_name
            if name in names:
                stem, _, extension = name.rpartition(".")
                name = f"{stem}_{doc_id}.{extension}"
            names.add(name)
            bundle.writestr(name, result.data)
            exported += 1

    conn = get_db()
    if not exported:
        return audited_error(
            conn,
            "No reviewed copy could be built for this matter.",
            409,
            "export.matter_batch",
            metadata={"matter_id": matter_id, "held_back": held_back},
        )
    audit_and_commit(
        conn,
        "export.matter_batch",
        metadata={"matter_id": matter_id, "exported": exported, "held_back": held_back, "style": redaction_style},
    )
    conn.close()
    response = send_file(
        BytesIO(archive.getvalue()),
        as_attachment=True,
        download_name=f"matter_{matter_id}_reviewed_copies.zip",
        mimetype="application/zip",
    )
    response.headers["X-Exported-Count"] = str(exported)
    response.headers["X-Held-Back-Count"] = str(held_back)
    return response
