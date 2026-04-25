<div align="center">
  <img src="./logo.png" alt="hermes-dayshift-glm" width="300">

<h1>hermes-dayshift-glm</h1>
</div>

<p align="center">
  human-approved implementer for hermes-nightshift-glm. kanban triage, model lanes, quota-gated GLM execution, Codex execution, and guarded PR repair.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-mit-000000?style=flat-square" alt="license badge"></a>
</p>

<img width="1719" height="884" alt="screenshot1869_00-07-26-04-2026" src="https://github.com/user-attachments/assets/2b4e346b-d4e2-48a8-bdd8-268eede88b9d" />

---

`dayshift` is the companion implementer for [`hermes-nightshift-glm`](https://github.com/Microck/hermes-nightshift-glm). Nightshift creates issues and PRs. Dayshift scans those outputs, classifies what is actionable, puts them on a local kanban board, and only runs an executor after a human moves a card into an execution lane.

## how it works

1. **scan**: reads open GitHub issues and PRs that look like Nightshift output
2. **classify**: scores each item for fixability, risk, effort, and approval path
3. **triage**: stores local state in `~/.dayshift/state.json` and renders a jKanban board
4. **approve**: human moves cards into model-specific execution lanes
5. **execute**: runs Codex or Hermes/GLM with the issue or PR context
6. **validate**: installs lockfile dependencies when needed and runs detected tests/checks
7. **output**: issue cards create Dayshift PRs; PR cards can be repaired or merged when policy allows

## architecture

Dayshift is intentionally conservative:

```
scan ──→ classify ──→ board ──→ execution lane ──→ agent ──→ validate ──→ PR/merge
                  │                 │                         │
                  └──── local state ┘                         └── failed → retry lane
```

- **local board state is the source of truth**: GitHub labels are best-effort sync, but stale labels do not override local moves
- **execution lanes are explicit**: a lane chooses model, command, run policy, reasoning effort, and whether to merge after execution
- **terminal work stays off the board**: closed, merged, skipped, ignored, and done records remain in state but do not clutter kanban
- **failed means real execution failure**: label sync noise is hidden and missing old labels are treated as already-clean

## quickstart

requires a GitHub token that can read issues/PRs, apply labels, push branches, and create PRs in the target repos.

```bash
mkdir -p ~/dayshift-workspace ~/.dayshift
curl -sL https://raw.githubusercontent.com/Microck/hermes-dayshift-glm/main/dayshift.py > ~/dayshift-workspace/dayshift.py
curl -sL https://raw.githubusercontent.com/Microck/hermes-dayshift-glm/main/scripts/hermes-agent-runner.py > ~/dayshift-workspace/hermes-agent-runner.py
chmod +x ~/dayshift-workspace/dayshift.py ~/dayshift-workspace/hermes-agent-runner.py
```

save a GitHub token:

```bash
printf '%s\n' 'github-token-here' > ~/.dayshift/.gh-token-dayshift
chmod 600 ~/.dayshift/.gh-token-dayshift
```

scan, run once, or serve the board:

```bash
python3 ~/dayshift-workspace/dayshift.py scan
python3 ~/dayshift-workspace/dayshift.py run
python3 ~/dayshift-workspace/dayshift.py serve --host 127.0.0.1 --port 3001
```

## kanban board

The web UI is local-first and intentionally small. It supports:

- model-specific execution columns such as `execute: GLM 5.1` and `execute: GPT 5.3 Codex`
- per-card executor notes, injected into the next agent prompt as human guidance
- local search and bulk selection
- red `x` close action that closes the GitHub issue or PR
- yellow `/` ignore action that hides a card locally without closing GitHub
- bulk move, skip, and close actions

Default visible board order:

1. `issue inbox`
2. `PR inbox`
3. one `execute: <model>` column per configured lane
4. `ready` and `merge`, shown only when human merge approval is needed
5. `in-progress`
6. `failed`, only for real executor or validation errors

`done` and `skip` are workflow states, not visible columns. Those records are retained in `~/.dayshift/state.json` for auditability.

## execution lanes

The default lanes are:

| lane | model | runner | policy |
|------|-------|--------|--------|
| `dayshift/execute-glm-5-1` | `glm-5.1` | Hermes Agent adapter | waits for GLM quota window |
| `dayshift/execute-gpt-5-3-codex` | `gpt-5.3-codex` | Codex CLI | runs immediately |

The GLM lane uses `scripts/hermes-agent-runner.py`, which passes a prompt file to `hermes chat --query`. The Codex lane streams the prompt through stdin with:

```bash
codex exec --model gpt-5.3-codex -
```

## validation

Dayshift detects common project shapes before accepting agent output:

- Python tests: `python3 -m unittest discover`
- pnpm repos: `pnpm install --frozen-lockfile`, then `pnpm test`
- Bun repos: `bun install --frozen-lockfile`, then `bun test`
- Yarn repos: `yarn install --frozen-lockfile`, then `yarn test`
- npm repos: `npm ci`, then `npm test`
- CMake repos: configure, build, then `ctest --output-on-failure`

You can override this with `validation_commands` in config.

## config

Dayshift reads `~/.dayshift/config.json`. If the file does not exist, defaults are used.

```json
{
  "target_repos": [],
  "repo_discovery_mode": "dayshift",
  "exclude_repos": ["*-backup"],
  "public_only": true,
  "max_inactive_days": 30,
  "min_size_kb": 10,
  "max_repos_to_consider": 30,
  "max_prs_per_repo": 2,
  "github_token_file": "~/.dayshift/.gh-token-dayshift",
  "nightshift_token_file": "~/.nightshift/.gh-token-nightshift",
  "kanban_enabled": true,
  "scheduler_enabled": true,
  "scheduler_interval_seconds": 30,
  "glm_quota_command": "python3 ~/nightshift-workspace/glm_quota.py --check",
  "validation_commands": [],
  "merge_method": "squash"
}
```

The settings page exposes execution lanes, token file paths, scheduler options, merge policy, repo discovery, and validation commands.

## commands

```bash
python3 dayshift.py config-path
python3 dayshift.py scan
python3 dayshift.py scan --apply-labels
python3 dayshift.py run
python3 dayshift.py serve --host 127.0.0.1 --port 3001
```

Environment override:

```bash
DAYSHIFT_TARGET_REPOS=Microck/hermes-nightshift-glm python3 dayshift.py scan
```

## files

| file | description |
|------|-------------|
| `dayshift.py` | scanner, classifier, scheduler, kanban UI, executor orchestration |
| `scripts/hermes-agent-runner.py` | non-interactive Hermes Agent adapter for GLM lanes |
| `test_dayshift.py` | unit tests for parsing, state, kanban rendering, scheduler, execution lanes, validation |
| `architecture.md` | design notes for scanner, labels, board, executor, and merge policy |
| `classifier.md` | classifier heuristics and risk model |
| `input-sources.md` | GitHub input source notes |
| `kanban.md` | board model and workflow labels |
| `roadmap.md` | implementation roadmap |

## for llm agents

if a user asks you to install or run hermes-dayshift-glm:

1. create directories:
   ```bash
   mkdir -p ~/dayshift-workspace ~/.dayshift
   ```

2. download `dayshift.py` and `scripts/hermes-agent-runner.py` from this repo

3. ensure a GitHub token exists at `~/.dayshift/.gh-token-dayshift`

4. scan a narrow target first:
   ```bash
   DAYSHIFT_TARGET_REPOS=Microck/hermes-nightshift-glm python3 ~/dayshift-workspace/dayshift.py scan
   ```

5. start the board:
   ```bash
   python3 ~/dayshift-workspace/dayshift.py serve --host 127.0.0.1 --port 3001
   ```

6. move cards into execution lanes only after reviewing title, summary, risk, and approach

Never publish `~/.dayshift/config.json`, `~/.dayshift/state.json`, GitHub tokens, Hermes config, BrowserStack credentials, or agent runtime logs.

## license

[mit license](LICENSE)
