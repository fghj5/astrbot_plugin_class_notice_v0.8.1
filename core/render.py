from __future__ import annotations

from datetime import datetime, timedelta

from .store import Session

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def cn_day(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d")
    return f"{d.month}月{d.day}日 {WEEKDAYS[d.weekday()]}"


def day_block(day: str, sessions: list[Session], title: str | None = None) -> str:
    head = title or cn_day(day)
    if not sessions:
        return f"📅 {head}\n今天没有课喵"
    lines = [f"📅 {head}   共 {len(sessions)} 节"]
    for i, s in enumerate(sessions, 1):
        lines.append(s.line(i))
    return "\n".join(lines)


def week_block(anchor: str, sessions: list[Session]) -> str:
    start = datetime.strptime(anchor, "%Y-%m-%d")
    start = start - timedelta(days=start.weekday())
    end = start + timedelta(days=6)
    by_day: dict[str, list[Session]] = {}
    for s in sessions:
        by_day.setdefault(s.day, []).append(s)

    lines = [f"🗓 本周（{start.month}/{start.day} - {end.month}/{end.day}）共 {len(sessions)} 节"]
    for offset in range(7):
        d = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        lines.append("")
        lines.append(f"—— {cn_day(d)} ——")
        items = by_day.get(d) or []
        if not items:
            lines.append("  没课喵")
            continue
        for s in items:
            lines.append("  " + s.line())
    return "\n".join(lines)


def notify_text(session: Session, kind: str, minutes: int) -> str:
    where = session.location or ""
    tail = f"  @{where}" if where else ""
    if kind == "start":
        head = f"⏰ {minutes} 分钟后上课喵 · {session.start_time}-{session.end_time}"
    else:
        head = f"🔔 {minutes} 分钟后下课喵 · {session.end_time}"
    return f"{head}\n{session.name}{tail}"
