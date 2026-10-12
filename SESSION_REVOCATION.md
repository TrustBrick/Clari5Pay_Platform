# Per-session logout revocation (jti denylist)

## What changed
Normal `POST /api/auth/logout` now revokes **only the current session's token**, server-side,
instead of being a client-side courtesy. A stolen/leaked access token stops working on the next
request after that session logs out — on both the HTTP and WebSocket paths — while the user's
**other** sessions keep working.

This is distinct from the two existing all-sessions controls, which are unchanged:
- `POST /api/auth/logout-all` and password change/reset bump `token_version` → revoke **every**
  token the user holds (SEC-002).

## How it works
- Every **session** token (minted by `_issue_session_token`) now carries a unique `jti` claim.
  Short-lived OTP/intermediate tokens and anything built via `create_access_token` directly do
  **not** get a `jti` and are therefore not individually revocable (by design).
- `POST /logout` records the presented token's `jti` (and its `exp`) in the new
  `revoked_tokens` table (`app/core/revocation.revoke_token`).
- Authentication rejects a token whose `jti` is listed:
  - HTTP: `get_current_user` (`app/core/deps.py`) after the signature + `token_version` checks.
  - WebSocket: `support_ws` (`app/api/routes/support.py`) on its own auth path.
- Idempotent: once revoked, the token is rejected everywhere, so a repeated logout of the same
  token returns `401` (not a 500); the store-level insert is also a no-op on a duplicate `jti`.

### Backward compatibility
Tokens minted **before** this feature have no `jti`. They are never considered revoked
(`is_jti_revoked(None)` short-circuits to `False`), so existing sessions keep working until they
expire; they can still be revoked in bulk via `logout-all` or a password change. No user is logged
out by deploying this change.

### Cleanup
A revoked token's row is only needed until its `exp` passes (after that the token is invalid on
its own). `app/core/revocation.purge_expired` deletes expired rows; it runs once at startup
(advisory — never blocks boot). Lingering rows are harmless, only slightly larger tables.

## Schema / migration
A new table `revoked_tokens` (columns: `id`, `jti` unique+indexed, `user_id`, `revoked_at`,
`expires_at` indexed). The project creates tables via `Base.metadata.create_all` at startup (no
Alembic), so the table is **auto-created on deploy**. The change is purely **additive**:
- **Rollout:** deploy the code; `create_all` creates `revoked_tokens` on first boot. No data
  migration, no backfill, no downtime, no existing-session impact.
- **Rollback:** redeploy the previous image. The code that reads/writes `revoked_tokens` is gone;
  the table can be left in place or dropped (`DROP TABLE revoked_tokens;`). With the table absent
  or unused, logout simply reverts to its prior client-side behavior — tokens remain valid until
  expiry / `logout-all`. No tokens are invalidated by the rollback.

## Operational notes
- Adds one indexed `SELECT` per authenticated request (the `jti` lookup). For high throughput this
  is a candidate for a short-TTL cache later; correctness does not depend on it.
- No token value, secret, or sensitive claim is logged by any of this code.

## Not changed
Token lifetimes (incl. the long Admin/SA lifetime), `token_version` semantics, password
change/reset invalidation, authorization/role checks, and all payment/deposit/withdrawal/
settlement/allocation logic are untouched. A shorter-lived access token + refresh-token redesign
remains a separate future project.
