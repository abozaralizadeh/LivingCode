# Contributing

Thanks for looking. Issues, ideas and changes are all welcome. For anything bigger than a small fix, please open an issue first, so we can agree on the approach before you spend time on it.

## The rule that matters most

Nothing may weaken the safety net. In practice:

- The guard keeps no LLM client, no LLM key and no code path that waits on the agent.
- The agent can only hand over a candidate. Only the guard deploys.
- The feature flag stays off by default, and the agent can't change it.
- A rollback never needs the network, the LLM or a working app.

## Before you send a change

- Add a test for what you change.
- Run the whole suite: `python3 -m unittest discover -s tests -t .`
- Keep the guard to the standard library.

## How contributions are licensed

LivingCode is source available under the PolyForm Noncommercial License 1.0.0, and its docs are under CC BY-NC 4.0. By sending a contribution, you agree that it's licensed under those same terms, and that Abozar Alizadeh may also relicense it under any other terms, commercial ones included. If you don't agree with that, please don't send the contribution.

## Commercial use

Commercial use needs written permission. Ask for it by opening an issue.
