# codex-provider-fork

Copy a Codex conversation into a **new thread that runs on a different model provider** —
and leave the original completely untouched.

Solves the desktop/CLI error:

> **Model provider mismatch** — This chat is using `OpenAI`, but your current provider is `ShareLLM`.
> Switch to `OpenAI` to continue.

Instead of editing the old thread (destructive, and it breaks the app's byte-offset
pagination), this tool asks Codex itself to fork the thread while overriding its
provider. You get a first-class copy: full history, new provider, new name.

```
source thread  ──thread/fork(modelProvider=sharellm)──▶  copy (sharellm)
   (openai, untouched)                                      history inherited by reference
```

## Why not just rewrite the provider?

Because the original then stops working for its own provider, and Codex's paginated
history is byte-offset based: rewriting a 300 MB rollout in place is a footgun.
`thread/fork` already records a `history_base` pointer to the source rollout segment,
so the copy reads the source history instead of duplicating it — exactly what Codex's
own "fork conversation" does. Zero copies of large files, original untouched.

## Requirements

- Codex CLI or desktop app with `app-server` support (tested on `codex-cli 0.167.0-alpha.4.1`).
- Python 3.11+ (`tomllib`). Standard library only — nothing to install.
- The target provider configured in `~/.codex/config.toml` (`[model_providers.<name>]`),
  or a built-in provider such as `openai`.

## Usage

```bash
# 1. find the conversation
python scripts/codex_fork_provider.py list --limit 20
python scripts/codex_fork_provider.py list --search "halcon" --provider openai

# 2. fork it onto the other provider (original untouched)
python scripts/codex_fork_provider.py fork --thread 01a1a5d3-ceaf --provider sharellm

# 3. sanity check: the copy must see the source's latest turn
python scripts/codex_fork_provider.py verify --thread <copy-id>

# 4. remove the copy again (never touches originals)
python scripts/codex_fork_provider.py undo --thread <copy-id>
```

Reverse direction is the same command:

```bash
python scripts/codex_fork_provider.py fork --thread <sharellm-thread> --provider openai
```

Options worth knowing:

| flag | meaning |
| --- | --- |
| `--model` | model id for the copy; defaults to the provider's configured model |
| `--name` / `--suffix` / `--no-rename` | rename the copy (default suffix ` · <provider> copy`) |
| `--cwd` | override the working directory of the copy |
| `--wait` | seconds per RPC (default 60); raise it for very large threads |
| `--no-backup` | skip the automatic `state_*.sqlite` backup |
| `--dry-run` | show the exact `thread/fork` payload and exit |
| `--force` | ignore the missing-API-key guard |

Runs in ~3 seconds even for a thread with 89 turns and 470 MB of rollout history.

## Use as an agent skill

`SKILL.md` is written for agent harnesses (Codex, Claude-style skill directories, DSH).
Point the skill loader at this folder, or copy it into your skills directory:

```powershell
# Windows / DSH example
Copy-Item -Recurse . "$env:USERPROFILE\.dsh\skills\codex-provider-fork"
```

The skill tells the agent *when* to fork rather than migrate, and which protocol
details are easy to get wrong (`thread/name/set` vs `thread/setName`, `thread/list`
not enumerating forks, `excludeTurns`, `.jsonl.zst` siblings).

## 中文速览

把某个 Codex 会话**拷贝**成一份新会话，并让副本改用另一个 provider（OpenAI 官方 ↔ 第三方），
**原始会话保持原样**。实现方式是调用 Codex 自带的 `thread/fork` 并覆盖 `modelProvider`，
历史按引用继承（不复制大文件），因此是原生的一等公民会话。

```bash
python scripts/codex_fork_provider.py list                       # 找会话
python scripts/codex_fork_provider.py fork --thread <id> --provider sharellm
python scripts/codex_fork_provider.py undo --thread <副本id>     # 删除副本
```

## Safety

- Only calls `thread/fork`, `thread/name/set`, `thread/turns/list`, `thread/delete`.
- Never writes to the source thread — no DB updates, no rollout edits.
- `undo` refuses any thread without `forked_from_id` (i.e. never an original).
- `state_*.sqlite` is backed up to `<codex_home>/fork_backups/<timestamp>/` before each fork.
- A copy inherits history by reference: keep the source thread (and its rollout files) around.

## License

MIT — see [LICENSE](LICENSE).
