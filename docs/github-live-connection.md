# SKIPPY AI Collaboration Bridge — Live Connection (Step 69D.2B.1A)

Foundation for controlled, real, read-only GitHub API requests.
No live request is executed by this milestone.

## Production transport

`ProductionHTTPTransport` in `skippy/collaboration/github_http_transport.py`:

- HTTPS only, host fixed to `api.github.com`.
- GET only.
- TLS validation via `ssl.create_default_context()`.
- Timeout capped at 10 seconds.
- Response body streamed with a 1 MB limit.
- Redirects rejected by `RedirectRejectingHandler` before a second request.
- Authorization headers are never forwarded because redirects are not followed.
- Unexpected ports and URL userinfo are rejected.
- HTTPError is converted to an `HTTPResponse` with the status and an empty
  body so the provider can classify 401, 403, 404, 429, and 5xx.
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

report = run_probe(live=True)
```

1. `live=False` (default) returns an offline report and performs no request.
2. `live=True` always prompts for the literal text `LIVE`.
3. Any other answer cancels before token entry or network access.
4. There is no `confirmed=True` bypass.
5. After confirmation, the token is read through a hidden prompt.
6. The command is not run by tests, import, startup, or schedules.
7. No autonomous agent may invoke it.

Tests inject `confirmer`, `token_reader`, and `transport_factory`. Production
callers omit them.

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
