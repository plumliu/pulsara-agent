$node = 'C:/Users/plumlocal/AppData/Local/mise/installs/node/22.23.3/node.exe'
Set-Location C:/python_workspace/pulsara-agent/frontend
# Before production change: the original 8-case version, 3 pass / 5 fail.
& $node node_modules/vitest/vitest.mjs run ../tests/frontend/app-shortcuts.test.tsx --reporter=verbose
# After the narrow production change and expanded 13-case regression.
& $node node_modules/vitest/vitest.mjs run ../tests/frontend/app-shortcuts.test.tsx ../tests/frontend/session-search-dialog.test.tsx --reporter=verbose
& $node node_modules/vitest/vitest.mjs run ../tests/frontend/app-shortcuts.test.tsx ../tests/frontend/session-search-dialog.test.tsx ../tests/frontend/pulsara-app.test.tsx ../tests/frontend/prompt-draft.test.tsx ../tests/frontend/prompt-suggestions.test.tsx
& $node node_modules/typescript/bin/tsc --noEmit
& $node node_modules/eslint/bin/eslint.js app/pulsara-app.tsx components/session-search-dialog.tsx
