// Every line of copy on /welcome, with what substantiates it. Paths are relative
// to the repository root. Numbers are never typed here: they are read from
// demo.json, which scripts/gen_demo.py produces by running the app.

import demo from "./demo.json";

const n = demo.findings.length; // 15
const gateCount = demo.states.uploaded.gate.conditions.length; // 8
const kept = demo.findings.find((f) => f.id === demo.kept_id); // the defendant company
const approvedInScript = Object.values(demo.states.kept.statuses).filter((s) => s === "approved").length; // 14

export const copy = {
  // Rebrand branch: templates/partials/brand.html, templates/base.html ("Local privacy workstation").
  tagline: "Local privacy workstation",

  nav: {
    how: "How it works",
    limits: "Limits",
    run: "Run it",
    // templates/landing.html: the nav CTA goes to auth.login.
    open: "Open the workstation",
  },

  hero: {
    // Only findings a person approves (or adds) are redacted:
    // privacy.REDACTED_REVIEW_STATUSES = {approved, added_by_reviewer} (privacy.py:121),
    // and every finding starts pending (samples/README.md, "All findings begin pending").
    // [PERSON_1] is the product's own placeholder format (privacy.py _placeholder_for).
    titleBefore: "Nothing becomes",
    titleChip: "[PERSON_1]",
    titleAfter: "until you decide.",
    primary: "Open the workstation",
    secondary: "Run it locally",
  },

  // The product definition, right after the headline: what it is, who it is for
  // and what it does. Set as a glossary entry: the wordmark is the term.
  define: {
    // README.md lines 3-5: "A local legal privacy document workstation. Organize documents
    // into matters, inspect privacy findings beside the document, review what should be
    // removed, and prepare reviewed DOCX or PDF redactions with export checks."
    what: "A local privacy workstation for legal documents. It sorts files into matters, flags what could identify a person right beside the text, and prepares reviewed DOCX and PDF redactions with export checks.",
    // README.md lines 7-8, nearly verbatim.
    who: "Built for technically comfortable lawyers, legal-tech builders and developers evaluating a local review workflow, especially for Turkish legal documents.",
    facts: [
      {
        // README.md: "You run it on your own computer. There is no hosted demo or document
        // upload service."; "Try it locally" is one command.
        term: "Runs on your own computer",
        text: "One command starts it. There is no hosted version and no upload service.",
      },
      {
        // REDACTED_REVIEW_STATUSES = {approved, added_by_reviewer} (privacy.py:121); every
        // finding starts pending (samples/README.md).
        term: "A person decides every finding",
        text: "Nothing is removed until a reviewer approves it, or adds it themselves.",
      },
      {
        // README.md "What the engine is".
        term: "Detection by rules, not a model",
        text: "Regular expressions, the official TCKN and VKN check digits, and a Turkish name gazetteer. The same document gives the same findings every time.",
      },
      {
        // SUPPORTED_EXTENSIONS (taxonomy.py): 12 extensions; redacted copies from DOCX and
        // PDF only (app.py docx_source / pdf path). OCR: blueprints/landing.py FEATURES,
        // "OCR runs locally and never touches findings or export readiness until a reviewer
        // explicitly accepts it."
        term: "Reads what legal work uses",
        text: "DOCX, PDF, UDF, XLSX, PPTX, text, images and ZIP. Scans go through local OCR, used only once a reviewer accepts it. Redacted copies are built from DOCX and PDF.",
      },
    ],
  },

  demo: {
    title: "Watch it work on a practice petition.",
    // samples/README.md: the bundled fictional files. The variant and the masking are
    // gen_demo.py's (see its docstring).
    body: "A fictional Turkish petition that ships with the app, run through the real detector. Scroll through the three steps, or select any bracket and decide it yourself.",
  },

  sheet: {
    // The bundled fictional sample (samples/README.md).
    file: "turkish_petition.docx",
    stages: { find: "Find", decide: "Decide", export: "Export" },
  },

  steps: {
    find: {
      title: "The rules bracket what could identify someone.",
      // Counts from demo.json. "pass their check digits": the TCKN/VKN rule bases
      // (CATEGORY_EXPLANATIONS). "says why it was flagged": the studio's "Why it was
      // flagged" line renders CATEGORY_EXPLANATIONS[category].basis (templates/studio.html),
      // which the popover here quotes. "Found is not removed": README.md "Detection is not redaction".
      body: [
        `${n} spans in this petition: the parties and their roles, a TCKN and a VKN that pass their check digits (masked on this page), an address, an email, the court, the case number, a date and an amount.`,
        "Each one says why it was flagged. Found is not removed: nothing in the document has changed yet.",
      ],
      // README.md "What the engine is": a pattern the rules do not cover is one they miss.
      aside: "Rules miss what they were not written for, so you still read the document.",
    },
    decide: {
      title: "You decide each one.",
      // The studio's own three decisions (templates/studio.html ROW_DECISIONS):
      // Approve / Not sensitive / Keep unredacted. README.md Threat Model: every finding
      // requires a human reviewer before it affects an export.
      body: [
        "Approve it, mark it not sensitive, or keep it unredacted. Nothing is decided for you.",
        (state) =>
          state === "kept"
            ? `Here the defendant company's name is kept and the other ${approvedInScript} are approved. Select any bracket to change its decision.`
            : "Select any bracket to change its decision.",
      ],
      approveRest: "Approve the undecided",
      clear: "Clear all decisions",
    },
    export: {
      title: "The copy you send carries placeholders.",
      // Verified by gen_demo.py: the plan text equals the exported DOCX text for the kept
      // and the all-approved runs, and verify_plan.mjs checks the port on 403 cases.
      // "wherever it appears": resolve_redaction redacts an approved finding at its own
      // position and, as the backstop, every other match of its sample (docx_redactor.py
      // docstring).
      // "removes every copy": approved targets are resolved first, so an undecided or
      // dismissed occurrence of approved text is replaced too. The one exception, text
      // wholly inside a retained finding, is never shipped: the DOCX export refuses
      // (app.py docx_export_conditions: no_approved_text_retained). See
      // redaction_plan.plan_segments; exercised by the plan_cases.json cases.
      body: [
        "This is the text of the reviewed DOCX the app builds from these decisions.",
        "Approved text is replaced everywhere it appears, so approving one copy of a name removes every copy. Everything else stays as written.",
        // app.py docx_export_conditions: redaction_completed and human_review_approved.
        "The app builds it once you mark the redaction complete and approve the document.",
      ],
      // pdf_redactor.py: approved regions are drawn as filled black boxes.
      pdf: "A PDF is redacted with black boxes over the regions you approve on the page.",
      // app.py docx_export_conditions: findings_decided.
      undecided: (k) =>
        `${k} ${k === 1 ? "finding is" : "findings are"} still undecided. The app does not build the reviewed copy until every finding is decided.`,
    },
  },

  decisions: {
    approved: { verb: "Approve", effect: (ph) => `Becomes ${ph} wherever this text appears.` },
    // dismissed ∈ RELEASE_CLEARED_REVIEW_STATUSES, ∉ REDACTED (privacy.py:121,134).
    dismissed: { verb: "Not sensitive", effect: () => "Stays in the text and stops counting against release." },
    // retained ∈ RESOLVED, ∉ RELEASE_CLEARED (privacy.py:134,141); studio copy
    // "Keep unredacted — identifier stays in the export".
    retained: { verb: "Keep unredacted", effect: () => "Stays in the export and still counts against release." },
  },

  gate: {
    title: "Clearing it for an LLM is a separate question.",
    // AGENTS.md: the reviewed export reflects the reviewer's decisions (a retained
    // identifier stays in it); the external-LLM gate is clearance to send it off the machine.
    // The count is derived from the gate itself (privacy.external_llm_gate_policy).
    // The app itself makes no outbound call (README.md "What the engine is").
    body: [
      "The reviewed copy reflects your decisions, a kept name included. Whether a document may go to an external LLM is decided separately.",
      `The app clears a document for external LLM use only when all ${gateCount} of these hold. A person's approval is one of them, and nothing stands in for it. The app never sends the document anywhere itself.`,
    ],
    // tests/test_auto_mode_removed.py: human approval has no substitute.
    neutral: "The app checks these against your decisions. This page does not work them out.",
    // The gate's own caveat: README.md "A clear result is not a guarantee".
    allNote:
      "Cleared means these conditions hold for the findings the rules made. It is not a guarantee: read what it does not do, below.",
    // The kept run: no_direct_identifiers fails and residual risk after review is Medium;
    // company_name is in DIRECT_IDENTIFIER_CATEGORIES (gen_demo.py refuses otherwise).
    keptNote:
      `The kept company name, ${kept.sample}, is still a direct identifier in the document, so the gate holds. Residual risk after review is measured on what remains.`,
  },

  limits: {
    title: "What it does not do.",
    // README.md "Know the limits before using real documents". This page quotes no
    // accuracy figure (blueprints/landing.py, the note above STATS).
    items: [
      {
        term: "Read English well",
        text: "The rules are tuned for Turkish. English name and address coverage is materially lower than Turkish coverage, so a document that does not read as Turkish is refused clearance. An English annex set out as labelled fields can still read as Turkish.",
      },
      {
        term: "Call anything anonymous",
        text: "Detection is not redaction, and redaction is not anonymization. Context can still identify a person or a matter after the obvious identifiers are gone.",
      },
      {
        term: "Serve a team",
        text: "One reviewer on one computer, bound to 127.0.0.1. There is no hosted version, and files sit unencrypted on local disk.",
      },
    ],
    // blueprints/landing.py: no accuracy figure on an unauthenticated page; both corpora
    // are synthetic (README.md "Prototype quality").
    footnote: "This page quotes no accuracy figure. Both of the project's test corpora are synthetic.",
  },

  run: {
    title: "Try it with the practice files.",
    // README.md "Try it locally".
    body: "One command builds the app with Tesseract and Poppler, loads six fictional practice documents, and prints your login when it is ready.",
    command: "./start.sh",
    needs: "Needs Docker. The workstation opens at 127.0.0.1:5055.",
    copy: "Copy",
    copied: "Copied",
  },

  footer: {
    // samples/README.md "About the demonstration identifiers".
    sample:
      "The petition in the demo is the fictional practice file that ships with the app, with its counsel line left out. Its ID numbers were randomly generated and pass check-digit validation, so a match with a real number cannot be excluded; the app reads them in full, and this page masks their middle digits.",
    // blueprints/landing.py STANDARDS and its note: "Designed around", never "compliant".
    standards:
      "Designed around GDPR, KVKK, HIPAA Safe Harbor and attorney–client privilege. Not certified or audited against any of them.",
    login: "Log in",
  },
};
