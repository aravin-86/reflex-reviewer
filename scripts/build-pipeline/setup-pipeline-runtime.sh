#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common.sh
source "${SCRIPT_DIR}/common.sh"

print_usage() {
  cat <<'USAGE'
Usage: setup-pipeline-runtime.sh [options]

Performs Build Pipeline setup by:
1) creating/updating runtime virtualenv
2) installing runtime using selected mode:
   - package mode (default): pip install RR_PACKAGE_INSTALL_TARGET using configured package indexes
   - clone mode: fresh clone + pip install -r requirements.txt from clone

Options:
  --venv-dir <path>     Virtualenv directory
                        (default: RR_VENV_DIR or mode-specific default)
  --python-bin <path>   Python executable used to create venv (default: python3)
  --recreate            Delete existing venv directory before creating it
  -h, --help            Show this help message

Environment:
  RR_PIPELINE_INSTALL_MODE  Runtime setup mode: package (default) or clone
  RR_PACKAGE_INSTALL_TARGET Pip install target for package mode (default: reflex-reviewer)
  RR_PACKAGE_INDEX_URL      Primary pip index for package mode (default: https://test.pypi.org/simple/)
  RR_PACKAGE_EXTRA_INDEX_URL Optional extra pip index for package mode (default: https://pypi.org/simple/)
  RR_VENV_DIR           Optional default venv path override
  RR_REPOSITORY_CLONE_URL   Required only when RR_PIPELINE_INSTALL_MODE=clone
  RR_REPOSITORY_DIR         Optional clone directory for clone mode (default: <cwd>/.reflex-reviewer-clone)
  RR_REPOSITORY_REF         Optional branch/tag for clone mode

Examples:
  ./scripts/build-pipeline/setup-pipeline-runtime.sh
  RR_PIPELINE_INSTALL_MODE=package RR_PACKAGE_INSTALL_TARGET='reflex-reviewer==0.1.3' ./scripts/build-pipeline/setup-pipeline-runtime.sh
  RR_PACKAGE_INDEX_URL='https://test.pypi.org/simple/' RR_PACKAGE_EXTRA_INDEX_URL='https://pypi.org/simple/' ./scripts/build-pipeline/setup-pipeline-runtime.sh
  RR_PIPELINE_INSTALL_MODE=clone RR_REPOSITORY_CLONE_URL='<REPO_CLONE_URL>' ./scripts/build-pipeline/setup-pipeline-runtime.sh
  ./scripts/build-pipeline/setup-pipeline-runtime.sh --venv-dir /opt/reflex-reviewer/venv
  ./scripts/build-pipeline/setup-pipeline-runtime.sh --python-bin /usr/local/bin/python3
USAGE
}

RECREATE_VENV="false"
VENV_DIR=""
BOOTSTRAP_PYTHON="python3"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  print_usage
  exit 0
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv-dir)
      [[ $# -ge 2 ]] || {
        rr_error "Missing value for --venv-dir"
        print_usage
        exit 1
      }
      VENV_DIR="$2"
      shift 2
      ;;
    --python-bin)
      [[ $# -ge 2 ]] || {
        rr_error "Missing value for --python-bin"
        print_usage
        exit 1
      }
      BOOTSTRAP_PYTHON="$2"
      shift 2
      ;;
    --recreate)
      RECREATE_VENV="true"
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      rr_error "Unknown argument: $1"
      print_usage
      exit 1
      ;;
  esac
done

INSTALL_MODE="$(rr_pipeline_install_mode)"
REPO_ROOT=""

if [[ "${INSTALL_MODE}" == "clone" ]]; then
  REPO_ROOT="$(rr_clone_repository_checkout)"
  rr_require_repo_layout "${REPO_ROOT}"
fi

if [[ -z "${VENV_DIR}" ]]; then
  if [[ "${INSTALL_MODE}" == "clone" ]]; then
    VENV_DIR="$(rr_default_venv_dir "${REPO_ROOT}")"
  else
    VENV_DIR="$(rr_default_venv_dir)"
  fi
fi

if ! command -v "${BOOTSTRAP_PYTHON}" >/dev/null 2>&1; then
  rr_error "Python executable not found: ${BOOTSTRAP_PYTHON}"
  exit 1
fi
BOOTSTRAP_PYTHON="$(command -v "${BOOTSTRAP_PYTHON}")"
rr_require_python_min_version "${BOOTSTRAP_PYTHON}"

if [[ "${RECREATE_VENV}" == "true" && -d "${VENV_DIR}" ]]; then
  rr_log "Removing existing virtualenv: ${VENV_DIR}"
  rm -rf "${VENV_DIR}"
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  rr_log "Creating virtualenv: ${VENV_DIR}"
  "${BOOTSTRAP_PYTHON}" -m venv "${VENV_DIR}"
else
  rr_log "Using existing virtualenv: ${VENV_DIR}"
fi

VENV_PYTHON="${VENV_DIR}/bin/python"
if [[ ! -x "${VENV_PYTHON}" ]]; then
  rr_error "Virtualenv Python executable is missing: ${VENV_PYTHON}"
  exit 1
fi

rr_log "Upgrading pip in virtualenv"
"${VENV_PYTHON}" -m pip install --upgrade pip

if [[ "${INSTALL_MODE}" == "clone" ]]; then
  rr_log "Installing runtime dependencies from requirements.txt"
  "${VENV_PYTHON}" -m pip install -r "${REPO_ROOT}/requirements.txt"

  rr_require_runtime_installation "${VENV_PYTHON}" "${REPO_ROOT}"
else
  PACKAGE_TARGET="$(rr_package_install_target)"
  PACKAGE_INDEX_URL="$(rr_package_index_url)"
  PACKAGE_EXTRA_INDEX_URL="$(rr_package_extra_index_url)"
  rr_log "Installing runtime package target: ${PACKAGE_TARGET}"
  rr_log "Using package index URL: ${PACKAGE_INDEX_URL}"
  if [[ -n "${PACKAGE_EXTRA_INDEX_URL}" ]]; then
    rr_log "Using package extra index URL: ${PACKAGE_EXTRA_INDEX_URL}"
    "${VENV_PYTHON}" -m pip install \
      --index-url "${PACKAGE_INDEX_URL}" \
      --extra-index-url "${PACKAGE_EXTRA_INDEX_URL}" \
      "${PACKAGE_TARGET}"
  else
    "${VENV_PYTHON}" -m pip install \
      --index-url "${PACKAGE_INDEX_URL}" \
      "${PACKAGE_TARGET}"
  fi

  rr_require_runtime_installation "${VENV_PYTHON}"
fi

rr_log "Runtime setup complete"
echo ""
if [[ "${INSTALL_MODE}" == "clone" ]]; then
  echo "Use this repository checkout for pipeline step scripts:"
  echo "  export RR_REPOSITORY_DIR=${REPO_ROOT}"
  echo ""
else
  echo "Package install mode selected (default)."
  echo "Set RR_PIPELINE_INSTALL_MODE=clone only when you need repository-clone runtime behavior."
  echo ""
fi
echo "Use this interpreter for pipeline step scripts:"
echo "  export PYTHON_BIN=${VENV_PYTHON}"
echo "or"
echo "  export RR_VENV_DIR=${VENV_DIR}"
