# input sources

## primary: nightshift-maker output

The maker runs first in the cycle and opens issues with structured findings. These are the easiest to fix because they come with:
- Exact file paths and line numbers
- Category (lint, security, code quality, etc.)
- Severity level
- Often a suggested fix or rule reference

The implementer should read these first and prioritize them.

### ingestion
- Query GitHub API for open issues labeled by nightshift in the target repo
- Or read nightshift output files directly if stored in a known location
- Nightshift issues should be tagged/classified by the maker (e.g., `nightshift/lint`, `nightshift/security`)

## secondary: community issues

Open issues from users. Harder to classify and fix, but the classifier can identify the actionable ones.

### what works well
- Bug reports with stack traces
- "X is broken after version Y" (regression, easier to bisect)
- Type errors, missing imports
- Documentation typos

### what doesn't work well
- Feature requests
- "X doesn't work" with no details
- Performance complaints without profiling data
- Architecture/design discussions

## tertiary: open PRs

The implementer can also act on PRs, not just issues:

### fixable PR actions
- **Fix failing CI**: rebase, resolve merge conflicts, update dependencies
- **Address review comments**: if comments are actionable code changes
- **Auto-merge**: trivial PRs where CI passes and changes are minimal (requires explicit opt-in per repo)
- **Close stale PRs**: PRs with no activity + conflicts that aren't worth resolving (similar to ClawSweeper behavior)

### PR-specific rules
- Never auto-merge PRs that modify security-sensitive code
- Never force-push to someone else's branch
- Always comment on the PR explaining what was done and why
