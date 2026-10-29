# Ordinary tool examples

These examples illustrate ways to connect calls and interpret their output.
Paths, search terms, and commands are illustrative.

## 1. Find relevant code, then read the surrounding lines

**Situation:** You want to understand a Python project's entry points. One
approach is to search, then read an excerpt around each match. In this example,
the reads depend on the search results but are independent of one another.

```js
const search = await tools.search_content({
  pattern: "def main",
  file_glob: "*.py",
  output_mode: "content",
  limit: 5
});
console.log({ total_count: search.total_count, truncated: search.truncated });

const excerpts = await Promise.all(search.matches.map(async match => ({
  target: match.target,
  matched_line: match.line,
  excerpt: await tools.read_file({
    target: match.target,
    offset: Math.max(1, match.line - 3),
    limit: 24
  })
})));
console.log(excerpts);
```

**How it works:** The search supplies real file targets and one-based line
numbers. Each read starts up to three lines before its match. JavaScript builds
the arguments, so ROOT does not have to transcribe paths and line numbers into
separate native calls.

**Read the result:** These are excerpts from up to five matches, not a complete
project analysis. They can help identify a relevant entry point. If more matches
matter, a narrower query or another page can extend the search.

## 2. Run a check once and follow the same process

**Situation:** You need the result of a project check that may take longer than
one tool observation. This example assumes the repository's Python environment
and the named test file exist; use the project's actual check command.

```js
let check = await tools.exec_process({
  command: ".venv/bin/python -m pytest tests/test_parser.py -q",
  yield_time_ms: 1000
});
console.log(check);

while (check.status === "running") {
  check = await tools.manage_process({
    action: "wait",
    target: check.target,
    timeout_seconds: 30,
    since_cursor: check.output_cursor
  });
  console.log(check);
}
```

**How it works:** `exec_process` starts the command once. Each subsequent call waits
on its actual process target and reads from its previous output cursor. The
loop waits on a tool; it does not repeatedly execute the command or spin in JS.

**Read the result:** Use the final status, `exit_code`, and output to assess the
check. If `exec_cell` yields while this loop is waiting, continue reading the script
with `manage_cell` with `action:"wait"`. No second terminal command is needed.

## 3. Inspect a long log and keep its complete available archive

**Situation:** The check above produced a long result with `output_artifact`.
ROOT might want a readable excerpt and a file containing the available archive.
This block can follow the previous code while `check` is still in scope. For a
separate `exec_cell`, copy the actual artifact target from the earlier output instead.

```js
const artifact = check.output_artifact;
if (artifact?.target) {
  const page = await tools.read_artifact({
    target: artifact.target,
    max_chars: 4000
  });
  console.log(page);
  console.log(await tools.export_artifact({
    target: artifact.target,
    path: "output/parser-test-log.txt"
  }));
} else {
  console.log("This result has no readable output artifact.");
}
```

**How it works:** The first call reads one page for inspection. The second exports
the available artifact as a UTF-8 file; it does not export only that first page.
Choose a new destination path, since export does not overwrite an existing file.

**Read the result:** The page's `has_more` and `next_offset_chars` let you continue
reading with `offset_chars`. The export reports its real file target and coverage.
Neither the page nor the file implies that output outside the artifact's stated
coverage is available. See [Results and errors](results-and-errors.md).
