#!/usr/bin/env bash
set -euo pipefail

RR_MIN_PYTHON_MAJOR=3
RR_MIN_PYTHON_MINOR=9
RR_REQUIRED_PYTHON_MODULES=(
  "openai"
  "requests"
  "tenacity"
  "dotenv"
  "authlib"
)

rr_log() {
  echo "[reflex-pipeline] $*"
}

rr_error() {
  echo "[reflex-pipeline] ERROR: $*" >&2
}

rr_require_cmd() {
  local cmd_name="$1"
  if ! command -v "${cmd_name}" >/dev/null 2>&1; then
    rr_error "Required command is missing: ${cmd_name}"
    return 1
  fi

  return 0
}

rr_require_file() {
  local file_path="$1"
  if [[ ! -f "${file_path}" ]]; then
    rr_error "Required file is missing: ${file_path}"
    return 1
  fi

  return 0
}

rr_require_dir() {
  local dir_path="$1"
  if [[ ! -d "${dir_path}" ]]; then
    rr_error "Required directory is missing: ${dir_path}"
    return 1
  fi

  return 0
}

rr_require_env() {
  local env_name="$1"
  local env_value="${!env_name-}"

  if [[ -z "${env_value:-}" ]]; then
    rr_error "Required environment variable is missing: ${env_name}"
    return 1
  fi

  return 0
}

rr_require_llm_api_auth() {
  if [[ -n "${LLM_API_KEY:-}" ]]; then
    return 0
  fi

  rr_require_env "OAUTH2_TOKEN_URL"
  rr_require_env "OAUTH2_USER_ID"
  rr_require_env "OAUTH2_USER_SECRET"
}

rr_pipeline_install_mode() {
  local mode="${RR_PIPELINE_INSTALL_MODE:-package}"
  mode="$(printf '%s' "${mode}" | tr '[:upper:]' '[:lower:]')"

  case "${mode}" in
    clone|package)
      ;;
    *)
      rr_log "Unknown RR_PIPELINE_INSTALL_MODE='${mode}'. Falling back to 'package'." >&2
      mode="package"
      ;;
  esac

  echo "${mode}"
}

rr_package_install_target() {
  local target="${RR_PACKAGE_INSTALL_TARGET:-reflex-reviewer}"
  target="$(printf '%s' "${target}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  if [[ -z "${target}" ]]; then
    target="reflex-reviewer"
  fi

  echo "${target}"
}

rr_package_index_url() {
  local index_url="${RR_PACKAGE_INDEX_URL:-https://test.pypi.org/simple/}"
  index_url="$(printf '%s' "${index_url}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  if [[ -z "${index_url}" ]]; then
    index_url="https://test.pypi.org/simple/"
  fi

  echo "${index_url}"
}

rr_package_extra_index_url() {
  local extra_index_url="${RR_PACKAGE_EXTRA_INDEX_URL-https://pypi.org/simple/}"
  extra_index_url="$(printf '%s' "${extra_index_url}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

  echo "${extra_index_url}"
}

rr_repo_root_from_script_dir() {
  local script_dir="$1"
  (cd "${script_dir}/../.." && pwd)
}

rr_default_repository_dir() {
  echo "${RR_REPOSITORY_DIR:-${PWD}/.reflex-reviewer-clone}"
}

rr_clone_repository_checkout() {
  rr_require_cmd "git"
  rr_require_env "RR_REPOSITORY_CLONE_URL"

  local repo_url="${RR_REPOSITORY_CLONE_URL}"
  local repo_dir
  repo_dir="$(rr_default_repository_dir)"
  local repo_ref="${RR_REPOSITORY_REF:-}"

  if [[ -e "${repo_dir}" ]]; then
    rr_log "Removing existing repository directory before fresh clone: ${repo_dir}" >&2
    rm -rf "${repo_dir}"
  fi

  rr_log "Cloning repository checkout into: ${repo_dir}" >&2
  if [[ -n "${repo_ref}" ]]; then
    git clone --quiet --branch "${repo_ref}" "${repo_url}" "${repo_dir}"
  else
    git clone --quiet "${repo_url}" "${repo_dir}"
  fi

  echo "${repo_dir}"
}

rr_require_prepared_repository_checkout() {
  local install_mode
  install_mode="$(rr_pipeline_install_mode)"
  if [[ "${install_mode}" != "clone" ]]; then
    rr_error "Prepared repository checkout is only required when RR_PIPELINE_INSTALL_MODE=clone."
    return 1
  fi

  local script_dir="${1:-}"
  local repo_dir
  repo_dir="$(rr_default_repository_dir)"

  if [[ -d "${repo_dir}/.git" ]]; then
    rr_require_repo_layout "${repo_dir}" || return 1
    echo "${repo_dir}"
    return 0
  fi

  if [[ -n "${RR_REPOSITORY_DIR:-}" ]]; then
    rr_error "Prepared repository checkout is invalid or missing: ${repo_dir}. Run setup-pipeline-runtime.sh first."
    return 1
  fi

  if [[ -n "${script_dir}" ]]; then
    local local_repo_root
    local_repo_root="$(rr_repo_root_from_script_dir "${script_dir}")"
    if [[ -d "${local_repo_root}/.git" ]]; then
      rr_require_repo_layout "${local_repo_root}" || return 1
      echo "${local_repo_root}"
      return 0
    fi
  fi

  if [[ -e "${repo_dir}" ]]; then
    rr_error "Prepared repository checkout is invalid (missing .git): ${repo_dir}. Run setup-pipeline-runtime.sh first."
  else
    rr_error "Prepared repository directory is missing: ${repo_dir}. Run setup-pipeline-runtime.sh first."
  fi
  return 1
}

rr_require_repo_layout() {
  local repo_root="$1"
  rr_require_dir "${repo_root}"
  rr_require_file "${repo_root}/requirements.txt"
  rr_require_dir "${repo_root}/reflex_reviewer"
}

rr_default_venv_dir() {
  local repo_root="${1:-}"

  if [[ -n "${RR_VENV_DIR:-}" ]]; then
    echo "${RR_VENV_DIR}"
    return 0
  fi

  if [[ -n "${repo_root}" ]]; then
    echo "${repo_root}/.venv"
  else
    echo "${PWD}/.reflex-reviewer-venv"
  fi
}

rr_python_bin() {
  local repo_root="${1:-}"
  local bin="${PYTHON_BIN:-}"

  if [[ "${bin}" == "python3" && -n "${repo_root}" ]]; then
    bin=""
  fi

  if [[ -z "${bin}" && -n "${RR_VENV_DIR:-}" ]]; then
    local configured_venv_bin
    configured_venv_bin="${RR_VENV_DIR}/bin/python"
    if [[ -x "${configured_venv_bin}" ]]; then
      bin="${configured_venv_bin}"
    fi
  fi

  if [[ -z "${bin}" && -n "${repo_root}" ]]; then
    local local_repo_venv_bin
    local_repo_venv_bin="${repo_root}/.venv/bin/python"
    if [[ -x "${local_repo_venv_bin}" ]]; then
      bin="${local_repo_venv_bin}"
    fi
  fi

  if [[ -z "${bin}" && -z "${repo_root}" ]]; then
    local package_mode_venv_bin
    package_mode_venv_bin="$(rr_default_venv_dir)/bin/python"
    if [[ -x "${package_mode_venv_bin}" ]]; then
      bin="${package_mode_venv_bin}"
    fi
  fi

  if [[ -z "${bin}" && -n "${repo_root}" ]]; then
    rr_error "Python executable not found. Expected cloned repo local runtime at '${repo_root}/.venv/bin/python' or override PYTHON_BIN/RR_VENV_DIR."
    return 1
  fi

  if [[ -z "${bin}" ]]; then
    bin="python3"
  fi

  if ! command -v "$bin" >/dev/null 2>&1; then
    rr_error "Python executable not found: ${bin}"
    return 1
  fi

  rr_log "Using python runtime: $(command -v "$bin")" >&2
  command -v "$bin"
}

rr_require_python_min_version() {
  local python_bin="$1"

  if ! "${python_bin}" -c "import sys; sys.exit(0 if sys.version_info >= (${RR_MIN_PYTHON_MAJOR}, ${RR_MIN_PYTHON_MINOR}) else 1)"; then
    rr_error "Python ${RR_MIN_PYTHON_MAJOR}.${RR_MIN_PYTHON_MINOR}+ is required. configured='${python_bin}'"
    return 1
  fi

  return 0
}

rr_require_python_module() {
  local python_bin="$1"
  local module_name="$2"

  if ! "${python_bin}" -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('${module_name}') else 1)"; then
    rr_error "Python module is not installed in runtime '${python_bin}': ${module_name}"
    return 1
  fi

  return 0
}

rr_require_runtime_installation() {
  local python_bin="$1"
  local repo_root="${2:-}"

  rr_require_python_min_version "${python_bin}"

  local module_name
  for module_name in "${RR_REQUIRED_PYTHON_MODULES[@]}"; do
    rr_require_python_module "${python_bin}" "${module_name}"
  done

  if ! "${python_bin}" -c 'import sys, importlib.util; module_name = "tomllib" if sys.version_info >= (3, 11) else "tomli"; sys.exit(0 if importlib.util.find_spec(module_name) else 1)'; then
    rr_error "Python TOML parser missing in runtime '${python_bin}'. expected='tomllib|tomli'"
    return 1
  fi

  if [[ -n "${repo_root}" ]]; then
    if ! (cd "${repo_root}" && "${python_bin}" -c 'import reflex_reviewer'); then
      rr_error "Unable to import local package 'reflex_reviewer' from repo root '${repo_root}'."
      return 1
    fi
  else
    if ! "${python_bin}" -c 'import reflex_reviewer'; then
      rr_error "Unable to import installed package 'reflex_reviewer' from runtime '${python_bin}'."
      return 1
    fi
  fi

  return 0
}

rr_resolve_pr_id() {
  local arg_pr_id="${1:-}"
  local normalized_pr_id

  if [[ -n "${arg_pr_id}" ]]; then
    if normalized_pr_id="$(rr_normalize_pr_id "${arg_pr_id}")"; then
      echo "${normalized_pr_id}"
    else
      echo "${arg_pr_id}"
    fi
    return 0
  fi

  local env_candidates=(
    "PR_ID"
    "VCS_PR_ID"
    "BITBUCKET_PR_ID"
    "BITBUCKET_PULL_REQUEST_ID"
    "PULL_REQUEST_ID"
  )

  local key
  for key in "${env_candidates[@]}"; do
    local value="${!key-}"
    if [[ -n "${value:-}" ]]; then
      if normalized_pr_id="$(rr_normalize_pr_id "${value}")"; then
        echo "${normalized_pr_id}"
      else
        echo "${value}"
      fi
      return 0
    fi
  done

  return 1
}

rr_normalize_pr_id() {
  local raw_pr_id="${1:-}"
  raw_pr_id="$(printf '%s' "${raw_pr_id}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

  if [[ -z "${raw_pr_id}" ]]; then
    return 1
  fi

  if [[ "${raw_pr_id}" =~ ^[0-9]+$ ]]; then
    echo "${raw_pr_id}"
    return 0
  fi

  if [[ "${raw_pr_id}" =~ ^[^-]+-([0-9]+)$ ]]; then
    echo "${BASH_REMATCH[1]}"
    return 0
  fi

  return 1
}

rr_is_pr_id_token() {
  local raw_pr_id="${1:-}"
  rr_normalize_pr_id "${raw_pr_id}" >/dev/null
}

rr_validate_pr_id() {
  local pr_id="$1"
  if [[ ! "${pr_id}" =~ ^[0-9]+$ ]]; then
    rr_error "PR id must be numeric. received='${pr_id}'"
    return 1
  fi
}

rr_require_runtime_env() {
  local require_judge_model="${1:-0}"

  rr_require_env "TEAM_NAME"
  rr_require_env "DRAFT_MODEL"
  if [[ "${require_judge_model}" == "1" ]]; then
    rr_require_env "JUDGE_MODEL"
  fi
  rr_require_env "LLM_API_BASE_URL"
  rr_require_env "VCS_BASE_URL"
  rr_require_env "VCS_PROJECT_KEY"
  rr_require_env "VCS_REPO_SLUG"
  rr_require_env "VCS_TOKEN"
  rr_require_llm_api_auth
}

rr_ensure_directory() {
  local dir_path="$1"

  if [[ -z "${dir_path}" ]]; then
    rr_error "Directory path must not be empty."
    return 1
  fi

  if ! mkdir -p "${dir_path}"; then
    rr_error "Failed to create directory: ${dir_path}"
    return 1
  fi

  if [[ ! -w "${dir_path}" ]]; then
    rr_error "Directory is not writable: ${dir_path}"
    return 1
  fi

  return 0
}