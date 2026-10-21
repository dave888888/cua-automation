# Operator console end-to-end



A separate Playwright browser operates the real console page against the live replay session. `console.webm` is the operator's screen; `shots/` are the proof points; `run/` is the replay's own evidence; `assertions.json` has the observed values.

| | proof | expected |
|---|---|---|
| ✅ | 1. replay encounters the supervisor hold | intervention requested with code SUPERVISOR_HOLD and a failure screenshot |
| ✅ | 2. automation pauses | state awaiting_operator; an agent browser action is refused |
| ✅ | 3. resume attempted before claim | HTTP 409 ownership error |
| ✅ | 4. operator claims the intervention | owner ops.jdoe, state human, epoch advanced |
| ✅ | 5. resume before releasing the hold | rejected; the operator keeps control |
| ✅ | 6. operator clicks Release Hold through the console | human action recorded against the same session, attributed to the owner |
| ✅ | 7. operator resumes | accepted; the original failure is cleared and a checkpoint verified |
| ✅ | 8. automation finishes | status success, one browser session, intervention on that session, s4 completed after the verified hand-back |
| ✅ | 9. old operator token/epoch reused | every request rejected |
