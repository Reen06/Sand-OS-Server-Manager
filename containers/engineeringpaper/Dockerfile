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
COPY --from=build /src/dist /usr/share/nginx/html
