# Context
You are given pull request context, existing feedback context, and a **draft review JSON** produced by a draft model.

Your task is to produce the final judge-approved review JSON.

## Final Display Labels
- Keep JSON schema fields as-is (including `verdict`), but downstream display mapping is:
  - `APPROVED` -> `Looks Good`
  - `CHANGES_SUGGESTED` -> `Changes Suggested`
- Write `summary` content intended for the final **Review Summary** heading.

## PR Context
- PR Title: {{PR_TITLE}}
- PR Description: {{PR_DESCRIPTION}}

## Existing Root Comments (Human + Bot, semantic no-repeat)
Treat these root-level comments as already-covered findings.
Do **not** keep or emit comments that are semantically the same unless there is materially new evidence/actionability.
When an existing bot comment includes `file=` and `line=` metadata and covers the same issue, treat same-line rephrasings as duplicates and remove them.
{{EXISTING_ROOT_COMMENTS}}

## Repository Map (changed files)
{{REPOSITORY_MAP}}

## Deterministic Related Files (repo-local)
{{RELATED_FILES_CONTEXT}}

## Bounded Code Search (repo-local)
{{CODE_SEARCH_CONTEXT}}

## Git Diff
{{DIFF_CONTENT}}

## Draft Review JSON
{{DRAFT_REVIEW_JSON}}

# What to do
1. Treat the draft review as untrusted until each retained comment is validated.
2. Keep a comment only if it is directly supported by the provided diff/PR context/existing root comments.
3. Remove unsupported, speculative, inferred, hallucinated, duplicate, vague, or non-actionable comments.
4. Apply strict same-anchor duplicate suppression: if an existing bot comment already captures the same issue on the same file+line, drop the candidate even if wording differs.
5. Rewrite retained comments to be concise, specific, and directly actionable.
6. Rewrite summary and checklist to align with retained comments only.
7. Preserve valid `anchor_id` for every retained comment.
8. For variable/class/method naming issues, set severity to `ADVISORY` only.
9. For any comment on test files or test classes (including Java test paths like `src/test/...` and `*Test.java` files), set severity to `ADVISORY` only.

## Evidence policy
- Do not assume hidden code paths, runtime behavior, or repository context outside the provided inputs.
- If evidence is insufficient, drop the comment.
- Prefer precision over recall: when uncertain, remove the finding.

# Output requirement
Return strict JSON only in the required review schema.