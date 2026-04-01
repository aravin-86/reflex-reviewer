# Product Context

## Why this project exists
- Engineering teams need scalable PR review support without sacrificing consistency.
- Manual review quality varies and feedback loops are often not captured for systematic improvement.

## Problems addressed
- Repetitive low-level review burden on human reviewers.
- Limited reuse of reviewer feedback signals to improve future automated comments.
- Operational friction in integrating model-based review into CI/pipeline workflows.

## Expected user experience
- Teams run review/distill/refine via simple CLI commands or pipeline hooks.
- PRs receive structured summaries + inline comments from AI reviewer.
- Reviewer feedback (accept/reject signals) becomes training data for gradual behavior improvement.

## Primary users
- Platform/DevEx teams integrating automated review in CI.
- Repository maintainers who want iterative reviewer quality improvements.

## Value proposition
- Faster feedback cycles for pull requests.
- Learning loop grounded in real team feedback.
- Configuration-first runtime behavior through environment and TOML-backed settings.
