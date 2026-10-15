from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta

from astrbot.api import logger


def _to_min(hhmm: str) -> int:
    h, _, m = hhmm.partition(":")
    return int(h) * 60 + int(m)


class NotifyScheduler:
    """多班级版：上课前 / 下课前提醒 + 每日课程推送调度器。

    每 TICK 秒检查一次「今天」的课，命中提醒时间窗口就推送到所有目标会话。
    用「宽限窗口」而不是持久化已发标记：
      - 重启后不会补发已经过期的提醒（delta < 0 直接忽略）
      - 短时间内重复 tick 也不会重复发（同一个 key 只发一次）
    """

    TICK_SECONDS = 20
    GRACE_SECONDS = 300

    def __init__(self, plugin):
        self.plugin = plugin
        self._task: asyncio.Task | None = None
        self._sent: set[str] = set()
        self._daily_sent: set[str] = set()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("[class_notice] 提醒调度已启动")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[class_notice] 调度异常: %s", exc)
            await asyncio.sleep(self.TICK_SECONDS)

    # ------------------------------------------------------------ 单次检查

    def _target_time(self, now: datetime, hhmm: str, minutes: int) -> datetime | None:
        if not hhmm:
            return None
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return base + timedelta(minutes=_to_min(hhmm) - int(minutes))

    def _daily_key(self, today: str, class_id: str) -> str:
        return f"day={today}|class={class_id or 'default'}"

    async def tick(self) -> None:
        # Sync all configured classes once per tick (cheap)
        for cls in self.plugin._classes():
            try:
                self.plugin._sync_class_manual_entries(str(cls.get('id') or ''), force=False)
            except Exception:
                pass

        now = self.plugin.now()
        today = now.strftime("%Y-%m-%d")

        classes = self.plugin._classes()
        if not classes:
            # Legacy fallback: use global config
            await self._tick_class(None, now, today)
            return

        default_id = self.plugin._default_class_id()
        for cls in classes:
            cid = str(cls.get("id") or "")
            await self._tick_class(cid if cid else default_id, now, today)

    async def _tick_class(self, class_id: str, now: datetime, today: str) -> None:
        cfg = self.plugin.notify_config(class_id)
        targets = cfg["targets"]
        if not cfg["enabled"] or not targets:
            return

        sessions = self.plugin.store.day(today, class_id)

        # Daily push for this class
        daily_key = self._daily_key(today, class_id)
        if cfg.get("daily_on") and daily_key not in self._daily_sent:
            daily_time = cfg.get("daily_time") or "08:00"
            offset = int(cfg.get("daily_offset", 0))
            push_day = (
                (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset)).strftime("%Y-%m-%d")
                if offset else today
            )
            at = self._target_time(now, daily_time, 0)
            if at is not None:
                delta = (now - at).total_seconds()
                if delta >= 0:
                    self._daily_sent.add(daily_key)
                    logger.info(
                        "[class_notice] 每日课程推送触发 class=%s push_day=%s time=%s offset=%s fmt=%s",
                        class_id or "default", push_day, daily_time, offset, cfg.get("push_format") or "auto",
                    )
                    lines = await self.plugin._send_daily_push(push_day, class_id)
                    for line in lines:
                        logger.info("[class_notice] %s", line)

        if not sessions:
            self._prune(today)
            return

        overrides = cfg.get("overrides") or []

        def _override_for(s):
            for o in overrides:
                ml = str(o.get("match_location") or "")
                ms = str(o.get("match_start") or "")
                if ml and ml.lower() not in str(s.location or "").lower():
                    continue
                if ms and ms != s.start_time:
                    continue
                return o
            return None

        for s in sessions:
            ov = _override_for(s)
            adv = int(ov.get("advance_minutes", cfg["advance"])) if ov else cfg["advance"]
            end_adv = int(ov.get("end_advance_minutes", cfg["end_advance"])) if ov else cfg["end_advance"]
            checks = []
            if cfg["start_on"]:
                checks.append(("start", s.start_time, adv))
            if cfg["end_on"]:
                checks.append(("end", s.end_time, end_adv))
            for kind, hhmm, minutes in checks:
                at = self._target_time(now, hhmm, minutes)
                if at is None:
                    continue
                key = f"{class_id or 'default'}|{today}|{s.start_time}|{s.name}|{kind}"
                if key in self._sent:
                    continue
                delta = (now - at).total_seconds()
                if 0 <= delta <= self.GRACE_SECONDS:
                    self._sent.add(key)
                    await self._push(s, kind, minutes, targets, class_id)

    def _prune(self, today: str) -> None:
        if len(self._sent) > 500:
            self._sent = {k for k in self._sent if today in k}
        if len(self._daily_sent) > 200:
            self._daily_sent = {k for k in self._daily_sent if today in k}

    async def _push(self, session, kind: str, minutes: int, targets: list[str], class_id: str) -> None:
        try:
            text = self.plugin.render_notify(session, kind, minutes)
        except Exception as exc:
            logger.exception("[class_notice] 渲染失败: %s", exc)
            return
        for target in targets:
            try:
                ok = await self.plugin.send_text(target, text)
                logger.info(
                    "[class_notice] 已推送 class=%s (%s) %s %s -> %s ok=%s",
                    class_id or "default", kind,
                    session.day, session.name,
                    target, ok,
                )
            except Exception as exc:
                logger.exception(
                    "[class_notice] 推送 class=%s 到 %s 失败: %s",
                    class_id or "default", target, exc,
                )
