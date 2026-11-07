# SKIPPY AI Collaboration Bridge — Trusted Chief Identity

Architecture and offline synthetic prototype.
No real authentication is activated. No protected execution is enabled.
`execution_enabled` remains `False` on every authorization decision.

## Selected cryptographic standard

Ed25519, provided by the `cryptography` library.
No custom signature algorithm is implemented.

- Public keys are 32 bytes.
- Signatures are 64 bytes.
- Verification uses `Ed25519PublicKey.verify`.
- The verifier does not provide a general-purpose signing capability.

## Canonical payload

Domain-separated, deterministic UTF-8:

```
skippy.chief.authorization\nchief-auth-1\n<json>
```

The JSON object contains exactly the required fields in fixed order.
Unknown fields fail closed. `expected_sha` must be 40 hexadecimal characters.
Timestamps must be timezone-aware ISO. Lifetime is capped at 15 minutes.

## Trusted challenge binding

Challenges originate from the verifier. Each challenge stores:

- authorization scope (action, digest, repository, environment, project,
  task, expected SHA, authorization identifier)
- intended key identifier
- nonce
- issued_at and expires_at
- status (`open` or `consumed`)

Issuance requires an active registered key. The returned challenge already
contains the trusted key identifier. Callers cannot modify the stored
challenge after issuance.

Verification compares the signed payload against every trusted field,
including `issued_at`, `expires_at`, and `key_id`. A signature over altered
timestamps is rejected. Expiration is evaluated from the trusted registry
timestamp, so a payload that claims a later expiration is still rejected
when the registry challenge has expired.

## Signing-key binding

The challenge is bound to one key identifier. A signature from a different
registered key fails. A revoked key fails. An unknown key cannot be used to
issue a challenge. Duplicate key identifiers are rejected.

## Replay protection

Acceptance and challenge consumption occur in one transaction. A reused
challenge is rejected, including after a verifier restart. Concurrent
attempts: exactly one succeeds. There are no automatic retries.

## Synthetic registration

`register_public_key()` is a synthetic, test-facing configuration method.
It does not establish authenticated Chief enrollment. It must not be
exposed through a production collaboration interface without a separately
reviewed trusted enrollment mechanism. Private keys are never accepted or
persisted.

## Trusted clock

The optional `now` argument exists for deterministic tests. Any future
production verification entrypoint must obtain time from a trusted local
clock. An untrusted caller must not be able to override that clock to
revive an expired authorization.

## Human intent limitation

A valid signature establishes possession of a signing key, not necessarily
informed human consent. Future real approval capture must display the exact
action, repository, target, and consequences through a trusted Chief-controlled
interface. This milestone does not implement that interface.

## Remaining commissioning blockers

- Real enrollment and capture are not implemented.
- `execution_enabled` remains `False`.
- Cross-component HALT coordination remains unresolved. AuthorizationPolicy
  does not detect CollaborationProtocol.halt(). No external protected
  execution may be enabled until this is commissioned.
- Hermes is not integrated and has no execution privilege.
- The audit is not claimed to be tamper-proof.
- Recovery after private-key loss is designed, not implemented.

This checkpoint introduces no credentials, no network calls, and no
execution paths.
