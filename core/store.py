from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# 兼容旧库的全局表（v2 之前：单表 sessions + class_id 列）
_LEGACY_SCHEMA = """
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


def _safe_id(class_id: str) -> str:
    """把班级 ID 转成安全的表名后缀。"""
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(class_id or ""))
    return s or "default"


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
    """以「班级」为单位存储课表。

    每个班级对应独立的表：sessions_<class_id> / meta_<class_id>。
    旧版单表（sessions + class_id 列）数据会在 init() 时自动迁移到班级表。
    class_id 为空时视为默认班级表（sessions_default）。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 表名

    def _ptable(self, class_id: str = "") -> str:
        return f"sessions_{_safe_id(class_id)}"

    def _mtable(self, class_id: str = "") -> str:
        return f"meta_{_safe_id(class_id)}"

    # ------------------------------------------------------------ 连接

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _ensure_tables(self, class_id: str = "") -> None:
        """确保指定班级的课表表与 meta 表存在。"""
        t = self._ptable(class_id)
        m = self._mtable(class_id)
        with self._conn() as c:
            c.execute(
                f"""CREATE TABLE IF NOT EXISTS {t} (
                    uid        TEXT PRIMARY KEY,
                    day        TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time   TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    location   TEXT NOT NULL DEFAULT '',
                    teacher    TEXT NOT NULL DEFAULT '',
                    source     TEXT NOT NULL DEFAULT 'import'
                )"""
            )
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_day ON {t}(day)")
            c.execute(
                f"""CREATE TABLE IF NOT EXISTS {m} (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )"""
            )

    # ------------------------------------------------------------ 初始化 / 迁移

    def init(self) -> None:
        with self._conn() as c:
            legacy = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
        if not legacy:
            # 全新库：默认班级表 + 全局 meta（兼容旧调用）
            self._ensure_tables("")
            self.set_meta("migration:v2", "1", "")
            return
        # 旧库迁移：把单表 sessions 按 class_id 拆到班级表
        with self._conn() as c:
            cols = [row[1] for row in c.execute("PRAGMA table_info(sessions)").fetchall()]
            if "class_id" not in cols:
                c.execute("ALTER TABLE sessions ADD COLUMN class_id TEXT NOT NULL DEFAULT ''")
            c.execute(
                "UPDATE sessions SET class_id='default' WHERE class_id IS NULL OR class_id=''"
            )
            cids = [
                r[0] for r in c.execute("SELECT DISTINCT class_id FROM sessions").fetchall()
            ]
        for cid in cids:
            self._ensure_tables(cid)
            src_t = self._ptable(cid)
            with self._conn() as c:
                c.execute(
                    f"INSERT OR REPLACE INTO {src_t} "
                    "(uid, day, start_time, end_time, name, location, teacher, source) "
                    "SELECT uid, day, start_time, end_time, name, location, teacher, source "
                    "FROM sessions WHERE class_id = ?",
                    (cid,),
                )
        # 旧 meta 表数据并入全局 meta
        with self._conn() as c:
            try:
                for key, value in c.execute("SELECT key, value FROM meta").fetchall():
                    c.execute(
                        "INSERT OR REPLACE INTO meta_default (key, value) VALUES (?, ?)",
                        (key, str(value)),
                    )
            except Exception:
                pass
        # 迁移完成标记（旧表保留不删，作为备份）
        self.set_meta("migration:v2", "1", "")
        logger = None
        try:
            from astrbot.api import logger as _lg
            logger = _lg
        except Exception:
            pass
        if logger:
            logger.info(
                "[class_notice] store 已迁移为按班级存储: %s", ", ".join(map(str, cids))
            )

    # ------------------------------------------------------------ 会话

    @staticmethod
    def _row_to_session(row: tuple, class_id: str = "") -> Session:
        return Session(
            day=row[1],
            start_time=row[2],
            end_time=row[3],
            name=row[4],
            location=row[5],
            teacher=row[6],
            source=row[7],
            class_id=class_id,
        )

    @staticmethod
    def _row_values(s: Session) -> tuple:
        return (s.uid, s.day, s.start_time, s.end_time, s.name, s.location, s.teacher, s.source)

    # ------------------------------------------------------------ 统计 / 查询

    def count(self, class_id: str = "") -> int:
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        with self._conn() as c:
            return c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

    def count_by_source(self, class_id: str = "") -> dict:
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        with self._conn() as c:
            cur = c.execute(f"SELECT source, COUNT(*) FROM {t} GROUP BY source")
            return {row[0]: row[1] for row in cur.fetchall()}

    def day(self, day: str, class_id: str = "") -> list[Session]:
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        with self._conn() as c:
            cur = c.execute(
                f"SELECT uid, day, start_time, end_time, name, location, teacher, source "
                f"FROM {t} WHERE day = ? ORDER BY start_time",
                (day,),
            )
            return [self._row_to_session(r, class_id) for r in cur.fetchall()]

    def between(self, start: str, end: str, class_id: str = "") -> list[Session]:
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        with self._conn() as c:
            cur = c.execute(
                f"SELECT uid, day, start_time, end_time, name, location, teacher, source "
                f"FROM {t} WHERE day BETWEEN ? AND ? ORDER BY day, start_time",
                (start, end),
            )
            return [self._row_to_session(r, class_id) for r in cur.fetchall()]

    def first_day(self, class_id: str = "") -> str:
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        with self._conn() as c:
            row = c.execute(f"SELECT MIN(day) FROM {t}").fetchone()
            return row[0] or "" if row else ""

    def last_day(self, class_id: str = "") -> str:
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        with self._conn() as c:
            row = c.execute(f"SELECT MAX(day) FROM {t}").fetchone()
            return row[0] or "" if row else ""

    # ------------------------------------------------------------ 写入

    def replace_all(self, sessions, class_id: str = "") -> int:
        """清空班级表后写入全部记录。"""
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        rows = [self._row_values(s) for s in sessions]
        with self._conn() as c:
            c.execute(f"DELETE FROM {t}")
            c.executemany(
                f"INSERT OR REPLACE INTO {t} "
                "(uid, day, start_time, end_time, name, location, teacher, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def replace_import(self, sessions, class_id: str = "") -> int:
        """替换指定班级的「导入来源」记录，保留手动录入(manual)和命令添加(cmd)。"""
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        rows = [self._row_values(s) for s in sessions]
        with self._conn() as c:
            c.execute(f"DELETE FROM {t} WHERE source NOT IN ('manual','cmd')")
            c.executemany(
                f"INSERT OR REPLACE INTO {t} "
                "(uid, day, start_time, end_time, name, location, teacher, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def replace_manual(self, sessions, class_id: str = "") -> int:
        """替换指定班级的手动录入记录（source='manual'）。"""
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        rows = [self._row_values(s) for s in sessions]
        with self._conn() as c:
            c.execute(f"DELETE FROM {t} WHERE source = 'manual'")
            c.executemany(
                f"INSERT OR REPLACE INTO {t} "
                "(uid, day, start_time, end_time, name, location, teacher, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def add_sessions(self, sessions, class_id: str = "") -> int:
        """为指定班级新增日程（不删除已有记录）。"""
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        rows = [self._row_values(s) for s in sessions]
        with self._conn() as c:
            c.executemany(
                f"INSERT OR REPLACE INTO {t} "
                "(uid, day, start_time, end_time, name, location, teacher, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    # ------------------------------------------------------------ 删除

    def delete_at(self, day: str, start_time: str = "", class_id: str = "") -> list[Session]:
        """按班级 + 日期（+可选开始时间）删除，返回被删记录。"""
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        cols = "uid, day, start_time, end_time, name, location, teacher, source"
        with self._conn() as c:
            if start_time:
                rows = c.execute(
                    f"SELECT {cols} FROM {t} WHERE day = ? AND start_time = ? ORDER BY start_time",
                    (day, start_time),
                ).fetchall()
                c.execute(
                    f"DELETE FROM {t} WHERE day = ? AND start_time = ?",
                    (day, start_time),
                )
            else:
                rows = c.execute(
                    f"SELECT {cols} FROM {t} WHERE day = ? ORDER BY start_time",
                    (day,),
                ).fetchall()
                c.execute(f"DELETE FROM {t} WHERE day = ?", (day,))
            return [self._row_to_session(r, class_id) for r in rows]

    def clear(self, class_id: str = "") -> None:
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        with self._conn() as c:
            c.execute(f"DELETE FROM {t}")

    def delete_before(self, day: str, class_id: str = "") -> int:
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        with self._conn() as c:
            cur = c.execute(f"DELETE FROM {t} WHERE day < ?", (day,))
            return cur.rowcount

    def delete_matching(self, keyword: str, class_id: str = "") -> int:
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        like = f"%{keyword}%"
        with self._conn() as c:
            cur = c.execute(
                f"DELETE FROM {t} WHERE name LIKE ? OR location LIKE ?", (like, like)
            )
            return cur.rowcount

    # ------------------------------------------------------------ 编辑支持

    def get_by_uid(self, uid: str, class_id: str = "") -> Session | None:
        """按 uid 查询单条记录。"""
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        with self._conn() as c:
            row = c.execute(
                f"SELECT uid, day, start_time, end_time, name, location, teacher, source "
                f"FROM {t} WHERE uid = ?",
                (uid,),
            ).fetchone()
            return self._row_to_session(row, class_id) if row else None

    def delete_by_uid(self, uid: str, class_id: str = "") -> int:
        """按 uid 删除单条记录，返回删除条数。"""
        t = self._ptable(class_id)
        self._ensure_tables(class_id)
        with self._conn() as c:
            cur = c.execute(f"DELETE FROM {t} WHERE uid = ?", (uid,))
            return cur.rowcount

    # ------------------------------------------------------------ 班级管理

    def class_tables(self) -> list[str]:
        """返回已存在的班级课表表对应的班级 ID 列表。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sessions\\_%' ESCAPE '\\'"
            ).fetchall()
        out = []
        for (name,) in rows:
            cid = name[len("sessions_"):]
            if cid == "default":
                continue
            out.append(cid)
        return out

    def drop_class(self, class_id: str = "") -> None:
        """彻底删除指定班级的课表表与 meta 表。"""
        t, m = self._ptable(class_id), self._mtable(class_id)
        with self._conn() as c:
            c.execute(f"DROP TABLE IF EXISTS {t}")
            c.execute(f"DROP TABLE IF EXISTS {m}")
            c.execute(
                "DELETE FROM meta_default WHERE key IN (?, ?, ?)",
                (f"imported_at:{class_id}", f"imported_from:{class_id}", f"imported_kind:{class_id}"),
            )

    def copy_class(self, src_class_id: str, dst_class_id: str) -> int:
        """把源班级的课表复制到目标班级，返回复制条数。"""
        src_t, dst_t = self._ptable(src_class_id), self._ptable(dst_class_id)
        self._ensure_tables(src_class_id)
        self._ensure_tables(dst_class_id)
        with self._conn() as c:
            # uid 里含旧的 class_id 前缀：目标班级重新生成 uid
            c.execute(
                f"INSERT OR REPLACE INTO {dst_t} "
                "(uid, day, start_time, end_time, name, location, teacher, source) "
                f"SELECT ? || '|' || source || '|' || day || '|' || start_time || '|' || name || '|' || location, "
                f"day, start_time, end_time, name, location, teacher, source FROM {src_t}",
                (dst_class_id,),
            )
            return c.execute(f"SELECT COUNT(*) FROM {dst_t}").fetchone()[0]

    # ------------------------------------------------------------ meta

    def set_meta(self, key: str, value: str, class_id: str = "") -> None:
        m = self._mtable(class_id)
        self._ensure_tables(class_id)
        with self._conn() as c:
            c.execute(
                f"INSERT OR REPLACE INTO {m} (key, value) VALUES (?, ?)",
                (key, str(value)),
            )

    def get_meta(self, key: str, default: str = "", class_id: str = "") -> str:
        m = self._mtable(class_id)
        self._ensure_tables(class_id)
        with self._conn() as c:
            row = c.execute(f"SELECT value FROM {m} WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default