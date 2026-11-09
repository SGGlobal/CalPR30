# BRIDGE-02B.1 Offline Signature Verifier

Offline synthetic cryptographic identity verification.
Does not authorize, admit, or enforce durable replay.

## Scope
- Ed25519 signature verification over canonical envelope bytes.
- Injected immutable synthetic trust root. No file loading.
- Actor and role binding against the trust root.
- Exclusive time boundaries: 24h max age, 60s max future skew.
- Schema version 1 rejects reviewed_sha.
- Pure verifier: no adapter construction, no journal or ledger writes.

## Separation of concerns
- Verifier returns "cryptographically_valid" or raises SignatureError.
- Durable replay rejection is the admission component's responsibility.
- A valid signature is not Chief authorization or reviewer approval.

## Commissioning limitations
- Synthetic keys only. No live credentials.
- No transport. No network. No deployment.
- No HALT wiring.
- execution_enabled remains False.

Frozen BRIDGE-02A baseline: aadc12e40d841a60c10900ae29ed1b9f927914c7

## BRIDGE-02B.1 security corrections
- payload_digest is verified against sha256 of the canonical payload bytes.
- Restricted canonical JSON profile: sorted keys, no whitespace, integers only, recursive NFC checks on keys and values. Not full RFC 8785.
- Timestamps with fractional seconds are rejected. Required fields must be non-empty. Nested reviewed_sha is rejected.
- SyntheticRegistryManager provides process-local version rollback detection. Demonstrative only; does not survive restart.
- Raw signed-wire decoding is deferred. The verifier operates on constructed SignedEnvelope objects.

execution_enabled remains False. External transport remains disabled.

## BRIDGE-02B.1 final verification closeout
- SyntheticRegistryManager.accept() is protected by a threading.Lock. Concurrent rollback attempts are rejected.
- Non-integer registry versions (bool, float) are rejected.
- now and envelope timestamps must be UTC-aware. Raw ValueError/OverflowError in canonical_bytes are converted to SignatureError.
- An independently established deterministic Ed25519 test vector verifies both canonical_bytes output and the fixed signature.

execution_enabled remains False. External transport remains disabled.
