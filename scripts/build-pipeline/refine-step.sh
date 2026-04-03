#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common.sh
source "${SCRIPT_DIR}/common.sh"

print_usage() {
  cat <<'USAGE'
Usage: refine-step.sh [extra reflex-refine args]

Runs Reflex Reviewer refine flow for monthly scheduled or on-demand steps.

Required environment variables:
  RR_REPOSITORY_CLONE_URL
  TEAM_NAME
  DRAFT_MODEL
  VCS_BASE_URL
  VCS_PROJECT_KEY
  VCS_REPO_SLUG
  VCS_TOKEN
  DPO_TRAINING_DATA_DIR

LiteLLM auth:
  - either LITELLM_API_KEY
  - or OAUTH2_TOKEN_URL + OAUTH2_USER_ID + OAUTH2_USER_SECRET

Optional:
  RR_REPOSITORY_DIR (default: <cwd>/.reflex-reviewer-clone)
  RR_REPOSITORY_REF (optional branch/tag for clone)
  PYTHON_BIN (optional explicit interpreter override)
  RR_VENV_DIR (optional explicit venv dir override; resolved as <RR_VENV_DIR>/bin/python)
  Default runtime without overrides: <repo>/.venv/bin/python
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  print_usage
  exit 0
fi

rr_bootstrap_cloned_pipeline_script "$(basename "${BASH_SOURCE[0]}")" "$@"

EXTRA_ARGS=("$@")

REPO_ROOT="$(rr_repo_root_from_script_dir "${SCRIPT_DIR}")"
rr_require_repo_layout "${REPO_ROOT}"

PYTHON_BIN="$(rr_python_bin "${REPO_ROOT}")"
rr_require_runtime_installation "${PYTHON_BIN}" "${REPO_ROOT}"

rr_require_runtime_env
rr_require_env "DPO_TRAINING_DATA_DIR"
rr_ensure_directory "${DPO_TRAINING_DATA_DIR}"

cd "${REPO_ROOT}"
rr_log "Invoking refine flow."

cmd=(
  "${PYTHON_BIN}" -m reflex_reviewer.refine
  --team-name "${TEAM_NAME}"
  --draft-model "${DRAFT_MODEL}"
  --dpo-training-data-dir "${DPO_TRAINING_DATA_DIR}"
)

if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  cmd+=("${EXTRA_ARGS[@]}")
fi

"${cmd[@]}"
