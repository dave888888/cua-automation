# Design report

## 1. Architecture

One Python process, one live browser session per run, with three paths over a shared seam:

```
 discovery (LLM)  ─┐                        ┌─ WebSurface (Playwright + scan.js)   implemented
 replay (no LLM)  ─┼─▶ Guard ─▶ Surface ────┤
 operator console ─┘    ▲        seam       └─ DesktopSurface (UIA / AT-SPI)        design only
                        │
            SessionControl (control lease)
```

**Trust boundaries.** Each layer trusts only the layer below it:

| layer | trusted for | never trusted for |
|---|---|---|
| model (discovery only) | *proposing* one action per turn, by element ref and symbolic `{{param}}` | values, execution, deciding a step worked |
| host: guard + surface | validating, binding real values, executing under the session lease, the sandbox | — |
| verifier | accepting a step into the artifact only with an observed proof | — |
| artifact + contract | what replay does and what "success" means | — (reviewed, hash-pinned, contract-checked) |
| operator | acting while holding the lease | the state they hand back (re-verified) |

**Target.** A mock "CoreLink Teller" (`mockbank/`) built to be hostile:

- framesets, layout tables, label-less inputs, no test IDs
- a native `confirm()` on commit
- two tenants of the same product, with different version and labels
- injectable runtime faults

I chose a local app over a public demo site because the brief's hard problems are
runtime errors and exceptional states. I need to produce each one on demand, and
safely, including a commit whose response is lost.

**Key decisions:**

- **Python + Playwright, custom perception.** The surface computes role,
  accessible name and *neighbouring label* itself (`scan.js`), and uses the same
  code at record time and replay time. Legacy markup is not ARIA-clean; "the text
  in the cell to the left" is the reliable signal. Playwright gives frame
  handling, network interception (used for the allowlist) and dialogs.
  Selenium has weaker waiting and interception. Sikuli-style image matching is too
  brittle across tenant branding, so it stays a last-resort strategy rather than
  the engine.
- **Hybrid observation for the model.** The model gets a masked screenshot plus an
  element list with refs, and acts by ref; `click_at` is the coordinate fallback.
  Acting by ref lets the recorder *verify* locators against exactly the element
  the model meant. That is what makes record-once/replay-many sound.
- **Claude via tool use, one action per turn.** Every tool takes an `intent`,
  which becomes the step's reviewer-facing "why". Only the latest screenshot stays
  in context. Login runs from a vendor recipe, so the model never sees
  credentials.
- **Verified discovery** (`discovery/agent.py`, `verify.py`). Each turn runs
  Observe → Decide → Validate → Execute → Verify → Record:
  - *Validate*: the ref is in the latest observation, the action fits the
    control's role, params are declared, policy and risk allow it.
  - *Execute*: the host binds the value and acts under the lease.
  - *Verify*:
    - fill and select are read back through the *recorded* locator
    - a click must produce a navigation whose canonical route is re-checked
      live; an in-page change is accepted but flagged `review_required`, with no
      invented postcondition; no change at all is rejected
    - an extract must parse as the declared type
    - a product detector firing afterwards rejects the step
  - Only then is the step *Recorded*, with its `verification` proof.
- **Simple boundaries.** Synchronous runs, files on disk, an in-process operator
  console. Each seam (`Surface`, `LLM`, `SessionControl`, the store) is where a
  service boundary would go. None of them is built out, per §7 of the brief.

## 2. Artifact schema

See `cua/schema/artifact.py` (Pydantic → JSON Schema via `cua schema`). Example:
`evidence/artifacts/`. The shape answers three readers.

**The calling agent** gets a contract, not a step list:

- `inputs`: name, type, pattern/enum, and `sensitivity` (public, internal, pii,
  financial, secret)
- `outputs`: typed, each bound to the extract step that produces it
- `success`: the checkpoint
- `side_effects`: derived as the worst step risk
- `outcomes`: the business results it may return

`cua catalog` generates tool definitions straight from this, so the agent's view
can't drift from what replay enforces.

**The reviewer** gets each step's `intent`, `risk`, `provenance` (agent, human
or authored) and `verification` (how discovery proved it, or `review_required`),
plus a generated review card (`.md`). Approval pins the content hash
(`review.approved_hash`), so any edit silently reverts the artifact to draft.
Semver: re-discovery bumps the minor version, and artifacts are immutable per
version.

**Replay** gets per-step targets and postconditions:

- A `Target` is a frame path plus an **ordered strategy bundle**:
  `role+name → label anchor → text → css → xpath → point`.
- Each strategy is kept only if it resolved *uniquely to the same element*
  during recording.
- Strategies never key on data. Extract targets anchor on the row header
  (`"Share Savings"`, row_cell+1), never on the value. Strategies containing a
  parameter value or a PII pattern are discarded.
- A `fingerprint` (role, name, neighbouring text) records what the control looked
  like, so drift can be explained.
- `expect` is the canonical route of the frame the step navigated, for example
  `/mbr/result?q1={{member_id}}`. Routes are the most stable signal in
  server-rendered apps, and they are relative to the tenant base path.

**Values are references.** A value is `{param}`, `{secret: ref}` or a lint-checked
`{literal}`. Saving refuses an artifact containing a known sensitive value or a
PII pattern.

**Business invariants.** The condition model gains two leaf kinds; there is no
expression language:

- `value`: the control at a target *displays* a param or literal, compared after
  `text`, `digits` or `decimal` normalisation, or matching a regex
- `output`: an extracted output equals a param or matches a regex

These are used in three places:

- **Step `require`** (precondition). Extract steps get "the record on screen is
  the requested one", for example `Account #` shows `{{member_id}}`, so an output
  is only trusted if it was read from the right record.
- **Capability `invariants`**, checked with the success checkpoint. Discovery
  proposes them only after checking them on the live app:
  - the final screen shows each submitted value (`Share Type` shows
    `{{share_type}}`)
  - string outputs keep their observed shape (`^[A-Z]+\-\d+$`)
- **Schema rule.** An input marked `identifies_record` must be bound by a value
  assertion or a route placeholder (`/mbr/{{member_id}}/…`), in the success
  checkpoint, an invariant or an extract precondition. `any` branches don't
  count. A capability whose success is just "the right title appeared" cannot be
  constructed, and an irreversible step must declare a postcondition.

**Workflow contracts** (`config/contracts/<product>.yaml`, `contracts.py`) are
trusted, reviewed constraints matched by capability-id glob, ending in a strict
catch-all. A contract constrains:

- permitted actions, side effects and the number of irreversible steps
- the route scope
- required and identity inputs
- required output types, and whether regex transformations are allowed
- checkpoint strength (`screen` < `identity_bound` < `identity_displayed`)
- `must_confirm` params, routes that must be classed irreversible
- approval, and unattended use

Contracts are checked at approval, and at replay against both the recorded and
the tenant-effective artifact. They are plain typed checks: no code, nothing
model-supplied.

## 3. Determinism & error handling

The same loop runs every step (`replay/engine.py`):

1. Check detectors.
2. Precondition (`require`), e.g. record identity.
3. Resolve the target: one pass over the bundle; *the engine* polls, re-checking
   detectors between polls.
4. Re-check the guard against the *live* control name.
5. Act.
6. Action check: fill/select must read back their value; a navigate must land on
   its route.
7. Await the postcondition, polling detectors.

A strategy matching 0 or more than 1 elements is a miss. Nothing ever does "click
the first match". The run fails unless every declared output was extracted, the
success checkpoint holds, *and* every invariant holds. A displayed value that
contradicts the request on two consecutive polls fails at once
(`RECORD_MISMATCH`) instead of waiting for the timeout.

**Detectors** are known runtime states, recognised by text or URL conditions, and
each is classified. The vendor profile holds the product-wide ones; capabilities
can add their own. This is the result taxonomy the brief asks for:

| class | examples | response |
|---|---|---|
| business outcome | `NOT_FOUND`, `ACCESS_DENIED`, `VALIDATION_ERROR` | `status=business_outcome`, code + app message; don't retry |
| recoverable | interstitial | dismiss |
| recoverable | transient `TP-500` | back off, **restart the flow** |
| recoverable | session expired / signed out | re-login, then restart |
| (no status) | slowness | absorbed by polling up to step timeout |
| blocked (pre-flight) | bad input, version, policy, approval | nothing touched |
| hard failure | auth rejected, unexpected dialog, unknown state, target not found, recovery exhausted | `status=failed`: step, expected, observed, masked screenshot + redacted text |
| **invariant_violated** | wrong record on screen, confirmation not reflecting the request | `status=failed`, never success; the mismatching value is *not* logged (it is another customer's data) |
| **in_doubt** | anything after an irreversible step | never auto-retried; the hint says to verify in the app |
| handback_unverified | operator said resume, nothing provable | session terminated, automation does not resume |

**Restart instead of retry-the-step.** After an error page in a server-rendered
POST flow, the form state the step needed is gone. Flows are read-only until their
commit, so replaying from the entry point is the only retry that is both
deterministic and safe. Once an irreversible step has executed, restart becomes
illegal and the run goes `in_doubt`. The evidence shows a lost-response commit
posting exactly once.

**Drift (secondary).** When a lower-priority strategy resolves, the result
carries a `DriftNote` (step, which strategy matched, what missed). Replay still
succeeds, and drift is visible per tenant before it becomes failure.

Drift is only reported once it is *confirmed*. A URL postcondition holds as soon
as navigation commits, before the new document is parsed. On a slower machine the
primary strategy could run against the half-loaded page and miss, while a
fallback a few milliseconds later hit, which looked like drift on the very tenant
the capability was recorded on. Two changes stop this:

- every postcondition now waits for the page to finish loading
- a fallback hit triggers one settled retry of the primary before it is recorded
  as drift

## 4. Heterogeneity & multi-tenant

**Surface seam.** The artifact never mentions Playwright. A step is "act on
`Target` in frame path F; expect condition C". A surface must:

- observe (elements + screenshot)
- resolve a bundle in one attempt
- act
- read
- evaluate a URL or title condition
- *ground* an element into a bundle

| surface | maps to |
|---|---|
| legacy web | the current adapter (framesets, label anchors) |
| desktop | frame path → window/pane path; role/name/label → UI Automation ControlType/Name and LabeledBy (or sibling text); url → window title or screen id; css/xpath → automation-tree path; point/visual stays last resort and is flagged for review |

The engine, detectors, guard and handoff don't change.

**Reuse across tenants.** Configuration has three layers:

| layer | who owns it | what it holds |
|---|---|---|
| vendor `AppProfile` | written once per product, shared by every capability | login recipe, detectors, version probe |
| capability | recorded once against the product | bound to `product` + a PEP 440 version range |
| `TenantConfig` overlay | per tenant | base URL, version, secret refs, `label_aliases`, per-step target overrides |

`tenancy.specialize` produces the effective artifact and lists every override
applied:

- It rewrites only semantic strategies, including targets *inside* conditions
  and invariants. Structural fallbacks are left alone so they keep working, and
  that is where drift shows up.
- The effective artifact is re-validated against the schema.
- `check_overlay_preserves` compares it to the recorded one with every target
  masked out. An overlay may change *how a control is found*, never the steps,
  risk, values, dialogs, conditions or invariants. Violations block replay
  (`OVERLAY_INVALID`), and the contract is checked again on the effective
  artifact.

The evidence shows this working:

- An artifact recorded on `acme` 4.2.1 runs on `blueriver` 4.3.0 (relabelled) with
  no drift through its overlay.
- Without the overlay it still succeeds via the CSS fallbacks and reports drift on
  all four steps. That drift report is the signal to add aliases instead of
  re-recording.
- Version checks happen pre-flight (tenant config) and at session start (probed
  from the banner).

**At scale** (design): aggregate drift notes per (product, version, tenant) to
detect a vendor upgrade early, re-verify affected capabilities on a canary
tenant, then widen `versions`.

## 5. Escalation & handoff

**Detecting "stuck".**

- In discovery: the model calls `ask_human`; the screen state hash is unchanged
  N times after acting; policy blocks repeat; or step/time limits are hit.
- In replay: any escalatable failure when `on_failure=escalate`, or an
  irreversible step under `irreversible=ask_operator`.

**Control-transfer model** (`handoff/control.py`). There is one lease per live
session, modelled as a small state machine:

```
AUTOMATION ─escalate─▶ PAUSING ─(agent drained)─▶ AWAITING_OPERATOR ─claim(op, epoch)─▶ HUMAN (owned)
    ▲                                                                                   │ resolve(op, lease, epoch)
    │                                     HUMAN ◀── rejected, recoverable ──┐           ▼
    └─────────── verify() proves a checkpoint ───────────────────────────── RESUMING (human drained)
                                                                            │ abort / timeout / unprovable
                                                                            ▼
                                                                        TERMINATED
```

- **Every browser action runs inside `acting(actor)`.** It is admitted only in
  AUTOMATION (agent) or HUMAN (human). The admission check and the in-flight
  increment have no `await` between them, so on the event loop they are atomic.
- **Transitions are ordered.** A transition first changes state, so nothing new
  is admitted, then drains the previous actor's in-flight actions. An action that
  outlives the drain is recorded as unaccounted, and the hand-back then fails
  closed.
- **Ownership is explicit and enforced.**
  - After escalation nobody holds the session (AWAITING_OPERATOR): neither
    automation nor an unclaimed human can act.
  - An operator on the tenant's roster must *claim* the intervention. The claim
    returns a lease token and bumps the epoch.
  - Every later act or resolve must carry the owner's name, that lease and the
    current epoch.
  - Double claims, other operators, forged leases, stale or missing epochs, and
    resolving before claiming are all refused.
- **Verification happens inside the resolution, before control moves.** There
  are three outcomes:
  - **Proven checkpoint:** AUTOMATION.
  - **Recoverable rejection:** the condition that caused the escalation is still
    on screen, or the next step can't run from here, but nothing is unsafe. The
    operator *keeps* the session and is told why; the epoch bumps.
  - **Everything else:** abort, timeout, an unprovable state, or a failed
    approval check on an irreversible step. TERMINATED, fail-closed.
- An `InterventionRequest` is persisted with: subject (capability@version or
  goal), epoch, step and intent, code, reason, masked screenshot, frame routes,
  and the allowed decisions (`resume`/`abort`, or `approve`/`deny`/`abort`).

**The human operates the same session.** The operator console
(`handoff/operator.py`) runs in the automation's event loop and drives the same
Playwright page:

- a live masked screenshot you can click, element actions, fill/select/keys
- with `--headed`, the operator can also just use the browser window, where an
  injected listener captures their clicks and changes

Every action is grounded and recorded on the resolution, attributed to the
owner. During discovery, console actions are compiled into the artifact as
`provenance: human` steps.

**Console security** (`handoff/operator.py`). Localhost-only, and:
- **Session token:** each live session has its own console token, delivered with
  the intervention notice. GET pages and images accept it as `?token=`; every
  mutation must send it in a header.
- **Status codes:** 401 without the token, 403 for the wrong operator or lease,
  409 for state/epoch conflicts and rejected hand-backs, 400 for a missing epoch.
- **Escaping:** every dynamic field is HTML-escaped, and values embedded in the
  page script are JSON-encoded with `<>&` escaped.
- **Where it stops:** operator identity is a roster check plus a claim-bound
  lease, which stands in for SSO.

**Hand-back never trusts state.** `replay/resume.py` is a pure function over
live facts. It resumes only from a proven checkpoint, in this order:

1. The success checkpoint holds, with all invariants and outputs: finish.
2. The failed step's postcondition holds: the human did it; continue after it.
3. The latest earlier postcondition that holds: continue right after it.
4. Nothing holds and nothing irreversible was attempted: restart from the entry
   point.
5. Otherwise: unsafe. The session is terminated.

**Intervention-specific clearance.** The resume point alone is not enough. A
supervisor hold is served *at* the member-detail URL, so the previous step's
postcondition already holds while the hold is still showing; automation would
resume and simply fail again. Before accepting a hand-back, replay therefore
also checks two things:

- the detector that caused the escalation, for example `SUPERVISOR_HOLD` (a new
  `requires_human` detector class), no longer matches
- the step automation would run next has its precondition and control on screen

If either fails, the hand-back is rejected *recoverably*.

An *attempted* irreversible step is never re-executed. That was a real bug in the
previous version: a commit that threw after being attempted could be retried on
resume. An approval is likewise void (`APPROVAL_STATE_CHANGED`) unless the
operator hands back exactly the screen they approved. Interventions are capped
per run.

In the evidence handoff, the policy forbids automation from clicking "Release
Hold" (a supervisor action). `evidence/17-operator-console-e2e/` shows the whole
contract through the real console page, driven by a separate Playwright browser
and recorded as video. Each step has a screenshot and an entry in
`assertions.json`:

1. The replay escalates.
2. The agent is refused while the intervention is unclaimed.
3. Resume before claim gets HTTP 409.
4. The operator claims it.
5. An early resume is rejected, and the operator keeps control.
6. The operator clicks "Release Hold" through the console, on the same session.
7. The resume is accepted.
8. Replay finishes on the one browser session.
9. The old lease and epoch are refused afterwards.

## 6. Safety

**Allowlist and sandbox.** The sandbox lives in `LiveSession`, so discovery,
replay and the operator console share it. It is enforced at three levels:

1. **Network filter** on every request from any frame or actor:
   - origins and route globs
   - HTTP methods; non-GET requests go only to declared `mutation_routes`, which
     are the legacy POST forms (sign-on, review, commit)
   - no data:/blob: documents
2. **Browser behaviour:**
   - popups are closed
   - downloads are cancelled
   - service workers are blocked, and no permissions are granted
   - unexpected `confirm`/`prompt`/`beforeunload` dialogs are dismissed, never
     answered
   - frame navigations the network filter can't see (`data:`, `javascript:`) are
     reset to `about:blank`
3. **Per action, before execution:** allowed action types, and denied control
   names (System Admin, Sign Off, supervisor actions).

Every block is logged as `sandbox_blocked`.

**Risk.**

- Risk is classified deterministically by policy regex on the control name. The
  agent's `commits=true` can raise it, never lower it.
- Replay re-classifies against the *current* policy and the *live* control name,
  so a stricter policy applies to old artifacts.
- Irreversible actions:
  - In discovery: require human approval through the console.
  - In replay: require an *approved* artifact **and** explicit caller
    authorisation, either `allow` or per-run `ask_operator` approval. Without
    both, the run is blocked before anything is touched.
  - The API never exposes `ask_operator`.

I chose "block unless authorised" over "flag" because a wrong share or a wrong
transfer at a bank is not undoable by a later review.

**Data handling.**

- **The model works with symbols.** It sees `{{member_id}}` in the goal and on
  screen, and passes `param="member_id"`. The host binds the value just before
  acting. A literal that looks sensitive is refused.
- **Exposing a value to the model is explicit.** It needs a per-param `expose`
  flag, plus `model_exposure.allow_sensitive_values` in policy for
  pii/financial/secret. A denied exposure is logged.
- **Field-level sensitivity.** The app profile names labels whose neighbouring
  value is sensitive (`Member:`, `Account #:`). The surface registers those values
  for redaction the moment it observes them. Names never pattern-match, yet they
  stay out of prompts, logs, screenshots and failure diagnostics. Money is a
  product pattern.
- Secrets resolve per tenant from env refs; this is the seam to a vault.
- Inputs and outputs declared pii, financial or secret are registered with the
  redactor and scrubbed verbatim, including the display form (`2,345.67`).
- Patterns catch the rest: SSN, Luhn-valid card numbers, emails.
- Redaction applies to: every log event, the observation text sent to the model,
  saved and console screenshots (element boxes blacked out), intervention
  records, and an artifact lint on save.
- Playwright traces are off by default because they store raw DOM.

**Limits.**

- Field-level masking covers only labels the profile names. Unlabelled free-text
  PII on a screen can still reach the model; production still wants a
  zero-retention model endpoint.
- Pattern redaction can miss free-text PII.
- Masking relies on element boxes, so text drawn on a canvas would escape it.
- Policy keys on visible names; a mislabelled commit button would be classed
  `read`. Postconditions and approval are the backstop.
- Console actions bypass action-level policy by design (an authorised human), but
  not the network allowlist or the sandbox.
- Invariants are proposed only where a submitted value is *displayed verbatim*. A
  confirmation screen that reformats values (dates, amounts) needs a reviewer to
  add the assertion; the contract (`must_confirm`) makes that gap block approval.
- An in-page change (no navigation) is accepted but flagged `review_required`.
  It cannot be approved or run unattended until a reviewer supplies an explicit
  condition.
- Operator actions in the headed browser window are logged but not grounded;
  they mark the recording for review instead of becoming steps.

## 7. Cuts

**Built, beyond the core.** Two stretch goals:

- **Cross-tenant reuse**, with overrides and drift.
- **Agent-facing catalog**: `cua catalog`, `cua serve`, and
  `examples/agent_invoke.py`.

Approval pinning exists as a safety gate, not as the confidence-scoring stretch
goal.

**Cut deliberately:**

- the desktop adapter (seam and mapping only)
- the visual-template locator strategy
- the operator console as a real co-browsing app: it is a bare page plus an API,
  with session tokens and claim leases but no SSO, queue routing or real
  notifications (the notice is printed and kept in-process)
- recording headed-browser human actions *into the artifact* (they are logged
  only, because they aren't grounded through the surface)
- LLM-proposed outcome detectors (detectors are authored per vendor profile)
- multi-run stability scoring
- bounded single-step LLM recovery
- persistence beyond files
- concurrent runs (the API serialises them; ownership is per session)

**Next, in order:**

1. Run N-times stability on every new artifact and gate `approved` on it.
2. Use negative-input discovery runs to propose business-outcome detectors for
   review.
3. Add a bounded, policy-checked single-step LLM fallback on `target_not_found`,
   recorded as a proposed artifact patch rather than applied silently.
4. Build a UI Automation desktop surface against a small WinForms app.
5. Route operator interventions to a real queue with auth, SLAs and session
   streaming.
