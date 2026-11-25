// The production entry: the markup is already in the page (scripts/prerender.mjs
// rendered it into templates/landing.html), so React attaches to it.
import { StrictMode } from "react";
import { hydrateRoot } from "react-dom/client";
import "./page.css";
import App from "./App.jsx";

hydrateRoot(
  document.getElementById("root"),
  <StrictMode>
    <App />
  </StrictMode>,
);
