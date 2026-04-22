# System Patterns

## High-level architecture
- The project follows a three-flow agentic loop:
  1. **Review flow (`review.py`)**: fetch PR context, build prompts, call model, post summary/inline feedback.
  2. **Distill flow (`distill.py`)**: gather PR comment threads, classify sentiment in batch, extract DPO preference pairs.
  3. **Refine flow (`refine.py`)**: split datasets, trigger fine-tune workflow, monitor job completion.

## Key module responsibilities
- `reflex_reviewer/config.py`
  - Central runtime configuration resolution.
  - Supports CLI/environment/TOML composition and normalization.
- `reflex_reviewer/llm/api_client.py`
  - Handles API communication with retry support and response parsing.
- `reflex_reviewer/auth/oauth2.py`
  - OAuth2 token retrieval/caching for auth fallback when API key is not provided.
- `reflex_reviewer/vcs/vcs_client.py`
  - Protocol/interface definition for VCS clients used by runtime flows.
- `reflex_reviewer/vcs/bitbucket_data_center.py`
  - Bitbucket operations for PR metadata, activities, and comment posting/updating.
- `reflex_reviewer/llm/response_handler.py`
  - Parses model responses into typed/structured payloads used by runtime flows.
- `reflex_reviewer/review_runtime/response_state.py`
  - Stores/retrieves previous response IDs for responses API continuity.

## Design patterns in use
- **Config-first runtime behavior:** centralized in `config.py` + `reflex_reviewer.toml`.
- **Adapter-style VCS abstraction:** VCS-specific logic isolated under `vcs/`.
- **Retry wrappers for network I/O:** tenacity policies around HTTP calls.
- **Pipeline-compatible CLI entrypoints:** each major flow has a callable module + script entry point.

## Operational flow
- Review hooks run on PR create/update.
- Distill hooks run post-merge or on chosen trigger.
- Refine runs on monthly schedule or on-demand trigger once sufficient DPO data exists.
- Standalone launcher orchestration for build automation is copy-friendly and env-driven:
  - `standalone_launcher/reflex_reviewer_launcher.py` is the user-facing entrypoint and command dispatcher (`review`, `distill`, `refine`).
  - `standalone_launcher/reflex_reviewer_bootstrap.py` contains shared validation, PR-id resolution, command builders, and runtime bootstrap helpers.
  - Runner entrypoint: `python3 reflex_reviewer_launcher.py`.
  - Command can be supplied by env via `RR_LAUNCHER_COMMAND` with optional `RR_LAUNCHER_ARGS` passthrough.
  - Launcher always recreates a fresh local venv and reinstalls package dependencies before flow execution.
  - Console script `reflex-pipeline` is removed from packaging metadata.
