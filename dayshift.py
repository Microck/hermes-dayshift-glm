#!/usr/bin/env python3
"""
Dayshift v1: companion implementer for hermes-nightshift-glm.

Dayshift scans GitHub issues and PRs created by Nightshift, classifies the
work, tracks human approval with GitHub labels, and optionally repairs or
merges PRs under explicit policy gates.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


STATE_DIR = Path(os.environ.get("DAYSHIFT_STATE_DIR", os.path.expanduser("~/.dayshift")))
WORKSPACE = Path(os.environ.get("DAYSHIFT_WORKSPACE", os.path.expanduser("~/dayshift-workspace")))
STATE_FILE = STATE_DIR / "state.json"
CONFIG_FILE = STATE_DIR / "config.json"
SCHEDULER_LOCK = threading.Lock()
SCHEDULER_THREAD: threading.Thread | None = None
CODEX_AGENT_COMMAND = "codex exec --model {model} -"
HERMES_GLM_AGENT_COMMAND = (
    f"python3 {Path(__file__).resolve().parent / 'scripts' / 'hermes-agent-runner.py'} "
    "--provider zai --model {model} --prompt {prompt}"
)

DAYSHIFT_LABELS = {
    "dayshift/inbox",
    "dayshift/issue-inbox",
    "dayshift/pr-inbox",
    "dayshift/ready",
    "dayshift/in-progress",
    "dayshift/merge",
    "dayshift/done",
    "dayshift/skip",
    "dayshift/failed",
}

LABEL_COLORS = {
    "dayshift/inbox": "ededed",
    "dayshift/issue-inbox": "ededed",
    "dayshift/pr-inbox": "cfd3d7",
    "dayshift/ready": "0e8a16",
    "dayshift/in-progress": "fbca04",
    "dayshift/merge": "5319e7",
    "dayshift/done": "1d76db",
    "dayshift/skip": "cfd3d7",
    "dayshift/failed": "b60205",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "target_repos": [],
    "repo_discovery_mode": "dayshift",
    "exclude_repos": [
        "*-backup", "pi-backup", "testweb", "gitlab-acc-creator",
        "opencode-gitlab-multi-pat", "lucidus-45", "lucid-track-span",
        "chalcopyrite", "Microck", "Celeste-QuarziteSkin",
    ],
    "public_only": True,
    "max_inactive_days": 30,
    "min_size_kb": 10,
    "max_repos_to_consider": 30,
    "max_prs_per_repo": 2,
    "reuse_nightshift_token": False,
    "github_token_file": "~/.dayshift/.gh-token-dayshift",
    "nightshift_token_file": "~/.nightshift/.gh-token-nightshift",
    "auto_merge_implement_prs": False,
    "auto_merge_maker_prs": False,
    "kanban_enabled": True,
    "max_attempts": 2,
    "agent_command": CODEX_AGENT_COMMAND,
    "execution_lanes": [
        {
            "label": "dayshift/execute-glm-5-1",
            "title": "execute: GLM 5.1",
            "model": "glm-5.1",
            "agent_command": HERMES_GLM_AGENT_COMMAND,
            "run_policy": "glm_quota_window",
            "reasoning_effort": "medium",
            "execute_and_merge": False,
        },
        {
            "label": "dayshift/execute-gpt-5-3-codex",
            "title": "execute: GPT 5.3 Codex",
            "model": "gpt-5.3-codex",
            "agent_command": "",
            "run_policy": "immediate",
            "reasoning_effort": "high",
            "execute_and_merge": False,
        },
    ],
    "scheduler_enabled": True,
    "scheduler_interval_seconds": 30,
    "glm_quota_command": "python3 ~/nightshift-workspace/glm_quota.py --check",
    "validation_commands": [],
    "merge_method": "squash",
}


@dataclass
class WorkItem:
    repo: str
    number: int
    kind: str
    title: str
    url: str
    state: str
    labels: list[str]
    body: str = ""
    task: str | None = None
    category: str | None = None
    author_association: str | None = None
    head_ref: str | None = None
    head_repo: str | None = None
    base_ref: str | None = None
    mergeable_state: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.kind}-{self.number}"


@dataclass
class Classification:
    verdict: str
    fixability: float
    confidence: float
    effort: str
    risk: str
    approach: str
    reasons: list[str] = field(default_factory=list)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def save_json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with open(tmp_path, "w") as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


def save_config(config: dict[str, Any]) -> None:
    normalized = normalize_config(config)
    persisted = {key: normalized.get(key, DEFAULT_CONFIG[key]) for key in DEFAULT_CONFIG}
    save_json_file(CONFIG_FILE, persisted)


def merge_default_execution_lanes(saved_lanes: Any) -> list[dict[str, Any]]:
    if not isinstance(saved_lanes, list):
        return DEFAULT_CONFIG["execution_lanes"]

    defaults_by_label = {lane["label"]: lane for lane in DEFAULT_CONFIG["execution_lanes"]}
    saved_by_label = {
        lane["label"]: lane
        for lane in saved_lanes
        if isinstance(lane, dict) and lane.get("label") in defaults_by_label
    }
    merged_lanes = []
    for default_lane in DEFAULT_CONFIG["execution_lanes"]:
        saved_lane = saved_by_label.get(default_lane["label"], {})
        merged_lane = default_lane.copy()
        merged_lane.update(
            {
                key: value
                for key, value in saved_lane.items()
                if value is not None and not (key == "agent_command" and value == "")
            }
        )
        merged_lanes.append(merged_lane)

    for saved_lane in saved_lanes:
        if not isinstance(saved_lane, dict):
            merged_lanes.append(saved_lane)
            continue
        if saved_lane.get("label") in defaults_by_label:
            continue
        merged_lane = {}
        merged_lane.update(
            {
                key: value
                for key, value in saved_lane.items()
                if value is not None and not (key == "agent_command" and value == "")
            }
        )
        merged_lanes.append(merged_lane)
    return merged_lanes


def normalize_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    if isinstance(raw_config, dict):
        for key, value in raw_config.items():
            if value is None:
                continue
            if key == "agent_command" and value == "":
                continue
            if key == "execution_lanes":
                config[key] = merge_default_execution_lanes(value)
                continue
            config[key] = value
    return config


def load_config() -> dict[str, Any]:
    config = normalize_config(load_json_file(CONFIG_FILE, {}))

    env_repos = os.environ.get("DAYSHIFT_TARGET_REPOS", "").strip()
    if env_repos:
        config["target_repos"] = [repo.strip() for repo in env_repos.split(",") if repo.strip()]

    if not isinstance(config.get("target_repos"), list):
        raise ValueError("target_repos must be a list")
    if config.get("repo_discovery_mode") not in {"dayshift", "nightshift"}:
        raise ValueError("repo_discovery_mode must be dayshift or nightshift")
    for field in ("max_inactive_days", "min_size_kb", "max_repos_to_consider", "max_prs_per_repo"):
        if not isinstance(config.get(field), int) or config[field] < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    if not isinstance(config.get("public_only"), bool):
        raise ValueError("public_only must be true or false")
    if not isinstance(config.get("exclude_repos"), list) or any(not isinstance(item, str) for item in config["exclude_repos"]):
        raise ValueError("exclude_repos must be a list of strings")
    if not isinstance(config.get("execution_lanes"), list):
        raise ValueError("execution_lanes must be a list")
    for lane in config.get("execution_lanes", []):
        if not isinstance(lane, dict):
            raise ValueError("each execution lane must be an object")
        if not lane.get("label") or not str(lane["label"]).startswith("dayshift/execute-"):
            raise ValueError("execution lane labels must start with dayshift/execute-")
        if not lane.get("title"):
            raise ValueError("execution lane title is required")
        if not lane.get("model"):
            raise ValueError("execution lane model is required")
        if lane.get("run_policy", "immediate") not in {"immediate", "glm_quota_window"}:
            raise ValueError("execution lane run_policy must be immediate or glm_quota_window")
        if lane.get("reasoning_effort", "medium") not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("execution lane reasoning_effort must be low, medium, high, or xhigh")
    if not isinstance(config.get("scheduler_enabled"), bool):
        raise ValueError("scheduler_enabled must be true or false")
    if not isinstance(config.get("scheduler_interval_seconds"), int) or config["scheduler_interval_seconds"] < 1:
        raise ValueError("scheduler_interval_seconds must be a positive integer")
    if config.get("merge_method") not in {"merge", "squash", "rebase"}:
        raise ValueError("merge_method must be one of: merge, squash, rebase")
    return config


def execution_lanes(config: dict[str, Any]) -> list[dict[str, str]]:
    lanes = []
    for lane in config.get("execution_lanes", []):
        lanes.append(
            {
                "label": str(lane["label"]),
                "title": str(lane["title"]),
                "model": str(lane["model"]),
                "agent_command": str(lane.get("agent_command") or config.get("agent_command") or ""),
                "run_policy": str(lane.get("run_policy") or ("glm_quota_window" if str(lane["model"]).startswith("glm") else "immediate")),
                "reasoning_effort": str(lane.get("reasoning_effort") or ("high" if str(lane["model"]) == "gpt-5.3-codex" else "medium")),
                "execute_and_merge": bool(lane.get("execute_and_merge", False)),
            }
        )
    return lanes


def execution_lane_labels(config: dict[str, Any]) -> set[str]:
    return {lane["label"] for lane in execution_lanes(config)}


def workflow_labels(config: dict[str, Any]) -> set[str]:
    return DAYSHIFT_LABELS | execution_lane_labels(config)


def label_color(label: str) -> str:
    if label.startswith("dayshift/execute-"):
        return "5319e7"
    return LABEL_COLORS.get(label, "ededed")


def config_for_execution_label(config: dict[str, Any], label: str | None) -> dict[str, Any]:
    if not label:
        return config
    for lane in execution_lanes(config):
        if lane["label"] == label:
            merged = config.copy()
            merged["agent_command"] = lane["agent_command"]
            merged["selected_model"] = lane["model"]
            merged["selected_execution_label"] = lane["label"]
            merged["selected_execution_title"] = lane["title"]
            merged["selected_run_policy"] = lane["run_policy"]
            merged["selected_reasoning_effort"] = lane["reasoning_effort"]
            merged["selected_execute_and_merge"] = lane["execute_and_merge"]
            return merged
    return config


def load_state() -> dict[str, Any]:
    return load_json_file(STATE_FILE, {"items": {}, "events": [], "last_scan": None})


def save_state(state: dict[str, Any]) -> None:
    save_json_file(STATE_FILE, state)


def token_env(config: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    token_file = config["nightshift_token_file"] if config.get("reuse_nightshift_token") else config["github_token_file"]
    token_path = Path(os.path.expanduser(token_file))
    if token_path.exists() and not env.get("GH_TOKEN"):
        env["GH_TOKEN"] = token_path.read_text().strip()
    return env


def run_command(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
    input_text: str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def gh_api(
    path: str,
    config: dict[str, Any],
    *,
    method: str = "GET",
    fields: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    cmd = ["gh", "api"]
    if method != "GET":
        cmd.extend(["--method", method])
    if method == "GET" and "per_page" not in path:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}per_page=100"
    cmd.append(path)
    input_text = None
    if json_body is not None:
        cmd.extend(["--input", "-"])
        input_text = json.dumps(json_body)
    else:
        for key, value in (fields or {}).items():
            cmd.extend(["-f", f"{key}={value}"])

    try:
        result = run_command(cmd, env=token_env(config), input_text=input_text, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh api timed out after 30s: {path}") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh api failed: {path}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def is_excluded_repo(name: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.startswith("*") and name.endswith(pattern[1:]):
            return True
        if name == pattern:
            return True
    return False


def get_open_nightshift_prs_count(repo_full_name: str, config: dict[str, Any]) -> int:
    pulls = gh_api(f"/repos/{repo_full_name}/pulls?state=open", config)
    if not isinstance(pulls, list):
        return 0
    return sum(1 for pr in pulls if "nightshift" in (pr.get("title") or "").lower())


def discover_target_repos(config: dict[str, Any]) -> list[str]:
    if config.get("target_repos"):
        return list(dict.fromkeys(config["target_repos"]))
    repos = gh_api("/user/repos?sort=updated&direction=desc", config)
    if config.get("repo_discovery_mode") == "dayshift":
        return [
            repo["full_name"]
            for repo in repos
            if not repo.get("archived") and not repo.get("fork") and not repo.get("private")
        ]

    now = datetime.now(timezone.utc)
    selected = []
    for repo in repos:
        name = repo.get("name", "")
        if is_excluded_repo(name, config.get("exclude_repos", [])):
            continue
        if repo.get("archived") or repo.get("fork"):
            continue
        if config.get("public_only", True) and repo.get("private"):
            continue
        if config.get("max_inactive_days", 0) > 0 and repo.get("pushed_at"):
            pushed_at = datetime.fromisoformat(repo["pushed_at"].replace("Z", "+00:00"))
            if now - pushed_at > timedelta(days=config["max_inactive_days"]):
                continue
        if (repo.get("size", 0) or 0) < config.get("min_size_kb", 10):
            continue
        if not repo.get("language"):
            continue
        if get_open_nightshift_prs_count(repo["full_name"], config) >= config.get("max_prs_per_repo", 2):
            continue
        selected.append(repo["full_name"])
    return selected[: config.get("max_repos_to_consider", 30)]


def parse_task_metadata(title: str, body: str) -> tuple[str | None, str | None]:
    combined = f"{title}\n{body}"
    task = None
    category = None

    task_match = re.search(r"\*\*Task:\*\*\s*`?([a-z0-9_-]+)`?", combined, re.IGNORECASE)
    if not task_match:
        task_match = re.search(r"\|\s*Task:\s*([a-z0-9_-]+)", combined, re.IGNORECASE)
    if not task_match:
        task_match = re.search(r"\[nightshift\]\s*([a-z0-9_-]+)\s*:", title, re.IGNORECASE)
    if not task_match:
        task_match = re.search(r"nightshift:\s*([a-z0-9_-]+)", title, re.IGNORECASE)
    if task_match:
        task = task_match.group(1).strip()

    category_match = re.search(r"\*\*Category:\*\*\s*`?([a-z0-9_-]+)`?", combined, re.IGNORECASE)
    if not category_match:
        category_match = re.search(r"\|\s*Category:\s*([a-z0-9_-]+)", combined, re.IGNORECASE)
    if category_match:
        category = category_match.group(1).strip()

    return task, category


def is_nightshift_output(title: str, body: str) -> bool:
    combined = f"{title}\n{body}".lower()
    return "nightshift" in combined


def scan_repo(repo: str, config: dict[str, Any]) -> list[WorkItem]:
    owner_repo = urllib.parse.quote(repo, safe="/")
    issues = gh_api(f"/repos/{owner_repo}/issues?state=open", config)
    pulls = gh_api(f"/repos/{owner_repo}/pulls?state=open", config)
    items: list[WorkItem] = []

    for issue in issues:
        if issue.get("pull_request"):
            continue
        title = issue.get("title", "")
        body = issue.get("body") or ""
        if not is_nightshift_output(title, body):
            continue
        task, category = parse_task_metadata(title, body)
        items.append(
            WorkItem(
                repo=repo,
                number=issue["number"],
                kind="issue",
                title=title,
                url=issue.get("html_url", ""),
                state=issue.get("state", "open"),
                labels=[label["name"] for label in issue.get("labels", [])],
                body=body,
                task=task,
                category=category,
                author_association=issue.get("author_association"),
                created_at=issue.get("created_at"),
                updated_at=issue.get("updated_at"),
            )
        )

    for pr in pulls:
        title = pr.get("title", "")
        body = pr.get("body") or ""
        if not is_nightshift_output(title, body):
            continue
        task, category = parse_task_metadata(title, body)
        head = pr.get("head") or {}
        base = pr.get("base") or {}
        items.append(
            WorkItem(
                repo=repo,
                number=pr["number"],
                kind="pr",
                title=title,
                url=pr.get("html_url", ""),
                state=pr.get("state", "open"),
                labels=[label["name"] for label in pr.get("labels", [])],
                body=body,
                task=task,
                category=category,
                author_association=pr.get("author_association"),
                head_ref=head.get("ref"),
                head_repo=(head.get("repo") or {}).get("full_name"),
                base_ref=base.get("ref"),
                mergeable_state=pr.get("mergeable_state"),
                created_at=pr.get("created_at"),
                updated_at=pr.get("updated_at"),
            )
        )
    return items


def classify_item(item: WorkItem, state: dict[str, Any], config: dict[str, Any]) -> Classification:
    text = f"{item.title}\n{item.body}".lower()
    attempts = state.get("items", {}).get(item.key, {}).get("attempts", 0)
    fixability = 0.35
    confidence = 0.35
    reasons: list[str] = []
    effort = "medium"
    risk = "medium"

    if item.kind == "pr":
        fixability += 0.25
        confidence += 0.2
        effort = "small"
        reasons.append("nightshift PR can be maintained directly")
        if item.mergeable_state in {"dirty", "blocked", "behind", "unstable"}:
            reasons.append(f"PR merge state is {item.mergeable_state}")

    if item.task:
        fixability += 0.15
        confidence += 0.1
        reasons.append(f"structured task metadata: {item.task}")
    if re.search(r"`[^`]+\.(py|js|ts|tsx|go|rs|md|json|yaml|yml)`", item.body):
        fixability += 0.15
        confidence += 0.1
        reasons.append("body references concrete files")
    if any(marker in text for marker in ("### recommendations", "files to fix", "actionable", "line ~", "severity")):
        fixability += 0.2
        confidence += 0.15
        reasons.append("body contains actionable recommendations")
    if any(marker in text for marker in ("documentation", "readme", "doc drift", "typo")):
        risk = "low"
        effort = "small"
    sensitive_scope = " ".join(part for part in [item.title, item.task or "", item.category or ""] if part).lower()
    if any(marker in sensitive_scope for marker in ("auth", "crypto", "security", "privacy", "pii")):
        risk = "high"
        fixability -= 0.2
        reasons.append("security-sensitive area")
    intent_scope = " ".join(part for part in [item.title, item.task or "", item.category or ""] if part).lower()
    if any(marker in intent_scope for marker in ("feature request", "idea-generator", "architecture/design")):
        fixability -= 0.25
        risk = "high"
        reasons.append("not clearly a fix task")
    if attempts >= config.get("max_attempts", 2):
        reasons.append(f"attempt limit reached: {attempts}")
        return Classification("skip", round(fixability, 2), round(confidence, 2), effort, risk, "Stop retrying; needs human review.", reasons)

    fixability = max(0.0, min(1.0, fixability))
    confidence = max(0.0, min(1.0, confidence))

    if item.kind == "pr":
        verdict = "ready" if fixability >= 0.55 else "kanban"
        approach = "Repair the existing Nightshift PR branch, validate, then merge only if policy or human approval allows it."
    elif fixability >= 0.8 and confidence >= 0.65 and risk == "low" and effort in {"trivial", "small"}:
        if config.get("kanban_enabled", True):
            verdict = "kanban"
            approach = "High-confidence fix candidate; wait for user approval in the web UI."
            reasons.append("kanban mode requires user approval")
        else:
            verdict = "auto-fix"
            approach = "Create a focused Dayshift fix PR for the Nightshift issue."
    elif fixability >= 0.5 and fixability + confidence >= 1.15:
        verdict = "kanban"
        approach = "Show in the web UI for approval before creating a fix PR."
    else:
        verdict = "skip"
        approach = "Leave for human triage; not enough actionable context."

    return Classification(verdict, round(fixability, 2), round(confidence, 2), effort, risk, approach, reasons)


def default_inbox_label(item: WorkItem) -> str:
    if item.kind == "issue":
        return "dayshift/issue-inbox"
    if item.kind == "pr":
        return "dayshift/pr-inbox"
    return "dayshift/inbox"


def summarize_item(title: str, body: str, classification: Classification | dict[str, Any]) -> str:
    text = re.sub(r"<[^>]+>", " ", body or "")
    lines = [re.sub(r"^[#*\-\s>`]+", "", line).strip() for line in text.splitlines()]
    summary = next((line for line in lines if len(line) >= 40 and not line.lower().startswith(("repo:", "task:", "category:"))), "")
    if not summary:
        summary = title
    summary = re.sub(r"\s+", " ", summary).strip()
    if len(summary) > 180:
        summary = summary[:177].rstrip() + "..."
    return summary


def sort_timestamp(record: dict[str, Any]) -> str:
    return str(record.get("updated_at") or record.get("created_at") or record.get("last_seen") or "")


def should_show_on_board(record: dict[str, Any]) -> bool:
    workflow_label = record.get("workflow_label")
    if record.get("ignored_by_dayshift"):
        return False
    if record.get("closed_by_dayshift") or record.get("closed_at"):
        return False
    if workflow_label in {"dayshift/done", "dayshift/skip"}:
        return False
    if workflow_label == "dayshift/failed" and not record.get("last_error"):
        return False
    return True


def ensure_dayshift_labels(repo: str, config: dict[str, Any]) -> None:
    existing = {label["name"] for label in gh_api(f"/repos/{repo}/labels", config)}
    for label in sorted(workflow_labels(config) - existing):
        gh_api(
            f"/repos/{repo}/labels",
            config,
            method="POST",
            fields={
                "name": label,
                "color": label_color(label),
                "description": "Managed by hermes-dayshift-glm",
            },
        )


def set_workflow_label(item: WorkItem, label: str, config: dict[str, Any]) -> None:
    labels = workflow_labels(config)
    if label not in labels:
        raise ValueError(f"unknown workflow label: {label}")
    ensure_dayshift_labels(item.repo, config)
    for existing in sorted(set(item.labels) & labels):
        if existing != label:
            encoded = urllib.parse.quote(existing, safe="")
            try:
                gh_api(f"/repos/{item.repo}/issues/{item.number}/labels/{encoded}", config, method="DELETE")
            except RuntimeError as exc:
                # GitHub can return 404 when Dayshift's local state still lists a
                # workflow label that has already disappeared upstream. That is
                # already the desired state, so do not poison the card with a
                # label_sync_error or make it look like executor work failed.
                if "Label does not exist" not in str(exc):
                    raise
    if label not in item.labels:
        gh_api(f"/repos/{item.repo}/issues/{item.number}/labels", config, method="POST", json_body={"labels": [label]})


def save_human_note(key: str, note: str) -> dict[str, Any]:
    state = load_state()
    record = state.get("items", {}).get(key)
    if not record:
        raise KeyError(f"item not found: {key}")
    record["human_note"] = note.strip()
    record["human_note_updated_at"] = now_iso()
    save_state(state)
    return {"status": "saved", "item": key, "human_note": record["human_note"]}


def ignore_work_item(key: str) -> dict[str, Any]:
    state = load_state()
    record = state.get("items", {}).get(key)
    if not record:
        raise KeyError(f"item not found: {key}")
    record["ignored_by_dayshift"] = True
    record["ignored_at"] = now_iso()
    save_state(state)
    return {"status": "ignored", "item": key}


def sync_scan(config: dict[str, Any], *, apply_labels: bool = False) -> tuple[list[WorkItem], dict[str, Classification]]:
    state = load_state()
    items: list[WorkItem] = []
    classifications: dict[str, Classification] = {}

    for repo in discover_target_repos(config):
        repo_items = scan_repo(repo, config)
        items.extend(repo_items)
        for item in repo_items:
            classification = classify_item(item, state, config)
            classifications[item.key] = classification
            record = state.setdefault("items", {}).setdefault(item.key, {})
            github_workflow_label = next((label for label in item.labels if label in workflow_labels(config)), None)
            # The web board is the operator's source of truth. GitHub labels are
            # synced best-effort, but scans must not undo a local card move just
            # because GitHub is stale or label sync failed.
            workflow_label = record.get("workflow_label") or github_workflow_label or default_inbox_label(item)
            if workflow_label == "dayshift/inbox":
                workflow_label = default_inbox_label(item)
            record.update(
                {
                    "repo": item.repo,
                    "number": item.number,
                    "kind": item.kind,
                    "title": item.title,
                    "url": item.url,
                    "task": item.task,
                    "category": item.category,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "classification": asdict(classification),
                    "summary": summarize_item(item.title, item.body, classification),
                    "workflow_label": workflow_label,
                    "last_seen": now_iso(),
                }
            )
            if apply_labels and not set(item.labels) & DAYSHIFT_LABELS:
                set_workflow_label(item, default_inbox_label(item), config)

    state["last_scan"] = now_iso()
    save_state(state)
    return items, classifications


def item_from_state_record(key: str, record: dict[str, Any]) -> WorkItem:
    return WorkItem(
        repo=record["repo"],
        number=int(record["number"]),
        kind=record["kind"],
        title=record.get("title", ""),
        url=record.get("url", ""),
        state="open",
        labels=[record["workflow_label"]] if record.get("workflow_label") else [],
        task=record.get("task"),
        category=record.get("category"),
        created_at=record.get("created_at"),
        updated_at=record.get("updated_at"),
    )


def clone_repo(repo: str, config: dict[str, Any]) -> Path:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    clone_dir = WORKSPACE / repo.split("/")[-1]
    if clone_dir.exists():
        shutil.rmtree(clone_dir)
    result = run_command(["git", "clone", f"https://github.com/{repo}.git", str(clone_dir)], env=token_env(config))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git clone failed")
    return clone_dir


def build_agent_prompt(item: WorkItem, classification: Classification, config: dict[str, Any]) -> str:
    human_note = str(config.get("human_note") or "").strip()
    human_guidance = f"\n        Human guidance:\n        {human_note}\n" if human_note else ""
    return textwrap.dedent(
        f"""
        You are Dayshift, the implementer companion for Nightshift.

        Repo: {item.repo}
        Item: {item.kind} #{item.number}
        URL: {item.url}
        Title: {item.title}
        Task: {item.task or "unknown"}
        Category: {item.category or "unknown"}
        Selected model: {config.get("selected_model", "default")}
        Reasoning effort: {config.get("selected_reasoning_effort", "default")}
        Execution lane: {config.get("selected_execution_title", "default")}

        Classification:
        - verdict: {classification.verdict}
        - fixability: {classification.fixability}
        - confidence: {classification.confidence}
        - effort: {classification.effort}
        - risk: {classification.risk}
        - approach: {classification.approach}
        {human_guidance}

        Requirements:
        - Fix the issue or PR linked above.
        - Make a targeted fix only.
        - Do not run broad formatters.
        - Do not add dependencies unless the issue explicitly requires it.
        - Run relevant tests or checks.
        - Commit changes with a Dayshift trailer.

        Original body:
        {item.body}
        """
    ).strip()


def run_agent(item: WorkItem, classification: Classification, clone_dir: Path, config: dict[str, Any]) -> None:
    command = config.get("agent_command") or os.environ.get("DAYSHIFT_AGENT_CMD", "")
    prompt = build_agent_prompt(item, classification, config)
    if not command:
        prompt_file = clone_dir / ".dayshift-prompt.md"
        prompt_file.write_text(prompt)
        raise RuntimeError(f"agent_command is not configured; prompt written to {prompt_file}")

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(prompt)
        prompt_path = f.name
    try:
        env = token_env(config)
        if config.get("selected_model"):
            env["DAYSHIFT_MODEL"] = str(config["selected_model"])
        if config.get("selected_reasoning_effort"):
            env["DAYSHIFT_REASONING_EFFORT"] = str(config["selected_reasoning_effort"])
        args = shlex.split(command.format(
            prompt=prompt_path,
            model=config.get("selected_model", ""),
            reasoning_effort=config.get("selected_reasoning_effort", ""),
        ))
        if "{prompt}" not in command:
            if args and args[-1] == "-":
                result = run_command(args, cwd=clone_dir, env=env, input_text=prompt)
            else:
                args.append(prompt_path)
                result = run_command(args, cwd=clone_dir, env=env)
        else:
            result = run_command(args, cwd=clone_dir, env=env)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "agent command failed")
    finally:
        Path(prompt_path).unlink(missing_ok=True)


def dependency_install_commands(clone_dir: Path) -> list[str]:
    if not (clone_dir / "package.json").exists() or (clone_dir / "node_modules").exists():
        return []
    if (clone_dir / "pnpm-lock.yaml").exists():
        return ["pnpm install --frozen-lockfile"]
    if (clone_dir / "bun.lock").exists() or (clone_dir / "bun.lockb").exists():
        return ["bun install --frozen-lockfile"]
    if (clone_dir / "yarn.lock").exists():
        return ["yarn install --frozen-lockfile"]
    if (clone_dir / "package-lock.json").exists():
        return ["npm ci"]
    return []


def validation_commands_for_checkout(clone_dir: Path, config: dict[str, Any]) -> list[str]:
    commands = config.get("validation_commands") or []
    if commands:
        return commands
    has_python_tests = any(clone_dir.glob("test_*.py")) or any((clone_dir / "tests").glob("**/test_*.py"))
    if has_python_tests:
        return ["python3 -m unittest discover"]
    if (clone_dir / "package.json").exists() and (clone_dir / "pnpm-lock.yaml").exists():
        return ["pnpm test"]
    if (clone_dir / "package.json").exists() and ((clone_dir / "bun.lock").exists() or (clone_dir / "bun.lockb").exists()):
        return ["bun test"]
    if (clone_dir / "package.json").exists() and (clone_dir / "yarn.lock").exists():
        return ["yarn test"]
    if (clone_dir / "package.json").exists() and (clone_dir / "package-lock.json").exists():
        return ["npm test"]
    if (clone_dir / "CMakeLists.txt").exists():
        return ["cmake -S . -B build", "cmake --build build", "ctest --test-dir build --output-on-failure"]
    return []


def prepare_checkout(clone_dir: Path, config: dict[str, Any]) -> list[str]:
    logs: list[str] = []
    for command in dependency_install_commands(clone_dir):
        result = run_command(shlex.split(command), cwd=clone_dir)
        logs.append(f"$ {command}\n{result.stdout}{result.stderr}".strip())
        if result.returncode != 0:
            raise RuntimeError(logs[-1])
    return logs


def validate_checkout(clone_dir: Path, config: dict[str, Any]) -> tuple[bool, list[str]]:
    commands = validation_commands_for_checkout(clone_dir, config)
    if not commands:
        return False, ["No validation command detected."]

    logs: list[str] = []
    for command in commands:
        result = run_command(shlex.split(command), cwd=clone_dir)
        logs.append(f"$ {command}\n{result.stdout}{result.stderr}".strip())
        if result.returncode != 0:
            return False, logs
    return True, logs


def commit_all_changes(clone_dir: Path, message: str) -> bool:
    run_command(["git", "add", "-A"], cwd=clone_dir, check=True)
    diff = run_command(["git", "diff", "--cached", "--quiet"], cwd=clone_dir)
    if diff.returncode == 0:
        return False
    commit = run_command(
        ["git", "-c", "user.name=Dayshift", "-c", "user.email=contact+dayshift@micr.dev", "commit", "-m", message],
        cwd=clone_dir,
    )
    if commit.returncode != 0:
        raise RuntimeError(commit.stderr.strip() or "commit failed")
    return True


def repair_pr_branch(item: WorkItem, classification: Classification, config: dict[str, Any]) -> dict[str, Any]:
    if not item.head_ref:
        raise RuntimeError("PR head ref is missing")
    source_repo = item.head_repo or item.repo
    clone_dir = clone_repo(source_repo, config)
    checkout = run_command(["git", "checkout", item.head_ref], cwd=clone_dir)
    if checkout.returncode != 0:
        raise RuntimeError(checkout.stderr.strip() or f"could not checkout {item.head_ref}")

    run_agent(item, classification, clone_dir, config)
    valid, logs = validate_checkout(clone_dir, config)
    if not valid:
        raise RuntimeError(logs[-1] if logs else "validation failed")

    committed = commit_all_changes(
        clone_dir,
        f"fix: repair nightshift PR {item.number}\n\nDayshift-Source: {item.repo}#{item.number}",
    )
    if committed:
        push = run_command(["git", "push", "origin", f"HEAD:{item.head_ref}"], cwd=clone_dir, env=token_env(config))
        if push.returncode != 0:
            raise RuntimeError(push.stderr.strip() or "push failed")
        return {"repaired": True, "detail": "pushed repair commit"}
    return {"repaired": False, "detail": "agent made no changes"}


def can_auto_merge(item: WorkItem, config: dict[str, Any], *, approved_by_label: bool) -> bool:
    if approved_by_label:
        return True
    if item.kind != "pr":
        return False
    is_maker_pr = item.title.lower().startswith("[nightshift]") or (item.head_ref or "").startswith("nightshift/")
    if is_maker_pr:
        return bool(config.get("auto_merge_maker_prs"))
    return bool(config.get("auto_merge_implement_prs"))


def github_checks_pass(repo: str, pr_number: int, config: dict[str, Any]) -> tuple[bool, str]:
    data = gh_api(f"/repos/{repo}/pulls/{pr_number}", config)
    mergeable_state = data.get("mergeable_state")
    if mergeable_state in {"dirty", "blocked", "unknown"}:
        return False, f"mergeable_state={mergeable_state}"

    status = run_command(
        ["gh", "pr", "view", str(pr_number), "-R", repo, "--json", "statusCheckRollup,mergeStateStatus"],
        env=token_env(config),
    )
    if status.returncode != 0:
        return False, status.stderr.strip() or "could not read PR checks"
    payload = json.loads(status.stdout)
    checks = payload.get("statusCheckRollup") or []
    if not checks:
        return False, "no GitHub checks reported"
    failed = [check for check in checks if check.get("conclusion") not in {"SUCCESS", "SKIPPED"} and check.get("status") != "COMPLETED"]
    if failed:
        return False, "one or more GitHub checks are not passing"
    return True, "checks passed"


def merge_pr(item: WorkItem, config: dict[str, Any]) -> str:
    method = config.get("merge_method", "squash")
    flag = {"merge": "--merge", "squash": "--squash", "rebase": "--rebase"}[method]
    result = run_command(["gh", "pr", "merge", str(item.number), "-R", item.repo, flag, "--delete-branch"], env=token_env(config))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh pr merge failed")
    return result.stdout.strip()


def close_work_item(item: WorkItem, config: dict[str, Any]) -> dict[str, Any]:
    gh_api(f"/repos/{item.repo}/issues/{item.number}", config, method="PATCH", fields={"state": "closed"})
    state = load_state()
    record = state.setdefault("items", {}).setdefault(item.key, {})
    record["workflow_label"] = "dayshift/done"
    record["closed_at"] = now_iso()
    record["closed_by_dayshift"] = True
    save_state(state)
    return {"status": "closed", "item": item.key}


def move_work_item(key: str, label: str, config: dict[str, Any]) -> dict[str, Any]:
    state = load_state()
    record = state.get("items", {}).get(key)
    if not record:
        raise KeyError(f"item not found: {key}")
    if label not in workflow_labels(config):
        raise ValueError(f"unknown workflow label: {label}")
    item = item_from_state_record(key, record)
    try:
        set_workflow_label(item, label, config)
        record.pop("label_sync_error", None)
    except Exception as exc:
        record["label_sync_error"] = str(exc)
    record["workflow_label"] = label
    record["last_moved_at"] = now_iso()
    save_state(state)
    return {"status": "moved", "item": key, "label": label}


def bulk_move_work_items(keys: list[str], label: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes = []
    for key in keys:
        try:
            outcomes.append(move_work_item(key, label, config))
        except Exception as exc:
            outcomes.append({"status": "failed", "item": key, "error": str(exc)})
    return outcomes


def bulk_close_work_items(keys: list[str], config: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes = []
    state = load_state()
    records = state.get("items", {})
    for key in keys:
        record = records.get(key)
        if not record:
            outcomes.append({"status": "failed", "item": key, "error": "item not found"})
            continue
        try:
            outcomes.append(close_work_item(item_from_state_record(key, record), config))
        except Exception as exc:
            outcomes.append({"status": "failed", "item": key, "error": str(exc)})
    return outcomes


def parse_pr_number_from_url(url: str) -> int | None:
    match = re.search(r"/pull/(\d+)", url or "")
    return int(match.group(1)) if match else None


def maybe_merge_created_pr(pr_url: str, source_item: WorkItem, config: dict[str, Any], record: dict[str, Any]) -> None:
    pr_number = parse_pr_number_from_url(pr_url)
    if not pr_number:
        record["post_execute_merge"] = "could not parse PR number"
        return
    pr_item = WorkItem(
        repo=source_item.repo,
        number=pr_number,
        kind="pr",
        title=f"[dayshift] fix issue {source_item.number}",
        url=pr_url,
        state="open",
        labels=["dayshift/merge"],
    )
    checks_ok, reason = github_checks_pass(source_item.repo, pr_number, config)
    if not checks_ok:
        record["post_execute_merge"] = f"waiting for checks: {reason}"
        return
    record["post_execute_merge"] = merge_pr(pr_item, config)


def act_on_item(item: WorkItem, classification: Classification, config: dict[str, Any], execution_label: str | None = None) -> dict[str, Any]:
    state = load_state()
    record = state.setdefault("items", {}).setdefault(item.key, {})
    record["attempts"] = int(record.get("attempts", 0)) + 1
    record["last_attempt"] = now_iso()
    if execution_label:
        record["execution_label"] = execution_label
        record["execution_model"] = config_for_execution_label(config, execution_label).get("selected_model")
    set_workflow_label(item, "dayshift/in-progress", config)
    record["workflow_label"] = "dayshift/in-progress"
    save_state(state)

    try:
        execution_config = config_for_execution_label(config, execution_label)
        execution_config["human_note"] = record.get("human_note", "")
        if item.kind == "issue":
            clone_dir = clone_repo(item.repo, config)
            branch = f"dayshift/issue-{item.number}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"
            run_command(["git", "checkout", "-b", branch], cwd=clone_dir, check=True)
            prepare_checkout(clone_dir, execution_config)
            run_agent(item, classification, clone_dir, execution_config)
            valid, logs = validate_checkout(clone_dir, execution_config)
            if not valid:
                raise RuntimeError(logs[-1] if logs else "validation failed")
            committed = commit_all_changes(
                clone_dir,
                f"fix: resolve nightshift issue {item.number}\n\nDayshift-Source: {item.repo}#{item.number}",
            )
            if not committed:
                raise RuntimeError("agent made no changes")
            push = run_command(["git", "push", "-u", "origin", branch], cwd=clone_dir, env=token_env(execution_config))
            if push.returncode != 0:
                raise RuntimeError(push.stderr.strip() or "push failed")
            body = f"Automated by Dayshift from Nightshift issue #{item.number}.\n\nValidation passed locally."
            pr = run_command(["gh", "pr", "create", "-R", item.repo, "--title", f"[dayshift] fix issue {item.number}: {item.title}", "--body", body], cwd=clone_dir, env=token_env(execution_config))
            if pr.returncode != 0:
                raise RuntimeError(pr.stderr.strip() or "PR create failed")
            record["result_url"] = pr.stdout.strip()
            if execution_config.get("selected_execute_and_merge"):
                maybe_merge_created_pr(pr.stdout.strip(), item, execution_config, record)
            set_workflow_label(item, "dayshift/done", config)
            record["workflow_label"] = "dayshift/done"
            outcome = {"status": "done", "url": pr.stdout.strip()}
        else:
            approved = "dayshift/merge" in item.labels
            checks_ok, checks_reason = github_checks_pass(item.repo, item.number, config)
            if not checks_ok and ("dayshift/ready" in item.labels or approved or can_auto_merge(item, config, approved_by_label=False)):
                record["repair"] = repair_pr_branch(item, classification, config)
                checks_ok, checks_reason = github_checks_pass(item.repo, item.number, config)
            if checks_ok and can_auto_merge(item, config, approved_by_label=approved):
                merge_output = merge_pr(item, config)
                record["merge_output"] = merge_output
                set_workflow_label(item, "dayshift/done", config)
                record["workflow_label"] = "dayshift/done"
                outcome = {"status": "merged", "detail": merge_output}
            else:
                record["blocked_reason"] = checks_reason if not checks_ok else "merge policy did not allow merge"
                set_workflow_label(item, "dayshift/ready", config)
                record["workflow_label"] = "dayshift/ready"
                outcome = {"status": "ready", "detail": record["blocked_reason"]}
    except Exception as exc:
        record["last_error"] = str(exc)
        set_workflow_label(item, "dayshift/failed", config)
        record["workflow_label"] = "dayshift/failed"
        outcome = {"status": "failed", "error": str(exc)}

    save_state(state)
    return outcome


def glm_quota_window_open(config: dict[str, Any]) -> tuple[bool, str]:
    command = str(config.get("glm_quota_command") or "").strip()
    if not command:
        return False, "glm_quota_command is not configured"
    try:
        result = run_command([os.path.expanduser(part) for part in shlex.split(command)], env=token_env(config), timeout=30)
    except Exception as exc:
        return False, str(exc)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return False, output or f"quota command exited {result.returncode}"
    if output.lower().startswith("skip:"):
        return False, output
    return True, output or "quota window is open"


def lane_by_label(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {lane["label"]: lane for lane in execution_lanes(config)}


def lane_can_run_now(lane: dict[str, Any], config: dict[str, Any], quota_cache: dict[str, tuple[bool, str]]) -> tuple[bool, str]:
    policy = lane.get("run_policy", "immediate")
    if policy == "immediate":
        return True, "immediate lane"
    if policy == "glm_quota_window":
        if "glm" not in quota_cache:
            quota_cache["glm"] = glm_quota_window_open(config)
        return quota_cache["glm"]
    return False, f"unknown run policy: {policy}"


def is_quota_wait_error(outcome: dict[str, Any]) -> bool:
    text = str(outcome.get("error") or outcome.get("detail") or "").lower()
    return any(marker in text for marker in ("quota", "rate limit", "rate-limit", "429", "too many requests"))


def record_scheduler_event(state: dict[str, Any], event_type: str, message: str, **extra: Any) -> None:
    event = {"timestamp": now_iso(), "type": event_type, "message": message}
    event.update(extra)
    state.setdefault("events", []).append(event)
    state["events"] = state["events"][-200:]


def run_ready_items(config: dict[str, Any], *, respect_run_policy: bool = False, apply_labels: bool = True) -> list[dict[str, Any]]:
    items, classifications = sync_scan(config, apply_labels=apply_labels)
    outcomes = []
    lanes = lane_by_label(config)
    lane_labels = set(lanes)
    quota_cache: dict[str, tuple[bool, str]] = {}
    for item in items:
        state = load_state()
        record = state.get("items", {}).get(item.key, {})
        if record.get("ignored_by_dayshift"):
            continue
        record_label = record.get("workflow_label")
        labels = set(item.labels)
        if record_label:
            labels.add(record_label)
        classification = classifications[item.key]
        execution_label = next((label for label in labels if label in lane_labels), None)
        should_run = "dayshift/ready" in labels or (item.kind == "issue" and execution_label is not None)
        if not config.get("kanban_enabled", True) and classification.verdict == "auto-fix":
            should_run = True
        if item.kind == "pr" and ("dayshift/merge" in labels or can_auto_merge(item, config, approved_by_label=False)):
            should_run = True
        if not should_run or "dayshift/skip" in labels:
            continue
        if respect_run_policy and execution_label:
            can_run, reason = lane_can_run_now(lanes[execution_label], config, quota_cache)
            if not can_run:
                record = state.setdefault("items", {}).setdefault(item.key, {})
                record["scheduler_waiting"] = reason
                record["scheduler_waiting_at"] = now_iso()
                record_scheduler_event(state, "scheduler_waiting", reason, item=item.key, lane=execution_label)
                save_state(state)
                continue
            state.setdefault("items", {}).setdefault(item.key, {}).pop("scheduler_waiting", None)
            save_state(state)
        outcomes.append({"item": item.key, **act_on_item(item, classification, config, execution_label)})
        if respect_run_policy and execution_label and lanes[execution_label].get("run_policy") == "glm_quota_window" and is_quota_wait_error(outcomes[-1]):
            state = load_state()
            record = state.setdefault("items", {}).setdefault(item.key, {})
            record["workflow_label"] = execution_label
            record["scheduler_waiting"] = "quota exhausted during execution; retrying in next window"
            record["scheduler_waiting_at"] = now_iso()
            record_scheduler_event(state, "scheduler_quota_wait", record["scheduler_waiting"], item=item.key, lane=execution_label)
            save_state(state)
            break
    return outcomes


def run_scheduled_items(config: dict[str, Any]) -> list[dict[str, Any]]:
    if not config.get("scheduler_enabled", True):
        return []
    if not SCHEDULER_LOCK.acquire(blocking=False):
        return []
    try:
        outcomes = run_ready_items(config, respect_run_policy=True, apply_labels=False)
        if outcomes:
            state = load_state()
            record_scheduler_event(state, "scheduler_run", f"processed {len(outcomes)} item(s)", outcomes=outcomes)
            save_state(state)
        return outcomes
    except Exception as exc:
        state = load_state()
        record_scheduler_event(state, "scheduler_error", str(exc))
        save_state(state)
        return []
    finally:
        SCHEDULER_LOCK.release()


def scheduler_loop(config: dict[str, Any], stop_event: threading.Event) -> None:
    while not stop_event.wait(int(config.get("scheduler_interval_seconds", 30))):
        run_scheduled_items(config)


def start_scheduler(config: dict[str, Any]) -> threading.Event | None:
    global SCHEDULER_THREAD
    if not config.get("scheduler_enabled", True):
        return None
    stop_event = threading.Event()
    SCHEDULER_THREAD = threading.Thread(target=scheduler_loop, args=(config, stop_event), daemon=True, name="dayshift-scheduler")
    SCHEDULER_THREAD.start()
    return stop_event


def render_board(state: dict[str, Any], config: dict[str, Any] | None = None) -> str:
    config = config or DEFAULT_CONFIG
    human_merge_required = not (
        bool(config.get("auto_merge_implement_prs")) and bool(config.get("auto_merge_maker_prs"))
    )
    ordered_labels = [
        "dayshift/issue-inbox",
        "dayshift/pr-inbox",
        *[lane["label"] for lane in execution_lanes(config)],
        *(["dayshift/ready", "dayshift/merge"] if human_merge_required else []),
        "dayshift/in-progress",
        "dayshift/failed",
    ]
    columns = {label: [] for label in ordered_labels}
    for key, record in state.get("items", {}).items():
        if not should_show_on_board(record):
            continue
        classification = record.get("classification", {})
        status = record.get("workflow_label") or "dayshift/inbox"
        columns.setdefault(status, []).append((key, record, classification))

    boards = []
    lanes = execution_lanes(config)
    title_by_label = {lane["label"]: lane["title"] for lane in lanes}
    policy_by_label = {lane["label"]: lane["run_policy"] for lane in lanes}
    for column, rows in columns.items():
        cards = []
        sorted_rows = sorted(rows, key=lambda row: sort_timestamp(row[1]), reverse=True)
        for key, record, classification in sorted_rows:
            safe_key = html.escape(key)
            safe_title = html.escape(record.get("title", ""))
            safe_url = html.escape(record.get("url", ""))
            safe_kind = html.escape(record.get("kind", "item"))
            safe_verdict = html.escape(classification.get("verdict", "unknown"))
            safe_risk = html.escape(classification.get("risk", "unknown"))
            safe_approach = html.escape(classification.get("approach", ""))
            safe_summary = html.escape(record.get("summary") or summarize_item(record.get("title", ""), "", classification))
            safe_waiting = html.escape(record.get("scheduler_waiting", ""))
            human_note = str(record.get("human_note") or "")
            safe_human_note = html.escape(human_note)
            note_button_title = "Edit executor note" if human_note else "Add executor note"
            note_button_class = "note-card has-note" if human_note else "note-card"
            note_html = f"<p class='human-note'>💬 {safe_human_note}</p>" if human_note else ""
            waiting_html = f"<p class='waiting'>{safe_waiting}</p>" if safe_waiting else ""
            search_text = html.escape(" ".join(str(part) for part in [
                key,
                record.get("repo", ""),
                record.get("kind", ""),
                record.get("title", ""),
                record.get("summary", ""),
                record.get("human_note", ""),
                classification.get("verdict", ""),
                classification.get("risk", ""),
                classification.get("approach", ""),
            ]).lower())
            card_classes = html.escape(f"day-card kind-{record.get('kind', 'item')} risk-{classification.get('risk', 'unknown')} status-{column.replace('dayshift/', '').replace('-', '_')}")
            cards.append(
                {
                    "id": key,
                    "title": (
                        f"<div class='{card_classes}' data-search='{search_text}'>"
                        f"<div class='card-top'><label class='select-row'><input class='card-select' data-key='{safe_key}' type='checkbox'><span class='card-meta'>{safe_key}</span></label><div class='card-actions'><button class='{note_button_class}' data-key='{safe_key}' data-note='{safe_human_note}' type='button' title='{note_button_title}'>💬</button><button class='ignore-card' data-key='{safe_key}' type='button' title='Ignore without closing'>/</button><button class='close-card' data-key='{safe_key}' type='button' title='Close on GitHub'>✕</button></div></div>"
                        f"<a class='card-title' href='{safe_url}' target='_blank' rel='noreferrer'>{safe_title}</a>"
                        f"<div class='badges'><span class='kind-badge'>{safe_kind}</span><span>{safe_verdict}</span><span>{safe_risk}</span></div>"
                        f"<p>{safe_summary}</p>"
                        f"{note_html}"
                        f"<p class='approach'>{safe_approach}</p>"
                        f"{waiting_html}"
                        f"</div>"
                    ),
                }
            )
        title = title_by_label.get(column, column.replace("dayshift/", "").replace("-", " "))
        if column in policy_by_label:
            policy_text = "waits for GLM quota" if policy_by_label[column] == "glm_quota_window" else "runs immediately"
            title = f"{title}<span class='column-policy'>{policy_text}</span>"
        boards.append({"id": column, "title": title, "item": cards})

    board_json = json.dumps(boards)
    label_options = [
        {"label": label, "title": title_by_label.get(label, label.replace("dayshift/", "").replace("-", " "))}
        for label in ordered_labels
        if label not in {"dayshift/in-progress", "dayshift/done", "dayshift/failed"}
    ]
    label_options_json = json.dumps(label_options)

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dayshift</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/jkanban@1.3.1/dist/jkanban.min.css">
  <style>
    body {{ margin: 0; font: 14px system-ui, sans-serif; background: #f4f5f7; color: #172b4d; }}
    header {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 16px 22px; background: #0c1f3f; color: white; }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    nav {{ display: flex; gap: 8px; }}
    header a {{ color: white; border: 1px solid rgba(255,255,255,.5); border-radius: 5px; padding: 7px 10px; text-decoration: none; }}
    .toolbar {{ align-items: center; display: flex; flex-wrap: wrap; gap: 10px; padding: 12px 18px; background: white; border-bottom: 1px solid #dfe1e6; }}
    .toolbar input[type='search'], .toolbar select {{ border: 1px solid #c1c7d0; border-radius: 5px; font: inherit; padding: 8px; }}
    .toolbar input[type='search'] {{ min-width: min(420px, 100%); }}
    .toolbar button {{ border: 0; border-radius: 5px; background: #0c66e4; color: white; cursor: pointer; font-weight: 700; padding: 8px 11px; }}
    .toolbar .danger {{ background: #c9372c; }}
    .toolbar .secondary {{ background: #626f86; }}
    .selection-count {{ color: #42526e; font-weight: 700; }}
    #board {{ padding: 18px; overflow-x: auto; }}
    .kanban-container {{ align-items: flex-start; }}
    .kanban-board {{ background: #e9edf3; border-radius: 7px; box-shadow: none; border-top: 4px solid #8993a4; }}
    .kanban-board[data-id='dayshift/issue-inbox'] {{ border-top-color: #0c66e4; }}
    .kanban-board[data-id='dayshift/pr-inbox'] {{ border-top-color: #7f5f01; }}
    .kanban-board[data-id^='dayshift/execute-'] {{ border-top-color: #6e5dc6; }}
    .kanban-board[data-id='dayshift/ready'] {{ border-top-color: #22a06b; }}
    .kanban-board[data-id='dayshift/merge'] {{ border-top-color: #c9372c; }}
    .kanban-board[data-id='dayshift/done'] {{ border-top-color: #1f845a; }}
    .kanban-board[data-id='dayshift/skip'] {{ border-top-color: #626f86; }}
    .kanban-board[data-id='dayshift/failed'] {{ border-top-color: #ae2a19; }}
    .kanban-board header {{ background: transparent; color: #172b4d; padding: 12px 12px 6px; font-weight: 700; }}
    .column-policy {{ color: #626f86; display: block; font-size: 12px; font-weight: 600; margin-top: 4px; }}
    .kanban-item {{ border-radius: 6px; box-shadow: 0 1px 2px rgba(9,30,66,.18); padding: 0; overflow: hidden; }}
    .day-card {{ border-left: 5px solid #8993a4; padding: 12px; background: #fff; }}
    .kind-issue {{ border-left-color: #0c66e4; }}
    .kind-pr {{ border-left-color: #7f5f01; }}
    .risk-low {{ background: #f4fff8; }}
    .risk-medium {{ background: #fffaf0; }}
    .risk-high {{ background: #fff4f2; }}
    .card-top {{ align-items: flex-start; display: flex; gap: 8px; justify-content: space-between; margin-bottom: 7px; }}
    .select-row {{ align-items: flex-start; cursor: pointer; display: flex; gap: 7px; min-width: 0; }}
    .card-select {{ cursor: pointer; flex: 0 0 auto; height: 16px; margin: 0; pointer-events: auto; width: 16px; }}
    .card-meta {{ color: #5e6c84; font-size: 12px; overflow-wrap: anywhere; }}
    .card-title {{ cursor: pointer; display: block; color: #0747a6; font-weight: 700; line-height: 1.25; text-decoration: none; }}
    .card-title:hover {{ text-decoration: underline; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 9px 0; }}
    .badges span {{ background: #dfe7f3; border-radius: 999px; color: #253858; font-size: 12px; padding: 3px 7px; }}
    .kind-badge {{ background: #e9f2ff !important; }}
    .kind-pr .kind-badge {{ background: #fff0b3 !important; }}
    .close-card {{ border: 1px solid #ff8f73; border-radius: 999px; background: #ffebe6; color: #ae2a19; cursor: pointer; flex: 0 0 auto; font-size: 13px; font-weight: 900; height: 26px; line-height: 1; padding: 0; width: 26px; }}
    .close-card:hover {{ background: #ffd5cc; }}
    .card-actions {{ align-items: center; display: flex; flex: 0 0 auto; gap: 5px; }}
    .ignore-card {{ border: 1px solid #e2b203; border-radius: 999px; background: #fff7d6; color: #7f5f01; cursor: pointer; flex: 0 0 auto; font-size: 16px; font-weight: 900; height: 26px; line-height: 1; padding: 0; width: 26px; }}
    .ignore-card:hover {{ background: #f8e6a0; }}
    .note-card {{ border: 1px solid #b8c7e0; border-radius: 999px; background: #f4f7fb; color: #253858; cursor: pointer; font-size: 13px; font-weight: 700; line-height: 1; padding: 5px 7px; }}
    .note-card.has-note {{ background: #e3fcef; border-color: #57d9a3; color: #216e4e; }}
    .note-card:hover {{ background: #dfe7f3; }}
    .closing-card {{ opacity: .55; pointer-events: none; }}
    p {{ margin: 0; color: #42526e; line-height: 1.35; }}
    .human-note {{ background: #e3fcef; border: 1px solid #57d9a3; border-radius: 5px; color: #216e4e; margin-top: 9px; padding: 7px; white-space: pre-wrap; }}
    .approach {{ border-top: 1px solid #edf0f5; margin-top: 9px; padding-top: 9px; }}
    .waiting {{ background: #fff7d6; border: 1px solid #f5cd47; border-radius: 5px; color: #7f5f01; margin-top: 9px; padding: 7px; }}
    .is-filtered {{ display: none !important; }}
  </style>
</head>
<body>
  <header>
    <h1>hermes-dayshift-glm</h1>
    <nav>
      <a href="/settings">Settings</a>
      <a href="/scan">Scan GitHub</a>
    </nav>
  </header>
  <section class="toolbar" aria-label="Bulk actions">
    <input id="search" type="search" placeholder="Search cards by repo, title, summary, risk, or kind">
    <span class="selection-count"><span id="selected-count">0</span> selected</span>
    <button id="select-visible" class="secondary" type="button">Select visible</button>
    <button id="clear-selection" class="secondary" type="button">Clear selection</button>
    <select id="bulk-target" aria-label="Move selected to"></select>
    <button id="bulk-move" type="button">Move selected</button>
    <button id="bulk-skip" class="secondary" type="button">Skip selected</button>
    <button id="bulk-close" class="danger" type="button">Close selected</button>
  </section>
  <main id="board"></main>
  <script src="https://cdn.jsdelivr.net/npm/jkanban@1.3.1/dist/jkanban.min.js"></script>
  <script>
    const boards = {board_json};
    const labelOptions = {label_options_json};
    new jKanban({{
      element: "#board",
      gutter: "12px",
      widthBoard: "320px",
      dragBoards: false,
      boards,
      dropEl: function(el, target) {{
        const key = el.dataset.eid;
        const label = target.parentElement.dataset.id;
        fetch("/move", {{
          method: "POST",
          headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
          body: new URLSearchParams({{ key, label }})
        }}).then((response) => {{
          if (!response.ok) window.location.reload();
        }}).catch(() => window.location.reload());
      }}
    }});
    function initCardSelectionControls() {{
      document.querySelectorAll(".select-row").forEach((row) => {{
        if (row.dataset.selectionReady) return;
        row.dataset.selectionReady = "1";
        ["pointerdown", "mousedown", "touchstart"].forEach((eventName) => {{
          row.addEventListener(eventName, (event) => event.stopPropagation());
        }});
        row.addEventListener("click", (event) => {{
          event.stopPropagation();
          window.setTimeout(updateSelectedCount, 0);
        }});
      }});
      document.querySelectorAll(".card-select").forEach((input) => {{
        if (input.dataset.selectionReady) return;
        input.dataset.selectionReady = "1";
        input.addEventListener("change", updateSelectedCount);
      }});
    }}
    const targetSelect = document.getElementById("bulk-target");
    labelOptions.forEach((option) => {{
      const el = document.createElement("option");
      el.value = option.label;
      el.textContent = option.title;
      targetSelect.appendChild(el);
    }});
    function selectedKeys() {{
      return Array.from(document.querySelectorAll(".card-select:checked")).map((input) => input.dataset.key);
    }}
    function visibleCardInputs() {{
      return Array.from(document.querySelectorAll(".kanban-item:not(.is-filtered) .card-select"));
    }}
    function updateSelectedCount() {{
      document.getElementById("selected-count").textContent = String(selectedKeys().length);
    }}
    function applySearch() {{
      const query = document.getElementById("search").value.trim().toLowerCase();
      document.querySelectorAll(".kanban-item").forEach((item) => {{
        const card = item.querySelector(".day-card");
        item.classList.toggle("is-filtered", Boolean(query) && !card.dataset.search.includes(query));
      }});
    }}
    function postBulk(action, extra = {{}}) {{
      const keys = selectedKeys();
      if (!keys.length) return;
      fetch("/bulk", {{
        method: "POST",
        headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
        body: new URLSearchParams({{ action, keys: keys.join("\\n"), ...extra }})
      }}).then(async (response) => {{
        if (!response.ok) {{
          alert(await response.text());
          return;
        }}
        window.location.reload();
      }}).catch((error) => alert(error.message));
    }}
    document.getElementById("search").addEventListener("input", applySearch);
    document.getElementById("select-visible").addEventListener("click", () => {{
      visibleCardInputs().forEach((input) => {{
        input.checked = true;
      }});
      updateSelectedCount();
    }});
    document.getElementById("clear-selection").addEventListener("click", () => {{
      document.querySelectorAll(".card-select:checked").forEach((input) => {{
        input.checked = false;
      }});
      updateSelectedCount();
    }});
    document.getElementById("bulk-move").addEventListener("click", () => postBulk("move", {{ label: targetSelect.value }}));
    document.getElementById("bulk-skip").addEventListener("click", () => postBulk("move", {{ label: "dayshift/skip" }}));
    document.getElementById("bulk-close").addEventListener("click", () => {{
      const keys = selectedKeys();
      if (!keys.length || !confirm(`Close ${{keys.length}} selected item(s) on GitHub?`)) return;
      postBulk("close");
    }});
    initCardSelectionControls();
    document.addEventListener("click", (event) => {{
      const title = event.target.closest(".card-title");
      if (title) {{
        event.preventDefault();
        event.stopPropagation();
        window.open(title.href, "_blank", "noopener,noreferrer");
        return;
      }}
      const closeButton = event.target.closest(".close-card");
      const ignoreButton = event.target.closest(".ignore-card");
      const noteButton = event.target.closest(".note-card");
      if (!closeButton && !ignoreButton && !noteButton) return;
      event.preventDefault();
      event.stopPropagation();
      if (noteButton) {{
        const key = noteButton.dataset.key;
        const currentNote = noteButton.dataset.note || "";
        const note = window.prompt("Executor note for this card:", currentNote);
        if (note === null) return;
        fetch("/note", {{
          method: "POST",
          headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
          body: new URLSearchParams({{ key, note }})
        }}).then(async (response) => {{
          if (!response.ok) {{
            alert(await response.text());
            return;
          }}
          window.location.reload();
        }}).catch((error) => alert(error.message));
        return;
      }}
      const button = closeButton || ignoreButton;
      const action = ignoreButton ? "ignore" : "close";
      if (ignoreButton && !confirm("Ignore this item in Dayshift without closing it on GitHub?")) return;
      const key = button.dataset.key;
      const kanbanItem = button.closest(".kanban-item");
      if (kanbanItem) kanbanItem.classList.add("closing-card");
      fetch(`/${{action}}`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
        body: new URLSearchParams({{ key }})
      }}).then(async (response) => {{
        if (!response.ok) {{
          if (kanbanItem) kanbanItem.classList.remove("closing-card");
          alert(await response.text());
          return;
        }}
        if (kanbanItem) kanbanItem.remove();
        updateSelectedCount();
      }}).catch((error) => {{
        if (kanbanItem) kanbanItem.classList.remove("closing-card");
        alert(error.message);
      }});
    }});
  </script>
</body>
</html>"""


def parse_form(body: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}


def split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def parse_settings_form(form: dict[str, str], base_config: dict[str, Any]) -> dict[str, Any]:
    config = base_config.copy()
    config["target_repos"] = split_lines(form.get("target_repos", ""))
    config["repo_discovery_mode"] = form.get("repo_discovery_mode", DEFAULT_CONFIG["repo_discovery_mode"])
    config["exclude_repos"] = split_lines(form.get("exclude_repos", ""))
    config["public_only"] = form.get("public_only") == "on"
    config["max_inactive_days"] = int(form.get("max_inactive_days", DEFAULT_CONFIG["max_inactive_days"]))
    config["min_size_kb"] = int(form.get("min_size_kb", DEFAULT_CONFIG["min_size_kb"]))
    config["max_repos_to_consider"] = int(form.get("max_repos_to_consider", DEFAULT_CONFIG["max_repos_to_consider"]))
    config["max_prs_per_repo"] = int(form.get("max_prs_per_repo", DEFAULT_CONFIG["max_prs_per_repo"]))
    config["reuse_nightshift_token"] = form.get("reuse_nightshift_token") == "on"
    config["github_token_file"] = form.get("github_token_file", DEFAULT_CONFIG["github_token_file"]).strip()
    config["nightshift_token_file"] = form.get("nightshift_token_file", DEFAULT_CONFIG["nightshift_token_file"]).strip()
    config["auto_merge_implement_prs"] = form.get("auto_merge_implement_prs") == "on"
    config["auto_merge_maker_prs"] = form.get("auto_merge_maker_prs") == "on"
    config["kanban_enabled"] = form.get("kanban_enabled") == "on"
    config["max_attempts"] = int(form.get("max_attempts", DEFAULT_CONFIG["max_attempts"]))
    config["scheduler_enabled"] = form.get("scheduler_enabled") == "on"
    config["scheduler_interval_seconds"] = int(form.get("scheduler_interval_seconds", DEFAULT_CONFIG["scheduler_interval_seconds"]))
    config["glm_quota_command"] = form.get("glm_quota_command", DEFAULT_CONFIG["glm_quota_command"]).strip()
    config["agent_command"] = form.get("agent_command", DEFAULT_CONFIG["agent_command"]).strip() or DEFAULT_CONFIG["agent_command"]
    config["validation_commands"] = split_lines(form.get("validation_commands", ""))
    config["merge_method"] = form.get("merge_method", DEFAULT_CONFIG["merge_method"])
    try:
        config["execution_lanes"] = json.loads(form.get("execution_lanes", "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"execution_lanes must be valid JSON: {exc}") from exc
    for index, lane in enumerate(config.get("execution_lanes", [])):
        if not isinstance(lane, dict):
            continue
        run_policy_key = f"lane_{index}_run_policy"
        reasoning_key = f"lane_{index}_reasoning_effort"
        merge_key = f"lane_{index}_execute_and_merge"
        if run_policy_key in form:
            lane["run_policy"] = form[run_policy_key]
        if reasoning_key in form:
            lane["reasoning_effort"] = form[reasoning_key]
        lane["execute_and_merge"] = merge_key in form
    config["execution_lanes"] = merge_default_execution_lanes(config["execution_lanes"])

    # Reuse the normal config validator without writing a half-valid settings file.
    if not isinstance(config.get("target_repos"), list):
        raise ValueError("target_repos must be a list")
    if config.get("repo_discovery_mode") not in {"dayshift", "nightshift"}:
        raise ValueError("repo_discovery_mode must be dayshift or nightshift")
    for field in ("max_inactive_days", "min_size_kb", "max_repos_to_consider", "max_prs_per_repo"):
        if not isinstance(config.get(field), int) or config[field] < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    if not isinstance(config.get("execution_lanes"), list):
        raise ValueError("execution_lanes must be a list")
    if config.get("merge_method") not in {"merge", "squash", "rebase"}:
        raise ValueError("merge_method must be one of: merge, squash, rebase")
    if not isinstance(config.get("scheduler_interval_seconds"), int) or config["scheduler_interval_seconds"] < 1:
        raise ValueError("scheduler_interval_seconds must be a positive integer")
    for lane in config.get("execution_lanes", []):
        if not isinstance(lane, dict):
            raise ValueError("each execution lane must be an object")
        if not lane.get("label") or not str(lane["label"]).startswith("dayshift/execute-"):
            raise ValueError("execution lane labels must start with dayshift/execute-")
        if not lane.get("title"):
            raise ValueError("execution lane title is required")
        if not lane.get("model"):
            raise ValueError("execution lane model is required")
        if lane.get("run_policy", "immediate") not in {"immediate", "glm_quota_window"}:
            raise ValueError("execution lane run_policy must be immediate or glm_quota_window")
        if lane.get("reasoning_effort", "medium") not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("execution lane reasoning_effort must be low, medium, high, or xhigh")
    return config


def checked(value: bool) -> str:
    return " checked" if value else ""


def render_lane_controls(config: dict[str, Any]) -> str:
    controls = []
    for index, lane in enumerate(config.get("execution_lanes", [])):
        title = html.escape(str(lane.get("title") or lane.get("label") or f"Execution lane {index + 1}"))
        policy = str(lane.get("run_policy") or "immediate")
        reasoning = str(lane.get("reasoning_effort") or ("high" if str(lane.get("model")) == "gpt-5.3-codex" else "medium"))
        controls.append(
            f"""
            <div class="lane-control" data-lane-index="{index}">
              <h3>{title}</h3>
              <label for="lane_{index}_run_policy">Run policy</label>
              <select id="lane_{index}_run_policy" name="lane_{index}_run_policy" data-lane-field="run_policy" data-lane-index="{index}">
                <option value="immediate"{' selected' if policy == 'immediate' else ''}>Run immediately</option>
                <option value="glm_quota_window"{' selected' if policy == 'glm_quota_window' else ''}>Wait for GLM quota window</option>
              </select>
              <label for="lane_{index}_reasoning_effort">Reasoning effort</label>
              <select id="lane_{index}_reasoning_effort" name="lane_{index}_reasoning_effort" data-lane-field="reasoning_effort" data-lane-index="{index}">
                <option value="low"{' selected' if reasoning == 'low' else ''}>Low</option>
                <option value="medium"{' selected' if reasoning == 'medium' else ''}>Medium</option>
                <option value="high"{' selected' if reasoning == 'high' else ''}>High</option>
                <option value="xhigh"{' selected' if reasoning == 'xhigh' else ''}>Extra high</option>
              </select>
              <label><input type="checkbox" name="lane_{index}_execute_and_merge" data-lane-field="execute_and_merge" data-lane-index="{index}"{checked(bool(lane.get("execute_and_merge")))}> Execute &amp; merge directly afterwards</label>
            </div>
            """
        )
    return "\n".join(controls)


def render_settings(config: dict[str, Any], error: str | None = None) -> str:
    target_repos = html.escape("\n".join(config.get("target_repos", [])))
    exclude_repos = html.escape("\n".join(config.get("exclude_repos", [])))
    validation_commands = html.escape("\n".join(config.get("validation_commands", [])))
    lanes = html.escape(json.dumps(config.get("execution_lanes", []), indent=2))
    lane_controls = render_lane_controls(config)
    error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    merge_options = "".join(
        f"<option value='{method}'{' selected' if config.get('merge_method') == method else ''}>{method}</option>"
        for method in ("squash", "merge", "rebase")
    )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dayshift Settings</title>
  <style>
    body {{ margin: 0; font: 14px system-ui, sans-serif; background: #f4f5f7; color: #172b4d; }}
    header {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 16px 22px; background: #0c1f3f; color: white; }}
    nav {{ display: flex; gap: 8px; }}
    header a {{ color: white; border: 1px solid rgba(255,255,255,.5); border-radius: 5px; padding: 7px 10px; text-decoration: none; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 20px; }}
    form {{ display: grid; gap: 16px; }}
    section {{ background: white; border: 1px solid #dfe1e6; border-radius: 7px; padding: 16px; }}
    h1, h2 {{ margin: 0; letter-spacing: 0; }}
    h2 {{ font-size: 16px; margin-bottom: 12px; }}
    label {{ display: block; font-weight: 700; margin: 12px 0 6px; }}
    input[type='text'], input[type='number'], select, textarea {{ box-sizing: border-box; width: 100%; border: 1px solid #c1c7d0; border-radius: 5px; padding: 8px; font: inherit; }}
    textarea {{ min-height: 110px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .lanes {{ min-height: 260px; }}
    .checks label {{ display: flex; align-items: center; gap: 8px; font-weight: 600; }}
    .lane-controls {{ display: grid; gap: 10px; margin-top: 12px; }}
    .lane-control {{ border: 1px solid #dfe1e6; border-radius: 7px; padding: 12px; }}
    .lane-control h3 {{ font-size: 14px; margin: 0 0 8px; }}
    .lane-control label {{ display: flex; align-items: center; gap: 8px; font-weight: 600; margin: 8px 0; }}
    .lane-control select {{ max-width: 260px; }}
    .actions {{ display: flex; gap: 10px; }}
    button {{ border: 0; border-radius: 5px; background: #0c66e4; color: white; font-weight: 700; padding: 9px 14px; cursor: pointer; }}
    .secondary {{ display: inline-block; border: 1px solid #c1c7d0; border-radius: 5px; color: #172b4d; padding: 8px 13px; text-decoration: none; }}
    .hint {{ color: #5e6c84; margin: 6px 0 0; }}
    .error {{ background: #ffebe6; border: 1px solid #ff8f73; border-radius: 5px; color: #7a1f0b; padding: 10px; }}
  </style>
</head>
<body>
  <header>
    <h1>Dayshift Settings</h1>
    <a href="/">Board</a>
  </header>
  <main>
    {error_html}
    <form method="post" action="/settings">
      <section>
        <h2>Repos</h2>
        <label for="target_repos">Target repos, one per line</label>
        <textarea id="target_repos" name="target_repos">{target_repos}</textarea>
        <p class="hint">If this is empty, Dayshift discovers repos using the selected discovery mode.</p>
        <label for="repo_discovery_mode">Empty target repo discovery</label>
        <select id="repo_discovery_mode" name="repo_discovery_mode">
          <option value="dayshift"{' selected' if config.get("repo_discovery_mode") == "dayshift" else ""}>Dayshift broad scan</option>
          <option value="nightshift"{' selected' if config.get("repo_discovery_mode") == "nightshift" else ""}>Nightshift-compatible filters</option>
        </select>
        <label for="exclude_repos">Nightshift-style exclude repos, one per line</label>
        <textarea id="exclude_repos" name="exclude_repos">{exclude_repos}</textarea>
        <div class="checks">
          <label><input type="checkbox" name="public_only"{checked(bool(config.get("public_only")))}> Public repos only</label>
        </div>
        <label for="max_inactive_days">Max inactive days</label>
        <input id="max_inactive_days" name="max_inactive_days" type="number" min="0" value="{int(config.get("max_inactive_days", 30))}">
        <label for="min_size_kb">Minimum repo size KB</label>
        <input id="min_size_kb" name="min_size_kb" type="number" min="0" value="{int(config.get("min_size_kb", 10))}">
        <label for="max_repos_to_consider">Max repos to consider</label>
        <input id="max_repos_to_consider" name="max_repos_to_consider" type="number" min="0" value="{int(config.get("max_repos_to_consider", 30))}">
        <label for="max_prs_per_repo">Max open Nightshift PRs per repo</label>
        <input id="max_prs_per_repo" name="max_prs_per_repo" type="number" min="0" value="{int(config.get("max_prs_per_repo", 2))}">
      </section>

      <section class="checks">
        <h2>Mode And Merge Policy</h2>
        <label><input type="checkbox" name="kanban_enabled"{checked(bool(config.get("kanban_enabled")))}> Kanban mode: require user approval</label>
        <label><input type="checkbox" name="scheduler_enabled"{checked(bool(config.get("scheduler_enabled", True)))}> Run approved execution lanes from the web server</label>
        <label><input type="checkbox" name="auto_merge_implement_prs"{checked(bool(config.get("auto_merge_implement_prs")))}> Auto-merge Dayshift PRs</label>
        <label><input type="checkbox" name="auto_merge_maker_prs"{checked(bool(config.get("auto_merge_maker_prs")))}> Auto-merge Nightshift PRs</label>
        <label for="merge_method">Merge method</label>
        <select id="merge_method" name="merge_method">{merge_options}</select>
        <label for="max_attempts">Max attempts per item</label>
        <input id="max_attempts" name="max_attempts" type="number" min="0" value="{int(config.get("max_attempts", 2))}">
        <label for="scheduler_interval_seconds">Scheduler poll seconds</label>
        <input id="scheduler_interval_seconds" name="scheduler_interval_seconds" type="number" min="1" value="{int(config.get("scheduler_interval_seconds", 30))}">
      </section>

      <section>
        <h2>GitHub Identity</h2>
        <div class="checks">
          <label><input type="checkbox" name="reuse_nightshift_token"{checked(bool(config.get("reuse_nightshift_token")))}> Reuse Nightshift bot token</label>
        </div>
        <label for="github_token_file">Dayshift token file</label>
        <input id="github_token_file" name="github_token_file" type="text" value="{html.escape(str(config.get("github_token_file", "")))}">
        <label for="nightshift_token_file">Nightshift token file</label>
        <input id="nightshift_token_file" name="nightshift_token_file" type="text" value="{html.escape(str(config.get("nightshift_token_file", "")))}">
      </section>

      <section>
        <h2>Agents And Models</h2>
        <label for="agent_command">Default agent command</label>
        <input id="agent_command" name="agent_command" type="text" value="{html.escape(str(config.get("agent_command", "")))}">
        <p class="hint">Lane commands override this. Commands can use {{model}} and {{prompt}} placeholders.</p>
        <label for="glm_quota_command">GLM quota window command</label>
        <input id="glm_quota_command" name="glm_quota_command" type="text" value="{html.escape(str(config.get("glm_quota_command", "")))}">
        <p class="hint">Execution lanes with run_policy=glm_quota_window run only when this command exits successfully.</p>
        <div id="lane-controls" class="lane-controls">{lane_controls}</div>
        <label for="execution_lanes">Execution lanes JSON</label>
        <textarea id="execution_lanes" name="execution_lanes" class="lanes">{lanes}</textarea>
        <p class="hint">The controls above update this JSON. Use execute & merge directly afterwards on lanes where you want the created PR merged as soon as checks allow it.</p>
      </section>

      <section>
        <h2>Validation</h2>
        <label for="validation_commands">Validation commands, one per line</label>
        <textarea id="validation_commands" name="validation_commands">{validation_commands}</textarea>
      </section>

      <div class="actions">
        <button type="submit">Save Settings</button>
        <a class="secondary" href="/">Cancel</a>
      </div>
    </form>
  </main>
  <script>
    const lanesTextarea = document.getElementById("execution_lanes");
    const laneControls = document.getElementById("lane-controls");
    function readLanes() {{
      try {{
        const parsed = JSON.parse(lanesTextarea.value || "[]");
        return Array.isArray(parsed) ? parsed : [];
      }} catch {{
        return [];
      }}
    }}
    function writeLanes(lanes) {{
      lanesTextarea.value = JSON.stringify(lanes, null, 2);
    }}
    function syncLaneControlsToJson() {{
      const lanes = readLanes();
      document.querySelectorAll(".lane-control").forEach((box) => {{
        const index = Number(box.dataset.laneIndex);
        if (!Number.isInteger(index) || !lanes[index]) return;
        const policy = box.querySelector("[data-lane-field='run_policy']");
        const reasoning = box.querySelector("[data-lane-field='reasoning_effort']");
        const merge = box.querySelector("[data-lane-field='execute_and_merge']");
        if (policy) lanes[index].run_policy = policy.value;
        if (reasoning) lanes[index].reasoning_effort = reasoning.value;
        if (merge) lanes[index].execute_and_merge = merge.checked;
      }});
      writeLanes(lanes);
    }}
    function renderLaneControls() {{
      const lanes = readLanes();
      laneControls.innerHTML = "";
      lanes.forEach((lane, index) => {{
        const box = document.createElement("div");
        box.className = "lane-control";
        const title = document.createElement("h3");
        title.textContent = lane.title || lane.label || `Execution lane ${{index + 1}}`;
        box.appendChild(title);

        const policyLabel = document.createElement("label");
        policyLabel.append("Run policy ");
        const policy = document.createElement("select");
        [["immediate", "Run immediately"], ["glm_quota_window", "Wait for GLM quota window"]].forEach(([value, text]) => {{
          const option = document.createElement("option");
          option.value = value;
          option.textContent = text;
          option.selected = (lane.run_policy || "immediate") === value;
          policy.appendChild(option);
        }});
        policy.addEventListener("change", () => {{
          const current = readLanes();
          current[index].run_policy = policy.value;
          writeLanes(current);
        }});
        policy.name = `lane_${{index}}_run_policy`;
        policyLabel.appendChild(policy);
        box.appendChild(policyLabel);

        const reasoningLabel = document.createElement("label");
        reasoningLabel.append("Reasoning effort ");
        const reasoning = document.createElement("select");
        reasoning.name = `lane_${{index}}_reasoning_effort`;
        [["low", "Low"], ["medium", "Medium"], ["high", "High"], ["xhigh", "Extra high"]].forEach(([value, text]) => {{
          const option = document.createElement("option");
          option.value = value;
          option.textContent = text;
          option.selected = (lane.reasoning_effort || (lane.model === "gpt-5.3-codex" ? "high" : "medium")) === value;
          reasoning.appendChild(option);
        }});
        reasoning.addEventListener("change", () => {{
          const current = readLanes();
          current[index].reasoning_effort = reasoning.value;
          writeLanes(current);
        }});
        reasoningLabel.appendChild(reasoning);
        box.appendChild(reasoningLabel);

        const mergeLabel = document.createElement("label");
        const merge = document.createElement("input");
        merge.type = "checkbox";
        merge.name = `lane_${{index}}_execute_and_merge`;
        merge.checked = Boolean(lane.execute_and_merge);
        merge.addEventListener("change", () => {{
          const current = readLanes();
          current[index].execute_and_merge = merge.checked;
          writeLanes(current);
        }});
        mergeLabel.appendChild(merge);
        mergeLabel.append("Execute & merge directly afterwards");
        box.appendChild(mergeLabel);

        laneControls.appendChild(box);
      }});
    }}
    lanesTextarea.addEventListener("input", renderLaneControls);
    document.querySelector("form").addEventListener("submit", syncLaneControlsToJson);
    renderLaneControls();
  </script>
</body>
</html>"""


def render_error(title: str, message: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin: 0; font: 14px system-ui, sans-serif; background: #f4f5f7; color: #172b4d; }}
    main {{ max-width: 760px; margin: 40px auto; background: white; border: 1px solid #dfe1e6; border-radius: 7px; padding: 20px; }}
    h1 {{ margin: 0 0 12px; letter-spacing: 0; }}
    pre {{ white-space: pre-wrap; background: #ffebe6; border: 1px solid #ff8f73; border-radius: 5px; padding: 12px; }}
    a {{ color: #0747a6; }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(title)}</h1>
    <pre>{html.escape(message)}</pre>
    <p><a href="/">Back to board</a></p>
  </main>
</body>
</html>"""


class DayshiftHandler(BaseHTTPRequestHandler):
    config: dict[str, Any] = {}

    def send_html(self, status: int, markup: str) -> None:
        body = markup.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/scan":
            try:
                sync_scan(self.config, apply_labels=False)
            except Exception as exc:
                state = load_state()
                state.setdefault("events", []).append({"timestamp": now_iso(), "type": "scan_error", "message": str(exc)})
                save_state(state)
                self.send_html(500, render_error("Scan failed", str(exc)))
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path == "/settings":
            self.send_html(200, render_settings(self.config))
            return
        state = load_state()
        self.send_html(200, render_board(state, self.config))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode()
        form = parse_form(raw_body)
        if self.path == "/settings":
            try:
                self.config = parse_settings_form(form, self.config)
                DayshiftHandler.config = self.config
                save_config(self.config)
            except Exception as exc:
                body = render_settings(self.config, str(exc)).encode()
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path == "/move":
            label = form.get("label", "")
            key = form.get("key", "")
            if key and label:
                try:
                    move_work_item(key, label, self.config)
                except Exception:
                    pass
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path == "/note":
            key = form.get("key", "")
            try:
                result = save_human_note(key, form.get("note", ""))
            except Exception as exc:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(str(exc).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = json.dumps(result).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/bulk":
            keys = split_lines(form.get("keys", ""))
            action = form.get("action", "")
            try:
                if action == "move":
                    outcomes = bulk_move_work_items(keys, form.get("label", ""), self.config)
                elif action == "close":
                    outcomes = bulk_close_work_items(keys, self.config)
                else:
                    raise ValueError(f"unknown bulk action: {action}")
            except Exception as exc:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(exc).encode())
                return
            failed = [outcome for outcome in outcomes if outcome.get("status") == "failed"]
            if failed:
                self.send_response(207)
            else:
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = json.dumps(outcomes).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/close":
            state = load_state()
            record = state.get("items", {}).get(form.get("key", ""))
            if not record:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"item not found")
                return
            try:
                close_work_item(item_from_state_record(form["key"], record), self.config)
            except Exception as exc:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(exc).encode())
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"closed")
            return
        if self.path == "/ignore":
            try:
                ignore_work_item(form.get("key", ""))
            except Exception as exc:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(str(exc).encode())
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ignored")
            return
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"unknown action")


def serve(config: dict[str, Any], host: str, port: int) -> None:
    DayshiftHandler.config = config
    server = ThreadingHTTPServer((host, port), DayshiftHandler)
    stop_scheduler = start_scheduler(config)
    print(f"Dayshift web UI: http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        if stop_scheduler:
            stop_scheduler.set()
        server.server_close()


def print_items(items: list[WorkItem], classifications: dict[str, Classification]) -> None:
    for item in items:
        c = classifications[item.key]
        print(f"{item.key}\t{c.verdict}\tfix={c.fixability:.2f}\tconf={c.confidence:.2f}\trisk={c.risk}\t{item.title}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dayshift v1 - implement Nightshift findings and maintain Nightshift PRs")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser("scan", help="scan GitHub for Nightshift issues and PRs")
    scan_p.add_argument("--apply-labels", action="store_true")

    sub.add_parser("run", help="run ready or auto-fixable items")

    serve_p = sub.add_parser("serve", help="start local web UI")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=3000)

    sub.add_parser("config-path", help="print config path")

    args = parser.parse_args(argv)
    config = load_config()

    if args.command == "config-path":
        print(CONFIG_FILE)
        return 0
    if args.command == "scan":
        items, classifications = sync_scan(config, apply_labels=args.apply_labels)
        print_items(items, classifications)
        return 0
    if args.command == "run":
        print(json.dumps(run_ready_items(config), indent=2))
        return 0
    if args.command == "serve":
        serve(config, args.host, args.port)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
