// SM mobile touch layer for streamed (Selkies/WebRTC) apps — injected by the
// proxy on touch devices. Selkies' own touch handling is "one finger = left
// drag at the finger" only; this replaces it with TRACKPAD-style control:
//
//   1 finger  drag → move the cursor (relative, like a laptop trackpad —
//             the pointer is separate from your finger)
//             tap → left click at the cursor
//             tap, then immediately drag → click-and-drag (button held)
//             long-press (no movement) → right click at the cursor
//   2 fingers vertical drag → scroll wheel at the cursor
//             pinch → LOCAL view zoom (like a VNC viewer) · drag while
//             zoomed → view pan
//   3 fingers drag → middle-button drag (CAD pan)
//   toolbar   [1:1] reset view zoom · [⌨] summon the soft keyboard
//
// The remote desktop renders its own cursor (Selkies sends "p,1" on touch
// devices), so you see the pointer glide as you swipe. It talks Selkies' own
// input protocol ("m,x,y,buttonMask,0" via window.webrtc.input.send), with
// coordinates computed from the video's bounding rect so they stay correct
// while the view is CSS-zoomed.
(function () {
    "use strict";
    if (!("ontouchstart" in window)) return;

    var LEFT = 1, MIDDLE = 2, RIGHT = 4, WHEEL_DOWN = 8, WHEEL_UP = 16;
    var TAP_MS = 350;        // max touch duration that still counts as a tap
    var TAPDRAG_MS = 300;    // touch again within this after a tap = drag
    var LONGPRESS_MS = 550;  // hold still this long = right click
    var TAP_SLOP = 10;       // px of finger travel before a tap becomes a move
    var PINCH_SLOP = 30;     // px of distance change before 2-finger = pinch
    var WHEEL_STEP = 36;     // px of 2-finger travel per wheel click
    var SPEED = 1.5;         // trackpad cursor speed multiplier

    var video = document.getElementById("stream");
    var container = document.getElementById("video_container");
    if (!video || !container) return;

    function input() {
        return (window.webrtc && window.webrtc.input) || null;
    }

    // ── virtual cursor (client coords, persists between gestures) ──────────
    var cursor = { x: null, y: null };

    function clampCursor() {
        var rect = video.getBoundingClientRect();
        if (cursor.x === null) {           // first use: center of the stream
            cursor.x = rect.left + rect.width / 2;
            cursor.y = rect.top + rect.height / 2;
        }
        cursor.x = Math.max(rect.left, Math.min(rect.right - 1, cursor.x));
        cursor.y = Math.max(rect.top, Math.min(rect.bottom - 1, cursor.y));
    }

    // ── server-coordinate mapping (transform-safe: rect reflects CSS zoom) ──
    function serverXY() {
        clampCursor();
        var rect = video.getBoundingClientRect();
        var fw = video.videoWidth || rect.width;
        var fh = video.videoHeight || rect.height;
        var x = Math.round((cursor.x - rect.left) / rect.width * fw);
        var y = Math.round((cursor.y - rect.top) / rect.height * fh);
        return [
            Math.max(0, Math.min(fw - 1, x)),
            Math.max(0, Math.min(fh - 1, y)),
        ];
    }

    function send(mask) {
        var inp = input();
        if (!inp) return;
        var xy = serverXY();
        inp.x = xy[0];
        inp.y = xy[1];
        inp.buttonMask = mask;
        inp.send(["m", xy[0], xy[1], mask, 0].join(","));
    }

    function click(button) {
        send(button);
        send(0);
    }

    function sendWheel(up) {
        // wheel = press+release of the wheel-button mask bit at the cursor
        send(state.mask | (up ? WHEEL_UP : WHEEL_DOWN));
        send(state.mask);
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
        mode: null,        // track | tapdrag | two | three
        mask: 0,           // buttons currently held
        fx: 0, fy: 0,      // last finger position (for relative deltas)
        startT: 0,
        travel: 0,         // total finger travel this touch (tap detection)
        longPress: 0,      // timer id
        lastTapEnd: 0,     // for tap-then-drag detection
        dist: 0, midX: 0, midY: 0, wheelAcc: 0, pinching: false,
    };

    function release() {
        if (state.mask) {
            send(0);
            state.mask = 0;
        }
        clearTimeout(state.longPress);
        state.mode = null;
        state.pinching = false;
        state.wheelAcc = 0;
    }

    function moveCursor(dx, dy, mask) {
        cursor.x += dx * SPEED;
        cursor.y += dy * SPEED;
        send(mask);
    }

    function touchDist(t) {
        return Math.hypot(
            t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    }

    function onStart(e) {
        var t = e.touches;
        clearTimeout(state.longPress);
        if (t.length === 1) {
            var now = Date.now();
            state.fx = t[0].clientX;
            state.fy = t[0].clientY;
            state.startT = now;
            state.travel = 0;
            if (now - state.lastTapEnd < TAPDRAG_MS) {
                state.mode = "tapdrag";      // tap-then-drag: hold the button
                state.mask = LEFT;
                send(LEFT);
            } else {
                state.mode = "track";        // plain trackpad move
                send(0);                     // wake the remote cursor
                state.longPress = setTimeout(function () {
                    if (state.mode === "track" && state.travel <= TAP_SLOP) {
                        click(RIGHT);        // held still: right click
                        state.mode = null;
                    }
                }, LONGPRESS_MS);
            }
        } else if (t.length === 2) {
            release();
            state.mode = "two";
            state.dist = touchDist(t);
            state.midX = (t[0].clientX + t[1].clientX) / 2;
            state.midY = (t[0].clientY + t[1].clientY) / 2;
        } else if (t.length >= 3) {
            release();
            state.mode = "three";
            state.fx = t[0].clientX;
            state.fy = t[0].clientY;
            state.mask = MIDDLE;
            send(MIDDLE);
        }
    }

    function onMove(e) {
        var t = e.touches;
        if ((state.mode === "track" || state.mode === "tapdrag") && t.length === 1) {
            var dx = t[0].clientX - state.fx;
            var dy = t[0].clientY - state.fy;
            state.fx = t[0].clientX;
            state.fy = t[0].clientY;
            state.travel += Math.hypot(dx, dy);
            if (state.travel > TAP_SLOP) clearTimeout(state.longPress);
            moveCursor(dx, dy, state.mask);
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
                    sendWheel(false);
                    state.wheelAcc -= WHEEL_STEP;
                }
                while (state.wheelAcc < -WHEEL_STEP) {
                    sendWheel(true);
                    state.wheelAcc += WHEEL_STEP;
                }
            }
            state.midX = midX;
            state.midY = midY;
        } else if (state.mode === "three" && t.length >= 3) {
            var mdx = t[0].clientX - state.fx;
            var mdy = t[0].clientY - state.fy;
            state.fx = t[0].clientX;
            state.fy = t[0].clientY;
            moveCursor(mdx, mdy, state.mask);
        }
    }

    function onEnd(e) {
        if (e.touches.length > 0) return; // wait for the last finger
        var now = Date.now();
        if (state.mode === "track" && state.travel <= TAP_SLOP &&
                now - state.startT < TAP_MS) {
            click(LEFT);                   // tap = click at the cursor
            state.lastTapEnd = now;
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
