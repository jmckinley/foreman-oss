# intuitive-ux

A full UX review of this project's user-facing surface with **one goal: make it dramatically more
intuitive for a first-time user** — someone who has never seen it, has no docs open, and will not
hover to discover meaning. Report a prioritized redesign plan and score; do **not** edit code here
(report-only, no writes). For foreman the surface is the dashboard (`python -m collectors.web`,
served at 127.0.0.1:8787) rendered by `collectors/web.py`.

Judge by **jobs-to-be-done**, not aesthetics. The test is always: *can a brand-new operator do
this on sight, without help, without a wrong turn?*

Do this:

1. Render the UI (start/curl the dashboard, or open it with the Playwright MCP and screenshot the
   full page). Inspect the rendered HTML + CSS as a user sees it, not just the source.

2. Walk the **core jobs** below as a first-time user. For each, decide: does it *succeed on sight*,
   need a *guess/hover/scroll-hunt* (friction), or *fail* (can't be done, or done wrong, unaided)?
   - **Catch up** — "what changed while I was away?" Is the answer immediate and scannable?
   - **Triage** — "what needs me right now, and what do I do about it?" Is the one next action per
     item obvious?
   - **Act on a queued item** — run a queued dispatch, or approve a push. Are the buttons and their
     consequences self-explanatory (e.g. is "▶ Run now" vs "runs when opened" clear)?
   - **Enable & schedule a loop for a project** — from zero. Can they find it, pick where it runs,
     set a cadence, without knowing the word "cadence" or "tier"?
   - **Understand a loop's state** — verdict, trend, next fire, "where it runs". Is each legible
     without a key?
   - **Read repo health** — "which project needs attention and what should I do?" Does the
     "Suggested next" guidance land, or do the raw stats still overwhelm?
   - **Manage a secret** — see declared keys, spot a shared/leaked value, edit an `.env`.

3. For every friction or failure, cite concrete evidence (section, element, screenshot region) and
   name the **specific proposed change** (new label, reordered element, empty-state copy, removed
   step, added affordance). Rank the findings by leverage — biggest intuitiveness gain per unit of
   change first. This ranked list *is* the deliverable.

4. Also weigh the cross-cutting intuitiveness factors, citing evidence:
   - **Labels & jargon** — does any control use an internal term a newcomer won't know?
   - **Affordances** — is every control obviously clickable/editable and its effect predictable?
     Flag anything whose meaning lives only in a `title=`/hover.
   - **Information scent** — from a collapsed/summary state, can the user tell what's inside and
     whether it needs them?
   - **Empty, never-run & error states** — labelled and inviting, or dead/cryptic?
   - **Consistency** — do the same concept and the same action look the same everywhere?

5. Write `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with exactly these `metrics`:
   - `task_completion_failures` (int): core jobs from step 2 a first-time user **cannot complete
     unaided** (or completes wrongly). This is the primary signal.
   - `friction_points` (int): distinct points of confusion or friction across the walkthrough — a
     guessed label, a hidden action, an ambiguous state, an unexplained term.
   - `intuitiveness_score` (0-100): how much of the UI a first-time user understands and acts on
     correctly **on sight**, with no tooltip, doc, or trial-and-error.

   Plus `deltas` vs the previous intuitive-ux receipt (a JSON array of
   `{"kind":"metric","name":<metric>,"direction":"better|worse|flat"}`), and a one-sentence
   `next_action` naming the single highest-leverage change to make the app more intuitive.
