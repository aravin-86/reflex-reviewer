## Reflex Reviewer Local Rules

1. **Logging (minimal + safe):**
   - Keep routine logs minimal: only critical entry/exit and key decisions.
   - Do **not** log sensitive data (tokens, secrets, prompt/diff bodies, credentials, raw payloads).
   - For critical failures, log full exception details with stack trace (`logger.exception(...)` or `exc_info=True`).

2. **Centralized configuration:**
   - Move configuration-related and hardcoded runtime values to `reflex_reviewer/config.py`.
   - Prefer environment-variable-backed defaults in config for easy modification.
   - Avoid scattering magic numbers/constants across runtime modules.

3. **Documentation discipline:**
   - Update `README.md` whenever behavior/configuration changes are considerable.
   - Ensure `README.md` only adds variables that fetches values from os env.
   - Always update `README.md` when runtime config is added/modified.

4. **Project-specific workflow override:**
   - Skip reading/updating memory-bank files for this repository.
   - Always create a config in `reflex_reviewer/reflex_reviewer.toml` for values fetched from os env and use.

5. **Coding expectations:**
   - Remove unused imports.
   - Keep code easy to understand, modify, and test.
   - Use appropriate warning/error logs in exception paths.
   - Prefer DRY/simple implementations over duplicated logic.
   - Fix all from `@problems`
