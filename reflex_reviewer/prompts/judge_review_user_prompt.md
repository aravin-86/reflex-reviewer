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
1. Filter out low-quality/duplicate/non-actionable comments.
2. Rewrite retained comments to be concise and directly actionable.
3. Rewrite summary and checklist to align with retained comments only.
4. Preserve valid `anchor_id` for every retained comment.

# Output requirement
Return strict JSON only in the required review schema.