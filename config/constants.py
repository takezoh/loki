STATE_PLANNING = "Planning"
STATE_IMPLEMENTING = "Implementing"
STATE_IN_PROGRESS = "In Progress"
STATE_IN_REVIEW = "In Review"
STATE_PENDING_APPROVAL = "Pending Approval"
STATE_PLAN_CHANGES_REQUESTED = "Plan Changes Requested"
STATE_CHANGES_REQUESTED = "Changes Requested"
STATE_DONE = "Done"
STATE_FAILED = "Failed"
STATE_CANCELLED = "Cancelled"
STATE_TODO = "Todo"

TERMINAL_STATES = {STATE_IN_PROGRESS, STATE_IN_REVIEW, STATE_DONE, STATE_CANCELLED, STATE_FAILED}

PHASE_PLANNING = "planning"
PHASE_IMPLEMENTING = "implementing"
PHASE_REVIEW = "review"
PHASE_PLAN_REVIEW = "plan_review"

PHASE_DENIED_TOOLS = {
    PHASE_PLANNING: [
        "mcp__linear-server__get_issue",
        "mcp__linear-server__list_issue_statuses",
        "mcp__linear-server__save_comment",
    ],
    PHASE_IMPLEMENTING: [
        "mcp__linear-server__get_issue",
        "mcp__linear-server__list_documents",
        "mcp__linear-server__list_comments",
        "mcp__linear-server__save_issue",
    ],
    PHASE_PLAN_REVIEW: [
        "mcp__linear-server__get_issue",
        "mcp__linear-server__list_issue_statuses",
        "mcp__linear-server__save_comment",
    ],
    PHASE_REVIEW: [
        "mcp__linear-server__save_issue",
        "mcp__linear-server__get_issue",
        "mcp__linear-server__list_documents",
    ],
}

# Agent Session states
SESSION_PENDING = "pending"
SESSION_ACTIVE = "active"
SESSION_ERROR = "error"
SESSION_AWAITING_INPUT = "awaitingInput"
SESSION_COMPLETE = "complete"

# Agent Activity types
ACTIVITY_THOUGHT = "thought"
ACTIVITY_ACTION = "action"
ACTIVITY_RESPONSE = "response"
ACTIVITY_ERROR = "error"
ACTIVITY_ELICITATION = "elicitation"
