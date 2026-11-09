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
