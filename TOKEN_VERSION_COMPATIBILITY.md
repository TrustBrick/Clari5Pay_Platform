# `token_version` — Compatibility Review & Implementation Plan

**Purpose:** confirm Option E (`token_version`) breaks nothing, and plan its rollout.
**Status:** review and plan only. Nothing implemented.
**Companion:** `AUTH_SESSION_ARCHITECTURE.md` (design), `SECURITY_REVIEW.md` SEC-002 (the finding).

> **Headline:** compatible, with **one blocker that must be fixed in the same change** — the
> support WebSocket validates tokens on its own code path and would otherwise become a revocation
> bypass. See §1.2.

---

# 1. Every JWT touchpoint

Exhaustive search across `backend/app` and `backend/main.py` for `jwt.encode`, `jwt.decode`,
`create_access_token`, `decode_token`, `oauth2_scheme`.

## 1.1 Creation — 4 sites

| # | Site | Token | Claims | Lifetime | Needs `token_version`? |
|---|---|---|---|---|---|
| 1 | `auth.py:44` `_issue_session_token` | **session** | `sub` | 10 y (Admin/SA) / 24 h | ✅ **yes** |
| 2 | `auth.py:93` | interim OTP | `sub`, `purpose` | OTP+5 min | ❌ no — see below |
| 3 | `auth.py:218` support direct-login | **session** | `sub` | 24 h | ✅ **yes** |
| 4 | `auth.py:422` reset confirmation | reset-scoped | `sub`, `purpose="reset_ok"` | 15 min | ❌ no |

Sites 2 and 4 are short-lived, purpose-scoped, and validated by `_decode_purpose_token`
(`auth.py:102`) rather than `get_current_user`. They never reach the authenticated path. Adding
the claim there is harmless but pointless; **omitting it is correct** and keeps the change minimal.

**Site 3 is easy to miss.** Support agents bypass OTP (SEC-014) and get their token from a
different line than everyone else. A change that only touches `_issue_session_token` would leave
every support token unversioned.

## 1.2 Decoding — 3 sites, and one is a blocker

| # | Site | Purpose | Checks today | Action |
|---|---|---|---|---|
| 1 | `deps.py:21` `get_current_user` | all HTTP auth | signature, `exp`, `user.active` | ✅ add version check |
| 2 | `auth.py:102` `_decode_purpose_token` | OTP / reset | signature, `exp`, `purpose` | ❌ leave alone |
| 3 | **`support.py:167` WebSocket** | **chat auth** | signature, `sub`, `user.active`, role | 🔴 **must add version check** |

### 🔴 Blocker — the support WebSocket is a second authentication path

```python
@router.websocket("/ws")
async def support_ws(websocket: WebSocket, token: str):     # support.py:165
    payload = decode_token(token)
    if not payload or not payload.get("sub"):
        await websocket.close(code=4401); return
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == int(payload["sub"])))).scalar_one_or_none()
    if not user or not user.active or user.role not in (MERCHANT, SUPPORT_AGENT):
        await websocket.close(code=4403); return
```

It does **not** use `get_current_user` — it reimplements validation. If `token_version` is added
only to `deps.py`, a revoked token is rejected on every REST call but **still opens a chat
WebSocket**. That is a revocation bypass, and it must be closed in the same commit.

**Two further WebSocket facts that need explicit decisions:**

**(a) Long-lived connections survive revocation.** Even with a check at connect time, an
already-open socket keeps streaming — nothing re-validates mid-connection. Recommendation: check
at connect (closes the bypass) and accept the residual window, or add a periodic re-check. The
window is bounded by how long a chat session stays open.

**(b) The token travels in the query string** (`api.ts:411`):
```js
const q = `?token=${encodeURIComponent(token)}`;
```
URLs land in access logs, proxy logs and browser history in a way `Authorization` headers do not.
Out of scope here, but it belongs in the security backlog alongside SEC-003.

## 1.3 Refresh, forwarding, background jobs, webhooks, service-to-service

| Concern | Finding |
|---|---|
| **Refresh** | None exists. No refresh endpoint, no refresh token. |
| **Forwarded** | No JWT is forwarded anywhere. |
| **Background jobs** | **None.** No scheduler, no Celery, no `create_task`, no `@repeat_every`. `lifespan` (`main.py:12`) is startup/shutdown only. Redis is cache + rate limit, not a queue. Nothing runs without a request. |
| **Webhooks** | Telegram and Meta use their own shared secrets (`telegram.py:47`, `whatsapp.py:99`). **No platform JWT involved.** Unaffected. |
| **Service-to-service** | None. The only outbound `Bearer` is `whatsapp.py:277`, carrying `WHATSAPP_TOKEN` — a Twilio/Meta credential, not a platform JWT. |
| **SSE `/api/active-users/stream`** | Uses `Depends(get_current_admin)` → `get_current_user`. **Covered automatically.** Note it is long-lived like the WebSocket: revocation applies at connect. |

**Conclusion: two code paths validate platform JWTs — `deps.py` and `support.py`. Both must
change together.**

---

# 2. Where invalidation *should* fire

## 2.1 Password change — 6 call sites, none currently invalidate

`set_password` (`passwords.py:38`) writes `password_history` and `hashed_password`. **It does not
touch sessions or tokens.** Today, changing a password anywhere leaves every existing token valid.

| # | Site | Scenario | Should invalidate? |
|---|---|---|---|
| 1 | `auth.py:445` | password **reset** via forgot-password | ✅ **yes — highest priority** |
| 2 | `users.py:300` | admin sets another user's password | ✅ yes |
| 3 | `users.py:321` | `POST /users/change-password` — self-service, requires current password | ⚠️ yes, but **keep the current session alive** |
| 4 | `users.py:341` | `PATCH /users/me` — profile update with optional `new_password` | ⚠️ same |
| 5 | `support_management.py:377` | admin resets a support member's password | ✅ yes |

Site 1 matters most: password reset is the recovery path after a suspected compromise. If it
doesn't invalidate, an attacker who stole a token keeps access **even after the victim resets
their password** — which is precisely when the user believes they have locked the attacker out.

**Recommended:** increment `token_version`, then re-issue a token to the caller for sites 3 and 4
so a user changing their own password isn't logged out mid-session.

**Best placed inside `set_password`** so no future caller can forget — with an opt-out parameter
for the self-service case.

## 2.2 Account disable — 2 sites

| Site | Path | Status |
|---|---|---|
| `users.py:250` `PATCH /users/{id}/toggle` | admin activates/deactivates | ✅ **already effective** |
| `support_management.py:438` | support member archived | ✅ already effective |

Both set `user.active = False`, and `get_current_user` already rejects inactive users
(`deps.py:31`) — as does the WebSocket (`support.py:178`). **This is the one revocation path that
works today.** Incrementing `token_version` here is belt-and-braces, not required.

## 2.3 Admin actions that should force logout

| Action | Site | Recommendation |
|---|---|---|
| Deactivate user | `users.py:250` | works via `active`; increment anyway for consistency |
| Reset another user's password | `users.py:300`, `support_management.py:377` | ✅ increment |
| Archive support member | `support_management.py:438` | works via `active` |
| **"Log out everywhere"** | **does not exist** | new endpoint — increment only |
| Change role / `merchant_role` | **no such endpoint found** | n/a today; if added later, must increment — permissions are read live from `users`, but a stale session is still worth ending |

**Note:** role changes are read from the `users` row on every request, so a privilege change takes
effect immediately without re-issuing. That is a genuine strength of the current design and
`token_version` does not disturb it.

## 2.4 External integrations relying on current JWT behaviour

**None.** Verified: no third party is issued or validates a platform JWT. Telegram and Meta
webhooks authenticate with their own secrets. Melento (KYC), Brevo/Gmail (SMTP), Twilio and
Razorpay IFSC are all outbound calls using their own credentials. The Anthropic API route
(`/api/ai`) uses `ANTHROPIC_API_KEY`.

**The only consumers of platform JWTs are the two first-party frontends.** No partner integration
can break.

---

# 3. Migration concerns for existing tokens

**The core compatibility question.** Live tokens carry `{"sub", "exp"}` and no version claim.

**Strategy: treat a missing claim as version 0.**

```python
token_ver = payload.get("ver", 0)          # absent → 0
if token_ver != (user.token_version or 0):
    raise credentials_exception
```

With the column defaulting to `0`, every existing token continues to work. **Zero forced logouts.**

| Risk | Assessment |
|---|---|
| Existing admin tokens (10 y) | keep working — intended; step 2 shortens the lifetime |
| Existing 24 h tokens | keep working, expire naturally within a day |
| Column default | `DEFAULT 0 NOT NULL`, additive, matches `migrate.py` convention |
| Rollback with versioned tokens live | safe — the older build ignores the unknown `ver` claim |
| Grandfathering weakness | ⚠️ **real**: until a user's version is first incremented, their pre-existing token stays valid. A stolen admin token is *not* retroactively killed. **A one-off bump of every row after deploy closes this** — at the cost of logging everyone out once. Recommended, scheduled deliberately. |

**Claim naming:** use `ver`. Short, and `jose` passes unknown claims through untouched — verified
by `decode_token` returning the raw payload (`security.py:46`).

---

# 4. Implementation plan

## 4.1 Deployment order

**Stage 1 — schema (no behaviour change).** Add `users.token_version INTEGER DEFAULT 0 NOT NULL`
to the model and `migrate.py` `_NEW_COLUMNS`. Deploy. Nothing reads it yet. Independently
revertible; the column is inert.

**Stage 2 — issue and check the claim (backend only, one commit).**
- `_issue_session_token` (`auth.py:44`) and support login (`auth.py:218`) add `"ver"`
- `get_current_user` (`deps.py`) compares, missing → 0
- **`support.py:167` WebSocket compares too** ← the blocker; must ship together
- Deploy. Existing tokens unaffected.

**Stage 3 — wire invalidation.** Increment in `set_password` (with self-service opt-out and
re-issue), and on admin password reset. Add `POST /api/auth/logout-all`.

**Stage 4 — shorten the admin lifetime.** `ADMIN_TOKEN_EXPIRE_DAYS` 3650 → 7–30. Config only,
independently revertible. **Announce first** — admins currently never re-authenticate.

**Stage 5 — optional, scheduled.** One-off `UPDATE users SET token_version = token_version + 1`
to kill all grandfathered tokens. Logs everyone out once. Do it in a low-traffic window.

Stages 1–2 close the vulnerability for all *newly issued* tokens. Stage 5 closes it for existing
ones.

## 4.2 Backward compatibility

| Direction | Behaviour |
|---|---|
| Old token → new backend | ✅ works (missing claim = 0) |
| New token → old backend | ✅ works (unknown claim ignored) |
| Old frontend → new backend | ✅ no API change |
| WebSocket, old token | ✅ works, same rule |

**No frontend change is required at any stage.** That is the decisive advantage over refresh
tokens: with manual deploys and no CI, a backend-only change is the only kind that can be shipped
and reverted safely.

## 4.3 Rollback

| Stage | Rollback | Risk |
|---|---|---|
| 1 | leave the column (harmless) | none |
| 2 | revert both check sites | none — versioned tokens still validate |
| 3 | revert increments | none |
| 4 | restore 3650 | none; already-issued short tokens expire early |
| 5 | **not reversible** — bumped versions cannot be un-bumped | users must log in again |

Only stage 5 is irreversible, and its worst case is a forced re-login.

## 4.4 Testing plan

**Unit** (`tests/test_token_version.py`)
- token with matching `ver` → accepted
- token with stale `ver` → 401
- token with **no** `ver` (legacy) → accepted as 0
- increment invalidates previously-issued tokens
- WebSocket rejects a stale-`ver` token (**closes the §1.2 blocker**)
- OTP and reset tokens still validate — the `purpose` path is untouched
- `user.active = False` still rejects, independently of version

**Integration (demo)** — full login → token → authenticated call → increment → same token now 401
→ re-login works. Password reset invalidates. WebSocket connects, then refuses after increment.

**Regression** — run against the *unfixed* code first and confirm the version tests fail. A test
that passes either way proves nothing; this is what caught the vacuous case in the SEC-001 work.

## 4.5 Production validation checklist

**Before**
- [ ] RDS snapshot
- [ ] Confirm `SECRET_KEY` unchanged (rotation would confound results)
- [ ] Note current active session count

**After stage 1**
- [ ] Column exists, all rows `0`
- [ ] No behaviour change; zero 401s above baseline

**After stage 2**
- [ ] Fresh login succeeds; token contains `ver`
- [ ] A token captured *before* deploy still works (grandfathering)
- [ ] Support-agent login works (separate issuance path)
- [ ] **Support chat WebSocket connects**
- [ ] Zero 401 spike in API logs
- [ ] OTP and password-reset flows unaffected

**After stage 3**
- [ ] Password reset invalidates other sessions
- [ ] Self-service password change does **not** log the user out
- [ ] `logout-all` invalidates every device
- [ ] Audit rows still written

**After stage 4**
- [ ] Admin token carries the shorter `exp`
- [ ] Admins were notified

**After stage 5**
- [ ] All users logged out exactly once
- [ ] Re-login works across all portals
- [ ] Support WebSocket reconnects

---

# 5. Verification notes

**Verified from source:** all 4 creation and 3 decoding sites, by exhaustive search; the WebSocket
path and its independent validation; the absence of background jobs, schedulers, refresh flows and
service-to-service JWT use; webhook authentication using provider secrets; all 6 `set_password`
callers; both `user.active = False` sites; that `get_current_user` and the WebSocket both check
`active`; that no role-change endpoint exists; the query-string token in `api.ts:411`.

**Resolved during review:** `users.py:321` and `:341` are two distinct self-service endpoints —
`POST /users/change-password` (requires the current password) and `PATCH /users/me` (profile
update carrying an optional `new_password`). **Both must be wired in stage 3**; handling only the
obvious one leaves a password-change path that does not invalidate.

**[UNVERIFIED]:** the behaviour of long-lived SSE and WebSocket connections under revocation was
reasoned from the code, not tested — confirm on demo during stage 2.

**Not covered:** the query-string token exposure (§1.2b) — a separate finding for the security
backlog, not a `token_version` concern.
