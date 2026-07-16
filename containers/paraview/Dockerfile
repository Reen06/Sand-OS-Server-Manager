# Thin layer on kitware/paraviewweb: adds retry=0 to the launcher ProxyPass.
#
# Apache's mod_proxy has a built-in circuit breaker: the first time a
# ProxyPass backend connection fails, that worker is marked "disabled" for
# a retry-cooldown window (default 60s) — EVERY request in that window gets
# an immediate 503 with no further connection attempt at all, regardless of
# whether the backend has since come up. Confirmed live (2026-07-16):
# `AH00940: HTTP: disabled connection for (localhost)` in the container's
# own 001-pvw_error.log, and a real POST to /paraview/ 503ing consistently
# for 40+ seconds straight — Apache wasn't even trying to reconnect, it was
# just replaying the cooldown.
#
# The backend here (the wslink launcher, port 9000) is a same-container
# sibling process, not a flaky remote service — there's no real network
# between them for a retry-delay to protect against, and one early failed
# connection (during the container's own startup — a race between Apache
# starting and the launcher's socket actually binding) shouldn't lock out
# every real user's very first attempt for a full minute. retry=0 tells
# Apache to always retry on the very next request instead of enforcing
# that cooldown, which is the correct behavior for a co-located backend.
FROM kitware/paraviewweb:pvw-visualizer-osmesa-5.5.0

RUN sed -i \
    's|^\(\s*ProxyPass /paraview http://localhost:9000/paraview\)\s*$|\1 retry=0|' \
    /etc/apache2/sites-available/001-pvw.conf \
    && grep -q 'ProxyPass /paraview http://localhost:9000/paraview retry=0' /etc/apache2/sites-available/001-pvw.conf
