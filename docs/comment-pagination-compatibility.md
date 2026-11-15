# Offline comment pagination compatibility correction

Baseline: `0551de3cbc0571ed4e0f488045fc1d129709e06b`.

## Problem and scope

The reader previously accepted pagination metadata only in the named repository
path form with `per_page=25&page=N` in that order. Synthetic checks establish
that numeric repository paths and the reversed order were refused after an
otherwise acceptable HTTP 200 response. GitHub documents numeric repository
paths in its pagination examples:
https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api

This is a compatibility finding, not a diagnosis of the earlier Railway
response. That response's headers and body were not preserved, and its failed
validation stage is unknown. No claim is made that the issue-comments endpoint
returned either newly accepted form during that attempt.

## Behavior

Only response Link metadata gains a second parsing path. It accepts precisely:

- `https://api.github.com/repos/SGGlobal/QLA/issues/{23|26}/comments`
- `https://api.github.com/repositories/1448604243/issues/{23|26}/comments`

The thread must match the requested thread. Repository ID `1448604243` was
independently read from the connected GitHub repository metadata API for
`SGGlobal/QLA` during this work. There is no runtime lookup or arbitrary ID
acceptance. The only query strings accepted are `per_page=25&page=N` and
`page=N&per_page=25`, with the existing ASCII page range 1..9999. Returned next
tokens are normalized to the original named path and query order.

Caller-supplied page tokens remain restricted to the original canonical grammar.
Other origins, repositories, threads, userinfo, explicit ports, fragments,
escapes, extra or repeated query keys and noncanonical integers remain refused.
Duplicate relations, contradictory links, header bounds and strict content-type
and JSON validation retain their existing checks. Links are never followed
automatically. The wrapper still permits only one issue-23 page-1 GET.

Only the reader, a new test file and this document change. The transport,
credential wrapper, launcher, Dockerfile, dependencies, workflows and persistent
attempt reservation logic are unchanged. `execution_enabled=False` remains.

## Verification

All tests use synthetic responses and credentials in an isolated Linux cloud
copy of the baseline. This is author verification, not CI or independent review.
Python 3.12.14; pytest 8.4.2. Runtime dependencies were installed outside the
source tree from the existing requirements; requirements were not modified.

- Targeted reader, transport, wrapper, launcher, diagnostics and compatibility:
  251 passed, including 32 new cases.
- Full suite (`python -m pytest -q`): 1905 passed, 30 ResourceTracker cleanup
  warnings in `tests/test_adapter_signed_admission.py`, 107.05 seconds, exit 0.
  An initial collection attempt was blocked by missing PyNaCl; installing the
  existing required dependency outside the source tree resolved collection.
- Negative control: replacing only the process-local response parser with the
  original canonical parser gives 8 expected failures and 2 passes in the 10
  positive normalization/wrapper/launcher cases. Source files were not changed
  by this control.
- The launcher test confirms one delegated request, unchanged marker bytes and
  inode, and restart refusal without a second request.

No project-code live request, Railway change, credential access, marker reset,
merge, workflow dispatch, runner activation, admission, posting or trading was
performed. The deployed inspection command and original reservation remain
untouched. This change neither authorizes a new attempt nor makes the bridge
operational. Independent review is the next checkpoint before deployment.
