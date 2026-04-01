#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common.sh
source "${SCRIPT_DIR}/common.sh"

print_usage() {
  cat <<'USAGE'
Usage: setup-pipeline-runtime.sh [options]

Creates/updates the cloned repository local virtualenv used by
Build Pipeline step scripts and installs required Python packages
from requirements.txt.

Options:
  --venv-dir <path>     Virtualenv directory
                        (default: RR_VENV_DIR or <repo>/.venv)
  --python-bin <path>   Python executable used to create venv (default: python3)
  --recreate            Delete existing venv directory before creating it
  -h, --help            Show this help message

Environment:
  RR_VENV_DIR           Optional default venv path override
  RR_REPOSITORY_CLONE_URL   Required remote repository URL for cloning checkout
  RR_REPOSITORY_DIR         Optional clone directory (default: <cwd>/.reflex-reviewer-clone)
  RR_REPOSITORY_REF         Optional branch/tag for clone

Examples:
  ./scripts/build-pipeline/setup-pipeline-runtime.sh
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

rr_bootstrap_cloned_pipeline_script "$(basename "${BASH_SOURCE[0]}")" "$@"

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

REPO_ROOT="$(rr_repo_root_from_script_dir "${SCRIPT_DIR}")"
rr_require_repo_layout "${REPO_ROOT}"

if [[ -z "${VENV_DIR}" ]]; then
  VENV_DIR="$(rr_default_venv_dir "${REPO_ROOT}")"
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

rr_log "Installing runtime dependencies from requirements.txt"
"${VENV_PYTHON}" -m pip install -r "${REPO_ROOT}/requirements.txt"

rr_require_runtime_installation "${VENV_PYTHON}" "${REPO_ROOT}"

rr_log "Runtime setup complete"
echo ""
echo "Use this interpreter for pipeline step scripts:"
echo "  export PYTHON_BIN=${VENV_PYTHON}"
echo "or"
echo "  export RR_VENV_DIR=${VENV_DIR}"
