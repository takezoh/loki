import json
import sys
from datetime import datetime
from pathlib import Path

from config import FORGE_ROOT, load_env, get_api_key
from config.constants import (STATE_PENDING_APPROVAL, STATE_DONE, STATE_IN_REVIEW,
                        STATE_FAILED, PHASE_PLANNING, PHASE_IMPLEMENTING,
                        PHASE_REVIEW, PHASE_PLAN_REVIEW)
from lib.linear import (emit_thought, emit_action, emit_response, emit_error,
                        update_issue_state, create_comment, create_attachment,
                        fetch_issue_detail, fetch_issue_comments,
                        fetch_todo_state_id, fetch_sub_issues)
from lib.git import (detect_default_branch, has_new_commits, worktree_add,
                  worktree_remove, merge, merge_abort, push, delete_branch,
                  pr_diff, fetch_pr_review_comments)
from lib.claude import run as run_claude
from forge.queue import wake

def resolve_config(phase: str, env: dict) -> dict:
    model_key = f"FORGE_MODEL_{phase.upper()}"
    budget_key = f"FORGE_BUDGET_{phase.upper()}"
    turns_key = f"FORGE_MAX_TURNS_{phase.upper()}"
    return {
        "model": env.get(model_key, env["FORGE_MODEL"]),
        "budget": env.get(budget_key, "1.00"),
        "max_turns": env[turns_key],
        "phase": phase,
    }


def parse_claude_result(log_file: Path) -> tuple[str, str | None]:
    if not log_file.exists():
        return "", None

    text = log_file.read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        lines = text.splitlines()
        return f"```\n{'\n'.join(lines[-20:])}\n```", None

    raw_json = json.dumps(data, indent=2, ensure_ascii=False)
    parts = []

    result_text = data.get("result", "")
    if result_text:
        parts.append(result_text)

    stop_reason = data.get("stop_reason", "")
    duration_s = round(data.get("duration_ms", 0) / 1000)
    cost = data.get("total_cost_usd", 0)
    turns = data.get("num_turns", 0)
    parts.append(f"**Stop reason**: {stop_reason} | **Duration**: {duration_s}s | **Cost**: ${cost:.2f} | **Turns**: {turns}")

    denials = data.get("permission_denials", [])
    if denials:
        denial_lines = []
        for d in denials:
            tool = d.get("tool_name", "unknown")
            inp = d.get("tool_input", {})
            path = inp.get("file_path") or inp.get("path") or ""
            denial_lines.append(f"- `{tool}` → `{path}`" if path else f"- `{tool}`")
        parts.append("**Permission denials**:\n" + "\n".join(denial_lines))

    return "\n\n".join(parts), raw_json


def mark_failed(issue_id: str, log_file: Path, reason: str = "",
                session_id: str = "", api_key: str = ""):
    comment_body, raw_json = parse_claude_result(log_file)

    update_issue_state(issue_id, STATE_FAILED)
    parts = []
    if reason:
        parts.append(reason)
    if comment_body:
        parts.append(comment_body)
    body = "\n\n".join(parts) or "Execution failed."
    create_comment(issue_id, body)

    if raw_json:
        create_attachment(issue_id, "Execution Log",
                          raw_json.encode(), f"{issue_id}.json")

    if session_id and api_key:
        lines = body.splitlines()
        error_tail = lines[-1] if lines else body
        emit_error(session_id, error_tail, api_key)


def prepare_prompt(phase, issue_id, issue_identifier, parent_issue_id, parent_identifier, repo_path, env):
    prompt_file = FORGE_ROOT / "prompts" / f"{phase}.md"
    if not prompt_file.exists():
        print(f"Prompt file not found: {prompt_file}", file=sys.stderr)
        sys.exit(1)

    prompt = prompt_file.read_text()
    prompt = prompt.replace("{{ISSUE_ID}}", issue_id)
    prompt = prompt.replace("{{ISSUE_IDENTIFIER}}", issue_identifier)
    prompt = prompt.replace("{{PARENT_ISSUE_ID}}", parent_issue_id)
    prompt = prompt.replace("{{PARENT_IDENTIFIER}}", parent_identifier)
    prompt = prompt.replace("{{FORGE_ROOT}}", str(FORGE_ROOT))

    if phase == PHASE_PLANNING:
        issue_detail = fetch_issue_detail(issue_id)
        prompt = prompt.replace("{{ISSUE_DETAIL}}", json.dumps(issue_detail, indent=2, ensure_ascii=False))
        todo_state_id = fetch_todo_state_id()
        prompt = prompt.replace("{{TODO_STATE_ID}}", todo_state_id)
    elif phase == PHASE_REVIEW:
        issue_detail = fetch_issue_detail(issue_id)
        prompt = prompt.replace("{{ISSUE_DETAIL}}", json.dumps(issue_detail, indent=2, ensure_ascii=False))

        parent_data = fetch_sub_issues(issue_id)
        prompt = prompt.replace("{{PLAN_DOCUMENTS}}", json.dumps(parent_data.get("documents", []), indent=2, ensure_ascii=False))

        prompt = prompt.replace("{{PR_DIFF}}", pr_diff(repo_path, issue_identifier))

        review_comments = fetch_pr_review_comments(issue_identifier, repo_path)
        prompt = prompt.replace("{{REVIEW_COMMENTS}}", review_comments or "(no comments)")

    elif phase == PHASE_PLAN_REVIEW:
        issue_detail = fetch_issue_detail(issue_id)
        prompt = prompt.replace("{{ISSUE_DETAIL}}", json.dumps(issue_detail, indent=2, ensure_ascii=False))

        parent_data = fetch_sub_issues(issue_id)
        prompt = prompt.replace("{{PLAN_DOCUMENTS}}", json.dumps(parent_data.get("documents", []), indent=2, ensure_ascii=False))
        prompt = prompt.replace("{{SUB_ISSUES}}", json.dumps(parent_data.get("sub_issues", []), indent=2, ensure_ascii=False))

        comments = fetch_issue_comments(issue_id)
        prompt = prompt.replace("{{REVIEW_COMMENTS}}", json.dumps(comments, indent=2, ensure_ascii=False))

        todo_state_id = fetch_todo_state_id()
        prompt = prompt.replace("{{TODO_STATE_ID}}", todo_state_id)

    elif phase == PHASE_IMPLEMENTING:
        sub_detail = fetch_issue_detail(issue_id)
        prompt = prompt.replace("{{SUB_ISSUE_DETAIL}}", json.dumps(sub_detail, indent=2, ensure_ascii=False))
        parent_detail = fetch_issue_detail(parent_issue_id)
        prompt = prompt.replace("{{PARENT_ISSUE_DETAIL}}", json.dumps(parent_detail, indent=2, ensure_ascii=False))
        parent_data = fetch_sub_issues(parent_issue_id)
        prompt = prompt.replace("{{PLAN_DOCUMENTS}}", json.dumps(parent_data.get("documents", []), indent=2, ensure_ascii=False))
        sub_comments = fetch_issue_comments(issue_id)
        prompt = prompt.replace("{{SUB_ISSUE_COMMENTS}}", json.dumps(sub_comments, indent=2, ensure_ascii=False))

    return prompt


def setup_worktree(phase, repo, issue_identifier, parent_identifier, worktree_base, log_file, issue_id,
                   session_id="", api_key=""):
    worktree_dir = worktree_base / repo.name / issue_identifier
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    if phase in (PHASE_PLANNING, PHASE_PLAN_REVIEW):
        default_branch = detect_default_branch(str(repo))
        ret = worktree_add(str(repo), str(worktree_dir), default_branch, detach=True)
        if ret.returncode != 0:
            mark_failed(issue_id, log_file, session_id=session_id, api_key=api_key)
            sys.exit(1)
        return worktree_dir, worktree_dir

    if phase == PHASE_IMPLEMENTING:
        base_branch = parent_identifier if parent_identifier else detect_default_branch(str(repo))
        ret = worktree_add(str(repo), str(worktree_dir), base_branch, new_branch=issue_identifier)
        if ret.returncode != 0:
            print(f"worktree add (new branch) failed: {ret.stderr.strip()}", file=sys.stderr)
            ret = worktree_add(str(repo), str(worktree_dir), issue_identifier)
            if ret.returncode != 0:
                print(f"Failed to create worktree for {issue_identifier}: {ret.stderr.strip()}", file=sys.stderr)
                mark_failed(issue_id, log_file, session_id=session_id, api_key=api_key)
                sys.exit(1)
    elif phase == PHASE_REVIEW:
        ret = worktree_add(str(repo), str(worktree_dir), issue_identifier)
        if ret.returncode != 0:
            print(f"Failed to create worktree for review {issue_identifier}: {ret.stderr.strip()}", file=sys.stderr)
            mark_failed(issue_id, log_file, session_id=session_id, api_key=api_key)
            sys.exit(1)

    return worktree_dir, worktree_dir


def post_execute(phase, issue_id, issue_identifier, parent_issue_id, parent_identifier, repo,
                 worktree_base, lock_dir, log_file, work_dir=None, base_branch=None,
                 session_id="", api_key="", env=None):
    if phase == PHASE_PLANNING:
        result = fetch_sub_issues(issue_id)
        if not result.get("sub_issues"):
            mark_failed(issue_id, log_file, reason="Planning completed but no sub-issues were created.", session_id=session_id, api_key=api_key)
            sys.exit(1)
        update_issue_state(issue_id, STATE_PENDING_APPROVAL)
    elif phase == PHASE_PLAN_REVIEW:
        update_issue_state(issue_id, STATE_PENDING_APPROVAL)
    elif phase == PHASE_IMPLEMENTING:
        comment_body, raw_json = parse_claude_result(log_file)
        already_implemented = "ALREADY_IMPLEMENTED" in (comment_body or "")

        if work_dir and base_branch and not has_new_commits(str(work_dir), base_branch):
            if already_implemented:
                if comment_body:
                    create_comment(issue_id, comment_body)
                update_issue_state(issue_id, STATE_DONE)
            else:
                mark_failed(issue_id, log_file, reason="No commits were created.", session_id=session_id, api_key=api_key)
                sys.exit(1)
        else:
            if comment_body:
                create_comment(issue_id, comment_body)
            update_issue_state(issue_id, STATE_DONE)

        if parent_issue_id:
            summary = f"**{issue_identifier}**: {'Already implemented' if already_implemented else 'Implementation complete'}"
            create_comment(parent_issue_id, summary)

        pid_file = (env or {}).get("FORGE_PID_FILE", "")
        wake(pid_file)
    elif phase == PHASE_REVIEW:
        update_issue_state(issue_id, STATE_IN_REVIEW)

    if parent_identifier:
        import fcntl
        merge_lock = lock_dir / f"merge-{parent_identifier}.lock"
        parent_wt = worktree_base / repo.name / parent_identifier
        with open(merge_lock, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            merge_ret = merge(str(parent_wt), issue_identifier,
                              f"Merge {issue_identifier}")
            if merge_ret.returncode != 0:
                merge_abort(str(parent_wt))
                mark_failed(issue_id, log_file, session_id=session_id, api_key=api_key)
                sys.exit(1)
            push(str(parent_wt), parent_identifier)


def run(phase: str, issue_id: str, issue_identifier: str, repo_path: str,
        parent_issue_id: str = "", parent_identifier: str = "",
        session_id: str = ""):
    env = load_env()
    api_key = get_api_key(env)

    log_dir = Path(env["FORGE_LOG_DIR"])
    lock_dir = Path(env["FORGE_LOCK_DIR"])
    log_file = log_dir / f"{issue_identifier}-{datetime.now():%Y%m%d-%H%M%S}.log"
    lock_file = lock_dir / f"{issue_id}.lock"
    worktree_base = Path(env["FORGE_WORKTREE_DIR"])
    repo = Path(repo_path)

    if session_id:
        emit_thought(session_id, f"Investigating {issue_identifier}...", api_key)

    prompt = prepare_prompt(phase, issue_id, issue_identifier, parent_issue_id,
                            parent_identifier, repo_path, env)

    work_dir, worktree_dir = setup_worktree(phase, repo, issue_identifier,
                                            parent_identifier, worktree_base,
                                            log_file, issue_id,
                                            session_id=session_id, api_key=api_key)

    try:
        extra_write = [str(repo / ".git" / "worktrees")]
        if parent_identifier:
            extra_write.append(str(worktree_base / repo.name / parent_identifier))

        cfg = resolve_config(phase, env)

        if session_id:
            emit_action(session_id, "Executing Claude", phase, api_key)

        ret = run_claude(prompt, work_dir, **cfg,
                         log_file=log_file,
                         allow_write=extra_write)

        if ret.returncode != 0:
            mark_failed(issue_id, log_file, session_id=session_id, api_key=api_key)
            sys.exit(1)

        if session_id:
            result_text, _ = parse_claude_result(log_file)
            emit_action(session_id, "Executing Claude", phase, api_key, result=result_text)
            emit_response(session_id, result_text, api_key)

        base_branch = None
        if phase == PHASE_IMPLEMENTING:
            base_branch = parent_identifier if parent_identifier else detect_default_branch(str(repo))

        post_execute(phase, issue_id, issue_identifier, parent_issue_id,
                     parent_identifier, repo, worktree_base, lock_dir, log_file,
                     work_dir=work_dir, base_branch=base_branch,
                     session_id=session_id, api_key=api_key, env=env)

        if session_id:
            emit_response(session_id, f"Completed {phase}", api_key)
    finally:
        lock_file.unlink(missing_ok=True)
        if worktree_dir and worktree_dir.exists():
            worktree_remove(str(repo), str(worktree_dir))
        if phase == PHASE_IMPLEMENTING:
            delete_branch(str(repo), issue_identifier)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("phase")
    parser.add_argument("issue_id")
    parser.add_argument("identifier")
    parser.add_argument("repo_path")
    parser.add_argument("parent_issue_id", nargs="?", default="")
    parser.add_argument("parent_identifier", nargs="?", default="")
    parser.add_argument("--session-id", default="")
    args = parser.parse_args()
    run(args.phase, args.issue_id, args.identifier, args.repo_path,
        args.parent_issue_id, args.parent_identifier, args.session_id)
