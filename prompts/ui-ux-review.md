# ui-ux-review

A full UI/UX review of this project's user-facing surface. Report findings and score; do not
redesign here. For foreman the surface is the dashboard (`python -m collectors.web`, served at
127.0.0.1:8787) rendered by `collectors/web.py`.

Do this:
1. Render the UI (start/curl the dashboard, or open it with the Playwright MCP and screenshot
   the full page). Inspect the rendered HTML + CSS, not just the code.
2. Evaluate, as a first-time user, across these dimensions and cite concrete evidence
   (section, element, screenshot region) for every finding:
   - **Information hierarchy** — is the most important thing obvious? Are sections visually
     distinct, or does everything read at the same weight?
   - **Affordances** — is every control self-explanatory? Flag anything that requires a
     hover/tooltip to understand (e.g. a bare `-` cell whose meaning is only in a title=).
   - **Empty & edge states** — are "nothing here", "not enabled", "never run" states labelled
     and inviting, or dead/cryptic?
   - **Section headers & copy** — do headers and their descriptions actually orient the user,
     or are they terse/weak?
   - **Visual design** — spacing, typography, colour, alignment, consistency; does it look
     modern and trustworthy or primitive?
   - **Layout & responsiveness** — scannability, density, grouping.
3. Write `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with exactly these `metrics`:
   `high_severity_findings` (int), `intuitiveness_score` (0-100), `visual_polish_score`
   (0-100); `deltas` vs the previous ui-ux-review receipt (a JSON array of
   `{"kind":"metric","name":<metric>,"direction":"better|worse|flat"}`); and a one-sentence
   `next_action` naming the single highest-leverage fix.
