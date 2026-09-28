# Vendored frontend libraries

The terminal loads no JavaScript or CSS from a CDN (PK-18). A compromised
or silently changed CDN file would run with the page's full same-origin
power, including approving tickets. The two third-party libraries are
therefore served from here, at exact versions.

| File | Package | Version | License | SHA-256 | SRI (in `index.html`) |
|---|---|---|---|---|---|
| `Sortable-1.15.2.min.js` | `sortablejs` | 1.15.2 | MIT (`Sortable-LICENSE.txt`) | `ca68430703c4f5960e90735867c6e94d29b5a3de37107d8100e5a301007e9e6e` | `sha384-BSxuMLxX+FCbTdYec3TbXlnMGEEM2QXTFdtDaveen71o+jswm2J36+xFqp8k4VHM` |
| `lightweight-charts-5.2.1.standalone.production.js` | `lightweight-charts` | 5.2.1 | Apache-2.0 (`lightweight-charts-LICENSE.txt`; the package ships no NOTICE file) | `e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf` | `sha384-KkYqTZlM13Zya6fVUF3IGCBOQ1ehFeJVVgxaVvD/0MebineF9EI8Jkd3NnWNChqN` |

## Provenance

Each file was taken from its package's npm registry tarball:

- `https://registry.npmjs.org/sortablejs/-/sortablejs-1.15.2.tgz`, verified
  against the registry's `dist.integrity`,
  `sha512-FJF5jgdfvoKn1MAKSdGs33bIqLi3LmsgVTliuX6iITj834F+JRQZN90Z93yql8h0K2t0RwDPBmxwlbZfDcxNZA==`.
  The file is `package/Sortable.min.js`.
- `https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-5.2.1.tgz`,
  verified against the registry's `dist.integrity`,
  `sha512-IVwoK1RLFiLPubaKIjNbtjWLnpPMqiABSrTay6whmNa8L1+19292VtHJ+BWyPUuLCwF0tcQlhEWd1CLB2a1nsQ==`.
  The file is `package/dist/lightweight-charts.standalone.production.js`.

Both files are byte-identical to the CDN copies the app loaded before PK-18:
cdnjs `ajax/libs/Sortable/1.15.2/Sortable.min.js` and jsDelivr
`npm/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.js`.

## Upgrading

1. Fetch the new version from the npm registry tarball and check the tarball
   against `dist.integrity`.
2. Replace the file here and rename it with the new version.
3. Update the `src` and `integrity` in `frontend/index.html` (the value is
   `sha384-` followed by the base64 SHA-384 of the file) and update this table.
4. Run `pytest tests/test_security_hardening.py`. It fails if an `integrity`
   value does not match its file, or if any script or stylesheet points off-site.

Lightweight Charts changed its series API between v4 and v5, so test the
Signal Detail chart after any major-version change.
