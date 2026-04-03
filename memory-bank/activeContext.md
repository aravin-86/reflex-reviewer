# Active Context

## Current focus
- Keep Build Pipeline execution simple with setup-first repository/runtime bootstrapping.
- Ensure Build Pipeline step scripts run from setup-prepared repo context with cloned-repo local `.venv` runtime by default.
- Keep runtime setup hardened for Build Pipeline runner hosts using cloned-repo local virtualenv bootstrap and fail-fast checks.
- Keep local unit-test bootstrap deterministic under PEP 668 environments via isolated virtualenv usage.
- Enforce strict inline comment severity taxonomy in review/distill flows:
  - allowed severities: `CRITICAL`, `MAJOR`, `ADVISORY`
  - comments on test files must always be `ADVISORY`.
- Introduce simple LLM-as-a-Judge review orchestration with explicit `DRAFT_MODEL` + `JUDGE_MODEL` configuration.

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
  - pipeline-runner runtime bootstrap instructions,
  - explicit VCS support status: Bitbucket Data Center only (current) and GitHub as next target.

## Important implementation preferences/rules (repo-local)
- Keep runtime configuration centralized in `reflex_reviewer/config.py`.
- Prefer values sourced through `reflex_reviewer.toml` (env-backed where appropriate).
- Keep logging minimal and safe; avoid sensitive payload logging.

## Decisions captured in this update
- Build Pipeline native repository hooks are not supported for this integration model.
- Keep pipeline step scripts as thin shell wrappers around existing Python module entry points.
- Keep runtime value resolution centralized through env + `reflex_reviewer.toml` + `config.py`.
- Use asynchronous monthly/on-demand triggering for refine to avoid tying model training to synchronous PR lifecycle latency.
- Use `requirements.txt` for pipeline-runner runtime dependency installation in cloned repo local `.venv` (overrideable via `RR_VENV_DIR`).
- Add fail-fast runtime checks in Build Pipeline scripts for Python version, required module imports, repository layout, and data-directory writability.
- Prefer runtime auto-discovery via `<repo>/.venv/bin/python` with `PYTHON_BIN` / `RR_VENV_DIR` overrides.
- `setup-pipeline-runtime.sh` is now the only script that clones `RR_REPOSITORY_CLONE_URL` into `RR_REPOSITORY_DIR`.
- Setup clone flow always removes `RR_REPOSITORY_DIR` (when present) and performs a fresh clone for deterministic execution.
- Step scripts (`review-step.sh` / `distill-step.sh` / `refine-step.sh`) no longer clone/re-exec; they fail fast unless setup has prepared repository/runtime.
- Optional clone target selection supported via `RR_REPOSITORY_REF` (branch/tag).
- Pipeline runtime resolver now logs selected interpreter path with minimal safe verbosity.
- Review flow now normalizes model-returned severities and coerces test-file comments to `ADVISORY` before dedupe keying and posting.
- Distill flow now extracts normalized bot-comment severity metadata and includes it in batched sentiment payloads, with test-file advisory coercion.
- Severity parsing in both flows now defaults unknown/missing labels to `ADVISORY` for safety and consistency.
- Add package test extra (`.[test]`) in `pyproject.toml` for explicit local test tooling install.
- Keep local verification path venv-first (`python3 -m venv .venv` + editable install) to avoid system Python mutation under externally managed environments.
- Align config unit test expectation with current TOML/runtime default where `model_endpoint` defaults to `chat_completions`.
- Document local unit test bootstrap and execution commands in `README.md`.
- Full unit suite verification performed successfully in local venv (`84 tests`, `OK`).
- OAuth2 helper module now supports direct execution via `python3 -m reflex_reviewer.oauth2` to print access token to stdout for pipeline/shell usage.
- Direct-run OAuth2 path configures minimal logging, preserves existing token cache/refresh behavior, and exits non-zero on fetch failures without logging token values.
- Model configuration now uses explicit `DRAFT_MODEL` and `JUDGE_MODEL` names (replacing `PRIMARY_MODEL`) across runtime, CLI, TOML/env, and pipeline wrappers.
- Review flow now runs a strict two-stage inference path:
  - draft stage (`review_system_prompt.md` + `review_user_prompt.md`) using `DRAFT_MODEL`,
  - judge stage (`judge_review_system_prompt.md` + `judge_review_user_prompt.md`) using `JUDGE_MODEL`.
- Judge stage output is now the only payload posted to VCS; existing severity normalization, test-file advisory coercion, and dedupe safeguards remain in final posting path.
- README architecture diagram now explicitly visualizes the review flow as `Draft Review (DRAFT_MODEL) -> LLM Judge (JUDGE_MODEL) -> VCS posting` to match runtime behavior.
- Distill/refine flows and pipeline wrappers now consume `--draft-model` / `DRAFT_MODEL` naming for consistency.
- Build Pipeline review preflight now requires `JUDGE_MODEL` in addition to `DRAFT_MODEL`.
- Removed obsolete provider-specific pipeline example assets and cleaned corresponding repository/doc references.
- README now clearly states VCS support status in prominent/limitations/future sections:
  - current support: **Bitbucket Data Center only**,
  - next target: **GitHub support**.
- LiteLLM client request logs now include a safe, best-effort context-window token estimate for both Chat Completions and Responses API calls.
- Context-window token estimate uses a lightweight character-based heuristic (~4 chars/token) and avoids logging raw prompt/input payloads.
- Removed explicit `certifi` pin from `requirements.txt`; runtime dependency set now relies on Python/requests default certificate handling without a direct project-level certifi requirement.

## Next likely updates
- Add deployment-specific examples showing host paths for `RR_REPOSITORY_DIR` and venv placement.
- Add automated shell tests for setup-first bootstrap and step precondition behavior in pipeline scripts.
- Add CI-friendly command wrappers for local/PR test execution (optional quality-of-life improvement).
- Plan and implement vector-DB-backed preference memory from distilled DPO pairs for on-the-fly review guidance (retrieve accepted/rejected exemplars during `review`, keep `refine` as offline optimization).
- Add dedicated unit coverage for judge-stage payload quality constraints (for example, verifying invalid anchor removal and schema-safe rewriting behavior).
