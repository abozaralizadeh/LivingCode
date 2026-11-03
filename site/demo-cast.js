// A real run of scripts/demo.py, made by scripts/record_demo.py. Don't edit by hand.
window.DEMO_CAST = {
 "recorded": "2026-11-03",
 "seconds": 32.6,
 "lines": [
  [
   1.164,
   "LivingCode lab demo. Local fake LLM, throwaway sample app, nothing leaves this Mac."
  ],
  [
   1.166,
   ""
  ],
  [
   1.166,
   "== 1. Install the sample app and check everything before turning anything on"
  ],
  [
   2.638,
   "$ livingcode install"
  ],
  [
   2.638,
   "  Installed. Live release: r0001"
  ],
  [
   4.136,
   "$ livingcode doctor"
  ],
  [
   4.136,
   "  OK   config is valid: <lab>/etc/sample.toml"
  ],
  [
   4.136,
   "  OK   target is the throwaway sample"
  ],
  [
   4.136,
   "  OK   sandbox is available"
  ],
  [
   4.136,
   "  NOTE using the local fake LLM, so no key is needed and nothing is paid for"
  ],
  [
   4.136,
   "  WARN separate guard and agent users aren't in place on this machine, see README"
  ],
  [
   4.136,
   "  OK   the app is running"
  ],
  [
   4.136,
   "  OK   health URL answers: 200"
  ],
  [
   4.136,
   "  OK   smoke checks pass"
  ],
  [
   4.136,
   "  OK   practice rollback worked"
  ],
  [
   4.136,
   "  Doctor passed."
  ],
  [
   4.258,
   "$ livingcode on"
  ],
  [
   4.258,
   "  The feature flag is on."
  ],
  [
   4.369,
   "$ livingcode status"
  ],
  [
   4.369,
   "  Feature flag:   on"
  ],
  [
   4.369,
   "  Live release:   r0001"
  ],
  [
   4.369,
   "  App running:    yes"
  ],
  [
   4.369,
   "  Breaker:        closed"
  ],
  [
   4.369,
   "  Rollbacks in a row: 0"
  ],
  [
   4.369,
   "  Waiting proposals:  0"
  ],
  [
   4.369,
   "  LLM spent this month: $0.0000 of $20.00"
  ],
  [
   4.369,
   ""
  ],
  [
   4.369,
   "== 2. A visitor hits a bug in the sample shop"
  ],
  [
   4.431,
   "  visitor GET /share?total=10&people=0 -> 500"
  ],
  [
   4.433,
   ""
  ],
  [
   4.433,
   "== 3. The agent reads the error and writes a fix and a test (bug fixing is on propose)"
  ],
  [
   5.755,
   "$ livingcode run"
  ],
  [
   5.755,
   "  proposed: Handle zero people in share."
  ],
  [
   5.755,
   "  Candidate: c00001-e90107cbf0"
  ],
  [
   5.783,
   "$ livingcode proposals"
  ],
  [
   5.784,
   "  c00001-e90107cbf0  bug_fixing  Handle zero people in share."
  ],
  [
   5.813,
   "$ livingcode show c00001-e90107cbf0"
  ],
  [
   5.813,
   "  Candidate:  c00001-e90107cbf0"
  ],
  [
   5.813,
   "  Capability: bug_fixing"
  ],
  [
   5.813,
   "  Built on:   r0001"
  ],
  [
   5.813,
   "  Why:        Handle zero people in share."
  ],
  [
   5.813,
   "  "
  ],
  [
   5.813,
   "  --- a/pricing.py"
  ],
  [
   5.813,
   "  +++ b/pricing.py"
  ],
  [
   5.813,
   "  @@ -3,4 +3,6 @@"
  ],
  [
   5.813,
   "   "
  ],
  [
   5.813,
   "   def share(total, people):"
  ],
  [
   5.813,
   "       # Planted bug for the lab: people=0 crashes with ZeroDivisionError."
  ],
  [
   5.813,
   "  +    if people <= 0:"
  ],
  [
   5.813,
   "  +        return tinyfmt.money(0)"
  ],
  [
   5.813,
   "       return tinyfmt.money(total / people)"
  ],
  [
   5.813,
   "  --- a/tests/test_zero_people.py"
  ],
  [
   5.813,
   "  +++ b/tests/test_zero_people.py"
  ],
  [
   5.813,
   "  @@ -0,0 +1,7 @@"
  ],
  [
   5.813,
   "  +import unittest"
  ],
  [
   5.813,
   "  +import pricing"
  ],
  [
   5.813,
   "  +"
  ],
  [
   5.813,
   "  +"
  ],
  [
   5.813,
   "  +class ZeroPeople(unittest.TestCase):"
  ],
  [
   5.813,
   "  +    def test_zero(self):"
  ],
  [
   5.813,
   "  +        self.assertEqual(pricing.share(10, 0), '$0.00')"
  ],
  [
   5.813,
   ""
  ],
  [
   5.813,
   "== 4. A person approves it, and only the guard deploys it"
  ],
  [
   9.922,
   "$ livingcode approve c00001-e90107cbf0"
  ],
  [
   9.922,
   "  Settled: r0003"
  ],
  [
   10.032,
   "  visitor GET /share?total=10&people=0 -> 200"
  ],
  [
   10.032,
   ""
  ],
  [
   10.032,
   "== 5. A person deploys a version that breaks the checkout page. Its tests pass, the smoke check doesn't"
  ],
  [
   19.537,
   "$ livingcode deploy <lab>/people-change"
  ],
  [
   19.538,
   "  RolledBack: smoke check http://127.0.0.1:60623/checkout gave 500"
  ],
  [
   19.538,
   "  (exit 1)"
  ],
  [
   19.543,
   "  visitor GET /checkout -> 200"
  ],
  [
   19.543,
   ""
  ],
  [
   19.543,
   "== 6. Roll back to the release before the fix, with the network blocked by macOS"
  ],
  [
   30.659,
   "$ sandbox-exec (no network) livingcode rollback"
  ],
  [
   30.659,
   "  RolledBack: now on r0001"
  ],
  [
   30.857,
   "  visitor GET /share?total=10&people=0 -> 500"
  ],
  [
   30.861,
   ""
  ],
  [
   30.861,
   "== 7. What happened, and what it cost"
  ],
  [
   30.892,
   "$ livingcode history"
  ],
  [
   30.892,
   "    r0004  failed    human    2026-11-03 22:44:05"
  ],
  [
   30.892,
   "    r0003  settled   agent    2026-11-03 22:44:02"
  ],
  [
   30.892,
   "    r0002  practice  practice 2026-11-03 22:43:58"
  ],
  [
   30.892,
   "  * r0001  settled   human    2026-11-03 22:43:57"
  ],
  [
   30.892,
   "  "
  ],
  [
   30.892,
   "    #4  RolledBack  human    -  failed: manual"
  ],
  [
   30.892,
   "    #3  RolledBack  human    -  failed: startup"
  ],
  [
   30.892,
   "    #2  Settled     agent    bug_fixing"
  ],
  [
   30.892,
   "    #1  RolledBack  practice -  failed: practice"
  ],
  [
   30.92,
   "$ livingcode cost"
  ],
  [
   30.92,
   "  Today:      $0.0077"
  ],
  [
   30.92,
   "  This month: $0.0077 of $20.00"
  ],
  [
   30.92,
   "  (the fake LLM costs nothing. It reports rough token counts so the budget code still gets used)"
  ],
  [
   31.016,
   "  2 LLM calls are written down in sent-to-llm.jsonl, after redaction"
  ],
  [
   31.033,
   "$ livingcode off"
  ],
  [
   31.033,
   "  The feature flag is off. No new rounds start and candidates are refused."
  ],
  [
   31.033,
   ""
  ],
  [
   31.033,
   "Done in 30 seconds."
  ],
  [
   31.296,
   "$ livingcode lab stop"
  ],
  [
   31.296,
   "  The sample app is stopped."
  ]
 ]
};
