# Optional private Telegram companion (v0.23.0)

See [Telegram setup and threat boundaries](../../docs/TELEGRAM.md). The bot is disabled by default. Configure its token and one allowed numeric owner ID in the existing root-only runtime environment, never in source or a public form. The in-process worker uses private-chat long polling and the same visitor leases/transaction lock; no webhook/public ingress rule is added. Pair from `/telegram` in the intended visitor browser. New source alone does not connect a bot or enable a paid model.

The optional private-model flag permits existing CASHFLOW_MODEL_* environment configuration only for that allowlisted bot. Public visitor Web requests always receive a disconnected adapter. Without the complete explicit private Telegram configuration, a model API key still fails public startup. Retain consistent snapshots of `/data/telegram-control.sqlite` as well as visitor directories before updates; these contain review/outbox metadata and must stay private. The legacy host backup timer is not a complete integration backup. Daily AI quota/spend caps are not included in this release.

# Public visitor demo (v0.22.0)

Set `FLUXORA_PUBLIC_DEMO=1` to remove the login challenge while keeping anonymous visitors in separate synthetic workspaces. HTTPS and cookies are required. When first opening a workspace, each browser profile receives a server-generated opaque `__Host-fluxora-visitor` cookie (Secure, HttpOnly, SameSite=Lax, Path=/, no Domain). Only its SHA-256 digest is stored in `/data/visitors/registry.sqlite`. The server selects a digest-named directory, containing its own `state.sqlite` and recovery copies; URL/form inputs cannot select another database. Upload and review tokens are scoped to that session's memory. Shared browser profiles share access; a stolen cookie grants that session's access. This is not account authentication.

The old `/data/state.sqlite` is retained for health/rollback and is not used as visitor seed data. Each new workspace gets a bundled synthetic example (Atlas by default; Studio or Retail through the displayed example links). Existing workspaces never change on refresh or example selection. Public mode refuses a nonempty model API key unless the complete explicit private Telegram model exception described above is enabled. Existing deterministic tools, reviewed forms and labelled mocks remain available. No paid live provider is enabled by visitor isolation.

A session lasts 24 hours from creation, including across app restart. Missing/expired session POSTs return 409 without mutation; a workspace GET creates a new session when capacity permits. Public product/docs/example pages, known assets, health and unknown paths do not create a workspace. Clearing cookies loses access. Expired inactive directories (including their local backups) are removed when later application traffic triggers cleanup; this is not scheduled secure erasure. Pending previews disappear on restart and expire after 15 minutes. Requests currently using an expired workspace hold a lease until completion.

Default hosted admission limits: 128 sessions (FLUXORA_VISITOR_CAPACITY accepts 1–256), 32 MiB per workspace, 256 MiB in the visitor directory, 16 pending previews per session and 128 across all sessions. Disk checks run before requests, so an operation can exceed the threshold; these are admission checks, not filesystem quotas. Capacity returns 503 and never evicts a valid visitor to admit another. Existing global request/worker limits remain. Use one hosted server process with one data volume; do not scale independent processes against the visitor registry. Host administrators are trusted. Do not enter real records.

The existing host backup timer copies the legacy database only; it does not promise durable visitor retention. User-created recovery copies stay within their visitor directory and expire with it. Before deployment, retain a root-only consistent snapshot of the visitor registry/directories if they already exist. Never put databases, cookies or snapshots in a submission/source archive. Reverting to the old shared public build would remove isolation: stop public access or use authenticated mode before such a rollback. Restoring `FLUXORA_PUBLIC_DEMO=0` uses the retained legacy owner database/verifier, not a visitor's data.

See [boundaries, tests and rollout](../../docs/TECHNICAL.md). The authenticated-mode instructions below apply only when public mode is disabled.

# Restricted VPS demo

This deployment is a single trusted owner's synthetic sandbox. It is **not** a multi-tenant product or a production identity system. Everyone with its shared owner credential has the owner's local-demo powers. Do not distribute that credential to untrusted reviewers or use real business records. Basic authentication has no application logout/session revocation; browsers may retain the credential until their session/profile is cleared. Replace the verifier and restart to revoke it.

The original local CLI continues to bind to loopback. `python -m cashflow_copilot.hosted` is a separate, explicitly configured entry point for a private Docker network behind the existing HTTPS Caddy proxy. It refuses startup without a DNS hostname and valid owner password verifier.

## Layout

- `/opt/fluxora/releases/<release>/`: immutable uploaded source and deployment files.
- `/opt/fluxora/private/runtime.env`: root-only owner verifier, domain and database path; optionally the three documented model environment variables. Never put a plaintext API key in source, an image, chat, command arguments or a report.
- `/opt/fluxora/private/owner-access.txt`: generated bootstrap credential for the owner; separate from distributable artifacts, mode 0600.
- `/opt/fluxora/data/`: writable UID/GID 10001 database volume.
- `/opt/fluxora/backups/`: root-only verified SQLite copies; host backup script retains its last fourteen matching files. This is local recovery, not encrypted off-site disaster recovery.
- `/opt/fluxora/ops/`: backup and rollback operations; the original shared proxy configuration has a root-only snapshot before modification.

Build with a recorded digest-pinned official Python image, using `deploy/vps/Dockerfile`. Compose exposes **no host port**. The named dedicated network `fluxora_front` contains the demo and shared Caddy only. Attaching Caddy uses gateway priority -1 to retain its existing default route. Do not recreate or restart unrelated projects. The compose image is supplied as an immutable release tag through `FLUXORA_IMAGE`.

## Boundary checks

In authenticated mode, every application page, download and asset requires owner authentication. Password verification uses a salted scrypt hash; credential and provider headers are not logged by the app. The private loopback-only `/healthz` exposes only status and tests read-only database availability.

The wrapper enforces the canonical Host, one Authorization header, exact HTTPS Origin for POST, existing CSRF validation, supported request framing, identity-only request bodies and the existing 2 MiB upload bound. It also applies fixed per-owner/global request limits, bounded authentication work, worker capacity, timeouts, a pending-preview cap and a hosted preview expiry. Health checks do not mutate the ledger. Caddy handles TLS and a second request-body limit. Do not add proxy retries for mutating requests.

The container has a read-only root filesystem, a non-root UID, dropped capabilities, no-new-privileges, memory/CPU/PID/log limits, writable data volume and temporary filesystem. It restarts unless explicitly stopped. SIGTERM requests a graceful shutdown and waits for current workers; previews held only in memory expire/disappear across restart. Persisted idempotency/history rules remain in the underlying engine.

Security headers include no-store on dynamic responses, frame denial, nosniff, same-origin referrer policy (no cross-origin referrers) and disabled camera/microphone/geolocation. The CSP retains inline styles/scripts for legacy form confirmation compatibility; this is a known limit, not a strict-XSS guarantee. Passwords are transported to the owner through the existing SSH connection and used only over trusted HTTPS. No authentication proxy credential is sent to a model.

## Deploy and rollback

1. Verify SSH and authoritative DNS, inventory running projects and record existing-domain HTTP/TLS canaries.
2. Upload a clean checksummed source release. Generate owner credentials on the host with exclusive file creation and root-only permissions. Never overwrite an existing runtime configuration during an update.
3. Build the image, create the dedicated network, start an authenticated private container and run local HTTP checks. A clean database starts with synthetic fixtures only.
4. Snapshot the shared Caddyfile and persist a rollback script. Append only `fluxora.sklab.cc`; verify the adapted config's non-Fluxora host blocks remain equal. Validate before a graceful Caddy reload; keep its original process and other containers running.
5. Verify trusted HTTPS, anonymous rejection, owner access, Origin/CSRF refusal, a synthetic DRAFT-to-observation/reconciliation workflow, reviewed owner operations and restart/replay. Restore the clean synthetic ledger after disposable canary work. Check every existing-domain canary and container identity again.

Rollback restores the saved proxy file and reloads Caddy, then stops only the Fluxora container. It retains its data, recovery copies and release for inspection. Do not delete shared proxy volumes, images, SSH files or other projects. Rolling the app back to an earlier version additionally requires a source-compatible database backup, not merely an older image tag.

## Limits and model configuration

The Python `http.server` family is not a general production server. This wrapper and HTTPS proxy reduce exposure for a bounded single-owner demonstration; they do not establish production suitability, audited authentication, resistance to all denial-of-service attacks, tenant isolation, malware scanning or encryption at rest. Host root / Docker administrators remain trusted. Provider uptime and semantic understanding are not guaranteed. Real banking, outgoing payments and messages remain absent.

No local model API key or existing local business database is uploaded. Initial hosted operation is model-free: deterministic reports, source review, owner forms and labelled mock paths work. To enable a live adapter later, the owner configures `CASHFLOW_MODEL_API_KEY`, `CASHFLOW_MODEL_BASE_URL` and `CASHFLOW_MODEL_NAME` in the private server environment, restarts only the demo, verifies the connection and explicitly runs qualification. Do not fabricate a current live-model result or automatically purchase/provider-test anything.

Actual deployment URLs, pinned image digests, health checks, commands, backup/rollback paths and remaining limitations belong to the accompanying deployment report. Syntax tests or HTTP responses do not replace rendered mobile/browser checks or independent human feedback.


## Demo access (0.23.3)

Use `/healthz` for monitoring. Public documentation, product pages, known static assets and `/demo/examples` bypass workspace admission and do not issue or renew a cookie. They render only bundled synthetic data and never use the legacy shared ledger. Unknown paths/assets return 404 without allocation. Workspace and financial/export/history routes still require visitor-scoped admission; do not treat arbitrary paths as public. The capacity/storage checks remain in force. At capacity a workspace GET gives a 503 page linking to read-only examples. No valid session is evicted to make room. Existing sessions, Telegram binding and financial records are retained on update; there is no traffic-based unique-human count.

`/demo/examples?example=atlas|studio|retail` computes three fixed synthetic examples from code. A new session may choose one using the displayed `/app?demo_example=...` link. Existing sessions open unchanged; switching preview examples is read only, not a reset. An explicit original demo reset still restores Atlas. No simulated customer acceptance, receipt or approval is pre-granted. Every example has its own matching synthetic source descriptions, separate USD/EUR forecasts and protected payroll. Example previews need no model/network calls.

For HTTP tests reuse one CookieJar per intended workspace. Use temporary local/network-disabled candidate environments for visitor mutation/restart regression. A check without a cookie on a workspace route still represents a new anonymous session; this change does not identify humans or prevent all automated traffic. The 24-hour expiry and 256 MiB total/32 MiB per-workspace admission checks are unchanged; configured session capacity is not a concurrency guarantee or a storage quota.
