// The `npm run dev` entry: nothing is prerendered, so React renders from scratch.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./page.css";
import App from "./App.jsx";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
