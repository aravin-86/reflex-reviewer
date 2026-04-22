import argparse
import os
import random
import logging
import time
import requests  # type: ignore[reportMissingImports,reportMissingModuleSource]
from tenacity import (  # type: ignore[reportMissingImports]
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
from .llm.api_client import (
    upload_file,
    create_fine_tune_job,
    retrieve_fine_tune_job_status,
)
from .config import (
    clear_runtime_overrides,
    get_common_config,
    get_refine_config,
    resolve_dpo_training_data_file_path,
    resolve_refine_split_file_paths,
    sanitize_team_name_for_identifier,
    set_runtime_overrides,
)

refine_config = get_refine_config()
TIMEOUT_SECONDS = refine_config["timeout_seconds"]
INITIAL_POLL_INTERVAL_SECONDS = refine_config["initial_poll_interval_seconds"]
MAX_POLL_INTERVAL_SECONDS = refine_config["max_poll_interval_seconds"]
TRAIN_SPLIT_RATIO = refine_config["train_split_ratio"]
MIN_SAMPLES_TO_TRAIN = refine_config["min_samples_to_train"]

logger = logging.getLogger(__name__)


def _parse_bool(value):
    if isinstance(value, bool):
        return value

    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise argparse.ArgumentTypeError(
        "Expected a boolean value: true/false, yes/no, on/off, 1/0"
    )


def _resolve_runtime_settings(config_overrides=None):
    common_config = get_common_config(config_overrides)
    dpo_training_data_dir = str(
        common_config.get("dpo_training_data_dir") or ""
    ).strip()
    team_name = str(common_config.get("team_name") or "")
    draft_model = str(common_config.get("draft_model") or "")
    stream_response = bool(common_config.get("stream_response"))

    if not team_name:
        raise ValueError(
            "TEAM_NAME is required. Pass --team-name (identifier for your team to the LLM model)."
        )

    if not dpo_training_data_dir:
        raise ValueError(
            "DPO training data directory is required. Pass --dpo-training-data-dir."
        )

    if not isinstance(MIN_SAMPLES_TO_TRAIN, int) or MIN_SAMPLES_TO_TRAIN < 0:
        raise ValueError(
            "refine.min_samples_to_train is required in reflex_reviewer.toml."
        )

    if not draft_model:
        raise ValueError("DRAFT_MODEL is required. Pass --draft-model.")

    dpo_training_data_file = resolve_dpo_training_data_file_path(
        team_name=team_name,
        dpo_training_data_dir=dpo_training_data_dir,
    )

    return {
        "dpo_training_data_file": dpo_training_data_file,
        "dpo_training_data_dir": dpo_training_data_dir,
        "team_name": team_name,
        "draft_model": draft_model,
        "stream_response": stream_response,
    }


def _build_runtime_overrides(
    team_name,
    draft_model,
    stream_response,
    dpo_training_data_dir,
    vcs_base_url=None,
    vcs_project_key=None,
    vcs_repo_slug=None,
    vcs_token=None,
    llm_api_base_url=None,
    llm_api_proxy_url=None,
    llm_api_key=None,
    llm_api_reasoning_effort=None,
):
    return {
        "team_name": team_name,
        "draft_model": draft_model,
        "stream_response": stream_response,
        "dpo_training_data_dir": dpo_training_data_dir,
        "vcs_base_url": vcs_base_url,
        "vcs_project_key": vcs_project_key,
        "vcs_repo_slug": vcs_repo_slug,
        "vcs_token": vcs_token,
        "llm_api_base_url": llm_api_base_url,
        "llm_api_proxy_url": llm_api_proxy_url,
        "llm_api_key": llm_api_key,
        "llm_api_reasoning_effort": llm_api_reasoning_effort,
    }


@retry(
    wait=wait_exponential(multiplier=2, min=10, max=120),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((requests.exceptions.RequestException,)),
    reraise=True,
)
def run_training_cycle(train_path, val_path, draft_model, team_name):
    sanitized_suffix = sanitize_team_name_for_identifier(team_name)
    training_file_id = upload_file(train_path, purpose="fine-tune")
    validation_file_id = upload_file(val_path, purpose="fine-tune")

    job_id = create_fine_tune_job(
        training_file_id=training_file_id,
        validation_file_id=validation_file_id,
        model=draft_model,
        method="dpo",
        suffix=sanitized_suffix,
    )
    return job_id


@retry(
    wait=wait_exponential(multiplier=2, min=10, max=120),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((requests.exceptions.RequestException,)),
    reraise=True,
)
def get_fine_tune_job_status(job_id):
    return retrieve_fine_tune_job_status(job_id)


def wait_for_fine_tune_completion(job_id):
    terminal_statuses = {"succeeded", "failed", "cancelled"}
    timeout_seconds = TIMEOUT_SECONDS
    poll_interval_seconds = INITIAL_POLL_INTERVAL_SECONDS
    max_poll_interval_seconds = MAX_POLL_INTERVAL_SECONDS
    elapsed_seconds = 0

    while elapsed_seconds <= timeout_seconds:
        job_data = get_fine_tune_job_status(job_id)
        status = (job_data.get("status") or "unknown").lower()
        logger.info("Fine-tune job status update. job_id=%s status=%s", job_id, status)

        if status in terminal_statuses:
            return job_data

        time.sleep(poll_interval_seconds)
        elapsed_seconds += poll_interval_seconds
        poll_interval_seconds = min(
            poll_interval_seconds * 2, max_poll_interval_seconds
        )

    logger.warning(
        "Fine-tune job did not reach terminal state before timeout. job_id=%s timeout_seconds=%s",
        job_id,
        timeout_seconds,
    )
    return None


def run(
    dpo_training_data_dir=None,
    team_name=None,
    draft_model=None,
    stream_response=None,
    vcs_base_url=None,
    vcs_project_key=None,
    vcs_repo_slug=None,
    vcs_token=None,
    llm_api_base_url=None,
    llm_api_proxy_url=None,
    llm_api_key=None,
    llm_api_reasoning_effort=None,
):
    runtime_overrides = _build_runtime_overrides(
        team_name=team_name,
        draft_model=draft_model,
        stream_response=stream_response,
        dpo_training_data_dir=dpo_training_data_dir,
        vcs_base_url=vcs_base_url,
        vcs_project_key=vcs_project_key,
        vcs_repo_slug=vcs_repo_slug,
        vcs_token=vcs_token,
        llm_api_base_url=llm_api_base_url,
        llm_api_proxy_url=llm_api_proxy_url,
        llm_api_key=llm_api_key,
        llm_api_reasoning_effort=llm_api_reasoning_effort,
    )
    set_runtime_overrides(runtime_overrides)

    logger.info("Refine run started.")
    try:
        runtime_settings = _resolve_runtime_settings(runtime_overrides)
        resolved_data_file = runtime_settings["dpo_training_data_file"]
        split_file_paths = resolve_refine_split_file_paths(
            runtime_settings["dpo_training_data_dir"]
        )
        train_file_path = split_file_paths["train"]
        val_file_path = split_file_paths["val"]
        run_team_name = runtime_settings["team_name"]
        run_draft_model = runtime_settings["draft_model"]
        run_stream_response = runtime_settings["stream_response"]
        logger.info("Refine runtime settings resolved. stream=%s", run_stream_response)

        if (
            not os.path.exists(resolved_data_file)
            or os.stat(resolved_data_file).st_size == 0
        ):
            logger.info("No training data available.")
            return

        with open(resolved_data_file, "r") as f:
            lines = f.readlines()

        logger.info("Loaded DPO dataset. samples=%s", len(lines))

        if len(lines) < MIN_SAMPLES_TO_TRAIN:
            logger.info(
                "Dataset below minimum threshold. samples=%s min_required=%s",
                len(lines),
                MIN_SAMPLES_TO_TRAIN,
            )
            return

        # Train/validation split ratio comes from the TOML configuration.
        random.shuffle(lines)
        split = int(len(lines) * TRAIN_SPLIT_RATIO)
        train_lines, val_lines = lines[:split], lines[split:]

        with open(train_file_path, "w") as f:
            f.writelines(train_lines)
        with open(val_file_path, "w") as f:
            f.writelines(val_lines)

        job_id = run_training_cycle(
            train_file_path,
            val_file_path,
            run_draft_model,
            run_team_name,
        )
        logger.info("DPO fine-tune job started. job_id=%s", job_id)

        final_job_data = wait_for_fine_tune_completion(job_id)
        final_status = (final_job_data or {}).get("status", "unknown").lower()

        if final_status == "succeeded":
            open(resolved_data_file, "w").close()
            logger.info("Training cache cleared after successful fine-tuning.")
        elif final_job_data:
            logger.error(
                "Fine-tune job %s ended with status: %s",
                job_id,
                final_status,
            )
        else:
            logger.warning(
                "Fine-tune job status check timed out. training_cache_retained=true job_id=%s",
                job_id,
            )
    except Exception:
        logger.exception("Refine run failed")
    finally:
        clear_runtime_overrides()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run DPO fine-tuning refinement from a JSONL dataset"
    )
    parser.add_argument(
        "--dpo-training-data-dir",
        required=True,
        help=(
            "Parent directory for DPO training data. Reflex Reviewer reads/writes "
            "the team-specific dataset file as "
            "<dir>/{sanitized_team_name}_dpo_training_data.jsonl."
        ),
    )
    parser.add_argument(
        "--team-name",
        required=True,
        help="Identifier for your team to the LLM model",
    )
    parser.add_argument(
        "--draft-model",
        required=False,
        help=(
            "Draft model used across distill/refine flows "
            "(overrides model.draft_model in reflex_reviewer.toml)"
        ),
    )
    parser.add_argument(
        "--stream-response",
        type=_parse_bool,
        default=None,
        help="Enable streaming responses (overrides model.stream_response from reflex_reviewer.toml)",
    )
    parser.add_argument("--vcs-base-url", help="Override VCS_BASE_URL")
    parser.add_argument("--vcs-project-key", help="Override VCS_PROJECT_KEY")
    parser.add_argument("--vcs-repo-slug", help="Override VCS_REPO_SLUG")
    parser.add_argument("--vcs-token", help="Override VCS_TOKEN")
    parser.add_argument("--llm-api-base-url", help="Override LLM_API_BASE_URL")
    parser.add_argument("--llm-api-proxy-url", help="Override LLM_API_PROXY_URL")
    parser.add_argument("--llm-api-key", help="Override LLM_API_KEY")
    parser.add_argument(
        "--llm-api-reasoning-effort",
        help="LLM API reasoning effort: low|medium|high (defaults to env or high)",
    )
    args = parser.parse_args()
    run(
        dpo_training_data_dir=args.dpo_training_data_dir,
        team_name=args.team_name,
        draft_model=args.draft_model,
        stream_response=args.stream_response,
        vcs_base_url=args.vcs_base_url,
        vcs_project_key=args.vcs_project_key,
        vcs_repo_slug=args.vcs_repo_slug,
        vcs_token=args.vcs_token,
        llm_api_base_url=args.llm_api_base_url,
        llm_api_proxy_url=args.llm_api_proxy_url,
        llm_api_key=args.llm_api_key,
        llm_api_reasoning_effort=args.llm_api_reasoning_effort,
    )
