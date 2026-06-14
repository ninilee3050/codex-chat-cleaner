from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk


def sqlite_sort_key(path: Path) -> tuple[int, float]:
    match = re.search(r"_(\d+)\.sqlite$", path.name)
    version = int(match.group(1)) if match else -1
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (version, mtime)


def find_latest_sqlite(pattern: str, fallback_name: str) -> Path:
    candidates = [path for path in CODEX_HOME.glob(pattern) if path.is_file()]
    if candidates:
        return max(candidates, key=sqlite_sort_key)
    return CODEX_HOME / fallback_name


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
SESSIONS_ROOT = CODEX_HOME / "sessions"
GENERATED_IMAGES_ROOT = CODEX_HOME / "generated_images"
WORKSPACES_ROOT = Path.home() / "Documents" / "Codex"
STATE_DB = find_latest_sqlite("state_*.sqlite", "state_5.sqlite")
LOGS_DB = find_latest_sqlite("logs_*.sqlite", "logs_2.sqlite")
GOALS_DB = find_latest_sqlite("goals_*.sqlite", "goals_1.sqlite")
MEMORIES_DB = find_latest_sqlite("memories_*.sqlite", "memories_1.sqlite")
SESSION_INDEX = CODEX_HOME / "session_index.jsonl"
GLOBAL_STATE = CODEX_HOME / ".codex-global-state.json"
GLOBAL_STATE_FILES = (GLOBAL_STATE, GLOBAL_STATE.with_name(".codex-global-state.json.bak"))
MANUAL_PROTECTION_FILE = CODEX_HOME / "chat_cleaner_protected_threads.json"
INTERNAL_REVIEW_PREFIX = "The following is the Codex agent history"
THREAD_ID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_MIN_COLUMNS = 4
IMAGE_CARD_MIN_WIDTH = 220
COMPACT_MIN_RECLAIM_BYTES = 50 * 1024 * 1024
COMPACT_MIN_RECLAIM_RATIO = 0.25

COLORS = {
    "bg": "#171717",
    "sidebar": "#202020",
    "panel": "#171717",
    "row": "#1f1f1f",
    "row_hover": "#282828",
    "border": "#303030",
    "field": "#242424",
    "button": "#2b2b2b",
    "button_hover": "#343434",
    "danger": "#5a2d2d",
    "danger_hover": "#6a3535",
    "text": "#ededed",
    "muted": "#a3a3a3",
    "subtle": "#737373",
    "accent": "#8ab4f8",
}

FONT = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 9, "bold")
FONT_TITLE = ("Segoe UI", 10, "bold")


@dataclass(frozen=True)
class ThreadRow:
    thread_id: str
    title: str
    first_user_message: str
    updated_at: int
    source: str
    provider: str
    archived: int
    rollout_path: Path
    cwd: Path | None


@dataclass(frozen=True)
class SessionIndexEntry:
    thread_id: str
    title: str
    updated_at: int


@dataclass(frozen=True)
class GeneratedImage:
    path: Path
    size: int
    updated_at: float


def fmt_time(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_file_time(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def ensure_under(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    return resolved


def connect_db(path: Path, write: bool = False) -> sqlite3.Connection:
    if write:
        return sqlite3.connect(path, timeout=20)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)


def connect_state(write: bool = False) -> sqlite3.Connection:
    return connect_db(STATE_DB, write=write)


def table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    row = cur.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (table,),
    ).fetchone()
    return row is not None


def column_exists(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    if not table_exists(cur, table):
        return False
    return any(row[1] == column for row in cur.execute(f'pragma table_info("{table}")'))


def delete_by_ids(
    db_path: Path,
    table: str,
    column: str,
    target_ids: set[str],
    counts: dict[str, int],
    count_key: str,
) -> None:
    if not target_ids or not db_path.exists():
        return
    with connect_db(db_path, write=True) as con:
        cur = con.cursor()
        if not table_exists(cur, table):
            counts["skipped_tables"] = counts.get("skipped_tables", 0) + 1
            return
        if not column_exists(cur, table, column):
            counts["skipped_columns"] = counts.get("skipped_columns", 0) + 1
            return
        ids = sorted(target_ids)
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            cur.execute(
                f'delete from "{table}" where "{column}" in ({placeholders})',
                tuple(chunk),
            )
            counts[count_key] = counts.get(count_key, 0) + cur.rowcount
        con.commit()


def fetch_thread_ids() -> set[str]:
    if not STATE_DB.exists():
        return set()
    with connect_state(write=False) as con:
        cur = con.cursor()
        if not table_exists(cur, "threads") or not column_exists(cur, "threads", "id"):
            return set()
        return {row[0] for row in cur.execute("select id from threads").fetchall()}


def parse_iso_timestamp(value: str) -> int:
    if not value:
        return 0
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    match = re.match(r"(.+\.)(\d{6})\d+([+-]\d\d:\d\d)$", text)
    if match:
        text = "".join(match.groups())
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def read_session_index_entries() -> list[SessionIndexEntry]:
    if not SESSION_INDEX.exists():
        return []
    entries: list[SessionIndexEntry] = []
    for line in SESSION_INDEX.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread_id = item.get("id")
        if not isinstance(thread_id, str) or not THREAD_ID_RE.fullmatch(thread_id):
            continue
        title = item.get("thread_name") or item.get("title") or ""
        updated_at = parse_iso_timestamp(item.get("updated_at") or "")
        entries.append(
            SessionIndexEntry(
                thread_id=thread_id,
                title=title if isinstance(title, str) else "",
                updated_at=updated_at,
            )
        )
    return entries


def rollout_files_by_thread_id() -> dict[str, Path]:
    if not SESSIONS_ROOT.exists():
        return {}
    files: dict[str, Path] = {}
    for path in SESSIONS_ROOT.rglob("rollout-*.jsonl"):
        match = THREAD_ID_RE.search(path.name)
        if match is None:
            continue
        thread_id = match.group(0)
        previous = files.get(thread_id)
        if previous is None or path.stat().st_mtime > previous.stat().st_mtime:
            files[thread_id] = path
    return files


def read_rollout_summary(path: Path) -> dict[str, object]:
    summary: dict[str, object] = {
        "cwd": None,
        "source": "",
        "provider": "",
        "first_user_message": "",
        "timestamp": 0,
    }
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return summary

    for line in lines[:300]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue

        if item.get("type") == "session_meta":
            cwd = payload.get("cwd")
            if isinstance(cwd, str):
                summary["cwd"] = parse_windows_path(cwd)
            source = payload.get("source")
            if isinstance(source, str):
                summary["source"] = source
            provider = payload.get("model_provider")
            if isinstance(provider, str):
                summary["provider"] = provider
            timestamp = payload.get("timestamp")
            if isinstance(timestamp, str):
                summary["timestamp"] = parse_iso_timestamp(timestamp)

        if not summary["first_user_message"]:
            message = rollout_user_message(item, payload)
            if message and not message.startswith("<environment_context>"):
                summary["first_user_message"] = message

        if summary["cwd"] is not None and summary["first_user_message"]:
            break

    if not summary["timestamp"]:
        try:
            summary["timestamp"] = int(path.stat().st_mtime)
        except OSError:
            pass
    return summary


def rollout_user_message(item: dict, payload: dict) -> str:
    if item.get("type") == "event_msg" and payload.get("type") == "user_message":
        message = payload.get("message")
        return message if isinstance(message, str) else ""

    if item.get("type") != "response_item":
        return ""
    if payload.get("type") != "message" or payload.get("role") != "user":
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "input_text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def thread_row_from_rollout(entry: SessionIndexEntry, path: Path) -> ThreadRow:
    summary = read_rollout_summary(path)
    first_user_message = str(summary.get("first_user_message") or "")
    title = entry.title or first_user_message.splitlines()[0][:80]
    updated_at = entry.updated_at or int(summary.get("timestamp") or 0)
    return ThreadRow(
        thread_id=entry.thread_id,
        title=title,
        first_user_message=first_user_message,
        updated_at=updated_at,
        source=str(summary.get("source") or "unknown"),
        provider=str(summary.get("provider") or "openai"),
        archived=0,
        rollout_path=path,
        cwd=summary.get("cwd") if isinstance(summary.get("cwd"), Path) else None,
    )


def fetch_state_threads() -> list[ThreadRow]:
    if not STATE_DB.exists():
        return []
    with connect_state(write=False) as con:
        cur = con.cursor()
        if not table_exists(cur, "threads"):
            return []
        rows = cur.execute(
            """
            select id, title, first_user_message, updated_at, source, model_provider, archived, rollout_path, cwd
            from threads
            order by updated_at desc
            """
        ).fetchall()
    return [
        ThreadRow(
            thread_id=row[0],
            title=row[1] or "",
            first_user_message=row[2] or "",
            updated_at=row[3] or 0,
            source=row[4] or "",
            provider=row[5] or "",
            archived=row[6] or 0,
            rollout_path=Path(row[7] or ""),
            cwd=parse_windows_path(row[8] or ""),
        )
        for row in rows
    ]


def fetch_threads() -> list[ThreadRow]:
    rows = fetch_state_threads()
    by_id = {row.thread_id: row for row in rows}
    rollout_files = rollout_files_by_thread_id()

    for entry in read_session_index_entries():
        if entry.thread_id in by_id:
            continue
        rollout_path = rollout_files.get(entry.thread_id)
        if rollout_path is None:
            continue
        by_id[entry.thread_id] = thread_row_from_rollout(entry, rollout_path)

    return sorted(by_id.values(), key=lambda row: row.updated_at, reverse=True)


def fetch_generated_images() -> list[GeneratedImage]:
    if not GENERATED_IMAGES_ROOT.exists():
        return []
    images: list[GeneratedImage] = []
    for path in GENERATED_IMAGES_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        images.append(GeneratedImage(path=path, size=stat.st_size, updated_at=stat.st_mtime))
    return sorted(images, key=lambda item: item.updated_at, reverse=True)


def delete_generated_images(images: list[GeneratedImage]) -> dict[str, int]:
    deleted_files = 0
    folders_to_check: set[Path] = set()

    for image in images:
        safe_path = ensure_under(image.path, GENERATED_IMAGES_ROOT)
        if not safe_path.exists() or not safe_path.is_file():
            continue
        safe_path.unlink()
        deleted_files += 1
        folders_to_check.add(safe_path.parent)

    deleted_folders = 0
    root = GENERATED_IMAGES_ROOT.resolve()
    for folder in sorted(folders_to_check, key=lambda item: len(item.parts), reverse=True):
        current = folder.resolve()
        while current != root:
            try:
                current.relative_to(root)
            except ValueError:
                break
            if not current.exists() or not current.is_dir():
                current = current.parent
                continue
            if any(current.iterdir()):
                break
            current.rmdir()
            deleted_folders += 1
            current = current.parent

    return {"image_files": deleted_files, "empty_image_dirs": deleted_folders}


def parse_windows_path(raw_path: str) -> Path | None:
    if not raw_path:
        return None
    if raw_path.startswith("\\\\?\\"):
        raw_path = raw_path[4:]
    return Path(raw_path)


def path_from_text(raw_path: str) -> Path:
    return Path(raw_path.replace("/", "\\"))


def extracted_existing_paths(text: str) -> list[Path]:
    candidates: list[Path] = []
    # Stop at quotes, angle brackets, control characters, and common Korean/English whitespace.
    for match in re.finditer(r"[A-Za-z]:[\\/][^\r\n\t\"<>|]+", text):
        raw = match.group(0).strip().rstrip(".,;:)]}")
        # If the path was followed by prose on the same line, trim at double spaces first.
        raw = re.split(r"\s{2,}", raw, maxsplit=1)[0]
        path = path_from_text(raw)
        if path.exists():
            candidates.append(path.parent if path.is_file() else path)
    return candidates


def workspace_candidates(row: ThreadRow) -> list[Path]:
    candidates: list[Path] = []
    if row.cwd is not None:
        candidates.append(row.cwd)
    candidates.extend(extracted_existing_paths(f"{row.first_user_message}\n{row.title}"))

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return deduped


def first_existing_workspace(row: ThreadRow) -> Path | None:
    for path in workspace_candidates(row):
        if path.exists() and path.is_dir():
            return path
    return None


def is_internal_review(row: ThreadRow) -> bool:
    if row.title.startswith(INTERNAL_REVIEW_PREFIX):
        return True
    if row.first_user_message.startswith(INTERNAL_REVIEW_PREFIX):
        return True
    try:
        source = json.loads(row.source)
    except json.JSONDecodeError:
        return '"guardian"' in row.source.lower()
    if not isinstance(source, dict):
        return False
    subagent = source.get("subagent")
    return isinstance(subagent, dict) and subagent.get("other") == "guardian"


def is_related_internal_review(row: ThreadRow, target_ids: set[str]) -> bool:
    if not is_internal_review(row):
        return False
    haystack = f"{row.title}\n{row.first_user_message}"
    return any(thread_id in haystack for thread_id in target_ids)


def read_session_index_ids() -> set[str]:
    return {entry.thread_id for entry in read_session_index_entries()}


def indexed_thread_ids_with_rollouts() -> set[str]:
    rollout_files = rollout_files_by_thread_id()
    return {
        entry.thread_id
        for entry in read_session_index_entries()
        if entry.thread_id in rollout_files
    }


def filter_session_index(target_ids: set[str]) -> int:
    if not SESSION_INDEX.exists():
        return 0
    lines = SESSION_INDEX.read_text(encoding="utf-8", errors="replace").splitlines()
    kept: list[str] = []
    removed = 0
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if item.get("id") in target_ids:
            removed += 1
        else:
            kept.append(line)
    SESSION_INDEX.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return removed


def json_thread_key_ids(value) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and THREAD_ID_RE.fullmatch(key):
                ids.add(key)
            ids.update(json_thread_key_ids(child))
    elif isinstance(value, list):
        for child in value:
            ids.update(json_thread_key_ids(child))
    return ids


def remove_thread_keys(value, target_ids: set[str]) -> int:
    removed = 0
    if isinstance(value, dict):
        for key in list(value):
            if isinstance(key, str) and key in target_ids:
                del value[key]
                removed += 1
            else:
                removed += remove_thread_keys(value[key], target_ids)
    elif isinstance(value, list):
        for child in value:
            removed += remove_thread_keys(child, target_ids)
    return removed


def global_state_thread_ids() -> set[str]:
    ids: set[str] = set()
    for path in GLOBAL_STATE_FILES:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        projectless_ids = data.get("projectless-thread-ids")
        if isinstance(projectless_ids, list):
            ids.update(item for item in projectless_ids if isinstance(item, str))
        ids.update(json_thread_key_ids(data))
    return ids


def active_global_thread_ids() -> set[str]:
    if not GLOBAL_STATE.exists():
        return set()
    try:
        data = json.loads(GLOBAL_STATE.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return set()
    projectless_ids = data.get("projectless-thread-ids")
    if not isinstance(projectless_ids, list):
        return set()
    return {item for item in projectless_ids if isinstance(item, str)}


def read_manual_protected_thread_ids() -> set[str]:
    if not MANUAL_PROTECTION_FILE.exists():
        return set()
    try:
        data = json.loads(MANUAL_PROTECTION_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return set()

    if isinstance(data, dict):
        raw_ids = data.get("thread_ids")
    else:
        raw_ids = data
    if not isinstance(raw_ids, list):
        return set()
    return {
        item
        for item in raw_ids
        if isinstance(item, str) and THREAD_ID_RE.fullmatch(item)
    }


def write_manual_protected_thread_ids(thread_ids: set[str]) -> None:
    if not thread_ids:
        if MANUAL_PROTECTION_FILE.exists():
            MANUAL_PROTECTION_FILE.unlink()
        return
    payload = {"thread_ids": sorted(thread_ids)}
    MANUAL_PROTECTION_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def prune_manual_protected_thread_ids(existing_ids: set[str]) -> set[str]:
    protected_ids = read_manual_protected_thread_ids()
    kept_ids = protected_ids & existing_ids
    if kept_ids != protected_ids:
        write_manual_protected_thread_ids(kept_ids)
    return kept_ids


def clean_global_state_file(path: Path, target_ids: set[str]) -> int:
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    removed = 0

    projectless_ids = data.get("projectless-thread-ids")
    if isinstance(projectless_ids, list):
        new_ids = [item for item in projectless_ids if item not in target_ids]
        removed += len(projectless_ids) - len(new_ids)
        data["projectless-thread-ids"] = new_ids

    removed += remove_thread_keys(data, target_ids)

    if removed:
        path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    return removed


def clean_global_state(target_ids: set[str]) -> int:
    removed = 0
    for path in GLOBAL_STATE_FILES:
        removed += clean_global_state_file(path, target_ids)
    return removed


def delete_empty_workspace_dirs(rows: list[ThreadRow]) -> dict[str, int]:
    deleted = 0
    skipped_nonempty = 0
    skipped_missing = 0
    seen: set[Path] = set()
    root = WORKSPACES_ROOT.resolve()

    for row in rows:
        if row.cwd is None:
            continue
        try:
            workspace = row.cwd.resolve()
            workspace.relative_to(root)
        except (OSError, ValueError):
            continue
        if workspace in seen:
            continue
        seen.add(workspace)
        if not workspace.exists() or not workspace.is_dir():
            skipped_missing += 1
            continue
        if any(workspace.iterdir()):
            skipped_nonempty += 1
            continue
        workspace.rmdir()
        deleted += 1

    return {
        "empty_workspace_dirs": deleted,
        "nonempty_workspace_dirs": skipped_nonempty,
        "missing_workspace_dirs": skipped_missing,
    }


def delete_empty_session_dirs() -> int:
    if not SESSIONS_ROOT.exists():
        return 0
    deleted = 0
    root = SESSIONS_ROOT.resolve()
    folders = sorted(
        (path for path in SESSIONS_ROOT.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for folder in folders:
        try:
            current = folder.resolve()
            current.relative_to(root)
        except (OSError, ValueError):
            continue
        if current == root:
            continue
        try:
            if any(current.iterdir()):
                continue
            current.rmdir()
            deleted += 1
        except OSError:
            continue
    return deleted


def count_empty_session_dirs() -> int:
    if not SESSIONS_ROOT.exists():
        return 0
    count = 0
    for folder in SESSIONS_ROOT.rglob("*"):
        if not folder.is_dir():
            continue
        try:
            if not any(folder.iterdir()):
                count += 1
        except OSError:
            continue
    return count


def delete_rollout_files(paths: list[Path]) -> int:
    deleted_files = 0
    for path in paths:
        if not path.name or not path.exists():
            continue
        safe_path = ensure_under(path, SESSIONS_ROOT)
        if not safe_path.name.startswith("rollout-"):
            raise RuntimeError(f"Unexpected session filename:\n{safe_path}")
        safe_path.unlink()
        deleted_files += 1
    return deleted_files


def thread_ids_from_db(db_path: Path, table: str, column: str) -> set[str]:
    if not db_path.exists():
        return set()
    with connect_db(db_path, write=False) as con:
        cur = con.cursor()
        if not table_exists(cur, table) or not column_exists(cur, table, column):
            return set()
        return {
            row[0]
            for row in cur.execute(
                f'select distinct "{column}" from "{table}" where "{column}" is not null and "{column}" != ""'
            ).fetchall()
            if isinstance(row[0], str)
        }


def state_rows_with_missing_rollout() -> int:
    if not STATE_DB.exists():
        return 0
    with connect_state(write=False) as con:
        cur = con.cursor()
        if not table_exists(cur, "threads") or not column_exists(cur, "threads", "rollout_path"):
            return 0
        missing = 0
        for (raw_path,) in cur.execute("select rollout_path from threads").fetchall():
            if raw_path and not Path(raw_path).exists():
                missing += 1
        return missing


def orphan_rollout_files(state_ids: set[str]) -> list[Path]:
    if not SESSIONS_ROOT.exists():
        return []
    files: list[Path] = []
    for path in SESSIONS_ROOT.rglob("rollout-*.jsonl"):
        match = THREAD_ID_RE.search(path.name)
        if match is None or match.group(0) not in state_ids:
            files.append(path)
    return files


def automatic_protected_thread_ids(rows: list[ThreadRow]) -> set[str]:
    protected: set[str] = set()
    cutoff = int(time.time()) - 10 * 60
    for row in rows:
        if row.updated_at and row.updated_at >= cutoff:
            protected.add(row.thread_id)
    return protected


def protected_thread_ids(rows: list[ThreadRow]) -> set[str]:
    return automatic_protected_thread_ids(rows) | read_manual_protected_thread_ids()


def inspect_orphans() -> dict[str, object]:
    state_ids = fetch_thread_ids()
    indexed_ids = indexed_thread_ids_with_rollouts()
    known_ids = state_ids | indexed_ids
    protected_ids = active_global_thread_ids() | read_manual_protected_thread_ids()
    logs_ids = thread_ids_from_db(LOGS_DB, "logs", "thread_id")
    goals_ids = thread_ids_from_db(GOALS_DB, "thread_goals", "thread_id")
    memory_ids = thread_ids_from_db(MEMORIES_DB, "stage1_outputs", "thread_id")
    session_index_ids = read_session_index_ids()
    global_ids = global_state_thread_ids()
    orphan_ids = (
        logs_ids
        | goals_ids
        | memory_ids
        | session_index_ids
        | global_ids
    ) - known_ids - protected_ids
    files = orphan_rollout_files(known_ids | protected_ids)
    return {
        "orphan_ids": orphan_ids,
        "orphan_thread_ids": len(orphan_ids),
        "orphan_logs": len(logs_ids - known_ids - protected_ids),
        "orphan_goals": len(goals_ids - known_ids - protected_ids),
        "orphan_memory_outputs": len(memory_ids - known_ids - protected_ids),
        "orphan_session_index": len(session_index_ids - known_ids - protected_ids),
        "orphan_global_state": len(global_ids - known_ids - protected_ids),
        "orphan_rollout_files": len(files),
        "orphan_rollout_paths": files,
        "empty_session_dirs": count_empty_session_dirs(),
        "broken_state_rollouts": state_rows_with_missing_rollout(),
        "protected_thread_ids": len(protected_ids),
    }


def delete_thread_artifacts(target_ids: set[str], counts: dict[str, int]) -> None:
    state_deletions = [
        ("agent_job_items", "assigned_thread_id", "agent_job_items"),
        ("stage1_outputs", "thread_id", "state_stage1_outputs"),
        ("thread_dynamic_tools", "thread_id", "thread_dynamic_tools"),
        ("thread_spawn_edges", "parent_thread_id", "thread_spawn_edges"),
        ("thread_spawn_edges", "child_thread_id", "thread_spawn_edges"),
        ("threads", "id", "threads"),
    ]
    for table, column, count_key in state_deletions:
        delete_by_ids(STATE_DB, table, column, target_ids, counts, count_key)

    delete_by_ids(LOGS_DB, "logs", "thread_id", target_ids, counts, "logs")
    delete_by_ids(GOALS_DB, "thread_goals", "thread_id", target_ids, counts, "goals")
    delete_by_ids(MEMORIES_DB, "stage1_outputs", "thread_id", target_ids, counts, "memory_outputs")

    counts["session_index"] = counts.get("session_index", 0) + filter_session_index(target_ids)
    counts["global_state"] = counts.get("global_state", 0) + clean_global_state(target_ids)


def delete_orphan_artifacts() -> dict[str, int]:
    report = inspect_orphans()
    target_ids = set(report["orphan_ids"])
    counts: dict[str, int] = {}
    delete_thread_artifacts(target_ids, counts)
    counts["orphan_rollout_files"] = delete_rollout_files(list(report["orphan_rollout_paths"]))
    counts["empty_session_dirs"] = delete_empty_session_dirs()
    counts["orphan_thread_ids"] = len(target_ids)
    return counts


def sqlite_reclaimable_bytes(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    try:
        with connect_db(db_path, write=False) as con:
            page_size = con.execute("pragma page_size").fetchone()[0] or 0
            free_pages = con.execute("pragma freelist_count").fetchone()[0] or 0
    except sqlite3.OperationalError:
        return 0
    return int(page_size) * int(free_pages)


def should_compact_db(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    reclaimable = sqlite_reclaimable_bytes(db_path)
    if reclaimable >= COMPACT_MIN_RECLAIM_BYTES:
        return True
    try:
        size = db_path.stat().st_size
    except OSError:
        return False
    if size <= 0:
        return False
    return reclaimable >= 5 * 1024 * 1024 and (reclaimable / size) >= COMPACT_MIN_RECLAIM_RATIO


def compact_sqlite_databases(force: bool = True) -> dict[str, int]:
    counts = {
        "compacted_dbs": 0,
        "compact_failed": 0,
        "compact_skipped_dbs": 0,
        "compact_reclaimable_bytes": 0,
    }
    seen: set[Path] = set()
    for db_path in (STATE_DB, LOGS_DB, GOALS_DB, MEMORIES_DB):
        if db_path in seen or not db_path.exists():
            continue
        seen.add(db_path)
        reclaimable = sqlite_reclaimable_bytes(db_path)
        counts["compact_reclaimable_bytes"] += reclaimable
        if not force and not should_compact_db(db_path):
            counts["compact_skipped_dbs"] += 1
            continue
        try:
            with connect_db(db_path, write=True) as con:
                con.execute("pragma wal_checkpoint(TRUNCATE)")
                con.execute("vacuum")
            counts["compacted_dbs"] += 1
        except sqlite3.OperationalError:
            counts["compact_failed"] += 1
    return counts


def compact_needed_databases() -> dict[str, int]:
    return compact_sqlite_databases(force=False)


def orphan_report_message(report: dict[str, object]) -> str:
    return "\n".join(
        [
            f"남은 thread 흔적: {report.get('orphan_thread_ids', 0)}개",
            f"남은 로그 흔적: {report.get('orphan_logs', 0)}개",
            f"남은 goal 흔적: {report.get('orphan_goals', 0)}개",
            f"남은 memory output 흔적: {report.get('orphan_memory_outputs', 0)}개",
            f"남은 session index 흔적: {report.get('orphan_session_index', 0)}개",
            f"남은 global state 흔적: {report.get('orphan_global_state', 0)}개",
            f"남은 rollout 파일: {report.get('orphan_rollout_files', 0)}개",
            f"빈 sessions 폴더: {report.get('empty_session_dirs', 0)}개",
            f"rollout 파일이 없는 state 세션: {report.get('broken_state_rollouts', 0)}개",
            f"보호 중인 세션: {report.get('protected_thread_ids', 0)}개",
        ]
    )


def remaining_junk_count(report: dict[str, object]) -> int:
    return (
        int(report.get("orphan_thread_ids", 0))
        + int(report.get("orphan_rollout_files", 0))
        + int(report.get("empty_session_dirs", 0))
    )


def add_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        if isinstance(value, int):
            target[key] = target.get(key, 0) + value


def deletion_summary(counts: dict[str, int]) -> str:
    lines = [
        f"삭제한 채팅: {counts.get('threads', 0)}개",
        f"삭제한 세션 파일: {counts.get('session_files', 0)}개",
        f"삭제한 로그: {counts.get('logs', 0)}줄",
        f"삭제한 goals: {counts.get('goals', 0)}개",
        f"삭제한 memory outputs: {counts.get('memory_outputs', 0)}개",
        f"삭제한 session index: {counts.get('session_index', 0)}개",
        f"삭제한 global state 항목: {counts.get('global_state', 0)}개",
        f"삭제한 빈 sessions 폴더: {counts.get('empty_session_dirs', 0)}개",
        f"삭제한 빈 작업 폴더: {counts.get('empty_workspace_dirs', 0)}개",
    ]
    if counts.get("orphan_thread_ids", 0):
        lines.insert(0, f"정리한 남은 thread 흔적: {counts.get('orphan_thread_ids', 0)}개")
    if counts.get("orphan_rollout_files", 0):
        lines.append(f"삭제한 남은 rollout 파일: {counts.get('orphan_rollout_files', 0)}개")
    if counts.get("internal_reviews", 0):
        lines.append(f"함께 삭제한 내부 검토 기록: {counts.get('internal_reviews', 0)}개")
    if counts.get("nonempty_workspace_dirs", 0):
        lines.append(f"파일이 있어 남긴 작업 폴더: {counts.get('nonempty_workspace_dirs', 0)}개")
    if counts.get("skipped_tables", 0):
        lines.append(f"없는 테이블 건너뜀: {counts.get('skipped_tables', 0)}개")
    if counts.get("skipped_columns", 0):
        lines.append(f"없는 컬럼 건너뜀: {counts.get('skipped_columns', 0)}개")
    if counts.get("compact_reclaimable_bytes", 0):
        lines.append(f"용량 줄이기 후보 공간: {fmt_size(counts.get('compact_reclaimable_bytes', 0))}")
    if counts.get("compacted_dbs", 0):
        lines.append(f"용량 줄이기 처리: {counts.get('compacted_dbs', 0)}개 DB")
    elif counts.get("compact_skipped_dbs", 0):
        lines.append("용량 줄이기: 필요할 만큼 큰 빈 공간이 없어 건너뜀")
    if counts.get("compact_failed", 0):
        lines.append("용량 줄이기: DB 사용 중이라 나중에 다시 시도 가능")
    return "\n".join(lines)


def delete_threads(rows: list[ThreadRow]) -> dict[str, int]:
    target_ids = {row.thread_id for row in rows}
    if not target_ids:
        return {}

    counts: dict[str, int] = {}
    delete_thread_artifacts(target_ids, counts)
    counts.update(delete_empty_workspace_dirs(rows))
    counts["session_files"] = delete_rollout_files([row.rollout_path for row in rows])
    counts["empty_session_dirs"] = counts.get("empty_session_dirs", 0) + delete_empty_session_dirs()
    return counts


def smart_cleanup_artifacts(rows_to_delete: list[ThreadRow], internal_reviews: int = 0) -> dict[str, int]:
    counts: dict[str, int] = {}
    if rows_to_delete:
        add_counts(counts, delete_threads(rows_to_delete))
        if internal_reviews:
            counts["internal_reviews"] = internal_reviews
    add_counts(counts, delete_orphan_artifacts())
    add_counts(counts, compact_needed_databases())
    return counts


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Codex 채팅 삭제기")
        self.geometry(self.screen_center_geometry(980, 560))
        self.minsize(860, 440)
        self.configure(bg=COLORS["bg"])

        self.rows: list[ThreadRow] = []
        self.visible_rows: list[ThreadRow] = []
        self.images: list[GeneratedImage] = []
        self.visible_images: list[GeneratedImage] = []
        self.checked_ids: set[str] = set()
        self.checked_image_paths: set[str] = set()
        self.manual_protected_ids: set[str] = read_manual_protected_thread_ids()
        self.check_vars: dict[str, tk.BooleanVar] = {}
        self.protect_vars: dict[str, tk.BooleanVar] = {}
        self.image_check_vars: dict[str, tk.BooleanVar] = {}
        self.thumbnail_refs: list[tk.PhotoImage] = []
        self.search_text = tk.StringVar(value="")
        self.view_mode = "sessions"
        self.image_columns = IMAGE_MIN_COLUMNS
        self.large_preview_window: tk.Toplevel | None = None
        self.last_orphan_report: dict[str, object] | None = None

        self._build_ui()
        self.refresh()

    def screen_center_geometry(self, width: int, height: int) -> str:
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = max((screen_width - width) // 2, 0)
        y = max((screen_height - height) // 2, 0)
        return f"{width}x{height}+{x}+{y}"

    def _build_ui(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        sidebar = tk.Frame(self, bg=COLORS["sidebar"], width=220)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        sidebar.rowconfigure(6, weight=1)

        tk.Label(
            sidebar,
            text="Codex",
            bg=COLORS["sidebar"],
            fg=COLORS["text"],
            font=("Segoe UI", 12, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 2))
        tk.Label(
            sidebar,
            text="채팅 기록 삭제",
            bg=COLORS["sidebar"],
            fg=COLORS["muted"],
            font=FONT,
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 18))

        self.sidebar_total_label, self.sidebar_total = self._sidebar_line(sidebar, "전체 세션", "0", 2)
        self.sidebar_visible_label, self.sidebar_visible = self._sidebar_line(sidebar, "표시 중", "0", 3)
        self.sidebar_checked_label, self.sidebar_checked = self._sidebar_line(sidebar, "체크됨", "0", 4)

        tk.Label(
            sidebar,
            text="내부 검토 기록은 숨김",
            bg=COLORS["sidebar"],
            fg=COLORS["subtle"],
            font=FONT,
            anchor="w",
        ).grid(row=7, column=0, sticky="ew", padx=14, pady=(0, 12))

        main = tk.Frame(self, bg=COLORS["bg"])
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        top = tk.Frame(main, bg=COLORS["bg"])
        top.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        top.columnconfigure(2, weight=1)

        self.session_button = self._button(top, "채팅 세션", self.show_sessions)
        self.session_button.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.image_button = self._button(top, "이미지 관리", self.open_image_manager)
        self.image_button.grid(row=0, column=1, sticky="w", padx=(0, 10))

        search = tk.Entry(
            top,
            textvariable=self.search_text,
            bg=COLORS["field"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            bd=0,
            font=FONT,
        )
        search.grid(row=0, column=2, sticky="ew", ipady=7)
        search.bind("<KeyRelease>", lambda _event: self.apply_filter())

        self._button(top, "새로고침", self.refresh).grid(row=0, column=3, padx=(8, 0))

        self.header = tk.Frame(main, bg=COLORS["bg"])
        self.header.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 4))
        self.render_header(["", "보호", "수정일", "제목", "출처", "모델", "폴더"])

        body = tk.Frame(main, bg=COLORS["bg"])
        body.grid(row=2, column=0, sticky="nsew", padx=(18, 10), pady=(0, 0))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(body, bg=COLORS["bg"], highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.configure(yscrollcommand=self._sync_scroll_state)

        self.list_frame = tk.Frame(self.canvas, bg=COLORS["bg"])
        self.list_frame.columnconfigure(0, weight=1)
        self.list_window = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind("<Configure>", self._resize_list)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        bottom = tk.Frame(main, bg=COLORS["bg"])
        bottom.grid(row=3, column=0, sticky="ew", padx=18, pady=(10, 14))
        bottom.columnconfigure(0, weight=1)
        self.orphan_scan_button = self._button(bottom, "남은 찌꺼기 확인", self.scan_orphans)
        self.orphan_scan_button.grid(row=0, column=1, padx=(0, 6))
        self.compact_button = self._button(bottom, "용량 줄이기", self.compact_databases)
        self.compact_button.grid(row=0, column=2, padx=(0, 10))
        self.check_all_button = self._button(bottom, "보이는 항목 모두 체크", self.check_visible)
        self.check_all_button.grid(row=0, column=3, padx=6)
        self.clear_button = self._button(bottom, "체크 해제", self.clear_checks)
        self.clear_button.grid(row=0, column=4, padx=(0, 6))
        self.delete_button = self._button(bottom, "스마트 정리", self.delete_checked, danger=True)
        self.delete_button.grid(row=0, column=5)

    def _sidebar_line(
        self, parent: tk.Frame, label: str, value: str, row: int
    ) -> tuple[tk.Label, tk.Label]:
        frame = tk.Frame(parent, bg=COLORS["sidebar"])
        frame.grid(row=row, column=0, sticky="ew", padx=14, pady=3)
        frame.columnconfigure(0, weight=1)
        label_widget = tk.Label(
            frame,
            text=label,
            bg=COLORS["sidebar"],
            fg=COLORS["muted"],
            font=FONT,
            anchor="w",
        )
        label_widget.grid(row=0, column=0, sticky="ew")
        value_label = tk.Label(
            frame,
            text=value,
            bg=COLORS["sidebar"],
            fg=COLORS["text"],
            font=FONT_BOLD,
            anchor="e",
        )
        value_label.grid(row=0, column=1, sticky="e")
        return label_widget, value_label

    def _button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        danger: bool = False,
    ) -> tk.Button:
        bg = COLORS["danger"] if danger else COLORS["button"]
        hover = COLORS["danger_hover"] if danger else COLORS["button_hover"]
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=COLORS["text"],
            activebackground=hover,
            activeforeground=COLORS["text"],
            relief="flat",
            bd=0,
            padx=10,
            pady=6,
            font=FONT,
            cursor="hand2",
        )
        button.bind("<Enter>", lambda _event: button.configure(bg=hover))
        button.bind("<Leave>", lambda _event: button.configure(bg=bg))
        return button

    def _configure_row_grid(self, frame: tk.Widget) -> None:
        for column in range(8):
            frame.columnconfigure(column, weight=0, minsize=0, uniform="")
        frame.columnconfigure(0, minsize=42)
        frame.columnconfigure(1, minsize=68)
        frame.columnconfigure(2, minsize=128)
        frame.columnconfigure(3, weight=1)
        frame.columnconfigure(4, minsize=72)
        frame.columnconfigure(5, minsize=72)
        frame.columnconfigure(6, minsize=64)

    def _configure_image_grid(self, frame: tk.Widget) -> None:
        for column in range(8):
            frame.columnconfigure(column, weight=0, minsize=0, uniform="")
        frame.columnconfigure(0, minsize=42)
        frame.columnconfigure(1, minsize=150)
        frame.columnconfigure(2, weight=1)
        frame.columnconfigure(3, minsize=120)
        frame.columnconfigure(4, minsize=110)
        frame.columnconfigure(5, minsize=84)

    def render_header(self, labels: list[str]) -> None:
        for child in self.header.winfo_children():
            child.destroy()
        if self.view_mode == "images":
            for column in range(8):
                self.header.columnconfigure(column, weight=0, minsize=0, uniform="")
            self.header.columnconfigure(0, weight=1)
        else:
            self._configure_row_grid(self.header)
        for column, text in enumerate(labels):
            tk.Label(
                self.header,
                text=text,
                bg=COLORS["bg"],
                fg=COLORS["muted"],
                font=FONT_BOLD,
                anchor="w",
            ).grid(row=0, column=column, sticky="ew", padx=(0, 8))

    def clear_list(self) -> None:
        self.check_vars.clear()
        self.protect_vars.clear()
        self.image_check_vars.clear()
        self.thumbnail_refs.clear()
        for child in self.list_frame.winfo_children():
            child.destroy()
        for column in range(16):
            self.list_frame.columnconfigure(column, weight=0, minsize=0, uniform="")
        self.list_frame.columnconfigure(0, weight=1)

    def update_view_controls(self) -> None:
        if self.view_mode == "images":
            self.sidebar_total_label.configure(text="전체 이미지")
            self.sidebar_visible_label.configure(text="표시 중")
            self.sidebar_checked_label.configure(text="체크됨")
            self.orphan_scan_button.configure(state="disabled")
            self.compact_button.configure(state="disabled")
            self.check_all_button.configure(text="보이는 이미지 모두 체크")
            self.delete_button.configure(text="선택 이미지 삭제")
            self.render_header(["이미지 미리보기"])
        else:
            self.sidebar_total_label.configure(text="전체 세션")
            self.sidebar_visible_label.configure(text="표시 중")
            self.sidebar_checked_label.configure(text="체크됨")
            self.orphan_scan_button.configure(state="normal")
            self.compact_button.configure(state="normal")
            self.check_all_button.configure(text="보이는 항목 모두 체크")
            self.delete_button.configure(text="스마트 정리")
            self.render_header(["", "보호", "수정일", "제목", "출처", "모델", "폴더"])

    def _resize_list(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.list_window, width=event.width)
        if self.view_mode == "images":
            columns = self.gallery_columns(event.width)
            if columns != self.image_columns:
                self.image_columns = columns
                self.render_image_gallery()
        self._update_scroll_enabled()

    def gallery_columns(self, width: int | None = None) -> int:
        available_width = width or self.canvas.winfo_width()
        return max(IMAGE_MIN_COLUMNS, available_width // IMAGE_CARD_MIN_WIDTH)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if not getattr(self, "scroll_enabled", False):
            return
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _sync_scroll_state(self, first: str, last: str) -> None:
        self.scroll_enabled = float(first) > 0.0 or float(last) < 1.0

    def _update_scroll_enabled(self) -> None:
        bbox = self.canvas.bbox("all")
        if not bbox:
            self.scroll_enabled = False
            return
        content_height = bbox[3] - bbox[1]
        self.scroll_enabled = content_height > self.canvas.winfo_height()

    def refresh(self) -> None:
        if self.view_mode == "images":
            try:
                self.images = fetch_generated_images()
                existing = {self.image_key(image) for image in self.images}
                self.checked_image_paths &= existing
            except Exception as exc:
                messagebox.showerror("이미지 불러오기 실패", str(exc))
                self.images = []
                self.checked_image_paths.clear()
        else:
            try:
                self.rows = fetch_threads()
                existing_ids = {row.thread_id for row in self.rows}
                self.manual_protected_ids = prune_manual_protected_thread_ids(existing_ids)
                self.checked_ids &= existing_ids - protected_thread_ids(self.rows)
            except Exception as exc:
                messagebox.showerror("불러오기 실패", str(exc))
                self.rows = []
                self.checked_ids.clear()
        self.apply_filter()

    def apply_filter(self) -> None:
        self.update_view_controls()
        if self.view_mode == "images":
            self.apply_image_filter()
            return

        needle = self.search_text.get().strip().lower()
        self.visible_rows = []
        for row in self.rows:
            if is_internal_review(row):
                continue
            haystack = f"{row.title} {row.rollout_path}".lower()
            if needle and needle not in haystack:
                continue
            self.visible_rows.append(row)

        self.clear_list()

        if not self.visible_rows:
            tk.Label(
                self.list_frame,
                text="표시할 채팅이 없습니다.",
                bg=COLORS["bg"],
                fg=COLORS["muted"],
                font=FONT,
                anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=10, pady=14)
            self.update_status()
            return

        automatic_ids = automatic_protected_thread_ids(self.rows)
        protected_ids = protected_thread_ids(self.rows)
        for idx, row in enumerate(self.visible_rows):
            is_manual_protected = row.thread_id in self.manual_protected_ids
            is_auto_protected = row.thread_id in automatic_ids
            is_protected = row.thread_id in protected_ids
            item = tk.Frame(self.list_frame, bg=COLORS["row"], bd=0, highlightthickness=1)
            item.configure(highlightbackground=COLORS["border"], highlightcolor=COLORS["border"])
            item.grid(row=idx, column=0, sticky="ew", pady=(0, 5))
            self._configure_row_grid(item)
            title = row.title.replace("\r", " ").replace("\n", " ")[:120]
            if is_manual_protected:
                title = f"{title} [보호]"
            elif is_auto_protected:
                title = f"{title} [자동보호]"

            var = tk.BooleanVar(value=row.thread_id in self.checked_ids)
            self.check_vars[row.thread_id] = var
            check = tk.Checkbutton(
                item,
                variable=var,
                command=lambda thread_id=row.thread_id, check_var=var: self.set_checked(
                    thread_id, check_var.get()
                ),
                bg=COLORS["row"],
                activebackground=COLORS["row_hover"],
                selectcolor=COLORS["field"],
                fg=COLORS["text"],
                relief="flat",
                bd=0,
                cursor="hand2",
            )
            if is_protected:
                check.configure(state="disabled")
            check.grid(row=0, column=0, sticky="w", padx=(8, 0), pady=7)

            protect_var = tk.BooleanVar(value=is_manual_protected)
            self.protect_vars[row.thread_id] = protect_var
            protect_check = tk.Checkbutton(
                item,
                text="보호",
                variable=protect_var,
                command=lambda thread_id=row.thread_id, protect=protect_var: self.set_manual_protected(
                    thread_id, protect.get()
                ),
                bg=COLORS["row"],
                activebackground=COLORS["row_hover"],
                selectcolor=COLORS["field"],
                fg=COLORS["muted"],
                activeforeground=COLORS["text"],
                relief="flat",
                bd=0,
                cursor="hand2",
                font=FONT,
            )
            protect_check.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=7)

            tk.Label(
                item,
                text=fmt_time(row.updated_at),
                bg=COLORS["row"],
                fg=COLORS["muted"],
                font=FONT,
                anchor="w",
            ).grid(row=0, column=2, sticky="ew", padx=(0, 8))
            title_label = tk.Label(
                item,
                text=title,
                bg=COLORS["row"],
                fg=COLORS["accent"] if is_protected else COLORS["text"],
                font=FONT,
                anchor="w",
            )
            title_label.grid(row=0, column=3, sticky="ew", padx=(0, 8))
            tk.Label(
                item,
                text=row.source,
                bg=COLORS["row"],
                fg=COLORS["muted"],
                font=FONT,
                anchor="w",
            ).grid(row=0, column=4, sticky="ew", padx=(0, 8))
            tk.Label(
                item,
                text=row.provider,
                bg=COLORS["row"],
                fg=COLORS["muted"],
                font=FONT,
                anchor="w",
            ).grid(row=0, column=5, sticky="ew", padx=(0, 8))
            open_button = self._button(
                item,
                "열기",
                lambda thread_row=row: self.open_workspace_dir(thread_row),
            )
            open_button.grid(row=0, column=6, sticky="w", padx=(0, 8), pady=5)
            if first_existing_workspace(row) is None:
                open_button.configure(state="disabled")

            for widget in (item, title_label):
                if not is_protected:
                    widget.bind(
                        "<Button-1>",
                        lambda _event, thread_id=row.thread_id: self.toggle_checked(thread_id),
                    )
                widget.bind(
                    "<Enter>",
                    lambda _event, row_frame=item: self._set_row_bg(row_frame, COLORS["row_hover"]),
                )
                widget.bind(
                    "<Leave>",
                    lambda _event, row_frame=item: self._set_row_bg(row_frame, COLORS["row"]),
                )
        self.update_status()

    def _set_row_bg(self, row_frame: tk.Frame, color: str) -> None:
        row_frame.configure(bg=color)
        for child in row_frame.winfo_children():
            try:
                child.configure(bg=color, activebackground=color)
            except tk.TclError:
                pass

    def open_workspace_dir(self, row: ThreadRow) -> None:
        primary = first_existing_workspace(row)
        if primary is None:
            messagebox.showinfo("폴더 없음", "이 채팅에 연결된 작업 폴더 정보가 없습니다.")
            return
        os.startfile(primary)

    def show_sessions(self) -> None:
        if self.view_mode == "sessions":
            return
        self.view_mode = "sessions"
        self.search_text.set("")
        self.canvas.yview_moveto(0)
        self.refresh()

    def open_image_manager(self) -> None:
        if self.view_mode == "images":
            return
        self.view_mode = "images"
        self.search_text.set("")
        self.canvas.yview_moveto(0)
        self.refresh()

    def update_status(self) -> None:
        if self.view_mode == "images":
            self.sidebar_total.configure(text=str(len(self.images)))
            self.sidebar_visible.configure(text=str(len(self.visible_images)))
            self.sidebar_checked.configure(text=str(len(self.checked_image_paths)))
            return

        session_total = sum(1 for row in self.rows if not is_internal_review(row))
        self.sidebar_total.configure(text=str(session_total))
        self.sidebar_visible.configure(text=str(len(self.visible_rows)))
        self.sidebar_checked.configure(text=str(len(self.checked_ids)))

    def set_manual_protected(self, thread_id: str, protected: bool) -> None:
        previous_ids = set(self.manual_protected_ids)
        if protected:
            self.manual_protected_ids.add(thread_id)
            self.checked_ids.discard(thread_id)
        else:
            self.manual_protected_ids.discard(thread_id)
        try:
            write_manual_protected_thread_ids(self.manual_protected_ids)
        except Exception as exc:
            self.manual_protected_ids = previous_ids
            messagebox.showerror("보호 저장 실패", str(exc))
        self.apply_filter()

    def set_checked(self, thread_id: str, checked: bool) -> None:
        if checked and thread_id in protected_thread_ids(self.rows):
            return
        if checked:
            self.checked_ids.add(thread_id)
        else:
            self.checked_ids.discard(thread_id)
        self.update_status()

    def toggle_checked(self, thread_id: str) -> None:
        var = self.check_vars.get(thread_id)
        if var is None:
            return
        var.set(not var.get())
        self.set_checked(thread_id, var.get())

    def check_visible(self) -> None:
        if self.view_mode == "images":
            for image in self.visible_images:
                self.checked_image_paths.add(self.image_key(image))
            self.apply_filter()
            return
        protected_ids = protected_thread_ids(self.rows)
        for row in self.visible_rows:
            if row.thread_id not in protected_ids:
                self.checked_ids.add(row.thread_id)
        self.apply_filter()

    def clear_checks(self) -> None:
        if self.view_mode == "images":
            self.checked_image_paths.clear()
            self.apply_filter()
            return
        self.checked_ids.clear()
        self.apply_filter()

    def scan_orphans(self) -> None:
        try:
            self.last_orphan_report = inspect_orphans()
        except Exception as exc:
            messagebox.showerror("남은 찌꺼기 확인 실패", str(exc))
            return
        messagebox.showinfo("남은 찌꺼기 확인", orphan_report_message(self.last_orphan_report))

    def delete_orphans(self) -> None:
        try:
            report = inspect_orphans()
        except Exception as exc:
            messagebox.showerror("남은 찌꺼기 확인 실패", str(exc))
            return
        self.last_orphan_report = report
        target_ids = set(report["orphan_ids"])
        if (
            not target_ids
            and not report.get("orphan_rollout_files", 0)
            and not report.get("empty_session_dirs", 0)
        ):
            messagebox.showinfo("남은 찌꺼기 없음", "삭제할 남은 찌꺼기가 없습니다.")
            return
        if not self.ask_centered(
            "남은 찌꺼기 삭제 확인",
            (
                f"{orphan_report_message(report)}\n\n"
                "state DB의 threads에 없는 기록만 정리합니다.\n"
                "현재 활성 또는 사용자가 보호한 항목은 보호합니다."
            ),
        ):
            return
        try:
            counts = delete_orphan_artifacts()
        except sqlite3.OperationalError as exc:
            messagebox.showerror("남은 찌꺼기 삭제 실패", f"{exc}\n\nCodex 앱을 닫고 다시 시도해보세요.")
            return
        except Exception as exc:
            messagebox.showerror("남은 찌꺼기 삭제 실패", str(exc))
            return
        messagebox.showinfo("남은 찌꺼기 삭제 완료", deletion_summary(counts))
        self.refresh()

    def compact_databases(self) -> None:
        if not self.ask_centered(
            "용량 줄이기 확인",
            (
                "삭제 후 DB 파일 안에 남은 빈 공간을 정리합니다.\n\n"
                "Codex 앱이 DB를 사용 중이면 실패할 수 있습니다.\n"
                "실패하면 Codex 앱을 닫고 다시 시도하세요."
            ),
            confirm_text="용량 줄이기",
        ):
            return
        counts = compact_sqlite_databases(force=True)
        messagebox.showinfo(
            "용량 줄이기 완료",
            "\n".join(
                [
                    f"처리한 DB: {counts.get('compacted_dbs', 0)}개",
                    f"잠금 등으로 실패한 DB: {counts.get('compact_failed', 0)}개",
                ]
            ),
        )

    def image_key(self, image: GeneratedImage) -> str:
        return str(image.path.resolve()).lower()

    def apply_image_filter(self) -> None:
        needle = self.search_text.get().strip().lower()
        self.visible_images = []
        for image in self.images:
            haystack = f"{image.path.name} {image.path.parent}".lower()
            if needle and needle not in haystack:
                continue
            self.visible_images.append(image)

        self.image_columns = self.gallery_columns()
        self.render_image_gallery()

    def render_image_gallery(self) -> None:
        self.clear_list()
        if not self.visible_images:
            tk.Label(
                self.list_frame,
                text="표시할 이미지가 없습니다.",
                bg=COLORS["bg"],
                fg=COLORS["muted"],
                font=FONT,
                anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=10, pady=14)
            self.update_status()
            return

        for column in range(self.image_columns):
            self.list_frame.columnconfigure(column, weight=1, uniform="image_cards")
        for idx, image in enumerate(self.visible_images):
            self.render_image_card(idx, image)
        self.update_status()
        self._update_scroll_enabled()

    def render_image_card(self, idx: int, image: GeneratedImage) -> None:
        key = self.image_key(image)
        item = tk.Frame(self.list_frame, bg=COLORS["row"], bd=0, highlightthickness=1)
        item.configure(highlightbackground=COLORS["border"], highlightcolor=COLORS["border"])
        item.grid(
            row=idx // self.image_columns,
            column=idx % self.image_columns,
            sticky="nsew",
            padx=(0, 8),
            pady=(0, 8),
        )
        item.columnconfigure(0, weight=1)

        var = tk.BooleanVar(value=key in self.checked_image_paths)
        self.image_check_vars[key] = var
        check = tk.Checkbutton(
            item,
            variable=var,
            command=lambda image_key=key, check_var=var: self.set_image_checked(
                image_key, check_var.get()
            ),
            bg=COLORS["row"],
            activebackground=COLORS["row_hover"],
            selectcolor=COLORS["field"],
            fg=COLORS["text"],
            relief="flat",
            bd=0,
            cursor="hand2",
        )
        check.grid(row=0, column=0, sticky="nw", padx=8, pady=8)

        preview = self.load_thumbnail(image.path)
        preview_frame = tk.Frame(item, width=188, height=132, bg=COLORS["field"])
        preview_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))
        preview_frame.grid_propagate(False)
        preview_widget: tk.Widget
        if preview is not None:
            self.thumbnail_refs.append(preview)
            preview_widget = tk.Label(
                preview_frame, image=preview, bg=COLORS["field"], cursor="hand2"
            )
            preview_widget.place(relx=0.5, rely=0.5, anchor="center")
        else:
            preview_widget = tk.Label(
                preview_frame,
                text="미리보기\n불가",
                bg=COLORS["field"],
                fg=COLORS["muted"],
                font=FONT,
                justify="center",
                cursor="hand2",
            )
            preview_widget.place(relx=0.5, rely=0.5, anchor="center")

        name_label = tk.Label(
            item,
            text=image.path.name,
            bg=COLORS["row"],
            fg=COLORS["text"],
            font=FONT,
            anchor="w",
            wraplength=180,
            justify="left",
        )
        name_label.grid(row=2, column=0, sticky="ew", padx=10)
        tk.Label(
            item,
            text=f"{fmt_file_time(image.updated_at)}  /  {fmt_size(image.size)}",
            bg=COLORS["row"],
            fg=COLORS["muted"],
            font=FONT,
            anchor="w",
        ).grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 8))
        actions = tk.Frame(item, bg=COLORS["row"])
        actions.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 10))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self._button(actions, "크게 보기", lambda image_path=image.path: self.show_large_image(image_path)).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        self._button(actions, "폴더 열기", lambda folder=image.path.parent: os.startfile(folder)).grid(
            row=0, column=1, sticky="ew", padx=(4, 0)
        )

        for widget in (item, name_label, preview_frame, preview_widget):
            widget.bind("<Button-1>", lambda _event, image_key=key: self.toggle_image_checked(image_key))
            widget.bind(
                "<Enter>",
                lambda _event, row_frame=item: self._set_row_bg(row_frame, COLORS["row_hover"]),
            )
            widget.bind(
                "<Leave>",
                lambda _event, row_frame=item: self._set_row_bg(row_frame, COLORS["row"]),
            )

    def load_thumbnail(self, path: Path) -> tk.PhotoImage | None:
        try:
            image = tk.PhotoImage(file=str(path))
            factor = max(1, (image.width() + 187) // 188, (image.height() + 131) // 132)
            return image.subsample(factor, factor)
        except tk.TclError:
            return None

    def load_large_preview(self, path: Path) -> tk.PhotoImage | None:
        try:
            image = tk.PhotoImage(file=str(path))
            max_width = int(self.winfo_screenwidth() * 0.65)
            max_height = int(self.winfo_screenheight() * 0.65)
            factor = max(
                1,
                (image.width() + max_width - 1) // max_width,
                (image.height() + max_height - 1) // max_height,
            )
            return image.subsample(factor, factor)
        except tk.TclError:
            return None

    def show_large_image(self, path: Path) -> None:
        preview = self.load_large_preview(path)
        if preview is None:
            messagebox.showinfo("미리보기 불가", "이 이미지는 크게 볼 수 없습니다.")
            return

        if self.large_preview_window is not None and self.large_preview_window.winfo_exists():
            self.large_preview_window.destroy()

        padding = 16
        width = preview.width() + padding
        height = preview.height() + padding
        window = tk.Toplevel(self)
        self.large_preview_window = window
        window.title("이미지 크게 보기")
        window.geometry(self.center_geometry(width, height))
        window.resizable(False, False)
        window.configure(bg=COLORS["bg"])
        window.transient(self)
        window.large_preview_ref = preview
        window.protocol("WM_DELETE_WINDOW", lambda preview_window=window: self.close_large_preview(preview_window))
        window.bind("<Escape>", lambda _event, preview_window=window: self.close_large_preview(preview_window))

        preview_panel = tk.Frame(window, bg=COLORS["bg"])
        preview_panel.pack(fill="both", expand=True, padx=8, pady=8)
        image_label = tk.Label(preview_panel, image=preview, bg=COLORS["bg"], cursor="hand2")
        image_label.place(
            relx=0.5, rely=0.5, anchor="center"
        )
        image_label.bind("<Button-1>", lambda _event, preview_window=window: self.close_large_preview(preview_window))

    def center_geometry(self, width: int, height: int) -> str:
        self.update_idletasks()
        parent_x = self.winfo_rootx()
        parent_y = self.winfo_rooty()
        parent_width = max(self.winfo_width(), 1)
        parent_height = max(self.winfo_height(), 1)
        x = max(parent_x + (parent_width - width) // 2, 0)
        y = max(parent_y + (parent_height - height) // 2, 0)
        return f"{width}x{height}+{x}+{y}"

    def ask_centered(self, title: str, message: str, confirm_text: str = "삭제") -> bool:
        result = tk.BooleanVar(value=False)
        window = tk.Toplevel(self)
        window.title(title)
        window.geometry(self.center_geometry(520, 320))
        window.minsize(460, 260)
        window.configure(bg=COLORS["bg"])
        window.transient(self)
        window.grab_set()

        tk.Label(
            window,
            text=title,
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=FONT_TITLE,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(16, 8))

        text = tk.Text(
            window,
            bg=COLORS["field"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            bd=0,
            font=FONT,
            wrap="word",
            height=10,
        )
        text.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        text.insert("1.0", message)
        text.configure(state="disabled")

        bottom = tk.Frame(window, bg=COLORS["bg"])
        bottom.pack(fill="x", padx=16, pady=(0, 16))
        bottom.columnconfigure(0, weight=1)

        def confirm() -> None:
            result.set(True)
            window.destroy()

        self._button(bottom, "취소", window.destroy).grid(row=0, column=1, sticky="e", padx=(0, 8))
        self._button(bottom, confirm_text, confirm, danger=True).grid(row=0, column=2, sticky="e")

        window.bind("<Escape>", lambda _event: window.destroy())
        window.bind("<Return>", lambda _event: confirm())
        window.wait_window()
        return result.get()

    def close_large_preview(self, window: tk.Toplevel) -> None:
        if window.winfo_exists():
            window.destroy()
        if self.large_preview_window is window:
            self.large_preview_window = None

    def set_image_checked(self, image_key: str, checked: bool) -> None:
        if checked:
            self.checked_image_paths.add(image_key)
        else:
            self.checked_image_paths.discard(image_key)
        self.update_status()

    def toggle_image_checked(self, image_key: str) -> None:
        var = self.image_check_vars.get(image_key)
        if var is None:
            return
        var.set(not var.get())
        self.set_image_checked(image_key, var.get())

    def checked_images(self) -> list[GeneratedImage]:
        return [
            image
            for image in self.images
            if self.image_key(image) in self.checked_image_paths
        ]

    def delete_checked_images(self) -> None:
        images = self.checked_images()
        if not images:
            messagebox.showinfo("체크 없음", "삭제할 이미지를 먼저 체크하세요.")
            return

        preview = "\n".join(f"- {image.path.name}" for image in images[:8])
        if len(images) > 8:
            preview += f"\n- ... 외 {len(images) - 8}개"
        if not self.ask_centered(
            "이미지 삭제 확인",
            f"체크한 이미지 {len(images)}개를 삭제할까요?\n\n{preview}\n\n이미지가 사라진 빈 하위 폴더도 함께 삭제합니다.",
        ):
            return

        try:
            counts = delete_generated_images(images)
        except Exception as exc:
            messagebox.showerror("삭제 실패", str(exc))
            return

        self.checked_image_paths.difference_update(self.image_key(image) for image in images)
        messagebox.showinfo(
            "삭제 완료",
            "\n".join(
                [
                    f"삭제한 이미지: {counts.get('image_files', 0)}개",
                    f"삭제한 빈 이미지 폴더: {counts.get('empty_image_dirs', 0)}개",
                ]
            ),
        )
        self.refresh()

    def checked_rows(self) -> list[ThreadRow]:
        by_id = {row.thread_id: row for row in self.rows}
        protected_ids = protected_thread_ids(self.rows)
        return [
            by_id[thread_id]
            for thread_id in self.checked_ids
            if thread_id in by_id and thread_id not in protected_ids
        ]

    def related_internal_reviews(self, rows: list[ThreadRow]) -> list[ThreadRow]:
        target_ids = {row.thread_id for row in rows}
        selected_ids = {row.thread_id for row in rows}
        related: list[ThreadRow] = []
        for row in self.rows:
            if row.thread_id in selected_ids:
                continue
            if is_related_internal_review(row, target_ids):
                related.append(row)
        return related

    def delete_checked(self) -> None:
        if self.view_mode == "images":
            self.delete_checked_images()
            return

        rows = self.checked_rows()
        related_rows = self.related_internal_reviews(rows) if rows else []
        protected_ids = protected_thread_ids(self.rows)
        rows_to_delete = [row for row in rows + related_rows if row.thread_id not in protected_ids]

        try:
            report = inspect_orphans()
        except Exception as exc:
            messagebox.showerror("스마트 정리 실패", str(exc))
            return

        junk_count = remaining_junk_count(report)
        if not rows_to_delete and not junk_count:
            messagebox.showinfo(
                "정리할 항목 없음",
                "삭제할 체크 세션이나 남은 찌꺼기가 없습니다.\n\n보호 세션은 그대로 둡니다.",
            )
            return

        preview = "\n".join(f"- {row.title[:80]}" for row in rows_to_delete[:8])
        if len(rows_to_delete) > 8:
            preview += f"\n- ... 외 {len(rows_to_delete) - 8}개"
        related_msg = ""
        if related_rows:
            related_msg = f"\n\n관련 내부 검토 기록 {len(related_rows)}개도 함께 삭제합니다."
        checked_msg = f"\n\n{preview}{related_msg}" if preview else ""
        if not self.ask_centered(
            "스마트 정리 확인",
            (
                f"삭제할 체크 세션: {len(rows_to_delete)}개\n"
                f"정리할 남은 찌꺼기: {junk_count}개\n"
                f"보호된 세션: {report.get('protected_thread_ids', 0)}개"
                f"{checked_msg}\n\n"
                "체크하지 않은 정상 세션은 삭제하지 않습니다.\n"
                "보호 세션은 삭제하지 않습니다."
            ),
            confirm_text="정리",
        ):
            return

        try:
            deleted_ids = {row.thread_id for row in rows_to_delete}
            internal_review_count = sum(1 for row in related_rows if row.thread_id in deleted_ids)
            counts = smart_cleanup_artifacts(rows_to_delete, internal_reviews=internal_review_count)
            self.checked_ids.difference_update(row.thread_id for row in rows_to_delete)
        except sqlite3.OperationalError as exc:
            messagebox.showerror("스마트 정리 실패", f"{exc}\n\nCodex 앱을 닫고 다시 시도해보세요.")
            return
        except Exception as exc:
            messagebox.showerror("스마트 정리 실패", str(exc))
            return

        messagebox.showinfo("스마트 정리 완료", deletion_summary(counts))
        self.refresh()


class ImageManagerWindow(tk.Toplevel):
    def __init__(self, parent: App) -> None:
        super().__init__(parent)
        self.title("Codex 이미지 관리")
        self.geometry("900x620")
        self.minsize(760, 460)
        self.configure(bg=COLORS["bg"])
        self.transient(parent)

        self.images: list[GeneratedImage] = []
        self.checked_paths: set[str] = set()
        self.check_vars: dict[str, tk.BooleanVar] = {}
        self.thumbnail_refs: list[tk.PhotoImage] = []
        self.scroll_enabled = False

        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = tk.Frame(self, bg=COLORS["bg"])
        top.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        top.columnconfigure(1, weight=1)

        tk.Label(
            top,
            text="이미지 관리",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=FONT_TITLE,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.path_label = tk.Label(
            top,
            text=str(GENERATED_IMAGES_ROOT),
            bg=COLORS["bg"],
            fg=COLORS["subtle"],
            font=FONT,
            anchor="w",
        )
        self.path_label.grid(row=0, column=1, sticky="ew")
        self._button(top, "새로고침", self.refresh).grid(row=0, column=2, padx=(8, 0))

        body = tk.Frame(self, bg=COLORS["bg"])
        body.grid(row=1, column=0, sticky="nsew", padx=(18, 10), pady=(0, 0))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(body, bg=COLORS["bg"], highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.configure(yscrollcommand=self._sync_scroll_state)

        self.list_frame = tk.Frame(self.canvas, bg=COLORS["bg"])
        self.list_frame.columnconfigure(0, weight=1)
        self.list_window = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind("<Configure>", self._resize_list)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

        bottom = tk.Frame(self, bg=COLORS["bg"])
        bottom.grid(row=2, column=0, sticky="ew", padx=18, pady=(10, 14))
        bottom.columnconfigure(0, weight=1)
        self.status_label = tk.Label(
            bottom,
            text="이미지 0개 / 체크 0개",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=FONT,
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self._button(bottom, "보이는 이미지 모두 체크", self.check_all).grid(
            row=0, column=1, padx=6
        )
        self._button(bottom, "체크 해제", self.clear_checks).grid(row=0, column=2, padx=(0, 6))
        self._button(bottom, "선택 이미지 삭제", self.delete_checked, danger=True).grid(
            row=0, column=3
        )

    def _button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        danger: bool = False,
    ) -> tk.Button:
        bg = COLORS["danger"] if danger else COLORS["button"]
        hover = COLORS["danger_hover"] if danger else COLORS["button_hover"]
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=COLORS["text"],
            activebackground=hover,
            activeforeground=COLORS["text"],
            relief="flat",
            bd=0,
            padx=10,
            pady=6,
            font=FONT,
            cursor="hand2",
        )
        button.bind("<Enter>", lambda _event: button.configure(bg=hover))
        button.bind("<Leave>", lambda _event: button.configure(bg=bg))
        return button

    def _resize_list(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.list_window, width=event.width)
        self._update_scroll_enabled()

    def _on_mousewheel(self, event: tk.Event) -> None:
        if not self.scroll_enabled:
            return
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _sync_scroll_state(self, first: str, last: str) -> None:
        self.scroll_enabled = float(first) > 0.0 or float(last) < 1.0

    def _update_scroll_enabled(self) -> None:
        bbox = self.canvas.bbox("all")
        if not bbox:
            self.scroll_enabled = False
            return
        content_height = bbox[3] - bbox[1]
        self.scroll_enabled = content_height > self.canvas.winfo_height()

    def refresh(self) -> None:
        try:
            self.images = fetch_generated_images()
        except Exception as exc:
            messagebox.showerror("불러오기 실패", str(exc), parent=self)
            self.images = []
        existing = {str(image.path.resolve()).lower() for image in self.images}
        self.checked_paths &= existing
        self.render_images()

    def render_images(self) -> None:
        self.check_vars.clear()
        self.thumbnail_refs.clear()
        for child in self.list_frame.winfo_children():
            child.destroy()

        if not self.images:
            tk.Label(
                self.list_frame,
                text="표시할 이미지가 없습니다.",
                bg=COLORS["bg"],
                fg=COLORS["muted"],
                font=FONT,
                anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=10, pady=14)
            self.update_status()
            return

        for idx, image in enumerate(self.images):
            self._image_row(idx, image)
        self.update_status()

    def _image_row(self, idx: int, image: GeneratedImage) -> None:
        key = str(image.path.resolve()).lower()
        row = tk.Frame(self.list_frame, bg=COLORS["row"], bd=0, highlightthickness=1)
        row.configure(highlightbackground=COLORS["border"], highlightcolor=COLORS["border"])
        row.grid(row=idx, column=0, sticky="ew", pady=(0, 6))
        row.columnconfigure(2, weight=1)

        var = tk.BooleanVar(value=key in self.checked_paths)
        self.check_vars[key] = var
        tk.Checkbutton(
            row,
            variable=var,
            command=lambda image_key=key, check_var=var: self.set_checked(
                image_key, check_var.get()
            ),
            bg=COLORS["row"],
            activebackground=COLORS["row_hover"],
            selectcolor=COLORS["field"],
            fg=COLORS["text"],
            relief="flat",
            bd=0,
            cursor="hand2",
        ).grid(row=0, column=0, sticky="n", padx=(8, 0), pady=10)

        preview = self._load_thumbnail(image.path)
        preview_frame = tk.Frame(row, width=136, height=96, bg=COLORS["field"])
        preview_frame.grid(row=0, column=1, sticky="w", padx=10, pady=10)
        preview_frame.grid_propagate(False)
        if preview is not None:
            self.thumbnail_refs.append(preview)
            tk.Label(preview_frame, image=preview, bg=COLORS["field"]).place(relx=0.5, rely=0.5, anchor="center")
        else:
            tk.Label(
                preview_frame,
                text="미리보기\n불가",
                bg=COLORS["field"],
                fg=COLORS["muted"],
                font=FONT,
                justify="center",
            ).place(relx=0.5, rely=0.5, anchor="center")

        info = tk.Frame(row, bg=COLORS["row"])
        info.grid(row=0, column=2, sticky="nsew", padx=(0, 10), pady=10)
        info.columnconfigure(0, weight=1)
        tk.Label(
            info,
            text=image.path.name,
            bg=COLORS["row"],
            fg=COLORS["text"],
            font=FONT_BOLD,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        tk.Label(
            info,
            text=f"{fmt_file_time(image.updated_at)}  /  {fmt_size(image.size)}",
            bg=COLORS["row"],
            fg=COLORS["muted"],
            font=FONT,
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        tk.Label(
            info,
            text=str(image.path.parent),
            bg=COLORS["row"],
            fg=COLORS["subtle"],
            font=FONT,
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", pady=(4, 0))

        self._button(row, "폴더 열기", lambda folder=image.path.parent: os.startfile(folder)).grid(
            row=0, column=3, sticky="n", padx=(0, 10), pady=10
        )

        for widget in (row, info):
            widget.bind("<Button-1>", lambda _event, image_key=key: self.toggle_checked(image_key))
            widget.bind("<Enter>", lambda _event, row_frame=row: self._set_row_bg(row_frame, COLORS["row_hover"]))
            widget.bind("<Leave>", lambda _event, row_frame=row: self._set_row_bg(row_frame, COLORS["row"]))

    def _load_thumbnail(self, path: Path) -> tk.PhotoImage | None:
        try:
            image = tk.PhotoImage(file=str(path))
            factor = max(1, (image.width() + 135) // 136, (image.height() + 95) // 96)
            return image.subsample(factor, factor)
        except tk.TclError:
            return None

    def _set_row_bg(self, row_frame: tk.Frame, color: str) -> None:
        row_frame.configure(bg=color)
        for child in row_frame.winfo_children():
            try:
                child.configure(bg=color, activebackground=color)
            except tk.TclError:
                pass
            for grandchild in child.winfo_children():
                try:
                    grandchild.configure(bg=color, activebackground=color)
                except tk.TclError:
                    pass

    def set_checked(self, image_key: str, checked: bool) -> None:
        if checked:
            self.checked_paths.add(image_key)
        else:
            self.checked_paths.discard(image_key)
        self.update_status()

    def toggle_checked(self, image_key: str) -> None:
        var = self.check_vars.get(image_key)
        if var is None:
            return
        var.set(not var.get())
        self.set_checked(image_key, var.get())

    def check_all(self) -> None:
        self.checked_paths = {str(image.path.resolve()).lower() for image in self.images}
        self.render_images()

    def clear_checks(self) -> None:
        self.checked_paths.clear()
        self.render_images()

    def checked_images(self) -> list[GeneratedImage]:
        return [
            image
            for image in self.images
            if str(image.path.resolve()).lower() in self.checked_paths
        ]

    def update_status(self) -> None:
        self.status_label.configure(text=f"이미지 {len(self.images)}개 / 체크 {len(self.checked_paths)}개")

    def delete_checked(self) -> None:
        images = self.checked_images()
        if not images:
            messagebox.showinfo("체크 없음", "삭제할 이미지를 먼저 체크하세요.", parent=self)
            return

        preview = "\n".join(f"- {image.path.name}" for image in images[:8])
        if len(images) > 8:
            preview += f"\n- ... 외 {len(images) - 8}개"
        if not messagebox.askyesno(
            "이미지 삭제 확인",
            f"체크한 이미지 {len(images)}개를 삭제할까요?\n\n{preview}\n\n이미지가 사라진 빈 하위 폴더도 함께 삭제합니다.",
            parent=self,
        ):
            return

        try:
            counts = delete_generated_images(images)
        except Exception as exc:
            messagebox.showerror("삭제 실패", str(exc), parent=self)
            return

        self.checked_paths.difference_update(str(image.path.resolve()).lower() for image in images)
        messagebox.showinfo(
            "삭제 완료",
            "\n".join(
                [
                    f"삭제한 이미지: {counts.get('image_files', 0)}개",
                    f"삭제한 빈 이미지 폴더: {counts.get('empty_image_dirs', 0)}개",
                ]
            ),
            parent=self,
        )
        self.refresh()


if __name__ == "__main__":
    App().mainloop()
