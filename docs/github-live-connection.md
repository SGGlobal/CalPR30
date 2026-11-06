# SKIPPY AI Collaboration Bridge — Live Connection (Step 69D.2B.1)

Foundation for controlled, real, read-only GitHub API requests.
No live request is executed by this milestone.

## Production transport

`ProductionHTTPTransport` in `skippy/collaboration/github_http_transport.py`:

- HTTPS only, host fixed to `api.github.com`.
- GET only.
- TLS certificate validation via `ssl.create_default_context()`.
- Timeout capped at 10 seconds.
- Response body streamed with a 1 MB limit.
- Redirects rejected.
- No retries, no pagination, no arbitrary endpoints.
- No network access on import.
- Errors are credential-safe and do not include authorization headers.

## Secure credential entry

The probe reads a fine-grained PAT through `getpass.getpass` (or an injected
reader in tests). The token must be restricted to `SGGlobal/QLA` with read
permissions for Metadata, Contents, Pull requests, and Actions.

The token is never written to source, commits, arguments, fixtures, logs,
collaboration messages, or persistent configuration. Entry fails closed when
unavailable. Credentials are not requested from Skyppy or Jason.

## Manual activation

```python
from skippy.collaboration.github_live_probe import run_probe

report = run_probe(live=True, confirmed=True)
```

- `live=False` (default) returns an offline report and performs no request.
- `confirmed=False` prompts for the literal text `LIVE`.
- Any other confirmation cancels.
- The command is not run by tests, import, startup, or schedules.
- No autonomous agent may invoke it.

## Expected read-only output

A completed report contains repository name, privacy, integration branch,
HEAD SHA, optional PR number, and CI status. Source is `github_live`.
CI success is evidence of testing, not permission to merge or deploy.

If the required workflow `.github/workflows/test.yml` is absent or its
relationship to the commit cannot be verified, status is `UNAVAILABLE`.
Pytest counts are not fabricated.

## Failure handling

Authentication failure, rate limit, timeout, redirect, oversized body, and
malformed responses fail closed with static messages. Missing confirmation
or missing token cancels or fails closed.

## Credential rotation

Revoke the PAT in GitHub settings and issue a new one with the same read-only
scope. Do not store the new value in the repository. Re-run the probe only
after Chief's separate authorization.

## Limits

The transport and probe never push, create PRs, merge, edit files, change
permissions, trigger workflows, deploy, or trade. Hermes remains isolated.
