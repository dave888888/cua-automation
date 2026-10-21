# Evidence

Generate with a real model (required by the brief — the discovery run must be genuine):

```bash
python scripts/make_evidence.py --with-share
```

This writes numbered run folders and `INDEX.md` here: one LLM discovery run, the saved artifact
(`artifacts/`), and replays covering success, business outcomes (not found / access denied),
blocked input, recovered faults (transient error, interstitial, session expiry), a hard failure,
the second tenant with and without its overlay (drift), a human takeover of the live session, a
wrong-record response that business invariants refuse to call success, and
(with `--with-share`) the irreversible flow: blocked, authorised, and a lost-response commit
reported as `in_doubt`.
