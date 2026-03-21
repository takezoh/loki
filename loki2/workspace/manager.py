from __future__ import annotations

import logging
from pathlib import Path

from loki2.clients import git

log = logging.getLogger("loki2.workspace")


class WorkspaceManager:
    def __init__(self, worktree_dir: Path):
        self._worktree_dir = worktree_dir
        self._worktree_dir.mkdir(parents=True, exist_ok=True)

    def worktree_path(self, repo_path: str, identifier: str) -> Path:
        repo_name = Path(repo_path).name
        return self._worktree_dir / f"{repo_name}-{identifier}"

    def create_detached(self, repo_path: str, identifier: str, base_branch: str) -> Path:
        wt_path = self.worktree_path(repo_path, identifier)
        if wt_path.exists():
            self.destroy(repo_path, identifier)
        result = git.worktree_add(repo_path, str(wt_path), base_branch, detach=True)
        if result.returncode != 0:
            raise RuntimeError(f"worktree_add failed: {result.stderr}")
        return wt_path

    def create_branch(self, repo_path: str, identifier: str,
                      base_branch: str, new_branch: str) -> Path:
        wt_path = self.worktree_path(repo_path, identifier)
        if wt_path.exists():
            self.destroy(repo_path, identifier)

        if git.branch_exists(repo_path, new_branch):
            result = git.worktree_add(repo_path, str(wt_path), new_branch)
        else:
            result = git.worktree_add(repo_path, str(wt_path), base_branch,
                                      new_branch=new_branch)
        if result.returncode != 0:
            raise RuntimeError(f"worktree_add failed: {result.stderr}")
        return wt_path

    def destroy(self, repo_path: str, identifier: str):
        wt_path = self.worktree_path(repo_path, identifier)
        if wt_path.exists():
            git.worktree_remove(repo_path, str(wt_path))

    def merge_to_parent(self, repo_path: str, child_branch: str,
                        parent_identifier: str, parent_branch: str,
                        resolve_with_claude: bool = True) -> bool:
        wt_path = self.worktree_path(repo_path, parent_identifier)
        if not wt_path.exists():
            self.create_branch(repo_path, parent_identifier,
                               git.detect_default_branch(repo_path),
                               parent_branch)

        message = f"Merge {child_branch} into {parent_branch}"
        result = git.merge(str(wt_path), child_branch, message)
        if result.returncode == 0:
            return True

        if not resolve_with_claude:
            git.merge_abort(str(wt_path))
            return False

        log.info("Merge conflict detected, attempting LLM resolution for %s into %s",
                 child_branch, parent_branch)
        try:
            from loki2.clients.claude import run as run_claude, setup_settings
            setup_settings(wt_path, phase="review")
            resolve_result = run_claude(
                f"There is a merge conflict in this repository. "
                f"Run `git diff` to see the conflicts, resolve ALL conflicts, "
                f"then run `git add` on resolved files and `git commit -m '{message}'`. "
                f"Do not abort the merge.",
                wt_path,
                model="sonnet",
                max_turns="10",
                budget="1.00",
                capture_output=True,
                timeout=300,
            )
            if resolve_result.get("returncode", -1) == 0:
                if git.has_new_commits(str(wt_path), f"{parent_branch}@{{1}}"):
                    log.info("LLM successfully resolved merge conflict")
                    return True
        except Exception as e:
            log.warning("LLM conflict resolution failed: %s", e)

        log.warning("LLM conflict resolution unsuccessful, aborting merge")
        git.merge_abort(str(wt_path))
        return False
