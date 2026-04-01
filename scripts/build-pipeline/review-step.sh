#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common.sh
source "${SCRIPT_DIR}/common.sh"

print_usage() {
  cat <<'USAGE'
Usage: review-step.sh [pr_id] [extra reflex-review args]

Runs Reflex Reviewer review flow for PR Build Pipeline steps.

PR id resolution order:
  1) first positional argument
  2) PR_ID
  3) VCS_PR_ID
  4) BITBUCKET_PR_ID
  5) BITBUCKET_PULL_REQUEST_ID
  6) PULL_REQUEST_ID

Required environment variables:
  RR_REPOSITORY_CLONE_URL
  TEAM_NAME
  PRIMARY_MODEL
  VCS_BASE_URL
  VCS_PROJECT_KEY
  VCS_REPO_SLUG
  VCS_TOKEN

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

PR_ID_INPUT=""
if [[ $# -gt 0 && "${1}" =~ ^[0-9]+$ ]]; then
  PR_ID_INPUT="$1"
  shift
fi
EXTRA_ARGS=("$@")

REPO_ROOT="$(rr_repo_root_from_script_dir "${SCRIPT_DIR}")"
rr_require_repo_layout "${REPO_ROOT}"

PYTHON_BIN="$(rr_python_bin "${REPO_ROOT}")"
rr_require_runtime_installation "${PYTHON_BIN}" "${REPO_ROOT}"

rr_require_runtime_env

PR_ID="$(rr_resolve_pr_id "${PR_ID_INPUT}")" || {
  rr_error "Unable to resolve PR id from args/environment."
  print_usage
  exit 1
}
rr_validate_pr_id "${PR_ID}"

cd "${REPO_ROOT}"
rr_log "Invoking review flow. pr_id=${PR_ID}"

cmd=(
  "${PYTHON_BIN}" -m reflex_reviewer.review
  --vcs-type bitbucket
  --team-name "${TEAM_NAME}"
  --primary-model "${PRIMARY_MODEL}"
  --pr-id "${PR_ID}"
)

if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  cmd+=("${EXTRA_ARGS[@]}")
fi

"${cmd[@]}"
