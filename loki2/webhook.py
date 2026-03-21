from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
from typing import TYPE_CHECKING

from flask import Flask, request, jsonify

if TYPE_CHECKING:
    from loki2.clients.linear import LinearClient
    from loki2.config import Settings
    from loki2.loop import Scheduler

log = logging.getLogger("loki2.webhook")


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_issue_from_context(prompt_context: str) -> tuple[str, str]:
    identifier_match = re.search(r'<identifier>(.*?)</identifier>', prompt_context)
    id_match = re.search(r'<id>(.*?)</id>', prompt_context)
    identifier = identifier_match.group(1) if identifier_match else ""
    issue_id = id_match.group(1) if id_match else ""
    return identifier, issue_id


def create_app(settings: Settings, linear: LinearClient, scheduler: Scheduler) -> Flask:
    app = Flask(__name__)
    secret = settings.linear_webhook_secret.get_secret_value() if settings.linear_webhook_secret else ""

    @app.route("/webhook", methods=["POST"])
    def webhook():
        if not secret:
            return jsonify({"error": "webhook secret not configured"}), 500

        signature = request.headers.get("Linear-Signature", "")
        if not _verify_signature(request.data, signature, secret):
            return jsonify({"error": "invalid signature"}), 401

        payload = request.get_json(force=True, silent=True) or {}
        thread = threading.Thread(target=_process_event,
                                  args=(payload, settings, linear, scheduler),
                                  daemon=True)
        thread.start()
        return jsonify({"ok": True}), 200

    return app


def _process_event(payload: dict, settings: Settings,
                   linear: LinearClient, scheduler: Scheduler):
    try:
        event_type = payload.get("type")
        action = payload.get("action")
        log.info("Event: type=%s action=%s", event_type, action)

        if event_type == "AgentSessionEvent":
            if action == "created":
                _handle_agent_created(payload, settings, linear, scheduler)
            elif action == "stop":
                _handle_agent_stop(payload, scheduler)
        elif event_type == "Issue":
            if action == "update":
                _handle_status_change(payload, settings, linear, scheduler)
            elif action == "create":
                _handle_created_issue(payload, settings, linear, scheduler)
    except Exception:
        log.exception("Error processing webhook event")


def _handle_agent_created(payload: dict, settings: Settings,
                          linear: LinearClient, scheduler: Scheduler):
    session = payload.get("agentSession", {})
    session_id = session.get("id", "")
    prompt_context = session.get("promptContext", "")

    identifier, issue_id = _extract_issue_from_context(prompt_context)
    if not issue_id:
        return

    detail = linear.fetch_issue_detail(issue_id)
    if not identifier:
        identifier = detail.get("identifier", issue_id)

    from loki2.core.state import STATE_TO_PHASE, PHASE_PLANNING
    data = linear.graphql(
        "query($id: String!) { issue(id: $id) { state { name } } }",
        {"id": issue_id},
    )
    state_name = data.get("data", {}).get("issue", {}).get("state", {}).get("name", "")
    phase = STATE_TO_PHASE.get(state_name, PHASE_PLANNING)

    from loki2.clients.linear import _resolve_repo, _resolve_base_branch
    repos = {k: str(v) for k, v in settings.repos.items()}
    labels = detail.get("labels", [])
    repo_path = _resolve_repo(labels, repos)

    if repo_path:
        from loki2.store.models import Issue
        issue = Issue(
            id=issue_id, identifier=identifier,
            title=detail.get("title", ""),
            phase=phase, status="queued",
            repo_path=repo_path,
            base_branch=_resolve_base_branch(labels) or None,
            session_id=session_id,
        )
        scheduler.db.upsert_issue(issue)
        scheduler._dispatch(issue)


def _handle_agent_stop(payload: dict, scheduler: Scheduler):
    session_id = payload.get("agentSession", {}).get("id", "")
    with scheduler._running_lock:
        for issue_id, future in list(scheduler.running.items()):
            db_issue = scheduler.db.get_issue(issue_id)
            if db_issue and db_issue.session_id == session_id:
                log.info("Stopping task %s (session %s)", issue_id, session_id)
                future.cancel()
                break


def _handle_created_issue(payload: dict, settings: Settings,
                          linear: LinearClient, scheduler: Scheduler):
    data = payload.get("data", {})
    issue_id = data.get("id", "")
    identifier = data.get("identifier", "")
    state_name = data.get("state", {}).get("name", "")
    parent_id = data.get("parentId")

    if parent_id or not issue_id:
        return

    from loki2.core.state import STATE_TO_PHASE, STATE_PLANNING, PHASE_PLANNING
    phase = STATE_TO_PHASE.get(state_name)
    if phase is None:
        phase = PHASE_PLANNING
        linear.update_issue_state(issue_id, STATE_PLANNING)

    detail = linear.fetch_issue_detail(issue_id)
    from loki2.clients.linear import _resolve_repo, _resolve_base_branch
    repos = {k: str(v) for k, v in settings.repos.items()}
    labels = detail.get("labels", [])
    repo_path = _resolve_repo(labels, repos)

    if repo_path:
        from loki2.store.models import Issue
        issue = Issue(
            id=issue_id,
            identifier=identifier or detail.get("identifier", ""),
            title=detail.get("title", ""),
            phase=phase, status="queued",
            repo_path=repo_path,
            base_branch=_resolve_base_branch(labels) or None,
        )
        scheduler.db.upsert_issue(issue)
        scheduler._dispatch(issue)


def _handle_status_change(payload: dict, settings: Settings,
                          linear: LinearClient, scheduler: Scheduler):
    updated_from = payload.get("updatedFrom", {})
    if "stateId" not in updated_from:
        return

    data = payload.get("data", {})
    issue_id = data.get("id", "")
    identifier = data.get("identifier", "")
    state_name = data.get("state", {}).get("name", "")

    from loki2.core.state import STATE_TO_PHASE
    phase = STATE_TO_PHASE.get(state_name)
    if not issue_id or not phase:
        return

    detail = linear.fetch_issue_detail(issue_id)
    from loki2.clients.linear import _resolve_repo, _resolve_base_branch
    repos = {k: str(v) for k, v in settings.repos.items()}
    labels = detail.get("labels", [])
    repo_path = _resolve_repo(labels, repos)

    if repo_path:
        from loki2.store.models import Issue
        issue = Issue(
            id=issue_id,
            identifier=identifier or detail.get("identifier", ""),
            title=detail.get("title", ""),
            phase=phase, status="queued",
            repo_path=repo_path,
            base_branch=_resolve_base_branch(labels) or None,
        )
        scheduler.db.upsert_issue(issue)
        scheduler._dispatch(issue)
