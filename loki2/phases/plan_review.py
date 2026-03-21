from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loki2.phases import PhaseResult

if TYPE_CHECKING:
    from loki2.clients.linear import LinearClient
    from loki2.config import Settings
    from loki2.store.models import Issue

phase_name = "plan_review"


def prepare_prompt(issue: Issue, settings: Settings,
                   linear: LinearClient, prompt_builder) -> str:
    detail = linear.fetch_issue_detail(issue.id)
    sub_data = linear.fetch_sub_issues(issue.id)
    ref_docs = linear.resolve_attachment_documents(detail.get("attachments", []))
    comments = linear.fetch_issue_comments(issue.id)
    context = {
        "ISSUE_ID": issue.id,
        "ISSUE_IDENTIFIER": issue.identifier,
        "ISSUE_DETAIL": detail,
        "PLAN_DOCUMENTS": sub_data.get("documents", []),
        "REFERENCE_DOCUMENTS": ref_docs,
        "REVIEW_COMMENTS": comments,
    }
    return prompt_builder.build("plan_review", context)


def setup_workspace(issue: Issue, settings: Settings, workspace_mgr) -> Path:
    from loki2.clients.git import detect_default_branch
    base = issue.base_branch or detect_default_branch(issue.repo_path)
    return workspace_mgr.create_detached(issue.repo_path, issue.identifier, base)


def post_execute(issue: Issue, claude_result: dict) -> PhaseResult:
    result_text = claude_result.get("result", "")
    if "AUTO_APPROVED" in result_text:
        if "SINGLE" in result_text.upper().replace("AUTO_APPROVED", "").replace("_", " "):
            return PhaseResult(event="auto_approved_single", comment=result_text)
        return PhaseResult(event="auto_approved_multi", comment=result_text)
    if "NEEDS_HUMAN_REVIEW" in result_text:
        return PhaseResult(event="needs_review", comment=result_text)
    return PhaseResult(event="needs_review",
                       comment="Approval marker missing — defaulting to human review.\n\n" + result_text)
