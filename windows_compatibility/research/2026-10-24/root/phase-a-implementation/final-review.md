# Phase A root final review

2026-10-24. Implementation candidate reviewed against frozen §11.1/§13.1 and current production code. Only desktop resource, host and PG physical seams changed; all callers hard-cut. Main/PythonRuntime/BundledPostgres remain sole policy/lifecycle owners, with Node/PG mechanisms reused. No Phase B, Python/frontend/provider/schema change, new caps/durability/fingerprints, compatibility fallback or future empty adapter.

Root independently found the PG reset eager-path.resolve regression and required a narrow raw/resolved observation fix plus real deleted-cwd child test. The Mac temp-root symlink fixture was corrected rather than weakening the assertion. Final Node46/46 and Python23 pass; root read final test/host/PG/runtime/main/CLI code and no remaining implementation blocker was found.

`mise run --jobs 1 desktop:verify` passed: fresh renderer/application wheel, final Node suite, relocated PG/vector/pg_search/Python/Chromium, release resource/version check. Ruff and diff checks passed. Startup cover and browser actual Electron smokes passed, browser11/11. Installed wheel smoke ran outside checkout with site-packages source proof. Original baseline31/23 and resource/startup smokes are preserved in before/; the first attempted candidate Python run overlapped mise bootstrap removal of editable package, then passed after project installation. No production change was made for that scheduling issue.

Root additionally ran `real-posix-lifecycle.cjs`: real PG init/reuse/extensions/reset then current runtime with actual installed Python ready/HTTP200/inspect/normal stop/lock wait/reopen, all assertions passed in a fresh canonical short private home, cleaned after stopped. Independent agents supplied actual EOF failure, PG metadata/version/data preservation and package/CLI provenance evidence; root read their probes/logs/reports, not just their conclusions.

Actual Mac app-bundle GUI normal/slow/failed quit and reopen evidence, test-only metadata changes, helper-name harness correction, CLI accidental registration and completed restoration are detailed in `macos-gui-review.md` and `gui-cleanup.log`. GUI Python/PG/test processes are gone, exact temp home removed, only test bundle unregistered; original /Applications app/kernel continue running. User CLI now regular executable launcher targeting the existing production app, with no temporary symlink guard or test target. There was no pre-test exact launcher snapshot, so an unverifiable byte-identical restoration claim is deliberately absent.

Native Ubuntu x64/arm64 and Ubuntu graphical/installed qualification remain pending by explicit user decision. No container substitution, Windows claim, G0 or B implementation. No commit, push, public release or production install performed.

Root agrees the implemented Phase A code and actual Mac evidence can be frozen/delivered once all three final candidate reviews agree; this does not mark all-platform A acceptance complete. Final reviewer approvals recorded beside this report after their review.


## Final approval

Root has read all three `freeze-review.md` approvals in terminal/backend/desktop workspaces. Each explicitly approves this final Phase A code and actual Mac evidence with no remaining blocker, retaining native Ubuntu/GUI pending and B untouched. All independently checked the restored regular production CLI and original application/kernel; desktop additionally recorded the exact readonly cleanup JSON. Root agrees and freezes this implementation/evidence scope. Only approval/status metadata was updated afterward; production code and test behavior did not change. No all-platform A or Windows activation claim is made.
