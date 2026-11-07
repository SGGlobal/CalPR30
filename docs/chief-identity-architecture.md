# SKIPPY AI Collaboration Bridge — Trusted Chief Identity (Step 69E.2A)

Architecture and offline security design only.
No real authentication is activated. No protected execution is enabled.
`execution_enabled` remains `False` on every authorization decision.

This document does not implement enrollment or approval capture.
It defines the boundary that Step 69E.2 must later implement under review.

## Recommended authentication method

Public-key signatures over a challenge-response protocol.

- Chief holds a private key outside the repository and outside AI reach.
- The bridge stores only the corresponding public key.
- An approval is a signature over a canonical authorization payload.
- Verification uses the stored public key. Unsigned claims are rejected.

Platform-backed authentication (for example a hardware token or OS keystore)
is an acceptable future binding for the private key. It is not required for
the first commissioning step, and it is not a substitute for the signature
over the exact action payload.

GitHub personal access tokens are not Chief authentication credentials.
They authenticate to GitHub. They do not prove Chief intent for a scoped
authorization.

## Trust establishment and enrollment

Initial enrollment is a human-only, out-of-band procedure.

1. Chief generates a key pair on a device the AI agents cannot access.
2. Chief presents the public key to the bridge through a local, non-AI
   enrollment command that requires interactive confirmation.
3. The bridge records the public key, a key identifier, the enrollment
   timestamp, and a status of `active`.
4. The private key never enters the repository, the collaboration database,
   a message, a log, or an AI context.

AI agents cannot enroll an identity. They cannot replace the active public
key. They cannot register themselves as Chief. An enrollment message from
Skyppy, Jason, Hermes, or the bridge is rejected.

A second key may be enrolled only by a signature from the currently active
key, or by a documented recovery procedure that requires out-of-band
confirmation and leaves the previous key revoked.

## Identity verification process

Verification is independent of AI messages.

1. A participant proposes an action. The proposal is not an approval.
2. The bridge constructs a canonical authorization payload containing the
   action, parameters, repository, environment, project, task, expected SHA,
   expiration, and a fresh challenge nonce.
3. Chief signs that exact payload with the private key.
4. The bridge verifies the signature against the active public key.
5. On success, the authorization record may transition to APPROVED.
6. On failure, the record remains non-approved and the attempt is audited.

A sender name, a message field, an unsigned JSON claim, a repository
comment, or a Boolean flag is not verification. An AI message that claims
`authorization_status="APPROVED"` is not verification.

## Approval binding

The signed payload must include:

- Action type
- Canonical parameter digest
- Repository
- Environment
- Project identifier
- Task identifier
- Expected commit SHA, when applicable
- Expiration timestamp
- Challenge nonce
- Key identifier

An approval for a review does not authorize a merge. An approval for one
commit does not authorize another. An approval for a local environment does
not authorize production. A changed payload fails verification.

## Replay prevention

- Each challenge nonce is single-use and stored with the authorization.
- A repeated nonce is rejected.
- A signature over an expired payload is rejected.
- A consumed authorization is terminal and cannot be reused.
- The signature covers the nonce, so a captured signature cannot be
  rebound to a different action or a different nonce.

## Secret management

- Private keys are never stored in the repository, in tests, in fixtures,
  in logs, or in collaboration messages.
- The bridge stores only the public key and its identifier.
- GitHub PATs are excluded from this identity system.
- Key material is not requested from Skyppy or Jason.
- Test fixtures use synthetic public keys that cannot authorize real actions.

## Revocation and recovery

- Chief may revoke a key by signing a revocation payload with that key,
  or by an out-of-band recovery procedure.
- A revoked key cannot produce new approvals.
- Existing CONSUMED records remain historical and are not reopened.
- Key rotation enrolls a new public key and revokes the previous one in
  the same audited transaction.
- Recovery after loss of the private key requires out-of-band confirmation
  and produces a new enrollment. It does not automatically resume halted
  work and does not re-enable execution.

Authentication failure (bad signature, unknown key, expired payload,
reused nonce) fails closed. The authorization stays non-approved.

## Threat model

| Threat | Mitigation |
| --- | --- |
| AI claims to be Chief | Sender fields are not authentication |
| AI submits an unsigned approval | Signature required |
| AI enrolls its own key | Enrollment is human-only |
| AI replaces the active key | Replacement requires current-key signature or recovery |
| Replay of a valid signature | Nonce is single-use; payload is bound |
| Approval for a different action | Signature covers the exact payload |
| Stolen GitHub PAT used as identity | PATs are excluded |
| Captured challenge reused | Nonce stored and rejected on reuse |

Assumptions: the private key remains under Chief's control; the bridge
process that verifies signatures is the same process that records the
decision; the local database is not silently rewritten by an untrusted
party. The audit is not claimed to be tamper-proof.

## Offline test plan

Future tests, still offline and synthetic:

1. Valid signature over a matching payload is accepted as synthetic evidence.
2. Missing signature is rejected.
3. Signature from an unknown key is rejected.
4. Signature over a modified payload is rejected.
5. Reused nonce is rejected.
6. Expired payload is rejected.
7. AI enrollment attempt is rejected.
8. AI key-replacement attempt is rejected.
9. Revoked key cannot approve.
10. Consumed authorization cannot be reused.
11. No private key appears in fixtures, logs, or reports.
12. Every decision still returns `execution_enabled=False`.

No test contacts a network or uses a real credential.

## Remaining security limitations

- Real enrollment and capture are not implemented in this checkpoint.
- `execution_enabled` remains `False`.
- Cross-component HALT coordination remains a separate commissioning blocker.
  AuthorizationPolicy does not detect CollaborationProtocol.halt().
- No external protected execution may be enabled until HALT coordination
  and trusted identity capture are both commissioned.
- Hermes is not integrated and has no execution privilege.
- The audit is not claimed to be tamper-proof.
- Recovery after private-key loss is designed but not implemented.

## Trust boundaries

```
Chief device (private key)  -- signature -->  Bridge verifier
                                              |
                                              v
                                    AuthorizationPolicy
                                    (execution_enabled=False)

AI agents  -- proposals only -->  Bridge
AI agents  -- cannot -->  enroll, approve, replace key, execute
```

This checkpoint introduces no credentials, no network calls, and no
execution paths.
