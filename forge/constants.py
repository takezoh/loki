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

# Agent Session states (参照用、手動管理不要)
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
