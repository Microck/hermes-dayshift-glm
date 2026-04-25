import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dayshift


class DayshiftParsingTests(unittest.TestCase):
    def test_parse_task_metadata_from_issue_body(self):
        title = "nightshift: test-gap - coverage gaps"
        body = "**Task:** test-gap\n**Category:** analysis\n\nFiles to fix: `test_nightshift.py`"

        task, category = dayshift.parse_task_metadata(title, body)

        self.assertEqual(task, "test-gap")
        self.assertEqual(category, "analysis")

    def test_detects_nightshift_output_from_title_or_body(self):
        self.assertTrue(dayshift.is_nightshift_output("[nightshift] readme-improvements", ""))
        self.assertTrue(dayshift.is_nightshift_output("[dayshift] fix issue 9", "Automated from Nightshift issue #9"))
        self.assertFalse(dayshift.is_nightshift_output("regular bug", "plain report"))


class DayshiftClassificationTests(unittest.TestCase):
    def test_structured_doc_issue_waits_for_approval_in_kanban_mode(self):
        item = dayshift.WorkItem(
            repo="Microck/hermes-nightshift-glm",
            number=9,
            kind="issue",
            title="[nightshift] Documentation drift: inconsistent task counts",
            url="https://example.test/9",
            state="open",
            labels=[],
            body="**Task:** doc-drift\n**Category:** analysis\n### Recommendations\nFiles to fix: `README.md`, `SKILL.md`",
            task="doc-drift",
            category="analysis",
        )

        classification = dayshift.classify_item(item, {"items": {}}, dayshift.DEFAULT_CONFIG)

        self.assertEqual(classification.verdict, "kanban")
        self.assertEqual(classification.risk, "low")
        self.assertGreaterEqual(classification.fixability, 0.8)
        self.assertIn("kanban mode requires user approval", classification.reasons)

    def test_structured_doc_issue_can_auto_fix_when_kanban_mode_is_disabled(self):
        item = dayshift.WorkItem(
            repo="Microck/hermes-nightshift-glm",
            number=9,
            kind="issue",
            title="[nightshift] Documentation drift: inconsistent task counts",
            url="https://example.test/9",
            state="open",
            labels=[],
            body="**Task:** doc-drift\n**Category:** analysis\n### Recommendations\nFiles to fix: `README.md`, `SKILL.md`",
            task="doc-drift",
            category="analysis",
        )

        classification = dayshift.classify_item(item, {"items": {}}, {**dayshift.DEFAULT_CONFIG, "kanban_enabled": False})

        self.assertEqual(classification.verdict, "auto-fix")

    def test_attempt_limit_forces_skip(self):
        item = dayshift.WorkItem(
            repo="Microck/hermes-nightshift-glm",
            number=13,
            kind="issue",
            title="nightshift: test-gap",
            url="https://example.test/13",
            state="open",
            labels=[],
            body="**Task:** test-gap\n### Recommendations\nFiles to fix: `test_nightshift.py`",
            task="test-gap",
            category="analysis",
        )
        state = {"items": {item.key: {"attempts": 2}}}

        classification = dayshift.classify_item(item, state, dayshift.DEFAULT_CONFIG)

        self.assertEqual(classification.verdict, "skip")
        self.assertIn("attempt limit reached: 2", classification.reasons)


class DayshiftMergePolicyTests(unittest.TestCase):
    def test_approved_merge_label_overrides_global_auto_merge_flags(self):
        item = dayshift.WorkItem(
            repo="Microck/hermes-nightshift-glm",
            number=7,
            kind="pr",
            title="[nightshift] readme-improvements",
            url="https://example.test/7",
            state="open",
            labels=["dayshift/merge"],
        )

        self.assertTrue(dayshift.can_auto_merge(item, dayshift.DEFAULT_CONFIG, approved_by_label=True))

    def test_maker_and_implementer_merge_flags_are_independent(self):
        maker_pr = dayshift.WorkItem(
            repo="Microck/hermes-nightshift-glm",
            number=7,
            kind="pr",
            title="[nightshift] readme-improvements",
            url="https://example.test/7",
            state="open",
            labels=[],
            head_ref="nightshift/readme-improvements",
        )
        implementer_pr = dayshift.WorkItem(
            repo="Microck/hermes-nightshift-glm",
            number=14,
            kind="pr",
            title="[dayshift] fix issue 9",
            url="https://example.test/14",
            state="open",
            labels=[],
            head_ref="dayshift/issue-9",
        )
        config = {**dayshift.DEFAULT_CONFIG, "auto_merge_maker_prs": True, "auto_merge_implement_prs": False}

        self.assertTrue(dayshift.can_auto_merge(maker_pr, config, approved_by_label=False))
        self.assertFalse(dayshift.can_auto_merge(implementer_pr, config, approved_by_label=False))


class DayshiftStateTests(unittest.TestCase):
    def test_discover_target_repos_can_use_nightshift_filters(self):
        repos = [
            {
                "full_name": "Microck/active",
                "name": "active",
                "archived": False,
                "fork": False,
                "private": False,
                "pushed_at": "2026-04-20T00:00:00Z",
                "size": 42,
                "language": "Python",
            },
            {
                "full_name": "Microck/tiny",
                "name": "tiny",
                "archived": False,
                "fork": False,
                "private": False,
                "pushed_at": "2026-04-20T00:00:00Z",
                "size": 1,
                "language": "Python",
            },
            {
                "full_name": "Microck/old",
                "name": "old",
                "archived": False,
                "fork": False,
                "private": False,
                "pushed_at": "2020-01-01T00:00:00Z",
                "size": 42,
                "language": "Python",
            },
        ]

        def fake_gh_api(path, config, **kwargs):
            if path.startswith("/user/repos"):
                return repos
            if path.endswith("/pulls?state=open"):
                return []
            return {}

        with patch.object(dayshift, "gh_api", side_effect=fake_gh_api):
            discovered = dayshift.discover_target_repos({**dayshift.DEFAULT_CONFIG, "repo_discovery_mode": "nightshift"})

        self.assertEqual(discovered, ["Microck/active"])

    def test_save_and_load_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original = dayshift.STATE_FILE
            dayshift.STATE_FILE = Path(tmpdir) / "state.json"
            try:
                state = {"items": {"repo#issue-1": {"attempts": 1}}, "events": [], "last_scan": "now"}
                dayshift.save_state(state)

                self.assertEqual(dayshift.load_state(), state)
            finally:
                dayshift.STATE_FILE = original

    def test_render_board_includes_item_and_actions(self):
        state = {
            "items": {
                "repo#issue-1": {
                    "title": "nightshift: docs",
                    "url": "https://example.test/1",
                    "workflow_label": "dayshift/inbox",
                    "human_note": "Maintain the original README style.",
                    "classification": {"verdict": "kanban", "approach": "Review this."},
                }
            }
        }

        markup = dayshift.render_board(state)

        self.assertIn("nightshift: docs", markup)
        self.assertIn("jkanban", markup)
        self.assertIn("dayshift/merge", markup)
        self.assertNotIn('"id": "dayshift/done"', markup)
        self.assertNotIn('"id": "dayshift/skip"', markup)
        self.assertIn("execute: GLM 5.1", markup)
        self.assertIn("execute: GPT 5.3 Codex", markup)
        self.assertIn("Review this.", markup)
        self.assertIn("close-card", markup)
        self.assertIn("Close on GitHub", markup)
        self.assertIn("\\u2715", markup)
        self.assertIn("ignore-card", markup)
        self.assertIn("title='Ignore without closing'>/</button>", markup)
        self.assertIn("note-card", markup)
        self.assertIn("Maintain the original README style.", markup)
        self.assertIn("/note", markup)
        self.assertIn("Ignore this item in Dayshift without closing it on GitHub?", markup)
        self.assertIn("bulk-move", markup)
        self.assertIn("select-visible", markup)
        self.assertIn("clear-selection", markup)
        self.assertIn("Search cards", markup)
        self.assertIn("risk-unknown", markup)
        self.assertIn("kanban-board[data-id='dayshift/pr-inbox']", markup)

    def test_render_board_orders_execution_lanes_before_ready_and_merge(self):
        markup = dayshift.render_board({"items": {}}, dayshift.DEFAULT_CONFIG)

        issue_inbox_index = markup.index('"id": "dayshift/issue-inbox"')
        pr_inbox_index = markup.index('"id": "dayshift/pr-inbox"')
        glm_index = markup.index('"id": "dayshift/execute-glm-5-1"')
        codex_index = markup.index('"id": "dayshift/execute-gpt-5-3-codex"')
        ready_index = markup.index('"id": "dayshift/ready"')
        merge_index = markup.index('"id": "dayshift/merge"')

        self.assertLess(issue_inbox_index, pr_inbox_index)
        self.assertLess(pr_inbox_index, glm_index)
        self.assertLess(glm_index, codex_index)
        self.assertLess(codex_index, ready_index)
        self.assertLess(ready_index, merge_index)

    def test_render_board_sorts_cards_newest_first_within_column(self):
        state = {
            "items": {
                "repo#issue-1": {
                    "title": "older",
                    "url": "https://example.test/1",
                    "workflow_label": "dayshift/issue-inbox",
                    "updated_at": "2026-04-01T00:00:00Z",
                    "classification": {"verdict": "kanban", "risk": "low", "approach": "Old."},
                },
                "repo#issue-2": {
                    "title": "newer",
                    "url": "https://example.test/2",
                    "workflow_label": "dayshift/issue-inbox",
                    "updated_at": "2026-04-20T00:00:00Z",
                    "classification": {"verdict": "kanban", "risk": "low", "approach": "New."},
                },
            }
        }

        markup = dayshift.render_board(state)

        self.assertLess(markup.index(">newer</a>"), markup.index(">older</a>"))

    def test_render_board_hides_ignored_items(self):
        state = {
            "items": {
                "repo#issue-1": {
                    "title": "visible item",
                    "url": "https://example.test/1",
                    "workflow_label": "dayshift/issue-inbox",
                    "classification": {"verdict": "kanban", "risk": "low", "approach": "Show."},
                },
                "repo#issue-2": {
                    "title": "ignored item",
                    "url": "https://example.test/2",
                    "workflow_label": "dayshift/issue-inbox",
                    "ignored_by_dayshift": True,
                    "classification": {"verdict": "kanban", "risk": "low", "approach": "Hide."},
                },
            }
        }

        markup = dayshift.render_board(state)

        self.assertIn("visible item", markup)
        self.assertNotIn("ignored item", markup)

    def test_render_board_hides_terminal_and_non_error_failed_items(self):
        state = {
            "items": {
                "repo#issue-1": {
                    "title": "done item",
                    "url": "https://example.test/1",
                    "workflow_label": "dayshift/done",
                    "closed_by_dayshift": True,
                    "classification": {"verdict": "kanban", "risk": "low", "approach": "Done."},
                },
                "repo#issue-2": {
                    "title": "skip item",
                    "url": "https://example.test/2",
                    "workflow_label": "dayshift/skip",
                    "classification": {"verdict": "skip", "risk": "low", "approach": "Skip."},
                },
                "repo#issue-3": {
                    "title": "label sync noise",
                    "url": "https://example.test/3",
                    "workflow_label": "dayshift/failed",
                    "label_sync_error": "gh: Label does not exist (HTTP 404)",
                    "classification": {"verdict": "kanban", "risk": "low", "approach": "No real failure."},
                },
                "repo#issue-4": {
                    "title": "real failed item",
                    "url": "https://example.test/4",
                    "workflow_label": "dayshift/failed",
                    "last_error": "validation failed",
                    "classification": {"verdict": "kanban", "risk": "medium", "approach": "Needs inspection."},
                },
            }
        }

        markup = dayshift.render_board(state)

        self.assertNotIn("done item", markup)
        self.assertNotIn("skip item", markup)
        self.assertNotIn("label sync noise", markup)
        self.assertIn("real failed item", markup)

    def test_render_board_hides_ready_and_merge_when_both_automerge_modes_enabled(self):
        config = {
            **dayshift.DEFAULT_CONFIG,
            "auto_merge_implement_prs": True,
            "auto_merge_maker_prs": True,
        }

        markup = dayshift.render_board({"items": {}}, config)

        self.assertNotIn('"id": "dayshift/ready"', markup)
        self.assertNotIn('"id": "dayshift/merge"', markup)
        self.assertIn('"id": "dayshift/execute-glm-5-1"', markup)

    def test_settings_page_exposes_execution_lanes_and_core_options(self):
        markup = dayshift.render_settings(dayshift.DEFAULT_CONFIG)

        self.assertIn("execution_lanes", markup)
        self.assertIn("auto_merge_maker_prs", markup)
        self.assertIn("kanban_enabled", markup)
        self.assertIn("repo_discovery_mode", markup)
        self.assertIn("scheduler_enabled", markup)
        self.assertIn("glm_quota_command", markup)
        self.assertIn("Execute & merge directly afterwards", markup)
        self.assertIn("Wait for GLM quota window", markup)
        self.assertIn("Reasoning effort", markup)

    def test_parse_settings_form_updates_execution_lanes(self):
        form = {
            "target_repos": "Microck/hermes-nightshift-glm",
            "repo_discovery_mode": "nightshift",
            "exclude_repos": "scratch\n*-backup",
            "public_only": "on",
            "kanban_enabled": "on",
            "github_token_file": "~/.dayshift/token",
            "nightshift_token_file": "~/.nightshift/token",
            "max_attempts": "3",
            "max_inactive_days": "14",
            "min_size_kb": "20",
            "max_repos_to_consider": "7",
            "max_prs_per_repo": "1",
            "scheduler_enabled": "on",
            "scheduler_interval_seconds": "9",
            "glm_quota_command": "true",
            "lane_0_run_policy": "immediate",
            "lane_0_reasoning_effort": "high",
            "lane_0_execute_and_merge": "on",
            "agent_command": "codex exec --model {model} --prompt-file {prompt}",
            "merge_method": "squash",
            "validation_commands": "python3 -m unittest",
            "execution_lanes": '[{"label":"dayshift/execute-custom","title":"execute: custom","model":"custom-model","agent_command":"custom-agent {prompt}","run_policy":"glm_quota_window","execute_and_merge":false}]',
        }

        config = dayshift.parse_settings_form(form, dayshift.DEFAULT_CONFIG)

        self.assertEqual(config["target_repos"], ["Microck/hermes-nightshift-glm"])
        self.assertEqual(config["repo_discovery_mode"], "nightshift")
        self.assertEqual(config["exclude_repos"], ["scratch", "*-backup"])
        self.assertEqual(config["max_attempts"], 3)
        self.assertEqual(config["max_inactive_days"], 14)
        custom_lane = next(lane for lane in config["execution_lanes"] if lane["label"] == "dayshift/execute-custom")
        self.assertEqual(custom_lane["model"], "custom-model")
        self.assertEqual(config["scheduler_interval_seconds"], 9)
        self.assertEqual(config["glm_quota_command"], "true")
        self.assertEqual(custom_lane["reasoning_effort"], "high")
        self.assertTrue(custom_lane["execute_and_merge"])

    def test_load_config_ignores_null_scheduler_values_and_restores_default_agent_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original = dayshift.CONFIG_FILE
            dayshift.CONFIG_FILE = Path(tmpdir) / "config.json"
            try:
                dayshift.save_json_file(
                    dayshift.CONFIG_FILE,
                    {
                        "scheduler_enabled": None,
                        "scheduler_interval_seconds": None,
                        "agent_command": "",
                        "execution_lanes": [
                            {
                                "label": "dayshift/execute-gpt-5-3-codex",
                                "title": "execute: GPT 5.3 Codex",
                                "model": "gpt-5.3-codex",
                                "agent_command": "",
                            }
                        ],
                    },
                )

                config = dayshift.load_config()

                self.assertTrue(config["scheduler_enabled"])
                self.assertEqual(config["scheduler_interval_seconds"], 30)
                self.assertIn("codex exec", config["agent_command"])
                selected = dayshift.config_for_execution_label(config, "dayshift/execute-gpt-5-3-codex")
                self.assertIn("codex exec", selected["agent_command"])
                self.assertEqual(selected["selected_reasoning_effort"], "high")
                glm = dayshift.config_for_execution_label(config, "dayshift/execute-glm-5-1")
                self.assertIn("hermes-agent-runner.py", glm["agent_command"])
                self.assertNotIn("codex exec", glm["agent_command"])
            finally:
                dayshift.CONFIG_FILE = original

    def test_save_config_persists_normalized_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original = dayshift.CONFIG_FILE
            dayshift.CONFIG_FILE = Path(tmpdir) / "config.json"
            try:
                dayshift.save_config(
                    {
                        **dayshift.DEFAULT_CONFIG,
                        "scheduler_enabled": None,
                        "scheduler_interval_seconds": None,
                        "agent_command": "",
                        "execution_lanes": [
                            {
                                "label": "dayshift/execute-gpt-5-3-codex",
                                "title": "execute: GPT 5.3 Codex",
                                "model": "gpt-5.3-codex",
                                "agent_command": "",
                            }
                        ],
                    }
                )

                persisted = dayshift.load_json_file(dayshift.CONFIG_FILE, {})

                self.assertTrue(persisted["scheduler_enabled"])
                self.assertEqual(persisted["scheduler_interval_seconds"], 30)
                self.assertEqual(persisted["agent_command"], dayshift.DEFAULT_CONFIG["agent_command"])
                self.assertEqual(
                    persisted["execution_lanes"][0]["agent_command"],
                    dayshift.DEFAULT_CONFIG["execution_lanes"][0]["agent_command"],
                )
            finally:
                dayshift.CONFIG_FILE = original

    def test_default_inbox_label_splits_issues_and_prs(self):
        issue = dayshift.WorkItem("repo/name", 1, "issue", "issue", "", "open", [])
        pr = dayshift.WorkItem("repo/name", 2, "pr", "pr", "", "open", [])

        self.assertEqual(dayshift.default_inbox_label(issue), "dayshift/issue-inbox")
        self.assertEqual(dayshift.default_inbox_label(pr), "dayshift/pr-inbox")

    def test_summarize_item_prefers_substantive_body_line(self):
        summary = dayshift.summarize_item(
            "nightshift: test-gap",
            "**Task:** test-gap\n\nThe repository has very low test coverage around quota scheduling.",
            {"approach": "Show in the web UI for approval before creating a fix PR."},
        )

        self.assertIn("very low test coverage", summary)

    def test_close_work_item_closes_github_item_and_updates_state(self):
        item = dayshift.WorkItem(
            repo="repo/name",
            number=7,
            kind="issue",
            title="nightshift: docs",
            url="https://example.test/7",
            state="open",
            labels=[],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            original = dayshift.STATE_FILE
            dayshift.STATE_FILE = Path(tmpdir) / "state.json"
            try:
                with patch.object(dayshift, "gh_api", return_value={}) as gh_api:
                    result = dayshift.close_work_item(item, dayshift.DEFAULT_CONFIG)

                gh_api.assert_called_once_with("/repos/repo/name/issues/7", dayshift.DEFAULT_CONFIG, method="PATCH", fields={"state": "closed"})
                self.assertEqual(result["status"], "closed")
                self.assertEqual(dayshift.load_state()["items"][item.key]["workflow_label"], "dayshift/done")
            finally:
                dayshift.STATE_FILE = original

    def test_set_workflow_label_posts_labels_array_payload(self):
        item = dayshift.WorkItem(
            repo="repo/name",
            number=7,
            kind="issue",
            title="nightshift: docs",
            url="https://example.test/7",
            state="open",
            labels=[],
        )

        with patch.object(dayshift, "ensure_dayshift_labels", return_value=None), \
             patch.object(dayshift, "gh_api", return_value={}) as gh_api:
            dayshift.set_workflow_label(item, "dayshift/issue-inbox", dayshift.DEFAULT_CONFIG)

        gh_api.assert_called_once_with(
            "/repos/repo/name/issues/7/labels",
            dayshift.DEFAULT_CONFIG,
            method="POST",
            json_body={"labels": ["dayshift/issue-inbox"]},
        )

    def test_set_workflow_label_ignores_missing_old_label_during_delete(self):
        item = dayshift.WorkItem(
            repo="repo/name",
            number=7,
            kind="issue",
            title="nightshift: docs",
            url="https://example.test/7",
            state="open",
            labels=["dayshift/failed"],
        )

        def fake_gh_api(path, config, **kwargs):
            if kwargs.get("method") == "DELETE":
                raise RuntimeError("gh: Label does not exist (HTTP 404)")
            return {}

        with patch.object(dayshift, "ensure_dayshift_labels", return_value=None), \
             patch.object(dayshift, "gh_api", side_effect=fake_gh_api) as gh_api:
            dayshift.set_workflow_label(item, "dayshift/issue-inbox", dayshift.DEFAULT_CONFIG)

        calls = [call.args[0] for call in gh_api.call_args_list]
        self.assertIn("/repos/repo/name/issues/7/labels/dayshift%2Ffailed", calls)
        self.assertIn("/repos/repo/name/issues/7/labels", calls)

    def test_save_human_note_persists_executor_guidance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original = dayshift.STATE_FILE
            dayshift.STATE_FILE = Path(tmpdir) / "state.json"
            try:
                dayshift.save_state({
                    "items": {
                        "repo/name#issue-1": {
                            "repo": "repo/name",
                            "number": 1,
                            "kind": "issue",
                            "title": "nightshift: docs",
                            "url": "https://example.test/1",
                            "workflow_label": "dayshift/issue-inbox",
                        }
                    },
                    "events": [],
                    "last_scan": None,
                })

                result = dayshift.save_human_note("repo/name#issue-1", " Maintain the README style. ")

                record = dayshift.load_state()["items"]["repo/name#issue-1"]
                self.assertEqual(result["human_note"], "Maintain the README style.")
                self.assertEqual(record["human_note"], "Maintain the README style.")
                self.assertIn("human_note_updated_at", record)
            finally:
                dayshift.STATE_FILE = original

    def test_ignore_work_item_marks_local_state_without_closing_github(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original = dayshift.STATE_FILE
            dayshift.STATE_FILE = Path(tmpdir) / "state.json"
            try:
                dayshift.save_state({
                    "items": {
                        "repo/name#issue-1": {
                            "repo": "repo/name",
                            "number": 1,
                            "kind": "issue",
                            "title": "nightshift: docs",
                            "url": "https://example.test/1",
                            "workflow_label": "dayshift/execute-gpt-5-3-codex",
                        }
                    },
                    "events": [],
                    "last_scan": None,
                })

                with patch.object(dayshift, "gh_api") as gh_api:
                    result = dayshift.ignore_work_item("repo/name#issue-1")

                record = dayshift.load_state()["items"]["repo/name#issue-1"]
                gh_api.assert_not_called()
                self.assertEqual(result["status"], "ignored")
                self.assertTrue(record["ignored_by_dayshift"])
                self.assertIn("ignored_at", record)
                self.assertEqual(record["workflow_label"], "dayshift/execute-gpt-5-3-codex")
            finally:
                dayshift.STATE_FILE = original

    def test_sync_scan_keeps_local_workflow_label_when_github_label_is_stale(self):
        item = dayshift.WorkItem(
            repo="repo/name",
            number=1,
            kind="issue",
            title="nightshift: docs",
            url="https://example.test/1",
            state="open",
            labels=["dayshift/issue-inbox"],
            body="Fix docs.",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            original = dayshift.STATE_FILE
            dayshift.STATE_FILE = Path(tmpdir) / "state.json"
            try:
                dayshift.save_state({
                    "items": {
                        item.key: {
                            "repo": item.repo,
                            "number": item.number,
                            "kind": item.kind,
                            "title": item.title,
                            "url": item.url,
                            "workflow_label": "dayshift/execute-gpt-5-3-codex",
                            "last_moved_at": "2026-04-25T20:00:00Z",
                        }
                    },
                    "events": [],
                    "last_scan": None,
                })

                with patch.object(dayshift, "discover_target_repos", return_value=["repo/name"]), \
                     patch.object(dayshift, "scan_repo", return_value=[item]):
                    dayshift.sync_scan(dayshift.DEFAULT_CONFIG)

                record = dayshift.load_state()["items"][item.key]
                self.assertEqual(record["workflow_label"], "dayshift/execute-gpt-5-3-codex")
            finally:
                dayshift.STATE_FILE = original

    def test_bulk_move_work_items_updates_selected_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original = dayshift.STATE_FILE
            dayshift.STATE_FILE = Path(tmpdir) / "state.json"
            try:
                dayshift.save_state({
                    "items": {
                        "repo/name#issue-1": {
                            "repo": "repo/name",
                            "number": 1,
                            "kind": "issue",
                            "title": "nightshift: docs",
                            "url": "https://example.test/1",
                            "workflow_label": "dayshift/issue-inbox",
                        }
                    },
                    "events": [],
                    "last_scan": None,
                })
                with patch.object(dayshift, "set_workflow_label", return_value=None):
                    outcomes = dayshift.bulk_move_work_items(["repo/name#issue-1"], "dayshift/skip", dayshift.DEFAULT_CONFIG)

                self.assertEqual(outcomes[0]["status"], "moved")
                self.assertEqual(dayshift.load_state()["items"]["repo/name#issue-1"]["workflow_label"], "dayshift/skip")
            finally:
                dayshift.STATE_FILE = original


class DayshiftRunPolicyTests(unittest.TestCase):
    def test_kanban_mode_does_not_execute_auto_fix_verdict(self):
        item = dayshift.WorkItem(
            repo="repo/name",
            number=1,
            kind="issue",
            title="nightshift: docs",
            url="https://example.test/1",
            state="open",
            labels=[],
        )
        classification = dayshift.Classification("auto-fix", 0.9, 0.8, "small", "low", "Fix docs.")

        with patch.object(dayshift, "sync_scan", return_value=([item], {item.key: classification})), \
             patch.object(dayshift, "load_state", return_value={"items": {item.key: {"workflow_label": "dayshift/inbox"}}}), \
             patch.object(dayshift, "act_on_item") as act_on_item:
            outcomes = dayshift.run_ready_items({**dayshift.DEFAULT_CONFIG, "kanban_enabled": True})

        self.assertEqual(outcomes, [])
        act_on_item.assert_not_called()

    def test_auto_fix_can_execute_when_kanban_mode_is_disabled(self):
        item = dayshift.WorkItem(
            repo="repo/name",
            number=1,
            kind="issue",
            title="nightshift: docs",
            url="https://example.test/1",
            state="open",
            labels=[],
        )
        classification = dayshift.Classification("auto-fix", 0.9, 0.8, "small", "low", "Fix docs.")

        with patch.object(dayshift, "sync_scan", return_value=([item], {item.key: classification})), \
             patch.object(dayshift, "load_state", return_value={"items": {item.key: {"workflow_label": "dayshift/inbox"}}}), \
             patch.object(dayshift, "act_on_item", return_value={"status": "done"}) as act_on_item:
            outcomes = dayshift.run_ready_items({**dayshift.DEFAULT_CONFIG, "kanban_enabled": False})

        self.assertEqual(outcomes, [{"item": item.key, "status": "done"}])
        act_on_item.assert_called_once()

    def test_issue_in_execution_lane_runs_with_that_lane(self):
        item = dayshift.WorkItem(
            repo="repo/name",
            number=1,
            kind="issue",
            title="nightshift: docs",
            url="https://example.test/1",
            state="open",
            labels=[],
        )
        classification = dayshift.Classification("kanban", 0.9, 0.8, "small", "low", "Fix docs.")
        lane = "dayshift/execute-gpt-5-3-codex"

        with patch.object(dayshift, "sync_scan", return_value=([item], {item.key: classification})), \
             patch.object(dayshift, "load_state", return_value={"items": {item.key: {"workflow_label": lane}}}), \
             patch.object(dayshift, "act_on_item", return_value={"status": "done"}) as act_on_item:
            outcomes = dayshift.run_ready_items(dayshift.DEFAULT_CONFIG)

        self.assertEqual(outcomes, [{"item": item.key, "status": "done"}])
        act_on_item.assert_called_once_with(item, classification, dayshift.DEFAULT_CONFIG, lane)

    def test_ignored_execution_lane_item_does_not_run(self):
        item = dayshift.WorkItem(
            repo="repo/name",
            number=1,
            kind="issue",
            title="nightshift: docs",
            url="https://example.test/1",
            state="open",
            labels=[],
        )
        classification = dayshift.Classification("kanban", 0.9, 0.8, "small", "low", "Fix docs.")
        lane = "dayshift/execute-gpt-5-3-codex"

        with patch.object(dayshift, "sync_scan", return_value=([item], {item.key: classification})), \
             patch.object(dayshift, "load_state", return_value={"items": {item.key: {"workflow_label": lane, "ignored_by_dayshift": True}}}), \
             patch.object(dayshift, "act_on_item") as act_on_item:
            outcomes = dayshift.run_ready_items(dayshift.DEFAULT_CONFIG)

        self.assertEqual(outcomes, [])
        act_on_item.assert_not_called()

    def test_execution_lane_config_selects_model_and_command(self):
        config = {
            **dayshift.DEFAULT_CONFIG,
            "agent_command": "fallback-agent",
            "execution_lanes": [
                {
                    "label": "dayshift/execute-custom",
                    "title": "execute: custom",
                    "model": "custom-model",
                    "agent_command": "custom-agent --model {model} --prompt {prompt}",
                }
            ],
        }

        selected = dayshift.config_for_execution_label(config, "dayshift/execute-custom")

        self.assertEqual(selected["selected_model"], "custom-model")
        self.assertEqual(selected["agent_command"], "custom-agent --model {model} --prompt {prompt}")

    def test_default_codex_lane_uses_high_reasoning(self):
        selected = dayshift.config_for_execution_label(dayshift.DEFAULT_CONFIG, "dayshift/execute-gpt-5-3-codex")

        self.assertEqual(selected["selected_reasoning_effort"], "high")

    def test_default_glm_lane_uses_hermes_agent(self):
        selected = dayshift.config_for_execution_label(dayshift.DEFAULT_CONFIG, "dayshift/execute-glm-5-1")

        self.assertEqual(selected["selected_model"], "glm-5.1")
        self.assertIn("hermes-agent-runner.py", selected["agent_command"])
        self.assertIn("--provider zai", selected["agent_command"])
        self.assertNotIn("codex exec", selected["agent_command"])

    def test_default_codex_command_reads_prompt_from_stdin(self):
        item = dayshift.WorkItem(
            repo="repo/name",
            number=1,
            kind="issue",
            title="nightshift: docs",
            url="https://example.test/1",
            state="open",
            labels=[],
            body="Fix the docs.",
        )
        classification = dayshift.Classification("kanban", 0.9, 0.8, "small", "low", "Fix docs.")
        config = {
            **dayshift.config_for_execution_label(dayshift.DEFAULT_CONFIG, "dayshift/execute-gpt-5-3-codex"),
            "human_note": "Maintain the original README style.",
        }

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(dayshift, "run_command") as run_command:
            run_command.return_value.returncode = 0

            dayshift.run_agent(item, classification, Path(tmpdir), config)

        args, kwargs = run_command.call_args
        self.assertEqual(args[0], ["codex", "exec", "--model", "gpt-5.3-codex", "-"])
        self.assertIn("URL: https://example.test/1", kwargs["input_text"])
        self.assertIn("Fix the issue or PR linked above.", kwargs["input_text"])
        self.assertIn("Fix the docs.", kwargs["input_text"])
        self.assertIn("Human guidance:", kwargs["input_text"])
        self.assertIn("Maintain the original README style.", kwargs["input_text"])
        self.assertEqual(kwargs["cwd"], Path(tmpdir))

    def test_validation_detects_python_tests_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checkout = Path(tmpdir)
            tests_dir = checkout / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_client.py").write_text("import unittest\n\nclass ClientTests(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n")

            commands = dayshift.validation_commands_for_checkout(checkout, dayshift.DEFAULT_CONFIG)

        self.assertEqual(commands, ["python3 -m unittest discover"])

    def test_validation_detects_cmake_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checkout = Path(tmpdir)
            (checkout / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.16)\nproject(example)\n")

            commands = dayshift.validation_commands_for_checkout(checkout, dayshift.DEFAULT_CONFIG)

        self.assertEqual(commands, ["cmake -S . -B build", "cmake --build build", "ctest --test-dir build --output-on-failure"])

    def test_prepare_checkout_installs_lockfile_dependencies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checkout = Path(tmpdir)
            (checkout / "package.json").write_text('{"scripts":{"test":"echo ok"}}')
            (checkout / "yarn.lock").write_text("# lockfile")

            with patch.object(dayshift, "run_command") as run_command:
                run_command.return_value.returncode = 0
                run_command.return_value.stdout = ""
                run_command.return_value.stderr = ""
                dayshift.prepare_checkout(checkout, dayshift.DEFAULT_CONFIG)

        run_command.assert_called_once_with(["yarn", "install", "--frozen-lockfile"], cwd=checkout)

    def test_scheduler_waits_for_glm_quota_window(self):
        item = dayshift.WorkItem(
            repo="repo/name",
            number=1,
            kind="issue",
            title="nightshift: docs",
            url="https://example.test/1",
            state="open",
            labels=[],
        )
        classification = dayshift.Classification("kanban", 0.9, 0.8, "small", "low", "Fix docs.")
        lane = "dayshift/execute-glm-5-1"

        with tempfile.TemporaryDirectory() as tmpdir:
            original = dayshift.STATE_FILE
            dayshift.STATE_FILE = Path(tmpdir) / "state.json"
            try:
                dayshift.save_state({"items": {item.key: {"workflow_label": lane}}, "events": [], "last_scan": None})
                with patch.object(dayshift, "sync_scan", return_value=([item], {item.key: classification})), \
                     patch.object(dayshift, "glm_quota_window_open", return_value=(False, "not quota time")), \
                     patch.object(dayshift, "act_on_item") as act_on_item:
                    outcomes = dayshift.run_ready_items(dayshift.DEFAULT_CONFIG, respect_run_policy=True)

                self.assertEqual(outcomes, [])
                act_on_item.assert_not_called()
                self.assertEqual(dayshift.load_state()["items"][item.key]["scheduler_waiting"], "not quota time")
            finally:
                dayshift.STATE_FILE = original

    def test_glm_quota_skip_output_closes_window_even_with_zero_exit(self):
        with patch.object(dayshift, "run_command") as run_command:
            run_command.return_value.returncode = 0
            run_command.return_value.stdout = "SKIP: Token quota 12% used, resets in 214min — too early"
            run_command.return_value.stderr = ""

            open_window, reason = dayshift.glm_quota_window_open(dayshift.DEFAULT_CONFIG)

        self.assertFalse(open_window)
        self.assertIn("too early", reason)

    def test_scheduler_runs_immediate_lane_without_quota_check(self):
        item = dayshift.WorkItem(
            repo="repo/name",
            number=1,
            kind="issue",
            title="nightshift: docs",
            url="https://example.test/1",
            state="open",
            labels=[],
        )
        classification = dayshift.Classification("kanban", 0.9, 0.8, "small", "low", "Fix docs.")
        lane = "dayshift/execute-gpt-5-3-codex"

        with tempfile.TemporaryDirectory() as tmpdir:
            original = dayshift.STATE_FILE
            dayshift.STATE_FILE = Path(tmpdir) / "state.json"
            try:
                dayshift.save_state({"items": {item.key: {"workflow_label": lane}}, "events": [], "last_scan": None})
                with patch.object(dayshift, "sync_scan", return_value=([item], {item.key: classification})), \
                     patch.object(dayshift, "glm_quota_window_open") as quota, \
                     patch.object(dayshift, "act_on_item", return_value={"status": "done"}) as act_on_item:
                    outcomes = dayshift.run_ready_items(dayshift.DEFAULT_CONFIG, respect_run_policy=True)

                self.assertEqual(outcomes, [{"item": item.key, "status": "done"}])
                quota.assert_not_called()
                act_on_item.assert_called_once_with(item, classification, dayshift.DEFAULT_CONFIG, lane)
            finally:
                dayshift.STATE_FILE = original


if __name__ == "__main__":
    unittest.main()
