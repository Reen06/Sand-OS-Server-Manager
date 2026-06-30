# Sand-OS Server Manager

A lightweight, always-on **app & compute orchestrator** for the SandOS homelab. It
starts and stops apps and containers on demand across heterogeneous compute
(Proxmox host, Ubuntu boxes, the personal desktop, Pis), brokers an authenticated
user to a running instance, returns a stream URL, and reaps instances when idle.

Its first flagship workload is **streamed full FreeCAD 1.1** (multi-instance,
per-user, GPU-accelerated where available) — see the design docs.

> **Status: planning.** No application code yet. This repo currently holds only
> status/scaffolding. Architecture and decisions live in the vault, not here.

## Source of truth — the Obsidian vault

Per [ADR-0004 — Obsidian Vault as Source of Truth], the design lives in the
homelab knowledge base, **not** in this repo. Read these before contributing:

- `Homelab-SandOS-KnowledgeBase/Compute/Server Manager.md` — the orchestrator design spec
- `Homelab-SandOS-KnowledgeBase/Projects/Remote FreeCAD.md` — the flagship streamed-FreeCAD workload
- `Homelab-SandOS-KnowledgeBase/Decisions/ADR-0011 - App and Compute Orchestration via Server Manager.md`
- `Homelab-SandOS-KnowledgeBase/Compute/Distributed Compute.md` — the submit-job-to-worker model this implements

Vault root on this machine: `/home/control/Obsidian-Frogmouth/Homelab-SandOS-KnowledgeBase/`

## Sibling codebases

- `../SandOS Hub/` — the always-on Hub (chassis: auth, node registry, apps lifecycle UI) this builds on
- `../Sand-OS/` — the Gateway Node (travel router)

## Local status

See [`PROJECT_STATUS.md`](./PROJECT_STATUS.md).
