# Build guide: kodros-modeldeck

## You need

- Python 3.11 or newer. The server uses only the standard library, so there is nothing to install.
- A browser on the same machine.
- For live data: SSH aliases `cluster-a`, `cluster-b`, `build-host` and `client` in `~/.ssh/config`, and a profile gateway whose URL you put in `MODELDECK_CLUSTER_GATEWAY`. Without them the page still loads and marks those sources offline.

## Build

```sh
git clone https://github.com/thefiredev-cloud/kodros-modeldeck.git
cd kodros-modeldeck
```

There is no build step. The page is plain HTML, CSS and JavaScript under `modeldeck/static/`.

## Test

```sh
python3 -m unittest discover -s tests -v
```

On 2026-11-18 this ran 21 tests in about 1.5 seconds. No test runs a swap.

## Run

```sh
python3 -m modeldeck.server --port 8866
```

Open `http://127.0.0.1:8866/`. The server binds to `127.0.0.1` only and checks the `Host` header exactly.

## Install as a desktop entry (optional)

`bin/modeldeck` is a launcher script and `packaging/modeldeck.desktop` is a desktop entry. Replace `/path/to/modeldeck` in its `Exec=` and `Icon=` lines with your clone path, then copy it to `~/.local/share/applications/`.

## How a swap runs

1. Press Review swap. The server issues a 60-second, single-use ticket tied to the profile and the current state.
2. Type `SWAP <profile> WITH 3.5 MINUTE OUTAGE`.
3. A Jev decision check (`jev if ... --state -`) must answer yes.

Then a detached systemd unit calls the `swap-gate` helper on the cluster head. ModelDeck never calls the raw gateway swap route and never retries. A submitted swap is not proof of success. Read the live state and the unit journal.

## Data it writes

A SQLite file with 7 days of samples, mode 0600. The collector refreshes it every 30 seconds.

## Troubleshooting

- Every source shows offline: the SSH aliases or the gateway URL are missing. See "You need".
- A request returns 403: the `Host` header or `Origin` did not match, or the CSRF token is stale. Reload the page.
- Details of every setting and the request flow: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
