#!/usr/bin/env python3
"""Execute claude CLI per issue."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

FORGE_ROOT = Path(__file__).resolve().parent.parent

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


def mark_failed(env: dict, issue_id: str, log_file: Path):
    tail = ""
    if log_file.exists():
        lines = log_file.read_text().splitlines()
        tail = "\n".join(lines[-20:])

    run_env = {**os.environ}
    run_env.pop("CLAUDECODE", None)

    subprocess.run(
        [
            "claude", "--print",
            "--no-session-persistence",
            "--max-budget-usd", "0.03",
            "--model", env["FORGE_MODEL"],
            "--allowedTools", "mcp__linear-server__save_issue,mcp__linear-server__save_comment",
            "-p", f'Change the status of Linear issue ID {issue_id} to "Failed". Also post a comment reporting the execution failure. Log tail:\n{tail}',
        ],
        capture_output=True, text=True, env=run_env,
    )

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

        # Create worktree: new branch from parent branch (or main)
        base_branch = parent_identifier if parent_identifier else "main"
        ret = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", str(worktree_dir), "-b", issue_identifier, base_branch],
            capture_output=True,
        )
        if ret.returncode != 0:
            ret = subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", str(worktree_dir), issue_identifier],
                capture_output=True,
            )
            if ret.returncode != 0:
                print(f"Failed to create worktree for {issue_identifier}", file=sys.stderr)
                sys.exit(1)

        work_dir = worktree_dir
    else:
        print(f"Unknown phase: {phase}", file=sys.stderr)
        sys.exit(1)

    try:
        run_env = {**os.environ}
        run_env.pop("CLAUDECODE", None)

        extra_write = []
        if parent_identifier:
            parent_wt = worktree_base / repo.name / parent_identifier
            extra_write.append(str(parent_wt))
        setup_sandbox(work_dir, log_dir, extra_write_paths=extra_write or None)

        cmd = [
            "claude", "--print",
            "--no-session-persistence",
            "--max-budget-usd", budget,
            "--model", model,
            "--dangerously-skip-permissions",
            "-p", prompt,
        ]
        if max_turns:
            cmd.extend(["--max-turns", max_turns])

        with open(log_file, "w") as log:
            ret = subprocess.run(
                cmd,
                stdout=log, stderr=subprocess.STDOUT,
                cwd=work_dir, env=run_env,
            )

        if ret.returncode != 0:
            mark_failed(env, issue_id, log_file)
            sys.exit(1)

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
                    mark_failed(env, issue_id, log_file)
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

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: run_claude.py <phase> <issue_id> <identifier> <repo_path> [parent_issue_id] [parent_identifier]", file=sys.stderr)
        sys.exit(1)
    parent_id = sys.argv[5] if len(sys.argv) > 5 else ""
    parent_ident = sys.argv[6] if len(sys.argv) > 6 else ""
    run(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], parent_id, parent_ident)
