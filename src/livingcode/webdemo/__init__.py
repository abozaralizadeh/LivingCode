"""The web demo: a page where a visitor watches LivingCode fix a planted bug in the sample app.

Two processes, so the AI's code never runs next to the key:

- front.py serves the page, keeps the limits, holds the Azure key and relays LLM calls.
- runner.py does one run on a fresh lab. On the VM it runs in its own systemd unit with no
  network, and it reaches the LLM only through the front's relay socket.

A visitor can only pick one of the scenarios in scenarios.py. Nothing they type is ever run.
"""
