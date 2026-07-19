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
| 🟡 Medium | **6** |
| 🔵 Low | 4 |

**Revision history**

| Date | Change |
|---|---|
| 2026-07-19 (initial) | SEC-001 published as Critical — assessed as a platform-wide MFA bypass |
| 2026-07-19 (revised) | **SEC-001 downgraded to Medium.** The setting it writes is vestigial and is not consulted by the login flow, so the endpoint cannot disable authentication. The original assessment was wrong; see the correction note in SEC-001. |

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

**Related, for explicit decision:** `SUPPORT_AGENT` logins bypass OTP entirely (`auth.py:217`).
This is documented as deliberate — the support portal has no OTP screen — but it is an
authentication exemption for a role with access to customer conversations, and deserves a
conscious ruling rather than inheritance.

**Note:** `GET /api/auth/otp-status` (`auth.py:239`) is also unauthenticated. That one is
legitimate — the login page must know whether to show the OTP field — and discloses only a boolean.

---

# 🟠 HIGH

## SEC-002 — Admin and Super Admin tokens are valid for 10 years

**File:** `backend/app/core/config.py:14` — `ADMIN_TOKEN_EXPIRE_DAYS: int = 3650`

Merchant and support tokens expire in 24 hours (`:10`). Admin and Super Admin tokens — the most
privileged on the platform — last a decade.

The code documents this as deliberate ("no inactivity/session timeout … they stay signed in until
they explicitly log out"), and `user_sessions` supports revocation. But a leaked admin token from a
stolen laptop, browser history, proxy log, or XSS remains valid essentially forever, and JWTs are
stateless — possession is sufficient unless revocation is checked on every request.

**[UNVERIFIED]** whether `user_sessions` revocation is enforced on each request or only at login.
That determines whether this is High or Critical. **Worth confirming immediately.**

**Recommendation:** reduce to days rather than years, with a refresh token; or enforce a
server-side session check on every privileged request.

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

Denylist sanitisers are bypassable by construction. Concretely, this one misses:

- **Unquoted handlers** — `<img src=x onerror=alert(1)>`; the patterns require quotes
- **Non-space separators** — `<svg/onload=alert(1)>`; the patterns require a leading space
- **Nested tags** — `<scr<script>ipt>` reassembles after the inner match is removed

Blog content is rendered on the **public** site, so injected script would execute for every
visitor.

**Mitigated to Medium** because authoring requires `get_current_admin` (`blogs.py:93`) — this is
stored XSS by a privileged user, not an anonymous one. Still, an admin account compromise escalates
into visitor-wide script execution, and the CSP's `unsafe-inline` does not block it.

**Recommendation:** replace with DOMPurify. Two lines, and correct by allowlist.

## SEC-006 — Raw SQL built with f-strings

**Files:** `transactions.py:204`, `demo_admin.py:63,65`, `migrate.py:212,262,265`

```python
n = (await db.execute(text(f"SELECT nextval('{seq}')"))).scalar_one()
await db.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
```

**Not currently exploitable.** In every case the interpolated value derives from internal
constants or a merchant's configured prefix, not from request input — and PostgreSQL cannot
parameterise identifiers or sequence names anyway.

The risk is future drift: this is the pattern that becomes an injection the moment someone passes
user input into `seq` or `tables`. `transactions.py:204` deserves attention because `seq` is
derived from merchant configuration, and `users.pay_in`/`pay_out`/`settlement` are
`String(8)` with **no character-set constraint**.

**Recommendation:** validate against a strict allowlist regex (`^[a-z_]+$`) before interpolation.

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
| **1 — this week** | **SEC-002** verify `user_sessions` revocation is enforced per request. Determines whether a 10-year admin token is revocable at all. | investigation |
| 2 — this week | SEC-004 alert on rate-limiter failure; fail closed on login | small |
| 3 — this week | SEC-005 replace the regex sanitiser with DOMPurify | small |
| 4 — normal cycle | **SEC-001 deploy `bf925456`** (fixed, tested, awaiting release) | done |
| 5 — this month | SEC-001b wire up or remove the vestigial OTP setting; rule on the SUPPORT_AGENT exemption | small |
| 6 — this month | SEC-009 constrain `merchant_role`; SEC-006 allowlist SQL identifiers | small |
| 7 — planned | SEC-003 + SEC-007 move to httpOnly cookies with CSRF | significant |
| 8 — planned | SEC-008 column encryption for Aadhaar/bank data | significant |
| 9 — planned | SEC-013 CI with dependency and secret scanning | moderate |

---

# Verification notes

**Verified by source inspection:** all file and line references; the endpoint/guard census; CORS,
CSP and rate-limit configuration; password policy; IAM policy documents.

**Verified against live production (non-mutating):** SEC-001 reachability (HTTP 422, no state
change); `SECRET_KEY` is not the default; S3 permissions including delete-denied.

**[UNVERIFIED]:** whether `user_sessions` revocation is checked per-request (SEC-002 — the single
most important open question); applicable regulatory regime (SEC-008); dependency CVE status, since
no scanning exists; runtime frontend behaviour.

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
