# Operator guide

This is how to run LivingCode today. Today means the lab: the throwaway sample app on one Mac, with a fake LLM. The lab lock in `src/livingcode/common/lab.py` refuses anything else, so this guide doesn't cover a real server yet. The Linux part is at the end, and it has never been run.

## What you need

- macOS with Python 3.11 or newer. Nothing gets downloaded, the code only uses the standard library.
- `sandbox-exec`, which comes with macOS. The guard runs candidate tests inside it.

## Set up the lab

```bash
./setup_service.sh
```

The first run writes `lab/etc/sample.toml`, installs the sample app as release `r0001`, starts it, and runs the doctor. Run it again whenever you like. It keeps your config, restarts the app and runs the doctor again.

Everything lives in `lab/`, which git ignores:

| Folder | What's in it |
| --- | --- |
| `lab/etc` | The config, the fake LLM script, the local registry, advisories and feature list |
| `lab/opt/sample` | The app: `repo`, `releases/rNNNN`, the `current` link and `shared` |
| `lab/var/lib` | Guard and agent state, candidates, proposals, reports, backups |
| `lab/var/log` | Every log file, each one size limited |

To start from nothing, run `PYTHONPATH=src python3 -m livingcode lab reset` and then the setup again.

## Commands

All commands take `--config <file>`. Without it they use `LIVINGCODE_CONFIG`, then `lab/etc/sample.toml`. In the lab, run them as `PYTHONPATH=src python3 -m livingcode <command>`.

| Command | What it does |
| --- | --- |
| `doctor` | Checks the config, the lab lock, the sandbox, the app, health and smoke checks, and does a practice rollback |
| `on`, `off` | Turns the feature flag on or off. `on` only works after a doctor pass on the same config |
| `status` | Flag, live release, whether the app runs, breaker, waiting proposals, LLM spending |
| `run` | Starts one improvement round now |
| `proposals` | Lists changes waiting for you |
| `show <id>` | Shows a change, why it was made and its diff |
| `approve <id>` | Approves a proposal. The guard then checks and deploys it |
| `reject <id>` | Throws a proposal away |
| `deploy <ref>` | Deploys a person's change through the same checks, backup and rollback |
| `history` | Releases and deployments |
| `rollback [release]` | Goes back to the previous settled release, or the one you name |
| `resume` | Closes the circuit breaker after you've looked at what went wrong |
| `cost` | LLM spending today and this month |
| `guard-service`, `agent-service` | The two long running services. Add `--once` for a single pass |
| `lab stop`, `lab restart`, `lab reset` | Stops or restarts the sample app, or deletes the whole lab |

## A normal day

1. Run `doctor`, then `on`.
2. Run `guard-service` and `agent-service`, each in its own terminal. In VS Code, the launch config has both.
3. When the app logs an error, the agent picks it up, tries a fix and a test, and leaves a proposal.
4. Look at it with `show`, then `approve` or `reject`.
5. The guard backs up, builds, switches, watches health and smoke checks through probation, and either settles the release or rolls it back on its own.

`scripts/demo.py` does all of this once on a temp lab, in a few seconds. Its last output is in [demo-output.txt](demo-output.txt).

## Modes

Each capability in `[capabilities]` is `off`, `propose` or `auto`.

- `propose` waits for your `approve`. The approval is tied to the exact content of the change, so a change edited after you looked at it is refused.
- `auto` lets the guard deploy without you, still through every check.
- Behaviour watching always waits for you, even on `auto`.
- New features only go out on `auto` after `track_record_releases` settled releases, and only for ids in your own feature list. The agent's own feature ideas always wait.

## When something goes wrong

- A rollback needs no network, no LLM and no key. It switches the `current` link back and restarts the app.
- After `breaker_after` rollbacks in a row, the breaker opens and the agent stops. Only `resume` closes it.
- If a rollback itself fails, the guard stops everything, writes `NeedsHuman` and sends the alert. Look at `history` and the guard log, then `rollback <release>` by hand.
- If the guard is killed halfway through a deploy, it finishes or undoes that deploy on its next start.

## Logs

Logs go to `lab/var/log`: `guard.log`, `agent.log`, the app's own `sample.log`, and `cli-errors.log` for command errors. Each of these rotates at a fixed size and only a few old files are kept, so the whole folder stays small enough to copy off a machine. The app's raw output goes to `sample-stdout.log`, which is cleared on the next app start once it's over its limit.

What the agent sent to the LLM, after redaction, is in `lab/var/lib/agent/sent-to-llm.jsonl`.

## Config settings worth knowing

| Setting | Default | What it does |
| --- | --- | --- |
| `limits.rounds_per_day` | 3 | Improvement rounds per day |
| `limits.deploys_per_day` | 1 | Agent deploys per day |
| `limits.rollback_wait_hours` | 24 | No agent rounds for this long after a rollback |
| `limits.max_changed_files`, `max_changed_lines` | 20, 500 | Bigger changes are refused |
| `limits.breaker_after` | 3 | Rollbacks in a row before the breaker opens |
| `limits.update_wait_days` | 7 | A new dependency version has to be this old first |
| `health.probation_minutes` | 15 | How long a new release is watched |
| `llm.budget_per_round_usd`, `budget_per_month_usd` | 2, 20 | Hard spending limits |
| `safety.protected_paths` | migrations, deploy, auth, payments | Folders the agent can never change |
| `safety.backup_command` | empty | Your own command for backing up data before a deploy |
| `safety.alert_webhook` | empty | Where alerts go. In the lab it has to be on this machine |

## Web demo

The web page where visitors watch a run has its own guide: [WEB-DEMO.md](WEB-DEMO.md).

## Linux (never run)

On Linux, `setup_service.sh` creates the `lc-guard` and `lc-agent` users, the folders and their owners, two systemd services, and a sudoers rule that only lets the guard restart the app. It refuses to run unless you set `LIVINGCODE_I_KNOW_THIS_IS_UNTESTED=yes`, and the lab lock in the code refuses Linux anyway. Treat it as a draft until it has been tried on a throwaway VM. Once it runs, follow the logs with `journalctl -u livingcode-guard.service -f`.
