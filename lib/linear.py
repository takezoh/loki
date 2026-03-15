import sys
import time

import httpx

from config import load_env, get_api_key, parse_labels
from config.constants import TERMINAL_STATES, STATE_DONE, STATE_TODO


def graphql(api_key: str, query: str, variables: dict = None) -> dict:
    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(
                "https://api.linear.app/graphql",
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": api_key},
            )
        except httpx.HTTPError as e:
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"[graphql] connection error, retrying in {wait}s (attempt {attempt + 1}/{max_retries}): {e}", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        if resp.status_code >= 500 and attempt < max_retries:
            wait = 2 ** attempt
            print(f"[graphql] {resp.status_code} error, retrying in {wait}s (attempt {attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise httpx.HTTPStatusError("retries exhausted", request=resp.request, response=resp)

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
        print("LINEAR_OAUTH_TOKEN not set", file=sys.stderr)
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
    if not body or not body.strip():
        return
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
        print("LINEAR_OAUTH_TOKEN not set", file=sys.stderr)
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
    uploadFile { uploadUrl assetUrl headers { key value } }
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

    headers = {"Content-Type": content_type}
    for h in upload.get("headers", []):
        headers[h["key"]] = h["value"]
    httpx.put(upload["uploadUrl"], content=content, headers=headers).raise_for_status()

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


# --- Agent API (formerly agent_api.py) ---

AGENT_ACTIVITY_CREATE = """
mutation($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) {
    agentActivity { id }
  }
}
"""

AGENT_SESSION_UPDATE = """
mutation($id: String!, $input: AgentSessionUpdateInput!) {
  agentSessionUpdate(id: $id, input: $input) {
    agentSession { id }
  }
}
"""


def emit_activity(session_id: str, content: dict, api_key: str, signal: str = None,
                  signal_metadata: dict = None, ephemeral: bool = False):
    input_dict = {"agentSessionId": session_id, "content": content}
    if signal is not None:
        input_dict["signal"] = signal
    if signal_metadata is not None:
        input_dict["signalMetadata"] = signal_metadata
    if ephemeral:
        input_dict["ephemeral"] = True
    return graphql(api_key, AGENT_ACTIVITY_CREATE, {"input": input_dict})


def emit_thought(session_id: str, body: str, api_key: str):
    return emit_activity(session_id, {"type": "thought", "body": body}, api_key)


def emit_action(session_id: str, action: str, parameter: str, api_key: str, result: str = None):
    content = {"type": "action", "action": action, "parameter": parameter}
    if result is not None:
        content["result"] = result
    return emit_activity(session_id, content, api_key)


def emit_response(session_id: str, body: str, api_key: str):
    return emit_activity(session_id, {"type": "response", "body": body}, api_key)


def emit_error(session_id: str, body: str, api_key: str):
    return emit_activity(session_id, {"type": "error", "body": body}, api_key)


def emit_elicitation(session_id: str, body: str, api_key: str, signal: str = None,
                     signal_metadata: dict = None):
    return emit_activity(session_id, {"type": "elicitation", "body": body}, api_key,
                         signal=signal, signal_metadata=signal_metadata)


def update_session_plan(session_id: str, steps: list[dict], api_key: str):
    return graphql(api_key, AGENT_SESSION_UPDATE, {"id": session_id, "input": {"plan": steps}})


def update_session_external_urls(session_id: str, urls: list[dict], api_key: str):
    return graphql(api_key, AGENT_SESSION_UPDATE, {"id": session_id, "input": {"externalUrls": urls}})
