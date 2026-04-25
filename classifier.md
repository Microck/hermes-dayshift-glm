# classifier design

The classifier is the core intelligence of the implementer. It decides what gets auto-fixed, what goes to the kanban, and what gets skipped.

## input signals

For each issue/PR, the classifier sees:
- **Issue body**: title, description, labels, author association
- **Context**: affected files (if mentioned), stack traces, error messages
- **Source tag**: `nightshift-maker` (structured finding) vs `community` (user report) vs `dependency` (outdated deps)
- **Repo state**: recent commits, open PR count, branch activity
- **History**: has this issue been attempted before? (from reconciler state)

## classification rules

### auto-fix criteria (all must be true)
- Fixability >= 0.8
- Confidence >= 0.7
- Effort is `trivial` or `small`
- Risk is `low`
- Not authored by repo OWNER/MEMBER/COLLABORATOR (respect human intent)
- Not previously attempted and failed (prevent loops)

### kanban criteria
- Fixability >= 0.5
- Fixability + Confidence >= 1.2
- Effort <= `medium`

### skip criteria (any triggers skip)
- Fixability < 0.5
- Feature request or enhancement (vs bug/fix)
- No actionable context (vague "X doesn't work")
- Previously attempted 2+ times and failed

## scoring heuristics

| Signal | Increases fixability | Decreases fixability |
|--------|---------------------|---------------------|
| Has stack trace / error message | +0.3 | |
| References specific files/lines | +0.2 | |
| Nightshift-maker structured output | +0.3 | |
| Linting / formatting issue | +0.4 | |
| Simple one-file change | +0.2 | |
| | | Vague description (-0.3) |
| | | Multiple files involved (-0.2) |
| | | Requires new dependency (-0.3) |
| | | Touches core logic / auth / crypto (-0.4) |

## implementation

The classifier itself should be deterministic where possible:
- Rule-based scoring first (regex for stack traces, file references, labels)
- LLM fallback only for ambiguous cases
- Minimize LLM usage — most classification can be heuristic

### anti-loop protection
- Each item tracks attempt count in its markdown record
- After 2 failed auto-fix attempts, force to kanban
- After human dismisses from kanban, skip permanently (mark as `wontfix`)
