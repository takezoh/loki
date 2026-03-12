#!/usr/bin/env python3
"""forge main entry point: Linear polling → issue dispatch → background execution."""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

FORGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FORGE_ROOT / "bin"))

from poll import load_env, poll, fetch_sub_issues, update_issue_state

def load_repos() -> dict[str, str]:
    repos = {}
    conf = FORGE_ROOT / "config" / "repos.conf"
    with open(conf) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            if k and v:
                repos[k.strip()] = v.strip()
    return repos

def resolve_repo(labels: list[str], repos: dict[str, str]) -> str | None:
    for label in labels:
        if label.startswith("repo:"):
            key = label.removeprefix("repo:")
            return repos.get(key)
    return None

def count_locks(lock_dir: Path) -> int:
    return len(list(lock_dir.glob("*.lock")))

def clean_stale_locks(lock_dir: Path, timeout_min: int):
    now = time.time()
    for lock in lock_dir.glob("*.lock"):
        age_min = (now - lock.stat().st_mtime) / 60
        if age_min > timeout_min:
            lock.unlink(missing_ok=True)

def log(msg: str):
    print(f"[{datetime.now():%H:%M:%S}] {msg}")

def dispatch_issue(phase: str, issue: dict, lock_dir: Path, max_concurrent: int,
                   repos: dict[str, str], parent_id: str = "") -> subprocess.Popen | None:
    issue_id = issue["id"]
    identifier = issue["identifier"]
    title = issue["title"]
    labels = issue.get("labels", [])

    lock_file = lock_dir / f"{issue_id}.lock"
    if lock_file.exists():
        log(f"  Skip {identifier} (locked): {title}")
        return None

    if count_locks(lock_dir) >= max_concurrent:
        log(f"  Skip {identifier} (max concurrent): {title}")
        return None

    repo_path = resolve_repo(labels, repos)
    if not repo_path:
        log(f"  Skip {identifier} (no repo label): {title}")
        return None
    if not Path(repo_path).is_dir():
        log(f"  Skip {identifier} (repo not found: {repo_path}): {title}")
        return None

    log(f"  Start {identifier} ({phase}): {title}")
    lock_file.write_text(identifier)

    cmd = [sys.executable, str(FORGE_ROOT / "bin" / "run_claude.py"), phase, issue_id, identifier, repo_path]
    if parent_id:
        cmd.append(parent_id)

    return subprocess.Popen(cmd)

def main():
    env = load_env()
    log_dir = Path(env["FORGE_LOG_DIR"])
    lock_dir = Path(env["FORGE_LOCK_DIR"])
    max_concurrent = int(env["FORGE_MAX_CONCURRENT"])
    lock_timeout = int(env["FORGE_LOCK_TIMEOUT_MIN"])

    log_dir.mkdir(parents=True, exist_ok=True)
    lock_dir.mkdir(parents=True, exist_ok=True)

    repos = load_repos()

    clean_stale_locks(lock_dir, lock_timeout)

    log("=== forge started ===")

    log("Polling Planning issues...")
    planning_issues = poll("Planning")

    log("Polling Implementing issues...")
    implementing_issues = poll("Implementing")

    processes: list[subprocess.Popen] = []

    # Planning: dispatch parent issues directly
    if planning_issues:
        log(f"{len(planning_issues)} planning issue(s) found")
        for issue in planning_issues:
            p = dispatch_issue("planning", issue, lock_dir, max_concurrent, repos)
            if p:
                processes.append(p)

    # Implementing: parent issue → dispatch ready sub-issues by dependency order
    if implementing_issues:
        log(f"{len(implementing_issues)} implementing parent issue(s) found")
        for parent in implementing_issues:
            parent_id = parent["id"]
            parent_identifier = parent["identifier"]
            parent_labels = parent.get("labels", [])

            repo_path = resolve_repo(parent_labels, repos)
            if not repo_path:
                log(f"  Skip {parent_identifier} (no repo label): {parent['title']}")
                continue
            if not Path(repo_path).is_dir():
                log(f"  Skip {parent_identifier} (repo not found: {repo_path}): {parent['title']}")
                continue

            log(f"  Fetching sub-issues for {parent_identifier}...")
            result = fetch_sub_issues(parent_id)
            sub_issues = result["sub_issues"]

            if result.get("cycle"):
                log(f"  Skip {parent_identifier} (dependency cycle: {' -> '.join(result['cycle'])})")
                continue

            ready = [s for s in sub_issues if s.get("ready")]
            done = [s for s in sub_issues if s.get("state") in ("Done", "In Review")]
            log(f"  {parent_identifier}: {len(sub_issues)} sub-issues, {len(ready)} ready, {len(done)} done")

            for sub in ready:
                sub_issue = {
                    "id": sub["id"],
                    "identifier": sub["identifier"],
                    "title": sub["title"],
                    "labels": parent_labels,
                }
                p = dispatch_issue("implementing", sub_issue, lock_dir, max_concurrent, repos, parent_id=parent_id)
                if p:
                    update_issue_state(sub["id"], "In Progress")
                    processes.append(p)

    for p in processes:
        p.wait()

    log("=== forge finished ===")

if __name__ == "__main__":
    main()
