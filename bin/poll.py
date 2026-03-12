#!/usr/bin/env python3
"""Linear polling: output issues with a given status as a JSON array."""

import json
import os
import sys
import urllib.request

def load_env():
    env = {}
    conf = os.path.join(os.path.dirname(__file__), "..", "config", "forge.env")
    with open(conf) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            env[k] = v.strip('"').strip("'")
    return env

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

def poll(status: str) -> list[dict]:
    env = load_env()
    api_key = env.get("LINEAR_API_KEY") or os.environ.get("LINEAR_API_KEY", "")
    if not api_key:
        print("LINEAR_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    team_id = env["FORGE_TEAM_ID"]
    data = graphql(api_key, ISSUES_QUERY, {"teamId": team_id, "stateName": status})

    issues = []
    for node in data.get("data", {}).get("issues", {}).get("nodes", []):
        labels = []
        for label in node.get("labels", {}).get("nodes", []):
            parent = label.get("parent")
            name = label["name"]
            labels.append(f"{parent['name']}:{name}" if parent else name)
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

TERMINAL_STATES = {"In Progress", "In Review", "Done", "Cancelled", "Failed"}


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


def update_issue_state(issue_id: str, state_name: str):
    env = load_env()
    api_key = env.get("LINEAR_API_KEY") or os.environ.get("LINEAR_API_KEY", "")
    team_id = env["FORGE_TEAM_ID"]

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
            if blocker_state != "Done":
                return False
    return True


def detect_dependency_cycle(nodes: list[dict]) -> list[str] | None:
    """Return identifier list of the cycle if one exists in blocks relations among sub-issues."""
    id_set = {n["id"] for n in nodes}
    id_to_ident = {n["id"]: n["identifier"] for n in nodes}

    # adjacency: blocker -> blocked
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


def fetch_sub_issues(parent_id: str) -> dict:
    env = load_env()
    api_key = env.get("LINEAR_API_KEY") or os.environ.get("LINEAR_API_KEY", "")
    if not api_key:
        print("LINEAR_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    data = graphql(api_key, SUB_ISSUES_QUERY, {"parentId": parent_id})
    issue = data.get("data", {}).get("issue", {})
    nodes = issue.get("children", {}).get("nodes", [])

    sub_issues = []
    for node in nodes:
        state_name = node.get("state", {}).get("name", "")
        labels = []
        for label in node.get("labels", {}).get("nodes", []):
            p = label.get("parent")
            name = label["name"]
            labels.append(f"{p['name']}:{name}" if p else name)

        sub_issues.append({
            "id": node["id"],
            "identifier": node["identifier"],
            "title": node["title"],
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


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: poll.py <status_name>", file=sys.stderr)
        sys.exit(1)
    issues = poll(sys.argv[1])
    print(json.dumps(issues))
