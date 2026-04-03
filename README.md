# aiagent-repo

Standalone repository for the `aiagent` harness.

## What is included

- `aiagent/agent.py`: agent loop plus s01-s08 style features
- `aiagent/tools/`: structured tools, including buffer-based editing
- `pyproject.toml`: project metadata and dependencies for `uv`
- `requirements.txt`: compatibility dependency list matching the main runtime deps

## Environment

This repo is intended to run with `uv`.

Create or sync the environment:

```bash
uv sync
```

Set your API key in `.env` or the shell environment:

```bash
ANTHROPIC_API_KEY=...
```

If you prefer a traditional dependency file, `requirements.txt` is also included, but `uv sync` is the recommended path.

## Run

Check status:

```bash
uv run python -m aiagent.main --status
```

Interactive mode:

```bash
uv run python -m aiagent.main --interactive
```

One prompt:

```bash
uv run python -m aiagent.main --prompt "List the available tools"
```

Tool call:

```bash
uv run python -m aiagent.main --tool describe_tools
```

## Notes

- Skills are loaded from `skills/` if present.
- Persistent tasks are written to `.tasks/`.
- Conversation transcripts are written to `.transcripts/` when compaction runs.
- Buffer saves preserve the original file newline style.
- Runtime interventions are polled from `.aiagent-intervention.json`.

## Runtime intervention

While the agent loop is running, you can inject an intervention by writing:

```json
{
  "source": "human",
  "action": "pause_and_inject",
  "prompt": "Pause the current workflow. Summarize the blocker and choose a new strategy.",
  "reason": "manual correction"
}
```

to `.aiagent-intervention.json` in the repo root.

Supported actions:

- `pause_and_inject`
- `pause_only`
- `stop`

Example in PowerShell:

```powershell
@'
{
  "source": "human",
  "action": "pause_and_inject",
  "prompt": "Stop repeating the same tool call. Reassess and update todo first.",
  "reason": "manual correction"
}
'@ | Set-Content .aiagent-intervention.json
```
