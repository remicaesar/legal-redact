// The Legal Redact lockup (logo option 1c), as React. The markup is exactly what
// templates/partials/brand.html renders, and tests/test_brand.py checks it on
// /welcome like every other page. Size and colour come from static/workstation.css
// (.brand-icon, .wordmark: the 30px sidebar geometry, "[Redact]" in Courier Prime);
// src/page.css only scales the icon up to the 32px nav size.
// The brackets are aria-hidden, so the lockup reads as "Legal Redact".

export function BrandIcon() {
  return (
    <span className="brand-icon" aria-hidden="true">
      <span className="brand-icon__bracket brand-icon__bracket--open"></span>
      <span className="brand-icon__letter">L</span>
      <span className="brand-icon__bracket brand-icon__bracket--close"></span>
    </span>
  );
}

export function Wordmark({ className = "" }) {
  return (
    <span className={className ? `wordmark ${className}` : "wordmark"}>
      <span className="wordmark__legal">Legal</span>{" "}
      <span className="wordmark__redact">
        <span aria-hidden="true">[</span>Redact<span aria-hidden="true">]</span>
      </span>
    </span>
  );
}

// The sidebar lockup: the 30px icon, the wordmark, the tagline beneath.
export function Lockup({ tagline }) {
  return (
    <span className="lockup">
      <BrandIcon />
      <span className="lockup__text">
        <Wordmark className="wordmark--sidebar" />
        {tagline ? <span className="lockup__tagline">{tagline}</span> : null}
      </span>
    </span>
  );
}
