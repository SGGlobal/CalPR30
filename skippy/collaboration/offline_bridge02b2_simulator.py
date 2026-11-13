"""BRIDGE-02B.2.2 synthetic-only reservation simulator.

This is deliberately NOT an adapter: no network, protected ledger, dispatch,
operational evidence or resume-after-crash capability. Never use in production.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Protocol

from nacl.signing import SigningKey

from skippy.collaboration.offline_admission_gate import AdmissionError, OfflineAdmissionGate
from skippy.collaboration.offline_signature_verifier import (
    SignedEnvelope, TrustRoot, _canonical_json, canonical_bytes,
)
from skippy.collaboration.offline_bridge02b2_contract import (
    MATRIX, SyntheticContractError, SyntheticInput, WorkflowSnapshot,
    plan_transition, policy_inspection_copy, _fingerprint,
)


class SyntheticFixturePolicy(Protocol):
    """Test fixture only. This does not authenticate Chief or a real reviewer."""

    def check(self, message: SyntheticInput, now: datetime) -> str:
        """Return exactly 'synthetic_verified' for a configured test fixture."""


@dataclass(frozen=True)
class SimulationResult:
    outcome: str  # reserved | duplicate | rejected
    reason: str


class SyntheticBridgeSimulator:
    """One fresh, disposable SQLite gate per non-resumable synthetic session.

    Test trust roots must use 'test-b2b2-' key IDs. This is a separation
    convention, not an operational trust anchor or proof of key provenance.
    """

    SYNTHETIC_ONLY = True

    def __init__(
        self,
        *,
        database_path: Path,
        trust_root: TrustRoot,
        signers: dict[str, tuple[str, SigningKey]],
        evidence_verifier: object,
        halt_probe: object,
        fixture_policy: SyntheticFixturePolicy,
    ) -> None:
        if type(trust_root) is not TrustRoot or not trust_root.keys:
            raise SyntheticContractError("explicit synthetic trust root required")
        if type(signers) is not dict or set(signers) != {"chief", "jason", "skippy"}:
            raise SyntheticContractError("explicit synthetic signers required")
        if evidence_verifier is None or halt_probe is None or fixture_policy is None:
            raise SyntheticContractError("synthetic verification, HALT and fixture policy required")
        entries = {entry.actor_id: entry for entry in trust_root.keys}
        if len(entries) != 3 or len(trust_root.keys) != 3:
            raise SyntheticContractError("exactly three test actors required")
        roles = {"chief": "chief", "jason": "implementer", "skippy": "reviewer"}
        for actor, role in roles.items():
            entry = entries.get(actor)
            pair = signers[actor]
            if (entry is None or type(pair) is not tuple or len(pair) != 2
                    or type(pair[0]) is not str or not pair[0].startswith("test-b2b2-")
                    or pair[0] != entry.key_id or entry.permitted_role != role
                    or type(pair[1]) is not SigningKey
                    or pair[1].verify_key.encode() != entry.public_key):
                raise SyntheticContractError("non-synthetic or mismatched signer fixture")
        path = Path(database_path)
        if path.exists() or Path(str(path) + "-wal").exists() or Path(str(path) + "-shm").exists():
            raise SyntheticContractError("session requires a new disposable SQLite store")
        self._signers = dict(signers)
        self._fixture_policy = fixture_policy
        self._snapshot = WorkflowSnapshot()
        self._invalidated = False
        self._lock = RLock()
        # Single-active-transition flag; only read or written while holding _lock.
        self._in_progress = False
        self._gate = OfflineAdmissionGate(path, trust_root, evidence_verifier, halt_probe)

    @property
    def snapshot(self) -> WorkflowSnapshot:
        return self._snapshot

    def invalidate_for_restart(self) -> None:
        """A restarted simulation cannot reconstruct workflow authority."""
        self._invalidated = True

    def _sign_message(self, message: SyntheticInput) -> SignedEnvelope:
        """Create a *new* test signature from copied typed fields, never a legacy upgrade."""
        key_id, signer = self._signers[message.sender]
        payload_bytes = _canonical_json(message.payload)
        unsigned = SignedEnvelope(
            schema_version=1, sender=message.sender, recipient=message.recipient,
            role=message.role, project_id=message.project_id, task_id=message.task_id,
            checkpoint_id=message.checkpoint_id, assignment_id=message.assignment_id,
            message_id=message.message_id, idempotency_key=message.idempotency_key,
            sequence=message.sequence, timestamp=message.timestamp,
            expiry=message.expiry, payload_digest=hashlib.sha256(payload_bytes).digest(),
            payload=dict(message.payload), key_id=key_id, signature=b"\x00" * 64,
        )
        signature = signer.sign(canonical_bytes(unsigned)).signature
        return replace(unsigned, signature=signature)

    def simulate(self, message: SyntheticInput, *, now: datetime) -> SimulationResult:
        # One active transition per session (work order 6845727928). The RLock
        # still serializes threads: another thread waits for the lock and never
        # observes the flag set. The flag is checked and set under the lock, so
        # only a same-thread nested call (for example from a fixture policy, HALT
        # or evidence callback) sees it. That call is rejected before planning,
        # signing or gate access and changes no snapshot or reservation row. The
        # outer call then continues with its own plan and policy result. A plain
        # non-reentrant lock is deliberately not used: same-thread recursion would
        # deadlock. This is a fixture consistency control, not a sandbox against
        # arbitrary in-process code.
        with self._lock:
            if self._in_progress:
                return SimulationResult(
                    "rejected", "reentrant simulate rejected; one active transition per session")
            self._in_progress = True
            try:
                return self._simulate_locked(message, now=now)
            finally:
                self._in_progress = False

    def _simulate_locked(self, message: SyntheticInput, *, now: datetime) -> SimulationResult:
        if self._invalidated:
            return SimulationResult("rejected", "simulation invalidated; no crash/restart continuation")
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() != timedelta(0):
            return SimulationResult("rejected", "now must be UTC-aware")
        planned_from = self._snapshot
        try:
            plan = plan_transition(message, planned_from)
        except SyntheticContractError as exc:
            return SimulationResult("rejected", str(exc))
        try:
            # Mandatory explicit contextual test oracle. Real admission authority
            # cannot be supplied by a caller-controlled string or fixture.
            # Policy receives a separate scalar-only copy, never the internal
            # validated payload. Mutation is rejected before signing or reserve.
            validated = plan.message
            expected = _fingerprint(validated)
            policy_view = policy_inspection_copy(validated)
            policy_result = self._fixture_policy.check(policy_view, now)
            if (
                _fingerprint(validated) != expected
                or _fingerprint(policy_view) != expected
                or policy_view.payload is validated.payload
            ):
                return SimulationResult("rejected", "synthetic fixture policy mutated validated input")
            if self._snapshot is not planned_from:
                # Only reachable if the guard was bypassed (e.g. private API use).
                self._invalidated = True
                return SimulationResult(
                    "rejected", "workflow snapshot changed during policy; synthetic session invalidated")
            if policy_result != "synthetic_verified":
                return SimulationResult("rejected", "synthetic fixture policy denied")
            envelope = self._sign_message(validated)
        except Exception:
            return SimulationResult("rejected", "synthetic signing/policy failed closed")
        try:
            result = self._gate.reserve(envelope, now)
        except Exception:
            # A commit error may be indeterminate: never retry/replay/resume here.
            self._invalidated = True
            raise
        if self._snapshot is not planned_from:
            # Only reachable if the guard was bypassed during reserve. Never apply a stale plan.
            self._invalidated = True
            raise AdmissionError(
                "workflow snapshot changed during reservation; synthetic session invalidated")
        if result.outcome == "rejected":
            return SimulationResult("rejected", result.reason)
        if result.outcome == "duplicate" and plan.duplicate_candidate:
            return SimulationResult("duplicate", "already reserved; no transition")
        if result.outcome == "reserved" and not plan.duplicate_candidate:
            self._snapshot = plan.next_snapshot
            return SimulationResult("reserved", "synthetic reservation only; no ledger append")
        self._invalidated = True
        raise AdmissionError("unexpected reservation/duplicate outcome; synthetic session invalidated")
