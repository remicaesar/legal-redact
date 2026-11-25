// Used once, at build time, by scripts/prerender.mjs: the page's markup as the
// first client render will produce it, so the browser can hydrate it.
import { renderToString } from "react-dom/server";
import App from "./App.jsx";

export function render() {
  return renderToString(<App />);
}
