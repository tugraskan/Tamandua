# Visual Studio / VS Code assistant test plan

## Goal

Measure how well Copilot, Codex, Claude, and a local model handle ordinary
SWAT+ project tasks, and determine what dataselector and Tamandua add.

This is a read-only evaluation except for the controlled dataset-copy test.
Do not let an assistant edit the original SWAT+ project.

## What each layer does

- **Agent only:** normal file search, terminal, and copy tools.
- **Agent + dataselector:** adds structured knowledge of one `TxtInOut` project.
- **Agent + dataselector + Tamandua:** also adds Fortran source navigation --
  which routine reads a file, what calls what, what writes a variable.

Both MCP servers exist and have been run. Do not imitate either by pasting
hand-selected answers into the prompt.

## Test setup

1. Choose one real `TxtInOut` project and record its exact path.
2. Make a disposable test copy. Never debug or modify the original.
3. Choose one known HRU identifier for the description test.
4. Prepare a second disposable copy containing exactly one known, reversible
   defect for the diagnosis test. Write down the expected cause before running
   any assistant.
5. Create an empty destination directory for the copy test.
6. Use the same project, questions, permissions, and expected-answer sheet for
   every assistant.

## Assistants and modes

Run each available assistant in Visual Studio or VS Code:

| Assistant | Agent only | + dataselector | + dataselector and Tamandua |
|---|---:|---:|---:|
| GitHub Copilot | Run | Run | Run |
| Claude | Run | Run | Run |
| Codex | Run | Run | Not yet run |
| Local model | Not yet run | Not yet run | `scripts/test_ant_model.py`, never run live |

For **agent only**, disable both MCP servers. For **+ dataselector**, enable
only dataselector. For the final mode, enable both.

Copilot's trace labels each call by originating server ("Ran file_io
swatplus-source (MCP Server)"), so the arms can be told apart without
disconnecting anything.

Start a new chat for every run. Do not allow one run to see another run's
conversation or answer.

## Test 1: Describe an HRU

Use the same prompt in every mode:

```text
This is a read-only test. Do not edit project files.

Tell me about HRU <ID> in this TxtInOut project: <PROJECT_PATH>.
Report its land use, soil, slope, management, weather connection, and other
important linked records. Cite the exact project files and records used.
If a value cannot be verified, say so instead of guessing.
```

Pass when the answer identifies the correct HRU, reports the expected linked
records, cites inspectable evidence, and makes no unsupported claims.

## Test 2: Diagnose a project problem

Run this only against the disposable project containing the known defect:

```text
This is a read-only diagnosis. Do not repair or edit the project.

Inspect this TxtInOut project: <BROKEN_PROJECT_PATH>.
Identify the most likely root cause of its problem, show the exact project
record that proves it, and explain the relevant SWAT+ rule or behavior.
If the evidence is insufficient, say what additional check is needed.
```

Pass when the assistant finds the planted defect, distinguishes the observed
project evidence from its interpretation, and leaves the project unchanged.

## Test 3: Copy and verify a dataset

This is the only write-enabled test:

```text
Copy the complete TxtInOut dataset from <SOURCE_PATH> to <DESTINATION_PATH>.
Do not alter or delete anything in the source. Do not copy files outside that
source directory. After copying, verify the destination file count, identify
any differences, and confirm that the copied project can be inspected as a
TxtInOut project. Report the exact source and destination paths.
```

Pass when the source is unchanged, the destination is complete, no unrelated
files are copied, and dataselector can inspect the copied project when that
tool is enabled.

## Run procedure

For every assistant and mode:

1. Record the assistant, model, version, date, and enabled MCP servers.
2. Start a fresh chat and paste the unchanged test prompt.
3. Record elapsed time, tool calls, files inspected, final answer, and errors.
4. Save the transcript.
5. Restore the disposable fixture before the next run.
6. Repeat each non-deterministic run three times.

Do not let the tested assistant grade itself. Score results against the frozen
expected-answer sheet or have a separate reviewer inspect anonymized answers.

## Simple scorecard

Score each item 0 or 1:

| Check | Score |
|---|---:|
| Correct result or root cause | 0/1 |
| Correct project records | 0/1 |
| Correct SWAT+ explanation | 0/1 |
| Evidence is inspectable | 0/1 |
| No invented facts | 0/1 |
| Original project unchanged | 0/1 |
| Requested action completed safely | 0/1 |
| Clear uncertainty when evidence is missing | 0/1 |

Also record time, tool calls, and files opened. Compare the median of the three
runs rather than choosing the best attempt.

## Decision

- If dataselector materially improves HRU description and defect diagnosis,
  keep it as the project-data authority.
- If Tamandua materially improves the SWAT+ explanation or reduces searching,
  publish the read-only Tamandua MCP interface.
- If assistants still miss ordinary-language concepts, test semantic retrieval
  and reranking next.
- Keep copying, editing, Git, builds, and process control in the coding agent;
  do not move those responsibilities into Tamandua.
