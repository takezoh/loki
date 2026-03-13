import json
import sys
from datetime import datetime
from pathlib import Path

from .config import FORGE_ROOT, load_env
from .constants import (STATE_PENDING_APPROVAL, STATE_DONE, STATE_IN_REVIEW,
                        STATE_FAILED, PHASE_PLANNING, PHASE_IMPLEMENTING,
                        PHASE_REVIEW, PHASE_PLAN_REVIEW)
from .git import (detect_default_branch, worktree_add, worktree_remove,
                  merge, merge_abort, push, delete_branch, pr_diff,
                  fetch_pr_review_comments)
from .claude import run as run_claude, resolve_config
from .linear import (update_issue_state, create_comment, fetch_issue_detail,
                     fetch_issue_comments, fetch_todo_state_id, fetch_sub_issues)


def mark_failed(issue_id: str, log_file: Path):
    tail = ""
    if log_file.exists():
        lines = log_file.read_text().splitlines()
        tail = "\n".join(lines[-20:])

    update_issue_state(issue_id, STATE_FAILED)
    body = f"Execution failed.\n\n```\n{tail}\n```" if tail else "Execution failed."
    create_comment(issue_id, body)


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


def setup_worktree(phase, repo, issue_identifier, parent_identifier, worktree_base, log_file, issue_id):
    if phase in (PHASE_PLANNING, PHASE_PLAN_REVIEW):
        return repo, None

    worktree_dir = worktree_base / repo.name / issue_identifier
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    if phase == PHASE_IMPLEMENTING:
        base_branch = parent_identifier if parent_identifier else detect_default_branch(str(repo))
        ret = worktree_add(str(repo), str(worktree_dir), base_branch, new_branch=issue_identifier)
        if ret.returncode != 0:
            print(f"worktree add (new branch) failed: {ret.stderr.strip()}", file=sys.stderr)
            ret = worktree_add(str(repo), str(worktree_dir), issue_identifier)
            if ret.returncode != 0:
                print(f"Failed to create worktree for {issue_identifier}: {ret.stderr.strip()}", file=sys.stderr)
                mark_failed(issue_id, log_file)
                sys.exit(1)
    elif phase == PHASE_REVIEW:
        ret = worktree_add(str(repo), str(worktree_dir), issue_identifier)
        if ret.returncode != 0:
            print(f"Failed to create worktree for review {issue_identifier}: {ret.stderr.strip()}", file=sys.stderr)
            mark_failed(issue_id, log_file)
            sys.exit(1)

    return worktree_dir, worktree_dir


def post_execute(phase, issue_id, issue_identifier, parent_identifier, repo,
                 worktree_base, lock_dir, log_file):
    if phase == PHASE_PLANNING:
        update_issue_state(issue_id, STATE_PENDING_APPROVAL)
    elif phase == PHASE_PLAN_REVIEW:
        update_issue_state(issue_id, STATE_PENDING_APPROVAL)
    elif phase == PHASE_IMPLEMENTING:
        update_issue_state(issue_id, STATE_DONE)
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
                mark_failed(issue_id, log_file)
                sys.exit(1)
            push(str(parent_wt), parent_identifier)


def run(phase: str, issue_id: str, issue_identifier: str, repo_path: str,
        parent_issue_id: str = "", parent_identifier: str = ""):
    env = load_env()
    log_dir = Path(env["FORGE_LOG_DIR"])
    lock_dir = Path(env["FORGE_LOCK_DIR"])
    log_file = log_dir / f"{issue_identifier}-{datetime.now():%Y%m%d-%H%M%S}.log"
    lock_file = lock_dir / f"{issue_id}.lock"
    worktree_base = Path(env["FORGE_WORKTREE_DIR"])
    repo = Path(repo_path)

    prompt = prepare_prompt(phase, issue_id, issue_identifier, parent_issue_id,
                            parent_identifier, repo_path, env)

    work_dir, worktree_dir = setup_worktree(phase, repo, issue_identifier,
                                            parent_identifier, worktree_base,
                                            log_file, issue_id)

    try:
        extra_write = []
        if parent_identifier:
            extra_write.append(str(worktree_base / repo.name / parent_identifier))

        cfg = resolve_config(phase, env)
        ret = run_claude(prompt, work_dir, **cfg,
                         log_dir=log_dir, log_file=log_file,
                         extra_write_paths=extra_write or None)

        if ret.returncode != 0:
            mark_failed(issue_id, log_file)
            sys.exit(1)

        post_execute(phase, issue_id, issue_identifier, parent_identifier,
                     repo, worktree_base, lock_dir, log_file)
    finally:
        lock_file.unlink(missing_ok=True)
        if worktree_dir and worktree_dir.exists():
            worktree_remove(str(repo), str(worktree_dir))
        if phase == PHASE_IMPLEMENTING:
            delete_branch(str(repo), issue_identifier)


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python -m forge.executor <phase> <issue_id> <identifier> <repo_path> [parent_issue_id] [parent_identifier]", file=sys.stderr)
        sys.exit(1)
    parent_id = sys.argv[5] if len(sys.argv) > 5 else ""
    parent_ident = sys.argv[6] if len(sys.argv) > 6 else ""
    run(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], parent_id, parent_ident)
