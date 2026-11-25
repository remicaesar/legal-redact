// Writes ../templates/landing.html: landing.template.html with the React app's
// server render (the `vite build --ssr` output in .ssr/) in place of
// <!--app-html-->. Run by `npm run build`, after both vite builds.
import { readFileSync, writeFileSync } from "node:fs";
import { render } from "../.ssr/entry-server.js";

const here = (path) => new URL(path, import.meta.url);
const template = readFileSync(here("../landing.template.html"), "utf8");
const html = render();

// The markup goes inside {% raw %}, but the template around it is Jinja: refuse
// anything that could end the raw block or open a tag.
if (/\{%|%\}|\{\{|\{#/.test(html)) throw new Error("prerender: the rendered page contains Jinja syntax");
if (!template.includes("<!--app-html-->")) throw new Error("prerender: placeholder missing from the template");

writeFileSync(here("../../templates/landing.html"), template.replace("<!--app-html-->", html));
console.log(`prerender: templates/landing.html (${html.length} characters of markup)`);
