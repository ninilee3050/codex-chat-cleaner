from __future__ import annotations

import json
import os
import re
import sqlite3
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk


CODEX_HOME = Path(r"C:\Users\user\.codex")
SESSIONS_ROOT = CODEX_HOME / "sessions"
WORKSPACES_ROOT = Path(r"C:\Users\user\Documents\Codex")
STATE_DB = CODEX_HOME / "state_5.sqlite"
SESSION_INDEX = CODEX_HOME / "session_index.jsonl"
GLOBAL_STATE = CODEX_HOME / ".codex-global-state.json"
INTERNAL_REVIEW_PREFIX = "The following is the Codex agent history"

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


def fmt_time(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def ensure_under(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    return resolved


def connect_state(write: bool = False) -> sqlite3.Connection:
    if write:
        return sqlite3.connect(STATE_DB, timeout=20)
    return sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)


def fetch_threads() -> list[ThreadRow]:
    if not STATE_DB.exists():
        raise FileNotFoundError(f"State DB not found:\n{STATE_DB}")
    with connect_state(write=False) as con:
        rows = con.execute(
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
            rollout_path=Path(row[7]),
            cwd=parse_windows_path(row[8] or ""),
        )
        for row in rows
    ]


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


def clean_global_state(target_ids: set[str]) -> int:
    if not GLOBAL_STATE.exists():
        return 0
    data = json.loads(GLOBAL_STATE.read_text(encoding="utf-8", errors="replace"))
    removed = 0

    projectless_ids = data.get("projectless-thread-ids")
    if isinstance(projectless_ids, list):
        new_ids = [item for item in projectless_ids if item not in target_ids]
        removed += len(projectless_ids) - len(new_ids)
        data["projectless-thread-ids"] = new_ids

    hints = data.get("thread-workspace-root-hints")
    if isinstance(hints, dict):
        for thread_id in list(hints):
            if thread_id in target_ids:
                removed += 1
                del hints[thread_id]

    GLOBAL_STATE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
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


def delete_threads(rows: list[ThreadRow]) -> dict[str, int]:
    target_ids = {row.thread_id for row in rows}
    if not target_ids:
        return {}

    placeholders = ",".join("?" for _ in target_ids)
    params = tuple(target_ids)
    counts: dict[str, int] = {}

    with connect_state(write=True) as con:
        cur = con.cursor()
        deletions = [
            ("agent_job_items", f"assigned_thread_id in ({placeholders})"),
            ("stage1_outputs", f"thread_id in ({placeholders})"),
            ("thread_dynamic_tools", f"thread_id in ({placeholders})"),
            ("thread_spawn_edges", f"parent_thread_id in ({placeholders})"),
            ("thread_spawn_edges", f"child_thread_id in ({placeholders})"),
            ("threads", f"id in ({placeholders})"),
        ]
        for table, where in deletions:
            cur.execute(f"delete from {table} where {where}", params)
            counts[table] = counts.get(table, 0) + cur.rowcount
        con.commit()

    counts["session_index"] = filter_session_index(target_ids)
    counts["global_state"] = clean_global_state(target_ids)
    counts.update(delete_empty_workspace_dirs(rows))

    deleted_files = 0
    for row in rows:
        if row.rollout_path.exists():
            safe_path = ensure_under(row.rollout_path, SESSIONS_ROOT)
            if not safe_path.name.startswith("rollout-"):
                raise RuntimeError(f"Unexpected session filename:\n{safe_path}")
            safe_path.unlink()
            deleted_files += 1
    counts["session_files"] = deleted_files
    return counts


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Codex 채팅 삭제기")
        self.geometry("980x560")
        self.minsize(860, 440)
        self.configure(bg=COLORS["bg"])

        self.rows: list[ThreadRow] = []
        self.visible_rows: list[ThreadRow] = []
        self.checked_ids: set[str] = set()
        self.check_vars: dict[str, tk.BooleanVar] = {}
        self.search_text = tk.StringVar(value="")

        self._build_ui()
        self.refresh()

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

        self.sidebar_total = self._sidebar_line(sidebar, "전체 세션", "0", 2)
        self.sidebar_visible = self._sidebar_line(sidebar, "표시 중", "0", 3)
        self.sidebar_checked = self._sidebar_line(sidebar, "체크됨", "0", 4)

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
        top.columnconfigure(1, weight=1)

        tk.Label(
            top,
            text="채팅 세션",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=FONT_TITLE,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))

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
        search.grid(row=0, column=1, sticky="ew", ipady=7)
        search.bind("<KeyRelease>", lambda _event: self.apply_filter())

        self._button(top, "새로고침", self.refresh).grid(row=0, column=2, padx=(8, 0))

        header = tk.Frame(main, bg=COLORS["bg"])
        header.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 4))
        self._configure_row_grid(header)
        for column, text in enumerate(["", "수정일", "제목", "출처", "모델", "폴더"]):
            tk.Label(
                header,
                text=text,
                bg=COLORS["bg"],
                fg=COLORS["muted"],
                font=FONT_BOLD,
                anchor="w",
            ).grid(row=0, column=column, sticky="ew", padx=(0, 8))

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
        self._button(bottom, "보이는 항목 모두 체크", self.check_visible).grid(
            row=0, column=1, padx=6
        )
        self._button(bottom, "체크 해제", self.clear_checks).grid(
            row=0, column=2, padx=(0, 6)
        )
        self._button(bottom, "체크한 항목 삭제", self.delete_checked, danger=True).grid(
            row=0, column=3
        )

    def _sidebar_line(self, parent: tk.Frame, label: str, value: str, row: int) -> tk.Label:
        frame = tk.Frame(parent, bg=COLORS["sidebar"])
        frame.grid(row=row, column=0, sticky="ew", padx=14, pady=3)
        frame.columnconfigure(0, weight=1)
        tk.Label(
            frame,
            text=label,
            bg=COLORS["sidebar"],
            fg=COLORS["muted"],
            font=FONT,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        value_label = tk.Label(
            frame,
            text=value,
            bg=COLORS["sidebar"],
            fg=COLORS["text"],
            font=FONT_BOLD,
            anchor="e",
        )
        value_label.grid(row=0, column=1, sticky="e")
        return value_label

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
        frame.columnconfigure(0, minsize=42)
        frame.columnconfigure(1, minsize=128)
        frame.columnconfigure(2, weight=1)
        frame.columnconfigure(3, minsize=72)
        frame.columnconfigure(4, minsize=72)
        frame.columnconfigure(5, minsize=64)

    def _resize_list(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.list_window, width=event.width)
        self._update_scroll_enabled()

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
        try:
            self.rows = fetch_threads()
            existing_ids = {row.thread_id for row in self.rows}
            self.checked_ids &= existing_ids
        except Exception as exc:
            messagebox.showerror("불러오기 실패", str(exc))
            self.rows = []
            self.checked_ids.clear()
        self.apply_filter()

    def apply_filter(self) -> None:
        needle = self.search_text.get().strip().lower()
        self.visible_rows = []
        for row in self.rows:
            if is_internal_review(row):
                continue
            haystack = f"{row.title} {row.rollout_path}".lower()
            if needle and needle not in haystack:
                continue
            self.visible_rows.append(row)

        self.check_vars.clear()
        for child in self.list_frame.winfo_children():
            child.destroy()

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

        for idx, row in enumerate(self.visible_rows):
            item = tk.Frame(self.list_frame, bg=COLORS["row"], bd=0, highlightthickness=1)
            item.configure(highlightbackground=COLORS["border"], highlightcolor=COLORS["border"])
            item.grid(row=idx, column=0, sticky="ew", pady=(0, 5))
            self._configure_row_grid(item)
            title = row.title.replace("\r", " ").replace("\n", " ")[:120]

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
            check.grid(row=0, column=0, sticky="w", padx=(8, 0), pady=7)

            tk.Label(
                item,
                text=fmt_time(row.updated_at),
                bg=COLORS["row"],
                fg=COLORS["muted"],
                font=FONT,
                anchor="w",
            ).grid(row=0, column=1, sticky="ew", padx=(0, 8))
            title_label = tk.Label(
                item,
                text=title,
                bg=COLORS["row"],
                fg=COLORS["text"],
                font=FONT,
                anchor="w",
            )
            title_label.grid(row=0, column=2, sticky="ew", padx=(0, 8))
            tk.Label(
                item,
                text=row.source,
                bg=COLORS["row"],
                fg=COLORS["muted"],
                font=FONT,
                anchor="w",
            ).grid(row=0, column=3, sticky="ew", padx=(0, 8))
            tk.Label(
                item,
                text=row.provider,
                bg=COLORS["row"],
                fg=COLORS["muted"],
                font=FONT,
                anchor="w",
            ).grid(row=0, column=4, sticky="ew", padx=(0, 8))
            open_button = self._button(
                item,
                "열기",
                lambda thread_row=row: self.open_workspace_dir(thread_row),
            )
            open_button.grid(row=0, column=5, sticky="w", padx=(0, 8), pady=5)
            if first_existing_workspace(row) is None:
                open_button.configure(state="disabled")

            for widget in (item, title_label):
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

    def update_status(self) -> None:
        session_total = sum(1 for row in self.rows if not is_internal_review(row))
        self.sidebar_total.configure(text=str(session_total))
        self.sidebar_visible.configure(text=str(len(self.visible_rows)))
        self.sidebar_checked.configure(text=str(len(self.checked_ids)))

    def set_checked(self, thread_id: str, checked: bool) -> None:
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
        for row in self.visible_rows:
            self.checked_ids.add(row.thread_id)
        self.apply_filter()

    def clear_checks(self) -> None:
        self.checked_ids.clear()
        self.apply_filter()

    def checked_rows(self) -> list[ThreadRow]:
        by_id = {row.thread_id: row for row in self.rows}
        return [by_id[thread_id] for thread_id in self.checked_ids if thread_id in by_id]

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
        rows = self.checked_rows()
        if not rows:
            messagebox.showinfo("체크 없음", "삭제할 채팅을 먼저 체크하세요.")
            return

        related_rows = self.related_internal_reviews(rows)
        rows_to_delete = rows + related_rows

        preview = "\n".join(f"- {row.title[:80]}" for row in rows[:8])
        if len(rows) > 8:
            preview += f"\n- ... 외 {len(rows) - 8}개"
        related_msg = ""
        if related_rows:
            related_msg = f"\n\n관련 내부 검토 기록 {len(related_rows)}개도 함께 삭제합니다."
        if not messagebox.askyesno(
            "삭제 확인",
            f"체크한 채팅 {len(rows)}개를 삭제할까요?\n\n{preview}{related_msg}\n\n최근 목록 DB와 실제 세션 파일을 함께 삭제합니다.",
        ):
            return

        try:
            counts = delete_threads(rows_to_delete)
        except sqlite3.OperationalError as exc:
            messagebox.showerror("삭제 실패", f"{exc}\n\nCodex 앱을 닫고 다시 시도해보세요.")
            return
        except Exception as exc:
            messagebox.showerror("삭제 실패", str(exc))
            return

        self.checked_ids.difference_update(row.thread_id for row in rows_to_delete)
        msg = [
            f"삭제한 채팅: {counts.get('threads', 0)}개",
            f"삭제한 파일: {counts.get('session_files', 0)}개",
            f"삭제한 빈 작업 폴더: {counts.get('empty_workspace_dirs', 0)}개",
        ]
        if counts.get("nonempty_workspace_dirs", 0):
            msg.append(f"파일이 있어 남긴 작업 폴더: {counts.get('nonempty_workspace_dirs', 0)}개")
        if related_rows:
            msg.append(f"함께 삭제한 내부 검토 기록: {len(related_rows)}개")
        messagebox.showinfo("삭제 완료", "\n".join(msg))
        self.refresh()


if __name__ == "__main__":
    App().mainloop()
