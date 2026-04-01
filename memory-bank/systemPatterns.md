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
- `reflex_reviewer/litellm_client.py`
  - Handles API communication with retry support and response parsing.
- `reflex_reviewer/oauth2.py`
  - OAuth2 token retrieval/caching for auth fallback when API key is not provided.
- `reflex_reviewer/vcs/bitbucket_vcs.py`
  - Bitbucket operations for PR metadata, activities, and comment posting/updating.
- `reflex_reviewer/response_handler.py`
  - Parses model responses into typed/structured payloads used by runtime flows.
- `reflex_reviewer/review_response_state.py`
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
- Build Pipeline step scripts use clone-first bootstrap:
  - clone remote repository from `RR_REPOSITORY_CLONE_URL`,
  - remove existing clone dir and re-clone for deterministic state,
  - re-exec same step script from cloned `scripts/build-pipeline/` path,
  - then run existing runtime validation + Python flow invocation.
