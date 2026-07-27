import io
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from project_sync import (
    ZERO_SHA,
    GitHubProjectsClient,
    ProjectContext,
    ProjectItem,
    PrSyncInfo,
    SyncConfig,
    _parse_int_env,
    extract_pull_request_numbers_from_commit_subjects,
    get_client_or_none,
    handle_pull_request,
    handle_push,
    is_backward_from_gate,
    load_config_from_env,
    main,
    process_pr_numbers,
    read_commit_subjects_between,
    read_event_payload,
    should_skip_pr,
    status_rank,
    sync_item,
    target_status_for_pull_request,
    target_status_for_push,
)

CONFIG = SyncConfig()
CONTEXT = ProjectContext(
    project_id="PVT_1",
    field_id="FIELD_1",
    option_ids={
        "in progress": "opt-inprogress",
        "code review": "opt-review",
        "qa": "opt-qa",
        "done": "opt-done",
    },
)


class RecordingClient:
    """Stand-in for GitHubProjectsClient used by the higher-level handlers."""

    def __init__(self, items_by_pr=None, context=CONTEXT, meta_by_pr=None):
        self.items_by_pr = items_by_pr or {}
        # pr_number -> (title, author_login); defaults to a non-skipped PR.
        self.meta_by_pr = meta_by_pr or {}
        self.context = context
        self.updated = []
        self.resolved = 0

    def resolve_project(self, config):
        self.resolved += 1
        return self.context

    def fetch_pr_sync_info(self, pr_number, config, project_id):
        title, author = self.meta_by_pr.get(pr_number, ("feat: work", "dev"))
        return PrSyncInfo(
            title=title, author_login=author, items=self.items_by_pr.get(pr_number, [])
        )

    def set_item_status(self, context, item_id, option_id):
        self.updated.append((item_id, option_id))


class ConfigTests(unittest.TestCase):
    def test_parse_int_env_default_when_unset_or_blank(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_parse_int_env("PROJECT_NUMBER", 3), 3)
        with patch.dict(os.environ, {"PROJECT_NUMBER": "  "}, clear=True):
            self.assertEqual(_parse_int_env("PROJECT_NUMBER", 3), 3)

    def test_parse_int_env_valid(self):
        with patch.dict(os.environ, {"PROJECT_NUMBER": "7"}, clear=True):
            self.assertEqual(_parse_int_env("PROJECT_NUMBER", 3), 7)

    def test_parse_int_env_invalid_raises(self):
        with patch.dict(os.environ, {"PROJECT_NUMBER": "abc"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "must be an integer"):
                _parse_int_env("PROJECT_NUMBER", 3)

    def test_load_config_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = load_config_from_env()
        self.assertEqual(config.project_owner, "howdycom")
        self.assertEqual(config.project_number, 3)
        self.assertEqual(config.statuses.review, "Code Review")

    def test_load_config_overrides(self):
        env = {
            "PROJECT_OWNER": "acme",
            "PROJECT_NUMBER": "9",
            "PROJECT_STATUS_FIELD": "State",
            "PROJECT_PR_BASE_BRANCH": "develop",
            "PROJECT_PRODUCTION_BRANCH": "release",
            "PROJECT_STATUS_IN_PROGRESS": "Doing",
            "PROJECT_STATUS_REVIEW": "Reviewing",
            "PROJECT_STATUS_POST_MERGE": "Testing",
            "PROJECT_STATUS_DONE": "Shipped",
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_config_from_env()
        self.assertEqual(config.project_owner, "acme")
        self.assertEqual(config.project_number, 9)
        self.assertEqual(config.status_field, "State")
        self.assertEqual(config.pr_base_branch, "develop")
        self.assertEqual(config.production_branch, "release")
        self.assertEqual(config.statuses.in_progress, "Doing")
        self.assertEqual(config.statuses.post_merge, "Testing")
        self.assertEqual(config.statuses.done, "Shipped")

    def test_option_id_for_case_insensitive(self):
        self.assertEqual(CONTEXT.option_id_for("Code Review"), "opt-review")
        self.assertIsNone(CONTEXT.option_id_for("Nonexistent"))


class StateMachineTests(unittest.TestCase):
    def test_pull_request_targets(self):
        self.assertEqual(
            target_status_for_pull_request("opened", True, False, "staging", CONFIG),
            "In progress",
        )
        self.assertEqual(
            target_status_for_pull_request("opened", False, False, "staging", CONFIG),
            "Code Review",
        )
        self.assertEqual(
            target_status_for_pull_request("ready_for_review", False, False, "staging", CONFIG),
            "Code Review",
        )
        self.assertEqual(
            target_status_for_pull_request("converted_to_draft", True, False, "staging", CONFIG),
            "In progress",
        )
        self.assertEqual(
            target_status_for_pull_request("closed", False, True, "staging", CONFIG),
            "QA",
        )

    def test_pull_request_noops(self):
        self.assertIsNone(target_status_for_pull_request("closed", False, False, "staging", CONFIG))
        self.assertIsNone(target_status_for_pull_request("opened", False, False, "main", CONFIG))
        self.assertIsNone(target_status_for_pull_request("edited", False, False, "staging", CONFIG))

    def test_push_target(self):
        self.assertEqual(target_status_for_push("refs/heads/main", CONFIG), "Done")
        self.assertIsNone(target_status_for_push("refs/heads/staging", CONFIG))

    def test_skip_rules(self):
        self.assertTrue(should_skip_pr("[TECH] - Cleanup", "dev"))
        self.assertTrue(should_skip_pr("Revert something", "dev"))
        self.assertTrue(should_skip_pr("Bump package", "dev"))
        self.assertTrue(should_skip_pr("[AP-1] - Deps", "dependabot[bot]"))
        self.assertFalse(should_skip_pr("[AP-1] - Work", "dev"))
        self.assertFalse(should_skip_pr(None, "dev"))


class BackwardGuardTests(unittest.TestCase):
    def test_status_rank(self):
        self.assertEqual(status_rank("In progress", CONFIG), 0)
        self.assertEqual(status_rank("code review", CONFIG), 1)
        self.assertEqual(status_rank("QA", CONFIG), 2)
        self.assertEqual(status_rank("Done", CONFIG), 3)
        self.assertEqual(status_rank("Todo", CONFIG), -1)
        self.assertEqual(status_rank(None, CONFIG), -1)

    def test_blocks_regression_from_qa_and_done(self):
        # A new/updated PR must not drag an item that already reached QA/Done back.
        self.assertTrue(is_backward_from_gate("QA", "Code Review", CONFIG))
        self.assertTrue(is_backward_from_gate("QA", "In progress", CONFIG))
        self.assertTrue(is_backward_from_gate("Done", "Code Review", CONFIG))
        self.assertTrue(is_backward_from_gate("Done", "QA", CONFIG))

    def test_allows_active_phase_oscillation_and_forward_moves(self):
        # In progress <-> Code Review (draft toggle) is allowed.
        self.assertFalse(is_backward_from_gate("Code Review", "In progress", CONFIG))
        self.assertFalse(is_backward_from_gate("In progress", "Code Review", CONFIG))
        # Forward moves and moves out of Todo are allowed.
        self.assertFalse(is_backward_from_gate("Todo", "Code Review", CONFIG))
        self.assertFalse(is_backward_from_gate("Code Review", "QA", CONFIG))
        self.assertFalse(is_backward_from_gate("QA", "Done", CONFIG))
        self.assertFalse(is_backward_from_gate(None, "In progress", CONFIG))


class CommitSubjectTests(unittest.TestCase):
    def test_extracts_squash_and_merge_commit_pr_numbers(self):
        subjects = [
            "feat: thing (#12)",
            "fix: other (#12)",  # dedupe
            "Merge pull request #34 from howdycom/feat/x",  # merge-commit form
            "no ref here",
        ]
        self.assertEqual(extract_pull_request_numbers_from_commit_subjects(subjects), [12, 34])

    def test_reads_subjects_from_commit_range(self):
        calls = []

        def runner(command):
            calls.append(command)
            return "feat (#1)\n\nchore (#2)\n"

        subjects = read_commit_subjects_between("aaa", "bbb", runner)
        self.assertEqual(calls, [["git", "log", "--format=%s", "aaa..bbb"]])
        self.assertEqual(subjects, ["feat (#1)", "chore (#2)"])

    def test_uses_after_sha_for_initial_push(self):
        calls = []

        def runner(command):
            calls.append(command)
            return "subject\n"

        read_commit_subjects_between(ZERO_SHA, "bbb", runner)
        self.assertEqual(calls, [["git", "log", "--format=%s", "bbb"]])

    def test_raises_descriptive_error_when_git_fails(self):
        def runner(command):
            raise RuntimeError("fatal: bad revision")

        with self.assertRaisesRegex(RuntimeError, "too shallow.*fatal: bad revision"):
            read_commit_subjects_between("aaa", "bbb", runner)

    def test_default_command_runner_invokes_git(self):
        with patch("project_sync.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="a (#1)\n", stderr="")
            subjects = read_commit_subjects_between("aaa", "bbb")
        self.assertEqual(subjects, ["a (#1)"])
        run.assert_called_once()


def _graphql_response(data):
    return io.BytesIO(json.dumps({"data": data}).encode("utf-8"))


class GraphQLClientTests(unittest.TestCase):
    def _client(self):
        return GitHubProjectsClient(token="secret", repository="howdycom/ai-matching-platform")

    def test_graphql_success(self):
        with patch("project_sync.urlopen", return_value=_ctx(_graphql_response({"ok": True}))):
            result = self._client()._graphql("query", {})
        self.assertEqual(result, {"ok": True})

    def test_graphql_http_error(self):
        fp = io.BytesIO(b"boom")
        error = HTTPError("http://x", 401, "Unauthorized", {}, fp)
        with patch("project_sync.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "401"):
                self._client()._graphql("query", {})

    def test_graphql_non_dict_response(self):
        with patch("project_sync.urlopen", return_value=_ctx(io.BytesIO(b"[1, 2, 3]"))):
            with self.assertRaisesRegex(RuntimeError, "Unexpected GitHub GraphQL response"):
                self._client()._graphql("query", {})

    def test_graphql_errors_key(self):
        body = io.BytesIO(json.dumps({"errors": [{"message": "nope"}]}).encode("utf-8"))
        with patch("project_sync.urlopen", return_value=_ctx(body)):
            with self.assertRaisesRegex(RuntimeError, "returned errors"):
                self._client()._graphql("query", {})

    def test_graphql_missing_data(self):
        body = io.BytesIO(json.dumps({"data": None}).encode("utf-8"))
        with patch("project_sync.urlopen", return_value=_ctx(body)):
            with self.assertRaisesRegex(RuntimeError, "missing data"):
                self._client()._graphql("query", {})

    def test_resolve_project_success(self):
        data = {
            "organization": {
                "projectV2": {
                    "id": "PVT_x",
                    "field": {
                        "id": "FIELD_x",
                        "options": [
                            {"id": "o1", "name": "In progress"},
                            {"id": "o2", "name": "Done"},
                        ],
                    },
                }
            }
        }
        with patch.object(GitHubProjectsClient, "_graphql", return_value=data):
            context = self._client().resolve_project(CONFIG)
        self.assertEqual(context.project_id, "PVT_x")
        self.assertEqual(context.field_id, "FIELD_x")
        self.assertEqual(context.option_id_for("done"), "o2")

    def test_resolve_project_missing_field_raises(self):
        data = {"organization": {"projectV2": {"id": "PVT_x", "field": None}}}
        with patch.object(GitHubProjectsClient, "_graphql", return_value=data):
            with self.assertRaisesRegex(RuntimeError, "Could not resolve project"):
                self._client().resolve_project(CONFIG)

    def test_fetch_pr_sync_info_filters_by_project_id_and_parses(self):
        data = {
            "repository": {
                "pullRequest": {
                    "title": "feat: add",
                    "author": {"login": "dev"},
                    "closingIssuesReferences": {
                        "nodes": [
                            None,  # non-dict issue node is skipped
                            {
                                "number": 100,
                                "projectItems": {
                                    "nodes": [
                                        "bad-node",  # non-dict item skipped
                                        {
                                            "id": "ITEM_A",
                                            "project": {"id": "PVT_1"},
                                            "fieldValueByName": {"name": "Todo"},
                                        },
                                        {
                                            # same project number elsewhere, different id
                                            "id": "ITEM_FOREIGN",
                                            "project": {"id": "PVT_OTHER"},
                                            "fieldValueByName": {"name": "Todo"},
                                        },
                                    ]
                                },
                            },
                            {
                                "number": None,  # missing issue number -> 0
                                "projectItems": {
                                    "nodes": [
                                        {
                                            "id": "ITEM_B",
                                            "project": {"id": "PVT_1"},
                                            "fieldValueByName": None,
                                        }
                                    ]
                                },
                            },
                        ]
                    },
                }
            }
        }
        with patch.object(GitHubProjectsClient, "_graphql", return_value=data):
            info = self._client().fetch_pr_sync_info(100, CONFIG, "PVT_1")
        self.assertEqual(info.title, "feat: add")
        self.assertEqual(info.author_login, "dev")
        self.assertEqual(
            info.items,
            [
                ProjectItem(issue_number=100, item_id="ITEM_A", current_status="Todo"),
                ProjectItem(issue_number=0, item_id="ITEM_B", current_status=None),
            ],
        )

    def test_fetch_pr_sync_info_handles_missing_author(self):
        data = {"repository": {"pullRequest": {"title": "t", "author": None}}}
        with patch.object(GitHubProjectsClient, "_graphql", return_value=data):
            info = self._client().fetch_pr_sync_info(1, CONFIG, "PVT_1")
        self.assertEqual(info.title, "t")
        self.assertIsNone(info.author_login)
        self.assertEqual(info.items, [])

    def test_set_item_status_sends_mutation(self):
        captured = {}

        def fake_graphql(self, query, variables):
            captured["query"] = query
            captured["variables"] = variables
            return {}

        with patch.object(GitHubProjectsClient, "_graphql", fake_graphql):
            self._client().set_item_status(CONTEXT, "ITEM_A", "opt-qa")
        self.assertIn("updateProjectV2ItemFieldValue", captured["query"])
        self.assertEqual(
            captured["variables"],
            {"project": "PVT_1", "item": "ITEM_A", "field": "FIELD_1", "option": "opt-qa"},
        )


class SyncItemTests(unittest.TestCase):
    def test_already_in_target(self):
        client = RecordingClient()
        item = ProjectItem(issue_number=1, item_id="ITEM_A", current_status="QA")
        self.assertEqual(
            sync_item(item, "QA", "opt-qa", CONTEXT, client, CONFIG), "already-in-target"
        )
        self.assertEqual(client.updated, [])

    def test_updated(self):
        client = RecordingClient()
        item = ProjectItem(issue_number=1, item_id="ITEM_A", current_status="Todo")
        self.assertEqual(sync_item(item, "QA", "opt-qa", CONTEXT, client, CONFIG), "updated")
        self.assertEqual(client.updated, [("ITEM_A", "opt-qa")])

    def test_updated_when_current_status_none(self):
        client = RecordingClient()
        item = ProjectItem(issue_number=1, item_id="ITEM_A", current_status=None)
        self.assertEqual(sync_item(item, "QA", "opt-qa", CONTEXT, client, CONFIG), "updated")

    def test_skips_backward_from_qa(self):
        client = RecordingClient()
        item = ProjectItem(issue_number=1, item_id="ITEM_A", current_status="QA")
        self.assertEqual(
            sync_item(item, "Code Review", "opt-review", CONTEXT, client, CONFIG),
            "skipped-backward",
        )
        self.assertEqual(client.updated, [])


class ProcessPrNumbersTests(unittest.TestCase):
    def test_raises_when_status_column_missing(self):
        client = RecordingClient()
        with self.assertRaisesRegex(RuntimeError, "does not exist on the project board"):
            process_pr_numbers([1], "Nonexistent", CONFIG, CONTEXT, client)

    def test_skips_pr_with_no_items(self):
        client = RecordingClient(items_by_pr={1: []})
        process_pr_numbers([1], "QA", CONFIG, CONTEXT, client)
        self.assertEqual(client.updated, [])

    def test_skips_pr_matching_skip_rules(self):
        items = [ProjectItem(issue_number=100, item_id="ITEM_A", current_status="Todo")]
        client = RecordingClient(
            items_by_pr={5: items}, meta_by_pr={5: ("Bump deps", "dependabot[bot]")}
        )
        process_pr_numbers([5], "Done", CONFIG, CONTEXT, client)
        self.assertEqual(client.updated, [])

    def test_updates_items(self):
        items = [ProjectItem(issue_number=100, item_id="ITEM_A", current_status="Todo")]
        client = RecordingClient(items_by_pr={5: items})
        process_pr_numbers([5], "QA", CONFIG, CONTEXT, client)
        self.assertEqual(client.updated, [("ITEM_A", "opt-qa")])

    def test_does_not_update_when_backward(self):
        items = [ProjectItem(issue_number=100, item_id="ITEM_A", current_status="Done")]
        client = RecordingClient(items_by_pr={5: items})
        process_pr_numbers([5], "Code Review", CONFIG, CONTEXT, client)
        self.assertEqual(client.updated, [])

    def test_raises_when_update_fails(self):
        items = [ProjectItem(issue_number=100, item_id="ITEM_A", current_status="Todo")]

        class FailingClient(RecordingClient):
            def set_item_status(self, context, item_id, option_id):
                raise RuntimeError("api down")

        client = FailingClient(items_by_pr={5: items})
        with self.assertRaisesRegex(RuntimeError, "1 of 1 project item updates failed"):
            process_pr_numbers([5], "QA", CONFIG, CONTEXT, client)


class ClientFactoryTests(unittest.TestCase):
    def test_returns_none_without_projects_token_even_if_github_token_set(self):
        # No GITHUB_TOKEN fallback: a missing PROJECTS_TOKEN must no-op.
        env = {"GITHUB_TOKEN": "gtok", "GITHUB_REPOSITORY": "a/b"}
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(get_client_or_none())

    def test_returns_none_without_repository(self):
        with patch.dict(os.environ, {"PROJECTS_TOKEN": "t"}, clear=True):
            self.assertIsNone(get_client_or_none())

    def test_uses_projects_token(self):
        env = {"PROJECTS_TOKEN": "ptok", "GITHUB_REPOSITORY": "a/b"}
        with patch.dict(os.environ, env, clear=True):
            client = get_client_or_none()
        self.assertEqual(client.token, "ptok")


class ReadEventPayloadTests(unittest.TestCase):
    def test_raises_without_event_path(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GITHUB_EVENT_PATH is not set"):
                read_event_payload()

    def test_raises_when_not_object(self):
        payload_file = io.StringIO("[1, 2]")
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": "/x"}, clear=True):
            with patch("builtins.open", return_value=payload_file):
                with self.assertRaisesRegex(RuntimeError, "must be a JSON object"):
                    read_event_payload()

    def test_reads_object(self):
        payload_file = io.StringIO('{"action": "opened"}')
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": "/x"}, clear=True):
            with patch("builtins.open", return_value=payload_file):
                self.assertEqual(read_event_payload(), {"action": "opened"})


class HandlePullRequestTests(unittest.TestCase):
    def test_raises_when_pull_request_not_dict(self):
        client = RecordingClient()
        with self.assertRaisesRegex(RuntimeError, "pull_request payload is missing"):
            handle_pull_request({"pull_request": "nope"}, CONFIG, client)

    def test_skips_when_no_target(self):
        payload = {
            "action": "edited",
            "pull_request": {"base": {"ref": "staging"}, "number": 1},
        }
        client = RecordingClient()
        handle_pull_request(payload, CONFIG, client)
        self.assertEqual(client.resolved, 0)

    def test_skips_when_should_skip(self):
        payload = {
            "action": "opened",
            "pull_request": {
                "base": {"ref": "staging"},
                "user": {"login": "dependabot[bot]"},
                "title": "Bump x",
                "number": 1,
                "draft": False,
            },
        }
        client = RecordingClient()
        handle_pull_request(payload, CONFIG, client)
        self.assertEqual(client.resolved, 0)

    def test_skips_when_number_not_int(self):
        payload = {
            "action": "opened",
            "pull_request": {
                "base": {"ref": "staging"},
                "user": {"login": "dev"},
                "title": "feat",
                "draft": False,
            },
        }
        client = RecordingClient()
        handle_pull_request(payload, CONFIG, client)
        self.assertEqual(client.resolved, 0)

    def test_processes_pr(self):
        items = [ProjectItem(issue_number=100, item_id="ITEM_A", current_status="Todo")]
        client = RecordingClient(items_by_pr={7: items})
        payload = {
            "action": "opened",
            "pull_request": {
                "base": {"ref": "staging"},
                "user": {"login": "dev"},
                "title": "feat: add",
                "number": 7,
                "draft": False,
            },
        }
        handle_pull_request(payload, CONFIG, client)
        self.assertEqual(client.updated, [("ITEM_A", "opt-review")])


class HandlePushTests(unittest.TestCase):
    def test_skips_non_production_push(self):
        client = RecordingClient()
        handle_push({"ref": "refs/heads/staging"}, CONFIG, client)
        self.assertEqual(client.resolved, 0)

    def test_skips_when_no_pr_numbers(self):
        client = RecordingClient()
        payload = {"ref": "refs/heads/main", "before": ZERO_SHA, "after": "bbb"}
        with patch("project_sync.read_commit_subjects_between", return_value=["no pr ref here"]):
            handle_push(payload, CONFIG, client)
        self.assertEqual(client.resolved, 0)

    def test_processes_push(self):
        items = [ProjectItem(issue_number=100, item_id="ITEM_A", current_status="QA")]
        client = RecordingClient(items_by_pr={12: items})
        payload = {"ref": "refs/heads/main", "before": "aaa", "after": "bbb"}
        with patch("project_sync.read_commit_subjects_between", return_value=["feat: thing (#12)"]):
            handle_push(payload, CONFIG, client)
        self.assertEqual(client.updated, [("ITEM_A", "opt-done")])


class MainTests(unittest.TestCase):
    def test_main_returns_without_client(self):
        with patch("project_sync.get_client_or_none", return_value=None):
            main()  # no raise

    def test_main_dispatches_pull_request(self):
        client = RecordingClient()
        with patch("project_sync.get_client_or_none", return_value=client), patch(
            "project_sync.read_event_payload", return_value={"action": "edited"}
        ), patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}, clear=True), patch(
            "project_sync.handle_pull_request"
        ) as handler:
            main()
        handler.assert_called_once()

    def test_main_dispatches_push(self):
        client = RecordingClient()
        with patch("project_sync.get_client_or_none", return_value=client), patch(
            "project_sync.read_event_payload", return_value={"ref": "x"}
        ), patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}, clear=True), patch(
            "project_sync.handle_push"
        ) as handler:
            main()
        handler.assert_called_once()

    def test_main_unsupported_event(self):
        client = RecordingClient()
        with patch("project_sync.get_client_or_none", return_value=client), patch(
            "project_sync.read_event_payload", return_value={}
        ), patch.dict(os.environ, {"GITHUB_EVENT_NAME": "issues"}, clear=True):
            main()  # no raise, just prints


class ScriptEntryPointTests(unittest.TestCase):
    """Execute the module as a script to cover the __main__ guard."""

    def _run(self, env):
        full_env = {**os.environ, **env}
        script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "project_sync.py")
        return subprocess.run(
            [sys.executable, script],
            capture_output=True,
            text=True,
            env=full_env,
        )

    def test_exits_zero_when_no_client(self):
        result = self._run({"PROJECTS_TOKEN": "", "GITHUB_REPOSITORY": ""})
        self.assertEqual(result.returncode, 0)
        self.assertIn("skipping project sync", result.stdout)

    def test_exits_one_on_error(self):
        result = self._run(
            {
                "PROJECTS_TOKEN": "tok",
                "GITHUB_REPOSITORY": "a/b",
                "GITHUB_EVENT_PATH": "",
            }
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("GITHUB_EVENT_PATH is not set", result.stderr)


def _ctx(body):
    """Wrap a BytesIO body in a context manager mimicking urlopen()."""

    class _CM:
        def __enter__(self):
            return body

        def __exit__(self, *args):
            return False

    return _CM()


if __name__ == "__main__":
    unittest.main()
