import hashlib
import hmac
import logging
import os
import re
import signal
import threading
from pathlib import Path

from flask import Flask, request, jsonify

from config import FORGE_ROOT, load_env, load_repos, resolve_repo, get_api_key
from config.constants import (STATE_PLANNING, STATE_IMPLEMENTING,
                        STATE_PLAN_CHANGES_REQUESTED, STATE_CHANGES_REQUESTED,
                        PHASE_PLANNING, PHASE_IMPLEMENTING,
                        PHASE_REVIEW, PHASE_PLAN_REVIEW)
from lib.linear import emit_thought, emit_response, emit_error, fetch_issue_detail, fetch_issue_state, update_issue_state
from forge.queue import enqueue, wake

app = Flask(__name__)

STATE_TO_PHASE = {
    STATE_PLANNING: PHASE_PLANNING,
    STATE_IMPLEMENTING: PHASE_IMPLEMENTING,
    STATE_PLAN_CHANGES_REQUESTED: PHASE_PLAN_REVIEW,
    STATE_CHANGES_REQUESTED: PHASE_REVIEW,
}


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_issue_from_context(prompt_context: str) -> tuple[str, str]:
    identifier_match = re.search(r'<identifier>(.*?)</identifier>', prompt_context)
    id_match = re.search(r'<id>(.*?)</id>', prompt_context)
    identifier = identifier_match.group(1) if identifier_match else ""
    issue_id = id_match.group(1) if id_match else ""
    return identifier, issue_id


def _handle_created(payload: dict, env: dict):
    session_id = payload.get("agentSession", {}).get("id", "")
    prompt_context = payload.get("agentSession", {}).get("promptContext", "")
    api_key = get_api_key(env)

    identifier, issue_id = _extract_issue_from_context(prompt_context)

    if not issue_id:
        emit_thought(session_id, "Could not extract issue ID from context.", api_key)
        return

    issue_detail = fetch_issue_detail(issue_id, env)
    if not identifier:
        identifier = issue_detail.get("identifier", issue_id)

    emit_thought(session_id, f"Queuing work on {identifier}...", api_key)

    state_name = fetch_issue_state(issue_id, env)
    phase = STATE_TO_PHASE.get(state_name, PHASE_PLANNING)

    queue_dir = env["FORGE_QUEUE_DIR"]
    pid_file = env["FORGE_PID_FILE"]

    enqueue(queue_dir, issue_id, session_id, phase)
    wake(pid_file)


def _handle_prompted(payload: dict, env: dict):
    session_id = payload.get("agentSession", {}).get("id", "")
    api_key = get_api_key(env)
    body = payload.get("agentActivity", {}).get("body", "")
    emit_thought(session_id, f"Received: {body}", api_key)


def _handle_stop(payload: dict, env: dict):
    session_id = payload.get("agentSession", {}).get("id", "")
    api_key = get_api_key(env)

    lock_dir = Path(env["FORGE_LOCK_DIR"])
    for lock_file in lock_dir.glob("*.lock"):
        try:
            lines = lock_file.read_text().splitlines()
            if len(lines) >= 3 and lines[2] == session_id:
                pid = int(lines[1])
                cmdline_path = Path(f"/proc/{pid}/cmdline")
                if cmdline_path.exists():
                    cmdline = cmdline_path.read_text()
                    if "forge.executor" in cmdline:
                        os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, OSError):
            continue

    emit_response(session_id, "Stopped.", api_key)


def _handle_created_issue(payload: dict, env: dict):
    data = payload.get("data", {})
    issue_id = data.get("id", "")
    state_name = data.get("state", {}).get("name", "")
    parent_id = data.get("parentId")

    if parent_id:
        return

    if not issue_id:
        return

    phase = STATE_TO_PHASE.get(state_name)
    if phase is None:
        phase = PHASE_PLANNING
        update_issue_state(issue_id, STATE_PLANNING, env)

    queue_dir = env["FORGE_QUEUE_DIR"]
    pid_file = env["FORGE_PID_FILE"]

    enqueue(queue_dir, issue_id, "", phase)
    wake(pid_file)


def _handle_status_change(payload: dict, env: dict):
    updated_from = payload.get("updatedFrom", {})
    if "stateId" not in updated_from:
        return

    data = payload.get("data", {})
    issue_id = data.get("id", "")
    state_name = data.get("state", {}).get("name", "")
    phase = STATE_TO_PHASE.get(state_name)

    if not issue_id or not phase:
        return

    queue_dir = env["FORGE_QUEUE_DIR"]
    pid_file = env["FORGE_PID_FILE"]

    enqueue(queue_dir, issue_id, "", phase)
    wake(pid_file)


def _process_event(payload: dict, env: dict):
    try:
        event_type = payload.get("type")
        if event_type == "AgentSessionEvent":
            action = payload.get("action")
            if action == "created":
                _handle_created(payload, env)
            elif action == "prompted":
                _handle_prompted(payload, env)
            elif action == "stop":
                _handle_stop(payload, env)
        elif event_type == "Issue":
            action = payload.get("action")
            if action == "update":
                _handle_status_change(payload, env)
            elif action == "create":
                _handle_created_issue(payload, env)
    except Exception as e:
        session_id = payload.get("agentSession", {}).get("id", "")
        api_key = get_api_key(env)
        logging.exception("Unhandled error in _process_event")
        if session_id:
            emit_error(session_id, f"Internal error: {e}", api_key)


@app.route("/webhook", methods=["POST"])
def webhook():
    env = app.config.get("FORGE_ENV") or {}
    secret = env.get("LINEAR_WEBHOOK_SECRET", "")
    if not secret:
        return jsonify({"error": "LINEAR_WEBHOOK_SECRET is not configured"}), 500

    signature = request.headers.get("Linear-Signature", "")
    if not _verify_signature(request.data, signature, secret):
        return jsonify({"error": "invalid signature"}), 401

    payload = request.get_json(force=True, silent=True) or {}

    thread = threading.Thread(target=_process_event, args=(payload, env), daemon=True)
    thread.start()

    return jsonify({"ok": True}), 200


def serve():
    env = load_env()
    app.config["FORGE_ENV"] = env

    host = env.get("WEBHOOK_HOST", "0.0.0.0")
    port = int(env.get("WEBHOOK_PORT", "3000"))

    app.run(host=host, port=port)
