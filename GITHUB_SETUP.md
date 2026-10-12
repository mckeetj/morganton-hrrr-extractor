# GitHub setup / V2 update

This repository publishes `burke-hrrr.json` through GitHub Pages.

## Existing repository: upgrade to V2

Replace/add the V2 files in the same repository, preserving their paths. The
most important application files are under `burke_hrrr/`; V2 also adds
`burke_hrrr/operational.py` and updates the tests.

Then update `.github/workflows/update-hrrr.yml` to the V2 workflow. The workflow
uses the current Pages action majors (`configure-pages@v6`,
`upload-pages-artifact@v5`, and `deploy-pages@v5`) and validates that the core V2
diagnostics are actually present before it publishes the JSON.

Run:

**Actions -> Update Burke County HRRR Data -> Run workflow**

A successful V2 run should pass:

1. dependency installation;
2. ecCodes self-check;
3. all unit tests;
4. HRRR extraction;
5. V2 JSON validation;
6. Pages deployment.

After deployment, verify:

`https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/burke-hrrr.json`

and confirm that the file contains:

- `"schema_version": 2`
- a non-empty `operational_summary.metrics`
- non-empty hourly `key_diagnostics`

## Schedule

The included workflow runs at **9:45 AM America/New_York every day**, providing
a nominal 30-minute buffer before a 10:15 AM downstream weather assessment.
GitHub scheduled workflows are best-effort and can occasionally start late.

## Freshness rule

A downstream task should inspect `generated_at`, `cycle_initialized`,
`cycle_age_hours`, and `through` before using the JSON. If the morning run has
not refreshed the file, identify the HRRR extractor data as stale rather than
presenting it as current guidance.

## Public repository caution

This repository should contain only public weather/model code and output. Do not
place City-sensitive, customer, credential, or internal operational data in it.
