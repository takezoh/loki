from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loki2.phases import PhaseResult

if TYPE_CHECKING:
    from loki2.clients.linear import LinearClient
    from loki2.config import Settings
    from loki2.store.models import Issue

phase_name = "pr"


def prepare_prompt(issue: Issue, settings: Settings,
                   linear: LinearClient, prompt_builder) -> str:
    from loki2.clients.git import diff_stat, detect_default_branch

    detail = linear.fetch_issue_detail(issue.id)
    sub_data = linear.fetch_sub_issues(issue.id)
    sub_summary = [f"- {s['identifier']}: {s['title']} ({s.get('state', '')})"
                   for s in sub_data.get("sub_issues", [])]
    default_branch = detect_default_branch(issue.repo_path)
    stat = diff_stat(issue.repo_path, default_branch, issue.identifier)

    context = {
        "PARENT_ISSUE_DETAIL": detail,
        "PLAN_DOCUMENTS": sub_data.get("documents", []),
        "SUB_ISSUES": "\n".join(sub_summary),
        "DIFF_STAT": stat,
    }
    return prompt_builder.build("pr", context)


def create_pr(issue: Issue, settings: Settings,
              linear: LinearClient, workspace_mgr,
              prompt_builder) -> PhaseResult:
    from loki2.clients.claude import run as run_claude
    from loki2.clients.git import detect_default_branch, pr_create, push

    parent_wt = workspace_mgr.worktree_path(issue.repo_path, issue.identifier)
    if parent_wt.exists():
        push(str(parent_wt), issue.identifier)

    prompt = prepare_prompt(issue, settings, linear, prompt_builder)
    model = settings.model_for_phase("pr")
    pc = settings.phase_config("pr")

    result = run_claude(
        prompt, Path(issue.repo_path),
        model=model, max_turns="1", budget=str(pc.budget),
        capture_output=True, timeout=pc.timeout,
    )

    detail = linear.fetch_issue_detail(issue.id)
    title = detail.get("title", issue.identifier)
    body = result.get("result", "")

    if "TITLE:" in body and "---" in body:
        parts = body.split("---", 1)
        for line in parts[0].splitlines():
            if line.startswith("TITLE:"):
                title = line.removeprefix("TITLE:").strip()
                break
        body = parts[1].strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[1] if "\n" in body else body
        if body.endswith("```"):
            body = body.rsplit("\n", 1)[0] if "\n" in body else body

    default_branch = detect_default_branch(issue.repo_path)
    ret = pr_create(issue.repo_path, title, body, issue.identifier, default_branch)
    if ret.returncode != 0:
        raise RuntimeError(f"PR creation failed: {ret.stderr}")

    return PhaseResult(event="all_done", comment=f"PR created: {title}")
