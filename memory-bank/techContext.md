# Tech Context

## Language and runtime
- **Language:** Python (requires Python >= 3.9)
- **Packaging:** `hatchling` build backend (`pyproject.toml`)
- **Distribution:** package name `reflex-reviewer`, with console scripts:
  - `reflex-review`
  - `reflex-distill`
  - `reflex-refine`

## Core dependencies
- `openai`
- `requests`
- `tenacity`
- `python-dotenv`
- `authlib`
- `tomli` (for Python < 3.11 compatibility)

## Configuration model
- Centralized config logic in `reflex_reviewer/config.py`.
- Runtime values resolve from CLI overrides, environment variables, and TOML defaults.
- Repository rule emphasizes use of `reflex_reviewer.toml` for env-driven values.

## External integrations
- **LLM API-compatible endpoint** for model inference and fine-tune related operations.
- **VCS provider integration** (Bitbucket implementation currently present) for PR data/comment APIs.
- **OAuth2** support for token-based auth fallback.

## Reliability and operational constraints
- HTTP paths use retry strategy (`tenacity`) for transient network failures.
- Very large PR diffs may be truncated for safety/performance.
- Distill quality depends on signal quality in PR review discussions.

## Testing and quality surface
- Tests exist under `tests/` for config behavior, VCS client behavior, model client parsing, and runtime utilities.
- pyproject and repo layout suggest package-first usage plus CI/pipeline invocation support.
