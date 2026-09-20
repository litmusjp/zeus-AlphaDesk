# Agent assessment API and MCP

AlphaDesk exposes a workspace-bound, read-only strategy assessment interface:

`POST /api/v1/desk/strategy-assessments`

Authenticate with `X-AlphaDesk-API-Key`. The request contains the underlying symbol, strategy type, side, quantity, explicit option legs, prices, Greeks, and market evidence timestamps. The response contains `pass`, `decision`, `assessment_id`, strategy identity, individual check codes, failed check codes, the server-owned Candidate Assessment policy snapshot and version, evidence timestamps and expiry, and the fixed safety flags `paper_only=true`, `human_approval_required=true`, and `execution_allowed=false`.

A pass means only that the supplied strategy and current evidence satisfy the deterministic Candidate Assessment checks. It is not an approval, order intent, submission, broker instruction, or trading authorization. Missing, stale, un-reconciled, or non-paper account evidence fails closed. This endpoint never creates approvals or orders and never calls a broker.

The optional MCP server exposes `assess_options_strategy`. Set `ALPHADESK_API_URL` to the AlphaDesk origin and `ALPHADESK_API_KEY` to a workspace agent key. The MCP tool forwards the request to the API; it contains no risk logic and has the same read-only boundary.

Agent keys are created and managed from `/desk/api`. AlphaDesk stores only a cryptographic hash and metadata. The plaintext secret is shown once on creation or rotation and is not returned by list or read endpoints. Keys are bound to one workspace and revoked keys are rejected.
