#!/usr/bin/env python3
"""Fork a Codex conversation into a *copy* that speaks a different model provider.

The original thread is never touched: this tool asks Codex's own app-server to
create a real fork (the same `thread/fork` RPC the desktop app uses), and pins
the fork to another `model_provider` in the same request. Because the fork keeps
Codex's native `history_base` plumbing, the copy inherits the whole conversation
- every segment, compaction and byte offset - without any byte-level surgery.

Usage:
    python codex_fork_provider.py list [--provider P] [--search TERM] [--limit N]
    python codex_fork_provider.py fork --thread ID --provider P [--model M]
    python codex_fork_provider.py verify --thread ID
    python codex_fork_provider.py undo --thread ID --yes

Run `fork --help` for the full option list.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

DEFAULT_PROTOCOL_WAIT = 60.0
STATE_DB_GLOB = "state_*.sqlite"


# --------------------------------------------------------------------------- #
# environment discovery
# --------------------------------------------------------------------------- #
def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def find_state_db(home: Path) -> Path:
    """Newest state_*.sqlite (Codex bumps the number across schema versions)."""
    cands = []
    for p in home.glob(STATE_DB_GLOB):
        if p.is_file():
            version = 0
            stem = p.stem  # state_5
            tail = stem.rsplit("_", 1)[-1]
            if tail.isdigit():
                version = int(tail)
            cands.append((version, p.stat().st_mtime, p))
    if not cands:
        raise SystemExit(f"no {STATE_DB_GLOB} found under {home}")
    cands.sort()
    return cands[-1][2]


def find_codex_exe(explicit: str | None = None) -> Path:
    """Locate a codex executable: --codex-exe, $CODEX_EXE, PATH, app bundle, VS Code."""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise SystemExit(f"--codex-exe not found: {p}")

    env = os.environ.get("CODEX_EXE")
    if env and Path(env).is_file():
        return Path(env)

    which = shutil.which("codex")
    if which:
        return Path(which)

    home = Path.home()

    def newest(paths: list[Path]) -> Path | None:
        paths = [p for p in paths if p.is_file()]
        if not paths:
            return None
        paths.sort(key=lambda p: p.stat().st_mtime)
        return paths[-1]

    # the desktop app is the primary consumer of these threads, so prefer its build
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found = newest(list((Path(local) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe")))
        if found:
            return found
        found = newest(list((Path(local) / "Programs" / "codex").glob("**/codex.exe")))
        if found:
            return found

    found = newest(list((home / ".vscode" / "extensions").glob("openai.chatgpt-*/bin/*/codex.exe")))
    if found:
        return found
    found = newest(list((home / ".cursor" / "extensions").glob("openai.chatgpt-*/bin/*/codex.exe")))
    if found:
        return found
    found = newest(list((home / ".codex" / "bin").glob("codex*")))
    if found:
        return found

    raise SystemExit(
        "codex executable not found. Pass --codex-exe, set CODEX_EXE, or put codex on PATH."
    )


# --------------------------------------------------------------------------- #
# state db (read-only)
# --------------------------------------------------------------------------- #
def open_state_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def load_thread(con: sqlite3.Connection, thread_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if row is None:
        raise SystemExit(f"thread not found: {thread_id}")
    return row


def resolve_thread(con: sqlite3.Connection, needle: str) -> sqlite3.Row:
    """Accept a full id, an id prefix (>=6 chars), or a unique title fragment."""
    needle = needle.strip()
    if not needle:
        raise SystemExit("empty thread selector")
    row = con.execute("SELECT * FROM threads WHERE id = ?", (needle,)).fetchone()
    if row is not None:
        return row
    if len(needle) >= 6 and all(c in "0123456789abcdef-" for c in needle.lower()):
        rows = con.execute(
            "SELECT * FROM threads WHERE id LIKE ? ORDER BY updated_at DESC", (needle + "%",)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            ids = ", ".join(r["id"][:13] for r in rows[:8])
            raise SystemExit(f"ambiguous thread prefix '{needle}' ({len(rows)} matches): {ids}")
    rows = con.execute(
        "SELECT * FROM threads WHERE COALESCE(name,'') LIKE ? OR COALESCE(title,'') LIKE ?"
        " ORDER BY updated_at DESC LIMIT 10",
        (f"%{needle}%", f"%{needle}%"),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise SystemExit(f"no thread matches '{needle}'")
    lines = [f"  {r['id']}  [{r['model_provider']}]  {(r['name'] or '')[:60]}" for r in rows]
    raise SystemExit("ambiguous match, pass the full thread id:\n" + "\n".join(lines))


def rollout_of(row: sqlite3.Row) -> Path | None:
    raw = row["rollout_path"]
    if not raw:
        return None
    p = Path(raw.replace("\\\\?\\", ""))
    if p.is_file():
        return p
    zst = p.with_suffix(p.suffix + ".zst")
    if zst.is_file():
        return zst
    return None


def config_providers(home: Path) -> dict:
    """Best-effort read of [model_providers.*] from config.toml."""
    cfg = home / "config.toml"
    if not cfg.is_file():
        return {}
    try:
        import tomllib  # py>=3.11
    except ModuleNotFoundError:  # pragma: no cover
        return {}
    try:
        with cfg.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return {}
    providers = data.get("model_providers") or {}
    out = {}
    for name, body in providers.items():
        if isinstance(body, dict):
            out[name] = {
                "name": body.get("name"),
                "base_url": body.get("base_url"),
                "env_key": body.get("env_key") or body.get("env_key_instructions"),
                "wire_api": body.get("wire_api"),
            }
    return out


def default_provider(home: Path) -> str | None:
    cfg = home / "config.toml"
    try:
        import tomllib
        with cfg.open("rb") as fh:
            return tomllib.load(fh).get("model_provider")
    except Exception:
        return None


def _win_user_env(name: str) -> str | None:
    """Windows keeps user env vars in the registry; a stale shell won't see them."""
    try:
        import winreg
    except Exception:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except OSError:
        return None


def env_key_state(env_key: str) -> str:
    if os.environ.get(env_key):
        return "set in this process"
    if os.name == "nt" and _win_user_env(env_key):
        return "set in the user environment (registry) but NOT in this process - reopen your shell/app"
    return "NOT SET - the forked thread will fail on its first request"


# --------------------------------------------------------------------------- #
# app-server client (JSON-RPC over stdio)
# --------------------------------------------------------------------------- #
class AppServer:
    """Minimal client for `codex app-server --listen stdio://`."""

    def __init__(self, exe: Path, cwd: str | None = None, verbose: bool = False):
        self.verbose = verbose
        self.proc = subprocess.Popen(
            [str(exe), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd or str(Path.home()),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._q: "queue.Queue[str]" = queue.Queue()
        self._err: list[str] = []
        self._id = 0
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_out(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._q.put(line)

    def _pump_err(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._err.append(line.rstrip())

    # -- transport ---------------------------------------------------------- #
    def _send(self, method: str, params, notify: bool = False):
        frame = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        if not notify:
            self._id += 1
            frame["id"] = self._id
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return frame.get("id")

    def _wait(self, req_id: int, timeout: float):
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == req_id and ("result" in msg or "error" in msg):
                return msg
            if self.verbose and "method" in msg:
                print(f"  [event] {msg['method']}", file=sys.stderr)
        return None

    # -- api ---------------------------------------------------------------- #
    def initialize(self, timeout: float = 20.0) -> None:
        rid = self._send(
            "initialize",
            {
                "clientInfo": {"name": "codex-provider-fork", "version": "1.0.0", "title": "fork"},
                "capabilities": {"experimentalApi": True},
            },
        )
        msg = self._wait(rid, timeout)
        if not msg or "error" in msg:
            raise SystemExit(f"app-server initialize failed: {msg}")
        self._send("initialized", None, notify=True)

    def call(self, method: str, params: dict | None, timeout: float):
        rid = self._send(method, params)
        msg = self._wait(rid, timeout)
        if msg is None:
            raise SystemExit(f"no response to {method} within {timeout:.0f}s")
        if "error" in msg:
            err = msg["error"]
            detail = err.get("message") if isinstance(err, dict) else str(err)
            raise SystemExit(f"{method} failed: {detail}")
        return msg.get("result")

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()

    def stderr_tail(self, n: int = 6) -> list[str]:
        return [line for line in self._err[-n:] if line.strip()]


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def fmt_time(epoch: int | None) -> str:
    if not epoch:
        return "?"
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def cmd_list(args) -> int:
    home = codex_home()
    con = open_state_ro(find_state_db(home))
    sql = "SELECT * FROM threads WHERE 1=1"
    params: list = []
    if args.provider:
        sql += " AND model_provider = ?"
        params.append(args.provider)
    if args.search:
        sql += " AND (COALESCE(name,'') LIKE ? OR COALESCE(title,'') LIKE ?)"
        params += [f"%{args.search}%", f"%{args.search}%"]
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, args.limit))
    rows = con.execute(sql, params).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("no threads matched")
        return 0
    print(f"{'thread id':38}  {'provider':10} {'model':18} {'updated':16} name")
    for r in rows:
        print(
            f"{r['id']:38}  {(r['model_provider'] or '')[:10]:10} "
            f"{(r['model'] or '')[:18]:18} {fmt_time(r['updated_at']):16} "
            f"{(r['name'] or r['title'] or '')[:70]}"
        )
    providers = {}
    for r in rows:
        providers[r["model_provider"]] = providers.get(r["model_provider"], 0) + 1
    print("\nproviders in this page:", ", ".join(f"{k}={v}" for k, v in providers.items()))
    print("config default provider:", default_provider(home) or "<unknown>")
    return 0


def backup_state_db(db: Path, home: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = home / "fork_backups" / stamp
    dst.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db) + suffix)
        if src.is_file():
            shutil.copy2(src, dst / src.name)
    return dst


def cmd_fork(args) -> int:
    home = codex_home()
    db = find_state_db(home)
    con = open_state_ro(db)
    src = resolve_thread(con, args.thread)
    source_id = src["id"]
    source_provider = src["model_provider"]
    source_model = src["model"]

    providers = config_providers(home)
    target = args.provider

    print(f"source thread : {source_id}")
    print(f"  provider    : {source_provider}   model: {source_model}")
    print(f"  name        : {(src['name'] or '')[:80]}")
    print(f"  cwd         : {src['cwd']}")
    print(f"  updated     : {fmt_time(src['updated_at'])}   history: {src['history_mode']}")
    if src["history_mode"] != "paginated":
        print("! note: non-paginated history; fork still works but is less battle-tested here")

    rollout = rollout_of(src)
    print(f"  rollout     : {rollout if rollout else '<missing>'}")

    if source_provider == target and not args.allow_same_provider:
        raise SystemExit(
            f"source already uses provider '{target}'. Pass --allow-same-provider to fork anyway."
        )
    if providers and target not in providers:
        # Built-in providers (openai, ...) are not declared in config.toml, so this
        # is only advisory; a real typo still fails loudly in thread/fork.
        print(f"! note: '{target}' is not declared in {home / 'config.toml'} "
              f"[model_providers] (known there: {', '.join(sorted(providers))}). "
              "Built-in providers need no declaration.")
    env_key = (providers.get(target) or {}).get("env_key")
    if env_key and ":" not in env_key:
        state = env_key_state(env_key)
        print(f"  target key  : {env_key} -> {state}")
        if state.startswith("NOT SET") and not args.force:
            print("! refusing without --force: the copy would authenticate against nothing")

    new_name = args.name
    if new_name is None and not args.no_rename:
        base = (src["name"] or src["title"] or "Codex thread").strip()
        suffix = args.suffix.format(provider=target)
        new_name = f"{base}{suffix}" if base else f"fork of {source_id[:8]}{suffix}"

    model = args.model
    if model is None and target == "openai" and source_model and re.match(r"^(gpt-|o\d|chatgpt)", source_model):
        # config's default model usually belongs to the *other* provider; keep the
        # source model when it is obviously an OpenAI one
        model = source_model
        print(f"  model       : inheriting source model {model} (OpenAI provider target)")

    if args.dry_run:
        print("\ndry run - would call thread/fork with:")
        print(json.dumps({"threadId": source_id, "modelProvider": target,
                          "model": model, "excludeTurns": True}, ensure_ascii=False))
        print(f"would rename to: {new_name!r}")
        return 0

    exe = find_codex_exe(args.codex_exe)
    print(f"\ncodex        : {exe}")

    if not args.no_backup:
        bdir = backup_state_db(db, home)
        print(f"backup       : {bdir}")

    server = AppServer(exe, verbose=args.verbose)
    try:
        server.initialize(timeout=min(args.wait, 25.0))
        params = {
            "threadId": source_id,
            "modelProvider": target,
            "excludeTurns": True,
        }
        if model:
            params["model"] = model
        if args.cwd:
            params["cwd"] = args.cwd

        print(f"forking      : thread/fork -> provider={target}"
              f"{' model=' + model if model else ''} ...")
        t0 = time.time()
        result = server.call("thread/fork", params, timeout=args.wait)
        dt = time.time() - t0
        thread = (result or {}).get("thread") or {}
        new_id = thread.get("id")
        if not new_id:
            raise SystemExit(f"unexpected fork response: {json.dumps(result)[:400]}")
        print(f"forked       : {new_id}  ({dt:.1f}s)")
        print(f"  provider {result.get('modelProvider')} | model {result.get('model')} "
              f"| effort {result.get('reasoningEffort')}")

        renamed = False
        if new_name:
            try:
                server.call("thread/name/set", {"threadId": new_id, "name": new_name},
                            timeout=min(args.wait, 20.0))
                renamed = True
                print(f"renamed      : {new_name}")
            except SystemExit as exc:
                print(f"! rename skipped: {exc}")

        if args.verify:
            ok = verify_thread(server, source_id, new_id, args.wait)
            if not ok:
                print("\n! verification failed; run `verify` again or continue in the app and check")
                return 4
    finally:
        tail = server.stderr_tail(4)
        server.close()
        for line in tail:
            if "unrecognized configuration" in line:
                continue
            print(f"  [codex] {line[:160]}", file=sys.stderr)

    print("\ndone. The original thread is untouched.")
    print(f"  open the copy : codex resume {new_id}")
    print(f"  or in the app : search for {new_name!r}" if renamed else "")
    print("  history is inherited by reference (same as Codex's own fork):"
          " keep the source thread around.")
    return 0


def newest_turn_id(server: AppServer, thread_id: str, timeout: float) -> str | None:
    res = server.call(
        "thread/turns/list",
        {"threadId": thread_id, "limit": 1, "sortDirection": "desc"},
        timeout=timeout,
    )
    data = (res or {}).get("data") or []
    return data[0].get("id") if data else None


def verify_thread(server: AppServer, source_id: str, fork_id: str, timeout: float) -> bool:
    print("verifying    : inherited history ...")
    src_turn = newest_turn_id(server, source_id, timeout)
    fork_turn = newest_turn_id(server, fork_id, timeout)
    print(f"  source newest turn: {src_turn}")
    print(f"  fork   newest turn: {fork_turn}")
    if not fork_turn:
        print("  FAIL: the forked thread returns no turns")
        return False
    if src_turn and fork_turn != src_turn:
        print("  FAIL: the fork's newest turn differs from the source's")
        return False
    print("  OK: the copy sees the source conversation, up to its latest turn")
    return True


def cmd_verify(args) -> int:
    home = codex_home()
    con = open_state_ro(find_state_db(home))
    row = resolve_thread(con, args.thread)
    if not row["rollout_path"]:
        raise SystemExit("thread has no rollout path")
    exe = find_codex_exe(args.codex_exe)
    server = AppServer(exe, verbose=args.verbose)
    try:
        server.initialize()
        print(f"thread   : {row['id']}")
        print(f"provider : {row['model_provider']}   model: {row['model']}")
        parent = None
        rollout = rollout_of(row)
        if rollout and rollout.suffix != ".zst":
            with rollout.open(encoding="utf-8", errors="replace") as fh:
                meta = json.loads(fh.readline() or "{}")
            payload = meta.get("payload") or {}
            parent = payload.get("forked_from_id")
            print(f"  rollout provider : {payload.get('model_provider')}")
            print(f"  forked_from      : {parent}"
                  f" @ ordinal {payload.get('forked_from_ordinal_exclusive')}")
            print(f"  history_base     : {json.dumps(payload.get('history_base'))}")
        turn = newest_turn_id(server, row["id"], args.wait)
        print(f"newest turn      : {turn}")
        print("OK" if turn else "FAIL: no turns readable")
        return 0 if turn else 4
    finally:
        server.close()


def cmd_undo(args) -> int:
    home = codex_home()
    con = open_state_ro(find_state_db(home))
    row = resolve_thread(con, args.thread)
    rollout = rollout_of(row)
    parent = None
    if rollout and rollout.suffix != ".zst":
        with rollout.open(encoding="utf-8", errors="replace") as fh:
            meta = json.loads(fh.readline() or "{}")
        parent = (meta.get("payload") or {}).get("forked_from_id")
    if not parent and not args.force:
        raise SystemExit(
            "that thread is not a fork (no forked_from_id); refusing to delete. Use --force."
        )
    if not args.yes:
        print(f"would delete thread {row['id']} (fork of {parent}) and its rollout {rollout}")
        print("re-run with --yes to actually delete")
        return 0
    exe = find_codex_exe(args.codex_exe)
    server = AppServer(exe, verbose=args.verbose)
    try:
        server.initialize()
        server.call("thread/delete", {"threadId": row["id"]}, timeout=args.wait)
        print(f"deleted {row['id']}")
        return 0
    finally:
        server.close()


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex_fork_provider.py",
        description="Copy a Codex conversation into a fork that uses a different model provider.",
    )
    p.add_argument("--codex-exe", help="path to codex(.exe); auto-detected by default")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list threads with their provider/model")
    pl.add_argument("--provider")
    pl.add_argument("--search", help="substring of the thread name/title")
    pl.add_argument("--limit", type=int, default=20)
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)

    pf = sub.add_parser("fork", help="fork a thread onto another provider")
    pf.add_argument("--thread", required=True, help="thread id, id prefix, or title fragment")
    pf.add_argument("--provider", required=True, help="target model_provider (e.g. sharellm, openai)")
    pf.add_argument("--model", help="model id for the copy; default = provider's configured model")
    pf.add_argument("--name", help="explicit name for the copy")
    pf.add_argument("--suffix", default=" · {provider} copy",
                    help="name suffix when --name is absent (default: '%(default)s')")
    pf.add_argument("--no-rename", action="store_true", help="keep the inherited name")
    pf.add_argument("--cwd", help="override the working directory of the copy")
    pf.add_argument("--wait", type=float, default=DEFAULT_PROTOCOL_WAIT,
                    help="seconds to wait for each RPC (default: %(default)s)")
    pf.add_argument("--no-backup", action="store_true", help="skip the state db backup")
    pf.add_argument("--no-verify", dest="verify", action="store_false",
                    help="skip the inherited-history check")
    pf.add_argument("--dry-run", action="store_true")
    pf.add_argument("--force", action="store_true", help="ignore the missing-API-key guard")
    pf.add_argument("--allow-same-provider", action="store_true")
    pf.set_defaults(func=cmd_fork)

    pv = sub.add_parser("verify", help="inspect a thread and prove its history is readable")
    pv.add_argument("--thread", required=True)
    pv.add_argument("--wait", type=float, default=DEFAULT_PROTOCOL_WAIT)
    pv.set_defaults(func=cmd_verify)

    pu = sub.add_parser("undo", help="delete a fork created by this tool")
    pu.add_argument("--thread", required=True)
    pu.add_argument("--yes", action="store_true")
    pu.add_argument("--force", action="store_true", help="delete even if it is not a fork")
    pu.add_argument("--wait", type=float, default=DEFAULT_PROTOCOL_WAIT)
    pu.set_defaults(func=cmd_undo)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
