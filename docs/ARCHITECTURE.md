# ModelDeck: architecture

Whiteboard-level structure: what systems exist, what they talk to, and when they run. Each box maps to a file listed in [Evidence](#10-evidence). Diagram sources are Mermaid files in [`architecture/`](architecture/).

Host names used below are role names: **workstation** (runs ModelDeck), **cluster A** (controller and swap helper), **cluster B** (Ollama catalogue), **build host** (model endpoint on a macOS machine) and **client** (second Linux machine with a local model server). Each remote name is an SSH host alias you define in `~/.ssh/config`.

## 1. Purpose

ModelDeck shows which models run where across the fleet: workstation GPU memory and local services, the two-node cluster, the build host and the client. A collector samples every 30 seconds and keeps seven days of history in SQLite. The same page can request a cluster profile swap, but only through a guard: fresh evidence, an idle controller, a single-use 60-second ticket, a typed confirmation phrase and a Jev yes. The swap runs the existing gated helper on cluster A as a detached user unit. ModelDeck never calls the raw gateway swap route.

## 2. Whiteboard view

Seven boxes. Reading the fleet is automatic; changing it takes a human typing a phrase and a Jev yes.

```mermaid
flowchart LR
  owner(["Owner<br/>browser, loopback"]):::actor
  server["ModelDeck server<br/>127.0.0.1:8866"]:::app
  collector["Collector<br/>every 30 s"]:::job
  fleet{{"Fleet services<br/>workstation, cluster A, cluster B, build host, client"}}:::ext
  db[("History<br/>SQLite, 7 days")]:::store
  guard["Swap guard<br/>ticket + phrase + Jev"]:::app
  helper{{"swap-gate on cluster A<br/>existing gated helper"}}:::ext
  owner -->|"views"| server
  collector -->|"read-only"| fleet
  collector -->|"samples"| db
  server -->|"snapshot"| db
  owner -->|"swap request"| guard
  guard -->|"only if gated"| helper
  classDef actor fill:#fde68a,stroke:#b45309,color:#111827
  classDef app fill:#bfdbfe,stroke:#1d4ed8,color:#0b1220
  classDef ext fill:#e5e7eb,stroke:#4b5563,color:#0b1220,stroke-dasharray:4 3
  classDef job fill:#fbcfe8,stroke:#be185d,color:#0b1220
  classDef store fill:#bbf7d0,stroke:#15803d,color:#0b1220
```

## 3. System context (C4 level 1)

Who and what touches the system from outside.

```mermaid
flowchart LR
  owner(["Owner (browser on the workstation)"]):::actor
  SYSTEM["ModelDeck<br/>the system in this repo"]:::app
  local{{"Workstation services (nvidia-smi, Clef, Jev, phone-fast, STT)"}}:::ext
  remote{{"Cluster A, cluster B, build host, client (over SSH)"}}:::ext
  gateway{{"Hermes gateway /v1/cluster/profiles"}}:::ext
  jev{{"Jev decision gate"}}:::ext
  owner -->|"views, requests swap"| SYSTEM
  SYSTEM -->|"HTTP, nvidia-smi"| local
  SYSTEM -->|"SSH probe"| remote
  SYSTEM -->|"profiles"| gateway
  SYSTEM -->|"jev if ... --state -"| jev
  SYSTEM -->|"swap-gate via systemd-run on cluster A"| remote
  classDef actor fill:#fde68a,stroke:#b45309,color:#111827
  classDef app fill:#bfdbfe,stroke:#1d4ed8,color:#0b1220
  classDef ext fill:#e5e7eb,stroke:#4b5563,color:#0b1220,stroke-dasharray:4 3
```

## 4. Containers and components (C4 level 2)

The parts that run or hold data inside the system boundary.

```mermaid
flowchart TB
  owner(["Owner (browser on the workstation)"]):::actor
  local{{"Workstation services (nvidia-smi, Clef, Jev, phone-fast, STT)"}}:::ext
  remote{{"Cluster A, cluster B, build host, client (over SSH)"}}:::ext
  gateway{{"Hermes gateway /v1/cluster/profiles"}}:::ext
  jev{{"Jev decision gate"}}:::ext
  subgraph SYSTEM["ModelDeck"]
    server["HTTP server<br/>ThreadingHTTPServer, 127.0.0.1:8866"]:::app
    collector["Collector<br/>thread, 30 s loop"]:::job
    adapters["Read-only adapters<br/>HTTP, SSH probes"]:::app
    store[("State store + history<br/>SQLite, dir 0700, file 0600")]:::store
    guard["Swap guard<br/>SwapGuard"]:::app
    pending[("pending-swap.json<br/>file, mode 0600")]:::store
    ui["Browser UI<br/>static HTML, JS, CSS"]:::app
  end
  owner -->|"views, requests swap"| ui
  ui -->|"/api/state, /api/swap/*"| server
  server -->|"snapshot"| store
  collector -->|"every 30 s"| adapters
  adapters -->|"HTTP, nvidia-smi"| local
  adapters -->|"SSH probe"| remote
  adapters -->|"profiles"| gateway
  collector -->|"samples"| store
  server -->|"prepare, commit"| guard
  guard -->|"jev if ... --state -"| jev
  guard -->|"admission record"| pending
  guard -->|"swap-gate via systemd-run on cluster A"| remote
  classDef actor fill:#fde68a,stroke:#b45309,color:#111827
  classDef app fill:#bfdbfe,stroke:#1d4ed8,color:#0b1220
  classDef ext fill:#e5e7eb,stroke:#4b5563,color:#0b1220,stroke-dasharray:4 3
  classDef job fill:#fbcfe8,stroke:#be185d,color:#0b1220
  classDef store fill:#bbf7d0,stroke:#15803d,color:#0b1220
```

| Container | Kind | Tech | What it does | Evidence |
|---|---|---|---|---|
| HTTP server | app | ThreadingHTTPServer, 127.0.0.1:8866 | Serves the static UI and `/api/state`. Rejects foreign Host headers; mutations need same-origin, a CSRF token and JSON. | `modeldeck/server.py` |
| Collector | job | thread, 30 s loop | Retries without restart. Offline periods are gaps, not zeros. A failed sample keeps the last data. | `modeldeck/state.py` (`Store.loop`, `Store.collect`) |
| Read-only adapters | app | HTTP, SSH probes | Local service probes, a remote probe script run over SSH with BatchMode, telemetry freshness checks, gateway profile reads. | `modeldeck/adapters.py`, `modeldeck/remote_probe.py` |
| State store + history | store | SQLite, dir 0700, file 0600 | Samples table with 7-day retention. The chart reads the latest 720 samples. | `modeldeck/state.py` (`Store.__init__`, `Store.collect`) |
| Swap guard | app | SwapGuard | `prepare()` issues a 60 s single-use ticket bound to a profile. `commit()` checks the phrase, records admission, then runs the helper. | `modeldeck/guard.py` (`SwapGuard`, `execute_gated`) |
| pending-swap.json | store | file, mode 0600 | Admission record written before SSH. Survives restarts and ambiguity. Cleared only by a fresh terminal job result with the controller idle. | `modeldeck/guard.py` (`SwapGuard`) |
| Browser UI | app | static HTML, JS, CSS | Refreshes every 10 s. Shows offline states, stale labels and the swap dialog. | `modeldeck/static/index.html`, `modeldeck/static/app.js` |

## 5. Key runtime sequences

### 5.1 Reviewing and committing a swap

Any failure at any step leaves the cluster alone. A transport timeout reports an unknown outcome and never retries.

```mermaid
sequenceDiagram
  autonumber
  actor owner as Owner
  participant srv as Server
  participant guard as SwapGuard
  participant jev as Jev
  participant clusterA as Cluster A (ssh)
  owner->>srv: Review swap (profile)
  srv->>guard: prepare(profile)
  guard->>guard: fresh evidence, idle, profile eligible
  guard-->>owner: ticket (60 s) and phrase
  owner->>srv: type SWAP profile WITH 3.5 MINUTE OUTAGE
  srv->>guard: commit(ticket, phrase)
  guard->>jev: safe to submit?
  alt Jev exit 0
    guard->>guard: write pending-swap.json (exclusive)
    guard->>clusterA: systemd-run --user swap-gate swap profile
    guard-->>owner: 202 submitted, not successful
  else anything else
    guard-->>owner: blocked
  end
```

### 5.2 One collector cycle

```mermaid
sequenceDiagram
  autonumber
  participant col as Collector
  participant ad as Adapters
  participant fleet as Fleet
  participant db as SQLite
  col->>ad: collect()
  ad->>fleet: probe services and hosts (SSH BatchMode)
  fleet-->>ad: state or offline
  col->>db: insert sample, delete older than 7 days
  col->>db: read latest 720 for the chart
  Note over col: on failure: keep last sample, retry in 30 s
```

## 6. Data flow and stores

Credentials stay in the server process. The browser gets state and a CSRF token, never keys or URLs.

```mermaid
flowchart LR
  fleet{{"Fleet probes"}}:::ext
  col["Collector"]:::job
  mem[("Snapshot + CSRF")]:::store
  db[("history.sqlite")]:::store
  ui["Browser UI"]:::app
  guard["Swap guard"]:::app
  pend[("pending-swap.json")]:::store
  fleet -->|"read-only"| col
  col -->|"latest"| mem
  col -->|"samples"| db
  mem -->|"/api/state"| ui
  ui -->|"ticket, phrase"| guard
  guard -->|"admission"| pend
  classDef app fill:#bfdbfe,stroke:#1d4ed8,color:#0b1220
  classDef ext fill:#e5e7eb,stroke:#4b5563,color:#0b1220,stroke-dasharray:4 3
  classDef job fill:#fbcfe8,stroke:#be185d,color:#0b1220
  classDef store fill:#bbf7d0,stroke:#15803d,color:#0b1220
```

| Store | What it holds | Written by | Evidence |
|---|---|---|---|
| history.sqlite | samples(at, payload): one summary per 30 s; 7-day retention. | Collector | `modeldeck/state.py` (`Store.collect`) |
| pending-swap.json | Admission record for an in-flight or ambiguous swap. | SwapGuard before SSH | `modeldeck/guard.py` (`SwapGuard`) |
| In-memory snapshot | Latest hosts, models, cluster, history and the per-process CSRF token. | Collector | `modeldeck/state.py` (`Store`) |

## 7. Deployment and runtime topology

One user process on the workstation, bound to loopback only. The swap runs on cluster A as a detached systemd user unit.

```mermaid
flowchart LR
  fleet{{"Other fleet hosts"}}:::ext
  subgraph host_ws["Workstation"]
    srv["python3 -m modeldeck.server<br/>127.0.0.1:8866"]:::app
    col["Collector thread<br/>every 30 s"]:::job
    data[("~/.local/share/modeldeck")]:::store
  end
  subgraph host_a["Cluster A"]
    unit["modeldeck-swap-‹id› unit<br/>only after an approved commit"]:::job
  end
  srv -->|"starts"| col
  col -->|"writes"| data
  col -->|"SSH, HTTP probes"| fleet
  srv -->|"ssh + systemd-run"| unit
  classDef app fill:#bfdbfe,stroke:#1d4ed8,color:#0b1220
  classDef ext fill:#e5e7eb,stroke:#4b5563,color:#0b1220,stroke-dasharray:4 3
  classDef job fill:#fbcfe8,stroke:#be185d,color:#0b1220
  classDef store fill:#bbf7d0,stroke:#15803d,color:#0b1220
```

| What | Where | Runs when | Trigger / port | Evidence |
|---|---|---|---|---|
| Collector sample | Workstation | Every 30 s | Wait loop in `Store.loop` | `modeldeck/state.py` |
| Browser refresh | Browser | Every 10 s | `app.js` | `modeldeck/static/app.js` |
| Swap ticket | SwapGuard | Expires after 60 s | `SwapGuard.prepare()` | `modeldeck/guard.py` |
| History retention | SQLite | 7 days | Delete on each sample | `modeldeck/state.py` (`Store.collect`) |

## 8. Failure modes and how to see them

| Failure | What the user sees | How to see it | Evidence |
|---|---|---|---|
| Cluster offline | Profile actions locked; no fresh profiles or logs | Offline state in the UI | [README](../README.md) |
| Foreign Host header or missing CSRF | 403 Foreign Host rejected, or Same-origin and CSRF required | Response body | `modeldeck/server.py` |
| Jev says anything but yes | Submission blocked | Blocked response; Jev exit code | `modeldeck/guard.py` (`jev_gate`) |
| SSH submission times out | Outcome unknown; never retried; pending record remains | `journalctl --user -u modeldeck-swap-<id>` on cluster A | [README](../README.md) |
| Collector error | Last sample retained; retry in 30 s | `collector_error` field in `/api/state` | `modeldeck/state.py` (`Store.loop`) |
| Controller state unknown | No swap: unknown means locked | Eligibility check message | `modeldeck/guard.py` (`SwapGuard`) |

## 9. State machine

Swap request lifecycle in the guard. Submitted is not successful.

```mermaid
stateDiagram-v2
  direction LR
  [*] --> Idle
  Idle --> Ticketed: prepare, 60 s ticket
  Ticketed --> Idle: ticket expires or phrase wrong
  Ticketed --> Blocked: Jev not yes
  Blocked --> Idle
  Ticketed --> Admitted: pending-swap.json written
  Admitted --> Running: swap-gate unit started
  Admitted --> Unknown: transport timeout
  Running --> Idle: terminal result and controller idle
  Unknown --> Idle: operator inspects, matching job terminal
```

## 10. Evidence

Paths are relative to the repository root.

| Path | Why it matters |
|---|---|
| `modeldeck/server.py` | HTTP handler: Host check, CSRF, JSON, swap routes. |
| `modeldeck/guard.py` | Swap guard, Jev gate, gated helper execution. |
| `modeldeck/state.py` | Store, collector and history. |
| `modeldeck/adapters.py` | Probes, telemetry freshness, gateway profile reads. |
| `modeldeck/remote_probe.py` | Script run over SSH on remote hosts. |
| `tests/test_modeldeck.py` | Server and guard tests. |
| `README.md` | What it shows, swap safety, security and limits. |
| `SOURCES.md` | Research sources and license checks. |

The Mermaid files in [`architecture/`](architecture/) are the diagram sources. This document is maintained by hand, so keep it in step with the code when behavior changes.
