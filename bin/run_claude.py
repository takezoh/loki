#!/usr/bin/env python3
"""Issue 単位で claude CLI を実行する。"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

FORGE_ROOT = Path(__file__).resolve().parent.parent

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
            "-p", f'Linear の Issue ID {issue_id} のステータスを "Failed" に変更して。また、コメントで実行が失敗したことを報告して。ログの末尾:\n{tail}',
        ],
        capture_output=True, text=True, env=run_env,
    )

def run(phase: str, issue_id: str, issue_identifier: str, repo_path: str, parent_issue_id: str = ""):
    env = load_env()
    log_dir = Path(env["FORGE_LOG_DIR"])
    lock_dir = Path(env["FORGE_LOCK_DIR"])
    log_file = log_dir / f"{issue_identifier}-{datetime.now():%Y%m%d-%H%M%S}.log"
    lock_file = lock_dir / f"{issue_id}.lock"
    worktree_dir = None

    prompt_file = FORGE_ROOT / "prompts" / f"{phase}.md"
    if not prompt_file.exists():
        print(f"Prompt file not found: {prompt_file}", file=sys.stderr)
        sys.exit(1)

    prompt = prompt_file.read_text()
    prompt = prompt.replace("{{ISSUE_ID}}", issue_id)
    prompt = prompt.replace("{{ISSUE_IDENTIFIER}}", issue_identifier)
    prompt = prompt.replace("{{PARENT_ISSUE_ID}}", parent_issue_id)

    repo = Path(repo_path)

    model_key = f"FORGE_MODEL_{phase.upper()}"
    model = env.get(model_key, env["FORGE_MODEL"])

    if phase == "planning":
        budget = env["FORGE_BUDGET_PLANNING"]
        work_dir = repo
    elif phase == "implementing":
        budget = env["FORGE_BUDGET_IMPLEMENTING"]
        worktree_base = Path(env["FORGE_WORKTREE_DIR"])
        worktree_dir = worktree_base / repo.name / issue_identifier
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        # worktree 作成: 新規ブランチ or 既存ブランチ
        ret = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", str(worktree_dir), "-b", issue_identifier, "main"],
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

        with open(log_file, "w") as log:
            ret = subprocess.run(
                [
                    "claude", "--print",
                    "--no-session-persistence",
                    "--max-budget-usd", budget,
                    "--model", model,
                    "--permission-mode", "bypassPermissions",
                    "-p", prompt,
                ],
                stdout=log, stderr=subprocess.STDOUT,
                cwd=work_dir, env=run_env,
            )

        if ret.returncode != 0:
            mark_failed(env, issue_id, log_file)
            sys.exit(1)
    finally:
        lock_file.unlink(missing_ok=True)
        if worktree_dir and worktree_dir.exists():
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", str(worktree_dir), "--force"],
                capture_output=True,
            )

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: run_claude.py <phase> <issue_id> <identifier> <repo_path> [parent_issue_id]", file=sys.stderr)
        sys.exit(1)
    parent_id = sys.argv[5] if len(sys.argv) > 5 else ""
    run(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], parent_id)
