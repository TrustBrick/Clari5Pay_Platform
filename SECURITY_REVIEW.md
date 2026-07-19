# Clari5Pay — Security Review (Part 4)

**Date:** 2026-07-19 · **Scope:** backend, frontend, infrastructure, IAM
**Method:** source inspection plus non-mutating verification against live production.
Anything not verified is marked **[UNVERIFIED]** rather than inferred.

> ### ⚠️ Correction issued 2026-07-19 — SEC-001 was downgraded Critical → Medium. See §SEC-001.

---

## Summary

| Severity | Count |
|---|---|
| 🔴 Critical | **0** |
| 🟠 High | 3 |
| 🟡 Medium | **7** |
| 🔵 Low | 4 |
| ⬜ Withdrawn | 1 |

**Most serious open finding: SEC-002** — Admin/Super Admin tokens are valid for ten years and,
now verified, **cannot be revoked by any mechanism**.

**Revision history**

| Date | Change |
|---|---|
| 2026-07-19 (r1) | Initial publication. SEC-001 rated Critical — assessed as a platform-wide MFA bypass. |
| 2026-07-19 (r2) | **SEC-001 → Medium.** The setting it writes is vestigial and never consulted by the login flow, so the endpoint cannot disable authentication. Production was never single-factor. Added SEC-001b (the vestigial setting is itself a defect). |
| 2026-07-19 (r3) | **Full re-verification pass.** Every remaining finding re-traced to code rather than inference. **SEC-002 confirmed and now the top finding** — no revocation exists anywhere. **SEC-006 withdrawn** — its claim that a sequence name derived from merchant config was false; all interpolated values are module constants. **SEC-005 bypass list corrected** — one asserted bypass removed as untraced. **SEC-014 added** for the Support Agent OTP exemption. |

**Verification standard applied in r3.** Every finding below has been traced to the code path that
enforces (or fails to enforce) it. Reachability and impact are treated as separate questions
requiring separate evidence — the r1 error was verifying the former and assuming the latter. Where
a claim could not be traced, it has been withdrawn rather than softened.

The platform's security fundamentals are largely sound: parameterised queries throughout, bcrypt
password hashing with a real complexity policy, Redis-backed rate limiting on authentication,
least-privilege IAM, and a strict CSP. No finding in this review permits authentication bypass,
privilege escalation, or unauthenticated access to customer data.

---

# 🟡 SEC-001 — Unauthenticated write to the OTP configuration setting

**File:** `backend/app/api/routes/auth.py:245`
**Endpoint:** `POST /api/auth/otp-config`
**Severity: MEDIUM** (revised down from Critical — see correction below)
**Status:** reachable unauthenticated on production, confirmed; **fixed** in `bf925456`.

> ### ⚠️ Correction — this was first published as CRITICAL. That was wrong.
>
> The original assessment stated that this endpoint disables multi-factor authentication
> platform-wide, and that production was consequently running single-factor. **Both claims were
> incorrect.**
>
> `_otp_enabled` is referenced in exactly four places: its getter (`auth.py:47`), its setter
> (`:52`), the `/otp-status` GET (`:242`) and this endpoint (`:263`). **It is never consulted by
> the login flow.** `auth.py:214` states it directly — *"OTP is mandatory for every successful
> login … there is no toggle to disable it"* — and `:236` unconditionally issues an OTP after a
> valid credential check.
>
> The setting is **vestigial**. Writing `enabled: false` changes only what `/otp-status` reports,
> which drives a login-page display hint. It does not weaken authentication. This was confirmed
> empirically: production has `otp_enabled = 'false'` and OTP rows were still being issued the
> same day (most recent 2026-07-19 08:24).
>
> **Root cause of the error:** the endpoint was classified from its name, docstring and
> reachability without tracing the setting's consumers. Reachability was verified rigorously; the
> *impact* was assumed. The correct method is to follow a setting to its enforcement point before
> assigning severity — an unauthenticated write is only as severe as what the value controls.

```python
@router.post("/otp-config")
async def otp_config(
    data: OtpConfigRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Toggle login OTP on/off (testing aid)."""
    await _set_otp_enabled(db, data.enabled)
```

No authentication dependency, no role guard, no rate limit. Verified there is no router-level
dependency (`auth.py:25`) and no application-level auth middleware (`backend/main.py`).

**Verification performed (non-mutating):** an empty JSON body was POSTed to the production
endpoint. It returned **HTTP 422** with a Pydantic "field required" error — proving the request
reached the validation layer without encountering any authentication gate. Because `enabled: bool`
(`schemas.py:78`) has no default, validation fails before the handler executes, so **no state was
changed**. The endpoint was never invoked with a valid payload.

**Actual impact (revised).** Anyone on the internet can write the `otp_enabled` row in
`app_settings` and generate audit entries. Because nothing enforces that value, this does **not**
weaken authentication. What it does permit:

- **Misleading UI state** — `/otp-status` will report OTP as disabled while it is in fact enforced,
  so the login page can tell users something untrue about the platform's security posture.
- **Unauthenticated write to a configuration table** by an anonymous caller.
- **Audit-log noise** — an attacker can generate unlimited `OTP_CONFIG` rows with no rate limit,
  diluting a table used for forensics.
- **Latent escalation** — if anyone later wires the setting into the login flow (the natural
  reading of its name), this silently becomes the Critical issue it was first assessed as. That
  possibility is the main reason to fix it rather than delete it.

The change is persisted to `app_settings` (`auth.py:52`), so it survives restarts, and it *is*
written to the audit log — detectable after the fact, but not prevented.

**Recommended fix** — add the guard already used by every comparable endpoint:

```python
@router.post("/otp-config")
async def otp_config(
    data: OtpConfigRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_super_admin),   # ← add
):
```

**Applied in `bf925456`**, together with `actor=actor` on `record_audit`: while the endpoint was
unauthenticated there was no caller identity to record, so all 28 pre-fix production rows are
attributed to `"system"` and are traceable only by IP.

Regression tests: `backend/tests/test_auth_otp_config.py`. Validated against the *vulnerable* code
as well as the fixed code — **6 of 7 fail without the guard, all 7 pass with it** — so they detect
the defect rather than passing vacuously.

**Production audit review.** 28 `OTP_CONFIG` events, all between 2026-06-25 and 2026-06-27, none
since. All attributed to `"system"` (no caller identity existed). Seven source IPs: one range
matching the operating team, several Sri Lankan ranges consistent with the merchant base, and one
Cloudflare IP where the real client address was not resolved. Rapid on/off toggling across a
two-day window is consistent with the "testing aid" purpose. **Assessment: internal testing, not
attack — but the audit trail cannot prove it**, which is precisely the gap the `actor` fix closes.

---

## SEC-001b — The OTP setting is vestigial (new finding, arising from the correction)

**Severity: MEDIUM** · `auth.py:47,52,242,263`

`otp_enabled` is written and read but never enforced. `/otp-status` currently returns `false` on
production while OTP is unconditionally required at login. A security control that *appears*
configurable but is not is worse than either a working toggle or no toggle: operators may believe
MFA is off when it is on, or trust a switch that does nothing.

**Recommendation:** either wire `_otp_enabled` into the login flow (`auth.py:236`) so the setting
means what it says, or remove the setting, both endpoints, and the login-page hint. Do not leave it
half-implemented.

**Note:** `GET /api/auth/otp-status` (`auth.py:239`) is also unauthenticated. That one is
legitimate — the login page must know whether to show the OTP field — and discloses only a boolean.

---

## SEC-014 — Support Agents are exempt from OTP

**Severity: MEDIUM** · `backend/app/api/routes/auth.py:217-233`

```python
if user.role == UserRole.SUPPORT_AGENT:
    token = create_access_token({"sub": str(user.id)})
    ...
    return {"access_token": token, ...}     # returns before _issue_otp
```

Every other role reaches `return await _issue_otp(db, user, ip)` (`:236`). `SUPPORT_AGENT` returns
a token directly from the credential check — **password only, no second factor**.

**Is this intentional or a risk? Both — it is an intentional decision with unacknowledged risk.**

*Intentional:* the code says so explicitly — *"Support agents use the separate support portal's
direct-login flow (that portal has no OTP screen), so they remain exempt by design."* It is a
deliberate consequence of the support portal being a separate application without an OTP screen,
not an oversight.

*Risk:* the justification is **implementation convenience, not a risk assessment**. Support agents
can read customer support conversations, which in a payments context routinely contain transaction
references, partial account details and personal information. The role is exempted because its UI
lacks a screen — which is a reason to build the screen, not to weaken the control.

Compounding factors, both verified elsewhere in this report:
- Support tokens last 24h (better than admin's 10 years) but are still **unrevocable** (SEC-002)
- They are stored in `localStorage` (SEC-003)

**Recommendation:**
1. **Record the exemption as an explicit, dated decision** with a named owner — not an inline code
   comment. It currently survives only as a remark that a future refactor could silently drop or
   widen.
2. **Review it periodically** (suggest six-monthly, and on any change to support-agent
   permissions). The correct question each time: *does this role still see little enough that
   single-factor is acceptable?* As the support module gains capability, the answer drifts.
3. **Build the OTP screen in the support portal** and remove the exemption. This is the durable
   fix; the exemption exists only because that screen does not.
4. If the exemption is kept, **compensate**: shorter token lifetime for this role, and IP or
   device restrictions if support staff work from known locations.

---

# 🟠 HIGH

## SEC-002 — Admin and Super Admin tokens are valid for 10 years

**File:** `backend/app/core/config.py:14` — `ADMIN_TOKEN_EXPIRE_DAYS: int = 3650`

Merchant and support tokens expire in 24 hours (`:10`). Admin and Super Admin tokens — the most
privileged on the platform — last a decade.

The code documents the lifetime as deliberate ("no inactivity/session timeout … they stay signed
in until they explicitly log out").

**✅ NOW VERIFIED — there is no revocation of any kind.** The first publication left this open;
it has since been traced end to end:

1. **`get_current_user` never consults `user_sessions`** (`deps.py:12-33`). It decodes the JWT,
   loads the user by id, and checks `user.active`. That is the entire check on every request.
2. **Logout does not revoke.** `auth.py:275` states it plainly — *"Access tokens here are
   stateless JWTs, so they are not server-side revoked — the client clears its own
   token/cookies/storage."* Logout writes an audit row and ends the presence session; the token
   keeps working.
3. **No revocation mechanism exists.** A search for `jti`, denylist, blacklist, revoke or
   token_version across `backend/app` returns only that one comment.

**Consequence.** A leaked Admin or Super Admin token is valid for **ten years** and **cannot be
revoked**. The only remedies are deactivating the user account (`user.active = False`, which the
per-request check does catch) or rotating `SECRET_KEY`, which invalidates every session on the
platform at once. There is no way to revoke one compromised token.

**This is now the most serious finding in this review.** It is rated High rather than Critical
because it requires prior token theft — it is not remotely exploitable on its own. But note the
chain: **SEC-005** (stored XSS on a public page) → **SEC-003** (token readable from
`localStorage`) → **SEC-002** (that token then works for a decade, unrevocably). Each link is
individually modest; together they are not.

**Recommendation, in priority order:**
1. Enforce the existing `user_sessions` record on every request — the table already exists and is
   populated, so this is a lookup in `get_current_user`, not new infrastructure.
2. Reduce `ADMIN_TOKEN_EXPIRE_DAYS` from 3650 to days, with a refresh token for convenience.
3. Ensure logout marks the session inactive so step 1 makes it effective.

## SEC-003 — JWT stored in `localStorage`

**File:** `frontend/src/context/AuthContext.tsx:39`

```js
localStorage.setItem('clari5pay_token', accessToken);
```

`localStorage` is readable by any JavaScript on the origin, so any XSS becomes full token theft —
and with SEC-002, a stolen admin token is valid for 10 years. An `httpOnly` cookie would be
unreadable by script.

Mitigating: the CSP is strict, and no third-party scripts are loaded. But `unsafe-inline` is
permitted for scripts (`Caddyfile`), which weakens the protection materially.

**Recommendation:** move to `httpOnly` + `Secure` + `SameSite=Strict` cookies. Non-trivial (CSRF
tokens become necessary), so treat as planned work, not a hotfix.

## SEC-004 — Rate limiter fails open

**File:** `backend/app/core/ratelimit.py:54`

```python
except Exception as exc:
    logger.warning("[ratelimit] backend error, failing open: %s", exc)
```

If Redis is unavailable, all rate limiting silently stops — including on `/api/auth/login`, OTP
verification, and password reset. An attacker who can disrupt Redis, or who catches an outage,
gets unlimited credential-stuffing attempts.

Failing open is a defensible availability choice; failing open **silently on authentication
endpoints** is not. At minimum this should alert. For login specifically, failing closed is the
safer default.

---

# 🟡 MEDIUM

## SEC-005 — HTML sanitiser is a bypassable regex denylist

**File:** `frontend/src/pages/BlogPages.tsx:16`, rendered via `dangerouslySetInnerHTML` at `:145`

```js
const sanitize = (html) => html
  .replace(/<\s*script[^>]*>[\s\S]*?<\s*\/\s*script>/gi, '')
  .replace(/ on\w+\s*=\s*"[^"]*"/gi, '')
  .replace(/ on\w+\s*=\s*'[^']*'/gi, '')
  .replace(/javascript:/gi, '');
```

Denylist sanitisers are bypassable by construction. Two bypasses are verifiable directly from the
patterns above:

- **Unquoted handlers** — `<img src=x onerror=alert(1)>`. Both handler patterns require a quote
  character (`"[^"]*"` / `'[^']*'`); an unquoted attribute value matches neither.
- **Non-space separators** — `<svg/onload=alert(1)>`. Both patterns require a **leading space**
  (` on\w+`); a `/` separator, which HTML accepts, matches neither.

*(An earlier revision also claimed a nested-tag bypass, `<scr<script>ipt>`. That was asserted from
familiarity rather than traced against these specific patterns, and has been removed. The two
above are sufficient and are demonstrable from the regexes themselves.)*

Blog content is rendered on the **public** site, so injected script would execute for every
visitor.

**Mitigated to Medium** because authoring requires `get_current_admin` (`blogs.py:93`) — this is
stored XSS by a privileged user, not an anonymous one. Still, an admin account compromise escalates
into visitor-wide script execution, and the CSP's `unsafe-inline` does not block it.

**Recommendation:** replace with DOMPurify. Two lines, and correct by allowlist.

## ~~SEC-006 — Raw SQL built with f-strings~~ — WITHDRAWN, not a finding

**Withdrawn on re-verification.** The first revision listed this as Medium and stated that
`transactions.py:204` "deserves attention because `seq` is derived from merchant configuration".
**That was incorrect.** Every interpolated value traces to a module-level constant:

| Site | Interpolated value | Source |
|---|---|---|
| `transactions.py:204` | `seq` | `_REF_SEQUENCES[kind]` — a 3-entry constant dict (`:192`). `kind` is a literal `"DEP"`/`"WIT"`/`"SET"` at all three call sites (`:1553,1635,1700`). |
| `demo_admin.py:63,65` | `tables`, `seq` | `_RESET_TABLES` / `_RESET_SEQUENCES` constants. Additionally gated by `settings.is_demo` (403 in production) and a `confirm == "RESET"` body check. |
| `migrate.py:212,262,265` | column/sequence names | Constant DDL lists, executed at startup only. |

The merchant's configured code *is* used at `transactions.py:205`, but only to build a Python
string for the reference prefix — **it never reaches SQL**. There is no path from request input to
any of these statements.

Retained in the report as a withdrawn entry rather than deleted, so the correction is visible to
anyone who read the earlier revision. Recorded as a **style note, not a risk**: constants in
f-string SQL are safe today, and PostgreSQL cannot parameterise identifiers regardless.

## SEC-007 — No CSRF protection

No CSRF tokens anywhere in the backend. Currently **not exploitable**, because authentication uses
a `Bearer` header rather than cookies, and browsers do not attach headers automatically. Recorded
because it becomes a real vulnerability the moment SEC-003 is fixed by moving to cookies — the two
must be addressed together.

## SEC-008 — PII stored unencrypted at column level

Bank account numbers and IFSC codes (`merchant_bank_accounts`, `transactions`), Aadhaar photos
(`kyc_verification_history.aadhaar_photo`), phone numbers and emails are stored as plaintext
columns. RDS encryption at rest is enabled, and S3 uses SSE-S3, so this is protected against media
theft — but not against a database credential compromise or an over-broad query.

For a payments platform handling Aadhaar data, column-level encryption or tokenisation of the most
sensitive fields is the standard expectation.

**[UNVERIFIED]** whether any regulatory regime (RBI/PCI-DSS) applies here and mandates it.

## SEC-009 — `merchant_role` is unconstrained free text

**File:** `models.py:89` — `String(32)`, no enum, no CHECK constraint.

Authorization decisions depend on it (`deps.py:80`). A typo or unexpected value fails
authorization *open or closed* depending on the comparison, and the database will accept any
string. `UserRole` is a proper enum; this one should be too.

---

# 🔵 LOW

## SEC-010 — Default JWT secret in source
`config.py:8` ships `SECRET_KEY = "changeme-super-secret-jwt-key-at-least-32-chars"`. **Verified
production overrides it** (64 chars, not the default). The risk is a new environment silently
booting with a publicly-known signing key. **Recommendation:** fail startup if the default is
detected outside development.

## SEC-011 — Verbose validation errors
Pydantic errors return field names, types and the submitted input (as seen in the SEC-001 probe).
Minor information disclosure about internal schema shape.

## SEC-012 — `AWS_REGION` default is wrong for this deployment
`config.py:26` defaults to `eu-north-1`; the platform runs in `ap-south-1`. Not a vulnerability,
but a misconfiguration that has already caused one production defect (the presigned-URL failure)
and could route data to an unintended region.

## SEC-013 — No automated security testing
No dependency scanning, SAST, or secret scanning. No CI at all (verified: no `.github/workflows`),
so nothing runs on push.

---

# What is done well

These are verified strengths, not assumptions:

**No SQL injection via ORM.** All user-facing queries use SQLAlchemy with bound parameters. The
f-string cases (SEC-006) involve identifiers, not values, and none take request input.

**Password handling is correct.** bcrypt via passlib (`security.py:11,33`); a real complexity
policy — 8+ chars, upper, lower, digit, special (`:15`); `password_history` prevents reuse;
`failed_attempts` + `locked_until` provide lockout.

**Authentication rate limiting is present and well-tuned** — login 30/60s, verify-otp 20/60s,
resend-otp 5/300s, forgot-password 5/300s (`auth.py:147,281,339,351`). Stricter limits on the
expensive operations, which is the right instinct.

**Authorization is systematic.** 13 role dependencies in `deps.py`; **190 of 197 endpoints carry an
explicit guard**. The 7 without are: 2 legitimate public endpoints, 4 provider webhooks with their
own secret verification, and SEC-001.

**Webhooks verify their callers.** Telegram checks `X-Telegram-Bot-Api-Secret-Token`
(`telegram.py:47`); Meta verifies `hub.verify_token` (`whatsapp.py:99`).

**IAM is genuinely least-privilege.** `s3:GetObject`/`PutObject` on one prefix, `ListBucket` on the
bucket, and **no `DeleteObject` anywhere** — a compromised application cannot destroy stored
documents. Environments were separated into distinct roles on 2026-07-19.

**S3 is correctly locked down.** Block Public Access on, versioning on, SSE-S3, access only via
short-lived presigned URLs minted after endpoint authorization.

**File uploads are validated centrally.** `core/uploads.py` enforces a MIME allowlist and a 5 MB
decoded cap; Caddy imposes a 12 MB body cap as an outer backstop.

**CSP is strict and CORS is explicitly allowlisted** — no wildcard origins (`main.py:41`).

**Audit coverage is real.** `audit_logs` (4,738 rows), `system_logs` (2,680), and `record_audit` /
`log_event` calls on sensitive operations — including, usefully, on SEC-001 itself.

---

# Recommended order

Re-ordered after the SEC-001 correction. **SEC-002 is now the most urgent item** — it was second
only because SEC-001 was overstated.

| Priority | Finding | Effort |
|---|---|---|
| **1 — this week** | **SEC-002** enforce `user_sessions` in `get_current_user`; reduce `ADMIN_TOKEN_EXPIRE_DAYS`. Verified: a leaked admin token is currently valid 10 years and unrevocable. | small–moderate |
| 2 — this week | SEC-005 replace the regex sanitiser with DOMPurify (breaks the XSS→token-theft chain) | small |
| 3 — this week | SEC-004 alert on rate-limiter failure; fail closed on login | small |
| 4 — normal cycle | **SEC-001 deploy `bf925456`** (fixed, tested, awaiting release) | done |
| 5 — this month | SEC-001b wire up or remove the vestigial OTP setting | small |
| 6 — this month | SEC-014 record and schedule review of the Support Agent exemption; plan the support-portal OTP screen | small |
| 7 — this month | SEC-009 constrain `merchant_role` to an enum | small |
| 8 — planned | SEC-003 + SEC-007 move to httpOnly cookies with CSRF | significant |
| 9 — planned | SEC-008 column encryption for Aadhaar/bank data | significant |
| 10 — planned | SEC-013 CI with dependency and secret scanning | moderate |

Items 1 and 2 together break the highest-impact chain in the report
(**SEC-005 → SEC-003 → SEC-002**): stored XSS on a public page steals a `localStorage` token that
then works, unrevocably, for a decade.

---

# Verification notes

**Verified by source inspection:** all file and line references; the endpoint/guard census; CORS,
CSP and rate-limit configuration; password policy; IAM policy documents.

**Verified against live production (non-mutating):** SEC-001 reachability (HTTP 422, no state
change); `SECRET_KEY` is not the default; S3 permissions including delete-denied.

**[UNVERIFIED], after r3:** applicable regulatory regime (SEC-008 — a business/legal question, not
answerable from code); dependency CVE status, since no scanning exists; runtime frontend
behaviour; the origin of the two orphan database tables noted in `ARCHITECTURE.md`.

*(SEC-002's revocation question, previously listed here as the most important open item, was
resolved in r3: there is no revocation. See SEC-002.)*

**Not performed:** penetration testing, authenticated session testing, fuzzing, or dependency CVE
analysis. This is a code and configuration review. A review of this kind cannot prove the absence
of vulnerabilities — only the presence of the ones it found.

**Method note, added with the SEC-001 correction.** The initial publication of this report rated
SEC-001 Critical on the strength of the endpoint's name, docstring and confirmed reachability. The
reachability was verified rigorously; the *impact* was not — the setting's consumers were never
traced, and it turned out nothing consumed it. Reachability and impact are separate questions and
require separate evidence. **For every future finding in this review series: follow the value to
its enforcement point before assigning severity.** An unauthenticated write is only as severe as
what the written value controls.
