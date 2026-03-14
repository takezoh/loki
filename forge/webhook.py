import hashlib
import hmac
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, request, jsonify

from .agent_api import emit_thought, emit_response, emit_error
from .config import FORGE_ROOT, load_env, load_repos, resolve_repo, get_api_key
from .constants import (STATE_PLANNING, STATE_IMPLEMENTING,
                        STATE_PLAN_CHANGES_REQUESTED, STATE_CHANGES_REQUESTED,
                        PHASE_PLANNING, PHASE_IMPLEMENTING,
                        PHASE_REVIEW, PHASE_PLAN_REVIEW)
from .linear import fetch_issue_detail, fetch_issue_state

app = Flask(__name__)

_processes: dict[str, subprocess.Popen] = {}
_processes_lock = threading.Lock()

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

    labels = issue_detail.get("labels", [])

    emit_thought(session_id, f"Starting work on {identifier}...", api_key)

    state_name = fetch_issue_state(issue_id, env)
    phase = STATE_TO_PHASE.get(state_name)
    if not phase:
        phase = PHASE_PLANNING

    repos = load_repos()
    repo_path = resolve_repo(labels, repos)
    if not repo_path:
        emit_thought(session_id, f"No repo label found for {identifier}.", api_key)
        return
    if not Path(repo_path).is_dir():
        emit_thought(session_id, f"Repo not found: {repo_path}", api_key)
        return

    lock_dir = Path(env["FORGE_LOCK_DIR"])
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / f"{issue_id}.lock"
    if lock_file.exists():
        emit_thought(session_id, f"Issue {identifier} is already being processed.", api_key)
        return

    lock_file.write_text(identifier)

    cmd = [
        sys.executable, "-m", "forge.executor",
        phase, issue_id, identifier, repo_path,
    ]

    extra_env = {**os.environ, "FORGE_SESSION_ID": session_id}

    try:
        proc = subprocess.Popen(cmd, cwd=str(FORGE_ROOT), env=extra_env)
    except Exception as e:
        lock_file.unlink(missing_ok=True)
        emit_error(session_id, f"Failed to start executor: {e}", api_key)
        return

    with _processes_lock:
        _processes[session_id] = proc

    def _wait():
        returncode = proc.wait()
        with _processes_lock:
            _processes.pop(session_id, None)
        if returncode != 0:
            lock_file.unlink(missing_ok=True)

    threading.Thread(target=_wait, daemon=True).start()


def _handle_prompted(payload: dict, env: dict):
    session_id = payload.get("agentSession", {}).get("id", "")
    api_key = get_api_key(env)
    body = payload.get("agentActivity", {}).get("body", "")
    emit_thought(session_id, f"Received: {body}", api_key)


def _handle_stop(payload: dict, env: dict):
    session_id = payload.get("agentSession", {}).get("id", "")
    api_key = get_api_key(env)

    with _processes_lock:
        proc = _processes.get(session_id)
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)

    emit_response(session_id, "Stopped.", api_key)


def _process_event(payload: dict, env: dict):
    try:
        event_type = payload.get("type")
        if event_type != "AgentSessionEvent":
            return

        action = payload.get("action")
        if action == "created":
            _handle_created(payload, env)
        elif action == "prompted":
            _handle_prompted(payload, env)
        elif action == "stop":
            _handle_stop(payload, env)
    except Exception as e:
        session_id = payload.get("agentSession", {}).get("id", "")
        api_key = get_api_key(env)
        logging.exception("Unhandled error in _process_event")
        if session_id:
            emit_error(session_id, f"Internal error: {e}", api_key)


@app.route("/webhook", methods=["POST"])
def webhook():
    secret = os.environ.get("LINEAR_WEBHOOK_SECRET", "")
    if secret:
        signature = request.headers.get("Linear-Signature", "")
        if not _verify_signature(request.data, signature, secret):
            return jsonify({"error": "invalid signature"}), 401
    else:
        logging.warning("LINEAR_WEBHOOK_SECRET is not set; skipping signature verification")

    payload = request.get_json(force=True, silent=True) or {}

    env = app.config.get("FORGE_ENV") or {}

    thread = threading.Thread(target=_process_event, args=(payload, env), daemon=True)
    thread.start()

    return jsonify({"ok": True}), 200


def serve():
    env = load_env()
    app.config["FORGE_ENV"] = env

    host = env.get("WEBHOOK_HOST", "0.0.0.0")
    port = int(env.get("WEBHOOK_PORT", "3000"))

    app.run(host=host, port=port)
