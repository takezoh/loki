from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from pathlib import Path

from loki2.clients.claude import run as run_claude, setup_settings
from loki2.clients.linear import LinearClient, _resolve_repo, _resolve_base_branch
from loki2.config import Settings
from loki2.core.state import (
    PHASE_PLANNING, PHASE_PLAN_REVIEW, PHASE_SUBISSUE_CREATION,
    PHASE_IMPLEMENTING, PHASE_REVIEW, PHASE_PR,
    STATE_IMPLEMENTING, STATE_PLANNING, STATE_CHANGES_REQUESTED,
    STATE_TO_PHASE, STATE_DONE, STATE_FAILED, STATE_CANCELLED,
    END_STATES, FINISHED_STATE_TYPES,
    next_state,
)
from loki2.phases import PhaseResult, planning, plan_review, subissue_creation
from loki2.phases import implementing, review
from loki2.phases import pr as pr_phase
from loki2.prompt import PromptBuilder
from loki2.store.db import Database
from loki2.store.models import Issue
from loki2.workspace.manager import WorkspaceManager

log = logging.getLogger("loki2")


class Scheduler:
    def __init__(self, settings: Settings, db: Database,
                 linear: LinearClient, workspace: WorkspaceManager,
                 prompt_builder: PromptBuilder):
        self.settings = settings
        self.db = db
        self.linear = linear
        self.workspace = workspace
        self.prompt_builder = prompt_builder
        self._executor = ThreadPoolExecutor(max_workers=settings.max_concurrent)
        self._semaphore = threading.Semaphore(settings.max_concurrent)
        self._shutdown = threading.Event()
        self._merge_locks: dict[str, threading.Lock] = {}
        self._merge_locks_guard = threading.Lock()
        self.running: dict[str, Future] = {}
        self._running_lock = threading.Lock()

    def run(self):
        log.info("Scheduler started (poll_interval=%ds, max_concurrent=%d)",
                 self.settings.poll_interval, self.settings.max_concurrent)

        self._recover_stale_tasks()

        while not self._shutdown.is_set():
            try:
                self._poll_and_dispatch()
            except Exception:
                log.exception("Error in poll cycle")

            self._shutdown.wait(timeout=self.settings.poll_interval)

        log.info("Scheduler shutting down...")
        self._executor.shutdown(wait=True, cancel_futures=True)
        log.info("Scheduler stopped")

    def stop(self):
        self._shutdown.set()

    def _recover_stale_tasks(self):
        stale = self.db.get_running_issues()
        if not stale:
            return
        log.info("Recovering %d stale running tasks from previous session", len(stale))
        for issue in stale:
            pid = issue.pid
            if pid:
                try:
                    os.kill(pid, 0)
                    log.warning("[%s] PID %d still alive", issue.identifier, pid)
                except (ProcessLookupError, PermissionError):
                    pass

            if issue.retry_count < self.settings.max_retries:
                log.info("[%s] Resetting to queued for retry (%d/%d)",
                         issue.identifier, issue.retry_count + 1, self.settings.max_retries)
                self.db.update_status(
                    issue.id, "queued", retry_count=issue.retry_count + 1, pid=None)
            else:
                log.warning("[%s] Max retries reached, marking as failed", issue.identifier)
                self.db.update_status(issue.id, "failed", error="Stale after crash", pid=None)

    def _poll_and_dispatch(self):
        repos = {k: str(v) for k, v in self.settings.repos.items()}

        for state in [STATE_CHANGES_REQUESTED, STATE_PLANNING, STATE_IMPLEMENTING]:
            phase = STATE_TO_PHASE.get(state)
            if not phase:
                continue

            issues = self.linear.poll(state)
            for issue_data in issues:
                issue_id = issue_data["id"]
                with self._running_lock:
                    if issue_id in self.running:
                        continue

                repo_path = _resolve_repo(issue_data["labels"], repos)
                if not repo_path:
                    continue

                base_branch = _resolve_base_branch(issue_data["labels"])

                if state == STATE_PLANNING:
                    self._handle_planning_issue(issue_data, repo_path, base_branch)
                elif state == STATE_IMPLEMENTING:
                    self._handle_implementing_issue(issue_data, repo_path, base_branch)
                elif state == STATE_CHANGES_REQUESTED:
                    self._dispatch(Issue(
                        id=issue_id,
                        identifier=issue_data["identifier"],
                        title=issue_data["title"],
                        phase=PHASE_REVIEW,
                        status="queued",
                        repo_path=repo_path,
                        base_branch=base_branch or None,
                    ))

    def _handle_planning_issue(self, issue_data: dict, repo_path: str, base_branch: str):
        sub_data = self.linear.fetch_sub_issues(issue_data["id"])
        has_plan = bool(sub_data.get("documents"))
        phase = PHASE_PLAN_REVIEW if has_plan else PHASE_PLANNING
        self._dispatch(Issue(
            id=issue_data["id"],
            identifier=issue_data["identifier"],
            title=issue_data["title"],
            phase=phase,
            status="queued",
            repo_path=repo_path,
            base_branch=base_branch or None,
        ))

    def _handle_implementing_issue(self, issue_data: dict, repo_path: str, base_branch: str):
        parent_id = issue_data["id"]
        parent_identifier = issue_data["identifier"]
        sub_data = self.linear.fetch_sub_issues(parent_id)

        if sub_data.get("cycle"):
            log.error("[%s] Dependency cycle detected: %s", parent_identifier, sub_data["cycle"])
            return

        sub_issues = sub_data.get("sub_issues", [])

        if not sub_issues:
            self._dispatch(Issue(
                id=parent_id, identifier=parent_identifier,
                title=issue_data["title"],
                phase=PHASE_SUBISSUE_CREATION, status="queued",
                repo_path=repo_path, base_branch=base_branch or None,
            ))
            return

        all_finished = all(s["state_type"] in FINISHED_STATE_TYPES for s in sub_issues)
        if all_finished:
            if all(s["state"] in (STATE_DONE, STATE_CANCELLED) for s in sub_issues):
                self._dispatch(Issue(
                    id=parent_id, identifier=parent_identifier,
                    title=issue_data["title"],
                    phase=PHASE_PR, status="queued",
                    repo_path=repo_path, base_branch=base_branch or None,
                ))
            return

        for sub in sub_issues:
            if not sub["ready"]:
                continue
            sub_id = sub["id"]
            with self._running_lock:
                if sub_id in self.running:
                    continue
            self._dispatch(Issue(
                id=sub_id, identifier=sub["identifier"],
                title=sub["title"],
                phase=PHASE_IMPLEMENTING, status="queued",
                repo_path=repo_path, base_branch=base_branch or None,
                parent_id=parent_id, parent_identifier=parent_identifier,
            ))

    def _dispatch(self, issue: Issue):
        with self._running_lock:
            if issue.id in self.running:
                return
        if not self._semaphore.acquire(blocking=False):
            log.debug("Max concurrent reached, skipping %s", issue.identifier)
            return

        self.db.upsert_issue(issue)
        future = self._executor.submit(self._execute, issue)
        with self._running_lock:
            self.running[issue.id] = future

        def _done(f, iid=issue.id):
            self._semaphore.release()
            with self._running_lock:
                self.running.pop(iid, None)

        future.add_done_callback(_done)

    def _execute(self, issue: Issue):
        log.info("[%s] Starting phase=%s", issue.identifier, issue.phase)
        self.db.update_status(issue.id, "running", phase=issue.phase)
        self.db.log_event(issue.id, "phase_start", {"phase": issue.phase})

        if issue.phase == PHASE_IMPLEMENTING and issue.parent_id:
            from loki2.core.state import STATE_IN_PROGRESS
            self.linear.update_issue_state(issue.id, STATE_IN_PROGRESS)

        try:
            result = self._run_phase(issue)

            if issue.phase == PHASE_PR:
                from loki2.core.state import STATE_IN_REVIEW
                self.linear.update_issue_state(issue.id, STATE_IN_REVIEW)
            elif issue.phase == PHASE_IMPLEMENTING:
                self.linear.update_issue_state(issue.id, STATE_DONE)
                if result.comment:
                    self.linear.create_comment(issue.id, result.comment)
            elif issue.phase == PHASE_SUBISSUE_CREATION:
                # subissue_creation runs while Linear state is Implementing;
                # after completion, state stays Implementing (orchestrator will
                # dispatch sub-issues on next poll)
                self.linear.update_issue_state(issue.id, STATE_IMPLEMENTING)
                if result.comment:
                    self.linear.create_comment(issue.id, result.comment)
            else:
                new_state = next_state(
                    self._phase_to_linear_state(issue.phase), result.event)
                log.info("[%s] Phase complete: event=%s -> state=%s",
                         issue.identifier, result.event, new_state)
                self.linear.update_issue_state(issue.id, new_state)
                if result.comment:
                    self.linear.create_comment(issue.id, result.comment)

            self.db.update_status(issue.id, "done", phase=issue.phase)
            self.db.log_event(issue.id, "phase_complete",
                              {"phase": issue.phase, "event": result.event})

        except Exception as e:
            log.exception("[%s] Phase failed: %s", issue.identifier, e)

            db_issue = self.db.get_issue(issue.id)
            retry_count = (db_issue.retry_count if db_issue else 0) + 1

            if retry_count <= self.settings.max_retries:
                log.info("[%s] Scheduling retry %d/%d",
                         issue.identifier, retry_count, self.settings.max_retries)
                self.db.update_status(
                    issue.id, "queued", error=str(e), retry_count=retry_count)
                self.db.log_event(issue.id, "phase_retry",
                                  {"phase": issue.phase, "error": str(e), "retry": retry_count})
                try:
                    self.linear.create_comment(
                        issue.id,
                        f"Phase `{issue.phase}` failed (retry {retry_count}/{self.settings.max_retries}): {e}")
                except Exception:
                    pass
            else:
                self.db.update_status(issue.id, "failed", error=str(e))
                self.db.log_event(issue.id, "phase_error",
                                  {"phase": issue.phase, "error": str(e)})
                try:
                    self.linear.update_issue_state(issue.id, STATE_FAILED)
                    self.linear.create_comment(
                        issue.id, f"Phase `{issue.phase}` failed (max retries reached): {e}")
                except Exception:
                    log.exception("[%s] Failed to update Linear on error", issue.identifier)

        finally:
            if issue.repo_path:
                try:
                    self.workspace.destroy(issue.repo_path, issue.identifier)
                except Exception:
                    pass

    def _run_phase(self, issue: Issue) -> PhaseResult:
        phase_map = {
            PHASE_PLANNING: self._run_generic_phase,
            PHASE_PLAN_REVIEW: self._run_generic_phase,
            PHASE_SUBISSUE_CREATION: self._run_subissue_creation,
            PHASE_IMPLEMENTING: self._run_implementing,
            PHASE_REVIEW: self._run_review,
            PHASE_PR: self._run_pr,
        }
        handler = phase_map.get(issue.phase)
        if not handler:
            raise NotImplementedError(f"Phase {issue.phase} not implemented")
        return handler(issue)

    def _run_generic_phase(self, issue: Issue) -> PhaseResult:
        module = {
            PHASE_PLANNING: planning,
            PHASE_PLAN_REVIEW: plan_review,
        }[issue.phase]
        prompt = module.prepare_prompt(issue, self.settings, self.linear, self.prompt_builder)
        work_dir = module.setup_workspace(issue, self.settings, self.workspace)
        claude_result = self._invoke_claude(issue, work_dir, prompt)
        return module.post_execute(issue, claude_result)

    def _run_subissue_creation(self, issue: Issue) -> PhaseResult:
        prompt = subissue_creation.prepare_prompt(
            issue, self.settings, self.linear, self.prompt_builder)
        work_dir = subissue_creation.setup_workspace(issue, self.settings, self.workspace)
        claude_result = self._invoke_claude(issue, work_dir, prompt)
        return subissue_creation.post_execute(issue, claude_result, self.linear)

    def _get_merge_lock(self, parent_identifier: str) -> threading.Lock:
        with self._merge_locks_guard:
            if parent_identifier not in self._merge_locks:
                self._merge_locks[parent_identifier] = threading.Lock()
            return self._merge_locks[parent_identifier]

    def _run_implementing(self, issue: Issue) -> PhaseResult:
        prompt = implementing.prepare_prompt(
            issue, self.settings, self.linear, self.prompt_builder)
        work_dir = implementing.setup_workspace(issue, self.settings, self.workspace)
        claude_result = self._invoke_claude(issue, work_dir, prompt)
        merge_lock = self._get_merge_lock(issue.parent_identifier) if issue.parent_identifier else None
        return implementing.post_execute(
            issue, claude_result, self.linear, self.workspace, merge_lock)

    def _run_review(self, issue: Issue) -> PhaseResult:
        prompt = review.prepare_prompt(issue, self.settings, self.linear, self.prompt_builder)
        work_dir = review.setup_workspace(issue, self.settings, self.workspace)
        claude_result = self._invoke_claude(issue, work_dir, prompt)
        return review.post_execute(issue, claude_result, self.linear, self.workspace)

    def _run_pr(self, issue: Issue) -> PhaseResult:
        return pr_phase.create_pr(
            issue, self.settings, self.linear, self.workspace, self.prompt_builder)

    def _invoke_claude(self, issue: Issue, work_dir: Path, prompt: str) -> dict:
        setup_settings(work_dir, phase=issue.phase,
                       log_dir=self.settings.log_dir)

        pc = self.settings.phase_config(issue.phase)
        model = self.settings.model_for_phase(issue.phase)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        log_file = self.settings.log_dir / f"{issue.identifier}-{timestamp}.log"

        exec_id = self.db.start_execution(
            issue.id, issue.phase, model=model, log_file=str(log_file))

        start = time.monotonic()

        claude_result = run_claude(
            prompt, work_dir,
            model=model,
            max_turns=str(pc.max_turns),
            budget=str(pc.budget),
            log_file=log_file,
            timeout=pc.timeout,
            idle_timeout=pc.idle_timeout,
        )

        elapsed = time.monotonic() - start
        cost = claude_result.get("total_cost_usd", 0)
        turns = claude_result.get("num_turns", 0)

        if claude_result.get("returncode", -1) != 0:
            error = claude_result.get("error", "Claude exited with non-zero")
            self.db.finish_execution(
                exec_id, status="failed", duration_s=elapsed,
                cost_usd=cost, turns=turns, error=error)
            raise RuntimeError(error)

        self.db.finish_execution(
            exec_id, status="done", duration_s=elapsed,
            cost_usd=cost, turns=turns)

        return claude_result

    def _phase_to_linear_state(self, phase: str) -> str:
        reverse = {v: k for k, v in STATE_TO_PHASE.items()}
        return reverse.get(phase, STATE_PLANNING)
