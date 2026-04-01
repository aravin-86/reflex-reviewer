# Active Context

## Current focus
- Keep Build Pipeline execution simple with clone-first repository bootstrapping.
- Ensure Build Pipeline step scripts always run from a freshly cloned remote checkout.
- Keep runtime setup hardened for Build Pipeline runner hosts using a dedicated virtualenv bootstrap and fail-fast checks.

## Current repository snapshot
- Project is an automated AI PR review system with three operational flows:
  - `review` (actuation)
  - `distill` (feedback signal extraction)
  - `refine` (DPO-oriented optimization)
- Core package: `reflex_reviewer/`
- Build Pipeline script wrappers available under `scripts/build-pipeline/`:
  - `common.sh`
  - `review-step.sh`
  - `distill-step.sh`
  - `refine-step.sh`
  - `setup-pipeline-runtime.sh`
- README now includes:
  - required env/auth variables,
  - event-to-script architecture guidance,
  - pipeline-runner runtime bootstrap instructions.

## Important implementation preferences/rules (repo-local)
- Keep runtime configuration centralized in `reflex_reviewer/config.py`.
- Prefer values sourced through `reflex_reviewer.toml` (env-backed where appropriate).
- Keep logging minimal and safe; avoid sensitive payload logging.

## Decisions captured in this update
- Build Pipeline native repository hooks are not supported for this integration model.
- Keep pipeline step scripts as thin shell wrappers around existing Python module entry points.
- Keep runtime value resolution centralized through env + `reflex_reviewer.toml` + `config.py`.
- Use asynchronous monthly/on-demand triggering for refine to avoid tying model training to synchronous PR lifecycle latency.
- Use `requirements.txt` for pipeline-runner runtime dependency installation in a dedicated venv.
- Add fail-fast runtime checks in Build Pipeline scripts for Python version, required module imports, repository layout, and data-directory writability.
- Prefer managed runtime auto-discovery via `<repo>/.build-pipeline-venv/bin/python` with `PYTHON_BIN` / `RR_VENV_DIR` overrides.
- Build Pipeline step scripts now bootstrap by cloning `RR_REPOSITORY_CLONE_URL` into `RR_REPOSITORY_DIR` before execution.
- Clone flow is intentionally simple/deterministic: existing checkout directory is removed, then repository is recloned.
- Step scripts re-exec from the cloned repository's `scripts/build-pipeline/` path using `RR_USE_CLONED_PIPELINE_SCRIPT=1` guard to avoid recursion.
- Optional clone target selection supported via `RR_REPOSITORY_REF` (branch/tag).

## Next likely updates
- Add deployment-specific examples showing host paths for `RR_REPOSITORY_DIR` and venv placement.
- Add automated shell tests for clone-bootstrap and re-exec behavior in pipeline scripts.
