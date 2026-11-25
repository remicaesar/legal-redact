// The demo's centrepiece: the bundled synthetic petition on a sheet of paper,
// with the detector's findings drawn over it by a visx layer.
//
// The text is real HTML (selectable, wraps, read by screen readers). The ink --
// brackets hugging each finding, the tint behind it, and the leader curves out
// to the margin notes -- is an SVG drawn with visx from the measured position of
// every finding on the page, re-measured whenever the text reflows.
import { forwardRef, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Group } from "@visx/group";
import { LinkHorizontal } from "@visx/shape";
import demo from "./demo.json";
import { copy } from "./copy.js";
import { planSegments, sourceSegments, toParagraphs } from "./plan.js";

// Measuring layout must happen before paint in the browser; on the server (the
// build-time prerender) there is no layout, and the plain effect is a no-op.
const useMeasureEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

const FINDINGS = demo.findings;
const BY_ID = Object.fromEntries(FINDINGS.map((f) => [f.id, f]));
const ORDER = Object.fromEntries(FINDINGS.map((f, i) => [f.id, i]));
const RISK = { CRITICAL: "Critical risk", HIGH: "High risk", MEDIUM: "Medium risk", LOW: "Low risk" };
const STAGES = ["find", "decide", "export"];
const WIDE = 680; // paper width at which the margin notes fit beside the text
const LABEL_H = 19;
const LABEL_GAP = 3;

// Paragraph roles, read off the source once: the title, the synthetic-sample
// notes that open and close the file, the court line, the field block under it,
// the one-word section headings, and body text.
const SOURCE_PARAS = toParagraphs(sourceSegments(demo.text, FINDINGS));
const ROLES = (() => {
  const texts = SOURCE_PARAS.map((p) => p.map((s) => s.text).join(""));
  const hasFinding = SOURCE_PARAS.map((p) => p.some((s) => s.kind !== "text"));
  const firstHeading = texts.findIndex((t, i) => i > 2 && t.length <= 12 && !hasFinding[i]);
  return texts.map((t, i) => {
    if (i === 0) return "title";
    if (i === 1 || i === texts.length - 1) return "note";
    if (i === 2) return "court";
    if (i < firstHeading) return "field";
    if (t.length <= 12 && !hasFinding[i]) return "heading";
    return "body";
  });
})();

function markState(stage, status) {
  if (stage === "find") return "found";
  return status || "pending";
}

function marginText(stage, f, state) {
  if (stage === "find") return { text: f.label };
  if (state === "approved") return { text: f.placeholder, mono: true };
  if (state === "retained") return { text: "Kept unredacted" };
  if (state === "dismissed") return { text: "Not sensitive" };
  return { text: stage === "export" ? "Undecided" : f.label };
}

export function Sheet({ stage, statuses, onDecide, onStage, revealed, intro }) {
  const paperRef = useRef(null);
  const textRef = useRef(null);
  const popRef = useRef(null);
  const [geom, setGeom] = useState(null);
  const [hover, setHover] = useState(null);
  const [open, setOpen] = useState(null); // { key, id }
  const [focusKey, setFocusKey] = useState(null);

  const segments = useMemo(
    () => (stage === "export" ? planSegments(demo.text, FINDINGS, statuses) : sourceSegments(demo.text, FINDINGS)),
    [stage, statuses],
  );
  const paragraphs = useMemo(() => toParagraphs(segments), [segments]);

  // Everything a reader can select, in reading order: marks, and in the Export
  // step the placeholders too. One tab stop; arrow keys move between them.
  const items = useMemo(() => {
    const out = [];
    for (const seg of segments) {
      if (seg.kind === "text") continue;
      const state = seg.kind === "redacted" ? "approved" : markState(stage, statuses[seg.findingId]);
      if (stage === "export" && state === "dismissed") continue;
      out.push({ key: `${seg.findingId}@${seg.start}`, id: seg.findingId, kind: seg.kind, state });
    }
    return out;
  }, [segments, stage, statuses]);
  const itemByKey = useMemo(() => Object.fromEntries(items.map((it) => [it.key, it])), [items]);
  const activeKey = itemByKey[focusKey] ? focusKey : items[0]?.key;

  // Measure where each selectable run landed, relative to the paper.
  useMeasureEffect(() => {
    const paper = paperRef.current;
    const textEl = textRef.current;
    if (!paper || !textEl) return undefined;
    let frame = 0;
    const measure = () => {
      const base = paper.getBoundingClientRect();
      const t = textEl.getBoundingClientRect();
      const rects = {};
      textEl.querySelectorAll("[data-key]").forEach((el) => {
        rects[el.dataset.key] = Array.from(el.getClientRects())
          .filter((r) => r.width > 0.5)
          .map((r) => ({ x: r.left - base.left, y: r.top - base.top, w: r.width, h: r.height }));
      });
      setGeom({
        width: base.width,
        height: base.height,
        textRight: t.right - base.left,
        wide: base.width >= WIDE,
        rects,
      });
    };
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    };
    measure();
    const ro = new ResizeObserver(schedule);
    ro.observe(paper);
    document.fonts?.ready?.then(schedule);
    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
    };
  }, [paragraphs]);

  // Margin notes: one per mark, at its first line, pushed down so none overlap.
  const notes = useMemo(() => {
    if (!geom?.wide) return [];
    const list = items
      .filter((it) => it.kind === "marked" && geom.rects[it.key]?.length)
      .map((it) => {
        const r = geom.rects[it.key][0];
        return { ...it, anchorY: r.y + r.h / 2, anchorX: r.x };
      })
      .sort((a, b) => a.anchorY - b.anchorY || a.anchorX - b.anchorX);
    let floor = -Infinity;
    for (const n of list) {
      n.top = Math.max(n.anchorY - LABEL_H / 2, floor);
      floor = n.top + LABEL_H + LABEL_GAP;
    }
    return list;
  }, [geom, items]);

  const marginLeft = geom ? geom.textRight + 34 : 0;

  const openFor = useCallback((key, id) => setOpen({ key, id }), []);
  // Where focus goes back to once the popover has unmounted (after commit).
  const refocusKey = useRef(null);
  const openRef = useRef(open);
  openRef.current = open;
  const close = useCallback((refocus = true) => {
    refocusKey.current = refocus ? openRef.current?.key ?? null : null;
    setOpen(null);
  }, []);
  useEffect(() => {
    if (open || !refocusKey.current) return;
    const key = refocusKey.current;
    refocusKey.current = null;
    const el =
      textRef.current?.querySelector(`[data-key="${CSS.escape(key)}"]`) ||
      textRef.current?.querySelector(`[data-key^="${CSS.escape(key.split("@")[0])}@"]`);
    el?.focus({ preventScroll: true });
  }, [open, paragraphs]);

  // A decision re-renders the text in the Export step, so an open popover's key
  // can disappear; close it when its run is gone.
  useEffect(() => {
    if (open && !itemByKey[open.key]) {
      const next = items.find((it) => it.id === open.id);
      setOpen(next ? { key: next.key, id: open.id } : null);
    }
  }, [itemByKey, items, open]);

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => {
      if (popRef.current?.contains(e.target)) return;
      if (e.target.closest?.("[data-key]")) return;
      close(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open, close]);

  useEffect(() => {
    if (!open) return;
    requestAnimationFrame(() => {
      const target = popRef.current?.querySelector('[aria-checked="true"]') || popRef.current?.querySelector("[role=radio]");
      target?.focus();
    });
  }, [open?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const onItemKey = (e, it) => {
    const idx = items.findIndex((x) => x.key === it.key);
    let next = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = items[Math.min(items.length - 1, idx + 1)];
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = items[Math.max(0, idx - 1)];
    else if (e.key === "Home") next = items[0];
    else if (e.key === "End") next = items[items.length - 1];
    else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openFor(it.key, it.id);
      return;
    }
    if (next) {
      e.preventDefault();
      setFocusKey(next.key);
      textRef.current?.querySelector(`[data-key="${CSS.escape(next.key)}"]`)?.focus();
    }
  };

  const decide = (id, status) => {
    onDecide(id, status);
  };

  const renderRun = (seg, i) => {
    if (seg.kind === "text") return <span key={i}>{seg.text}</span>;
    const key = `${seg.findingId}@${seg.start}`;
    const it = itemByKey[key];
    const f = BY_ID[seg.findingId];
    if (!it) return <span key={i}>{seg.text}</span>; // dismissed, in Export: plain text
    const isOpen = open?.key === key;
    const common = {
      "data-key": key,
      role: "button",
      tabIndex: key === activeKey ? 0 : -1,
      "aria-haspopup": "dialog",
      "aria-expanded": isOpen,
      onClick: () => (isOpen ? close(false) : openFor(key, it.id)),
      onKeyDown: (e) => onItemKey(e, it),
      onFocus: () => {
        setFocusKey(key);
        setHover(it.id);
      },
      onBlur: () => setHover(null),
      onMouseEnter: () => setHover(it.id),
      onMouseLeave: () => setHover(null),
    };
    const stateWord = { found: "found", pending: "undecided", approved: "approved", retained: "kept unredacted", dismissed: "not sensitive" }[it.state];
    if (seg.kind === "redacted") {
      return (
        <span
          key={i}
          {...common}
          className={`chip${hover === it.id ? " is-hot" : ""}`}
          style={{ "--i": ORDER[it.id] }}
          aria-label={`${seg.text}, replacing ${f.label.toLowerCase()} ${seg.source}. Change decision`}
        >
          {seg.text}
        </span>
      );
    }
    return (
      <span
        key={i}
        {...common}
        className={`mark mark--${it.state}${hover === it.id ? " is-hot" : ""}`}
        aria-label={`${seg.text}: ${f.label.toLowerCase()}, ${stateWord}`}
      >
        {seg.text}
      </span>
    );
  };

  const counts = useMemo(() => {
    const c = { pending: 0, approved: 0, retained: 0, dismissed: 0 };
    for (const f of FINDINGS) c[statuses[f.id] || "pending"] += 1;
    return c;
  }, [statuses]);
  const decided = FINDINGS.length - counts.pending;
  const status =
    stage === "find"
      ? `${FINDINGS.length} found`
      : stage === "decide"
        ? `${decided} of ${FINDINGS.length} decided`
        : counts.pending
          ? `Not ready: ${counts.pending} undecided`
          : "Reviewed DOCX text";

  return (
    <div className={`sheet sheet--${stage}${revealed ? " is-revealed" : ""}${intro ? " is-intro" : ""}${geom?.wide ? " is-wide" : ""}`}>
      <div className="sheet__bar">
        <span className="sheet__file">{copy.sheet.file}</span>
        <div className="sheet__tabs" role="tablist" aria-label="Demo step">
          {STAGES.map((s, i) => (
            <button
              key={s}
              type="button"
              role="tab"
              id={`tab-${s}`}
              aria-selected={stage === s}
              aria-controls="paper"
              tabIndex={stage === s ? 0 : -1}
              className="sheet__tab"
              onClick={() => onStage(s)}
              onKeyDown={(e) => {
                const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
                if (!d) return;
                e.preventDefault();
                const next = STAGES[(i + d + STAGES.length) % STAGES.length];
                onStage(next);
                e.currentTarget.parentElement.querySelector(`#tab-${next}`)?.focus();
              }}
            >
              <span className="sheet__tab-num" aria-hidden="true">
                {i + 1}
              </span>
              <span className="sheet__tab-label">{copy.sheet.stages[s]}</span>
            </button>
          ))}
        </div>
        <span className="sheet__status" aria-live="polite">
          {status}
        </span>
      </div>

      <div className="paper" id="paper" role="tabpanel" aria-labelledby={`tab-${stage}`} ref={paperRef}>
        {geom ? <Ink geom={geom} items={items} notes={notes} marginLeft={marginLeft} hover={hover} stage={stage} /> : null}

        <div className="paper__text" ref={textRef} key={stage === "export" ? "export" : "source"} lang="tr">
          {paragraphs.map((p, pi) => (
            <p key={pi} className={`para para--${ROLES[pi]}`}>
              {p.map(renderRun)}
            </p>
          ))}
        </div>

        {geom?.wide ? (
          <div className="paper__margin" style={{ left: marginLeft }} aria-hidden="true">
            {notes.map((n) => {
              const f = BY_ID[n.id];
              const t = marginText(stage, f, n.state);
              return (
                <span
                  key={n.key}
                  className={`note note--${n.state}${hover === n.id ? " is-hot" : ""}${t.mono ? " note--mono" : ""}`}
                  style={{ top: n.top, "--i": ORDER[n.id] }}
                  onMouseEnter={() => setHover(n.id)}
                  onMouseLeave={() => setHover(null)}
                  onClick={() => openFor(n.key, n.id)}
                >
                  {t.text}
                </span>
              );
            })}
          </div>
        ) : null}

        {open && geom ? (
          <Popover
            ref={popRef}
            finding={BY_ID[open.id]}
            status={statuses[open.id] || "pending"}
            rects={geom.rects[open.key]}
            geom={geom}
            onDecide={(s) => {
              decide(open.id, s);
              close(true);
            }}
            onClose={() => close(true)}
          />
        ) : null}
      </div>
    </div>
  );
}

// The visx layer: tint and brackets under each mark, leader curves to the margin.
function Ink({ geom, items, notes, marginLeft, hover, stage }) {
  const noteByKey = Object.fromEntries(notes.map((n) => [n.key, n]));
  return (
    <svg className="ink" width={geom.width} height={geom.height} aria-hidden="true" focusable="false">
      {items
        .filter((it) => it.kind === "marked")
        .map((it) => {
          const rects = geom.rects[it.key];
          if (!rects?.length) return null;
          const first = rects[0];
          const last = rects[rects.length - 1];
          const y1 = first.y - 1;
          const y2 = first.y + first.h + 1;
          const ly1 = last.y - 1;
          const ly2 = last.y + last.h + 1;
          const lx = first.x + 0.75;
          const rx = last.x + last.w - 0.75;
          const note = noteByKey[it.key];
          const hot = hover === it.id;
          return (
            <Group
              key={it.key}
              data-ink-key={it.key}
              className={`ink-mark ink-mark--${it.state}${hot ? " is-hot" : ""}`}
              style={{ "--i": ORDER[it.id] }}
            >
              {rects.map((r, i) => (
                <rect key={i} className="ink-tint" x={r.x} y={r.y} width={r.w} height={r.h} rx={2} />
              ))}
              <path className="ink-bracket ink-bracket--open" d={`M${lx + 4.5},${y1} H${lx} V${y2} H${lx + 4.5}`} />
              <path className="ink-bracket ink-bracket--close" d={`M${rx - 4.5},${ly1} H${rx} V${ly2} H${rx - 4.5}`} />
              {note ? (
                <LinkHorizontal
                  className="ink-leader"
                  data={{
                    source: { x: geom.textRight + 12, y: note.anchorY },
                    target: { x: marginLeft - 6, y: note.top + LABEL_H / 2 },
                  }}
                  x={(d) => d.x}
                  y={(d) => d.y}
                  pathLength={1}
                />
              ) : null}
              {note ? (
                <circle className="ink-leader-dot" cx={geom.textRight + 12} cy={note.anchorY} r={2} />
              ) : null}
            </Group>
          );
        })}
    </svg>
  );
}

// Only ever rendered on the client (nothing is open in the prerendered page), so
// it may read the viewport: below the wide layout it is a bottom sheet.
const Popover = forwardRef(function Popover({ finding, status, rects, geom, onDecide, onClose }, ref) {
  const narrow = !window.matchMedia("(min-width: 1080px)").matches;
  const W = 316;
  let style;
  if (!narrow && rects?.length) {
    const last = rects[rects.length - 1];
    const left = Math.max(12, Math.min(last.x, geom.width - W - 12));
    const below = last.y + last.h + 10;
    style = { left, top: below, width: W };
  }
  const onKey = (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
      return;
    }
    const radios = Array.from(e.currentTarget.querySelectorAll("[role=radio]"));
    const idx = radios.indexOf(document.activeElement);
    if (idx >= 0 && (e.key === "ArrowDown" || e.key === "ArrowRight" || e.key === "ArrowUp" || e.key === "ArrowLeft")) {
      e.preventDefault();
      const d = e.key === "ArrowDown" || e.key === "ArrowRight" ? 1 : -1;
      radios[(idx + d + radios.length) % radios.length].focus();
    }
  };
  const options = ["approved", "dismissed", "retained"];
  return (
    <div
      ref={ref}
      className={`pop${narrow ? " pop--sheet" : ""}`}
      style={style}
      role="dialog"
      aria-label={`${finding.label}: ${finding.sample}`}
      onKeyDown={onKey}
    >
      <div className="pop__head">
        <span className="pop__label">{finding.label}</span>
        <button type="button" className="pop__close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      <p className="pop__sample">
        {finding.sample}
        {finding.masked ? <span className="pop__masked"> digits masked on this page</span> : null}
      </p>
      <p className="pop__basis">
        <span className="pop__risk">{RISK[finding.risk]}.</span> {finding.basis}
      </p>
      <div className="pop__options" role="radiogroup" aria-label="Decision">
        {options.map((s) => (
          <button
            key={s}
            type="button"
            role="radio"
            aria-checked={status === s}
            tabIndex={status === s || (status === "pending" && s === "approved") ? 0 : -1}
            className={`pop__option pop__option--${s}`}
            onClick={() => onDecide(s)}
          >
            <span className="pop__verb">{copy.decisions[s].verb}</span>
            <span className="pop__effect">
              {s === "approved" ? (
                <>
                  Becomes <span className="pop__ph">{finding.placeholder}</span> wherever this text appears.
                </>
              ) : (
                copy.decisions[s].effect(finding.placeholder)
              )}
            </span>
          </button>
        ))}
      </div>
      {status !== "pending" ? (
        <button type="button" className="pop__undo" onClick={() => onDecide("pending")}>
          Undo this decision
        </button>
      ) : null}
    </div>
  );
});
