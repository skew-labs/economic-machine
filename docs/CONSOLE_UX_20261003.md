# Engine workspace usability release

The first-use screen now gives the owner four destinations: connect an account,
set shared limits, assign agents, and review their runs. It uses the existing
workspace, policy and agent journal. A read-only record never advances customer
setup progress. Historical payment evidence is collapsed and identified as a
different account; it is not a customer portfolio.

## What changed

- Login opens on an explicit action. A missing browser wallet has a visible
  explanation; no login signature was requested in verification.
- Before login, Connections describes the actual connector families instead of
  showing an empty connector selector. Agents and Order desk explain their
  prerequisites instead of presenting disabled operational forms.
- Authenticated shared-limit and agent forms open when they are the next setup
  step. The owner can still inspect advanced economic programs.
- Workspace, Services and Developer navigation groups share one console. The
  mobile menu exposes all ten destinations and closes after choosing one.
- Four original 64-unit vector app marks identify Atlas, SiteLens, DataPass and
  Engine. Engine uses the same identity on the tool page and in the console.
  Skew's wordmark and NVIDIA's existing brand asset are preserved.
- Layout bounds cover grid children, editor controls, action rows, tables and
  dialogs. Wide account tables can scroll inside their panel.

## Verification

Browser observations and screenshots are under `artifacts/atlas-release/`.
The public Canada console was inspected at 1280, 768, 390 and 320 CSS pixels,
including all ten navigation destinations. The final mobile header refinement
was checked again at 390 and 320. The five tool pages were checked at 1280, 390
and 320. These views had no page-level horizontal overflow, and console controls
outside deliberately scrollable tables stayed within the viewport.

Guide open/close, manual login open/close, menu selection, setup navigation,
historical evidence disclosure, and the C++ synthetic REDUCE example were
checked in the actual browser. The native example reported zero model calls and
no execution authority. Empty editor input now has an actionable message.

Seven focused backend checks passed on Canada: the five Engine portal checks,
the authenticated-prefix check, and the standalone asset/authentication check.
The first prefix invocation used the wrong test class name; it was corrected
and that check passed. Node syntax checks and Ruff passed. Unchanged native
libraries and previously completed broad suites were not rebuilt or rerun.

This is usability and connection-boundary evidence. No customer API keys were
configured, no customer wallet login was completed, and no funds moved. The
hosted runtime still disables live exchange transmission. Authenticated dynamic
account content is not covered by the unauthenticated responsive samples.
