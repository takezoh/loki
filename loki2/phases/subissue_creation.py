from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loki2.phases import PhaseResult

if TYPE_CHECKING:
    from loki2.clients.linear import LinearClient
    from loki2.config import Settings
    from loki2.store.models import Issue

phase_name = "subissue_creation"


def prepare_prompt(issue: Issue, settings: Settings,
                   linear: LinearClient, prompt_builder) -> str:
    detail = linear.fetch_issue_detail(issue.id)
    sub_data = linear.fetch_sub_issues(issue.id)
    ref_docs = linear.resolve_attachment_documents(detail.get("attachments", []))
    context = {
        "ISSUE_ID": issue.id,
        "ISSUE_IDENTIFIER": issue.identifier,
        "ISSUE_DETAIL": detail,
        "PLAN_DOCUMENTS": sub_data.get("documents", []),
        "REFERENCE_DOCUMENTS": ref_docs,
        "FORGE_ROOT": str(Path(__file__).resolve().parent.parent.parent),
    }
    return prompt_builder.build("subissue_creation", context)


def setup_workspace(issue: Issue, settings: Settings, workspace_mgr) -> Path:
    from loki2.clients.git import detect_default_branch
    base = issue.base_branch or detect_default_branch(issue.repo_path)
    return workspace_mgr.create_detached(issue.repo_path, issue.identifier, base)


def post_execute(issue: Issue, claude_result: dict, linear: LinearClient) -> PhaseResult:
    sub_data = linear.fetch_sub_issues(issue.id)
    if not sub_data.get("sub_issues"):
        raise RuntimeError("Sub-issue creation completed but no sub-issues were created.")

    detail = linear.fetch_issue_detail(issue.id)
    repo_label_ids = [n["id"] for n in detail.get("label_nodes", [])
                      if n.get("name", "").startswith("repo:")]

    from loki2.core.state import STATE_TODO, END_STATES
    for sub in sub_data["sub_issues"]:
        if sub["state"] != STATE_TODO and sub["state"] not in END_STATES:
            linear.update_issue_state(sub["id"], STATE_TODO)
        if repo_label_ids:
            linear.graphql(
                """mutation($issueId: String!, $labelIds: [String!]!) {
                  issueUpdate(id: $issueId, input: { labelIds: $labelIds }) { issue { id } }
                }""",
                {"issueId": sub["id"], "labelIds": repo_label_ids},
            )

    result_text = claude_result.get("result", "")
    return PhaseResult(event="subissues_created", comment=result_text)
