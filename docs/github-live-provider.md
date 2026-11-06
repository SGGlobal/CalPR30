# SKIPPY AI Collaboration Bridge — Live GitHub Provider (Step 69D.2)

Foundation only. Authenticated access is not enabled by default.

## Architecture

```
GitHubAdapter (expected_source)
  ├─ SyntheticGitHubProvider   (default, source=synthetic)
  └─ GitHubLiveProvider        (explicit, source=github_live)
       └─ injectable HTTPTransport
```

## Authentication

Supported credential: a fine-grained GitHub personal access token restricted
to `SGGlobal/QLA` with read permissions for Metadata, Contents, Pull requests,
and Actions. Write permissions are not required and must not be granted.

GitHub App installation authentication is a future alternative.

The token is supplied at runtime to `GitHubLiveProvider`. It is never stored
in source, never written to `.env` by this milestone, and never included in
logs, reports, or exception messages.

## Activation

1. Default mode is synthetic. No network requests.
2. Authenticated mode requires constructing `GitHubLiveProvider` with a token
   and a transport, then wrapping it in `GitHubAdapter(..., expected_source="github_live")`.
3. Missing or malformed token fails closed.
4. Live failure does not fall back to synthetic success.

## Provenance

`synthetic` and `github_live` are the only accepted sources. The adapter
rejects a snapshot whose source does not match `expected_source`. Mixed
reports are impossible because every snapshot is checked.

## Endpoints (GET only)

- `GET /repos/{owner}/{repo}`
- `GET /repos/{owner}/{repo}/branches/{branch}`
- `GET /repos/{owner}/{repo}/commits/{sha}`
- `GET /repos/{owner}/{repo}/pulls/{number}`
- `GET /repos/{owner}/{repo}/actions/runs?head_sha={sha}`

Fixed host: `https://api.github.com`. Callers cannot override it.

## CI verification

A workflow run is trusted only when its `head_sha` matches the requested SHA.
If GitHub reports success but does not provide independently verified pytest
counts, counts remain `None`. CI success is never merge authorization.

## Security

- Bounded timeout and response size.
- No retries.
- Redirects rejected.
- 401, 403, 404, 429, 5xx mapped to fail-closed errors.
- No write methods, no Git commands, no shell, no trading coupling.

## Configuring authenticated access (future)

1. Create a fine-grained PAT scoped to `SGGlobal/QLA` with the read permissions above.
2. Supply it at process start through an out-of-band secret mechanism.
3. Construct `GitHubLiveProvider(token, transport)` and an adapter with
   `expected_source="github_live"`.
4. Do not commit the token. Do not pass it through collaboration messages.

This milestone does not activate the connection.
