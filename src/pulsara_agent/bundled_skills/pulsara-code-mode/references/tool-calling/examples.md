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
console.log({ completeness: search.completeness, next: search.next });

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
check. `exec_cell` returns its accepted CELL immediately while this loop continues.
The selected output arrives actively; no second terminal command or log-fetching
wait is needed.

## 3. Read and save an accepted result body

**Situation:** A native result or a bridge-boundary error supplied a `read` call.
Use its actual value below as `savedRead`; copy it from the earlier result rather
than inventing an entry ID. The source remains the original accepted entry.

```js
const page = await tools.read_file(savedRead.arguments);
console.log(page);
console.log(await tools.write_file({
  target: { kind: "PATH", path: "output/parser-test-log.txt" },
  source: savedRead.arguments.target
}));
```

**How it works:** The read returns one complete text page. The write copies the
whole frozen body directly in the host; it does not save only that first page or
send the body through JavaScript. Choose a new destination because existing files
are never overwritten. Saving can be done without reading a page first.

**Read the result:** Copy the page's complete `next` call to continue reading.
The created file and read pages preserve the source coverage. They cannot recover
content absent from the original result. Reading or saving does not repeat the
command or remote action. See [Results and errors](results-and-errors.md).

## 4. Identify requirements in saved discussions

**Situation:** The user asks which requirements they previously gave for writing
Skills. Search supplies candidates; a semantic condition distinguishes a writing
requirement from an installation request or a report that a Skill failed. ROOT
then synthesizes the actual requirements and later corrections from their sources.

Find relevant sessions with `tools.search_sessions({ query: "Skill" })`, then pass
a selected item's actual `target` to `tools.search_session_content` with the search
query. The example below processes one actual content-search `page`. If a message
omits context needed for the judgment, first expand its returned `read` with
`tools.read_session_content(message.read.arguments)`, following its `next` as needed.
Keep the preceding proposal with replies such as “yes, use that wording.”

```js
const candidates = page.items;
const condition = "Does the user explicitly state a requirement about Skill content, writing style or usage guidance in this discussion?";
const inputs = candidates.map(item => JSON.stringify(
  item.messages.map(({ role, at, text }) => ({ role, at, text }))
));
if (tools.decide.available()) {
  const { decisions } = await tools.decide({ condition, inputs });
  const matches = candidates.filter((_, i) => decisions[i]);
  if (matches.length) console.log({ matches });
} else {
  console.log({ condition, inputs, candidates });
}
```

**How it works:** The condition applies independently to each complete discussion
unit, including speakers and relevant context. “Install this Skill” is different
from “Keep the bundled Skill's guidance concrete and concise.” The returned flags
select original objects, preserving message targets and read calls. ROOT reads and
organizes the matching requirements; Decision does not extract or rewrite them.

**Continue:** Copy the content search's actual `next` call for another source page
when relevant. Unavailable Decision leaves the same candidates and their sources
for ROOT to judge; the search and ordinary task continue without configuration.

## 5. Choose a next tool call from document meaning

**Situation:** The task is to check whether the README explains installing the
project's dependencies. This sample uses one text input, then chooses a further
source lookup when the instructions are absent. Use a read range that includes
the relevant document context, following `next` before judging if needed.

```js
const readme = await tools.read_file({
  target: { kind: "PATH", path: "README.md" }, limit: 200
});
const condition = "Does this document explain how to install this project's dependencies?";
const inputs = [readme.content];
if (tools.decide.available()) {
  const { decisions } = await tools.decide({ condition, inputs });
  if (decisions[0]) {
    console.log({ installation_source: readme });
  } else {
    const candidates = await tools.search_content({
      pattern: "install", file_glob: "*.md", output_mode: "content", limit: 10
    });
    console.log({ readme, installation_candidates: candidates });
  }
} else {
  console.log({ condition, inputs, source: readme });
}
```

**How it works:** The boolean controls an ordinary tool call in the same script.
The unavailable branch preserves the question and source for ROOT instead of
treating missing configuration as a negative judgment. The same interface can
combine file text and agent reports, as in [Workflows](../agent-orchestration/workflows.md).
