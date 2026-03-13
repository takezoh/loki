#!/usr/bin/env python3
"""Execute claude CLI per issue."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

FORGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FORGE_ROOT / "bin"))

from poll import (update_issue_state, create_comment, fetch_issue_detail,
                 fetch_issue_comments, fetch_todo_state_id, fetch_sub_issues)


def detect_default_branch(repo_path: str) -> str:
    ret = subprocess.run(
        ["git", "-C", repo_path, "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True,
    )
    if ret.returncode != 0:
        subprocess.run(
            ["git", "-C", repo_path, "remote", "set-head", "origin", "--auto"],
            capture_output=True,
        )
        ret = subprocess.run(
            ["git", "-C", repo_path, "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True,
        )
    if ret.returncode == 0:
        return ret.stdout.strip().split("/")[-1]
    return "main"

SANDBOX_SETTINGS = {
    "sandbox": {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "filesystem": {
            "denyRead": ["~/.ssh", "~/.aws", "~/.gnupg"],
            "denyWrite": ["~/.ssh", "~/.aws", "~/.gnupg", "~/.bashrc", "~/.zshrc"],
        },
        "network": {
            "allowManagedDomainsOnly": True,
            "allowedDomains": [
                "api.linear.app",
                "github.com",
                "*.github.com",
                "*.githubusercontent.com",
                "api.anthropic.com",
            ],
        },
    },
    "permissions": {
        "deny": [
            "Bash(rm -rf /)",
            "Bash(git push * --force *)",
            "Bash(git push * -f *)",
        ],
    },
}


def setup_sandbox(work_dir: Path, log_dir: Path, extra_write_paths: list[str] | None = None):
    settings = json.loads(json.dumps(SANDBOX_SETTINGS))
    allow_write = [str(log_dir)]
    if extra_write_paths:
        allow_write.extend(extra_write_paths)
    settings["sandbox"]["filesystem"]["allowWrite"] = allow_write

    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_file = claude_dir / "settings.local.json"
    settings_file.write_text(json.dumps(settings, indent=2))


def load_env():
    env = {}
    with open(FORGE_ROOT / "config" / "forge.env") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            env[k] = v.strip('"').strip("'")
    return env


def fetch_pr_review_comments(branch: str, repo_path: str) -> str:
    # Get PR number
    pr_view = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "number,reviews,comments"],
        capture_output=True, text=True, cwd=repo_path,
    )
    if pr_view.returncode != 0:
        return ""

    pr_data = json.loads(pr_view.stdout)
    pr_number = pr_data["number"]
    parts = []

    # Top-level review comments
    for review in pr_data.get("reviews", []):
        body = review.get("body", "").strip()
        if body:
            state = review.get("state", "")
            author = review.get("author", {}).get("login", "unknown")
            parts.append(f"[review ({state}) by {author}]\n{body}")

    # Top-level PR comments
    for comment in pr_data.get("comments", []):
        body = comment.get("body", "").strip()
        if body:
            author = comment.get("author", {}).get("login", "unknown")
            parts.append(f"[comment by {author}]\n{body}")

    # Inline review comments (file-level)
    # Get repo owner/name from git remote
    remote = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True, text=True, cwd=repo_path,
    )
    if remote.returncode == 0:
        repo_slug = remote.stdout.strip()
        inline = subprocess.run(
            ["gh", "api", f"repos/{repo_slug}/pulls/{pr_number}/comments"],
            capture_output=True, text=True, cwd=repo_path,
        )
        if inline.returncode == 0:
            for c in json.loads(inline.stdout):
                path = c.get("path", "")
                line = c.get("original_line") or c.get("line") or ""
                body = c.get("body", "").strip()
                author = c.get("user", {}).get("login", "unknown")
                if body:
                    parts.append(f"[{path}:{line} by {author}]\n{body}")

    return "\n\n".join(parts)


def mark_failed(issue_id: str, log_file: Path):
    tail = ""
    if log_file.exists():
        lines = log_file.read_text().splitlines()
        tail = "\n".join(lines[-20:])

    update_issue_state(issue_id, "Failed")
    body = f"Execution failed.\n\n```\n{tail}\n```" if tail else "Execution failed."
    create_comment(issue_id, body)

def run(phase: str, issue_id: str, issue_identifier: str, repo_path: str,
        parent_issue_id: str = "", parent_identifier: str = ""):
    env = load_env()
    log_dir = Path(env["FORGE_LOG_DIR"])
    lock_dir = Path(env["FORGE_LOCK_DIR"])
    log_file = log_dir / f"{issue_identifier}-{datetime.now():%Y%m%d-%H%M%S}.log"
    lock_file = lock_dir / f"{issue_id}.lock"
    worktree_dir = None
    worktree_base = Path(env["FORGE_WORKTREE_DIR"])

    prompt_file = FORGE_ROOT / "prompts" / f"{phase}.md"
    if not prompt_file.exists():
        print(f"Prompt file not found: {prompt_file}", file=sys.stderr)
        sys.exit(1)

    prompt = prompt_file.read_text()
    prompt = prompt.replace("{{ISSUE_ID}}", issue_id)
    prompt = prompt.replace("{{ISSUE_IDENTIFIER}}", issue_identifier)
    prompt = prompt.replace("{{PARENT_ISSUE_ID}}", parent_issue_id)
    prompt = prompt.replace("{{PARENT_IDENTIFIER}}", parent_identifier)

    # Pre-fetch Linear data and inject into prompt
    if phase == "planning":
        issue_detail = fetch_issue_detail(issue_id)
        prompt = prompt.replace("{{ISSUE_DETAIL}}", json.dumps(issue_detail, indent=2, ensure_ascii=False))
        todo_state_id = fetch_todo_state_id()
        prompt = prompt.replace("{{TODO_STATE_ID}}", todo_state_id)
    elif phase == "review":
        issue_detail = fetch_issue_detail(issue_id)
        prompt = prompt.replace("{{ISSUE_DETAIL}}", json.dumps(issue_detail, indent=2, ensure_ascii=False))

        parent_data = fetch_sub_issues(issue_id)
        prompt = prompt.replace("{{PLAN_DOCUMENTS}}", json.dumps(parent_data.get("documents", []), indent=2, ensure_ascii=False))

        pr_diff = subprocess.run(
            ["gh", "pr", "diff", issue_identifier],
            capture_output=True, text=True, cwd=repo_path,
        )
        prompt = prompt.replace("{{PR_DIFF}}", pr_diff.stdout or "(unavailable)")

        review_comments = fetch_pr_review_comments(issue_identifier, repo_path)
        prompt = prompt.replace("{{REVIEW_COMMENTS}}", review_comments or "(no comments)")

    elif phase == "implementing":
        sub_detail = fetch_issue_detail(issue_id)
        prompt = prompt.replace("{{SUB_ISSUE_DETAIL}}", json.dumps(sub_detail, indent=2, ensure_ascii=False))
        parent_detail = fetch_issue_detail(parent_issue_id)
        prompt = prompt.replace("{{PARENT_ISSUE_DETAIL}}", json.dumps(parent_detail, indent=2, ensure_ascii=False))
        parent_data = fetch_sub_issues(parent_issue_id)
        prompt = prompt.replace("{{PLAN_DOCUMENTS}}", json.dumps(parent_data.get("documents", []), indent=2, ensure_ascii=False))
        sub_comments = fetch_issue_comments(issue_id)
        prompt = prompt.replace("{{SUB_ISSUE_COMMENTS}}", json.dumps(sub_comments, indent=2, ensure_ascii=False))

    repo = Path(repo_path)

    model_key = f"FORGE_MODEL_{phase.upper()}"
    model = env.get(model_key, env["FORGE_MODEL"])

    budget_key = f"FORGE_BUDGET_{phase.upper()}"
    turns_key = f"FORGE_MAX_TURNS_{phase.upper()}"
    budget = env.get(budget_key, "1.00")
    max_turns = env.get(turns_key, "")

    if phase == "planning":
        work_dir = repo
    elif phase == "implementing":
        worktree_dir = worktree_base / repo.name / issue_identifier
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        work_dir = worktree_dir
    elif phase == "review":
        worktree_dir = worktree_base / repo.name / issue_identifier
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        work_dir = worktree_dir
    else:
        print(f"Unknown phase: {phase}", file=sys.stderr)
        sys.exit(1)

    try:
        if phase == "implementing":
            # Create worktree: new branch from parent branch (or default branch)
            base_branch = parent_identifier if parent_identifier else detect_default_branch(str(repo))
            ret = subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", str(worktree_dir), "-b", issue_identifier, base_branch],
                capture_output=True, text=True,
            )
            if ret.returncode != 0:
                print(f"worktree add (new branch) failed: {ret.stderr.strip()}", file=sys.stderr)
                ret = subprocess.run(
                    ["git", "-C", str(repo), "worktree", "add", str(worktree_dir), issue_identifier],
                    capture_output=True, text=True,
                )
                if ret.returncode != 0:
                    print(f"Failed to create worktree for {issue_identifier}: {ret.stderr.strip()}", file=sys.stderr)
                    mark_failed(issue_id, log_file)
                    sys.exit(1)
        elif phase == "review":
            # Checkout existing branch (PR already exists)
            ret = subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", str(worktree_dir), issue_identifier],
                capture_output=True, text=True,
            )
            if ret.returncode != 0:
                print(f"Failed to create worktree for review {issue_identifier}: {ret.stderr.strip()}", file=sys.stderr)
                mark_failed(issue_id, log_file)
                sys.exit(1)

        run_env = {**os.environ}
        run_env.pop("CLAUDECODE", None)

        extra_write = []
        if parent_identifier:
            parent_wt = worktree_base / repo.name / parent_identifier
            extra_write.append(str(parent_wt))
        setup_sandbox(work_dir, log_dir, extra_write_paths=extra_write or None)

        disallowed_tools_map = {
            "planning": [
                "mcp__linear-server__get_issue",
                "mcp__linear-server__list_issue_statuses",
            ],
            "implementing": [
                "mcp__linear-server__get_issue",
                "mcp__linear-server__list_documents",
                "mcp__linear-server__list_comments",
                "mcp__linear-server__save_issue",
            ],
            "review": [
                "mcp__linear-server__save_issue",
                "mcp__linear-server__get_issue",
                "mcp__linear-server__list_documents",
            ],
        }

        cmd = [
            "claude", "--print",
            "--no-session-persistence",
            "--max-budget-usd", budget,
            "--model", model,
            "--dangerously-skip-permissions",
            "-p", prompt,
        ]
        disallowed = disallowed_tools_map.get(phase, [])
        if disallowed:
            cmd.extend(["--disallowedTools", ",".join(disallowed)])
        if max_turns:
            cmd.extend(["--max-turns", max_turns])

        with open(log_file, "w") as log:
            ret = subprocess.run(
                cmd,
                stdout=log, stderr=subprocess.STDOUT,
                cwd=work_dir, env=run_env,
            )

        if ret.returncode != 0:
            mark_failed(issue_id, log_file)
            sys.exit(1)

        # Post-exec status update
        if phase == "planning":
            update_issue_state(issue_id, "Pending Approval")
        elif phase == "implementing":
            update_issue_state(issue_id, "Done")
        elif phase == "review":
            update_issue_state(issue_id, "In Review")

        # Merge sub-issue branch into parent branch
        if parent_identifier and ret.returncode == 0:
            import fcntl
            merge_lock = lock_dir / f"merge-{parent_identifier}.lock"
            parent_wt = worktree_base / repo.name / parent_identifier
            with open(merge_lock, "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                merge_ret = subprocess.run(
                    ["git", "-C", str(parent_wt), "merge", "--no-ff", issue_identifier,
                     "-m", f"Merge {issue_identifier}"],
                    capture_output=True, text=True,
                )
                if merge_ret.returncode != 0:
                    subprocess.run(["git", "-C", str(parent_wt), "merge", "--abort"],
                                   capture_output=True)
                    mark_failed(issue_id, log_file)
                    sys.exit(1)
                subprocess.run(
                    ["git", "-C", str(parent_wt), "push", "-u", "origin", parent_identifier],
                    capture_output=True,
                )
    finally:
        lock_file.unlink(missing_ok=True)
        if worktree_dir and worktree_dir.exists():
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", str(worktree_dir), "--force"],
                capture_output=True,
            )
        if phase == "implementing":
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", issue_identifier],
                capture_output=True,
            )
        # review: worktree removed but branch kept (push済みのため)

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: run_claude.py <phase> <issue_id> <identifier> <repo_path> [parent_issue_id] [parent_identifier]", file=sys.stderr)
        sys.exit(1)
    parent_id = sys.argv[5] if len(sys.argv) > 5 else ""
    parent_ident = sys.argv[6] if len(sys.argv) > 6 else ""
    run(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], parent_id, parent_ident)
