# Repository Guidelines

## Project Structure

- `tools/`: Python scripts that fetch GitCode PR data and generate reports.
  - `tools/gitcode_pr_report_site.py`: generates the static dashboard HTML.
  - `tools/gitcode_pr_multi_repo.py`: prints a CLI report (useful for debugging).
- `site/`: generated static site output.
  - `site/index.html`: **generated file**; update it by running the generator.
- `.github/`: GitHub Actions + default config.
  - `.github/gitcode_pr_config.toml`: default report config used by the site generator.

## Generated Files Policy

- Treat everything under `site/` as **generated output**.
- Do **not** hand-edit files in `site/`. Make changes in `tools/gitcode_pr_report_site.py` (or related generator code) and then re-generate.
- PRs that touch `site/` must include the generator change that produced the output.

## Build, Test, and Development Commands

This repo is script-driven (no build system).

- Generate the dashboard HTML:  
  `python3 tools/gitcode_pr_report_site.py -c .github/gitcode_pr_config.toml -o site/index.html`
- Run the CLI report:  
  `python3 tools/gitcode_pr_multi_repo.py -c .github/gitcode_pr_config.toml`
- Quick sanity check (syntax only):  
  `python3 -m py_compile tools/gitcode_pr_report_site.py`

Dependencies: Python **3.11+** (uses `tomllib`) and `requests` (`python3 -m pip install requests`).

## Coding Style & Naming Conventions

- Python: 4-space indentation, type hints preferred, keep functions small and single-purpose.
- HTML/JS: keep UI logic in the generator (`tools/gitcode_pr_report_site.py`) and re-generate `site/index.html` rather than hand-editing it.
- Data attributes: prefer `data-*` for UI filtering/sorting state (see existing `.pr-card` usage).

## Testing Guidelines

No dedicated test suite currently. For changes:

- Run `python3 -m py_compile ...` and open `site/index.html` in a browser for a quick UI smoke test.

## Commit & Pull Request Guidelines

- Commit messages generally follow a Conventional Commits style (`feat: ...`, `refactor: ...`, `chore: ...`).
- PRs should describe the user-facing change and include a screenshot for UI changes.
- If you change the generator, update the generated output in the same PR (`tools/...` + `site/index.html`).

## Configuration & Security

- Prefer setting `GITCODE_TOKEN` (or `GITCODE_PAT`) via environment variables; avoid committing secrets.
- Update `.github/gitcode_pr_config.toml` to change repos/users/groups used in the report.
