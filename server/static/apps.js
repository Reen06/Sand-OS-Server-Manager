"use strict";

const grid = document.getElementById("grid");
const STATUS_LABEL = { active: "Active", idle: "Idle", starting: "Starting…", stopped: "Stopped" };
let busy = new Set(); // app ids currently starting (don't let refresh stomp the button)

async function api(method, path) {
  const res = await fetch(path, { method, credentials: "same-origin" });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
  return res.json();
}

function card(app) {
  const starting = app.status === "starting" || busy.has(app.id);
  const running = app.status !== "stopped";
  const el = document.createElement("div");
  el.className = "card";
  el.dataset.id = app.id;
  const openLabel = starting ? "Starting…" : running ? "Open" : "▶ Start";
  el.innerHTML = `
    <div class="card__icon">⬡</div>
    <div class="card__name">${app.label}</div>
    <div class="card__desc">${app.desc || ""}</div>
    <button class="btn btn--primary act-open" ${starting ? "disabled" : ""}>${openLabel}</button>
    <div class="chip chip--${starting ? "starting" : app.status}"><span class="dot"></span>${starting ? STATUS_LABEL.starting : STATUS_LABEL[app.status]}</div>
    <button class="btn btn--stop act-stop" ${running || starting ? "" : "hidden"}>■ Stop</button>
  `;
  el.querySelector(".act-open").addEventListener("click", () => onOpen(app));
  el.querySelector(".act-stop").addEventListener("click", () => onStop(app));
  return el;
}

async function waitReady(id) {
  // Poll until the instance's web server is up, then return its URL. FreeCAD's
  // first boot is ~40-60s, so be patient.
  for (let i = 0; i < 80; i++) {
    const { status } = await api("GET", `/api/apps/${id}/status`);
    if (status === "idle" || status === "active") {
      const { apps } = await api("GET", "/api/apps");
      return (apps.find((a) => a.id === id) || {}).url;
    }
    if (status === "stopped") throw new Error("instance stopped before it was ready");
    await refresh();                       // keep the chip showing "Starting…"
    await new Promise((r) => setTimeout(r, 2500));
  }
  throw new Error("timed out waiting for the app to start");
}

async function onOpen(app) {
  if ((app.status === "active" || app.status === "idle") && app.url) {
    window.open(app.url, "_blank");
    return;
  }
  busy.add(app.id);
  await refresh();
  try {
    if (app.status === "stopped") await api("POST", `/api/apps/${app.id}/launch`);
    const url = await waitReady(app.id);    // ← only opens once it's actually serving
    busy.delete(app.id);
    if (url) window.open(url, "_blank");
    await refresh();
  } catch (e) {
    busy.delete(app.id);
    await refresh();
    alert("Couldn't start: " + e.message);
  }
}

async function onStop(app) {
  busy.delete(app.id);
  const c = grid.querySelector(`.card[data-id="${app.id}"]`);
  c.querySelector(".act-stop").disabled = true;
  try { await api("POST", `/api/apps/${app.id}/stop`); } catch (e) { alert(e.message); }
  await refresh();
}

async function refresh() {
  try {
    const { apps } = await api("GET", "/api/apps");
    grid.innerHTML = "";
    apps.forEach((a) => grid.appendChild(card(a)));
  } catch (e) {
    grid.innerHTML = `<div class="loading">Couldn't load apps: ${e.message}</div>`;
  }
}

refresh();
setInterval(() => { if (busy.size === 0) refresh(); }, 5000);
