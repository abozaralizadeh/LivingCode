# Roadmap

All of this is Proposed and can still change. The reasons behind each choice are in [DESIGN.md](DESIGN.md).

## How it gets built

The safety net comes first, then the thing it protects against. The guard gets proven with hand-made bad versions before any AI touches it. Each later capability goes out on its own, because each one carries more risk than the last.

## Phases

| Phase | What gets built | Done when |
| --- | --- | --- |
| 0. Setup | Open design choices settled, name checked, a test project picked | Nothing important left open |
| 1. Safety net | Guard, release structure, backups, rollback, breaker, commands, library with the flag | At least 20 kinds of bad version all roll back within 2 minutes, with no AI involved |
| 2. First version (v0.1) | Agent, LLM adapters, bug fixing from logs, redaction, budgets | Planted bugs get fixed and bad fixes get rolled back on the test app |
| 3. Real use | v0.1 running on a small real project for 30 days | No outage caused by LivingCode, and every failed deploy rolled back by itself |
| 4. Keeping current (v0.2) | Updating, fixing vulnerabilities, git sync | 30 days of real use with these switched on |
| 5. Behaviour (v0.3) | Behaviour capture, totals, proposals | Privacy review done, and no personal data found in an audit of what's sent to the LLM |
| 6. New features (v0.4) | Adding features, starting on `propose` | Several real features approved and deployed |
| 7. Public release (v1.0) | Docs, security review, publishing | Outside security review done and its findings fixed |

Each phase has to meet its "done when" before the next one starts.

## Where it is now

The code for phases 1, 2, 4, 5 and 6 exists as a lab. It runs only on the throwaway sample app in this repo, on macOS, with local stand-ins for the LLM, the package registry, the advisories and git. A lab lock in `src/livingcode/common/lab.py` refuses anything else.

Shown by tests that run today:

- 25 kinds of bad version, plus 3 broken builds, all roll back or stop with no AI involved
- killing the guard at every deploy step, then starting it again, leaves the app on a working release
- a rollback works with the network blocked by the operating system and no LLM key around
- the guard never imports the LLM or agent code, and uses only the standard library
- redaction, prompt injection and bypass attempts, budgets, rate limits and the circuit breaker
- the planted bug goes from error to fix to deploy, and a bad fix goes from deploy to rollback to retry
- updates wait 7 days, vulnerable versions get replaced, behaviour totals hide ids and rare routes, and feature size limits hold

Not done yet:

- the guard and agent running as separate operating system users, on Linux
- the Linux part of `setup_service.sh`, which has never been run
- any real LLM, real project or real server
- the privacy review for behaviour watching, and an outside security review

## What the first version includes

The first version proves the whole loop once, with the least risky capability.

- Python web apps run by systemd on one Linux server
- Feature flag, off by default
- Config file, outside the project
- Bug fixing by reading logs, and no other capability
- Triggers: new error in the logs, daily schedule, manual run
- `propose` and `auto` modes
- Claude and ChatGPT through your own key, with hard budget limits
- Secrets and personal details removed before anything goes to the LLM
- Sandbox for tests
- Guard with fixed checks, backup, deploy, health checks, probation, rollback, report, and circuit breaker
- Local git history of every release
- All commands in [DESIGN.md](DESIGN.md), and webhook alerts

Left out of the first version, on purpose:

- Dependency updates and vulnerability fixes
- User behaviour watching
- New features
- Pushing to a remote repository
- Database structure changes
- Any web dashboard

## Later

These come after v1.0. None has a date yet.

- Other programming languages
- Containers and setups with more than one server
- Health checks from outside the server
- More LLM providers, including ones that run on your own machines
- Support for database structure changes, if a safe way can be found

## Biggest risks

| Risk | What keeps it under control |
| --- | --- |
| A bad change passes all checks | Probation, 1 deploy a day, and manual rollback to any of the last 5 releases |
| Attacker text in logs steers the agent | Input treated as data, protected paths, no shell, sandbox, guard checks. This reduces the risk but doesn't remove it |
| Personal data reaches an LLM provider | Fixed removal rules, and a privacy review before real use |
| Rollback fails | Tested in every release, plus the practice rollback in `livingcode doctor`, and an urgent alert when it happens |
| Code rolls back but data doesn't | No database structure changes by the agent, and data restore is always manual |
| Costs run away | Hard limits per round and per month |
