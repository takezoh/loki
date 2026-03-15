from pathlib import Path
from unittest.mock import patch, call, MagicMock

import pytest

from forge.executor import post_execute
from config.constants import (
    PHASE_SUBISSUE_CREATION,
    STATE_TODO, STATE_IMPLEMENTING,
)

ISSUE_ID = "issue-parent"
ISSUE_IDENTIFIER = "DEV-10"


def _call(issue_id=ISSUE_ID, log_file=None, tmp_path=None, **kwargs):
    defaults = dict(
        phase=PHASE_SUBISSUE_CREATION,
        issue_id=issue_id,
        issue_identifier=ISSUE_IDENTIFIER,
        parent_issue_id=None,
        parent_identifier=None,
        repo=MagicMock(name="myrepo"),
        worktree_base=tmp_path or Path("/tmp"),
        lock_dir=tmp_path or Path("/tmp"),
        log_file=log_file or Path("/dev/null"),
    )
    defaults.update(kwargs)
    return post_execute(**defaults)


class TestPostExecuteSubissueCreationLabelPropagation:
    def _sub(self, sub_id, state=STATE_TODO):
        return {"id": sub_id, "state": state}

    @patch("forge.executor.update_issue_state")
    @patch("forge.executor.create_comment")
    @patch("forge.executor.parse_claude_result", return_value=(None, None))
    @patch("forge.executor.update_issue_labels")
    @patch("forge.executor.fetch_issue_detail")
    @patch("forge.executor.fetch_sub_issues")
    def test_repo_labels_applied_to_each_sub_issue(
        self, mock_fetch_subs, mock_fetch_detail, mock_update_labels,
        mock_parse, mock_comment, mock_update_state, tmp_path
    ):
        mock_fetch_subs.return_value = {
            "sub_issues": [self._sub("sub-1"), self._sub("sub-2")]
        }
        mock_fetch_detail.return_value = {
            "label_nodes": [
                {"id": "lbl-repo", "name": "repo:myrepo"},
                {"id": "lbl-other", "name": "priority:high"},
            ]
        }

        _call(tmp_path=tmp_path)

        mock_update_labels.assert_has_calls([
            call("sub-1", ["lbl-repo"]),
            call("sub-2", ["lbl-repo"]),
        ], any_order=False)

    @patch("forge.executor.update_issue_state")
    @patch("forge.executor.create_comment")
    @patch("forge.executor.parse_claude_result", return_value=(None, None))
    @patch("forge.executor.update_issue_labels")
    @patch("forge.executor.fetch_issue_detail")
    @patch("forge.executor.fetch_sub_issues")
    def test_no_repo_labels_skips_update_labels(
        self, mock_fetch_subs, mock_fetch_detail, mock_update_labels,
        mock_parse, mock_comment, mock_update_state, tmp_path
    ):
        mock_fetch_subs.return_value = {
            "sub_issues": [self._sub("sub-1")]
        }
        mock_fetch_detail.return_value = {
            "label_nodes": [
                {"id": "lbl-other", "name": "priority:high"},
            ]
        }

        _call(tmp_path=tmp_path)

        mock_update_labels.assert_not_called()

    @patch("forge.executor.update_issue_state")
    @patch("forge.executor.create_comment")
    @patch("forge.executor.parse_claude_result", return_value=(None, None))
    @patch("forge.executor.update_issue_labels")
    @patch("forge.executor.fetch_issue_detail")
    @patch("forge.executor.fetch_sub_issues")
    def test_empty_label_nodes_skips_update_labels(
        self, mock_fetch_subs, mock_fetch_detail, mock_update_labels,
        mock_parse, mock_comment, mock_update_state, tmp_path
    ):
        mock_fetch_subs.return_value = {
            "sub_issues": [self._sub("sub-1")]
        }
        mock_fetch_detail.return_value = {"label_nodes": []}

        _call(tmp_path=tmp_path)

        mock_update_labels.assert_not_called()

    @patch("forge.executor.update_issue_state")
    @patch("forge.executor.create_comment")
    @patch("forge.executor.parse_claude_result", return_value=(None, None))
    @patch("forge.executor.update_issue_labels")
    @patch("forge.executor.fetch_issue_detail")
    @patch("forge.executor.fetch_sub_issues")
    def test_multiple_repo_labels_all_passed(
        self, mock_fetch_subs, mock_fetch_detail, mock_update_labels,
        mock_parse, mock_comment, mock_update_state, tmp_path
    ):
        mock_fetch_subs.return_value = {
            "sub_issues": [self._sub("sub-1")]
        }
        mock_fetch_detail.return_value = {
            "label_nodes": [
                {"id": "lbl-r1", "name": "repo:alpha"},
                {"id": "lbl-r2", "name": "repo:beta"},
            ]
        }

        _call(tmp_path=tmp_path)

        mock_update_labels.assert_called_once_with("sub-1", ["lbl-r1", "lbl-r2"])

    @patch("forge.executor.sys.exit")
    @patch("forge.executor.mark_failed")
    @patch("forge.executor.update_issue_labels")
    @patch("forge.executor.fetch_issue_detail")
    @patch("forge.executor.fetch_sub_issues")
    def test_no_sub_issues_calls_mark_failed(
        self, mock_fetch_subs, mock_fetch_detail, mock_update_labels,
        mock_mark_failed, mock_exit, tmp_path
    ):
        mock_fetch_subs.return_value = {"sub_issues": []}
        mock_exit.side_effect = SystemExit(1)

        with pytest.raises(SystemExit):
            _call(tmp_path=tmp_path)

        mock_mark_failed.assert_called_once()
        mock_update_labels.assert_not_called()
        mock_fetch_detail.assert_not_called()
