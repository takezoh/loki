You are revising an existing implementation plan based on reviewer feedback.

## Target Issue
- Issue ID: {{ISSUE_ID}}
- Identifier: {{ISSUE_IDENTIFIER}}

## Issue Detail
```json
{{ISSUE_DETAIL}}
```

## Current Plan Document
```json
{{PLAN_DOCUMENTS}}
```

## Current Sub-issues
```json
{{SUB_ISSUES}}
```

## Review Feedback (comments on the issue)
```json
{{REVIEW_COMMENTS}}
```

## Steps

### 1. Understand Feedback
Read all review comments carefully. Identify what changes are requested:
- Sub-issue additions, removals, or modifications
- Changes to implementation approach
- Missing considerations or requirements

### 2. Investigate Code (if needed)
If the feedback requires re-investigating the codebase, launch an Agent tool (subagent_type: Plan, model: opus) to investigate specific areas.

### 3. Update Plan Document
Update the existing plan document using `update_document` to reflect the revised plan.
- Document ID is provided in the Current Plan Document section
- Preserve parts that don't need changes

### 4. Modify Sub-issues
Make targeted changes to sub-issues:
- **Modify**: Use `save_issue` to update title/description of existing sub-issues
- **Add**: Use `save_issue` with parentId={{ISSUE_ID}} and stateId={{TODO_STATE_ID}} for new sub-issues
- **Remove**: Use `save_issue` to move unnecessary sub-issues to Cancelled state
- Update `blockedBy` / `blocks` relations if dependencies change
- Apply the same labels as the parent issue

### 5. Dependency Cycle Check
After modifying sub-issues:
```bash
python {{FORGE_ROOT}}/scripts/check_cycle.py {{ISSUE_ID}}
```

### 6. Completion
Output a summary as your final response text:
- What was changed and why (based on the feedback)
- Updated sub-issue list with dependencies

## Notes
- Do NOT recreate sub-issues that don't need changes
- Do NOT modify any code files
- Focus only on addressing the specific feedback
- Status update to "Pending Approval" is handled automatically after completion
