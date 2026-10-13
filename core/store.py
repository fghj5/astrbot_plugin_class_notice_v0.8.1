from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    uid        TEXT PRIMARY KEY,
    class_id   TEXT NOT NULL DEFAULT '',
    day        TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time   TEXT NOT NULL,
    name       TEXT NOT NULL,
    location   TEXT NOT NULL DEFAULT '',
    teacher    TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'import'
);
CREATE INDEX IF NOT EXISTS idx_sessions_day ON sessions(day);
CREATE INDEX IF NOT EXISTS idx_sessions_class ON sessions(class_id, day);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class Session:
    day: str
    start_time: str
    end_time: str
    name: str
    location: str = ""
    teacher: str = ""
    source: str = "import"
    class_id: str = ""

    @property
    def uid(self) -> str:
        # 带上 source 与 class_id：多班级下同名日程互不覆盖
        return "|".join(
            (self.class_id, self.source, self.day, self.start_time, self.name, self.location)
        )

    def line(self, idx: int | None = None) -> str:
        text = f"{self.start_time}-{self.end_time}  {self.name}"
        if self.location:
            text += f"  @{self.location}"
        if self.teacher:
            text += f"  ({self.teacher})"
        return f"{idx}. {text}" if idx is not None else text


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def init(self) -> None:
        with self._conn() as c:
            # 先检查旧库是否已有 sessions 表
            exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
            if exists:
                cols = [row[1] for row in c.execute("PRAGMA table_info(sessions)").fetchall()]
                if "class_id" not in cols:
                    c.execute(
                        "ALTER TABLE sessions ADD COLUMN class_id TEXT NOT NULL DEFAULT ''"
                    )
                # 旧数据全部视为默认班级
                c.execute(
                    "UPDATE sessions SET class_id = 'default' "
                    "WHERE class_id IS NULL OR class_id = ''"
                )
            else:
                c.executescript(_SCHEMA)
            # 创建索引（新库/旧库都确保存在）
            c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_day ON sessions(day)")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_class ON sessions(class_id, day)"
            )

    def count(self, class_id: str = "") -> int:
        with self._conn() as c:
            if class_id:
                return c.execute(
                    "SELECT COUNT(*) FROM sessions WHERE class_id = ?", (class_id,)
                ).fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def _row_values(self, s: Session) -> tuple:
        return (s.uid, s.class_id, s.day, s.start_time, s.end_time, s.name, s.location, s.teacher, s.source)

    def _row_map(self, row: tuple) -> Session:
        # 兼容新旧两种行结构
        if len(row) == 8:
            return Session(
                day=row[1], start_time=row[2], end_time=row[3], name=row[4],
                location=row[5], teacher=row[6], source=row[7], class_id=row[0] if False else "",
            )
        if len(row) == 9:
            return Session(
                day=row[2], start_time=row[3], end_time=row[4], name=row[5],
                location=row[6], teacher=row[7], source=row[8], class_id=row[1],
            )
        # fallback: legacy
        return Session(*row[:8])

    def replace_all(self, sessions, class_id: str = "") -> int:
        for s in sessions:
            s.class_id = class_id
        rows = [self._row_values(s) for s in sessions]
        with self._conn() as c:
            if class_id:
                c.execute("DELETE FROM sessions WHERE class_id = ?", (class_id,))
            else:
                c.execute("DELETE FROM sessions")
            c.executemany(
                "INSERT OR REPLACE INTO sessions"
                " (uid, class_id, day, start_time, end_time, name, location, teacher, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def replace_import(self, sessions, class_id: str = "") -> int:
        """替换指定班级的「导入来源」记录，保留手动录入(manual)和命令添加(cmd)。"""
        for s in sessions:
            s.class_id = class_id
        rows = [self._row_values(s) for s in sessions]
        with self._conn() as c:
            if class_id:
                c.execute(
                    "DELETE FROM sessions WHERE source NOT IN ('manual','cmd') AND class_id = ?",
                    (class_id,),
                )
            else:
                c.execute("DELETE FROM sessions WHERE source NOT IN ('manual', 'cmd')")
            c.executemany(
                "INSERT OR REPLACE INTO sessions"
                " (uid, class_id, day, start_time, end_time, name, location, teacher, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def replace_manual(self, sessions, class_id: str = "") -> int:
        """替换指定班级的手动录入记录（source='manual'）。"""
        for s in sessions:
            s.class_id = class_id
        rows = [self._row_values(s) for s in sessions]
        with self._conn() as c:
            if class_id:
                c.execute(
                    "DELETE FROM sessions WHERE source = 'manual' AND class_id = ?",
                    (class_id,),
                )
            else:
                c.execute("DELETE FROM sessions WHERE source = 'manual'")
            c.executemany(
                "INSERT OR REPLACE INTO sessions"
                " (uid, class_id, day, start_time, end_time, name, location, teacher, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def add_sessions(self, sessions, class_id: str = "") -> int:
        """为指定班级新增日程（不删除已有记录）。"""
        for s in sessions:
            s.class_id = class_id
        rows = [self._row_values(s) for s in sessions]
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO sessions"
                " (uid, class_id, day, start_time, end_time, name, location, teacher, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def delete_at(self, day: str, start_time: str = "", class_id: str = "") -> list[Session]:
        """按班级 + 日期（+可选开始时间）删除，返回被删记录。"""
        cols = "class_id, day, start_time, end_time, name, location, teacher, source"
        with self._conn() as c:
            if class_id:
                base_where = "class_id = ?"
                base_params = [class_id]
            else:
                base_where = "class_id = '' OR class_id IS NULL"
                base_params = []
            if start_time:
                rows = c.execute(
                    f"SELECT {cols} FROM sessions WHERE {base_where} AND day = ? AND start_time = ?"
                    " ORDER BY start_time",
                    base_params + [day, start_time],
                ).fetchall()
                c.execute(
                    f"DELETE FROM sessions WHERE {base_where} AND day = ? AND start_time = ?",
                    base_params + [day, start_time],
                )
            else:
                rows = c.execute(
                    f"SELECT {cols} FROM sessions WHERE {base_where} AND day = ? ORDER BY start_time",
                    base_params + [day],
                ).fetchall()
                c.execute(
                    f"DELETE FROM sessions WHERE {base_where} AND day = ?",
                    base_params + [day],
                )
            out = []
            for r in rows:
                s = Session(
                    day=r[1], start_time=r[2], end_time=r[3], name=r[4],
                    location=r[5], teacher=r[6], source=r[7], class_id=r[0],
                )
                out.append(s)
            return out

    def count_by_source(self, class_id: str = "") -> dict:
        with self._conn() as c:
            if class_id:
                cur = c.execute(
                    "SELECT source, COUNT(*) FROM sessions WHERE class_id = ? GROUP BY source",
                    (class_id,),
                )
            else:
                cur = c.execute("SELECT source, COUNT(*) FROM sessions GROUP BY source")
            return {row[0]: row[1] for row in cur.fetchall()}

    def clear(self, class_id: str = "") -> None:
        with self._conn() as c:
            if class_id:
                c.execute("DELETE FROM sessions WHERE class_id = ?", (class_id,))
            else:
                c.execute("DELETE FROM sessions")

    def delete_before(self, day: str, class_id: str = "") -> int:
        with self._conn() as c:
            if class_id:
                cur = c.execute(
                    "DELETE FROM sessions WHERE day < ? AND class_id = ?", (day, class_id)
                )
            else:
                cur = c.execute("DELETE FROM sessions WHERE day < ?", (day,))
            return cur.rowcount

    def delete_matching(self, keyword: str, class_id: str = "") -> int:
        like = f"%{keyword}%"
        with self._conn() as c:
            if class_id:
                cur = c.execute(
                    "DELETE FROM sessions WHERE (name LIKE ? OR location LIKE ?) AND class_id = ?",
                    (like, like, class_id),
                )
            else:
                cur = c.execute(
                    "DELETE FROM sessions WHERE name LIKE ? OR location LIKE ?",
                    (like, like),
                )
            return cur.rowcount

    def day(self, day: str, class_id: str = "") -> list[Session]:
        with self._conn() as c:
            cols = "class_id, day, start_time, end_time, name, location, teacher, source"
            if class_id:
                cur = c.execute(
                    f"SELECT {cols} FROM sessions WHERE class_id = ? AND day = ? ORDER BY start_time",
                    (class_id, day),
                )
            else:
                cur = c.execute(
                    f"SELECT {cols} FROM sessions WHERE (class_id = '' OR class_id IS NULL) AND day = ? ORDER BY start_time",
                    (day,),
                )
            out = []
            for r in cur.fetchall():
                out.append(
                    Session(
                        day=r[1], start_time=r[2], end_time=r[3], name=r[4],
                        location=r[5], teacher=r[6], source=r[7], class_id=r[0],
                    )
                )
            return out

    def between(self, start: str, end: str, class_id: str = "") -> list[Session]:
        with self._conn() as c:
            cols = "class_id, day, start_time, end_time, name, location, teacher, source"
            if class_id:
                cur = c.execute(
                    f"SELECT {cols} FROM sessions WHERE class_id = ? AND day BETWEEN ? AND ? ORDER BY day, start_time",
                    (class_id, start, end),
                )
            else:
                cur = c.execute(
                    f"SELECT {cols} FROM sessions WHERE (class_id = '' OR class_id IS NULL) AND day BETWEEN ? AND ? ORDER BY day, start_time",
                    (start, end),
                )
            out = []
            for r in cur.fetchall():
                out.append(
                    Session(
                        day=r[1], start_time=r[2], end_time=r[3], name=r[4],
                        location=r[5], teacher=r[6], source=r[7], class_id=r[0],
                    )
                )
            return out

    def first_day(self, class_id: str = "") -> str:
        with self._conn() as c:
            if class_id:
                row = c.execute(
                    "SELECT MIN(day) FROM sessions WHERE class_id = ?", (class_id,)
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT MIN(day) FROM sessions WHERE class_id = '' OR class_id IS NULL"
                ).fetchone()
            return row[0] or "" if row else ""

    def last_day(self, class_id: str = "") -> str:
        with self._conn() as c:
            if class_id:
                row = c.execute(
                    "SELECT MAX(day) FROM sessions WHERE class_id = ?", (class_id,)
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT MAX(day) FROM sessions WHERE class_id = '' OR class_id IS NULL"
                ).fetchone()
            return row[0] or "" if row else ""

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    def get_meta(self, key: str, default: str = "") -> str:
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default
