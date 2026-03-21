from __future__ import annotations

import re
import sys
import time

import httpx

GRAPHQL_URL = "https://api.linear.app/graphql"


class LinearClient:
    def __init__(self, api_key: str, team_id: str | None = None):
        self._api_key = api_key
        self._team_id = team_id
        self._client = httpx.Client(
            headers={"Authorization": api_key},
            timeout=30.0,
        )

    def close(self):
        self._client.close()

    @property
    def team_id(self) -> str:
        if not self._team_id:
            raise RuntimeError("team_id not resolved; call resolve_team() first")
        return self._team_id

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                resp = self._client.post(
                    GRAPHQL_URL,
                    json={"query": query, "variables": variables or {}},
                )
            except httpx.HTTPError:
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise
            if resp.status_code >= 500 and attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("graphql retries exhausted")

    def resolve_team(self, team_name: str):
        data = self.graphql(
            """query($name: String!) {
              teams(filter: { name: { eq: $name } }) { nodes { id name } }
            }""",
            {"name": team_name},
        )
        nodes = data.get("data", {}).get("teams", {}).get("nodes", [])
        if not nodes:
            print(f"Team '{team_name}' not found", file=sys.stderr)
            raise SystemExit(1)
        self._team_id = nodes[0]["id"]

    def poll(self, status: str) -> list[dict]:
        data = self.graphql(
            """query($teamId: ID!, $stateName: String!) {
              issues(filter: {
                team: { id: { eq: $teamId } }
                state: { name: { eq: $stateName } }
              }) {
                nodes { id identifier title labels { nodes { name parent { name } } } }
              }
            }""",
            {"teamId": self.team_id, "stateName": status},
        )
        issues = []
        for node in data.get("data", {}).get("issues", {}).get("nodes", []):
            labels = _parse_labels(node.get("labels", {}).get("nodes", []))
            issues.append({
                "id": node["id"],
                "identifier": node["identifier"],
                "title": node["title"],
                "labels": labels,
            })
        return issues

    def fetch_issue_detail(self, issue_id: str) -> dict:
        data = self.graphql(
            """query($issueId: String!) {
              issue(id: $issueId) {
                id identifier title description
                labels { nodes { id name parent { name } } }
                attachments { nodes { id title url } }
              }
            }""",
            {"issueId": issue_id},
        )
        issue = data.get("data", {}).get("issue", {})
        label_nodes = issue.get("labels", {}).get("nodes", [])
        attachments = [
            {"id": a["id"], "title": a["title"], "url": a["url"]}
            for a in issue.get("attachments", {}).get("nodes", [])
        ]
        return {
            "id": issue.get("id", ""),
            "identifier": issue.get("identifier", ""),
            "title": issue.get("title", ""),
            "description": issue.get("description", ""),
            "labels": _parse_labels(label_nodes),
            "label_nodes": label_nodes,
            "attachments": attachments,
        }

    def fetch_issue_comments(self, issue_id: str) -> list[dict]:
        data = self.graphql(
            """query($issueId: String!) {
              issue(id: $issueId) {
                comments { nodes { body user { name } createdAt } }
              }
            }""",
            {"issueId": issue_id},
        )
        comments = data.get("data", {}).get("issue", {}).get("comments", {}).get("nodes", [])
        return [
            {"body": c["body"], "user": c.get("user", {}).get("name", ""), "createdAt": c["createdAt"]}
            for c in comments
        ]

    def update_issue_state(self, issue_id: str, state_name: str):
        data = self.graphql(
            """query($teamId: ID!) {
              workflowStates(filter: { team: { id: { eq: $teamId } } }) {
                nodes { id name }
              }
            }""",
            {"teamId": self.team_id},
        )
        states = data.get("data", {}).get("workflowStates", {}).get("nodes", [])
        state_id = next((s["id"] for s in states if s["name"] == state_name), None)
        if not state_id:
            print(f"State '{state_name}' not found", file=sys.stderr)
            return
        self.graphql(
            """mutation($issueId: String!, $stateId: String!) {
              issueUpdate(id: $issueId, input: { stateId: $stateId }) {
                issue { id state { name } }
              }
            }""",
            {"issueId": issue_id, "stateId": state_id},
        )

    def create_comment(self, issue_id: str, body: str):
        if not body or not body.strip():
            return
        self.graphql(
            """mutation($issueId: String!, $body: String!) {
              commentCreate(input: { issueId: $issueId, body: $body }) {
                comment { id }
              }
            }""",
            {"issueId": issue_id, "body": body},
        )

    def fetch_document(self, slug_id: str) -> dict | None:
        data = self.graphql(
            """query($slugId: String!) {
              documents(filter: { slugId: { eq: $slugId } }, first: 1) {
                nodes { id title content }
              }
            }""",
            {"slugId": slug_id},
        )
        nodes = data.get("data", {}).get("documents", {}).get("nodes", [])
        if not nodes:
            return None
        doc = nodes[0]
        return {"id": doc["id"], "title": doc["title"], "content": doc["content"]}

    def resolve_attachment_documents(self, attachments: list[dict]) -> list[dict]:
        doc_url_re = re.compile(r"https://linear\.app/[^/]+/document/.+-([0-9a-f]+)$")
        docs = []
        for att in attachments:
            m = doc_url_re.match(att.get("url", ""))
            if not m:
                continue
            doc = self.fetch_document(m.group(1))
            if doc:
                docs.append(doc)
        return docs

    def fetch_sub_issues(self, parent_id: str) -> dict:
        data = self.graphql(
            """query($parentId: String!) {
              issue(id: $parentId) {
                children {
                  nodes {
                    id identifier title description
                    state { name type }
                    labels { nodes { name parent { name } } }
                    relations { nodes { type relatedIssue { id } } }
                    inverseRelations { nodes { type issue { id state { name type } } } }
                  }
                }
                documents { nodes { id title content } }
              }
            }""",
            {"parentId": parent_id},
        )
        issue = data.get("data", {}).get("issue", {})
        nodes = issue.get("children", {}).get("nodes", [])

        sub_issues = []
        for node in nodes:
            state = node.get("state", {})
            sub_issues.append({
                "id": node["id"],
                "identifier": node["identifier"],
                "title": node["title"],
                "description": node.get("description", ""),
                "state": state.get("name", ""),
                "state_type": state.get("type", ""),
                "labels": _parse_labels(node.get("labels", {}).get("nodes", [])),
                "ready": _is_ready(node),
            })

        documents = [
            {"id": d["id"], "title": d["title"], "content": d["content"]}
            for d in issue.get("documents", {}).get("nodes", [])
        ]

        return {
            "sub_issues": sub_issues,
            "documents": documents,
            "cycle": _detect_dependency_cycle(nodes),
        }


def _parse_labels(label_nodes: list[dict]) -> list[str]:
    labels = []
    for node in label_nodes:
        parent_name = (node.get("parent") or {}).get("name")
        if parent_name:
            labels.append(f"{parent_name}:{node['name']}")
        else:
            labels.append(node["name"])
    return labels


def _resolve_repo(labels: list[str], repos: dict[str, str]) -> str | None:
    for label in labels:
        if label.startswith("repo:"):
            repo_name = label.split(":", 1)[1]
            return repos.get(repo_name)
    return None


def _resolve_base_branch(labels: list[str]) -> str:
    for label in labels:
        if label.startswith("branch:"):
            return label.split(":", 1)[1]
    return ""


def _is_ready(node: dict) -> bool:
    from loki2.core.state import FINISHED_STATE_TYPES, STATE_TODO
    state_name = node.get("state", {}).get("name", "")
    if state_name != STATE_TODO:
        return False
    for rel in node.get("inverseRelations", {}).get("nodes", []):
        if rel["type"] == "blocks":
            blocker_state_type = rel.get("issue", {}).get("state", {}).get("type", "")
            if blocker_state_type not in FINISHED_STATE_TYPES:
                return False
    return True


def _detect_dependency_cycle(nodes: list[dict]) -> list[str] | None:
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
