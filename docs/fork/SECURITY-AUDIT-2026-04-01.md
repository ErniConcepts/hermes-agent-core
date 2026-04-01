# Security Audit — hermes-agent-core fork

**Date:** 2026-04-01
**Branch:** feature/browser-tool
**Scope:** Full fork — auth, web app, runtime containers, file system, secret handling, agent tools, Tailscale integration, install scripts
**Auditor:** Claude Code (claude-sonnet-4-6)

---

## Summary

| # | Severity | Area | Title |
|---|----------|------|-------|
| F-1 | **High** | Container | gVisor registered with `--network=host` — partially defeats sandbox isolation |
| F-2 | **High** | Secrets | `OPENAI_BASE_URL` leaked in 503 error messages returned to users |
| F-3 | **High** | Session | Session signing key derived from OIDC client secret via SHA-256 |
| F-4 | **High** | Auth | Invite token does not enforce Tailscale login match at claim time |
| F-5 | **Medium** | Auth | Rate-limit state is in-process memory — resets on restart, doesn't cover distributed attacks |
| F-6 | **Medium** | Files | Workspace path resolution does not guard against symlink traversal |
| F-7 | **Medium** | HTTP | `/healthz` is unauthenticated and discloses `issuer_url` + internal URLs |
| F-8 | **Medium** | Files | Workspace directories set to `0o777` (world-writable) on the host |
| F-9 | **Medium** | Tools | Tirith security scanner defaults to fail-open — silent allow on any error |
| F-10 | **Medium** | Tools | Non-interactive non-gateway execution auto-approves all dangerous commands |
| F-11 | **Low** | Auth | `_AUTH_RATE_LIMITS` dict grows unboundedly — potential memory exhaustion |
| F-12 | **Low** | OIDC | OIDC discovery metadata fetched on every callback (no caching) |
| F-13 | **Low** | Install | Install script clones unversioned `main` branch without commit pinning |
| F-14 | **Low** | Config | Env file values validated only for control chars — no length limit or shell metachar check |
| F-15 | **Low** | HTTP | Admin `GET /api/admin/users` lacks CSRF check (inconsistent, not directly exploitable) |

---

## Positive Findings

Before findings: the following security controls were found to be correctly implemented.

- **CSRF** — Double-submit (session cookie + `X-Hermes-CSRF-Token` header) applied to all mutating POST endpoints.
- **OIDC** — PKCE (S256), state, nonce, `jwt.decode` with audience/issuer/expiry/nonce validation, PyJWKClient for key fetching. Correct.
- **Workspace path traversal** — `_normalize_relative_path` rejects `..` and absolute paths before `resolve()`. Defence in depth.
- **Token entropy** — All auth/runtime tokens use `secrets.token_urlsafe(32)` (256-bit), compared with `secrets.compare_digest`.
- **Session isolation** — User identity flows from authenticated session `user["sub"]` through all workspace and runtime lookups.
- **Container hardening** — `--read-only`, `--cap-drop=ALL`, `--security-opt no-new-privileges`, `--pids-limit`, read-only tmpfs, ports bound to `127.0.0.1`.
- **Invite one-time use** — Claimed tokens are marked `"claimed"` and rejected on reuse.
- **Admin self-protection** — Admins cannot deactivate their own account.

---

## Detailed Findings

---

### F-1 — High — gVisor Registered with `--network=host`

**File:** `hermes_cli/product_install.py:71`

```python
RUNSC_RUNTIME_CONFIG = {"path": "runsc", "runtimeArgs": ["--network=host"]}
```

The installer registers gVisor with `--network=host` as a global runtime argument. This causes every `runsc`-managed container to use the host network stack instead of gVisor's isolated `netstack`. gVisor's kernel emulation of socket syscalls is bypassed; the container process can reach loopback services, other containers' ports, Docker socket, and any host service on `127.0.0.1`.

**Attack scenario:** A compromised model prompt inside user A's gVisor container calls `connect(127.0.0.1, 18092)` — directly reaching user B's runtime service port. The operator assumes cross-container network isolation from gVisor; it isn't present.

**Likely cause:** The inference endpoint is on `127.0.0.1` and `--network=host` was the quick fix to reach it.

**Fix:** Create a dedicated Docker bridge network with a static gateway IP. Add `--add-host inference-endpoint:<gateway-ip>` in `docker_run_command`. Use iptables `DOCKER-USER` to allow container→gateway:inference-port only. Remove `--network=host` from the gVisor runtime config.

---

### F-2 — High — API Key / Base URL Leaked in 503 Error Messages

**File:** `hermes_cli/product_runtime_service.py:195–199`

```python
return (
    503,
    f"Model not available. Check that '{model}' is reachable at {endpoint}.",
)
```

`endpoint` is `OPENAI_BASE_URL`, which may contain embedded auth tokens (e.g., `http://secret-key@host:port`) or reveal internal infrastructure topology. This string is returned to the authenticated user via the SSE stream and non-streaming JSON error body.

**Fix:** Return a generic "Model not available — check server configuration" to the client. Log the endpoint server-side only.

---

### F-3 — High — Session Secret Derived from OIDC Client Secret

**File:** `hermes_cli/product_app.py:177–186`

```python
digest = hashlib.sha256(settings.client_secret.encode("utf-8")).hexdigest()
return f"hermes-product-session-{digest}"
```

SHA-256 is not a KDF — no salt, no stretching, fully deterministic. Anyone who obtains `HERMES_PRODUCT_TSIDP_OIDC_CLIENT_SECRET` can immediately derive the session signing key and forge valid session cookies with `is_admin: true` for any user.

**Fix:** Generate a dedicated random `HERMES_PRODUCT_SESSION_SECRET` with `secrets.token_urlsafe(32)`. Refuse to start (or emit a hard-to-miss warning) when neither `session_secret_ref` nor a pre-configured secret is available. Never derive from another secret.

---

### F-4 — High — Invite Token Does Not Enforce Tailscale Login at Claim Time

**File:** `hermes_cli/product_users.py:268–306`

Admin can optionally restrict an invite to a specific `tailscale_login`. This login is stored on `ProductInviteRecord.tailscale_login`. However, `claim_product_user_from_invite()` only checks that the token exists and has `status == "pending"` — it never verifies that the claiming identity matches `invite.tailscale_login`.

**Attack scenario:** Admin creates an invite for `bob@tailnet`. `carol@tailnet` intercepts the link (e.g., via shared messaging) and claims it as herself, gaining an account intended for Bob.

**Fix:** In `claim_product_user_from_invite`, add:
```python
if match.tailscale_login and _normalize_tailscale_login(match.tailscale_login) != normalized_login:
    raise ValueError("Invite is not valid for this Tailscale identity")
```

---

### F-5 — Medium — In-Process Rate-Limit Resets on Restart

**File:** `hermes_cli/product_app.py:76, 327–335`

```python
_AUTH_RATE_LIMITS: dict[tuple[str, str], deque[float]] = {}
```

The auth rate limiter (10 req / 300 s per IP) is in-process module state — lost on every restart. An attacker who can trigger a service reload gets a fresh window. Also provides no protection against distributed attacks across IPs.

**Fix:** For a Tailscale-only deployment the practical risk is low. Medium priority. If restarts are infrequent: acceptable. For production hardening: move rate-limit state to a Redis counter or a SQLite table with a TTL.

---

### F-6 — Medium — Workspace Symlink Traversal Not Defended

**File:** `hermes_cli/product_workspace.py:89–94`

`_resolve_workspace_path` and `store_workspace_file` call `Path.resolve()` and check path containment, but never call `Path.is_symlink()`. A symlink inside the workspace pointing to another user's workspace (which is under the same product users root) would pass the containment check and allow cross-user file reads/writes.

**Fix:**
```python
# In _resolve_workspace_path, after resolve():
for part in [target] + list(target.parents):
    if part == root:
        break
    if part.is_symlink():
        raise ValueError("Symlinks are not permitted in workspace paths")
```
Also add `if target.is_symlink(): raise ValueError(...)` in `store_workspace_file` before writing.

---

### F-7 — Medium — Unauthenticated `/healthz` Discloses Internal URLs

**File:** `hermes_cli/product_app_root_routes.py:19–25`

`/healthz` is unauthenticated (explicitly exempted from canonical-origin redirect) and returns `issuer_url`, `auth_provider`, and `app_base_url`. Visible to any device on the tailnet.

**Fix:** Either require authentication (even just a valid session), or strip sensitive fields from the public response. A minimal `{"status": "ok"}` is sufficient for load balancers. Move OIDC issuer URL to an authenticated `/api/config` endpoint.

---

### F-8 — Medium — Workspace Directories Set World-Writable (`0o777`)

**File:** `hermes_cli/product_runtime_common.py:72–76`

```python
def secure_runtime_writable_dir(path) -> None:
    path.chmod(0o777)
```

Called for `staged_hermes_home`, `memories/`, `staged_workspace_root`, and `.tmp/` on the host filesystem. On a shared GPU server where other users have SSH access, `0o777` means any local user can read or overwrite any Hermes user's workspace.

**Fix:** Use `0o755` (readable by all, writable only by owner). Since containers already run as `--user uid:gid` matching the workspace owner (`runtime_container_user`), world-write is not needed. If the container UID truly differs from the directory owner, use `setfacl` to grant the specific container UID access rather than opening it to all.

The function name `secure_runtime_writable_dir` is misleading — rename it or add a comment explaining why wide permissions are intentional (or remove the function and inline `0o755`).

---

### F-9 — Medium — Tirith Security Scanner Defaults to Fail-Open

**File:** `tools/tirith_security.py:73–84`

```python
"tirith_fail_open": True,
```

Any spawn error, timeout, or installation failure silently allows commands through without tirith's content-level scanning (prompt-injection detection, homograph URLs, terminal injection sequences). During background install (first startup), all commands run unscanned.

**Fix:** Consider changing the default to `tirith_fail_open: false` for the product runtime profile. The product runtime runs in a gVisor container so the blast radius of a missed injection is contained, but the `HERMES_INTERACTIVE` fallback (F-10 below) means dangerous commands would still be auto-approved regardless of tirith in that mode.

---

### F-10 — Medium — Non-Interactive Mode Auto-Approves All Dangerous Commands

**File:** `tools/approval.py:373–376`

```python
if not is_cli and not is_gateway:
    return {"approved": True, "message": None}
```

When `HERMES_INTERACTIVE` and `HERMES_GATEWAY_SESSION` are both unset, all approval checks are bypassed and every command is auto-approved. The product runtime sets `HERMES_PRODUCT_RUNTIME_MODE=product` but not `HERMES_INTERACTIVE` or `HERMES_GATEWAY_SESSION`.

For the product runtime this falls into the auto-approve path. Mitigated by gVisor sandbox. More critical for `batch_runner.py` or benchmark environments with `TERMINAL_ENV=local`.

**Fix:** Add `HERMES_PRODUCT_RUNTIME_MODE` to the set of env vars that trigger approval checking. Or explicitly set `HERMES_GATEWAY_SESSION=1` in the product runtime environment.

---

### F-11 — Low — `_AUTH_RATE_LIMITS` Dict Grows Unboundedly

**File:** `hermes_cli/product_app.py:76`

Old deque entries are removed from within a bucket when accessed but the dict key is never evicted. On a public-facing deployment this would be a memory exhaustion DoS. On a Tailscale-only deployment, the tailnet is small — low practical risk.

**Fix:** Periodically evict empty buckets (e.g., in a background thread or on each rate-limit check). Or use a TTL-based cache like `cachetools.TTLCache`.

---

### F-12 — Low — OIDC Discovery Metadata Not Cached

**File:** `hermes_cli/product_app_auth_routes.py:39–40`

`discover_product_oidc_provider_metadata` (a synchronous HTTP GET to `{issuer}/.well-known/openid-configuration`) is called on every OIDC callback. 10-second timeout. If tsidp is slow during login, every concurrent callback blocks for up to 10s.

**Fix:** Cache the metadata dict (e.g., with `functools.lru_cache` with TTL, or a module-level dict with an expiry timestamp). 5–15 minute TTL is appropriate.

---

### F-13 — Low — Install Script Clones Unversioned Branch

**File:** `scripts/install.sh:29–40`

```bash
git clone --branch main $REPO_URL_HTTPS
```

Clones `main` HEAD without pinning a commit hash. Supply-chain compromise of the upstream repo would immediately affect new installs. The `curl | bash` delivery mechanism means users cannot inspect the script before execution.

**Fix:** Pin to a specific commit hash or signed tag in the installer. Consider distributing a checksum-verified tarball instead of a live git clone.

---

### F-14 — Low — Env File Validation Only Checks Control Characters

**File:** `hermes_cli/product_runtime_staging.py:310–316`

Values are checked for `\n`, `\r`, `\x00` only. No length limit. Values with embedded `=` or unusual characters pass through unchecked. Docker's env-file parser splits on the first `=`, so this is low-risk in practice, but there is no defence against a pathologically long value.

**Fix:** Add a maximum value length check (e.g., 8192 chars). Consider logging a warning for values containing `=` characters beyond the key-value split.

---

### F-15 — Low — Admin `GET /api/admin/users` Lacks CSRF Check

**File:** `hermes_cli/product_app_admin_routes.py:9–12`

All `POST` mutation endpoints correctly call `require_csrf`. The admin user-list `GET` does not. Since `GET` is read-only and the browser's same-origin policy prevents cross-origin JSON reads, this is not directly exploitable. Noted for consistency.

**Fix:** Either add a CSRF check for belt-and-suspenders, or document the deliberate omission.

---

## Remediation Priority

### Immediate (before any public/multi-user deployment)

1. **F-3** — Generate and require a dedicated `HERMES_PRODUCT_SESSION_SECRET`
2. **F-4** — Enforce Tailscale login match when claiming invites
3. **F-8** — Change workspace directory permissions from `0o777` to `0o755`
4. **F-2** — Remove internal URLs from user-visible 503 error messages

### Near-term

5. **F-1** — Replace `--network=host` gVisor with a restricted bridge network
6. **F-6** — Add symlink traversal check in workspace path resolution
7. **F-10** — Make `HERMES_PRODUCT_RUNTIME_MODE=product` trigger approval checking

### Hardening (lower urgency on Tailscale-only deployment)

8. **F-9** — Set `tirith_fail_open: false` in product runtime profile
9. **F-7** — Strip sensitive fields from `/healthz` response
10. **F-5 / F-11** — Persistent rate-limit state, evict stale buckets
11. **F-12** — Cache OIDC discovery metadata
12. **F-13** — Pin install script to a specific commit/tag
13. **F-14 / F-15** — Env file length limit, CSRF consistency
