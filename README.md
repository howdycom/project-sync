# Project Sync

Composite GitHub Action that reproduces a Jira-style **PR/push → board
status** automation on a native **GitHub Project (v2)** board.

The board tracks GitHub **issues**; PRs link them with `Closes #N` /
`Fixes #N`. On each event the action reads the PR's linked closing issues
and moves *their* Status field:

| Trigger (base = `base_branch`) | Status column |
|---|---|
| PR opened as **draft** / converted to draft | `status_in_progress` |
| PR opened (ready) / `ready_for_review` | `status_review` |
| PR merged into `base_branch` | `status_post_merge` |
| push to `production_branch` | `status_done` |

Built-in skip rules: dependabot PRs, `Revert…`, `Bump …`, and `[TECH]`
titles are ignored. PRs with no linked board issue are a no-op.

Licensed under the [MIT License](LICENSE).

### No-regress guard

Unlike a locked-down Jira workflow (which only offers transitions valid from
the current status), the Projects API lets any status jump to any other. To
stop a follow-up PR from dragging an already-advanced issue backwards, a
target that would move an item **already at or beyond `status_post_merge`
(the QA gate)** back to an earlier column is skipped. The
`in_progress ↔ review` oscillation (e.g. converting a PR back to draft) and
all forward progress are still applied.

## Requirements

A **`token`** — a fine-grained PAT or GitHub App installation token — that
has **both**:

- repository **Pull requests: read** (and issues read) — the closing-issues
  lookup is a repo-level GraphQL read, required on a private repo; and
- organization **Projects: read and write**.

The default `GITHUB_TOKEN` **cannot** write Projects v2, so it is not usable
here and the action does not fall back to it.

## Usage

Add a thin caller workflow in the consuming repo. Two jobs — one for PR
events, one for the production push:

```yaml
name: GitHub Project Sync
on:
  pull_request_target:
    types: [opened, ready_for_review, converted_to_draft, closed]
    branches: [staging]
  push:
    branches: [main]

jobs:
  sync-pr:
    if: github.event_name == 'pull_request_target'
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      # No repo checkout: the trusted script ships with the pinned action, so
      # under pull_request_target it never runs code from the PR head.
      - uses: howdycom/project-sync@v1
        with:
          token: ${{ secrets.PROJECTS_SYNC_TOKEN }}
          project_owner: my-org
          project_number: "3"

  sync-push:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # git log before..after needs history
      - uses: howdycom/project-sync@v1
        with:
          token: ${{ secrets.PROJECTS_SYNC_TOKEN }}
          project_owner: my-org
          project_number: "3"
```

Override any of `status_field`, `base_branch`, `production_branch`,
`status_in_progress`, `status_review`, `status_post_merge`, `status_done` to
match a different board's columns and branch names.

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `token` | yes | — | Org Projects read/write token (PAT or App). |
| `project_owner` | yes | — | Org login owning the board. |
| `project_number` | yes | — | Project (v2) number from its URL. |
| `status_field` | no | `Status` | Single-select field to drive. |
| `base_branch` | no | `staging` | PR base gating the in-progress/review/post-merge moves. |
| `production_branch` | no | `main` | Push here marks issues done. |
| `status_in_progress` | no | `In progress` | Draft / converted-to-draft column. |
| `status_review` | no | `Code Review` | Ready-for-review column. |
| `status_post_merge` | no | `QA` | Merged-to-base column (QA gate). |
| `status_done` | no | `Done` | Pushed-to-production column. |

## Developing

```bash
python3 -m pip install pytest coverage
coverage run --source=project_sync -m pytest tests -q
coverage report -m --fail-under=100
```

The suite enforces 100% coverage — add tests with every behavior change.

## Versioning

Changes are tagged with semver (`v1`, `v1.1`, …). The major tag (`v1`) moves
to the latest compatible release; breaking changes bump the major version.
Don't reference `main` from a consumer workflow.

## Contributing

Changes go through a PR, not direct pushes to `main`. This action moves
issues on shared project boards in consuming orgs, so review matters here
more than usual.
