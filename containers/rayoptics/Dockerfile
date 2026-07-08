# Ray Optics Simulation — served as a 'web' app (shared, static). Built from a
# local checkout of https://github.com/ricktu288/ray-optics.git bind-mounted at
# build time as the RAYOPTICS_SRC build context (see build.sh).
#
# Only the simulator target is built (build-app), not the full site (gallery/
# docs) — the simulator is self-contained under dist/{simulator,img,locales}.
FROM node:20-alpine AS build
WORKDIR /src
COPY . .
RUN npm install --no-optional && npm run build-app

FROM nginx:alpine
COPY --from=build /src/dist /usr/share/nginx/html
# Relative (not "/simulator/") so the redirect resolves under whatever subpath
# the Hub proxy serves this app at (e.g. /apps/stream/rayoptics/) instead of
# jumping to the bare domain root.
RUN printf 'server {\n\
    listen 80;\n\
    root /usr/share/nginx/html;\n\
    location = / { return 302 simulator/; }\n\
    location / { try_files $uri $uri/ =404; }\n\
}\n' > /etc/nginx/conf.d/default.conf
