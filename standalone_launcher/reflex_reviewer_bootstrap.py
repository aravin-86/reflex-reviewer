import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PR_ID_ENV_CANDIDATES = (
    "PR_ID",
    "VCS_PR_ID",
    "BITBUCKET_PR_ID",
    "BITBUCKET_PULL_REQUEST_ID",
    "PULL_REQUEST_ID",
)

LAUNCHER_COMMAND_ENV = "RR_LAUNCHER_COMMAND"
LAUNCHER_EXTRA_ARGS_ENV = "RR_LAUNCHER_ARGS"

RUNNER_VENV_DIR_ENV = "RR_RUNNER_VENV_DIR"
PACKAGE_INSTALL_TARGET_ENV = "RR_PACKAGE_INSTALL_TARGET"
PACKAGE_INDEX_URL_ENV = "RR_PACKAGE_INDEX_URL"
PACKAGE_EXTRA_INDEX_URL_ENV = "RR_PACKAGE_EXTRA_INDEX_URL"

DEFAULT_RUNNER_VENV_DIR_NAME = ".reflex-reviewer-venv"
DEFAULT_PACKAGE_INSTALL_TARGET = "reflex-reviewer"


class LauncherExecutionError(RuntimeError):
    """Raised when launcher preflight validation or execution fails."""


def launcher_log(message):
    print(f"[reflex-reviewer-launcher] {message}")


def launcher_error(message):
    print(f"[reflex-reviewer-launcher] ERROR: {message}", file=sys.stderr)


def run_command(command, cwd=None):
    subprocess.run(command, cwd=cwd, check=True)


def normalize_pr_id(raw_pr_id):
    normalized = str(raw_pr_id or "").strip()
    if not normalized:
        return None

    if re.fullmatch(r"[0-9]+", normalized):
        return normalized

    ticket_match = re.fullmatch(r"[^-]+-([0-9]+)", normalized)
    if ticket_match:
        return ticket_match.group(1)

    return None


def is_pr_id_token(raw_pr_id):
    return normalize_pr_id(raw_pr_id) is not None


def split_pr_id_and_extra_args(raw_args):
    args = list(raw_args or [])
    if args and is_pr_id_token(args[0]):
        return args[0], args[1:]
    return None, args


def resolve_pr_id(arg_pr_id=None, environ=None):
    env = environ or os.environ

    if arg_pr_id is not None:
        normalized = normalize_pr_id(arg_pr_id)
        return normalized or str(arg_pr_id)

    for env_key in PR_ID_ENV_CANDIDATES:
        env_value = env.get(env_key)
        if env_value is None or not str(env_value).strip():
            continue

        normalized = normalize_pr_id(env_value)
        return normalized or str(env_value)

    return None


def validate_pr_id(pr_id):
    if not re.fullmatch(r"[0-9]+", str(pr_id or "")):
        raise LauncherExecutionError(f"PR id must be numeric. received='{pr_id}'")


def require_env(environ, env_name):
    value = str((environ or {}).get(env_name) or "").strip()
    if not value:
        raise LauncherExecutionError(
            f"Required environment variable is missing: {env_name}"
        )
    return value


def require_llm_api_auth(environ):
    if str((environ or {}).get("LLM_API_KEY") or "").strip():
        return

    require_env(environ, "OAUTH2_TOKEN_URL")
    require_env(environ, "OAUTH2_USER_ID")
    require_env(environ, "OAUTH2_USER_SECRET")


def require_launcher_env(environ, require_judge_model=False):
    env = environ or os.environ
    require_env(env, "TEAM_NAME")
    require_env(env, "DRAFT_MODEL")
    if require_judge_model:
        require_env(env, "JUDGE_MODEL")
    require_env(env, "LLM_API_BASE_URL")
    require_env(env, "VCS_BASE_URL")
    require_env(env, "VCS_PROJECT_KEY")
    require_env(env, "VCS_REPO_SLUG")
    require_env(env, "VCS_TOKEN")
    require_llm_api_auth(env)


def require_training_data_dir(environ):
    return require_env(environ, "DPO_TRAINING_DATA_DIR")


def ensure_directory(dir_path):
    normalized = str(dir_path or "").strip()
    if not normalized:
        raise LauncherExecutionError("Directory path must not be empty.")

    path = Path(normalized)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LauncherExecutionError(f"Failed to create directory: {path}") from exc

    if not os.access(str(path), os.W_OK):
        raise LauncherExecutionError(f"Directory is not writable: {path}")


def resolve_runner_venv_dir(runner_file, environ=None):
    env = environ or os.environ
    runner_dir = Path(runner_file).resolve().parent

    raw_venv_dir = str(env.get(RUNNER_VENV_DIR_ENV) or "").strip()
    if not raw_venv_dir:
        return runner_dir / DEFAULT_RUNNER_VENV_DIR_NAME

    configured_dir = Path(raw_venv_dir)
    if configured_dir.is_absolute():
        return configured_dir
    return runner_dir / configured_dir


def resolve_venv_python(venv_dir):
    venv_path = Path(venv_dir)
    if os.name == "nt":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def build_create_venv_command(python_bin, venv_dir):
    return [str(python_bin), "-m", "venv", str(venv_dir)]


def build_upgrade_pip_command(venv_python):
    return [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"]


def resolve_package_install_target(environ=None):
    env = environ or os.environ
    target = str(env.get(PACKAGE_INSTALL_TARGET_ENV) or DEFAULT_PACKAGE_INSTALL_TARGET).strip()
    if not target:
        raise LauncherExecutionError(
            f"Required environment variable is missing: {PACKAGE_INSTALL_TARGET_ENV}"
        )
    return target


def build_package_install_command(venv_python, environ=None):
    env = environ or os.environ
    target = resolve_package_install_target(env)

    command = [str(venv_python), "-m", "pip", "install"]

    index_url = str(env.get(PACKAGE_INDEX_URL_ENV) or "").strip()
    if index_url:
        command.extend(["--index-url", index_url])

    extra_index_url = str(env.get(PACKAGE_EXTRA_INDEX_URL_ENV) or "").strip()
    if extra_index_url:
        command.extend(["--extra-index-url", extra_index_url])

    command.append(target)
    return command


def bootstrap_runner_environment(python_bin, runner_file, environ=None):
    env = environ or os.environ
    venv_dir = resolve_runner_venv_dir(runner_file, env)

    launcher_log(f"Preparing fresh virtual environment at: {venv_dir}")
    try:
        if venv_dir.exists():
            shutil.rmtree(venv_dir)
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LauncherExecutionError(
            f"Failed to prepare virtual environment directory: {venv_dir}"
        ) from exc

    run_command(build_create_venv_command(python_bin, venv_dir))

    venv_python = resolve_venv_python(venv_dir)
    launcher_log("Installing Reflex Reviewer into fresh virtual environment.")
    run_command(build_upgrade_pip_command(venv_python))
    run_command(build_package_install_command(venv_python, env))
    return str(venv_python)


def build_review_command(python_bin, pr_id, environ, extra_args=None):
    command = [
        python_bin,
        "-m",
        "reflex_reviewer.review",
        "--vcs-type",
        "bitbucket",
        "--team-name",
        environ["TEAM_NAME"],
        "--draft-model",
        environ["DRAFT_MODEL"],
        "--judge-model",
        environ["JUDGE_MODEL"],
        "--pr-id",
        str(pr_id),
    ]
    command.extend(extra_args or [])
    return command


def build_distill_command(python_bin, pr_id, training_data_dir, environ, extra_args=None):
    command = [
        python_bin,
        "-m",
        "reflex_reviewer.distill",
        "--vcs-type",
        "bitbucket",
        "--team-name",
        environ["TEAM_NAME"],
        "--draft-model",
        environ["DRAFT_MODEL"],
        "--pr-id",
        str(pr_id),
        "--dpo-training-data-dir",
        str(training_data_dir),
    ]
    command.extend(extra_args or [])
    return command


def build_refine_command(python_bin, training_data_dir, environ, extra_args=None):
    command = [
        python_bin,
        "-m",
        "reflex_reviewer.refine",
        "--team-name",
        environ["TEAM_NAME"],
        "--draft-model",
        environ["DRAFT_MODEL"],
        "--dpo-training-data-dir",
        str(training_data_dir),
    ]
    command.extend(extra_args or [])
    return command