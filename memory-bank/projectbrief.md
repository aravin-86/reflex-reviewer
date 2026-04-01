# Project Brief

## Project
- **Name:** reflex-reviewer
- **Version:** 0.1.1
- **Purpose:** Provide an automated AI-assisted pull request review loop with continuous improvement.

## Core Objective
- Run a practical **review → distill → refine** cycle:
  - **Review (actuator):** analyze PRs and post review output.
  - **Distill (observer):** collect human feedback signals from PR discussions.
  - **Refine (optimizer):** run DPO-oriented fine-tuning workflows from distilled preference data.

## Scope
- Python package and CLI tooling for repository review automation.
- Pluggable VCS layer (currently Bitbucket implementation is present).
- LiteLLM-backed model access, including responses/chat completion paths.
- Runtime configuration via CLI + environment/TOML-backed config resolution.

## Non-goals (current snapshot)
- Multi-VCS implementations beyond current adapters in repo.
- UI/dashboard product; this repo focuses on runtime pipeline/automation flows.
- General-purpose agent orchestration outside PR review/refinement lifecycle.

## Primary Success Criteria
- Reliable PR review execution with structured outputs.
- High-signal preference data generation from real reviewer interactions.
- Repeatable refinement cycle that can improve future review quality.
