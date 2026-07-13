// SM mobile touch layer for streamed (Selkies/WebRTC) apps — injected by the
// proxy on touch devices. Selkies' own touch handling is "one finger = left
// drag" only; this replaces it with RDP/VNC-viewer style gestures:
//
//   1 finger  tap → left click · drag → left drag · long-press → right click
//   2 fingers pinch → LOCAL view zoom (like a VNC viewer) · drag-while-zoomed
//             → view pan · vertical drag (unzoomed) → scroll wheel
//   3 fingers drag → middle-button drag (CAD pan)
//   toolbar   [1:1] reset view zoom · [⌨] summon the soft keyboard
//
// It talks Selkies' own input protocol ("m,x,y,buttonMask,0" over the data
// channel via window.webrtc.input.send), with coordinates computed from the
// video's bounding rect so they stay correct while the view is CSS-zoomed.
(function () {
    "use strict";
    if (!("ontouchstart" in window)) return;

    var LEFT = 1, MIDDLE = 2, RIGHT = 4, WHEEL_DOWN = 8, WHEEL_UP = 16;
    var TAP_MS = 450, TAP_SLOP = 12, PINCH_SLOP = 30, WHEEL_STEP = 40;

    var video = document.getElementById("stream");
    var container = document.getElementById("video_container");
    if (!video || !container) return;

    function input() {
        return (window.webrtc && window.webrtc.input) || null;
    }

    // ── server-coordinate mapping (transform-safe: rect reflects CSS zoom) ──
    function serverXY(clientX, clientY) {
        var rect = video.getBoundingClientRect();
        var fw = video.videoWidth || rect.width;
        var fh = video.videoHeight || rect.height;
        var x = Math.round((clientX - rect.left) / rect.width * fw);
        var y = Math.round((clientY - rect.top) / rect.height * fh);
        return [
            Math.max(0, Math.min(fw - 1, x)),
            Math.max(0, Math.min(fh - 1, y)),
        ];
    }

    function sendMove(clientX, clientY, mask) {
        var inp = input();
        if (!inp) return;
        var xy = serverXY(clientX, clientY);
        inp.x = xy[0];
        inp.y = xy[1];
        inp.buttonMask = mask;
        inp.send(["m", xy[0], xy[1], mask, 0].join(","));
    }

    function sendWheel(up, clientX, clientY) {
        // wheel = press+release of the wheel-button mask bit at the pointer
        sendMove(clientX, clientY, state.mask | (up ? WHEEL_UP : WHEEL_DOWN));
        sendMove(clientX, clientY, state.mask);
    }

    // ── local view zoom / pan (the VNC-viewer style zoom) ──────────────────
    var view = { scale: 1, tx: 0, ty: 0 };

    function applyView() {
        // clamp pan so the (scaled) stream always covers the viewport
        var w = container.clientWidth, h = container.clientHeight;
        view.tx = Math.min(0, Math.max(w - w * view.scale, view.tx));
        view.ty = Math.min(0, Math.max(h - h * view.scale, view.ty));
        container.style.transformOrigin = "0 0";
        container.style.transform = view.scale === 1
            ? ""
            : "translate(" + view.tx + "px," + view.ty + "px) scale(" + view.scale + ")";
        resetBtn.style.display = view.scale === 1 ? "none" : "";
    }

    function zoomAt(cx, cy, factor) {
        var next = Math.max(1, Math.min(4, view.scale * factor));
        factor = next / view.scale;
        // keep the pinch focal point stationary on screen
        view.tx = cx - (cx - view.tx) * factor;
        view.ty = cy - (cy - view.ty) * factor;
        view.scale = next;
        applyView();
    }

    // ── gesture state machine ───────────────────────────────────────────────
    var state = {
        mode: null,        // tap | drag | two | three
        mask: 0,           // buttons we are currently holding
        startX: 0, startY: 0, startT: 0,
        moved: false,
        longPress: 0,      // timer id
        dist: 0, midX: 0, midY: 0, wheelAcc: 0, pinching: false,
    };

    function release() {
        if (state.mask) {
            sendMove(state.startX, state.startY, 0);
            state.mask = 0;
        }
        clearTimeout(state.longPress);
        state.mode = null;
        state.pinching = false;
        state.wheelAcc = 0;
    }

    function touchDist(t) {
        return Math.hypot(
            t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    }

    function onStart(e) {
        var t = e.touches;
        clearTimeout(state.longPress);
        if (t.length === 1) {
            state.mode = "tap";
            state.moved = false;
            state.startX = t[0].clientX;
            state.startY = t[0].clientY;
            state.startT = Date.now();
            sendMove(state.startX, state.startY, 0); // hover first: place cursor
            state.longPress = setTimeout(function () {
                if (state.mode === "tap" && !state.moved) {
                    state.mode = "drag";               // consumed as right-click
                    sendMove(state.startX, state.startY, RIGHT);
                    setTimeout(function () {
                        sendMove(state.startX, state.startY, 0);
                        state.mode = null;
                    }, 60);
                }
            }, TAP_MS);
        } else if (t.length === 2) {
            if (state.mask) sendMove(state.startX, state.startY, 0);
            state.mask = 0;
            clearTimeout(state.longPress);
            state.mode = "two";
            state.dist = touchDist(t);
            state.midX = (t[0].clientX + t[1].clientX) / 2;
            state.midY = (t[0].clientY + t[1].clientY) / 2;
            state.pinching = false;
            state.wheelAcc = 0;
        } else if (t.length >= 3) {
            if (state.mask) sendMove(state.startX, state.startY, 0);
            clearTimeout(state.longPress);
            state.mode = "three";
            state.startX = t[0].clientX;
            state.startY = t[0].clientY;
            state.mask = MIDDLE;
            sendMove(state.startX, state.startY, MIDDLE);
        }
    }

    function onMove(e) {
        var t = e.touches;
        if (state.mode === "tap" && t.length === 1) {
            var dx = t[0].clientX - state.startX;
            var dy = t[0].clientY - state.startY;
            if (Math.hypot(dx, dy) > TAP_SLOP) {
                state.moved = true;
                clearTimeout(state.longPress);
                state.mode = "drag";
                state.mask = LEFT;
                sendMove(state.startX, state.startY, LEFT);
            }
        }
        if (state.mode === "drag" && t.length === 1) {
            state.startX = t[0].clientX;
            state.startY = t[0].clientY;
            sendMove(state.startX, state.startY, state.mask);
        } else if (state.mode === "two" && t.length === 2) {
            var dist = touchDist(t);
            var midX = (t[0].clientX + t[1].clientX) / 2;
            var midY = (t[0].clientY + t[1].clientY) / 2;
            if (state.pinching || Math.abs(dist - state.dist) > PINCH_SLOP) {
                if (!state.pinching) {
                    state.pinching = true;
                    state.dist = dist;
                }
                zoomAt(midX, midY, dist / state.dist);
                state.dist = dist;
            } else if (view.scale > 1.05) {
                view.tx += midX - state.midX;   // pan the zoomed view
                view.ty += midY - state.midY;
                applyView();
            } else {
                state.wheelAcc += midY - state.midY;   // scroll wheel
                while (state.wheelAcc > WHEEL_STEP) {
                    sendWheel(false, midX, midY);
                    state.wheelAcc -= WHEEL_STEP;
                }
                while (state.wheelAcc < -WHEEL_STEP) {
                    sendWheel(true, midX, midY);
                    state.wheelAcc += WHEEL_STEP;
                }
            }
            state.midX = midX;
            state.midY = midY;
        } else if (state.mode === "three" && t.length >= 3) {
            state.startX = t[0].clientX;
            state.startY = t[0].clientY;
            sendMove(state.startX, state.startY, state.mask);
        }
    }

    function onEnd(e) {
        if (e.touches.length > 0) return; // wait for the last finger
        if (state.mode === "tap" && !state.moved &&
                Date.now() - state.startT < TAP_MS) {
            sendMove(state.startX, state.startY, LEFT);   // tap = click
            sendMove(state.startX, state.startY, 0);
            clearTimeout(state.longPress);
            state.mode = null;
        } else {
            release();
        }
    }

    // Capture-phase on window: fires before (and suppresses) Selkies' own
    // primitive window/element touch listeners.
    ["touchstart", "touchmove", "touchend", "touchcancel"].forEach(function (n) {
        window.addEventListener(n, function (e) {
            if (e.target !== video && !video.contains(e.target)) return;
            e.stopImmediatePropagation();
            e.preventDefault();
            if (n === "touchstart") onStart(e);
            else if (n === "touchmove") onMove(e);
            else onEnd(e);
        }, { capture: true, passive: false });
    });

    // ── toolbar: [1:1] view-zoom reset + [⌨] soft keyboard ────────────────
    function btn(label, title) {
        var b = document.createElement("button");
        b.textContent = label;
        b.title = title;
        b.style.cssText =
            "pointer-events:auto;width:44px;height:44px;border-radius:10px;" +
            "border:1px solid rgba(255,255,255,.25);background:rgba(20,23,28,.75);" +
            "color:#dde3ec;font-size:16px;backdrop-filter:blur(4px);";
        return b;
    }
    var bar = document.createElement("div");
    bar.style.cssText =
        "position:fixed;right:10px;bottom:14px;z-index:2147483000;display:flex;" +
        "flex-direction:column;gap:8px;pointer-events:none;";
    var resetBtn = btn("1:1", "Reset view zoom");
    resetBtn.style.display = "none";
    resetBtn.addEventListener("click", function () {
        view.scale = 1;
        view.tx = view.ty = 0;
        applyView();
    });
    var kbdInput = document.createElement("input");
    kbdInput.type = "text";
    kbdInput.autocapitalize = "off";
    kbdInput.autocomplete = "off";
    kbdInput.style.cssText =
        "position:fixed;left:-2px;bottom:0;width:1px;height:1px;opacity:0.01;";
    var kbdBtn = btn("⌨", "Show keyboard");
    kbdBtn.addEventListener("click", function () {
        if (document.activeElement === kbdInput) kbdInput.blur();
        else kbdInput.focus();   // Guacamole keyboard listens on window
    });
    bar.appendChild(resetBtn);
    bar.appendChild(kbdBtn);
    document.body.appendChild(bar);
    document.body.appendChild(kbdInput);

    window.addEventListener("resize", applyView);
})();
