# Thin layer on ghcr.io/open-webui/open-webui: fixes the code-interpreter
# (Pyodide) sandbox iframe so "Run" on a code block actually works.
#
# Open WebUI's default (non-worker) Pyodide loader creates a hidden iframe
# with sandbox="allow-scripts" — deliberately WITHOUT allow-same-origin, to
# isolate arbitrary user-executed Python from the main page's cookies/
# storage. But that gives the iframe an opaque/null origin, and browsers
# then treat loading pyodide.js into it as genuinely cross-origin even
# though the URL baked into the iframe's srcdoc is same-origin-looking —
# confirmed live 2026-07-16: Safari (mobile) blocks it outright with
# "Cross-origin script load denied by Cross-Origin Resource Sharing
# policy"; Chrome/Firefox (desktop) fail more silently, surfacing as
# "loadPyodide is not defined" once the calling code tries to use a
# function that never got attached to the (opaque-origin) global scope.
# Confirmed this is NOT a proxy/CORS-header issue on our end — pyodide.js
# itself already serves with Access-Control-Allow-Origin: * and loads
# correctly via direct fetch; the failure is specifically the sandboxed
# iframe's own opaque-origin script-loading restriction.
#
# Fix: add allow-same-origin to that one sandbox attribute. Tradeoff,
# accepted deliberately: the code-interpreter iframe now shares origin
# with the main page instead of being fully isolated — it's still a
# hidden, non-navigable, script-only iframe that only ever runs Python the
# user explicitly asked to run (same trust boundary as the chat UI itself
# already crosses), not a meaningfully weaker security posture in
# practice for this use case.
#
# Matched by STRING PATTERN, not filename — the actual file
# (.../chunks/C-Hh4I_e.js as of this writing) is content-hashed and WILL
# change on the next `:main` pull. Build-time grep makes this fail loudly
# instead of silently no-op'ing if the pattern ever stops matching (e.g.
# upstream changes how the sandbox attribute is set, or fixes this
# properly itself).
FROM ghcr.io/open-webui/open-webui:main

RUN grep -rl 'sandbox","allow-scripts"' /app/build/_app/immutable/chunks/ | \
    xargs sed -i 's/sandbox","allow-scripts"/sandbox","allow-scripts allow-same-origin"/g' \
    && grep -rq 'allow-scripts allow-same-origin' /app/build/_app/immutable/chunks/
