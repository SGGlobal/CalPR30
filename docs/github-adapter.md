# SKIPPY AI Collaboration Bridge — GitHub Adapter (Step 69D.1)

Offline, read-only foundation. No live GitHub API.

## Interface

`GitHubAdapter` exposes only read methods:

- `repository`
- `branch`
- `commit`
- `pull_request`
- `workflow`
- `ci_result`
- `development_report`

Return types are frozen dataclasses: `RepositorySnapshot`, `BranchSnapshot`,
`CommitSnapshot`, `PullRequestSnapshot`, `WorkflowSnapshot`, `CIResultSnapshot`,
`DevelopmentReport`.

## Provider

`SyntheticGitHubProvider` is a deterministic fixture store. Every snapshot
carries `source="synthetic"`. A future authenticated provider may implement
the same `GitHubProvider` protocol. Simulated observations must never be
presented as live GitHub results.

## Repository restrictions

Only `SGGlobal/QLA` is allowed. Arbitrary URLs and external endpoints cannot
override the configured repository. Branch names, commit SHAs, and pull
request numbers are validated. Unsupported inputs are rejected.

## CI status model

`NOT_STARTED`, `PENDING`, `RUNNING`, `SUCCESS`, `FAILED`, `CANCELLED`,
`UNAVAILABLE`.

A missing workflow is `UNAVAILABLE`, never `SUCCESS`. Status is bound to the
exact requested SHA. Unknown test counts remain `None`.

## Security boundaries

The adapter has no methods for commits, pushes, file edits, pull-request
creation or merge, branch deletion, workflow changes, approvals, or deploys.
No Git commands, no shell, no credentials, no network.

CI success is evidence of testing, not permission to merge.

## Planned transition

A later step may add an authenticated read-only GitHub provider behind the
same contract. The synthetic provider remains available for offline tests.
