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


## GitHub-native Daily Downburst handoff

The Operational Readiness Hub no longer depends on a scheduled ChatGPT task
writing to GitHub. GitHub Actions generates the 8:30 AM Initial and 11:00 AM
Model Refresh handoffs directly by calling the OpenAI Responses API with the
repository's current ECONet/HRRR context and current official NWS/SPC web
context.

One repository secret is required:

1. Open this repository in GitHub.
2. Go to **Settings > Secrets and variables > Actions**.
3. Under **Repository secrets**, choose **New repository secret**.
4. Name it exactly:
   `OPENAI_API_KEY`
5. Paste an OpenAI API project key with permission to call the Responses API.
6. Save the secret.

Optional model override:

- Under **Settings > Secrets and variables > Actions > Variables**, add
  `OPENAI_MODEL` if a different supported API model is desired.
- If not supplied, the workflow defaults to `gpt-5.1`.

The API key is never written to repository files or workflow logs.

Production workflow:

- `.github/workflows/generate-downburst-handoff.yml`
- Morning target: approximately 8:35 AM Eastern
- Late-morning target: approximately 11:05 AM Eastern
- The resilient Hub clock retries the appropriate run at later :17/:47
  checkpoints when needed.
- The generator is idempotent: if the requested run is already published for
  the current Eastern date, it exits before making another OpenAI API call.
- A committed handoff updates `current/downburst-outlook.json`, which then
  triggers the normal Operational Readiness Hub snapshot rebuild.

If the OpenAI API call fails or the secret is missing, the workflow does not
write a replacement handoff. The Hub's existing unavailable/degraded-data
rules remain the fail-safe behavior.
