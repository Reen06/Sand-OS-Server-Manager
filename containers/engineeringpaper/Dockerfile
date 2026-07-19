# EngineeringPaper.xyz — served as a 'web' app (shared, static). Built with
# the build context pointed at a local checkout of
# https://github.com/mgreminger/EngineeringPaper.xyz (e.g.
# `docker build -t sm-engineeringpaper:latest /home/control/EngineeringPaper.xyz`).
#
# Upstream's "build" script also produces a Cloudflare Worker bundle
# (dist/_worker.js) backing its KV-based sheet-sharing feature — that has no
# equivalent here (no Cloudflare account/KV namespace involved), so it's just
# never invoked; the core math-sheet editor is fully client-side (Pyodide/
# SymPy runs in-browser) and works completely without it.
FROM node:20-alpine AS build
# One git dependency (mathlive)'s own "prepare" script shells out to bash,
# which alpine doesn't ship by default.
RUN apk add --no-cache bash git
WORKDIR /src
COPY . .
RUN npm ci && npm run build:workers && npx vite build

FROM nginx:alpine
# Node runtime for the NAS save/open sidecar (nas-api/server.js, stdlib only,
# no npm deps needed at runtime). See nas-api/server.js for why this exists:
# per-user NAS mount at /nas, browsed/read/written via a small JSON API that
# nginx proxies at /nas-api/*.
RUN apk add --no-cache nodejs
COPY --from=build /src/dist /usr/share/nginx/html
COPY --from=build /src/sandos-nas-api/server.js /nas-api/server.js
COPY --from=build /src/sandos-nas-api/nginx-nas-api.conf /etc/nginx/conf.d/nas-api-location.conf.inc
COPY --from=build /src/sandos-nas-api/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && sed -i '/location \/ {/i\    include /etc/nginx/conf.d/nas-api-location.conf.inc;' /etc/nginx/conf.d/default.conf \
    && grep -q 'nas-api-location.conf.inc' /etc/nginx/conf.d/default.conf
ENTRYPOINT ["/entrypoint.sh"]
