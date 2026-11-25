import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { scaleBand } from "@visx/scale";
import { Bar } from "@visx/shape";
import { ParentSize } from "@visx/responsive";
import demo from "./demo.json";
import { copy } from "./copy.js";
import { BrandIcon, Lockup, Wordmark } from "./Brand.jsx";
import { Sheet } from "./Sheet.jsx";

const FINDINGS = demo.findings;
const STAGES = ["find", "decide", "export"];
const SCRIPT = demo.states.kept.statuses; // the defendant company kept, the rest approved

// The page is prerendered (scripts/prerender.mjs) and then hydrated, so the
// first client render must match the server's: nothing that decides markup may
// read the viewport. Media queries are read after mount, for behaviour only; the
// layout differences between phone and desktop are CSS.
const WIDE = "(min-width: 1080px)";
const isWide = () => typeof window !== "undefined" && window.matchMedia(WIDE).matches;
const prefersReducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function useMedia(query) {
  const [on, setOn] = useState(false);
  useEffect(() => {
    const m = window.matchMedia(query);
    const fn = () => setOn(m.matches);
    fn();
    m.addEventListener("change", fn);
    return () => m.removeEventListener("change", fn);
  }, [query]);
  return on;
}

const countsOf = (statuses) => {
  const c = { pending: 0, approved: 0, retained: 0, dismissed: 0 };
  for (const f of FINDINGS) c[statuses[f.id] || "pending"] += 1;
  return c;
};

// Which of the states the generator ran through the app these decisions are, if any.
function knownState(statuses) {
  const same = (ref) => FINDINGS.every((f) => (statuses[f.id] || "pending") === (ref[f.id] || "pending"));
  if (FINDINGS.every((f) => (statuses[f.id] || "pending") === "pending")) return "uploaded";
  if (same(demo.states.kept.statuses)) return "kept";
  if (same(demo.states.all.statuses)) return "all";
  return null;
}

export default function App() {
  const wide = useMedia(WIDE);
  const [stage, setStage] = useState("find");
  const [statuses, setStatuses] = useState({});
  const [revealed, setRevealed] = useState(false);
  const [intro, setIntro] = useState(true);
  const [sheetH, setSheetH] = useState(null);
  const sheetRef = useRef(null);
  const scripted = useRef(false);
  const timers = useRef([]);
  const stepRefs = useRef({});

  const stopScript = () => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  };

  // The one orchestrated moment: the brackets settle onto the petition the
  // first time it comes into view.
  useEffect(() => {
    if (prefersReducedMotion()) {
      setRevealed(true);
      return undefined;
    }
    const el = sheetRef.current;
    if (!el) return undefined;
    let t;
    const io = new IntersectionObserver(
      (entries) => {
        if (!entries.some((e) => e.isIntersecting)) return;
        io.disconnect();
        (document.fonts?.ready || Promise.resolve()).then(() => {
          t = setTimeout(() => setRevealed(true), 350);
        });
      },
      { threshold: 0.35 },
    );
    io.observe(el);
    return () => {
      io.disconnect();
      clearTimeout(t);
    };
  }, []);

  useEffect(() => {
    if (!revealed) return undefined;
    const t = setTimeout(() => setIntro(false), 2600);
    return () => clearTimeout(t);
  }, [revealed]);

  // The sheet can be taller than the viewport; its sticky offset needs its height.
  useEffect(() => {
    const el = sheetRef.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(() => setSheetH(el.offsetHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Reaching Decide for the first time plays the walkthrough's decisions.
  useEffect(() => {
    if (stage !== "decide" || scripted.current) return;
    scripted.current = true;
    if (FINDINGS.some((f) => (statuses[f.id] || "pending") !== "pending")) return;
    if (prefersReducedMotion()) {
      setStatuses({ ...SCRIPT });
      return;
    }
    FINDINGS.forEach((f, i) => {
      timers.current.push(
        setTimeout(() => setStatuses((cur) => ({ ...cur, [f.id]: SCRIPT[f.id] })), 250 + i * 85),
      );
    });
  }, [stage]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => stopScript(), []);

  // On wide screens the page's scroll position picks the step.
  useEffect(() => {
    if (!wide) return undefined;
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) if (e.isIntersecting) setStage(e.target.dataset.stage);
      },
      { rootMargin: "-42% 0px -52% 0px" },
    );
    Object.values(stepRefs.current).forEach((el) => el && io.observe(el));
    return () => io.disconnect();
  }, [wide]);

  // On a wide screen a step is a scroll position, so choosing one scrolls to it;
  // on a phone the steps take turns above the sheet and the choice is enough.
  const goTo = useCallback((s) => {
    setStage(s);
    if (isWide()) {
      stepRefs.current[s]?.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "center" });
    }
  }, []);

  const decide = useCallback(
    (id, status) => {
      stopScript();
      scripted.current = true;
      setStatuses((cur) => ({ ...cur, [id]: status }));
      if (stage === "find") goTo("decide");
    },
    [stage, goTo],
  );

  const approveRest = () => {
    stopScript();
    setStatuses((cur) => {
      const next = { ...cur };
      for (const f of FINDINGS) if ((next[f.id] || "pending") === "pending") next[f.id] = "approved";
      return next;
    });
  };
  const clearAll = () => {
    stopScript();
    setStatuses({});
  };

  const counts = countsOf(statuses);

  const stepBody = (s) => (
    <StepBody
      stage={s}
      counts={counts}
      statuses={statuses}
      onApproveRest={approveRest}
      onClear={clearAll}
    />
  );

  return (
    <>
      <a className="skip" href="#demo">
        Skip to the demo
      </a>
      <header className="nav wrap">
        <a className="nav__brand" href="#top">
          <BrandIcon />
          <Wordmark className="wordmark--nav" />
        </a>
        <nav className="nav__links" aria-label="Page">
          <a href="#demo">{copy.nav.how}</a>
          <a href="#limits">{copy.nav.limits}</a>
          <a href="#run">{copy.nav.run}</a>
          <a className="btn btn--primary btn--sm" href="/login">
            {copy.nav.open}
          </a>
        </nav>
      </header>

      <main id="top">
        <section className="hero wrap" aria-labelledby="hero-title">
          <h1 className="hero__title" id="hero-title">
            {copy.hero.titleBefore} <span className="hero__chip">{copy.hero.titleChip}</span>{" "}
            <span className="hero__line">{copy.hero.titleAfter}</span>
          </h1>
          <dl className="define" aria-label="What Legal Redact is">
            <dt className="define__term">
              <Wordmark className="wordmark--define" />
            </dt>
            <dd className="define__body">
              <p className="define__what">{copy.define.what}</p>
              <p className="define__who">{copy.define.who}</p>
              <div className="hero__actions">
                <a className="btn btn--primary" href="/login">
                  {copy.hero.primary}
                </a>
                <a className="link" href="#run">
                  {copy.hero.secondary}
                </a>
              </div>
              <ul className="define__facts">
                {copy.define.facts.map((f) => (
                  <li key={f.term}>
                    <strong>{f.term}</strong>
                    <span>{f.text}</span>
                  </li>
                ))}
              </ul>
            </dd>
          </dl>
        </section>

        <section className="demo-intro wrap ruled" id="demo" aria-labelledby="demo-title">
          <h2 className="section-title" id="demo-title">
            {copy.demo.title}
          </h2>
          <p>{copy.demo.body}</p>
        </section>

        <section className="demo wrap" aria-label="Demo">
          <div className="demo__story">
            {/* All three steps are always in the page. On a wide screen they are
                scroll positions; on a phone CSS shows the active one only, above
                the sheet, so the markup is the same everywhere. */}
            {STAGES.map((s, i) => (
              <div
                key={s}
                className={`step${stage === s ? " is-active" : ""}`}
                data-stage={s}
                ref={(el) => (stepRefs.current[s] = el)}
              >
                <p className="step__num" aria-hidden="true">
                  {i + 1}
                </p>
                {stepBody(s)}
              </div>
            ))}
          </div>

          <div className="demo__sheet" ref={sheetRef} style={sheetH ? { "--sheet-h": `${sheetH}px` } : undefined}>
            <Sheet
              stage={stage}
              statuses={statuses}
              onDecide={decide}
              onStage={goTo}
              revealed={revealed}
              intro={intro}
            />
            <div className="demo__next">
                {stage !== "export" ? (
                  <button
                    type="button"
                    className="btn btn--ghost"
                    onClick={() => {
                      const next = STAGES[STAGES.indexOf(stage) + 1];
                      goTo(next);
                      // The next step is display:none until React re-renders it as
                      // active, so scroll once it has been painted.
                      requestAnimationFrame(() =>
                        requestAnimationFrame(() =>
                          stepRefs.current[next]?.scrollIntoView({
                            behavior: prefersReducedMotion() ? "auto" : "smooth",
                            block: "start",
                          }),
                        ),
                      );
                    }}
                  >
                    Next: {copy.sheet.stages[STAGES[STAGES.indexOf(stage) + 1]]}
                  </button>
                ) : (
                  <a className="btn btn--ghost" href="#gate">
                    Next: clearing it for an LLM
                  </a>
                )}
            </div>
          </div>
        </section>

        <Gate statuses={statuses} />

        <section className="limits wrap ruled" id="limits" aria-labelledby="limits-title">
          <h2 className="section-title" id="limits-title">
            {copy.limits.title}
          </h2>
          <dl className="limits__list">
            {copy.limits.items.map((it) => (
              <div className="limits__item" key={it.term}>
                <dt>{it.term}</dt>
                <dd>{it.text}</dd>
              </div>
            ))}
          </dl>
          <p className="limits__foot">{copy.limits.footnote}</p>
        </section>

        <section className="run wrap ruled" id="run" aria-labelledby="run-title">
          <div className="run__text">
            <h2 className="section-title" id="run-title">
              {copy.run.title}
            </h2>
            <p>{copy.run.body}</p>
            <p className="run__needs">{copy.run.needs}</p>
          </div>
          <div className="run__cmd">
            <Command />
            <a className="btn btn--primary" href="/login">
              {copy.hero.primary}
            </a>
          </div>
        </section>
      </main>

      <footer className="footer wrap ruled">
        <div className="footer__brand">
          <Lockup tagline={copy.tagline} />
        </div>
        <div className="footer__notes">
          <p>{copy.footer.sample}</p>
          <p>{copy.footer.standards}</p>
        </div>
        <nav className="footer__links" aria-label="Footer">
          <a href="#demo">{copy.nav.how}</a>
          <a href="#limits">{copy.nav.limits}</a>
          <a href="#run">{copy.nav.run}</a>
          <a href="/login">
            {copy.footer.login}
          </a>
        </nav>
      </footer>
    </>
  );
}

function StepBody({ stage, counts, statuses, onApproveRest, onClear }) {
  const s = copy.steps[stage];
  return (
    <>
      <h2 className="step__title">{s.title}</h2>
      {s.body.map((b, i) => (
        <p className="step__text" key={i}>
          {typeof b === "function" ? b(knownState(statuses)) : b}
        </p>
      ))}
      {stage === "find" ? <p className="step__aside">{s.aside}</p> : null}
      {stage === "decide" ? (
        <div className="ledger">
          <Ledger statuses={statuses} />
          <p className="ledger__legend">
            {[
              ["approved", "approved"],
              ["retained", "kept"],
              ["dismissed", "not sensitive"],
              ["pending", "undecided"],
            ].map(([k, word]) => (
              <span className="ledger__key" key={k}>
                <span className={`swatch swatch--${k}`} aria-hidden="true" />
                {counts[k]} {word}
              </span>
            ))}
          </p>
          <div className="ledger__actions">
            <button type="button" className="btn btn--ghost btn--sm" onClick={onApproveRest} disabled={!counts.pending}>
              {s.approveRest}
            </button>
            <button type="button" className="link link--quiet" onClick={onClear}>
              {s.clear}
            </button>
          </div>
        </div>
      ) : null}
      {stage === "export" ? (
        <>
          {counts.pending ? <p className="step__warn">{s.undecided(counts.pending)}</p> : null}
          <p className="step__aside">{s.pdf}</p>
        </>
      ) : null}
    </>
  );
}

// One cell per finding, in reading order, coloured by its decision.
function Ledger({ statuses }) {
  return (
    <div className="ledger__bar" aria-hidden="true">
      <ParentSize debounceTime={40}>
        {({ width }) => {
          if (!width) return null;
          const x = scaleBand({ domain: FINDINGS.map((f) => f.id), range: [0, width], paddingInner: 0.18 });
          return (
            <svg width={width} height={14}>
              {FINDINGS.map((f) => {
                const st = statuses[f.id] || "pending";
                return (
                  <Bar
                    key={f.id}
                    className={`ledger__cell ledger__cell--${st}`}
                    x={x(f.id)}
                    y={1}
                    width={x.bandwidth()}
                    height={12}
                    rx={2}
                  />
                );
              })}
            </svg>
          );
        }}
      </ParentSize>
    </div>
  );
}

function Gate({ statuses }) {
  const state = knownState(statuses);
  const conditions = demo.states.uploaded.gate.conditions;
  const gate = state ? demo.states[state].gate : null;
  const failed = gate ? gate.conditions.filter((c) => !c.passed).length : 0;
  const lead =
    state === "uploaded"
      ? "As uploaded, before anyone has decided anything:"
      : state
        ? "For these decisions, once the redaction is marked complete and the document approved:"
        : copy.gate.neutral;
  const verdict = gate ? (gate.allowed ? gate.decision : `Blocked: ${failed} of ${conditions.length} conditions fail.`) : null;
  return (
    <section className="gate wrap ruled" id="gate" aria-labelledby="gate-title">
      <div className="gate__text">
        <h2 className="section-title" id="gate-title">
          {copy.gate.title}
        </h2>
        {copy.gate.body.map((b, i) => (
          <p key={i}>{b}</p>
        ))}
      </div>
      <div className="gate__panel">
        <p className="gate__lead" aria-live="polite">
          {lead}
          {verdict ? (
            <strong className={`gate__verdict${gate.allowed ? " is-allowed" : ""}`}> {verdict}</strong>
          ) : null}
        </p>
        <ul className="gate__list">
          {conditions.map((c, i) => {
            const now = gate ? gate.conditions[i] : null;
            const mark = now ? (now.passed ? "pass" : "fail") : "open";
            return (
              <li key={c.id} className={`gate__row gate__row--${mark}`}>
                <span className="gate__box" aria-hidden="true">
                  <span>[</span>
                  <svg viewBox="0 0 12 12" width="12" height="12">
                    {mark === "pass" ? <path d="M2.5 6.4 5 8.8 9.6 3.4" /> : null}
                    {mark === "fail" ? <path d="M3 3 9 9M9 3 3 9" /> : null}
                  </svg>
                  <span>]</span>
                </span>
                <span className="gate__label">
                  {c.label}
                  <span className="sr-only">{now ? (now.passed ? ", holds" : ", fails") : ""}</span>
                </span>
                {now ? <span className="gate__detail">{now.detail.replace(/_/g, " ")}</span> : null}
              </li>
            );
          })}
        </ul>
        {state === "kept" ? <p className="gate__note">{copy.gate.keptNote}</p> : null}
        {state === "all" ? <p className="gate__note">{copy.gate.allNote}</p> : null}
      </div>
    </section>
  );
}

function Command() {
  const [done, setDone] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(copy.run.command);
    } catch {
      /* clipboard can be unavailable; the command is on screen either way */
    }
    setDone(true);
    setTimeout(() => setDone(false), 2000);
  };
  return (
    <div className="cmd">
      <code className="cmd__text">
        <span className="cmd__prompt" aria-hidden="true">
          $
        </span>
        {copy.run.command}
      </code>
      <button type="button" className="cmd__copy" onClick={onCopy}>
        {done ? copy.run.copied : copy.run.copy}
      </button>
    </div>
  );
}
