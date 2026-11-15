# Vendored files

| Name | Version | Source | File | SHA-256 | Licence | Date |
|---|---|---|---|---|---|---|
| Chart.js | 4.6.1 | https://registry.npmjs.org/chart.js/-/chart.js-4.6.1.tgz (`package/dist/chart.umd.min.js`) | `chart.umd.min.js` | 87364cc2b2a76a6684929e7d6b80b18a897794db38400caff1fcf1791c6925d2 | MIT (`LICENSE.md`) | 2026-11-14 |

The file is served unchanged by the web app (`/static/vendor/chart.umd.min.js`) and loaded only by `/analytics` and `/health`. A test checks the SHA-256 above against the file; `.gitattributes` keeps the file byte for byte (`-text`).
