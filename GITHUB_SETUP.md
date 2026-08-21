# GitHub setup for the Burke County HRRR extractor

This package is ready to upload to a GitHub repository and publish the generated
`burke-hrrr.json` through GitHub Pages.

## 1. Create the repository

Create a **public** GitHub repository, for example:

`morganton-hrrr-extractor`

A public repository keeps the standard GitHub Actions runner and GitHub Pages
setup free for this use case. Do not place City-sensitive or customer data in
this repository.

## 2. Upload this package

Unzip this package on your computer. Upload **the contents of the folder**, not
the ZIP file itself, to the root of the new GitHub repository.

The repository root should contain at least:

- `.github/workflows/update-hrrr.yml`
- `.github/workflows/keep-schedule-active.yml`
- `burke_hrrr/`
- `tests/`
- `public/index.html`
- `pyproject.toml`
- `README.md`

No `requirements.txt` is required. `pyproject.toml` already declares the Python
dependencies, and the workflow installs the project with `pip install -e .`.

## 3. Enable GitHub Pages

In the repository:

1. Open **Settings**.
2. Open **Pages** under **Code and automation**.
3. Under **Build and deployment**, set **Source** to **GitHub Actions**.

## 4. Run the extractor manually once

Open:

**Actions -> Update Burke County HRRR Data -> Run workflow**

The workflow will:

1. Start an Ubuntu GitHub-hosted runner.
2. Install Python 3.12.
3. Install this project and its declared dependencies (`cfgrib`, `eccodes`,
   `metpy`, `numpy`, and `xarray`).
4. Run the ecCodes self-check.
5. Run the included unit tests.
6. Discover the newest HRRR cycle that can cover the requested period.
7. Download the needed NOAA/NCEP NOMADS subsets for the Burke County envelope.
8. Generate and validate `public/burke-hrrr.json`.
9. Publish the `public/` directory to GitHub Pages.

The scheduled run is configured for **9:45 AM America/New_York every day**.
This gives a 30-minute nominal buffer before a 10:15 AM ChatGPT weather task.
GitHub scheduled workflows are best-effort and may occasionally start late.

## 5. Find the stable URL

If the GitHub account is `YOUR-USERNAME` and the repository is
`morganton-hrrr-extractor`, the expected URL is:

`https://YOUR-USERNAME.github.io/morganton-hrrr-extractor/burke-hrrr.json`

The repository's Pages settings will show the actual published site URL after
the first successful deployment.

The site root also provides a small status page showing the JSON generation
and HRRR cycle timestamps.

## 6. Freshness rule for ChatGPT

The ChatGPT weather task should never assume that a reachable JSON file is
current. Before using the HRRR fields, it should inspect at least:

- `generated_at`
- `cycle_initialized`
- `cycle_age_hours`
- `through`

If the JSON was not regenerated for the current morning run, the task should
identify the HRRR extractor guidance as stale and should not present those
fields as current model guidance.

The extractor itself exits with an error when it cannot find/download/decode a
suitable complete HRRR cycle, so a failed run does not intentionally replace a
previous good file with fabricated or incomplete data.

## 7. Public-repository scheduled-workflow inactivity

GitHub documents that scheduled workflows in public repositories can be
automatically disabled after 60 days without repository activity. This package
therefore includes `.github/workflows/keep-schedule-active.yml`, which makes a
small timestamp commit once per month to keep actual repository commit activity
occurring.

If an organization policy prevents GitHub Actions from writing to the
repository, the HRRR/Pages workflow can still work, but the heartbeat workflow
may fail. In that case, periodically verify that the scheduled workflow remains
enabled.

## 8. What to send back after the first successful run

Send ChatGPT the published `burke-hrrr.json` URL. The 10:15 AM weather task can
then be updated to retrieve that stable URL and apply the freshness checks.
