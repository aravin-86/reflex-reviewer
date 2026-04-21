import os
import shlex
import subprocess
import sys
from pathlib import Path

try:
    from .reflex_reviewer_bootstrap import (
        LAUNCHER_COMMAND_ENV,
        LAUNCHER_EXTRA_ARGS_ENV,
        LauncherExecutionError,
        bootstrap_runner_environment,
        build_distill_command,
        build_refine_command,
        build_review_command,
        ensure_directory,
        launcher_error,
        launcher_log,
        require_launcher_env,
        require_refine_env,
        require_training_data_dir,
        resolve_pr_id,
        run_command,
        split_pr_id_and_extra_args,
        validate_pr_id,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from reflex_reviewer_bootstrap import (  # type: ignore
        LAUNCHER_COMMAND_ENV,
        LAUNCHER_EXTRA_ARGS_ENV,
        LauncherExecutionError,
        bootstrap_runner_environment,
        build_distill_command,
        build_refine_command,
        build_review_command,
        ensure_directory,
        launcher_error,
        launcher_log,
        require_launcher_env,
        require_refine_env,
        require_training_data_dir,
        resolve_pr_id,
        run_command,
        split_pr_id_and_extra_args,
        validate_pr_id,
    )

SUPPORTED_LAUNCHER_COMMANDS = ("review", "distill", "refine")
LAUNCHER_FILE_NAME = Path(__file__).name


def _is_help_request(raw_args):
    return len(raw_args) == 1 and raw_args[0] in {"-h", "--help"}


def _resolve_bootstrap_python(environ):
    return str((environ or {}).get("PYTHON_BIN") or "").strip() or sys.executable


def _resolve_runtime_python(environ):
    bootstrap_python = _resolve_bootstrap_python(environ)
    return bootstrap_runner_environment(bootstrap_python, __file__, environ)


def _resolve_env_extra_args(environ):
    raw_extra_args = str((environ or {}).get(LAUNCHER_EXTRA_ARGS_ENV) or "").strip()
    if not raw_extra_args:
        return []
    return shlex.split(raw_extra_args)


def _normalize_launcher_command(command):
    normalized = str(command or "").strip().lower()
    if normalized not in SUPPORTED_LAUNCHER_COMMANDS:
        expected_values = ", ".join(SUPPORTED_LAUNCHER_COMMANDS)
        raise LauncherExecutionError(
            f"Unsupported launcher command: '{command}'. Expected one of: {expected_values}"
        )
    return normalized


def _resolve_launcher_command(raw_args, environ):
    args = list(raw_args or [])
    if args:
        return _normalize_launcher_command(args[0]), args[1:]

    env_command = str((environ or {}).get(LAUNCHER_COMMAND_ENV) or "").strip()
    if env_command:
        return _normalize_launcher_command(env_command), []

    raise LauncherExecutionError(
        "Unable to resolve launcher command. "
        f"Provide it as first CLI argument or set {LAUNCHER_COMMAND_ENV}."
    )


def _print_main_usage():
    print(f"Usage: python3 {LAUNCHER_FILE_NAME} [review|distill|refine] [args]")
    print("")
    print("Environment-only mode:")
    print(f"  {LAUNCHER_COMMAND_ENV}=review|distill|refine")
    print(
        f"  {LAUNCHER_EXTRA_ARGS_ENV}='<optional pr_id and extra module args>'"
    )


def _print_review_usage():
    print(
        f"Usage: python3 {LAUNCHER_FILE_NAME} review "
        "[pr_id] [extra reflex-review args]"
    )


def _print_distill_usage():
    print(
        f"Usage: python3 {LAUNCHER_FILE_NAME} distill "
        "[pr_id] [extra reflex-distill args]"
    )


def _print_refine_usage():
    print(f"Usage: python3 {LAUNCHER_FILE_NAME} refine [extra reflex-refine args]")


def review_entrypoint(argv=None, environ=None):
    raw_args = list(argv or [])
    env = environ or os.environ
    if _is_help_request(raw_args):
        _print_review_usage()
        return 0

    return _run_with_error_handling(_run_review, raw_args, env)


def _run_review(raw_args, environ):
    require_launcher_env(environ, require_judge_model=True)

    pr_id_input, extra_args = split_pr_id_and_extra_args(raw_args)
    pr_id = resolve_pr_id(pr_id_input, environ)
    if pr_id is None:
        raise LauncherExecutionError("Unable to resolve PR id from args/environment.")
    validate_pr_id(pr_id)

    python_bin = _resolve_runtime_python(environ)
    launcher_log(f"Invoking review flow. pr_id={pr_id}")
    command = build_review_command(python_bin, pr_id, environ, extra_args)
    run_command(command)
    return 0


def distill_entrypoint(argv=None, environ=None):
    raw_args = list(argv or [])
    env = environ or os.environ
    if _is_help_request(raw_args):
        _print_distill_usage()
        return 0

    return _run_with_error_handling(_run_distill, raw_args, env)


def _run_distill(raw_args, environ):
    require_launcher_env(environ)
    training_data_dir = require_training_data_dir(environ)
    ensure_directory(training_data_dir)

    pr_id_input, extra_args = split_pr_id_and_extra_args(raw_args)
    pr_id = resolve_pr_id(pr_id_input, environ)
    if pr_id is None:
        raise LauncherExecutionError("Unable to resolve PR id from args/environment.")
    validate_pr_id(pr_id)

    python_bin = _resolve_runtime_python(environ)
    launcher_log(f"Invoking distill flow. pr_id={pr_id}")
    command = build_distill_command(
        python_bin,
        pr_id,
        training_data_dir,
        environ,
        extra_args,
    )
    run_command(command)
    return 0


def refine_entrypoint(argv=None, environ=None):
    raw_args = list(argv or [])
    env = environ or os.environ
    if _is_help_request(raw_args):
        _print_refine_usage()
        return 0

    return _run_with_error_handling(_run_refine, raw_args, env)


def _run_refine(raw_args, environ):
    require_refine_env(environ)
    training_data_dir = require_training_data_dir(environ)
    ensure_directory(training_data_dir)

    python_bin = _resolve_runtime_python(environ)
    launcher_log("Invoking refine flow.")
    command = build_refine_command(
        python_bin,
        training_data_dir,
        environ,
        raw_args,
    )
    run_command(command)
    return 0


def _run_with_error_handling(handler, *args, **kwargs):
    try:
        return handler(*args, **kwargs)
    except LauncherExecutionError as exc:
        launcher_error(str(exc))
        return 1
    except subprocess.CalledProcessError as exc:
        command_text = " ".join(str(part) for part in (exc.cmd or []))
        launcher_error(
            f"Command failed with exit code {exc.returncode}: {command_text}"
        )
        return int(exc.returncode or 1)
    except OSError as exc:
        launcher_error(str(exc))
        return 1


def main(argv=None, environ=None):
    raw_args = list(argv or [])
    env = environ or os.environ

    if _is_help_request(raw_args):
        _print_main_usage()
        return 0

    try:
        command, cli_args = _resolve_launcher_command(raw_args, env)
    except LauncherExecutionError as exc:
        launcher_error(str(exc))
        _print_main_usage()
        return 1

    merged_args = cli_args + _resolve_env_extra_args(env)
    if command == "review":
        return review_entrypoint(merged_args, environ=env)
    if command == "distill":
        return distill_entrypoint(merged_args, environ=env)
    if command == "refine":
        return refine_entrypoint(merged_args, environ=env)

    launcher_error(f"Unknown command: {command}")
    _print_main_usage()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))