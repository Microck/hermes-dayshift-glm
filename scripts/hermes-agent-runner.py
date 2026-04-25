#!/usr/bin/env python3
"""Run a Dayshift prompt through Hermes Agent.

Dayshift passes prompts as files for lane-specific commands. Hermes chat accepts
non-interactive prompts through --query, so this adapter keeps large prompt
handling out of shell quoting and preserves the selected Hermes provider/model.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a prompt file with Hermes Agent")
    parser.add_argument("--provider", default="zai")
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--max-turns", default="90")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    prompt = Path(args.prompt).read_text()
    env = os.environ.copy()
    env.setdefault("HERMES_ACCEPT_HOOKS", "1")
    command = [
        "hermes",
        "chat",
        "--provider",
        args.provider,
        "--model",
        args.model,
        "--quiet",
        "--yolo",
        "--accept-hooks",
        "--source",
        "dayshift",
        "--max-turns",
        args.max_turns,
        "--query",
        prompt,
    ]
    return subprocess.run(command, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
