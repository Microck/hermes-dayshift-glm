# roadmap

## phase 1: implemented prototype

- [x] Python CLI entrypoint
- [x] GitHub scan for Nightshift issues and PRs
- [x] Deterministic classifier
- [x] GitHub label workflow state
- [x] Local state file
- [x] Local web UI using jKanban
- [x] Configurable model execution lanes
- [x] Independent merge policy toggles for maker and implementer PRs
- [x] Maker PR repair path
- [x] Unit tests for core decisions

## phase 2: harden execution

- [ ] Add a documented default `agent_command` adapter for the preferred coding agent
- [ ] Add real fixture-driven tests for GitHub API payloads
- [ ] Improve validation command detection per language/package manager
- [ ] Add comments back to source Nightshift issues/PRs after action
- [ ] Add dry-run output for `run`

## phase 3: better control plane

- [ ] Add filtering and sorting to the local web UI
- [ ] Add a generated Markdown board export
- [ ] Add Discord notifications for state transitions
- [ ] Add a small auth gate if the UI is exposed beyond localhost

## phase 4: feedback loop

- [ ] Track fix success by Nightshift task type
- [ ] Tune classifier thresholds from historical outcomes
- [ ] Add retry strategies based on previous failure category
- [ ] Detect recurring Nightshift issue patterns and suggest maker-side improvements
