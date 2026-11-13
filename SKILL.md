---
name: codex-provider-fork
description: Copy a Codex conversation into a NEW thread that runs on a different model provider (OpenAI official <-> a third-party/sharellm-style provider), leaving the original thread untouched. Uses Codex's own app-server `thread/fork` RPC with a `modelProvider` override, then proves the copy inherits the full history. Use whenever a Codex chat shows "This chat is using X but your current provider is Y", when the user wants to continue an old conversation under another provider, or when they explicitly want a copy/branch rather than an in-place rewrite.
---

# Codex provider fork (copy a thread onto another provider)

## What this does

Creates a **copy** of an existing Codex thread whose `model_provider` is the other one,
so the original conversation keeps working exactly as before.

Codex already supports this natively: `thread/fork` accepts a `modelProvider`
override, and the new thread records a `history_base` pointer to the source rollout
segment instead of duplicating ~hundreds of MB of JSONL. That is the same mechanism
Codex itself uses for "fork conversation", so the copy is fully first-class.

Do **not** hand-copy rollout files, and do not rewrite `threads.model_provider` on the
source thread — that is what the sibling skill `codex-provider-migrate` does (in-place,
destructive); this skill exists precisely to avoid touching the original.

## Quick start

```bash
python scripts/codex_fork_provider.py list --limit 12           # find the thread
python scripts/codex_fork_provider.py fork --thread 01a1a5d3 --provider sharellm
python scripts/codex_fork_provider.py verify --thread <new id>  # optional, re-check
python scripts/codex_fork_provider.py undo --thread <new id>    # deletes the copy
```

Both directions work:

| source provider | `--provider` | typical model |
| --- | --- | --- |
| openai (official) | `sharellm` (or any provider in `config.toml`) | provider default from config.toml |
| sharellm / third-party | `openai` | source model inherited when it looks like `gpt-*` |

## Rules

- **Never** modify the source thread. No DB writes, no rollout edits. The tool only
  calls `thread/fork`, `thread/name/set`, and (on `undo`) `thread/delete`.
- Back up `state_5.sqlite` before forking (the tool does it automatically into
  `<codex_home>/fork_backups/<timestamp>/`; `--no-backup` to skip).
- Keep the source thread around: the copy inherits history **by reference**.
  Deleting the source (or its rollout files) orphans the copy's older turns.
- Renaming the copy is normal (`--suffix`, default ` · <provider> copy`) so the two
  threads are distinguishable in the app sidebar; `--no-rename` keeps the old name.
- `undo` refuses to delete a thread that has no `forked_from_id` — it only removes
  copies, never originals.

## How it works (implementation notes)

1. Resolve the thread: full id, ≥6-char id prefix, or a title fragment, via a
   read-only query on `<codex_home>/state_*.sqlite` (`threads` table). Do not use the
   `thread/list` RPC for this: it does not enumerate forked/segmented threads.
2. Start `codex app-server` on stdio, `initialize` (+ `initialized` notification).
3. `thread/fork` with `{threadId, modelProvider, model?, cwd?, excludeTurns: true, threadSource: "user"}`.
   `excludeTurns` keeps the response small (metadata + live fork state only).
4. `thread/name/set` to name the copy.
5. `verify` compares the newest `turn` id of source and copy via `thread/turns/list`
   (`sortDirection: "desc"`, `limit: 1`) — equal ids prove the history chain resolves.

Useful protocol facts:

- RPC name is `thread/name/set` (not `thread/setName`); delete is `thread/delete`.
- The copy's `session_meta.history_base` points at the **source's last rollout
  segment id** and its byte length; multi-segment threads therefore chain correctly.
- The app/server prints `model_providers.<name>.notify is ignored` on every start when
  `config.toml` has a `notify` key inside a provider table — harmless.
- The desktop app and this tool's own app-server process can run at the same time.

## Model selection

`--model` wins. Otherwise `--provider openai` with a `gpt-*`/`o\d`/`chatgpt*` source
model inherits the source model (the config default usually belongs to the *other*
provider). Anything else falls back to the provider's configured model, i.e. omit
`model` and let the server decide.

## API keys

The provider's `env_key` (e.g. `SHARELLM_API_KEY`) must be visible to the process that
runs `codex`. If it is only set as a **user** environment variable on Windows, the
current shell may not see it — the tool detects that case and says so. Either restart
the shell or set it for the run. `--force` skips this guard.

## Troubleshooting

- `thread/fork` hangs on a huge thread: raise `--wait` (seconds per RPC, default 60).
- Copy opens but the model errors: re-fork with an explicit `--model`.
- Copy looks empty in the sidebar but `verify` passes: the app hides forked/segmented
  threads from `thread/list`; open it by name or `codex resume <id>`.
- `.jsonl.zst` rollouts are supported (`list`/`verify` transparently use the `.zst`
  sibling when the plain `.jsonl` is absent).
