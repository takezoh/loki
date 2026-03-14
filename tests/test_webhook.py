import hashlib
import hmac as hmac_mod
import json
import os
import signal as sig_mod
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, ANY

import pytest

from agent.webhook import (
    _verify_signature, _extract_issue_from_context, _process_event,
    _handle_created, _handle_prompted, _handle_stop, _handle_created_issue,
    _handle_status_change, app, STATE_TO_PHASE,
)
from config.constants import (
    STATE_PLANNING, STATE_IMPLEMENTING,
    STATE_PLAN_CHANGES_REQUESTED, STATE_CHANGES_REQUESTED,
    PHASE_PLANNING, PHASE_IMPLEMENTING, PHASE_PLAN_REVIEW, PHASE_REVIEW,
)

SID = "sess-1"
KEY = "test-key"
ISSUE_ID = "issue-abc"


def _make_payload(action="created", session_id=SID, prompt_context="", body=""):
    p = {
        "type": "AgentSessionEvent",
        "action": action,
        "agentSession": {"id": session_id, "promptContext": prompt_context},
    }
    if body:
        p["agentActivity"] = {"body": body}
    return p


# --- _verify_signature ---

class TestVerifySignature:
    def test_valid(self):
        body = b"hello"
        secret = "mysecret"
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_signature(body, sig, secret) is True

    def test_invalid(self):
        assert _verify_signature(b"hello", "badsig", "mysecret") is False

    def test_empty_body(self):
        body = b""
        secret = "s"
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_signature(body, sig, secret) is True


# --- _extract_issue_from_context ---

class TestExtractIssue:
    def test_both(self):
        ctx = "<identifier>DEV-1</identifier><id>id1</id>"
        assert _extract_issue_from_context(ctx) == ("DEV-1", "id1")

    def test_id_only(self):
        ctx = "<id>id1</id>"
        assert _extract_issue_from_context(ctx) == ("", "id1")

    def test_identifier_only(self):
        ctx = "<identifier>DEV-1</identifier>"
        assert _extract_issue_from_context(ctx) == ("DEV-1", "")

    def test_empty(self):
        assert _extract_issue_from_context("") == ("", "")


# --- _process_event routing ---

class TestProcessEvent:
    def test_non_agent_event_ignored(self):
        with patch("agent.webhook._handle_created") as hc:
            _process_event({"type": "Other", "action": "created"}, {})
            hc.assert_not_called()

    @pytest.mark.parametrize("action,handler", [
        ("created", "_handle_created"),
        ("prompted", "_handle_prompted"),
        ("stop", "_handle_stop"),
    ])
    def test_routes(self, action, handler):
        payload = _make_payload(action=action)
        with patch(f"agent.webhook.{handler}") as h:
            _process_event(payload, {"k": "v"})
            h.assert_called_once_with(payload, {"k": "v"})

    def test_unknown_action_no_error(self):
        _process_event(_make_payload(action="unknown"), {})

    @pytest.mark.parametrize("action,handler", [
        ("create", "_handle_created_issue"),
        ("update", "_handle_status_change"),
    ])
    def test_issue_routes(self, action, handler):
        payload = {"type": "Issue", "action": action, "data": {"id": ISSUE_ID}}
        with patch(f"agent.webhook.{handler}") as h:
            _process_event(payload, {"k": "v"})
            h.assert_called_once_with(payload, {"k": "v"})

    @patch("agent.webhook.emit_error")
    @patch("agent.webhook._handle_created", side_effect=RuntimeError("boom"))
    def test_handler_exception_emits_error(self, _, mock_err):
        _process_event(_make_payload(action="created"), {"LINEAR_OAUTH_TOKEN": KEY})
        mock_err.assert_called_once()
        assert "boom" in mock_err.call_args[0][1]


# --- _handle_created ---

class TestHandleCreated:
    def _env(self, tmp_path):
        return {
            "LINEAR_OAUTH_TOKEN": KEY,
            "FORGE_QUEUE_DIR": str(tmp_path / "queue"),
            "FORGE_PID_FILE": str(tmp_path / "forge.pid"),
            "FORGE_LOCK_DIR": str(tmp_path / "locks"),
        }

    def _ctx(self, identifier="DEV-1", issue_id=ISSUE_ID):
        parts = []
        if identifier:
            parts.append(f"<identifier>{identifier}</identifier>")
        if issue_id:
            parts.append(f"<id>{issue_id}</id>")
        return "".join(parts)

    @patch("agent.webhook.emit_thought")
    @patch("agent.webhook.fetch_issue_state", return_value=STATE_PLANNING)
    @patch("agent.webhook.fetch_issue_detail", return_value={"identifier": "DEV-1"})
    @patch("agent.webhook.enqueue")
    @patch("agent.webhook.wake")
    def test_no_issue_id(self, mock_wake, mock_enqueue, mock_detail, mock_state, mock_thought, tmp_path):
        env = self._env(tmp_path)
        payload = _make_payload(prompt_context="<identifier>DEV-1</identifier>")
        _handle_created(payload, env)
        mock_thought.assert_called_once()
        assert "issue ID" in mock_thought.call_args[0][1]
        mock_enqueue.assert_not_called()

    @patch("agent.webhook.emit_thought")
    @patch("agent.webhook.fetch_issue_state", return_value=STATE_PLANNING)
    @patch("agent.webhook.fetch_issue_detail", return_value={"identifier": "DEV-1"})
    @patch("agent.webhook.enqueue")
    @patch("agent.webhook.wake")
    def test_normal_path(self, mock_wake, mock_enqueue, mock_detail, mock_state, mock_thought, tmp_path):
        env = self._env(tmp_path)
        payload = _make_payload(prompt_context=self._ctx())
        _handle_created(payload, env)
        mock_enqueue.assert_called_once_with(env["FORGE_QUEUE_DIR"], ISSUE_ID, SID, PHASE_PLANNING)
        mock_wake.assert_called_once_with(env["FORGE_PID_FILE"])


# --- _handle_created_issue ---

class TestHandleCreatedIssue:
    def _env(self, tmp_path):
        return {
            "LINEAR_OAUTH_TOKEN": KEY,
            "FORGE_QUEUE_DIR": str(tmp_path / "queue"),
            "FORGE_PID_FILE": str(tmp_path / "forge.pid"),
        }

    def _payload(self, issue_id=ISSUE_ID, state_name="Todo", parent_id=None):
        data = {"id": issue_id, "state": {"name": state_name}}
        if parent_id is not None:
            data["parentId"] = parent_id
        return {"type": "Issue", "action": "create", "data": data}

    @patch("agent.webhook.enqueue")
    @patch("agent.webhook.wake")
    @patch("agent.webhook.update_issue_state")
    def test_sub_issue_skipped(self, mock_update, mock_wake, mock_enqueue, tmp_path):
        env = self._env(tmp_path)
        payload = self._payload(parent_id="parent-id-123")
        _handle_created_issue(payload, env)
        mock_enqueue.assert_not_called()
        mock_wake.assert_not_called()
        mock_update.assert_not_called()

    @patch("agent.webhook.enqueue")
    @patch("agent.webhook.wake")
    @patch("agent.webhook.update_issue_state")
    def test_empty_issue_id_skipped(self, mock_update, mock_wake, mock_enqueue, tmp_path):
        env = self._env(tmp_path)
        payload = self._payload(issue_id="")
        _handle_created_issue(payload, env)
        mock_enqueue.assert_not_called()
        mock_wake.assert_not_called()
        mock_update.assert_not_called()

    @patch("agent.webhook.enqueue")
    @patch("agent.webhook.wake")
    @patch("agent.webhook.update_issue_state")
    def test_known_state_no_update(self, mock_update, mock_wake, mock_enqueue, tmp_path):
        env = self._env(tmp_path)
        payload = self._payload(state_name=STATE_IMPLEMENTING)
        _handle_created_issue(payload, env)
        mock_update.assert_not_called()
        mock_enqueue.assert_called_once_with(env["FORGE_QUEUE_DIR"], ISSUE_ID, "", PHASE_IMPLEMENTING)
        mock_wake.assert_called_once_with(env["FORGE_PID_FILE"])

    @patch("agent.webhook.enqueue")
    @patch("agent.webhook.wake")
    @patch("agent.webhook.update_issue_state")
    def test_unknown_state_uses_planning(self, mock_update, mock_wake, mock_enqueue, tmp_path):
        env = self._env(tmp_path)
        payload = self._payload(state_name="Todo")
        _handle_created_issue(payload, env)
        mock_update.assert_called_once_with(ISSUE_ID, STATE_PLANNING, env)
        mock_enqueue.assert_called_once_with(env["FORGE_QUEUE_DIR"], ISSUE_ID, "", PHASE_PLANNING)
        mock_wake.assert_called_once_with(env["FORGE_PID_FILE"])

    @patch("agent.webhook.enqueue")
    @patch("agent.webhook.wake")
    @patch("agent.webhook.update_issue_state")
    def test_enqueue_and_wake_args(self, mock_update, mock_wake, mock_enqueue, tmp_path):
        env = self._env(tmp_path)
        payload = self._payload(state_name=STATE_PLANNING)
        _handle_created_issue(payload, env)
        mock_enqueue.assert_called_once_with(env["FORGE_QUEUE_DIR"], ISSUE_ID, "", PHASE_PLANNING)
        mock_wake.assert_called_once_with(env["FORGE_PID_FILE"])


class TestStateToPhase:
    @pytest.mark.parametrize("state,phase", [
        (STATE_PLANNING, PHASE_PLANNING),
        (STATE_IMPLEMENTING, PHASE_IMPLEMENTING),
        (STATE_PLAN_CHANGES_REQUESTED, PHASE_PLAN_REVIEW),
        (STATE_CHANGES_REQUESTED, PHASE_REVIEW),
    ])
    def test_mapping(self, state, phase):
        assert STATE_TO_PHASE[state] == phase

    def test_unknown_state_default(self):
        assert STATE_TO_PHASE.get("Unknown") is None


# --- _handle_prompted ---

class TestHandlePrompted:
    @patch("agent.webhook.emit_thought")
    def test_emits_thought_with_body(self, mock_thought):
        payload = _make_payload(action="prompted", body="user msg")
        _handle_prompted(payload, {"LINEAR_OAUTH_TOKEN": KEY})
        mock_thought.assert_called_once()
        assert "user msg" in mock_thought.call_args[0][1]


# --- _handle_stop ---

class TestHandleStop:
    @patch("agent.webhook.emit_response")
    def test_no_process(self, mock_resp, tmp_path):
        env = {"LINEAR_OAUTH_TOKEN": KEY, "FORGE_LOCK_DIR": str(tmp_path / "locks")}
        _handle_stop(_make_payload(action="stop"), env)
        mock_resp.assert_called_once()


# --- Flask endpoint ---

class TestWebhookEndpoint:
    @pytest.fixture
    def client(self):
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c

    def test_no_secret_returns_500(self, client):
        app.config["FORGE_ENV"] = {}
        resp = client.post("/webhook", json={"type": "test"})
        assert resp.status_code == 500

    def test_invalid_signature_401(self, client):
        secret = "webhook-secret"
        app.config["FORGE_ENV"] = {"LINEAR_WEBHOOK_SECRET": secret}
        resp = client.post("/webhook", data=b'{"a":1}',
                           headers={"Linear-Signature": "bad"},
                           content_type="application/json")
        assert resp.status_code == 401

    @patch("agent.webhook._process_event")
    def test_valid_signature_200(self, _, client):
        secret = "webhook-secret"
        body = b'{"type":"AgentSessionEvent"}'
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        app.config["FORGE_ENV"] = {"LINEAR_WEBHOOK_SECRET": secret}
        resp = client.post("/webhook", data=body,
                           headers={"Linear-Signature": sig},
                           content_type="application/json")
        assert resp.status_code == 200
