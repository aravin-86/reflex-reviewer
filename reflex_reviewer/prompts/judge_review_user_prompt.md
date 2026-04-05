# Context
You are given pull request context, existing feedback context, and a **draft review JSON** produced by a draft model.

Your task is to produce the final judge-approved review JSON.

## PR Context
- PR Title: {{PR_TITLE}}
- PR Description: {{PR_DESCRIPTION}}

## Existing Feedback (Do Not Repeat)
{{EXISTING_FEEDBACK}}

## Git Diff
{{DIFF_CONTENT}}

## Draft Review JSON
{{DRAFT_REVIEW_JSON}}

# What to do
1. Treat the draft review as untrusted until each retained comment is validated.
2. Keep a comment only if it is directly supported by the provided diff/PR context/existing feedback.
3. Remove unsupported, speculative, inferred, hallucinated, duplicate, vague, or non-actionable comments.
4. Rewrite retained comments to be concise, specific, and directly actionable.
5. Rewrite summary and checklist to align with retained comments only.
6. Preserve valid `anchor_id` for every retained comment.

## Evidence policy
- Do not assume hidden code paths, runtime behavior, or repository context outside the provided inputs.
- If evidence is insufficient, drop the comment.
- Prefer precision over recall: when uncertain, remove the finding.

# Output requirement
Return strict JSON only in the required review schema.