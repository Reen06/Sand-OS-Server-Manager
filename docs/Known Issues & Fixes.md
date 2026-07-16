# Known Issues & Fixes

A running log of problems encountered and how they were resolved. Newest at top.

---

## 2026-07-16 (still later) — Logging back in from a PWA shortcut lands on the dashboard, not the app

**Symptom:** FreeCAD installed as a mobile home-screen PWA shortcut. Its
session expires; logging back in through the resulting login page lands on
the full dashboard instead of returning to FreeCAD.

**Root cause:** the next-after-login URL (Hub's `stream_http`, the
redirect-through-login flow for an unauthenticated top-level navigation)
was rebuilt as bare `f"https://{host}/{path}"` — dropping the
`stream/{app_id}/` portion of the path entirely. After logging back in,
that sends the browser to the host root, which itself redirects to the
dashboard — never back into the app.

This bug has been live since the redirect-through-login flow was first
built, but was silently masked for every `own_subdomain` app (Stirling
PDF, ParaView, EngineeringPaper, OpenFOAM GUI, Open WebUI): each one's
dedicated Caddy host unconditionally rewrites EVERY request onto
`/stream/{app_id}{uri}` regardless of what's actually requested, so the
missing prefix got silently re-added for free. Fully exposed for anything
WITHOUT a dedicated subdomain — FreeCAD, Filebrowser, WebCAD, Nextcloud,
Ollama — reached via the shared `/apps/stream/` subpath on the main
dashboard host, which has no such automatic rewrite.

**Fix:** has to branch on which case applies, or it just relocates the
bug: on an `own_subdomain` host, redirecting to `/stream/{app_id}/{path}`
would get that prefix prepended a SECOND time by Caddy's own rewrite (the
same double-prefix class of bug already hit with manifest links) — the
correct target there is bare `/{path}`, letting Caddy's rewrite add the
prefix itself. On the main dashboard host, `/stream/{app_id}/{path}` is
exactly right: it bypasses the `/apps/*` `handle_path` rule entirely and
lands on the same route directly via the Caddyfile's final catch-all
`handle` block. Branches on whether the request's Host is the bare
configured DuckDNS domain or a subdomain of it — the same lookup
`auth.py`'s `_cookie_domain` already uses.

**Verified:** FreeCAD (no subdomain) now redirects to `.../stream/freecad/`
(previously bare `/`), which correctly 401s (reachable) instead of
404ing/landing on the dashboard. ParaView (`own_subdomain`) still redirects
to bare `pv.<domain>/` (not double-prefixed), which also correctly 401s.

**Files:** `SandOS-Hub/backend/app/api/sm_proxy.py`.

---

## 2026-07-16 (final ParaView entry) — black 3D viewport (not a bug) + no NAS storage (a real one)

After the five stacked fixes above, ParaView loaded — but the 3D viewport
was solid black. Confirmed via a clarifying question rather than guessing:
toolbar/menus/panels all render correctly around it, which means the
session, WebSocket, and rendering pipeline are all genuinely working — a
black viewport with nothing loaded is ParaView's normal default state, not
a bug.

But the user's follow-up question ("is paraview hooked up to the NAS
storage for the user that opens it?") surfaced a real, separate gap: it
wasn't. ParaView had no `mounts` at all, and was `mode="shared"` (one
instance for every user). The launcher's own `launcher.json` hardcodes
`"dataDir": "/data"` as `pvw-visualizer.py`'s `--data` argument — with
nothing mounted there, the file browser panel had nowhere to open or save
anything, so there was never any data available to load in the first
place.

**Fix:** switched to `mode="per-user"` (a shared instance would have
dumped every user's files into the same `/data` with zero isolation —
matches FreeCAD's own per-user design for the same reason) and added the
same NAS home mount already shared across FreeCAD/Filebrowser/Nextcloud
(`Mount(name="home", path="/data", scope="per-user", storage="nfs")`).

**Verified:** launching now creates a real per-user instance
(`sm-paraview-{user}`, not the old shared `sm-paraview`), `/data` inside
the container shows the user's actual NAS home (same files already visible
in Filebrowser/FreeCAD), and the launcher still returns a clean 200 session
with the mount in place.

**Files:** `Sand-OS-Server-Manager/server/app/registry.py`.

---

## 2026-07-16 (yet later same day) — ParaView: quick flash then a completely white screen (five stacked causes)

The last app still broken after fixing the `/apps` prefix gap. Every other
app loaded correctly; ParaView rendered once and then went blank. Took
five rounds to actually fix — each fix was real and necessary, but not
sufficient on its own, so recording all five here.

**Bug 1 — absolute WebSocket URL, same class of bug as Stirling PDF/
EngineeringPaper/OpenFOAM GUI.** ParaViewWeb's own compiled `Visualizer.js`
builds its wslink WebSocket URL as `(location.protocol==='https:'?'wss':
'ws') + '://' + host + ':' + port + '/ws'` — confirmed by extracting the
bundle and grepping for the literal construction. The bare `/ws` has no
app-scope prefix, so it can never match either the Hub's or SM's routing.
**Fix:** the same treatment as the other three — `own_subdomain=True`, a
`pv.{domain}` Caddy block, `streamUrl()`'s `_SUBDOMAIN_APPS` map. This DID
fix routing (confirmed: SM's own `proxy.log` showed the POST correctly
reaching `/paraview/` afterward) — but the page still went blank.

**Bug 2 — double-prefixed manifest/icon links, affecting all four
subdomain apps, not just ParaView.** `pwa.py`'s `_scope()` (added for the
base-href fix) still returned `/stream/{app}/` for an `own_subdomain` app —
but that app's Caddy block already unconditionally prepends `/stream/
{app}` to EVERY request via `rewrite * /stream/{app}{uri}`, including a
request for the manifest itself. A browser request for `/stream/paraview/
sm-icon.svg` got rewritten AGAIN to `/stream/paraview/stream/paraview/
sm-icon.svg` — visible directly in the Hub's access log as a 404. **Fix:**
`own_subdomain` apps now get a bare `/` scope, so the browser requests
`/sm-icon.svg` and Caddy's rewrite lands it at the real route in one hop.
Cosmetic for Stirling PDF/EngineeringPaper/OpenFOAM GUI (their core
functionality never depended on the manifest resolving), but was part of
what made ParaView's page look broken.

**Bug 3 — the actual blocker: Apache's mod_proxy circuit breaker.** Still
blank after both fixes. A precise timing test (fresh container, real POST
with a proper JSON body every 0.5s) showed `/paraview/` 503ing
*consistently for 40+ straight seconds* — not a brief startup race. The
container's own `001-pvw_error.log` explained it: `AH00940: HTTP: disabled
connection for (localhost)`. Apache's mod_proxy has a built-in circuit
breaker — the first time a `ProxyPass` backend connection fails, that
worker is marked disabled for a retry-cooldown window (default 60s):
*every* request in that window gets an immediate 503 with no further
connection attempt at all, regardless of the backend coming back up
seconds later. The backend here (the wslink launcher, port 9000) is a
same-container sibling process — Apache starts before the launcher's own
socket binds, so the very first connection attempt (a real user's page
load, or even the dashboard's own readiness probe hitting the same
`ProxyPass` path) can genuinely fail once, locking out every subsequent
request for a full minute. Also added `AppDef.ready_path` (`models.py`) /
`web_ready()`'s `path` param (`docker_backend.py`) along the way — root
alone (fronted by Apache, always instant) was never a meaningful readiness
signal for an app whose real dependency is a separate backend process;
kept this fix since it's correct in principle even though it wasn't what
finally closed the bug.

**Fix:** `containers/paraview/Dockerfile`, a thin layer on the official
Kitware image patching the launcher's `ProxyPass` to add `retry=0` — Apache
always retries on the very next request instead of enforcing the cooldown,
correct here since there's no real network flakiness between two processes
in the same container for a retry-delay to protect against.

**Verified:** clean launch + exactly one real POST with a proper JSON body
(matching the app's own page-load JS) now returns 200 with a real wslink
session response (`sessionId`/`secret`/`sessionURL`) — previously 503 for
40+ seconds under identical conditions on the unpatched image.

**Gotcha hit while testing:** rapid repeated POSTs to `/paraview/` each
spawn a REAL `pvw-visualizer.py` session process — hammering it like a
normal HTTP health check (which an earlier diagnostic pass in this exact
investigation did) overwhelms the single-threaded Python2 launcher and
produces connection resets that look like a regression but aren't. Test
this endpoint with one clean request at a time.

**Bug 4 — "Server disconnected" right after the loading spinner, once all
three above were fixed.** The launcher's session JSON *itself* is broken
for anything but same-machine testing: `sessionURL` is a literal, hardcoded
`ws://localhost/proxy?sessionId=...&path=ws` baked into the image's
`launcher.json`. The app's own JS passes this straight into `new
WebSocket(url)` with zero rewriting of its own — and unlike `fetch()`/XHR,
the native `WebSocket` constructor requires a fully-qualified URL; it does
not resolve a relative path against the page's origin. So the browser
dutifully tried to open a WebSocket to *its own* localhost, not the actual
server. **Fix:** `_rewrite_paraview_session()` (`proxy.py`) replaces the
literal `ws://localhost` prefix with `wss://{the real Host header}` —
same established pattern as `_inject_extra_turn()`'s TURN-URL rewriting
used for a different app. Had to gate this on the exact launcher path
rather than content-type/`"json" in ct` (the usual pattern elsewhere in
this file): confirmed live that the launcher mislabels its own JSON
response as `text/html` (another old-Twisted-library quirk). Verified
end-to-end through the real `proxy.http()` path with a Host header
matching the actual `pv.<domain>` subdomain: `sessionURL` now correctly
reads `wss://pv.vpn1603.duckdns.org/proxy?...` instead of the broken
`ws://localhost/...`.

**Bug 5 — back to a plain blank white page (no spinner at all this time)
after fixing Bug 4.** The `ready_path`/Bug 3 fix checked the right
endpoint, but `web_ready()`'s lenient mode treats ANY HTTP response —
including a 503 — as "the server answered, therefore ready". ParaView's
launcher genuinely 400s a plain GET once truly listening (wrong method,
never a 2xx, so `strict_ready` can't be used either), but ALSO 503s
specifically when Apache's `mod_proxy` can't reach the backend at all —
and the lenient check couldn't tell those two apart. So the dashboard kept
reporting "ready" and loading the iframe during exactly the window
`ready_path` was built to detect, sending the one-shot (no-retry) launcher
POST straight into the same failure Bug 3's `retry=0` made recoverable but
not instant. **Fix:** added `AppDef.ready_bad_status` (`models.py`) —
specific statuses that mean genuinely-not-ready even in lenient mode —
threaded through `web_ready()`'s new `bad_status` param
(`docker_backend.py`). ParaView: `ready_bad_status=(503,)`. Verified live:
`status()` now correctly reports `"starting"` for ~2s after a fresh launch
(previously flipped to `"idle"` within 0.2s), and the real POST immediately
after `"idle"` returns 200 with a genuine session.

**Files:** `Sand-OS-Server-Manager/server/app/{registry,models,
docker_backend,config,proxy}.py`, `containers/paraview/Dockerfile`;
`SandOS-Hub/scripts/sandhub-caddy-tls`;
`SandOS-Hub/frontend/js/pages/apps.js`.

---

## 2026-07-16 (later same day) — FreeCAD/Filebrowser/WebCAD `{"detail":"Not Found"}`, reproducible, not transient

Reported right after the previous entry's app-fix marathon, on apps that
had already been verified working earlier that same day — first assumed to
be a transient hit during an SM service restart. It wasn't: closing and
reopening Filebrowser reproduced it again, and so did FreeCAD/WebCAD.

**Root cause:** the DuckDNS domain's Caddy block
(`sandhub-caddy-tls`'s `CADDY_BLOCK`, imported into
`/etc/sandhub/caddy-conf.d/duckdns.caddy`) never had the `/apps` prefix-
stripping that the IP-based vhosts (`config/Caddyfile`'s `handle_path
/apps/*`) have had all along. The Hub's own backend router is mounted at
`/stream/{app}/...` with no `/apps` prefix at all — any request for
literally `/apps/stream/{app}/...` arriving via the domain (confirmed live,
reproduced from a WireGuard peer IP) never matched any route and 404'd.
Direct evidence: SM's own `proxy.log` had **zero** entries for the failing
requests — they never even reached SM, because the Hub itself 404'd them
before attempting to forward anything.

This is why it looked like a NEW regression tied to that day's other
changes (base-href rewrite, `own_subdomain`, the `sandos-embed` query
param) but wasn't: it's a gap that's been there since the domain block was
first written, and it only ever affects apps *without* their own dedicated
subdomain. Open WebUI/Stirling PDF/EngineeringPaper/OpenFOAM GUI were never
affected — they bypass `/apps/stream/` entirely via their own
`ai./pdf./calc./cfd.` subdomains. FreeCAD, Filebrowser, WebCAD, Nextcloud,
Ollama, and any future app without a dedicated subdomain hit this whenever
reached via the domain (mobile, WireGuard, remote) rather than a raw LAN
IP — accessing by IP always worked, which is exactly why this had gone
unnoticed.

**Fix:** added the identical `handle_path /apps/*` block to the domain's
`CADDY_BLOCK` template, deployed and reloaded. Verified the exact
previously-404ing URL (`.../apps/stream/webcad/?sandos-embed=1`) now
returns 401 (our own auth gate) instead of 404; confirmed for FreeCAD and
Filebrowser too.

**Prevention:** any time a NEW Caddy site block gets added for this domain
(a future subdomain, a future rewrite), diff it against the existing
IP-vhost block in `config/Caddyfile` for path-handling parity — the two
are maintained in separate files (`config/Caddyfile` vs. the
`sandhub-caddy-tls`-generated one) with no automatic check that they stay
in sync.

**Files:** `SandOS-Hub/scripts/sandhub-caddy-tls`.

---

## 2026-07-16 — FreeCAD/OpenMapper/etc. wouldn't launch, then three apps 404'd or crashed after "fixing" it

A single long session that started as "USB-hosted apps hang on launch" and
ended up touching five distinct, unrelated bugs across three repos. Grouped
here in the order they were found; each is independent, and none of the
fixes below depend on the others.

### Issue 1 — USB-hosted apps (FreeCAD, OpenMapper, HeliX, RayOptics, Renode,
OpenFOAM GUI, ParaView) hung indefinitely on first launch

**Symptom:** Launching any app on the USB app-hosting drive's secondary
Docker daemon would hang past any timeout on the NFS `mkdir` that creates
its per-user NAS home directory — sometimes instantly, sometimes forever,
with no correlation to invocation method (bash vs Python) or how long a
timeout was given.

**Two real-but-insufficient fixes tried first:**
1. `ufw.service` flushing iptables before `docker.service`/the USB dockerd
   started, wiping their per-network rules (same class of bug as the
   2026-07-15 entry above, but for the USB daemon specifically) — fixed via
   an `After=ufw.service docker.service` drop-in on
   `sandos-usb-dockerd@.service`.
2. `erichough/nfs-server`'s entrypoint never starts `nfsdcld` (the NFSv4
   client-recovery-tracking daemon), so the kernel falls back to a degraded
   tracking mode (`NFSD: Unable to initialize client recovery tracking!
   (-110)`) — reads still worked, but writes intermittently stalled. Fixed
   with a custom `sandos-nfs-server` image (`containers/nfs-server/
   Dockerfile`) that sed-patches the base entrypoint to start `nfsdcld`
   before `nfsd`.

Both were real, both shipped — but FreeCAD **still** hung after both,
proving neither was the actual root cause.

**Actual root cause:** the USB app-hosting dockerd was never given its own
bridge network. It defaulted to the same name (`docker0`) and subnet
(`172.17.0.0/16`) as the main `docker.service` — dockerd's bridge driver
silently *adopts* an existing `docker0` device rather than refusing to
start, so both daemons' containers shared one L2 segment with
independently-allocated, colliding IP addresses. Confirmed live: a fresh
container on the USB daemon got handed `172.17.0.2` — the exact address the
NFS server container already held on the main daemon's `docker0`. A
duplicate-IP conflict on shared L2 explains the "sometimes instant,
sometimes hangs forever" pattern exactly — it came down to which ARP entry
won the race at that moment.

**Fix:** gave the USB dockerd its own dedicated bridge (`docker-usb0` at
`172.30.0.1/24`, pre-created via `ExecStartPre` in
`sandos-usb-dockerd@.service` so it exists fresh on every boot) and pointed
the daemon at it with `--bridge` (not `--bip`, which is mutually exclusive
with a custom bridge name on this Docker version — a custom-named bridge
must already exist with its IP assigned before dockerd starts).

**Verified:** 5/5 repeated NFS `mkdir` calls succeeded instantly
(previously intermittent/hanging); FreeCAD and OpenMapper launched and
stopped cleanly through the real SM code path in under 3s each.

**Files:** `Sand-OS-Server-Manager/containers/nfs-server/
sandos-usb-dockerd@.service`, `.../Dockerfile` (nfs-server image).

---

### Issue 2 — Stirling PDF: "instance stopped before it was ready", then
`{"detail":"Not Found"}`, then a blank white screen — three separate bugs
stacked on one app

**Bug 2a — JVM Metaspace OOM.** The container was crashing ~20s into every
boot: `java.lang.OutOfMemoryError: Metaspace`. Root cause: the image's own
entrypoint (`init-without-ocr.sh`) scales `-XX:MaxMetaspaceSize` by detected
container memory — 128m at ≤1024MB, 192m at ≤2048MB — and its own comments
already flag the 1024MB tier as tight. This build (2.14.2 — VeraPDF,
FontForge, PdfJson, cluster backplane) loads enough classes to blow the
128m cap in practice. **Fix:** bumped `mem_limit` from `1g`→`2g`
(`registry.py`), landing in the tier upstream actually designed for.
Verified: boots to `Health.Status=healthy` at ~35s and stays up.

**Bug 2b — its own login fighting our proxy SSO.** After the OOM fix, the
container ran, but opening it showed `{"detail":"Not Found"}`. Stirling
ships its own built-in login (`security.enableLogin: true` by default),
totally unaware of the Hub/SM's own SSO. An unauthenticated request
correctly got Spring Security's 401 "Full authentication is required," but
its React frontend then redirected to Stirling's OWN `/login` using an
absolute path — which escapes our `/apps/stream/stirlingpdf/` proxy prefix
entirely and lands on a route neither SM nor the Hub have. The literal
`{"detail":"Not Found"}` is FastAPI's own default 404, not anything
Stirling returned. **Fix:** `env={"SECURITY_ENABLELOGIN": "false"}`
(`registry.py`) — our proxy already gates access, so Stirling's own login
is redundant. Verified: direct container root now returns 200 with the
real app HTML; through the proxy, unauthenticated access still correctly
hits *our* login gate, not Stirling's.

**Bug 2c — blank white screen even after both fixes.** Stirling's compiled
JS bundle calls `fetch()` against hardcoded absolute paths like
`"/api/v1/auth/refresh"`. A leading-slash URL always resolves against the
browser's origin root in every browser — no `<base href>` rewrite can fix
an absolute-path `fetch()` call, since that mechanism only ever governs
relative-URL resolution. Every API call the app made was 404ing under any
subpath, full stop — same underlying class of bug Open WebUI had
(2026-07-15 entry), just manifesting as JS `fetch()` calls instead of HTML
asset tags. **Fix:** gave it the exact same treatment as Open WebUI — a
dedicated subdomain, `pdf.vpn1603.duckdns.org`, added to
`sandhub-caddy-tls`'s `CADDY_BLOCK` template (`rewrite * /stream/
stirlingpdf{uri}`) and `apps.js`'s `streamUrl()` `_SUBDOMAIN_APPS` map.
DuckDNS resolves every subdomain to the same IP and answers the shared
DNS-01 challenge for free, so this cost nothing extra in DNS/cert setup.

**A fourth, subtler bug this surfaced:** once served at its own subdomain,
the app went blank *again* — the proxy's `<base href>` rewrite (added for
the bug-2c fix, before the subdomain existed) was now actively wrong: it
force-rewrote `<base href="/">` to `/apps/stream/stirlingpdf/`, which
doesn't exist as a route on `pdf.<domain>` at all (Caddy's subdomain
rewrite lands requests at `/stream/stirlingpdf/...` directly, no `/apps`
prefix). Added `AppDef.own_subdomain` (`models.py`) and skip
`_rewrite_base_href` entirely when set — correct for subdomain access,
moot for the `/apps/stream/` fallback (already broken regardless, since the
absolute-path `fetch()` calls 404 either way). This same flag also fixed
the injected PWA manifest/icon `<link>` tags, which had the identical
"points at the wrong prefix" bug (`pwa.py`'s new `_scope()` helper builds a
root-relative scope — no `/apps` prefix — whenever `own_subdomain` is set).

**Files:** `Sand-OS-Server-Manager/server/app/{registry,models,proxy,
pwa}.py`; `SandOS-Hub/scripts/sandhub-caddy-tls`;
`SandOS-Hub/frontend/js/pages/apps.js`.

---

### Issue 3 — EngineeringPaper and OpenFOAM GUI also 404'd with
`{"detail":"Not Found"}` — same subpath-incompatibility bug, different apps

Confirmed by fetching each app's real HTML directly through `proxy.http()`
and finding the same class of absolute-path reference: EngineeringPaper's
Vite build emits `src="/assets/index-*.js"` (no leading `./`); OpenFOAM
GUI goes further — absolute CSS/JS (`/static/landing/...`), an absolute
`fetch('/api/lan-info')`, and even a service worker registered at absolute
scope `/`. **Fix:** the same subdomain treatment — `calc.vpn1603.duckdns.org`
and `cfd.vpn1603.duckdns.org`, `own_subdomain=True` on both AppDefs.

**A DuckDNS quirk hit here, worth knowing for the next subdomain add:**
requesting **two brand-new** subdomain certs in the same Caddy reload made
them race for DuckDNS's single shared DNS-01 TXT-record slot — both hit a
transient `Certificate not found` / `timed out waiting for record to
propagate` on their first attempt. Caddy's own retry (60s backoff)
recovered both within two attempts, no intervention needed — if this
happens again, wait for the automatic retry rather than assuming it's
broken.

**Building EngineeringPaper's updated image also surfaced Issue 4** (below)
— the build failed on a DNS lookup with no apparent connection to the app
itself.

**Files:** same as Issue 2, plus `registry.py`'s `engineeringpaper`/
`openfoamgui` AppDefs.

---

### Issue 4 — docker0 (the DEFAULT bridge) had zero outbound internet
access for any container on it

**Symptom:** `docker build` failed on `apk add`'s DNS lookup
(`dl-cdn.alpinelinux.org: DNS: transient error`) — but the HOST's own DNS
worked fine, and a container run with `--network host` also worked fine.

**Root cause:** `docker0`'s entire NAT/FORWARD ruleset was missing —
confirmed via `iptables -t nat -L POSTROUTING`: every OTHER bridge on the
host (the USB daemon's `docker-usb0`, and every custom Compose network) had
its masquerade rule; `docker0` had none. Same gap in the FORWARD chain
(`DOCKER-FORWARD`, `DOCKER-CT`) — with FORWARD's default-DROP policy and no
per-bridge ACCEPT for `docker0`, every container-initiated *outbound*
request was silently dropped. Host-to-container access via a published
port (`curl 127.0.0.1:8100`) kept working the whole time because that's
the OUTPUT chain, not FORWARD — which is exactly why nothing looked broken
until something inside a container tried to reach the internet on its own
(a `docker build`'s package manager, in this case). Root cause was never
conclusively pinned (docker.service is already `After=ufw.service`, so this
wasn't the same race that bit the USB dockerd in Issue 1) — plausibly a
side effect of the day's many docker network/daemon restarts.

**Fix:** restored the three missing rules by hand (mirroring the exact
working pattern already present for `docker-usb0`), then made it durable —
see the Persistence section below.

---

## Persistence audit for this session (2026-07-16)

Every fix above was checked against "does this survive a reboot / service
restart," not just "does it work right now." Two real gaps found and
closed:

1. **`sandos-server-manager.service` (the real, persistent SM process) had
   been dead since that morning.** Every fix in this session was tested
   against a manual `nohup ./run.sh &` in the working session instead — a
   process that vanishes on the next reboot with nothing to bring it back.
   Worse, its systemd env file (`/etc/sandos-server-manager.env`) was
   missing `SM_HUB_INTERNAL_URL`, `SM_LLM_API_KEY`, and
   `SM_OLLAMA_NAS_TRANSFER` (only had `SM_HUB_URL`, pointed at the raw LAN
   IP rather than `run.sh`'s own default). Restarting via systemd as-was
   would have silently dropped Hub SSO's fast LAN path, the Hub LLM Router
   seeding, and NAS-based Ollama model transfer — SM would *look* fine
   (starts, serves the dashboard) while quietly missing three
   integrations. **Fixed:** env file corrected to mirror `run.sh`'s actual
   defaults, and the running process switched over to the real
   systemd-managed service. Verified live: correct env vars present in the
   process, Hub SSO login-required response points at the right domain.

2. **The docker0 NAT/FORWARD rules (Issue 4) were pure live iptables state**
   with nothing to restore them on a reboot or the next `docker.service`
   restart. Added `ensure-docker0-forward-rules.sh` (idempotent — `iptables
   -C` before `-A`, safe to run any number of times) wired into a oneshot
   systemd unit (`sandos-docker0-forward-fix.service`,
   `After=`/`Requires=docker.service` since it checks docker-managed
   chains that only exist once the daemon is up), enabled at
   `multi-user.target`. `systemd/install.sh` now installs and enables it as
   part of the normal setup flow. Verified: enabled, active, and idempotent
   on a manual re-run.

**Already durable, confirmed, no action needed:**
- USB dockerd bridge fix (Issue 1) — a real systemd unit change
  (`ExecStartPre` + `--bridge`), already committed and deployed.
- All subdomain Caddy blocks (`ai.`/`pdf.`/`calc.`/`cfd.`) — written to
  `/etc/sandhub/caddy-conf.d/duckdns.caddy`, a real file `import`-ed by
  Caddy's config on every start, not regenerated from anything ephemeral.
- Hub frontend/backend (`apps.js`, `sm_proxy.py`, etc.) — deployed to
  `/opt/sandhub/`, a real file location; `sandhub-dashboard`/`caddy` both
  confirmed `enabled`.
- `registry.py`/`models.py`/`proxy.py`/`pwa.py` config changes (mem_limit,
  `own_subdomain`, `SECURITY_ENABLELOGIN`) — plain committed code, read
  fresh by SM on every start.
- OpenFOAM GUI's own source fixes (Issue 3 + the redundant-buttons cleanup
  below) — committed to `Reen06/OpenFOAM_GUI` AND live-bind-mounted (not a
  baked image), so even a full container recreate re-reads the same
  already-committed host files. The most robust of all of today's fixes.
- EngineeringPaper's rebuilt image (`sm-engineeringpaper:latest`) persists
  in the local Docker image store regardless of the source checkout's
  state.

**One risk flagged, not touched:** the Hub device's own on-disk checkout
(`/home/pi/SandOS-Hub`) is stale and has uncommitted local edits unrelated
to today's work (predates this session). It's harmless as long as updates
keep going through the documented `scp`+`sudo cp` → `/opt/sandhub/` path
(which is what this session used throughout) — but do NOT run the
`git pull && cp` update workflow from Hub's own CLAUDE.md against that
checkout without first reconciling it, or it'll either conflict on the
`pull` or silently overwrite `/opt/sandhub/` with something other than
what's actually been verified working.

**Not persisted, and shouldn't be:** the third-party EngineeringPaper.xyz
fork checkout (`/home/control/EngineeringPaper.xyz`) has the source patch
as a **local-only** git commit (not pushed to `mgreminger`'s upstream) —
protects against an accidental `git pull`/`reset` wiping it before a
future rebuild, without touching someone else's repository.

---

### Also this session: redundant per-app UI removed (not a bug fix, a
cleanup)

- **EngineeringPaper:** its own header showed a single "open this sheet in
  a new tab" button whenever embedded in ANY iframe (`window.self !==
  window.top`) — including the dashboard's own trusted, session-gated
  viewer, which already has a real "Open in window" button. Patched the
  app (locally committed, see above) to recognize a `sandos-embed=1` query
  param the dashboard's iframe `src` now sets, showing the normal full menu
  in that case instead. Every other iframe-gated behavior (autosave,
  keyboard sizing, code-cell restriction) untouched.
- **OpenFOAM GUI:** the landing page AND both case module pages
  (wind_tunnel, propeller) each had their own Install App button, LAN
  toggle (a single detected local IP), and notification toggle — the
  dashboard already provides proper per-app PWA install, whole-mesh
  WireGuard remote access, and (per the user) app notifications uniformly.
  Removed the buttons/panels, their CSS, the four JS setup methods
  (`setupPWA`, `setupLANToggle`+`fetchLANInfo`, `setupNotifications`,
  `sendNotification`) per page, their real call sites in the WebSocket
  run-complete/run-error handlers, and the now-fully-orphaned service
  worker files. Committed to `Reen06/OpenFOAM_GUI`.

---

## 2026-07-15 — Containers on custom Docker networks lose internet after reboot (ufw flush)

**Symptom:** Ollama model pulls fail with `dial tcp …: i/o timeout` (DNS works, TCP dies). Any container on a custom bridge network (`sm-llm-net`, Nextcloud's net) has no egress; containers on the default `docker0` are fine. Misdiagnosed twice: first as the Ollama internet kill-switch (it was off AND broken — `_ollama_container_ip` reads `.NetworkSettings.IPAddress`, empty on named networks), then as the kill-switch rule not clearing.

**Root cause:** `ufw.service` restores its ruleset at boot AFTER dockerd has written its iptables chains, wiping Docker's per-network FORWARD/CT/BRIDGE rules. NAT masquerade rules survive (different table), which is why DNS (via local resolver) worked while TCP didn't. Containers (re)started after the flush get their port-publish rules back, but the per-NETWORK egress rules only get written at dockerd startup / network creation.

**Fix applied:** Restored the missing rules for all three custom bridges by hand (DOCKER-BRIDGE dispatch, DOCKER-CT established, DOCKER-FORWARD ingress-ACCEPT, DOCKER default-deny), then made it permanent with a systemd drop-in `/etc/systemd/system/docker.service.d/after-ufw.conf` ordering Docker `After=ufw.service` so its rules land last.

**Prevention:** After any reboot of CortexPC, if a container can't reach the internet but LAN works, check `iptables -S DOCKER-FORWARD` for the `-i br-…  -j ACCEPT` lines before blaming the app.

---

## 2026-07-15 — Open WebUI via Hub: garbled binary page, then stuck black splash, then anonymous user

Three stacked issues, all hit while wiring Open WebUI through the Hub's `/apps/stream/` proxy.

**Issue 1 — page renders binary garbage.**
- **Root cause:** Open WebUI answers `Accept-Encoding: br` with brotli. The SM proxy's httpx lacked `brotlicffi`, so raw brotli bytes passed through while `content-encoding` was stripped from the response — the browser rendered compressed bytes as text. Worse, the browser **disk-cached** that broken response (`200 OK (from disk cache)`) and kept serving it after every server-side fix; DevTools "Disable cache" masked it while open but doesn't purge the entry.
- **Fix:** SM `proxy.py` forces `accept-encoding: gzip, deflate` upstream; `brotlicffi` installed in the SM venv as belt-and-suspenders; Hub `sm_proxy.py` does the same override and strips `content-encoding`; **every** SM proxy response now sends `Cache-Control: no-store` (previously only streamed apps did) so a broken response can never be cached again. User must clear "Cached images and files" (All time) once.

**Issue 2 — black splash screen, broken image, app never loads.**
- **Root cause:** Open WebUI hard-codes absolute paths (`/_app/*.js`, `/static/splash.png`) and has no subpath support (upstream PR #12002 closed unmerged) — under `/apps/stream/open-webui/` every asset 404s at the Hub root.
- **Fix:** dedicated subdomain `ai.vpn1603.duckdns.org`. The `sandhub-caddy-tls` helper template now also emits an `ai.<domain>` site block that rewrites everything to `/stream/open-webui{uri}` on the Hub proxy (DuckDNS resolves any subdomain to the same IP and answers DNS-01 for it, so the cert issues automatically). Hub session cookie is widened to `domain=<duckdns-domain>` on login (`auth.py _cookie_domain`) so the subdomain shares the session — users must re-login once. `streamUrl()` in Hub `apps.js` special-cases open-webui.

**Issue 3 — signed-in Open WebUI user is a random hex ID.**
- **Root cause:** SM ran in dev mode (no `SM_HUB_URL`), so identity was the anonymous per-browser `sm_user` cookie, which was injected into the `X-Forwarded-User` SSO header.
- **Fix:** enabled Hub SSO in the SM's `run.sh` (`SM_HUB_URL=https://vpn1603.duckdns.org`, `SM_HUB_INTERNAL_URL=https://10.0.0.177`). SM now validates the `hub_session` cookie against the Hub; real usernames reach the app. Existing Open WebUI admin account renamed to `admin` in `webui.db` to match. New users land "pending" until approved in Open WebUI's admin panel; scoped Hub users need the `app.open-webui` grant.

**Also:** `open-webui-data` moved to the fleet NAS (`shared/open-webui-data`, `storage="nfs"` in the SM registry). The bulk copy **deadlocked loopback NFS** (same-kernel client+server, `sync` export, 1.1 GB write) — mounts hung so hard Docker couldn't kill containers. Recovered via privileged `nsenter umount -f -l` + NFS server restart; re-copied by bind-mounting the NAS host dir directly (bypassing NFS) and skipping the ~1 GB re-downloadable embedding cache. **Prevention:** never bulk-copy through a loopback NFS mount — write to the export's host path directly.

---

## 2026-06-26 — SSH terminal on Hub dashboard disconnects immediately (no bash prompt)

**Symptom:** Opening the SSH terminal from the Hub dashboard showed the MOTD/banner but the bash prompt never appeared. Typing any key caused the connection to close instantly.

**Root cause:** The browser sends a `{"type":"resize"}` JSON message over the WebSocket immediately on `ws.onopen`. The backend's inner `try/except (json.JSONDecodeError, ValueError)` did not catch exceptions raised by `asyncssh`'s `process.change_terminal_size()`. The unhandled exception propagated to the outer `except (WebSocketDisconnect, Exception): pass`, which silently returned from `ws_to_ssh()`. `asyncio.wait(FIRST_COMPLETED)` then cancelled the SSH read task and closed everything — before bash ever sent its prompt.

**Fix applied:** Wrapped `process.change_terminal_size()` in its own `try/except Exception` so a failed resize is non-fatal and the loop continues. File: `SandOS Hub/backend/app/api/ssh.py`.

**Prevention:** Any call inside a WebSocket receive loop that can raise non-parse exceptions must have its own guard — don't rely on the outer loop's catch-all as the only safety net.

---

## 2026-06-27 — Hub passthrough proxy shows white screen when node is on home LAN

**Symptom:** Accessing the node dashboard through the Hub's "Node" page showed a white screen / blank iframe, even though the node was reachable directly at `http://10.0.0.90`.

**Root cause:** The hub DB column `nodes.uplink_pref` was set to `"10.79.114.5"` (the node's LTE WireGuard peer IP). `_best_target()` in `passthrough.py` checks: `if pref in peer_ips: return pref, 443`. Since `10.79.114.5` was in the WireGuard peer list, ALL proxy traffic was routed to the LTE peer IP — bypassing the LAN IP (`10.0.0.90`) entirely. At home, the LTE WireGuard peer is unreachable, so the proxy timed out at 20 s and returned a blank/504 response.

**Fix applied:** Updated the hub DB directly:
```bash
sudo sqlite3 /var/lib/sandhub/hub.db 'UPDATE nodes SET uplink_pref="auto" WHERE id="roku-e8c3"'
```
With `uplink_pref="auto"`, `_best_target()` falls back to the LAN IP when WireGuard peers are unreachable. Proxy round-trip went from timeout (20 s) to 184 ms.

**Prevention:** The `uplink_pref` setting should be visible and editable in the hub dashboard UI so it doesn't silently get stuck on a stale peer IP. When the node has both a LAN IP and WireGuard peers, `"auto"` should always be the default.

---

## 2026-06-27 — WiFi scan returns "no networks found" + speed test slow (3 MB/s) after bring-home reboot

**Symptom:** WiFi scan returned "no networks found." Speed test on the home network showed only 3 MB/s (~24 Mbps) instead of expected 80–100 Mbps.

**Root cause:** The Linux kernel renumbered the WiFi interfaces after a reboot. Previously `wlan0`=Broadcom (AP, 2.4 GHz only) and `wlan1`=TP-Link (upstream, 2.4+5 GHz); after reboot these swapped to `wlan0`=TP-Link and `wlan1`=Broadcom. The `sand-apply` script had written `unmanaged-devices=interface-name:wlan0` to the NM config (correct at apply time, wrong after renumber). After the renumber, `wlan0` is the TP-Link, so NM left TP-Link strictly unmanaged/DOWN and the Broadcom (wlan1) became the active upstream on 2.4 GHz only. Scanning the DOWN TP-Link returned no results; speed was limited to Broadcom's 2.4 GHz throughput.

**Fix applied:**
1. Changed `/etc/NetworkManager/conf.d/00-sand-unmanaged.conf` from `interface-name:wlan0` to `mac:88:a2:9e:d5:1e:77` (Broadcom's MAC) — stable across any renumbering.
2. Reloaded NM (`systemctl reload NetworkManager`) → NM unmanaged the Broadcom and made the TP-Link manageable.
3. TP-Link (wlan0) auto-connected to home SSID on 5 GHz (channel 157, 5785 MHz) → speed test went from 24 Mbps to 80 Mbps.
4. Removed the `connection.interface-name` pin from the SDMF76 NM connection profile (`nmcli connection modify SDMF76 connection.interface-name ''`) so NM connects by SSID on any wlanN.
5. Updated `scripts/sand-apply` in the codebase to write `mac:${AP_MAC}` (read from `/sys/class/net/`) instead of `interface-name:${AP_IFACE}`, so future `sand-apply` runs are stable.

**Prevention:** Never pin `unmanaged-devices` or NM connection profiles to `wlanN` names — kernel interface numbers are unstable when USB adapters re-enumerate. `sand-apply` now records the AP interface's MAC address, which is stable across all reboots.

---

## 2026-06-27 — Node dashboard always shows "session expired" after login (direct and proxy)

**Symptom:** Logging into the node's dashboard — either directly or through the hub passthrough proxy — immediately showed "session expired" after entering the correct password. The login call itself returned 200, but the first API call after that (GET `/api/themes/settings`) returned 401.

**Root cause:** `db.create_session()` takes an optional `user_id` parameter that defaults to `""`. The login endpoint called it with only 4 positional args (token, expiry, user-agent, ip), so `user_id` was always stored as `""`. But `db.get_session()` uses a `JOIN users ON user_id` to hydrate the session row — with `user_id=""` the JOIN never matched any user and `get_session()` returned `None` for every token. `require_auth` raised 401 on every post-login request.

**Fix applied:** In `backend/app/api/auth.py`: after password verification, look up the admin user's ID (`SELECT id FROM users WHERE username='admin' LIMIT 1`) and pass it to `create_session(..., user_id=user_id)`. Commit: `ff878bd`.

**Prevention:** When `get_session` uses a JOIN, `create_session` must supply the FK. The mismatch here was introduced when the `users` table JOIN was added to `get_session` (probably for a future multi-user feature) without updating the sole call site.

---

## 2026-06-26 — Hub passthrough shows "session expired" on laptop browsers (not phone)

**Symptom:** Logging into a node's dashboard through the Hub's passthrough proxy worked on a phone but immediately showed "session expired" on desktop browsers (Chrome, Firefox, Safari on laptop). Incognito made no difference.

**Root cause:** The passthrough proxy connects to the node over HTTPS (node uses Caddy `tls internal`). The node sets `Secure` on its session cookie. When that cookie is forwarded unchanged to a browser that reached the Hub over HTTP, the browser drops it — the `Secure` flag prohibits storing cookies from HTTP responses. Desktop browsers enforce this strictly; mobile browsers are more lenient.

**Fix applied:** Added `_rewrite_set_cookie()` in `SandOS Hub/backend/app/services/passthrough.py` that strips the `Secure` flag and rewrites the `Path` to the proxy prefix (e.g., `/nodes/{id}/proxy/`) so the cookie is scoped to the proxy path and not the entire Hub domain.

**Prevention:** When proxying from an HTTPS upstream to an HTTP downstream, always strip `Secure` from forwarded `Set-Cookie` headers. Also scope `Path` to the proxy prefix to avoid session token leakage onto unrelated Hub endpoints.

---

## 2026-06-26 — LTE not connecting as WireGuard fallback; node unreachable on throttled WiFi

**Symptom:** Node connected to an open/throttled WiFi (UW "University of Washington" network) that blocked WireGuard UDP. LTE was connected on `wwan0` but both WireGuard tunnels failed anyway. After the battery drained and the Pi rebooted, the device was unreachable until wlan1 dropped and LTE became the default route by accident.

**Root cause:** `wg0` (uplink=wifi) and `wg1` (uplink=lte) both sent their transport UDP via the kernel's default route, which pointed at wlan1 whenever wlan1 was connected. When wlan1's network blocked WireGuard UDP (port 51820), **both** tunnels failed — even wg1 which was explicitly intended for LTE. The `wireguard_profiles` table had an `uplink` column and a comment in `sand-wg` referenced `_setup_wg_transport_routing` in netapply, but that function was never implemented.

**Fix applied:** Implemented `_setup_wg_transport_routing()` in `backend/app/netapply.py`:
- Uses WireGuard's own `FwMark` feature (`wg set <iface> fwmark`) to mark each tunnel's outgoing UDP separately from AP-client device marks
- `wg0` fwmark `0x51` → ip rule → table 210 (populated with wlan1 gateway on each `dhcp4-change`)
- `wg1` fwmark `0x52` → ip rule → table 211 (always routes via `192.0.0.4 dev nat64`, LTE)
- Called from `apply_firewall()` so it re-runs on every NM dispatcher event (wlan1/wwan0 state change)

**Effect:** `wg1` always routes its own UDP through LTE regardless of wlan1 state. Even if a hotel WiFi throttles or blocks WireGuard UDP, the LTE management tunnel stays up.

**Prevention:** Transport routing (where the VPN tunnel's own packets go) must be configured separately from device routing (where VPN clients' packets go). Never assume the default route is adequate for a tunnel whose uplink differs from the default.

---

## 2026-06-11 — pisugar-server pinning 3 of 4 cores (275% CPU); dashboard tabs crawling

**Symptom:** Resources page showed every core peaked; pisugar dashboard process at ~300% CPU; web dashboard tabs took ages to load. Load average ~4.1 with 0% idle.

**Root cause (three compounding):**
1. `pisugar-server` 2.3.2 busy-spins on half-closed HTTP connections — CLOSE-WAIT sockets on `:8421` from disconnected web-UI clients pin its event loop at ~275% CPU. 51 hours of CPU time accumulated in 18 h uptime.
2. The dashboard read the battery **directly over I2C** (`smbus2`, addr 0x57) while pisugar-server polls the same bus — interleaved register reads corrupt both sides (explains pisugar's `Poll error: Remote I/O error` log spam).
3. Every `/api/overview` request synchronously forked 5+ sudo helper subprocesses with zero caching; with all cores starved, each request took many seconds, and every open tab re-paid full cost every 6 s.

**Fix applied:**
1. Staged `CPUQuota=25%` systemd override for pisugar-server (`/home/gateway/staging/pisugar-cpuquota.conf`) + restart — applied via `/home/gateway/staging/apply-fixes.sh` (needs sudo).
2. `services/display.py`: `_battery()` now queries pisugar-server's TCP socket (`127.0.0.1:8423`, `get battery` / `get battery_v`) and only falls back to raw I2C when the daemon is down.
3. New `core/cache.py` with a thread-safe `ttl_cache` decorator. `/api/overview` payload cached 5 s; `/api/system/resources` snapshot cached 3 s. Concurrent tabs now share one computation (the lock dedupes simultaneous misses).

**Prevention:** CPUQuota cap means a recurrence of the spin bug can't starve the system.

**Update (later same day) — root cause eliminated:** Applied the `CPUQuota=25%` override + restart, which dropped pisugar from 361% CPU to 0% and the system from 0% idle to ~92% idle. Then, since the user doesn't use the PiSugar web UI, rebound all three pisugar ports (`:8421/:8422/:8423`) from `0.0.0.0` to `127.0.0.1` in `/etc/default/pisugar-server` (backup `.bak` alongside). External browsers can no longer create the half-closed `:8421` sockets that trigger the spin, so the bug can't recur — and the unauthenticated-web-UI security hole is closed at the same time. The CPUQuota override stays as belt-and-suspenders. Verified `:8421` refuses connections from the LAN and the dashboard still reads the battery over `127.0.0.1:8423`.

---

## 2026-06-11 — Dashboard crash loop: `ModuleNotFoundError: No module named 'psutil'`

**Symptom:** `sand-dashboard` crash-looping (40+ restarts). Web dashboard shows blank white screen.

**Root cause:** `psutil` was added to `services/resources.py` but never added to `requirements.txt`. The root-owned production venv (`/opt/sandos/venv/`) was missing it.

**Fix applied:**
1. Renamed root-owned venv out of the way (gateway owns `/opt/sandos/` parent dir, so rename was possible without sudo):
   ```bash
   mv /opt/sandos/venv /opt/sandos/venv.bak
   ```
2. Created new gateway-owned venv:
   ```bash
   python3 -m venv /opt/sandos/venv
   ```
3. Installed all requirements + psutil:
   ```bash
   /opt/sandos/venv/bin/pip install -r /opt/sandos/backend/requirements.txt psutil
   ```
4. Added `psutil>=5.9` to `requirements.txt` so reinstall won't miss it again.
5. Service auto-restarted (has `Restart=always`) and came up successfully.

**Why sudo wasn't needed:** The venv is at `/opt/sandos/venv/` and the parent `/opt/sandos/` is `gateway:gateway`. Moving a dir only requires write permission on the parent. The new venv was created/owned by gateway, so pip install worked without sudo.

**Prevention:** Always add new imports to `requirements.txt` at the same time as the import is added to code.

---

## Template for future issues

```
## YYYY-MM-DD — Short title

**Symptom:** What the user/system observed.

**Root cause:** Why it happened.

**Fix applied:** Steps taken.

**Prevention:** How to avoid next time.
```

---

## Related

- [[Sand-OS Dashboard]]
- [[Services & Systemd Units]]
- [[Hardware - Raspberry Pi Zero 2 W]]
