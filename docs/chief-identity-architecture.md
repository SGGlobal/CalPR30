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

The JSON object contains exactly these fields, in this order:

- authorization_id
- application (`skippy-ai-collaboration-bridge`)
- protocol_version (`chief-auth-1`)
- action
- action_digest
- repository
- environment
- project_id
- task_id
- expected_sha
- key_id
- challenge_id
- nonce
- issued_at
- expires_at

Unknown fields fail closed. A different application or protocol version
fails closed. The domain prefix prevents cross-protocol reuse.

## Verification lifecycle

1. A participant proposes an action. The proposal is not an approval.
2. The bridge builds the canonical payload, including a fresh nonce.
3. A signature is presented. In this milestone it is synthetic.
4. The verifier checks expiration, key identity, key status, nonce reuse,
   and the signature over the exact bytes.
5. On success the result is `verified=True` with `execution_enabled=False`.
6. On failure the result is `verified=False` and the attempt is rejected.

Sender fields, unsigned claims, and identity strings are not evidence.

## Replay protection

- Nonces are single-use and stored in SQLite.
- Acceptance and nonce recording occur in one transaction.
- A reused nonce is rejected, including after a verifier restart.
- Expired challenges are rejected.
- Revoked keys are rejected.
- Concurrent attempts: exactly one succeeds.
- There are no automatic retries.

## Key handling

- Tests generate disposable Ed25519 key pairs in memory.
- Only the public key is registered with the verifier.
- Private keys are never written to fixtures, source, databases, logs,
  or collaboration messages.
- The production-facing verifier does not sign.
- No real Chief private key is requested or stored.

## Trust boundaries

```
Synthetic test key  -- signature -->  ChiefIdentityVerifier
                                      |
                                      v
                            VerificationResult
                            (execution_enabled=False)

AI agents  -- proposals only -->  Bridge
AI agents  -- cannot -->  enroll, approve, replace key, execute
```

## Human intent limitation

A valid signature establishes possession of a signing key, not necessarily
informed human consent. Future real approval capture must display the exact
action, repository, target, and consequences through a trusted Chief-controlled
interface. This milestone does not implement that interface. AI-generated
approval descriptions are not trusted consent evidence.

## Future enrollment

Real enrollment remains a human-only, out-of-band procedure reserved for a
later reviewed step. AI agents cannot enroll or replace the active key.
GitHub PATs are excluded.

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
