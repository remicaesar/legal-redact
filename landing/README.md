# landing/ — the /welcome page

The public landing page is a small React + [visx](https://github.com/airbnb/visx)
app, built ahead of time. The app serves the build; it never runs Node.

| Built file (committed) | What it is |
|---|---|
| `templates/landing.html` | The page, **prerendered**: `landing.template.html` with the React app's markup inside `#root`. It extends `base.html`, so it reads without JavaScript and carries the same skeleton, favicon and title as every page. |
| `static/landing/landing.js` | The script that attaches to that markup: the demo's brackets, decisions and export view. |
| `static/landing/landing.css` | The page's layout. Fonts, tokens and the Legal [Redact] lockup come from `static/workstation.css`, which the page also links. |

## Rebuild

```bash
cd landing
npm ci            # once
npm run build     # vite build + vite build --ssr + scripts/prerender.mjs
```

Commit the rebuilt `templates/landing.html` and `static/landing/` with your change.
`npm run dev` serves the page with hot reload on http://127.0.0.1:5190, and the
app's `/static/` files are served alongside it.

## The demo is the product's output

Every finding, placeholder and gate state the demo draws comes from running the
app on the bundled synthetic petition, `samples/documents/turkish_petition.docx`.
Two things differ from the file on disk:

- its "Vekili: Av. ..." paragraph is left out, so the plaintiff is `[PERSON_1]`,
  as the headline says;
- its TCKN and VKN are shown with their middle digits masked.

`scripts/gen_demo.py` explains both.

```bash
# from the repository root, after any change to detection, placeholders or the gates
.venv/bin/python landing/scripts/gen_demo.py
cd landing && npm run build
node scripts/verify_plan.mjs   # the page's plan port agrees with the product
```

`tests/test_landing.py` regenerates the demo and fails when the committed
`src/demo.json` or `scripts/plan_cases.json` has drifted from what the app
produces. It also checks that:

- the page makes no request to another host;
- every link resolves;
- the gate's condition count and labels match the code;
- no full sample ID number reaches the page.

## Rules for editing

- **Copy lives in `src/copy.js`, each line next to what substantiates it.** No
  accuracy figure, no percentage, no customer claim (see `blueprints/landing.py`).
- **The page never computes the release gate.** The Gate section shows only the
  states `gen_demo.py` ran through the app, and says so for any other set of
  decisions.
- **`src/plan.js` is a port of `legal_analyzer.redaction_plan.plan_segments`**
  for this one document. `scripts/verify_plan.mjs` must keep passing, and CI runs it.
- **Nothing loads from another host.** No CDN, no Google Fonts, no analytics.
- **Invented data only.** The demo is the synthetic sample; never put a real
  document in it.
- **Prerendered markup must equal the first client render.** Nothing that
  decides markup may read the viewport or `window`. Phone and desktop layouts
  differ in CSS, and media queries are read after mount, for behaviour only.
