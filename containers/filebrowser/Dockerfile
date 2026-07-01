# Server-Manager Filebrowser — the NAS file UI as a 'web' app.
# Thin wrapper over the upstream image: the only addition is an entrypoint that
# provisions noauth on first boot (see entrypoint.sh). Everything else — mounts,
# ports, baseURL — is supplied by the Server Manager's App Definition at runtime.
FROM filebrowser/filebrowser:latest

# Invoked via `sh` so no exec bit / RUN layer is needed (the base image's build
# shell is unreliable). busybox sh ships in the base (it runs /init.sh).
COPY entrypoint.sh /sm-entrypoint.sh

ENTRYPOINT ["sh", "/sm-entrypoint.sh"]
