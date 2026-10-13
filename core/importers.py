from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .store import Session

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_DURATION_MIN = 90

# 课程表 App 同步到日历时会额外写入它自己的提醒条目，
# 形如「10分钟后上课:大学英语A」「5分钟后下课:大学英语A」，零时长，必须滤掉。
_REMINDER_TITLE_RE = re.compile(r"^\s*\d+\s*分钟后\s*(上课|下课)")


def get_tz(name: str = DEFAULT_TZ):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone(timedelta(hours=8))


# ---------------------------------------------------------------- ICS

def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _parse_dt(raw_key: str, raw_val: str, tz):
    val = (raw_val or "").strip()
    if not val:
        return None
    if "VALUE=DATE" in raw_key.upper():
        return None
    local_tz = tz
    hit = re.search(r"TZID=([^;:]+)", raw_key, re.I)
    if hit:
        local_tz = get_tz(hit.group(1))
    try:
        if val.upper().endswith("Z"):
            return (
                datetime.strptime(val[:15], "%Y%m%dT%H%M%S")
                .replace(tzinfo=timezone.utc)
                .astimezone(local_tz)
            )
        core = val[:15] if len(val) >= 15 else val
        if "T" not in core:
            return None
        return datetime.strptime(core, "%Y%m%dT%H%M%S").replace(tzinfo=local_tz)
    except ValueError:
        return None


def _rrule_starts(rule: str, dtstart, exdates) -> list:
    parts = {}
    for kv in rule.split(";"):
        if "=" in kv:
            key, _, val = kv.partition("=")
            parts[key.strip().upper()] = val.strip()
    if parts.get("FREQ", "").upper() != "WEEKLY":
        return [dtstart]
    try:
        interval = max(1, int(parts.get("INTERVAL", "1")))
    except ValueError:
        interval = 1
    count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
    until = None
    if parts.get("UNTIL"):
        raw = parts["UNTIL"].rstrip("Z")
        for fmt, size in (("%Y%m%dT%H%M%S", 15), ("%Y%m%d", 8)):
            try:
                until = datetime.strptime(raw[:size], fmt)
                break
            except ValueError:
                continue
    out = []
    cur = dtstart
    while len(out) < 400:
        if until is not None and cur.replace(tzinfo=None) > until:
            break
        out.append(cur)
        if count is not None and len(out) >= count:
            break
        cur = cur + timedelta(weeks=interval)
    if exdates:
        skip = set(exdates)
        out = [d for d in out if d not in skip]
    return out


def parse_ics(text: str, tz_name: str = DEFAULT_TZ, stats: dict | None = None) -> list[Session]:
    tz = get_tz(tz_name)
    events, cur = [], None
    for line in _unfold(text):
        s = line.strip()
        if s == "BEGIN:VEVENT":
            cur = {}
        elif s == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in s:
            key, _, value = s.partition(":")
            cur.setdefault(key.split(";", 1)[0].strip().upper(), []).append(
                (key.strip(), value.strip())
            )

    sessions: list[Session] = []
    skipped = 0
    for ev in events:
        if (ev.get("STATUS") or [("", "")])[0][1].upper() == "CANCELLED":
            skipped += 1
            continue
        starts = ev.get("DTSTART") or []
        if not starts:
            continue
        start = _parse_dt(starts[0][0], starts[0][1], tz)
        if start is None:
            continue
        end = None
        if ev.get("DTEND"):
            end = _parse_dt(ev["DTEND"][0][0], ev["DTEND"][0][1], tz)
        if end is None:
            end = start + timedelta(minutes=DEFAULT_DURATION_MIN)
        elif end <= start:
            # 零时长/负时长：多半是 App 的提醒条目，跳过
            skipped += 1
            continue

        summary = ((ev.get("SUMMARY") or [("", "")])[0][1] or "未命名课程").strip()
        if _REMINDER_TITLE_RE.match(summary):
            skipped += 1
            continue
        location = ((ev.get("LOCATION") or [("", "")])[0][1] or "").strip()
        desc = ((ev.get("DESCRIPTION") or [("", "")])[0][1] or "").strip()
        if not location and desc:
            hit = re.search(r"(?:地点|教室|上课地点)\s*[:：]\s*([^\s\n;；]+)", desc)
            if hit:
                location = hit.group(1).strip()

        exdates = []
        for key, val in ev.get("EXDATE") or []:
            for piece in val.split(","):
                got = _parse_dt(key, piece.strip(), tz)
                if got:
                    exdates.append(got)

        base = [start]
        if ev.get("RRULE"):
            base = _rrule_starts(ev["RRULE"][0][1], start, exdates)
        duration = end - start
        for st in base:
            fin = st + duration
            sessions.append(
                Session(
                    day=st.strftime("%Y-%m-%d"),
                    start_time=st.strftime("%H:%M"),
                    end_time=fin.strftime("%H:%M"),
                    name=summary,
                    location=location,
                    source="ics",
                )
            )
    sessions.sort(key=lambda s: (s.day, s.start_time))
    if stats is not None:
        stats["skipped"] = skipped
    return sessions


# ---------------------------------------------------------- CSV / TSV

_ALIASES = {
    "day": {"day", "date", "日期", "上课日期", "年月日"},
    "start": {"start", "start_time", "begin", "开始", "开始时间", "上课时间"},
    "end": {"end", "end_time", "finish", "结束", "结束时间", "下课时间"},
    "name": {"name", "course", "title", "subject", "课程", "课程名", "课程名称", "标题"},
    "location": {"location", "room", "place", "地点", "教室", "上课地点"},
    "teacher": {"teacher", "tutor", "教师", "老师", "任课教师"},
}


def _pick(field: str, header: str) -> bool:
    return header.strip().lower() in _ALIASES[field]


def _norm_day(raw: str) -> str:
    raw = (raw or "").strip().replace("/", "-").replace(".", "-")
    hit = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if not hit:
        return ""
    return f"{int(hit.group(1)):04d}-{int(hit.group(2)):02d}-{int(hit.group(3)):02d}"


def _norm_hhmm(raw: str) -> str:
    hit = re.match(r"\s*(\d{1,2})\s*[:：]\s*(\d{2})", raw or "")
    if not hit:
        return ""
    return f"{int(hit.group(1)):02d}:{hit.group(2)}"


def parse_csv(text: str, tz_name: str = DEFAULT_TZ) -> list[Session]:
    first = text.lstrip().split("\n", 1)[0]
    delim = max((",", "\t", ";"), key=first.count)
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if len(rows) < 2:
        return []
    header = [str(c).strip() for c in rows[0]]
    idx = {}
    for field in _ALIASES:
        for i, col in enumerate(header):
            if _pick(field, col):
                idx[field] = i
                break
    if "day" not in idx or "start" not in idx:
        raise ValueError("CSV 需要包含『日期』和『开始时间』两列")

    sessions: list[Session] = []
    for row in rows[1:]:
        def cell(field: str) -> str:
            i = idx.get(field, -1)
            return str(row[i]).strip() if 0 <= i < len(row) else ""

        day = _norm_day(cell("day"))
        start = _norm_hhmm(cell("start"))
        if not day or not start:
            continue
        end = _norm_hhmm(cell("end"))
        if not end:
            end = (
                datetime.strptime(start, "%H:%M") + timedelta(minutes=DEFAULT_DURATION_MIN)
            ).strftime("%H:%M")
        sessions.append(
            Session(
                day=day,
                start_time=start,
                end_time=end,
                name=cell("name") or "未命名课程",
                location=cell("location"),
                teacher=cell("teacher"),
                source="csv",
            )
        )
    sessions.sort(key=lambda s: (s.day, s.start_time))
    return sessions


# ---------------------------------------------------------------- JSON

def parse_json(text: str, tz_name: str = DEFAULT_TZ) -> list[Session]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("sessions") or data.get("courses") or data.get("data") or []
    if not isinstance(data, list):
        return []
    sessions: list[Session] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        day = _norm_day(str(item.get("day") or item.get("date") or item.get("日期") or ""))
        start = _norm_hhmm(str(item.get("start") or item.get("start_time") or item.get("开始时间") or ""))
        if not day or not start:
            continue
        end = _norm_hhmm(str(item.get("end") or item.get("end_time") or item.get("结束时间") or ""))
        if not end:
            end = (
                datetime.strptime(start, "%H:%M") + timedelta(minutes=DEFAULT_DURATION_MIN)
            ).strftime("%H:%M")
        sessions.append(
            Session(
                day=day,
                start_time=start,
                end_time=end,
                name=str(item.get("name") or item.get("课程") or "未命名课程").strip(),
                location=str(item.get("location") or item.get("地点") or item.get("教室") or "").strip(),
                teacher=str(item.get("teacher") or item.get("老师") or item.get("教师") or "").strip(),
                source="json",
            )
        )
    sessions.sort(key=lambda s: (s.day, s.start_time))
    return sessions


# ---------------------------------------------------------------- 文本

_LINE_RE = re.compile(
    r"(?P<day>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})\s+"
    r"(?P<start>\d{1,2}[:：]\d{2})\s*[-~至]\s*(?P<end>\d{1,2}[:：]\d{2})\s+"
    r"(?P<name>[^@#]+?)\s*(?:@(?P<loc>\S+))?\s*$"
)


def parse_text(text: str, tz_name: str = DEFAULT_TZ) -> list[Session]:
    sessions: list[Session] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        hit = _LINE_RE.match(line)
        if not hit:
            continue
        sessions.append(
            Session(
                day=_norm_day(hit.group("day")),
                start_time=_norm_hhmm(hit.group("start")),
                end_time=_norm_hhmm(hit.group("end")),
                name=hit.group("name").strip(),
                location=(hit.group("loc") or "").strip(),
                source="text",
            )
        )
    sessions.sort(key=lambda s: (s.day, s.start_time))
    return sessions


# ------------------------------------------------------------ 统一入口

def parse_any(
    text: str, tz_name: str = DEFAULT_TZ, stats: dict | None = None
) -> tuple[list[Session], str]:
    head = text.lstrip()
    if "BEGIN:VEVENT" in head or "BEGIN:VCALENDAR" in head:
        sessions, kind = parse_ics(text, tz_name, stats), "ics"
    elif head[:1] in ("{", "["):
        sessions, kind = parse_json(text, tz_name), "json"
    else:
        first = head.split("\n", 1)[0]
        if "," in first or "\t" in first or ";" in first:
            sessions, kind = parse_csv(text, tz_name), "csv"
        else:
            sessions, kind = parse_text(text, tz_name), "text"

    merged_from = len(sessions)
    sessions = merge_sessions(sessions)
    if stats is not None:
        stats["merged"] = merged_from - len(sessions)
    return sessions, kind


def load_file(
    path: str | Path, tz_name: str = DEFAULT_TZ, stats: dict | None = None
) -> tuple[list[Session], str]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(path))
    return parse_any(p.read_text(encoding="utf-8-sig"), tz_name, stats)


# ------------------------------------------------------------ 连堂合并

MERGE_GAP_MIN = 20


def _to_min(hhmm: str) -> int:
    h, _, m = hhmm.partition(":")
    return int(h) * 60 + int(m)


def _to_hhmm(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


def merge_sessions(sessions: list[Session], gap_min: int = MERGE_GAP_MIN) -> list[Session]:
    """同一天、同名、同地点、间隔不超过 gap_min 的相邻节次合并成一段（连堂）。

    例如 09:45-10:25 与 10:30-11:10 的同一门课会合并成 09:45-11:10。
    """
    out: list[Session] = []
    merged = 0
    for s in sorted(sessions, key=lambda x: (x.day, x.start_time)):
        if out:
            last = out[-1]
            if last.day == s.day and last.name == s.name and last.location == s.location:
                gap = _to_min(s.start_time) - _to_min(last.end_time)
                if 0 <= gap <= gap_min:
                    last.end_time = max(last.end_time, s.end_time, key=_to_min)
                    merged += 1
                    continue
        out.append(s)
    return out



# ------------------------------------------------------------ 日期解析

def parse_day_input(text: str, today: date | None = None) -> str:
    """把用户输入的日期解析成 YYYY-MM-DD，解析不出来返回空串。

    支持：2026-09-16 / 2026/9/16 / 2026年9月16日 / 9-16 / 9/16 / 9月16日 / 0916
    只写月日时默认今年；若那天的今年版本已过去超过 180 天，则按明年算
    （这样 12 月输入 1/8 不会落到一年前去）。
    """
    if today is None:
        today = date.today()
    text = (text or "").strip()
    if not text:
        return ""
    year = None
    month = None
    day = None
    m = re.match(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?$", text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r"^(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?$", text)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
        else:
            m = re.match(r"^(\d{2})(\d{2})$", text)
            if m:
                month, day = int(m.group(1)), int(m.group(2))
    if month is None or day is None:
        return ""
    if year is None:
        year = today.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return ""
        if (today - candidate).days > 180:
            year += 1
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


# ------------------------------------------------------ 面板手动录入的日程

def parse_manual_lines(
    lines, tz_name: str = DEFAULT_TZ, today: date | None = None, stats: dict | None = None
):
    """解析面板「手动录入日程」里的每一行。

    每行格式：
        2026-09-16 08:00-09:40 高等数学 @教三201
    日期也支持 9/16、9月16日、0916（会自动补年份）。
    返回 (sessions, 解析失败的行)。所有结果的 source 强制为 manual。
    """
    if today is None:
        today = date.today()
    ok: list[Session] = []
    bad: list[str] = []
    for raw in lines or []:
        original = str(raw).strip()
        if not original or original.startswith("#"):
            continue
        text = original
        head, _, rest = text.partition(" ")
        fixed = parse_day_input(head, today)
        if fixed and rest.strip():
            text = fixed + " " + rest.strip()
        got = parse_text(text, tz_name)
        if got:
            for item in got:
                item.source = "manual"
            ok.extend(got)
        else:
            bad.append(original)
    if stats is not None:
        stats["failed"] = bad
    return merge_sessions(ok), bad

