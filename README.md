# project-sync

Reproduces a Jira-style **PR/push → board status** automation on a native
**GitHub Project (v2)** board. It is the stack-agnostic, reusable version of the
per-repo `jira-sync` scripts (`ai-matching-platform/scripts/jira_sync.py`).

The board tracks GitHub **issues**; PRs link them with `Closes #N` / `Fixes #N`.
On each event the action reads the PR's linked closing issues and moves *their*
Status field:

| Trigger (base = `base_branch`) | Status column |
|---|---|
| PR opened as **draft** / converted to draft | `status_in_progress` |
| PR opened (ready) / `ready_for_review` | `status_review` |
| PR merged into `base_branch` | `status_post_merge` |
| push to `production_branch` | `status_done` |

Same skip rules as `jira-sync`: dependabot, `Revert…`, `Bump …`, `[TECH]`.
PRs with no linked board issue are a no-op.

### No-regress guard

Unlike a locked-down Jira workflow (which only offers transitions valid from the
current status), the Projects API lets any status jump to any other. To stop a
follow-up PR from dragging an already-advanced issue backwards, a target that
would move an item **already at or beyond `status_post_merge` (the QA gate)**
back to an earlier column is skipped. The `in_progress ↔ review` oscillation
(e.g. converting a PR back to draft) and all forward progress are still applied.

## Requirements

A **`token`** — a fine-grained PAT or GitHub App installation token — that has
**both**:

- repository **Pull requests: read** (and issues read) — the closing-issues
  lookup is a repo-level GraphQL read, required on a private repo; and
- organization **Projects: read and write**.

The default `GITHUB_TOKEN` **cannot** write Projects v2, so it is not usable
here and the action does not fall back to it.

## Usage

Add a thin caller workflow in the consuming repo. Two jobs — one for PR events,
one for the production push:

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
    runs-on: ubuntu-slim
    permissions:
      contents: read
    steps:
      # No repo checkout: the trusted script ships with the pinned action, so
      # under pull_request_target it never runs code from the PR head.
      - uses: howdycom/workflows/actions/project-sync@v1
        with:
          token: ${{ secrets.PROJECTS_SYNC_TOKEN }}
          project_owner: howdycom
          project_number: "3"

  sync-push:
    if: github.event_name == 'push'
    runs-on: ubuntu-slim
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # git log before..after needs history
      - uses: howdycom/workflows/actions/project-sync@v1
        with:
          token: ${{ secrets.PROJECTS_SYNC_TOKEN }}
          project_owner: howdycom
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
