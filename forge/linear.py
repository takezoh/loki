import json
import sys
import urllib.request

from .config import load_env, get_api_key, parse_labels
from .constants import TERMINAL_STATES, STATE_DONE, STATE_TODO


def graphql(api_key: str, query: str, variables: dict = None) -> dict:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=payload,
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

TEAM_QUERY = """
query($teamName: String!) {
  teams(filter: { name: { eq: $teamName } }) {
    nodes { id name }
  }
}
"""


def resolve_team_id(team_name: str, api_key: str) -> str:
    data = graphql(api_key, TEAM_QUERY, {"teamName": team_name})
    nodes = data.get("data", {}).get("teams", {}).get("nodes", [])
    if not nodes:
        print(f"Team '{team_name}' not found", file=sys.stderr)
        sys.exit(1)
    return nodes[0]["id"]


ISSUES_QUERY = """
query($teamId: ID!, $stateName: String!) {
  issues(filter: {
    team: { id: { eq: $teamId } }
    state: { name: { eq: $stateName } }
  }) {
    nodes {
      id
      identifier
      title
      labels {
        nodes {
          name
          parent {
            name
          }
        }
      }
    }
  }
}
"""

def poll(status: str, env=None) -> list[dict]:
    if env is None:
        env = load_env()
    api_key = get_api_key(env)
    if not api_key:
        print("LINEAR_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    team_id = env["LINEAR_TEAM_ID"]
    data = graphql(api_key, ISSUES_QUERY, {"teamId": team_id, "stateName": status})

    issues = []
    for node in data.get("data", {}).get("issues", {}).get("nodes", []):
        labels = parse_labels(node.get("labels", {}).get("nodes", []))
        issues.append({
            "id": node["id"],
            "identifier": node["identifier"],
            "title": node["title"],
            "labels": labels,
        })
    return issues

SUB_ISSUES_QUERY = """
query($parentId: String!) {
  issue(id: $parentId) {
    children {
      nodes {
        id
        identifier
        title
        description
        state { name }
        labels {
          nodes {
            name
            parent { name }
          }
        }
        relations {
          nodes {
            type
            relatedIssue { id }
          }
        }
        inverseRelations {
          nodes {
            type
            issue {
              id
              state { name }
            }
          }
        }
      }
    }
    documents {
      nodes {
        id
        title
        content
      }
    }
  }
}
"""


UPDATE_STATE_MUTATION = """
mutation($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: { stateId: $stateId }) {
    issue { id state { name } }
  }
}
"""

WORKFLOW_STATES_QUERY = """
query($teamId: ID!) {
  workflowStates(filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name }
  }
}
"""


CREATE_COMMENT_MUTATION = """
mutation($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    comment { id }
  }
}
"""


def create_comment(issue_id: str, body: str, env=None):
    if env is None:
        env = load_env()
    api_key = get_api_key(env)
    graphql(api_key, CREATE_COMMENT_MUTATION, {"issueId": issue_id, "body": body})


def update_issue_state(issue_id: str, state_name: str, env=None):
    if env is None:
        env = load_env()
    api_key = get_api_key(env)
    team_id = env["LINEAR_TEAM_ID"]

    data = graphql(api_key, WORKFLOW_STATES_QUERY, {"teamId": team_id})
    states = data.get("data", {}).get("workflowStates", {}).get("nodes", [])
    state_id = next((s["id"] for s in states if s["name"] == state_name), None)
    if not state_id:
        print(f"State '{state_name}' not found", file=sys.stderr)
        return

    graphql(api_key, UPDATE_STATE_MUTATION, {"issueId": issue_id, "stateId": state_id})


def is_ready(node: dict) -> bool:
    state_name = node.get("state", {}).get("name", "")
    if state_name in TERMINAL_STATES:
        return False
    for rel in node.get("inverseRelations", {}).get("nodes", []):
        if rel["type"] == "blocks":
            blocker_state = rel.get("issue", {}).get("state", {}).get("name", "")
            if blocker_state != STATE_DONE:
                return False
    return True


def detect_dependency_cycle(nodes: list[dict]) -> list[str] | None:
    id_set = {n["id"] for n in nodes}
    id_to_ident = {n["id"]: n["identifier"] for n in nodes}

    graph: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for node in nodes:
        for rel in node.get("relations", {}).get("nodes", []):
            if rel["type"] == "blocks":
                target = rel["relatedIssue"]["id"]
                if target in id_set:
                    graph[node["id"]].append(target)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in id_set}
    parent: dict[str, str | None] = {}

    def dfs(u: str) -> list[str] | None:
        color[u] = GRAY
        for v in graph[u]:
            if color[v] == GRAY:
                cycle = [id_to_ident[v], id_to_ident[u]]
                cur = u
                while cur != v:
                    cur = parent.get(cur)
                    if cur is None:
                        break
                    cycle.append(id_to_ident[cur])
                cycle.reverse()
                return cycle
            if color[v] == WHITE:
                parent[v] = u
                result = dfs(v)
                if result:
                    return result
        color[u] = BLACK
        return None

    for nid in id_set:
        if color[nid] == WHITE:
            result = dfs(nid)
            if result:
                return result
    return None


def fetch_sub_issues(parent_id: str, env=None) -> dict:
    if env is None:
        env = load_env()
    api_key = get_api_key(env)
    if not api_key:
        print("LINEAR_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    data = graphql(api_key, SUB_ISSUES_QUERY, {"parentId": parent_id})
    issue = data.get("data", {}).get("issue", {})
    nodes = issue.get("children", {}).get("nodes", [])

    sub_issues = []
    for node in nodes:
        state_name = node.get("state", {}).get("name", "")
        labels = parse_labels(node.get("labels", {}).get("nodes", []))

        sub_issues.append({
            "id": node["id"],
            "identifier": node["identifier"],
            "title": node["title"],
            "description": node.get("description", ""),
            "state": state_name,
            "labels": labels,
            "ready": is_ready(node),
        })

    documents = []
    for doc in issue.get("documents", {}).get("nodes", []):
        documents.append({
            "id": doc["id"],
            "title": doc["title"],
            "content": doc["content"],
        })

    return {"sub_issues": sub_issues, "documents": documents, "cycle": detect_dependency_cycle(nodes)}


ISSUE_DETAIL_QUERY = """
query($issueId: String!) {
  issue(id: $issueId) {
    id
    identifier
    title
    description
    labels {
      nodes {
        name
        parent { name }
      }
    }
  }
}
"""

ISSUE_STATE_QUERY = """
query($issueId: String!) {
  issue(id: $issueId) {
    state { name }
  }
}
"""


def fetch_issue_state(issue_id: str, env=None) -> str:
    if env is None:
        env = load_env()
    api_key = get_api_key(env)
    data = graphql(api_key, ISSUE_STATE_QUERY, {"issueId": issue_id})
    return data.get("data", {}).get("issue", {}).get("state", {}).get("name", "")


ISSUE_COMMENTS_QUERY = """
query($issueId: String!) {
  issue(id: $issueId) {
    comments {
      nodes {
        body
        user { name }
        createdAt
      }
    }
  }
}
"""


def fetch_issue_detail(issue_id: str, env=None) -> dict:
    if env is None:
        env = load_env()
    api_key = get_api_key(env)
    data = graphql(api_key, ISSUE_DETAIL_QUERY, {"issueId": issue_id})
    issue = data.get("data", {}).get("issue", {})
    labels = parse_labels(issue.get("labels", {}).get("nodes", []))
    return {
        "id": issue.get("id", ""),
        "identifier": issue.get("identifier", ""),
        "title": issue.get("title", ""),
        "description": issue.get("description", ""),
        "labels": labels,
    }


def fetch_issue_comments(issue_id: str, env=None) -> list[dict]:
    if env is None:
        env = load_env()
    api_key = get_api_key(env)
    data = graphql(api_key, ISSUE_COMMENTS_QUERY, {"issueId": issue_id})
    comments = data.get("data", {}).get("issue", {}).get("comments", {}).get("nodes", [])
    return [{"body": c["body"], "user": c.get("user", {}).get("name", ""), "createdAt": c["createdAt"]} for c in comments]


UPLOAD_FILE_MUTATION = """
mutation($contentType: String!, $filename: String!, $size: Int!) {
  fileUpload(contentType: $contentType, filename: $filename, size: $size) {
    uploadFile { uploadUrl assetUrl }
  }
}
"""

ATTACHMENT_CREATE_MUTATION = """
mutation($issueId: String!, $title: String!, $url: String!) {
  attachmentCreate(input: { issueId: $issueId, title: $title, url: $url }) {
    attachment { id }
  }
}
"""


def create_attachment(issue_id: str, title: str, content: bytes, filename: str,
                      content_type: str = "application/json", env=None):
    if env is None:
        env = load_env()
    api_key = get_api_key(env)
    data = graphql(api_key, UPLOAD_FILE_MUTATION, {
        "contentType": content_type,
        "filename": filename,
        "size": len(content),
    })
    upload = data["data"]["fileUpload"]["uploadFile"]

    req = urllib.request.Request(upload["uploadUrl"], data=content, method="PUT")
    req.add_header("Content-Type", content_type)
    urllib.request.urlopen(req)

    graphql(api_key, ATTACHMENT_CREATE_MUTATION, {
        "issueId": issue_id,
        "title": title,
        "url": upload["assetUrl"],
    })


def fetch_todo_state_id(team_id: str = "", env=None) -> str:
    if env is None:
        env = load_env()
    api_key = get_api_key(env)
    if not team_id:
        team_id = env["LINEAR_TEAM_ID"]
    data = graphql(api_key, WORKFLOW_STATES_QUERY, {"teamId": team_id})
    states = data.get("data", {}).get("workflowStates", {}).get("nodes", [])
    return next((s["id"] for s in states if s["name"] == STATE_TODO), "")
