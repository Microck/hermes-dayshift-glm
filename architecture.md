# architecture

## high-level flow

```
GitHub Nightshift issues/PRs
        |
        v
Dayshift scanner -> classifier -> GitHub labels + local state
        |                         |
        v                         v
  auto/ready queue           local web UI
        |                         |
        +----------< approve >----+
        |
        v
issue fixer / PR repairer -> validation -> PR opened or merged
```

## components

### scanner

`dayshift.py scan` reads configured target repos and finds open issues/PRs with Nightshift markers in the title or body. It does not depend on Nightshift local state.

### classifier

The classifier is deterministic. It scores:

- fixability
- confidence
- effort
- risk

When kanban mode is enabled, every actionable issue waits for user approval in the board, even high-confidence low-risk findings. When kanban mode is disabled, low-risk structured issues may become `auto-fix`. Vague idea/feature-style items are skipped.

Execution lanes are configurable model columns. The default lanes are `execute: GLM 5.1` and `execute: GPT 5.3 Codex`; dragging an issue into one of those columns selects that lane's model, agent command, run policy, reasoning effort, and optional execute-then-merge behavior. The default GPT 5.3 Codex lane runs with high reasoning.

Lane run policies are intentionally simple:

- `immediate` runs on the next web scheduler poll.
- `glm_quota_window` runs only when the configured GLM quota command exits successfully. If quota is unavailable or appears exhausted during a GLM execution, the card remains in the lane and is retried in the next window.

### workflow state

GitHub labels are the shared state:

- `dayshift/inbox`
- `dayshift/ready`
- `dayshift/in-progress`
- `dayshift/merge`
- `dayshift/done`
- `dayshift/skip`
- `dayshift/failed`

Local state in `~/.dayshift/state.json` stores classifications, attempts, result URLs, errors, and cached board metadata.

### issue fixer

For Nightshift issues, Dayshift clones the target repo, creates a `dayshift/issue-<number>-<timestamp>` branch, invokes the selected execution lane's `agent_command`, validates the checkout, commits, pushes, and opens a PR.

### PR repairer

For Nightshift PRs, Dayshift assumes the configured token can push to the maker branch. It clones the PR head repo, checks out the head ref, invokes the configured `agent_command`, validates, commits repair changes, and pushes back to the same branch.

### merge lane

Merge authority is controlled by config and labels:

- `auto_merge_implement_prs`
- `auto_merge_maker_prs`
- `dayshift/merge` for human approval from the web UI

Before merge, Dayshift checks GitHub mergeability and status checks, then runs `gh pr merge` with the configured merge method.

### web UI

`dayshift.py serve` starts a local `http.server` board using the existing `jKanban` browser library from jsDelivr. It renders local state grouped by workflow label and lets a human drag issues into model execution lanes, or move PRs to ready, merge, or skip. Cards include a short local summary extracted from the Nightshift body plus the classifier approach.

The server also runs the lightweight scheduler when enabled. That means cards moved into execution lanes do not wait for `dayshift.py run`: non-GLM lanes run immediately, while GLM lanes wait for the quota window.

The board is ordered by decision flow: issue inbox, PR inbox, configured execute lanes, optional ready/merge approval columns when auto-merge is not fully enabled, then in-progress/done/skip/failed status columns.

Cards use color to distinguish item type, risk, and workflow column. Each card also includes a close action that confirms in the browser, closes the GitHub issue/PR, and moves local state to done. The toolbar supports search/filter plus bulk move, skip, and close actions for selected cards.

If no target repos are configured, Dayshift can use either its broad fallback discovery or Nightshift-compatible repo filters. The Nightshift-compatible mode applies the same class of constraints: excludes, public-only, recent activity, minimum size, language presence, repo cap, and open Nightshift PR cap.
