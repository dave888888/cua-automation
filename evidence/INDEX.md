# Evidence index


Each folder is a run directory copied verbatim: `events.jsonl` (redacted structured log), `shots/` (masked screenshots), `dom/` (redacted visible text on failures), `result.json`, `interventions/` (handoff records). Operator actions in handoff runs were performed by `cua/handoff/scripted_operator.py` through the operator console HTTP API (the same calls the console page makes) so the demo is reproducible.

| folder | result | run id |
|---|---|---|
| [01-discovery-savings-balance](01-discovery-savings-balance/) | discovery → recorded (4 steps, model=claude-sonnet-5-5, tokens={'input_tokens': 27331, 'output_tokens': 636}) | `discovery-20261021T185517-4716e7` |
| [02-replay-success](02-replay-success/) | success: {'savings_balance': '[REDACTED:output:savings_balance]'}; recoveries=[]; drift=0; interventions=0 | `replay-20261021T185529-ac3839` |
| [03-replay-not-found](03-replay-not-found/) | business_outcome: NOT_FOUND; recoveries=[]; drift=0; interventions=0 | `replay-20261021T185530-9711c9` |
| [04-replay-access-denied](04-replay-access-denied/) | business_outcome: ACCESS_DENIED; recoveries=[]; drift=0; interventions=0 | `replay-20261021T185531-8a06b7` |
| [05-replay-bad-input](05-replay-bad-input/) | blocked: INVALID_INPUT; recoveries=[]; drift=0; interventions=0 | `replay-20261021T185532-35846e` |
| [06-replay-transient-error-recovered](06-replay-transient-error-recovered/) | success: {'savings_balance': '[REDACTED:output:savings_balance]'}; recoveries=['restart_flow']; drift=0; interventions=0 | `replay-20261021T185532-7216d3` |
| [07-replay-interstitial-dismissed](07-replay-interstitial-dismissed/) | success: {'savings_balance': '[REDACTED:output:savings_balance]'}; recoveries=['dismiss']; drift=0; interventions=0 | `replay-20261021T185535-c73147` |
| [08-replay-session-expired-relogin](08-replay-session-expired-relogin/) | success: {'savings_balance': '[REDACTED:output:savings_balance]'}; recoveries=['restart_session']; drift=0; interventions=0 | `replay-20261021T185537-5e366a` |
| [09-replay-persistent-error-hard-fail](09-replay-persistent-error-hard-fail/) | failed: recovery_exhausted/APP_ERROR_TRANSIENT; recoveries=['restart_flow', 'restart_flow']; drift=0; interventions=0 | `replay-20261021T185538-3c5f1d` |
| [10-replay-tenant-blueriver-overlay](10-replay-tenant-blueriver-overlay/) | success: {'savings_balance': '[REDACTED:output:savings_balance]'}; recoveries=[]; drift=0; interventions=0 | `replay-20261021T185542-61ed70` |
| [11-replay-tenant-blueriver-no-overlay-drift](11-replay-tenant-blueriver-no-overlay-drift/) | success: {'savings_balance': '[REDACTED:output:savings_balance]'}; recoveries=[]; drift=4; interventions=0 | `replay-20261021T185543-afab83` |
| [12-replay-human-handoff](12-replay-human-handoff/) | success: {'savings_balance': '[REDACTED:output:savings_balance]'}; recoveries=[]; drift=0; interventions=1 | `replay-20261021T185544-2f4852` |
| [12b-replay-wrong-record-invariant](12b-replay-wrong-record-invariant/) | failed: invariant_violated/RECORD_MISMATCH; recoveries=[]; drift=0; interventions=0 | `replay-20261021T185546-5ed09a` |
| [13-discovery-open-share-with-approval](13-discovery-open-share-with-approval/) | discovery → recorded (irreversible step approved via operator console) | `discovery-20261021T185547-fbd090` |
| [14-replay-irreversible-blocked](14-replay-irreversible-blocked/) | blocked: IRREVERSIBLE_NOT_AUTHORIZED; recoveries=[]; drift=0; interventions=0 | `replay-20261021T185610-a79853` |
| [15-replay-irreversible-allowed](15-replay-irreversible-allowed/) | success: {'confirmation_number': 'SH-48214'}; recoveries=[]; drift=0; interventions=0 | `replay-20261021T185610-fad6fc` |
| [16-replay-lost-response-in-doubt](16-replay-lost-response-in-doubt/) | failed: in_doubt/APP_ERROR_TRANSIENT; recoveries=[]; drift=0; interventions=0 | `replay-20261021T185612-0a28e6` |
| [17-operator-console-e2e](17-operator-console-e2e/) | operator console driven by a real browser: 9/9 proofs passed; success | `replay-20261021T185614-50b7b4` |
