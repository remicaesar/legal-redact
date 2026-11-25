# Bundled brand fonts

Self-hosted so the app works offline and never requests Google Fonts or a CDN.
`@font-face` rules are at the top of `static/workstation.css`. Each family is
licensed under the SIL Open Font License 1.1; the licence sits next to its files.

| Family | Files | Font source | Licence source |
|---|---|---|---|
| Newsreader (roman + italic, variable `wght` 200–800, `opsz` 6–72) | `newsreader/` | https://fonts.google.com/specimen/Newsreader (served from fonts.gstatic.com, v26) | https://raw.githubusercontent.com/google/fonts/main/ofl/newsreader/OFL.txt |
| Hanken Grotesk (variable `wght` 100–900) | `hanken-grotesk/` | https://fonts.google.com/specimen/Hanken+Grotesk (fonts.gstatic.com, v12) | https://raw.githubusercontent.com/google/fonts/main/ofl/hankengrotesk/OFL.txt |
| IBM Plex Mono (static 400, 500, 600) | `ibm-plex-mono/` | https://fonts.google.com/specimen/IBM+Plex+Mono (fonts.gstatic.com, v20) | https://raw.githubusercontent.com/google/fonts/main/ofl/ibmplexmono/OFL.txt |
| Courier Prime (static 400; the wordmark's `[Redact]` only) | `courier-prime/` | https://fonts.google.com/specimen/Courier+Prime (fonts.gstatic.com, v11); upstream project https://github.com/quoteunquoteapps/CourierPrime | https://raw.githubusercontent.com/google/fonts/main/ofl/courierprime/OFL.txt |

Each family has a `latin` and a `latin-ext` WOFF2 subset, split on Google Fonts'
own `unicode-range`s; Turkish needs both (`ı` is in latin, `ğ ş İ` in latin-ext).
Files were fetched from the Google Fonts CSS2 API
(`https://fonts.googleapis.com/css2?family=...`) with a WOFF2-capable user agent.

Adding a `font-weight` for IBM Plex Mono beyond 400/500/600, or for Courier Prime
beyond 400, means adding its files here too; otherwise the browser synthesises
the weight. Courier Prime is the 1c logo's Courier-style mono and is reserved for
`.wordmark__redact` (`--font-wordmark`); all other mono UI text is IBM Plex Mono.
