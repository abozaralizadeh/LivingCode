# Web demo

A small web page where a visitor picks one of three planted bugs in the sample shop and watches LivingCode fix it live. When an Azure OpenAI key is set, the fix comes from a real AI. Without one, the page runs on each bug's scripted fake AI and says so.

It's made to run on a shared Linux VM behind nginx, under a path such as `/livingcode/`. It only ever works on the throwaway sample app.

## What a visitor sees

1. Three cards, one per bug: splitting a bill with nobody, typing a word for a price, and using a coupon that doesn't exist.
2. They pick one. A fresh lab starts, a visitor request hits the bug, and the agent reads the error and asks the AI for a fix and a test. Every step streams into the terminal window on the page.
3. If the tests pass, the page approves the fix, the guard deploys it and checks it, and the same request now works.
4. Then the demo deploys a broken checkout on purpose, and the guard rolls it back on its own, with no AI involved.

Everyone watching sees the same run. Only one run happens at a time.

## How it's put together

There are two processes.

| Part | systemd unit | What it does |
| --- | --- | --- |
| Front | `idea-livingcode.service` | Serves the page, the live event stream and the limits. It's the only part with the network and the AI key. It also runs the LLM relay, a unix socket in `data/sock/`. |
| Runner | `idea-livingcode-runner.socket` and `idea-livingcode-runner@.service` | Does one run, then exits. systemd starts a fresh one for each run, and the socket only takes one at a time. |

The front sends the runner one line, `{"scenario": "zero-people"}`, and reads back JSON events. When the agent needs the AI, the runner asks the relay, and the relay makes the real call with the key.

The code is in `src/livingcode/webdemo/`:

| File | What's in it |
| --- | --- |
| `front.py` | The web server, the event stream, the relay and the run timer |
| `runner.py` | One whole run, from a fresh lab to the rollback |
| `scenarios.py` | The three bugs, and a scripted fake AI for each |
| `limits.py` | Runs per visitor, runs per day and the token budget |
| `settings.py` | Reads `.env` |
| `static/` | The page. Every URL in it is relative, so it works under any path |

## How it's locked down

A visitor only picks a bug by its id. Nothing they type reaches the AI, a file or a command.

The AI can read and change files in its own copy of the shop, search them, and run the tests. There's no shell action. The tests run in a temp copy with CPU, file size and core dump limits, and the whole run lives inside the runner unit:

- `PrivateNetwork=yes`, so the run has no network but loopback. The runner checks that itself too: the lab lock only allows Linux when `LIVINGCODE_CONFINED=1` is set and every route goes through `lo`.
- `PrivateTmp=yes` is the run's work folder. It's new for each run and gone when the run ends.
- The project is mounted read only, and `.env`, the front's state and the logs aren't visible at all. The runner never gets the key.
- `RuntimeMaxSec` stops a run that hangs, on top of the front's own timer.
- No capabilities, `NoNewPrivileges`, a system call filter, memory and task limits.

The relay only answers while a run is going. It allows 12 calls per run, stops at the run's token cap and at the day's token budget, and only passes on plain chat messages.

The limits, all set in `.env`:

| Setting | Default | What it does |
| --- | --- | --- |
| `RUNS_PER_IP_PER_DAY` | 3 | Runs one visitor can start in a day |
| `DAILY_RUN_CAP` | 40 | Runs in a day, for everyone together |
| `DAILY_TOKEN_BUDGET` | 400000 | AI tokens in a day. 0 means no budget. When it's spent the page says so politely and keeps showing the last run |
| `RUN_TOKEN_CAP` | 80000 | AI tokens in one run |
| `RUN_TIMEOUT_SECONDS` | 300 | A run that takes longer is stopped and thrown away |
| `MAX_WATCHERS` | 100 | Open live streams at once |

Days are UTC. Visitor addresses are never written down. The counts are kept under a salted hash that changes every day.

nginx adds a request rate limit of 5 a second per address with a burst of 30, at most 8 open connections per address, and a 4 KB body limit. The page sends a strict content security policy and has no inline scripts.

## Settings

Copy `.env.example` to `.env`. The setup script does that for you on the first run, and never overwrites an existing `.env`.

| Name | What to put in |
| --- | --- |
| `AZURE_OPENAI_ENDPOINT` | Your Azure OpenAI endpoint, such as `https://<name>.openai.azure.com` |
| `AZURE_OPENAI_API_KEY` | Its key |
| `AZURE_OPENAI_DEPLOYMENT` | The deployment name. `.env.example` has `gpt-6-luna` |
| `AZURE_OPENAI_FALLBACK_ENDPOINT`, `AZURE_OPENAI_FALLBACK_API_KEY`, `AZURE_OPENAI_FALLBACK_DEPLOYMENT` | Optional. Used when the main one fails. `.env.example` has `gpt-5-mini` |
| `LIVINGCODE_WEB_LLM` | `auto` (the default) uses Azure when it's set and the fake otherwise. `fake` forces the fake, `azure` refuses to start without Azure |

The calls use the Azure OpenAI v1 API (`/openai/v1/chat/completions`) with the deployment name as the model.

`PORT`, `HOST` and `TRUST_PROXY` are set by the setup script. The page only listens on `127.0.0.1`, and it only trusts `X-Forwarded-For` from the addresses in `TRUST_PROXY`.

## Set it up on a shared VM

```bash
sudo SERVICE_NAME=idea-livingcode PORT=8606 BASE_PATH=/livingcode/ \
  NGINX_SNIPPET=/etc/nginx/max.d/livingcode.conf ./setup_service.sh
```

Run it from the project folder. The services run as the folder's owner, never as root. Run it again any time to pull the latest code (with `git pull --ff-only`, or skip that with `GIT_PULL=false`) and restart everything.

In this mode the script installs nothing. There's no apt, no pip, no firewall change, no certificates, and nothing in `sites-available` or `sites-enabled`. The code only needs the standard library, so it runs on the system's Python 3 (3.11 or newer) and doesn't need a virtual environment. If Python, nginx, systemd or curl is missing, it stops and says which.

It writes these files and no others:

| File | What it is |
| --- | --- |
| `/etc/systemd/system/idea-livingcode.service` | The front |
| `/etc/systemd/system/idea-livingcode-runner.socket` | The runner's socket |
| `/etc/systemd/system/idea-livingcode-runner@.service` | One run |
| `NGINX_SNIPPET` | `location = /livingcode` with a 301 to `/livingcode/`, and `location /livingcode/` with the proxy, the stream settings and the limits |
| `/etc/nginx/conf.d/idea-livingcode-limits.conf` | The rate and connection limit zones |
| `.env`, `data/` | In the project folder |

Then it runs `nginx -t`. If that fails, it puts both nginx files back the way they were and stops. If it passes, it reloads nginx and checks the health URL.

Logs:

```bash
journalctl -u idea-livingcode -f
journalctl -u 'idea-livingcode-runner@*' -f
```

There's also a size limited copy in `data/logs/livingcode-web.log` (5 MB, plus two older files), easy to copy off the VM.

To see the five files without touching anything, render them into a folder:

```bash
RENDER_TO=/tmp/livingcode-files SERVICE_NAME=idea-livingcode PORT=8606 \
  BASE_PATH=/livingcode/ NGINX_SNIPPET=/etc/nginx/max.d/livingcode.conf ./setup_service.sh
```

## Run it on a Mac

```bash
PYTHONPATH=src python3 -m livingcode.webdemo.front
```

Then open http://127.0.0.1:8606/. Here the runner is a plain child process with a clean environment and no key, but without the systemd walls. That's fine for trying it, not for hosting it.

## Tests

`tests/test_webdemo.py` covers the settings, the limits, the lab lock's one Linux exception, the Azure adapter against a local fake server, the relay, the trusted proxy, whole runs behind a `/livingcode/` prefix, every bug from start to rollback, and the files the setup script writes.

## What hasn't been tried

- The systemd units on a real VM. The runner did a whole run in a Linux container with no network, a read only project and a normal user, and the nginx files passed `nginx -t` and served the page and the stream in nginx 1.24. systemd itself wasn't part of that.
- A run with a real Azure deployment. The adapter is only tested against a local fake server.
