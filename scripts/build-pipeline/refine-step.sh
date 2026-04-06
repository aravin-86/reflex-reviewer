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
  TEAM_NAME
  DRAFT_MODEL
  LLM_API_BASE_URL
  VCS_BASE_URL
  VCS_PROJECT_KEY
  VCS_REPO_SLUG
  VCS_TOKEN
  DPO_TRAINING_DATA_DIR

LLM API auth:
  - either LLM_API_KEY
  - or OAUTH2_TOKEN_URL + OAUTH2_USER_ID + OAUTH2_USER_SECRET

Optional:
  LLM_API_PROXY_URL (optional proxy URL for outbound LLM API calls)
  RR_PIPELINE_INSTALL_MODE (package [default] or clone)
  RR_REPOSITORY_DIR (clone mode only; prepared checkout dir from setup script; default: <cwd>/.reflex-reviewer-clone)
  PYTHON_BIN (optional explicit interpreter override)
  RR_VENV_DIR (optional explicit venv dir override; resolved as <RR_VENV_DIR>/bin/python)
  Default runtime without overrides:
    - package mode: <cwd>/.reflex-reviewer-venv/bin/python (if created by setup script)
    - clone mode: <repo>/.venv/bin/python
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  print_usage
  exit 0
fi

EXTRA_ARGS=("$@")

INSTALL_MODE="$(rr_pipeline_install_mode)"
REPO_ROOT=""
if [[ "${INSTALL_MODE}" == "clone" ]]; then
  REPO_ROOT="$(rr_require_prepared_repository_checkout "${SCRIPT_DIR}")"
fi

PYTHON_BIN="$(rr_python_bin "${REPO_ROOT}")"
rr_require_runtime_installation "${PYTHON_BIN}" "${REPO_ROOT}"

rr_require_runtime_env
rr_require_env "DPO_TRAINING_DATA_DIR"
rr_ensure_directory "${DPO_TRAINING_DATA_DIR}"

if [[ -n "${REPO_ROOT}" ]]; then
  cd "${REPO_ROOT}"
fi
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
