from unittest.mock import patch

from lib.linear import update_issue_labels, UPDATE_ISSUE_LABELS_MUTATION


@patch("lib.linear.graphql")
def test_update_issue_labels(mock_gql):
    update_issue_labels("issue-1", ["label-1", "label-2"], env={"LINEAR_OAUTH_TOKEN": "key", "LINEAR_TEAM_ID": "team-1"})
    mock_gql.assert_called_once_with(
        "key", UPDATE_ISSUE_LABELS_MUTATION,
        {"issueId": "issue-1", "labelIds": ["label-1", "label-2"]},
    )
