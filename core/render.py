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


# -------------------------------------------------- 可视化课表表格（按周）

def _disp_width(s: str) -> int:
    """终端显示宽度：全角/中日韩字符按 2，其余按 1。"""
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x2E80 or ch in "—│┌┐└┘├┤┬┴┼●○★✓❌" else 1
    return w


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _disp_width(s))


def _fmt_cell(session: Session) -> str:
    parts = [session.name]
    if session.location:
        parts.append(f"@{session.location}")
    return " ".join(parts)


def week_table(anchor: str, sessions: list[Session], cell_width: int = 16) -> str:
    """按周输出课表表格：列为周一~周日，行为上课时间（节次），单元格放课程。

    同一节次若有多门课用「/」拼接；无课显示「·」。
    """
    start = datetime.strptime(anchor, "%Y-%m-%d")
    start = start - timedelta(days=start.weekday())
    day_dates = [start + timedelta(days=i) for i in range(7)]
    days = [d.strftime("%Y-%m-%d") for d in day_dates]
    by_day: dict[str, list[Session]] = {d: [] for d in days}
    slots = set()
    for s in sessions:
        if s.day in by_day:
            by_day[s.day].append(s)
            slots.add(s.start_time)
    slot_list = sorted(slots)
    end_by_start = {}
    for s in sessions:
        end_by_start.setdefault(s.start_time, s.end_time)

    label_w = 12
    lines = ["┌" + "┬".join(["─" * label_w] + ["─" * cell_width] * 7) + "┐"]
    header = "│" + "│".join([_pad("时间", label_w)] + [_pad(WEEKDAYS[i], cell_width) for i in range(7)]) + "│"
    lines.append(header)
    lines.append("├" + "┼".join(["─" * label_w] + ["─" * cell_width] * 7) + "┤")
    for st in slot_list:
        end = end_by_start.get(st, "")
        label = f"{st}-{end}"
        cells = []
        for d in days:
            names = [_fmt_cell(s) for s in by_day[d] if s.start_time == st]
            cell = " / ".join(names)[:cell_width - 1] if names else "·"
            cells.append(cell)
        lines.append("│" + "│".join([_pad(label, label_w)] + [_pad(c, cell_width) for c in cells]) + "│")
    lines.append("└" + "┴".join(["─" * label_w] + ["─" * cell_width] * 7) + "┘")
    rng = f"{day_dates[0].month}/{day_dates[0].day} - {day_dates[-1].month}/{day_dates[-1].day}"
    out = [f"🗓 本周课表（{rng}） 共 {len(sessions)} 节"]
    out.extend(lines)
    return "\n".join(out)


def week_table_markdown(anchor: str, sessions: list[Session]) -> str:
    """Markdown 版周课表表格（面板/文档使用）。"""
    start = datetime.strptime(anchor, "%Y-%m-%d")
    start = start - timedelta(days=start.weekday())
    day_dates = [start + timedelta(days=i) for i in range(7)]
    days = [d.strftime("%Y-%m-%d") for d in day_dates]
    by_day: dict[str, list[Session]] = {d: [] for d in days}
    end_by_start = {}
    for s in sessions:
        if s.day in by_day:
            by_day[s.day].append(s)
        end_by_start.setdefault(s.start_time, s.end_time)
    slot_list = sorted(end_by_start.keys())
    head = "| 时间 | " + " | ".join(WEEKDAYS) + " |"
    sep = "| --- | " + " | ".join(["---"] * 7) + " |"
    rows = [head, sep]
    for st in slot_list:
        cells = [f"{st}-{end_by_start.get(st,'')}"]
        for d in days:
            names = [
                (f"{s.name}<br>@{s.location}" if s.location else s.name)
                for s in by_day[d] if s.start_time == st
            ]
            cells.append(" / ".join(names) if names else "·")
        rows.append("| " + " | ".join(cells) + " |")
    rng = f"{day_dates[0].month}/{day_dates[0].day} - {day_dates[-1].month}/{day_dates[-1].day}"
    return f"🗓 本周课表（{rng}） 共 {len(slot_list)} 节\n" + "\n".join(rows)
