# Offline response-validation diagnostics

Parent: 443f0655a896ad18f1c4fcf7998d6881555b6c15.

The Railway observation reported UNCERTAIN, transport_calls=1, status=200,
reservation_retained=true. These fields do not identify the failed validation
check. No root cause for that response is established.

This change adds an allowlisted validation_code field to future uncertain
results. It preserves the existing response acceptance criteria, exception
messages, request budget, target, reservation path and execution-disabled flags.
No raw header, body, credential or foreign exception text is added to logs.

| Code | Failed stage |
| --- | --- |
| TRANSPORT | Transport delegate raised an exception |
| RESPONSE | Response type or status was refused |
| BODY_BOUNDS | Body type or byte bound was refused |
| CONTENT_TYPE | Strict selected-header/media-type checks failed |
| JSON | UTF-8 decoding or strict JSON parsing failed |
| PAGE_STRUCTURE | Page shape, count, comment fields or unique IDs failed |
| PAGINATION | Pagination header checks failed |
| UNCLASSIFIED | Failure outside these classified paths or an unknown code |

A code classifies the stage only. It does not disclose the remote value or
prove why that value was received. Several checks share a stage.

## Evidence

Python 3.12: 219 tests passed in the launcher, wrapper, strict reader,
HTTPS transport and new diagnostic suites. The 17 new cases cover end-to-end
classification, unknown-code redaction and transport failure. Test transports
are fake; no real token or live request was used.

The deployed source branch remains at 443f0655. This work is on a separate
review branch and has not been deployed. The existing persistent reservation
is not removed, renamed, bypassed or replaced. Deploying this version against
the same volume will still refuse before networking. It cannot recover the
cause of the previous attempt or authorize a fresh attempt.
