# LivingCode

LivingCode lets a project improve itself while it runs on its server. You install it, turn it on with a feature flag, and give it your own LLM API key. A config file says what it may do, such as fixing bugs it finds in the logs.

The AI part never deploys anything itself. It hands its change to a separate guard, and the guard has no AI in it at all. The guard checks the change, makes a backup, deploys it, watches it, and rolls it back on its own if something goes wrong. A rollback needs no network, no LLM and no key.

"LivingCode" is a working name.

## Status: a lab, not a product

Don't point this at a real project yet. Today it only runs on the throwaway sample app in this repo, on macOS. A hardcoded lab lock in `src/livingcode/common/lab.py` refuses any other project, any other machine and any real LLM host.

Nothing in the lab makes a paid or outside call. These are local stand-ins:

| Real thing | Stand-in in the lab | Real adapter |
| --- | --- | --- |
| Claude and ChatGPT | A scripted fake LLM | Written, tested only against a local fake server |
| PyPI | A local folder and a JSON file | Written, tested only against a local fake server |
| pip-audit, bandit | A local advisory file and a small built-in scanner | Written, checked only with a fake command runner |
| git and a remote | Plain folders | Written, checked only with a fake command runner |
| systemd and its sandbox | A local process manager and macOS `sandbox-exec` | Written, checked only with a fake command runner |

What's missing before anyone should use it for real:

- The guard and the agent running as separate operating system users. That split is the main protection in the design, and it needs Linux to test.
- A run of the Linux part of `setup_service.sh`. It has never been run.
- A try with a real LLM, a real project and a real server.
- A privacy review and an outside security review.

## Quick install

You need macOS and Python 3.11 or newer. There are no other dependencies.

```bash
git clone https://github.com/abozaralizadeh/LivingCode.git
cd LivingCode
./setup_service.sh
```

That writes the lab config to `lab/etc/sample.toml`, installs the sample app, starts it and runs `livingcode doctor`, which ends with a practice rollback. Run it again any time to update and restart everything.

Then try a few commands:

```bash
PYTHONPATH=src python3 -m livingcode status
PYTHONPATH=src python3 -m livingcode history
PYTHONPATH=src python3 -m livingcode lab stop
```

To see the whole thing work once, on a fresh temp lab, in a few seconds:

```bash
python3 scripts/demo.py
```

It shows a visitor hitting a bug, the agent writing a fix and a test, a person approving it, a broken version rolled back on its own, and a rollback with the network blocked. Output from a real run is in [docs/demo-output.txt](docs/demo-output.txt).

Logs go to `lab/var/log`, and each file there is size limited.

## Run the tests

```bash
python3 -m unittest discover -s tests -t .
```

The full run takes a few minutes, because many tests start and stop the sample app for real. Add `-W ignore::ResourceWarning` for quieter output.

## Docs

| Document | What's in it |
| --- | --- |
| [docs/DESIGN.md](docs/DESIGN.md) | Architecture, the reason for each decision, and how the lab build differs from the design |
| [docs/LOGICAL-FLOW.md](docs/LOGICAL-FLOW.md) | The main flows, step by step, with diagrams |
| [docs/OPERATOR.md](docs/OPERATOR.md) | Setting up and running the lab, the commands, and what to do when something goes wrong |
| [docs/ROADMAP.md](docs/ROADMAP.md) | The phases, where things stand, and the biggest risks |

## Contributing

Issues and ideas are welcome. Changes are welcome too, with one rule: nothing may weaken the safety net. The guard keeps no LLM client, no LLM key and no code path that waits on the agent. The agent can only hand over a candidate, and only the guard deploys. The feature flag stays off by default. A rollback never needs the network, the LLM or a working app.

Please add a test for anything you change, and run the whole suite before you send it.

## License

To be decided.
