# Changelog

All notable changes to this project are documented in this file.

## [1.1.0]

### Highlights
- Promoted publish-stage policy enforcement from deterministic `policy_guard` to the LLM-backed `policy_guard_agent` node in the review graph.
- Fixed summary recommendation behavior to honor unresolved existing bot comments, so not-yet-addressed prior findings can keep the final recommendation as `Changes Suggested`.
- Tightened draft and judge ReAct behavior so both now default to fetching repository context with at least one tool call before finalizing (when repository context is deferred and tool retrieval is enabled), and fixed prompt output-format conflicts via mode-aware contracts.

### Runtime and platform evolution
- Made ReAct execution repository-aware: ReAct now runs only when `REPOSITORY_PATH` resolves to a valid local repository, with safe fallback to the standard non-ReAct draft/judge path otherwise.
- Added stricter lazy-context ReAct control via `require_initial_repository_tool`, requiring draft and judge ReAct to collect repository evidence before finalizing when repository context is deferred (judge enforcement applies when judge tool retrieval is enabled).
- Introduced shared mode-aware output contracts for draft/judge prompts (`review_output_contracts.py`) so ReAct and non-ReAct paths enforce the correct JSON response shape.
- Bumped package version to `1.1.0`.

### Reliability and data quality improvements
- Strengthened same-anchor near-duplicate handling against existing bot inline comments and within-run generated comments.
- Added reply-sentiment-aware evaluation of prior bot comment threads (`REJECTED` / `NOT_REJECTED` / `UNSURE`) to improve outstanding-finding decisions.
- Improved outstanding-checklist and summary rewriting flow so unresolved prior comments are reflected coherently in the final review summary output.

## [1.0.0]

### Highlights
- Introduced a graph-orchestrated review runtime with deterministic stages plus two-agent draft/judge inference for stronger review quality.
- Added ReAct-style bounded tool loops for draft and judge agents, including lazy repository-context retrieval to balance context quality and token cost.
- Expanded repository-aware review context with Java + Python support (parser-backed Java extraction, related-file retrieval, bounded code search, and configurable ignore directories).
- Strengthened review guardrails with strict severity handling (`CRITICAL`, `MAJOR`, `ADVISORY`), advisory-only enforcement for naming and test-file comments, and deterministic same-anchor duplicate suppression.

### Runtime and platform evolution
- Standardized model roles and config across flows with explicit `DRAFT_MODEL` and `JUDGE_MODEL` behavior.
- Reorganized internals into focused packages (`llm`, `auth`, `review_runtime`, `repository_context`, `review_graph_runtime`) for maintainability.
- Standardized deployment execution around a standalone launcher (`RR_LAUNCHER_COMMAND=review|distill|refine`) with venv bootstrap/reuse controls.
- Clarified VCS positioning and naming around Bitbucket Data Center support.

### Reliability and data quality improvements
- Added safer, minimal observability (node-level traces, bounded context metrics, and payload-safe HTTP logging).
- Improved LLM retry behavior with slower backoff and `429 Retry-After` awareness.
- Enhanced distill behavior with deterministic Bitbucket reaction-aware sentiment overrides before LLM fallback.
- Refined review/distill summary handling and recommendation labeling for cleaner downstream signal extraction.

## [0.2.0]

### Core capabilities
- Established the foundational **review → distill → refine** product loop for automated pull-request quality improvement.
- Delivered early automated PR review behavior with structured review output and iterative refinement direction.
- Included initial VCS-oriented workflow support (Bitbucket Data Center-focused) for practical PR review automation.
- Added early pipeline/bootstrap groundwork so the flows could be executed repeatedly in remote/runtime environments.

### Product maturity at this stage
- Included initial packaging/distribution and runtime setup improvements that prepared the project for broader adoption in later releases.