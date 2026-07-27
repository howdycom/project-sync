#!/usr/bin/env python3
"""GitHub PR/push status sync for a GitHub Project (v2) board.

Stack-agnostic counterpart to a Jira PR-status automation. It runs on
pull_request / push events and moves the Status field of the board issues linked
to a PR, reproducing the Jira state machine on a native GitHub Project:

    PR opened as draft / converted to draft ......... In progress
    PR opened (ready) / ready_for_review ............ Code Review
    PR merged into the base branch .................. QA   (post-merge)
    push to the production branch ................... Done

The board tracks GitHub *issues*, so the script moves the Status field of every
issue linked to the PR via GitHub's native "Closes #N" / "Fixes #N" mechanism
(``closingIssuesReferences``). PRs with no linked board issue are a no-op.

Column names, board owner/number, and the base/production branches are all
configured via environment variables so the same script drives any repo's board.

PROJECTS_TOKEN must be able to BOTH read the repository's pull requests/issues
(the ``closingIssuesReferences`` lookup is a repo-level GraphQL read, which needs
repo PR/issue read access on a private repo) AND write organization Projects.
A fine-grained PAT with "Pull requests: read" on the repo + "Projects: read and
write" on the org, or a GitHub App installation token with the same, satisfies
both. The default GITHUB_TOKEN cannot write Projects and is not used.

Backward-move guard: unlike a locked-down Jira workflow (which only offers the
transitions valid from the current status), the Projects API allows any status
to move to any other. To avoid dragging an already-advanced issue backwards when
a follow-up PR is opened, a target that would move an item that has already
reached the post-merge (QA) or done column back to an earlier column is skipped.
The In progress <-> Code Review oscillation (e.g. converting a PR back to draft)
is still allowed, and all forward progress is always applied.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence
from urllib.error import HTTPError
from urllib.request import Request, urlopen

TargetStatus = str
SyncOutcome = Literal["updated", "already-in-target", "skipped-backward"]

ZERO_SHA = "0000000000000000000000000000000000000000"
GRAPHQL_URL = "https://api.github.com/graphql"
DEFAULT_PROJECT_OWNER = "howdycom"
DEFAULT_PROJECT_NUMBER = 3
DEFAULT_STATUS_FIELD = "Status"
DEFAULT_PR_BASE_BRANCH = "staging"
DEFAULT_PRODUCTION_BRANCH = "main"
# Matches both the squash-merge subject "Title (#123)" and the merge-commit
# subject "Merge pull request #123 from ...".
PULL_REQUEST_NUMBER_REGEX = r"\(#(\d+)\)|Merge pull request #(\d+)"


@dataclass(frozen=True)
class StatusConfig:
    in_progress: str = "In progress"
    review: str = "Code Review"
    post_merge: str = "QA"
    done: str = "Done"

    def pipeline(self) -> list[str]:
        """Column names in forward order; index is the rank used by the guard."""
        return [self.in_progress, self.review, self.post_merge, self.done]


@dataclass(frozen=True)
class SyncConfig:
    project_owner: str = DEFAULT_PROJECT_OWNER
    project_number: int = DEFAULT_PROJECT_NUMBER
    status_field: str = DEFAULT_STATUS_FIELD
    pr_base_branch: str = DEFAULT_PR_BASE_BRANCH
    production_branch: str = DEFAULT_PRODUCTION_BRANCH
    statuses: StatusConfig = StatusConfig()


@dataclass(frozen=True)
class ProjectContext:
    """Resolved ids for the target project's single-select Status field."""

    project_id: str
    field_id: str
    option_ids: dict[str, str] = field(default_factory=dict)

    def option_id_for(self, status_name: str) -> str | None:
        return self.option_ids.get(status_name.lower())


@dataclass(frozen=True)
class ProjectItem:
    issue_number: int
    item_id: str
    current_status: str | None


@dataclass(frozen=True)
class PrSyncInfo:
    """A PR's title/author (for skip rules) plus its linked board items."""

    title: str | None
    author_login: str | None
    items: list[ProjectItem]


def _parse_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}")


def load_config_from_env() -> SyncConfig:
    return SyncConfig(
        project_owner=os.environ.get("PROJECT_OWNER", DEFAULT_PROJECT_OWNER),
        project_number=_parse_int_env("PROJECT_NUMBER", DEFAULT_PROJECT_NUMBER),
        status_field=os.environ.get("PROJECT_STATUS_FIELD", DEFAULT_STATUS_FIELD),
        pr_base_branch=os.environ.get("PROJECT_PR_BASE_BRANCH", DEFAULT_PR_BASE_BRANCH),
        production_branch=os.environ.get("PROJECT_PRODUCTION_BRANCH", DEFAULT_PRODUCTION_BRANCH),
        statuses=StatusConfig(
            in_progress=os.environ.get("PROJECT_STATUS_IN_PROGRESS", "In progress"),
            review=os.environ.get("PROJECT_STATUS_REVIEW", "Code Review"),
            post_merge=os.environ.get("PROJECT_STATUS_POST_MERGE", "QA"),
            done=os.environ.get("PROJECT_STATUS_DONE", "Done"),
        ),
    )


def should_skip_pr(title: str | None, author_login: str | None) -> bool:
    """Skip the same PRs the Jira sync skips so the two syncs stay aligned."""
    if author_login == "dependabot[bot]":
        return True
    if not title:
        return False
    return title.startswith("Revert") or title.startswith("Bump ") or "[TECH]" in title


def target_status_for_pull_request(
    action: str,
    draft: bool,
    merged: bool,
    base_ref: str,
    config: SyncConfig,
) -> TargetStatus | None:
    if base_ref != config.pr_base_branch:
        return None

    if action == "closed":
        return config.statuses.post_merge if merged else None
    if action == "converted_to_draft":
        return config.statuses.in_progress
    if action == "opened":
        return config.statuses.in_progress if draft else config.statuses.review
    if action == "ready_for_review":
        return config.statuses.review

    return None


def target_status_for_push(ref: str, config: SyncConfig) -> TargetStatus | None:
    return config.statuses.done if ref == f"refs/heads/{config.production_branch}" else None


def status_rank(status_name: str | None, config: SyncConfig) -> int:
    """Forward-order rank of a column; Todo / unknown / None rank below the pipeline."""
    if not status_name:
        return -1
    lowered = [name.lower() for name in config.statuses.pipeline()]
    try:
        return lowered.index(status_name.lower())
    except ValueError:
        return -1


def is_backward_from_gate(
    current_status: str | None, target_status: TargetStatus, config: SyncConfig
) -> bool:
    """True if moving to target would regress an item already at/beyond QA (the gate)."""
    current_rank = status_rank(current_status, config)
    target_rank = status_rank(target_status, config)
    gate_rank = status_rank(config.statuses.post_merge, config)
    return current_rank >= gate_rank and target_rank < current_rank


def extract_pull_request_numbers_from_commit_subjects(subjects: Sequence[str]) -> list[int]:
    numbers: list[int] = []
    seen: set[int] = set()
    for subject in subjects:
        for match in re.finditer(PULL_REQUEST_NUMBER_REGEX, subject):
            number = int(match.group(1) or match.group(2))
            if number not in seen:
                seen.add(number)
                numbers.append(number)
    return numbers


CommandRunner = Callable[[Sequence[str]], str]


def _default_command_runner(cmd: Sequence[str]) -> str:
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout


def read_commit_subjects_between(
    before: str, after: str, runner: CommandRunner = _default_command_runner
) -> list[str]:
    revision = after if before == ZERO_SHA else f"{before}..{after}"
    command = ["git", "log", "--format=%s", revision]
    try:
        output = runner(command)
    except Exception as exc:
        raise RuntimeError(
            f"git log {revision} failed, likely because checkout history is too shallow. "
            f"Original: {exc}"
        ) from exc
    return [line for line in output.splitlines() if line.strip()]


class GitHubProjectsClient:
    def __init__(self, token: str, repository: str) -> None:
        self.token = token
        self.repository = repository

    def resolve_project(self, config: SyncConfig) -> ProjectContext:
        query = """
        query($login: String!, $number: Int!, $field: String!) {
          organization(login: $login) {
            projectV2(number: $number) {
              id
              field(name: $field) {
                ... on ProjectV2SingleSelectField {
                  id
                  options { id name }
                }
              }
            }
          }
        }
        """
        data = self._graphql(
            query,
            {
                "login": config.project_owner,
                "number": config.project_number,
                "field": config.status_field,
            },
        )
        org = data.get("organization") or {}
        project = org.get("projectV2") or {}
        project_id = project.get("id")
        field_node = project.get("field") or {}
        field_id = field_node.get("id")
        if not project_id or not field_id:
            raise RuntimeError(
                f"Could not resolve project {config.project_owner}/#{config.project_number} "
                f"or its {config.status_field!r} single-select field"
            )
        option_ids = {
            str(option.get("name", "")).lower(): str(option.get("id", ""))
            for option in (field_node.get("options") or [])
        }
        return ProjectContext(project_id=project_id, field_id=field_id, option_ids=option_ids)

    def fetch_pr_sync_info(self, pr_number: int, config: SyncConfig, project_id: str) -> PrSyncInfo:
        owner, _, repo = self.repository.partition("/")
        query = """
        query($owner: String!, $repo: String!, $number: Int!, $field: String!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              title
              author { login }
              closingIssuesReferences(first: 20) {
                nodes {
                  number
                  projectItems(first: 20) {
                    nodes {
                      id
                      project { id }
                      fieldValueByName(name: $field) {
                        ... on ProjectV2ItemFieldSingleSelectValue { name }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = self._graphql(
            query,
            {
                "owner": owner,
                "repo": repo,
                "number": pr_number,
                "field": config.status_field,
            },
        )
        repository = data.get("repository") or {}
        pull_request = repository.get("pullRequest") or {}
        title = pull_request.get("title")
        author_login = (pull_request.get("author") or {}).get("login")
        references = (pull_request.get("closingIssuesReferences") or {}).get("nodes") or []

        items: list[ProjectItem] = []
        for issue in references:
            if not isinstance(issue, dict):
                continue
            issue_number = issue.get("number")
            for node in (issue.get("projectItems") or {}).get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                project = node.get("project") or {}
                # Compare by the resolved project node id, not the number: project
                # numbers are scoped per owner, so a foreign project can share #3.
                if project.get("id") != project_id:
                    continue
                status_value = node.get("fieldValueByName") or {}
                items.append(
                    ProjectItem(
                        issue_number=int(issue_number) if issue_number is not None else 0,
                        item_id=str(node.get("id", "")),
                        current_status=status_value.get("name"),
                    )
                )
        return PrSyncInfo(title=title, author_login=author_login, items=items)

    def set_item_status(self, context: ProjectContext, item_id: str, option_id: str) -> None:
        mutation = """
        mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $project
            itemId: $item
            fieldId: $field
            value: { singleSelectOptionId: $option }
          }) {
            projectV2Item { id }
          }
        }
        """
        self._graphql(
            mutation,
            {
                "project": context.project_id,
                "item": item_id,
                "field": context.field_id,
                "option": option_id,
            },
        )

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = Request(
            GRAPHQL_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GitHub GraphQL request failed: {exc.code} {exc.reason} {error_body}"
            ) from exc

        parsed = json.loads(response_body)
        if not isinstance(parsed, dict):
            raise RuntimeError("Unexpected GitHub GraphQL response")
        if parsed.get("errors"):
            raise RuntimeError(f"GitHub GraphQL returned errors: {json.dumps(parsed['errors'])}")
        data = parsed.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("GitHub GraphQL response missing data")
        return data


def sync_item(
    item: ProjectItem,
    target_status: TargetStatus,
    option_id: str,
    context: ProjectContext,
    client: GitHubProjectsClient,
    config: SyncConfig,
) -> SyncOutcome:
    if item.current_status and item.current_status.lower() == target_status.lower():
        return "already-in-target"
    if is_backward_from_gate(item.current_status, target_status, config):
        return "skipped-backward"
    client.set_item_status(context, item.item_id, option_id)
    return "updated"


def process_pr_numbers(
    pr_numbers: Sequence[int],
    target_status: TargetStatus,
    config: SyncConfig,
    context: ProjectContext,
    client: GitHubProjectsClient,
) -> None:
    option_id = context.option_id_for(target_status)
    if not option_id:
        raise RuntimeError(
            f"Status column {target_status!r} does not exist on the project board; "
            f"available: {sorted(context.option_ids)}"
        )

    failures = 0
    total = 0
    for pr_number in pr_numbers:
        info = client.fetch_pr_sync_info(pr_number, config, context.project_id)
        # Apply skip rules on every path (incl. production pushes), so dependabot/
        # Revert/Bump/[TECH] PRs never move their linked board items.
        if should_skip_pr(info.title, info.author_login):
            print(f"  PR #{pr_number}: {info.title!r} is TECH/Revert/Bump/dependabot; skipping")
            continue
        if not info.items:
            print(f"  PR #{pr_number}: no linked board issue; skipping")
            continue
        for item in info.items:
            total += 1
            label = f"#{item.issue_number} (PR #{pr_number})"
            try:
                outcome = sync_item(item, target_status, option_id, context, client, config)
                print(f"  {label}: {outcome}")
            except Exception as exc:
                failures += 1
                print(f"  {label}: ERROR - {exc}", file=sys.stderr)

    if failures:
        raise RuntimeError(f"{failures} of {total} project item updates failed")


def get_client_or_none() -> GitHubProjectsClient | None:
    # Only PROJECTS_TOKEN: no GITHUB_TOKEN fallback. The default token cannot
    # write org Projects, so falling back would turn a missing-secret no-op into
    # a hard auth failure. An empty/absent PROJECTS_TOKEN must no-op.
    token = os.environ.get("PROJECTS_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repository:
        print("PROJECTS_TOKEN or GITHUB_REPOSITORY is not set; skipping project sync")
        return None
    return GitHubProjectsClient(token=token, repository=repository)


def read_event_payload() -> dict[str, Any]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise RuntimeError("GITHUB_EVENT_PATH is not set")
    with open(event_path, encoding="utf-8") as payload_file:
        payload = json.load(payload_file)
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub event payload must be a JSON object")
    return payload


def handle_pull_request(
    payload: dict[str, Any], config: SyncConfig, client: GitHubProjectsClient
) -> None:
    pr = payload.get("pull_request") or {}
    if not isinstance(pr, dict):
        raise RuntimeError("pull_request payload is missing")

    base = pr.get("base") or {}
    user = pr.get("user") or {}
    number = pr.get("number")
    target = target_status_for_pull_request(
        action=str(payload.get("action", "")),
        draft=bool(pr.get("draft", False)),
        merged=bool(pr.get("merged", False)),
        base_ref=str(base.get("ref", "")),
        config=config,
    )
    if not target:
        print(
            f"No status change for action={payload.get('action')} "
            f"base={base.get('ref')}; skipping"
        )
        return

    title = pr.get("title")
    author_login = user.get("login")
    if should_skip_pr(
        title=title if isinstance(title, str) else None,
        author_login=author_login if isinstance(author_login, str) else None,
    ):
        print(f"PR {title!r} is TECH/Revert/Bump/dependabot; skipping")
        return

    if not isinstance(number, int):
        print("PR payload has no numeric number; skipping")
        return

    context = client.resolve_project(config)
    print(f"Target status: {target}. PR #{number}")
    process_pr_numbers([number], target, config, context, client)


def handle_push(payload: dict[str, Any], config: SyncConfig, client: GitHubProjectsClient) -> None:
    ref = str(payload.get("ref", ""))
    target = target_status_for_push(ref, config)
    if not target:
        print(f"Push to {ref}; not {config.production_branch}, skipping")
        return

    subjects = read_commit_subjects_between(
        before=str(payload.get("before", "")),
        after=str(payload.get("after", "")),
    )
    pr_numbers = extract_pull_request_numbers_from_commit_subjects(subjects)
    if not pr_numbers:
        print("No PR references found in commit range; skipping")
        return

    context = client.resolve_project(config)
    print(f"Target status: {target}. PRs: {', '.join(f'#{n}' for n in pr_numbers)}")
    process_pr_numbers(pr_numbers, target, config, context, client)


def main() -> None:
    client = get_client_or_none()
    if not client:
        return

    config = load_config_from_env()
    payload = read_event_payload()
    event_name = os.environ.get("GITHUB_EVENT_NAME")

    if event_name in {"pull_request", "pull_request_target"}:
        handle_pull_request(payload, config, client)
    elif event_name == "push":
        handle_push(payload, config, client)
    else:
        print(f"Unsupported event: {event_name}; skipping")


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except Exception as err:
        print(err, file=sys.stderr)
        sys.exit(1)
