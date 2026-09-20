# Assessment execution redesign

This feature remains paper-only and is not releasable until these invariants are implemented and independently reviewed.

## Submission ownership

1. `REVALIDATING -> SUBMITTING` creates a durable submission lease with a distinct owner token and lease timestamp.
2. The successful final CAS transitions `SUBMITTING -> DISPATCH_AUTHORIZED` and records `dispatch_authorized_at`. This is the non-reassignable provider boundary: expiry, policy changes, lease age and worker liveness no longer authorize cancellation, token rotation or resubmission.
3. Recovery never reclaims a dispatch-authorized row. It may only read the provider by the immutable original client order ID and persist complete, identity-bound evidence. A not-found result is `SUBMISSION_UNCERTAIN`, never retry permission. Pre-boundary stale `SUBMITTING` rows are also made reconciliation-only because their boundary position cannot be proven.
4. Every execution path passes the owner token through the final broker boundary.
4. The client order ID is immutable and is the provider idempotency key. A retry must reconcile by that ID; it must never create a new ID. If the provider rejects a duplicate client ID, the adapter must read back that exact ID and return the matching existing order; if no matching order is found, the operation remains uncertain.
5. The provider response must be bound to workspace, broker account, `PAPER`, client ID, structure, quantity, price and status before persistence.
6. A stale worker may finish no state and may not create a second provider order. Local reservation release is allowed only for `SUBMISSION_STARTED` and the matching owner token.

## State outcomes

- `done_for_day` with zero fill: `SUBMITTED`; retain unresolved exposure and reconcile later.
- `done_for_day` with a positive fill below requested quantity: `PARTIALLY_FILLED`; retain the filled exposure and remaining quantity.
- `done_for_day` with a full fill: `FILLED`; retain the fill evidence and final exposure.
- Contradictory status, quantities, legs or average price: `SUBMISSION_UNCERTAIN`; never infer a fill.
- `DISPATCH_AUTHORIZED` has no approval expiry and is not renewable; only audited reconciliation may classify it.

The same matrix must be applied by open and close validators, state mappers, persistence validation, recovery, aggregate-risk calculations and UI projections.

## Identity and tenancy

All approval, intent, order and position reads and writes require workspace identity. Broker account ID and `PAPER` environment are persisted and validated at reconciliation, execution, recovery and persistence boundaries. Missing or mismatched identity is quarantined and blocks broker mutation. Existing broker identity values are not treated as trusted until `identity_validated_at` is populated by fresh, account-bound broker evidence; legacy rows with a null provenance timestamp fail closed without being deleted.

## Release gates

Run backend and frontend tests, formatting, lint, type checks, migration checks and diff checks. Then obtain a fresh exact-diff independent review. Do not commit, deploy, save a policy, approve an order, submit an order, or contact production until the reviewer returns an explicit GO for the exact candidate.
