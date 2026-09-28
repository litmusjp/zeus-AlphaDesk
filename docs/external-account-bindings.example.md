# External assessment binding template

`ALPHADESK_EXTERNAL_ACCOUNT_BINDINGS_JSON` is an optional server-only secret. Leave it unset until two separately approved, read-only Alpaca OAuth grants and two distinct AlphaDesk Agent API keys exist. AlphaDesk does not create OAuth applications, request grants, or generate tokens.

Use placeholders only in documentation or local examples; never commit a real bearer token:

```json
{
  "bindings": [
    {
      "workspace_id": "<workspace-uuid>",
      "key_id": "<distinct-agent-key-uuid-for-l1>",
      "account_id": "<alpaca-paper-account-l1>",
      "sandbox_id": "<op-sandbox-l1>",
      "environment": "PAPER",
      "access_token": "<read-only-paper-oauth-bearer-token-l1>"
    },
    {
      "workspace_id": "<workspace-uuid>",
      "key_id": "<distinct-agent-key-uuid-for-l2>",
      "account_id": "<alpaca-paper-account-l2>",
      "sandbox_id": "<op-sandbox-l2>",
      "environment": "PAPER",
      "access_token": "<read-only-paper-oauth-bearer-token-l2>"
    }
  ]
}
```

Each `workspace_id` + `key_id` may bind only one external account. The API reads only `https://paper-api.alpaca.markets/v2/account`; redirects are rejected. Missing, malformed, duplicate, mismatched, stale, blocked, non-paper, or failed evidence remains `UNAVAILABLE`. Tokens are parsed only at API startup and are never logged or returned.
