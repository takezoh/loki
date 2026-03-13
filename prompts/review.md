You are addressing review feedback on a pull request.

## Issue
- Identifier: {{ISSUE_IDENTIFIER}}

## Issue Detail
{json}
{{ISSUE_DETAIL}}
{/json}

## Plan Documents
{json}
{{PLAN_DOCUMENTS}}
{/json}

## PR Diff (current state)
{{PR_DIFF}}

## Review Comments
{{REVIEW_COMMENTS}}

## Instructions

1. Read and understand ALL review comments
2. Make targeted fixes to address each comment
3. Do NOT refactor or change code beyond what reviewers requested
4. Run relevant tests after changes
5. Commit: `{{ISSUE_IDENTIFIER}}: address review feedback`
6. Push: `git push origin {{ISSUE_IDENTIFIER}}`

## Notes
- You are on the parent branch ({{ISSUE_IDENTIFIER}}) — the PR already exists
- Address ALL review comments, not just some
