import { createReadStream, statSync } from "node:fs";
import { extname, resolve, sep } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const APP_STATIC = resolve(__dirname, "../static");
const TYPES = { ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".woff2": "font/woff2" };

// `npm run dev` serves the app's own /static/ (workstation.css, the fonts, the
// favicon) the way Flask does, so the dev page looks like /welcome. Build output
// is unaffected: the page links those files at runtime and never bundles them.
function appStatic() {
  return {
    name: "legal-redact-app-static",
    configureServer(server) {
      server.middlewares.use("/static", (req, res, next) => {
        const file = resolve(APP_STATIC, "." + decodeURIComponent(req.url.split("?")[0]));
        if (!file.startsWith(APP_STATIC + sep)) return next();
        try {
          if (!statSync(file).isFile()) return next();
        } catch {
          return next();
        }
        res.setHeader("Content-Type", TYPES[extname(file)] || "application/octet-stream");
        createReadStream(file).pipe(res);
      });
    },
  };
}

// `vite build` writes the client bundle into the app's static/landing/ with fixed
// names (templates/landing.html links them). `vite build --ssr` writes the
// server render used once, at build time, by scripts/prerender.mjs.
export default defineConfig(({ isSsrBuild }) => ({
  plugins: [react(), appStatic()],
  publicDir: false,
  base: "/static/landing/",
  server: { host: "127.0.0.1", port: 5190, strictPort: false },
  build: isSsrBuild
    ? { outDir: ".ssr", emptyOutDir: true }
    : {
        outDir: "../static/landing",
        emptyOutDir: true,
        cssCodeSplit: false,
        modulePreload: false,
        rollupOptions: {
          input: resolve(__dirname, "src/main.jsx"),
          output: {
            entryFileNames: "landing.js",
            chunkFileNames: "landing-[name].js",
            assetFileNames: "landing[extname]",
          },
        },
      },
}));
