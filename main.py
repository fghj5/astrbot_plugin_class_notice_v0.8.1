from __future__ import annotations

import asyncio as _asyncio
import json as _json
import re
import secrets as _secrets
import threading as _threading
import time as _time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
try:
    import urllib.parse as _urlparse
except Exception:
    _urlparse = None

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Image
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import GreedyStr

from .core import importers, render
from .core.importers import parse_day_input
from .core.scheduler import NotifyScheduler
from .core.store import Session, Store

PLUGIN_NAME = "astrbot_plugin_class_notice"
DEFAULT_FILES = ("timetable.ics", "timetable.csv", "timetable.json", "timetable.txt")
HELP = """📚 课表插件用法（多班级版）

不带班级 = 使用默认班级；带班级 = 指定班级，例如：
/课表 12
/课表 12 明天
/课表 12 状态
/课表 12 每日推送 07:30

基础查询（默认班级）：
/课表               今天
/课表 明天
/课表 本周
/课表 2026-09-16    指定日期（也支持 9/16、9月16日、0916）
/课表 状态          收录情况 + 推送目标 + 班级列表
/课表 帮助

指定班级查询/管理：
/课表 12            班级12今天
/课表 12 明天       班级12明天
/课表 12 状态       班级12状态
/课表 12 测试推送   往班级12推送目标发测试通知（管理员）
/课表 12 立即推送   手动触发班级12当天课程推送（管理员）
/课表 12 每日推送 07:30  班级12每天07:30推送（管理员）
/课表 12 每日推送 关    关闭班级12每日推送（管理员）
/课表 12 每日推送 图片  改为图片优先推送（auto 模式）（管理员）
/课表 12 每日推送 文本  改为纯文本推送（管理员）
/课表 12 导入 [路径]    重新导入班级12课表（管理员）
/课表 12 添加 9/16 15:30-17:10 体育 @游泳馆（管理员）
/课表 12 删除 9/16 15:30（管理员）
/课表 12 清理历史 [YYYY-MM-DD]（管理员，默认今天）
/课表 12 清空       清空班级12课表（管理员）

班级管理（管理员，等价于在配置页操作）：
/课表 班级 列表                          查看所有班级
/课表 班级 添加 13 13班                  新建班级13（自动继承默认推送目标）
/课表 班级 添加 13 13班 07:30            新建班级13并开启每日推送
/课表 班级 添加 13 13班 auto            新建班级13并指定推送格式
/课表 班级 添加 13 13班 沃:GroupMessage:xxx  新建班级13并指定推送目标
/课表 班级 目标 12 沃:GroupMessage:xxx   给班级12增加一个推送目标
/课表 班级 目标 12 删除 沃:GroupMessage:xxx  从班级12移除一个推送目标
/课表 班级 默认 13                        把默认班级改为13
/课表 班级 删除 13                         删除班级13
/课表 班级 复制 13 14                     复制班级13为14
/课表 班级 信息 13                         查看班级13配置摘要
"""




def split_targets(raw) -> list[str]:
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[\n,;，；]+", str(raw or ""))
    out: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


def _is_class_id(token: str) -> bool:
    token=(token or '').strip()
    return bool(re.fullmatch(r'[A-Za-z0-9_\-]+', token))


def split_class_action(raw: str) -> tuple[str, str]:
    """把 /课表 后的内容拆成 (class_id, sub_action)。"""
    text=(raw or '').strip()
    if not text:
        return '', ''
    parts=text.split(maxsplit=1)
    first=parts[0]
    rest=(parts[1] if len(parts)>1 else '').strip()

    known_actions={
        '', '今天','今日','today','明天','明日','tomorrow','本周','周','week','表格','周表','课表','table',
        '状态','status','info','测试推送','测试','test','立即推送','立即每日推送','手动推送','daily_push_now',
        '导入','重载','import','reload','删除','del','remove','添加','新增','add',
        '清理历史','清理','purge','清空','clear','帮助','help','?','-h','h',
        '班级','class','classes',
    }
    if first in known_actions:
        return '', first if first else ''
    if first.startswith('每日推送') or first.startswith('daily_push'):
        return '', first
    if _is_class_id(first):
        return first, rest
    if first in ('班级','class','classes'):
        return '', first + (' ' + rest if rest else '')
    return '', text


def find_class_cfg(classes: list[dict], class_id: str, default_class_id: str = '') -> dict | None:
    if not class_id:
        for c in classes:
            if str(c.get('id','')) == str(default_class_id):
                return c
        return classes[0] if classes else None
    for c in classes:
        if str(c.get('id','')) == str(class_id):
            return c
    return None


def class_cfg_defaults() -> dict:
    return {
        'id': '',
        'name': '',
        'notify_sessions': [],
        'confirm_session': '',
        'advance_minutes': 20,
        'end_advance_minutes': 5,
        'notify_class_start': True,
        'notify_class_end': True,
        'daily_push_enable': False,
        'daily_push_time': '08:00',
        'daily_push_offset_days': 0,
        'import_path': '',
        'manual_entries': [],
        'push_format': 'auto',
        'push_image_title': '',
        'reminder_overrides': [],
    }


def normalize_classes(raw) -> list[dict]:
    out=[]
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            # 面板 template_list 会写入 __template_key 标记字段，忽略它
            if "__template_key" in item:
                item = {k: v for k, v in item.items() if k != "__template_key"}
            if True:
                base=class_cfg_defaults()
                base.update(item)
                base['id']=str(base.get('id') or '').strip()
                base['name']=str(base.get('name') or base['id'])
                base['notify_sessions']=split_targets(base.get('notify_sessions'))
                base['confirm_session']=str(base.get('confirm_session') or '').strip()
                base['import_path']=str(base.get('import_path') or '').strip()
                base['daily_push_time']=str(base.get('daily_push_time') or '08:00').strip()
                try:
                    base['daily_push_offset_days']=max(0,int(base.get('daily_push_offset_days') or 0))
                except Exception:
                    base['daily_push_offset_days']=0
                base['push_format']=str(base.get('push_format') or 'auto').strip()
                base['push_image_title']=str(base.get('push_image_title') or '').strip()
                if isinstance(base.get('advance_minutes'), str):
                    try: base['advance_minutes']=int(base['advance_minutes'])
                    except: base['advance_minutes']=20
                if isinstance(base.get('end_advance_minutes'), str):
                    try: base['end_advance_minutes']=int(base['end_advance_minutes'])
                    except: base['end_advance_minutes']=5
                out.append(base)
    return out


@register(PLUGIN_NAME, "LIU", "导入课表并在上下课前提醒", "v0.9.0")
class ClassNoticePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        try:
            self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as exc:
            logger.warning("[class_notice] 解析数据目录失败，回退插件目录: %s", exc)
            self.data_dir = Path(__file__).resolve().parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir / "timetable.db")
        self.store.init()
        self._manual_key = None
        self._manual_class = None
        self._manual_stat = {"count": 0, "failed": []}
        self.scheduler = NotifyScheduler(self)
        self._register_web_apis()
        self._portal_server = None
        self._portal_thread = None
        self._portal_tokens: dict[str, float] = {}
        try:
            self._main_loop = _asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._main_loop = _asyncio.get_event_loop()
            except Exception:
                self._main_loop = None
        classes=normalize_classes(self.config.get('classes'))
        default_id=str(self.config.get('default_class_id') or '')
        logger.info(
            "[class_notice] 就绪，数据目录=%s，班级数=%d，默认班级=%s",
            self.data_dir, len(classes), default_id or '(first)'
        )

    # ------------------------------------------------------------ 基础

    def sync_manual_entries(self, force: bool = False) -> dict:
        """Backwards compatible: sync default class only."""
        return self._sync_class_manual_entries("", force=force)

    def _classes(self) -> list[dict]:
        return normalize_classes(self.config.get("classes"))

    def _default_class_id(self) -> str:
        return str(self.config.get("default_class_id") or "").strip()

    def _class_cfg(self, class_id: str = "") -> dict | None:
        classes = self._classes()
        if not classes:
            return None
        return find_class_cfg(classes, class_id, self._default_class_id())

    def _class_config(self, class_id: str = "") -> dict:
        """Build a class-level notify config compatible with scheduler/commands."""
        def as_bool(key, default):
            v = self.config.get(key, default)
            return default if v is None else bool(v)
        def as_int(key, default):
            try:
                return int(self.config.get(key, default))
            except Exception:
                return default

        default_id = self._default_class_id()
        c = self._class_cfg(class_id)
        if c:
            targets = split_targets(c.get("notify_sessions"))
            return {
                "class_id": str(c.get("id") or default_id or ""),
                "class_name": str(c.get("name") or c.get("id") or ""),
                "enabled": as_bool("enabled", True),
                "targets": targets,
                "confirm": str(c.get("confirm_session") or "").strip(),
                "advance": abs(as_int("advance_minutes", c.get("advance_minutes", 20))),
                "end_advance": abs(as_int("end_advance_minutes", c.get("end_advance_minutes", 5))),
                "start_on": bool(c.get("notify_class_start", as_bool("notify_class_start", True))),
                "end_on": bool(c.get("notify_class_end", as_bool("notify_class_end", True))),
                "daily_on": bool(c.get("daily_push_enable", as_bool("daily_push_enable", False))),
                "daily_time": str(c.get("daily_push_time") or "08:00").strip(),
                "daily_offset": max(0, int(c.get("daily_push_offset_days") or 0)),
                "push_format": str(c.get("push_format") or "auto").strip() or "auto",
                "push_image_title": str(c.get("push_image_title") or c.get("name") or c.get("id") or "课程").strip(),
                "overrides": c.get("reminder_overrides") or [],
            }
        # fallback to legacy global config
        targets = split_targets(self.config.get("notify_sessions"))
        if not targets:
            targets = split_targets(self.config.get("notify_session"))
        return {
            "class_id": default_id or "",
            "class_name": "",
            "enabled": as_bool("enabled", True),
            "targets": targets,
            "confirm": str(self.config.get("confirm_session") or "").strip(),
            "advance": abs(as_int("advance_minutes", 20)),
            "end_advance": abs(as_int("end_advance_minutes", 5)),
            "start_on": as_bool("notify_class_start", True),
            "end_on": as_bool("notify_class_end", True),
            "daily_on": as_bool("daily_push_enable", False),
            "daily_time": str(self.config.get("daily_push_time") or "08:00").strip(),
            "daily_offset": max(0, int(self.config.get("daily_push_offset_days") or 0)),
            "push_format": str(self.config.get("push_format") or "auto").strip() or "auto",
            "push_image_title": str(self.config.get("push_image_title") or "课程").strip(),
            "overrides": self.config.get("reminder_overrides") or [],
        }

    def _class_manual_lines(self, class_id: str = "") -> list[str]:
        c = self._class_cfg(class_id)
        if not c:
            return []
        raw = c.get("manual_entries")
        if isinstance(raw, str):
            return [x for x in raw.splitlines() if x.strip()]
        if isinstance(raw, (list, tuple)):
            return [str(x) for x in raw]
        return []

    def _sync_class_manual_entries(self, class_id: str = "", force: bool = False) -> dict:
        cid = self._class_config(class_id)["class_id"]
        lines = self._class_manual_lines(class_id)
        # v3：手动日程以数据库为准。配置里没有手动日程时，不覆盖数据库。
        if not lines:
            return self._manual_stat or {"count": 0, "failed": []}
        key = f"{cid}|\n".join(lines)
        if not force and key == self._manual_key and cid == (self._manual_class or ""):
            return self._manual_stat
        sessions, bad = importers.parse_manual_lines(
            lines, self._tz_name(), today=self.now().date()
        )
        for s in sessions:
            s.class_id = cid
        try:
            count = self.store.replace_manual(sessions, cid)
        except Exception as exc:
            logger.exception("[class_notice] 同步班级 %s 手动日程失败: %s", cid, exc)
            return self._manual_stat
        self._manual_key = key
        self._manual_class = cid
        self._manual_stat = {"count": count, "failed": bad}
        # 迁移成功：清空配置里的 manual_entries，让配置文件只承担「班级管理」职责
        try:
            for cls in self.config.get("classes") or []:
                if str(cls.get("id")) == str(cid) and cls.get("manual_entries"):
                    cls["manual_entries"] = []
                    self.config.save_config()
                    logger.info("[class_notice] 班级 %s 手动日程已迁入数据库，配置项已清空", cid)
        except Exception as exc:
            logger.warning("[class_notice] 清空配置 manual_entries 失败: %s", exc)
        if bad:
            logger.warning(
                "[class_notice] 班级 %s 手动日程有 %d 条无法解析（已忽略）: %s",
                cid, len(bad), bad,
            )
        else:
            logger.info("[class_notice] 班级 %s 手动日程已同步: %d 条", cid, count)
        return self._manual_stat

    # ------------------------------------------------------------ 可视化课表（按周）

    def _week_table_for(self, class_id: str, anchor: str) -> str:
        today = self._today()
        start_dt = datetime.strptime(anchor, "%Y-%m-%d") - timedelta(days=datetime.strptime(anchor, "%Y-%m-%d").weekday())
        end_dt = start_dt + timedelta(days=6)
        rows = self.store.between(start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"), class_id)
        return render.week_table(anchor, rows)

    # ------------------------------------------------------------ 可复用业务方法（面板 API 与门户服务共用）

    def _svc_get_timetable(self, class_id: str = "", week: str = "") -> dict:
        class_id = class_id or self._default_class_id()
        week = week or self._today()
        try:
            start_dt = datetime.strptime(week, "%Y-%m-%d") - timedelta(days=datetime.strptime(week, "%Y-%m-%d").weekday())
        except ValueError:
            start_dt = datetime.strptime(self._today(), "%Y-%m-%d") - timedelta(days=datetime.strptime(self._today(), "%Y-%m-%d").weekday())
        end_dt = start_dt + timedelta(days=6)
        rows = self.store.between(start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"), class_id)
        classes = [
            {"id": str(c.get("id") or ""), "name": str(c.get("name") or c.get("id") or "")}
            for c in self._classes()
        ]
        return {
            "class_id": class_id,
            "week_start": start_dt.strftime("%Y-%m-%d"),
            "week_end": end_dt.strftime("%Y-%m-%d"),
            "classes": classes,
            "sessions": [
                {
                    "uid": s.uid, "day": s.day, "start": s.start_time, "end": s.end_time,
                    "name": s.name, "location": s.location, "teacher": s.teacher, "source": s.source,
                }
                for s in rows
            ],
        }

    def _svc_post_session(self, body: dict) -> dict:
        action = str(body.get("_action") or "add").lower()
        class_id = str(body.get("class_id") or self._default_class_id())
        if action in ("update", "edit", "modify"):
            uid = str(body.get("uid") or "").strip()
            s = self.store.get_by_uid(uid, class_id)
            if not s:
                return {"ok": False, "message": "记录不存在或已被删除"}
            self.store.delete_by_uid(uid, class_id)
            new_s = Session(
                day=str(body.get("day") or s.day),
                start_time=str(body.get("start") or s.start_time),
                end_time=str(body.get("end") or s.end_time),
                name=str(body.get("name") or s.name),
                location=str(body.get("location") or s.location),
                teacher=str(body.get("teacher") or s.teacher),
                source=s.source, class_id=class_id,
            )
            self.store.add_sessions([new_s], class_id)
            return {"ok": True, "message": "修改成功", "uid": new_s.uid}
        if action in ("delete", "del", "remove"):
            uid = str(body.get("uid") or "").strip()
            if not uid:
                return {"ok": False, "message": "缺少 uid"}
            n = self.store.delete_by_uid(uid, class_id)
            return {"ok": n > 0, "message": f"已删除 {n} 条" if n else "记录不存在"}
        # add
        day = str(body.get("day") or "").strip()
        start = str(body.get("start") or "").strip()
        end = str(body.get("end") or "").strip()
        name = str(body.get("name") or "").strip()
        if not all([day, start, end, name]):
            return {"ok": False, "message": "缺少必填字段：day / start / end / name"}
        s = Session(day=day, start_time=start, end_time=end, name=name,
                    location=str(body.get("location") or "").strip(),
                    teacher=str(body.get("teacher") or "").strip(),
                    source="cmd", class_id=class_id)
        self.store.add_sessions([s], class_id)
        return {"ok": True, "message": f"已添加 {class_id} 班 {day} {start}-{end} {name}", "uid": s.uid}

    def _svc_get_classes(self) -> dict:
        classes = []
        for c in self._classes():
            cid = str(c.get("id") or "")
            classes.append({
                "id": cid,
                "name": str(c.get("name") or cid),
                "is_default": cid == self._default_class_id(),
                "notify_sessions": split_targets(c.get("notify_sessions")),
                "confirm_session": str(c.get("confirm_session") or ""),
                "advance_minutes": int(c.get("advance_minutes") or 20),
                "end_advance_minutes": int(c.get("end_advance_minutes") or 5),
                "notify_class_start": bool(c.get("notify_class_start", True)),
                "notify_class_end": bool(c.get("notify_class_end", True)),
                "daily_push_enable": bool(c.get("daily_push_enable", False)),
                "daily_push_time": str(c.get("daily_push_time") or "08:00"),
                "daily_push_offset_days": int(c.get("daily_push_offset_days") or 0),
                "push_format": str(c.get("push_format") or "auto"),
                "push_image_title": str(c.get("push_image_title") or ""),
                "session_count": self.store.count(cid),
                "reminder_overrides": c.get("reminder_overrides") or [],
            })
        return {"classes": classes, "default_class_id": self._default_class_id()}

    async def _push_test_coro(self, targets: list, text: str) -> list:
        out = []
        for target in targets:
            try:
                ok = await self.send_text(target, text)
                out.append(f"{'✅' if ok else '❌'} {target}")
            except Exception as exc:
                logger.warning("[class_notice] 门户测试推送到 %s 失败: %s", target, exc)
                out.append(f"❌ {target}（{exc}）")
        return out

    async def _dispatch_async_send(self, targets: list, text: str) -> list:
        """按调用环境调度发送：门户线程 → 主事件循环；面板/AstrBot 内 → 直接 await。"""
        try:
            cur = _asyncio.get_running_loop()
        except RuntimeError:
            cur = None
        if self._main_loop and cur is not self._main_loop:
            fut = _asyncio.run_coroutine_threadsafe(self._push_test_coro(targets, text), self._main_loop)
            return fut.result(timeout=30)
        return await self._push_test_coro(targets, text)

    async def _svc_post_classes(self, body: dict) -> dict:
        action = str(body.get("_action") or "list").lower()
        classes = list(self._classes())
        def _find(cid):
            for c in classes:
                if str(c.get("id")) == str(cid):
                    return c
            return None
        if action in ("add", "new", "create"):
            cid = str(body.get("id") or "").strip()
            if not cid:
                return {"ok": False, "message": "缺少班级编号 id"}
            if _find(cid):
                return {"ok": False, "message": f"班级 {cid} 已存在"}
            new_cls = class_cfg_defaults()
            new_cls.update({
                "id": cid, "name": str(body.get("name") or cid),
                "notify_sessions": split_targets(body.get("notify_sessions")),
                "confirm_session": str(body.get("confirm_session") or ""),
                "advance_minutes": int(body.get("advance_minutes") or 20),
                "end_advance_minutes": int(body.get("end_advance_minutes") or 5),
                "daily_push_enable": bool(body.get("daily_push_enable", False)),
                "daily_push_time": str(body.get("daily_push_time") or "08:00"),
                "daily_push_offset_days": int(body.get("daily_push_offset_days") or 0),
                "push_format": str(body.get("push_format") or "auto"),
                "push_image_title": str(body.get("push_image_title") or ""),
                "reminder_overrides": body.get("reminder_overrides") or [],
                "__template_key": "class",
            })
            self.store._ensure_tables(cid)
            classes.append(new_cls)
            self.config["classes"] = classes
            self._save_classes_config()
            return {"ok": True, "message": f"已添加班级 {cid}"}
        if action in ("update", "edit"):
            cid = str(body.get("id") or "").strip()
            c = _find(cid)
            if not c:
                return {"ok": False, "message": f"班级 {cid} 不存在"}
            c.setdefault("__template_key", "class")
            c.update({
                "name": str(body.get("name") or cid),
                "notify_sessions": split_targets(body.get("notify_sessions")),
                "confirm_session": str(body.get("confirm_session") or ""),
                "advance_minutes": int(body.get("advance_minutes") or 20),
                "end_advance_minutes": int(body.get("end_advance_minutes") or 5),
                "notify_class_start": bool(body.get("notify_class_start", c.get("notify_class_start", True))),
                "notify_class_end": bool(body.get("notify_class_end", c.get("notify_class_end", True))),
                "daily_push_enable": bool(body.get("daily_push_enable", c.get("daily_push_enable", False))),
                "daily_push_time": str(body.get("daily_push_time") or "08:00"),
                "daily_push_offset_days": int(body.get("daily_push_offset_days") or 0),
                "push_format": str(body.get("push_format") or "auto"),
                "push_image_title": str(body.get("push_image_title") or ""),
                "reminder_overrides": body.get("reminder_overrides") or [],
            })
            self.config["classes"] = classes
            self._save_classes_config()
            return {"ok": True, "message": f"班级 {cid} 已更新"}
        if action in ("delete", "del", "remove"):
            cid = str(body.get("id") or "").strip()
            c = _find(cid)
            if not c:
                return {"ok": False, "message": f"班级 {cid} 不存在"}
            kept = [x for x in classes if str(x.get("id")) != str(cid)]
            self.config["classes"] = kept
            if self._default_class_id() == cid:
                self.config["default_class_id"] = kept[0].get("id") if kept else ""
            self.store.drop_class(cid)
            self._save_classes_config()
            return {"ok": True, "message": f"班级 {cid} 已删除（数据库同步清理）"}
        if action in ("push_now", "立即推送", "立即"):
            cid = str(body.get("id") or "").strip()
            c = _find(cid)
            if not c:
                return {"ok": False, "message": f"班级 {cid} 不存在"}
            targets = split_targets(c.get("notify_sessions"))
            if not targets:
                return {"ok": False, "message": "该班级未配置推送目标"}
            today = self._today()
            try:
                cur = _asyncio.get_running_loop()
            except RuntimeError:
                cur = None
            if self._main_loop and cur is not self._main_loop:
                fut = _asyncio.run_coroutine_threadsafe(self._send_daily_push(today, cid), self._main_loop)
                lines = fut.result(timeout=30)
            else:
                lines = await self._send_daily_push(today, cid)
            return {"ok": True, "message": "\n".join(lines) if lines else "（无推送目标，未发送）"}

        if action in ("test_push", "test", "测试推送"):
            cid = str(body.get("id") or "").strip()
            c = _find(cid)
            if not c:
                return {"ok": False, "message": f"班级 {cid} 不存在"}
            targets = split_targets(c.get("notify_sessions"))
            if not targets:
                return {"ok": False, "message": "该班级未配置推送目标"}
            cfg = self._class_config(cid)
            text = (
                f"🧪 测试推送 · {cfg.get('class_name') or cid}\n"
                f"目标 {len(targets)} 个\n"
                f"格式 {cfg.get('push_format') or 'auto'} · 每日 "
                + (str(cfg.get('daily_time')) if cfg.get('daily_on') else '关')
                + f" · 推送内容 {'明天课程' if cfg.get('daily_offset') == 1 else '当天课程'}"
            )
            results = await self._dispatch_async_send(targets, text)
            return {"ok": True, "message": "\n".join(results)}

        if action in ("copy", "clone", "duplicate"):
            src = str(body.get("src") or body.get("id") or "").strip()
            dst = str(body.get("dst") or body.get("new_id") or "").strip()
            if not src or not dst:
                return {"ok": False, "message": "需要 src 和 dst"}
            if not _find(src):
                return {"ok": False, "message": f"源班级 {src} 不存在"}
            if _find(dst):
                return {"ok": False, "message": f"目标班级 {dst} 已存在"}
            src_c = dict(_find(src))
            src_c["id"] = dst
            src_c["name"] = dst
            src_c["__template_key"] = "class"
            try:
                n = self.store.copy_class(src, dst)
                data_msg = f"，已复制 {n} 条课程" if n else ""
            except Exception as exc:
                data_msg = "（数据复制失败）"
                logger.warning("[class_notice] 复制班级数据失败: %s", exc)
            classes.append(src_c)
            self.config["classes"] = classes
            self._save_classes_config()
            return {"ok": True, "message": f"已复制 {src} → {dst}{data_msg}"}
        if action in ("default", "set-default"):
            cid = str(body.get("id") or "").strip()
            if not _find(cid):
                return {"ok": False, "message": f"班级 {cid} 不存在"}
            self.config["default_class_id"] = cid
            self._save_classes_config()
            return {"ok": True, "message": f"默认班级已设为 {cid}"}
        return {"ok": False, "message": f"未知操作 {action}"}

    def _svc_get_settings(self) -> dict:
        return {
            "enabled": bool(self.config.get("enabled", True)),
            "timezone": str(self.config.get("timezone") or "Asia/Shanghai"),
            "default_class_id": str(self.config.get("default_class_id") or ""),
            "confirm_session": str(self.config.get("confirm_session") or ""),
            "advance_minutes": int(self.config.get("advance_minutes") or 20),
            "end_advance_minutes": int(self.config.get("end_advance_minutes") or 5),
            "notify_class_start": bool(self.config.get("notify_class_start", True)),
            "notify_class_end": bool(self.config.get("notify_class_end", True)),
            "daily_push_time": str(self.config.get("daily_push_time") or "08:00"),
            "daily_push_enable": bool(self.config.get("daily_push_enable", False)),
            "push_format": str(self.config.get("push_format") or "auto"),
            "push_image_title": str(self.config.get("push_image_title") or ""),
        }

    def _svc_post_settings(self, body: dict) -> dict:
        allowed = ["enabled", "timezone", "default_class_id", "confirm_session",
                   "advance_minutes", "end_advance_minutes", "notify_class_start",
                   "notify_class_end", "daily_push_time", "daily_push_enable",
                   "push_format", "push_image_title"]
        for k in allowed:
            if k in body:
                self.config[k] = body[k]
        try:
            self.config.save_config()
        except Exception as exc:
            logger.warning("[class_notice] 保存全局设置失败: %s", exc)
            return {"ok": False, "message": f"保存失败: {exc}"}
        return {"ok": True, "message": "全局设置已保存（重载插件后完全生效）"}

    # ------------------------------------------------------------ 独立管理门户（不进入 AstrBot 面板）

    def _portal_cfg(self) -> dict:
        def _g(k, d):
            try:
                v = self.config.get(k, d)
                return d if v is None or v == "" else v
            except Exception:
                return d
        allowed = []
        try:
            raw = self.config.get("manage_class_ids", [])
            if isinstance(raw, (list, tuple)):
                allowed = [str(x) for x in raw if str(x).strip()]
        except Exception:
            allowed = []
        return {
            "enable": bool(_g("manage_enable", True)),
            "port": int(_g("manage_port", 8321)),
            "user": str(_g("manage_user", "root")),
            "password": str(_g("manage_password", "root")),
            "full_user": str(_g("manage_full_user", "")),
            "full_password": str(_g("manage_full_password", "")),
            "allowed": allowed,
        }

    def _portal_start(self) -> None:
        cfg = self._portal_cfg()
        if not cfg["enable"]:
            logger.info("[class_notice] 管理门户未启用（manage_enable=false）")
            return
        if self._portal_server:
            return
        try:
            # 强制重新加载 portal 模块：AstrBot 重载插件时不会清除 sys.modules 里
            # 被动态 import 的子模块，不 reload 会导致门户一直跑旧代码。
            import importlib
            import sys as _sys
            _pkg = __name__.rsplit(".", 1)[0]  # data.plugins.astrbot_plugin_class_notice
            _portal_mod = importlib.import_module(f"{_pkg}.portal")
            importlib.reload(_portal_mod)
            run_portal_server = _portal_mod.run_portal_server
            self._portal_server, self._portal_thread = run_portal_server(
                bind_host="0.0.0.0",
                port=cfg["port"],
                username=cfg["user"],
                password=cfg["password"],
                full_user=cfg.get("full_user", ""),
                full_password=cfg.get("full_password", ""),
                allowed=cfg.get("allowed", []),
                plugin=self,
            )
            logger.info(
                "[class_notice] 管理门户已启动: http://0.0.0.0:%s （账号 %s），仅课表管理，不含 AstrBot 面板",
                cfg["port"], cfg["user"],
            )
        except Exception as exc:
            logger.exception("[class_notice] 管理门户启动失败: %s", exc)

    def _portal_stop(self) -> None:
        # 注意：不要调用 server.shutdown()（会阻塞等待 serve_forever 退出，
        # 在 asyncio 线程中可能卡死并留下僵尸监听）。直接 server_close()
        # 关闭监听 socket，daemon 线程会因 accept 失败自行退出。
        if self._portal_server:
            try:
                self._portal_server.server_close()
            except Exception:
                pass
            self._portal_server = None
        if self._portal_thread and self._portal_thread.is_alive():
            try:
                self._portal_thread.join(timeout=2)
            except Exception:
                pass
        self._portal_thread = None
        self._portal_tokens.clear()

    def _register_web_apis(self) -> None:
        """注册面板可视化课表页面的 Web API。"""
        from astrbot.api.web import request as plugin_request

        async def api_get_timetable() -> dict:
            class_id = plugin_request.query.get("class_id", "") or self._default_class_id()
            week = plugin_request.query.get("week", "") or self._today()
            start_dt = datetime.strptime(week, "%Y-%m-%d") - timedelta(days=datetime.strptime(week, "%Y-%m-%d").weekday())
            end_dt = start_dt + timedelta(days=6)
            rows = self.store.between(start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"), class_id)
            classes = [
                {"id": str(c.get("id") or ""), "name": str(c.get("name") or c.get("id") or "")}
                for c in self._classes()
            ]
            return {
                "class_id": class_id,
                "week_start": start_dt.strftime("%Y-%m-%d"),
                "week_end": end_dt.strftime("%Y-%m-%d"),
                "classes": classes,
                "sessions": [
                    {
                        "uid": s.uid,
                        "day": s.day,
                        "start": s.start_time,
                        "end": s.end_time,
                        "name": s.name,
                        "location": s.location,
                        "teacher": s.teacher,
                        "source": s.source,
                    }
                    for s in rows
                ],
            }

        async def api_add_session(body: dict) -> dict:
            class_id = str(body.get("class_id") or self._default_class_id())
            day = str(body.get("day") or "").strip()
            start = str(body.get("start") or "").strip()
            end = str(body.get("end") or "").strip()
            name = str(body.get("name") or "").strip()
            location = str(body.get("location") or "").strip()
            teacher = str(body.get("teacher") or "").strip()
            if not all([day, start, end, name]):
                return {"ok": False, "message": "缺少必填字段：day / start / end / name"}
            s = Session(day=day, start_time=start, end_time=end, name=name, location=location, teacher=teacher, source="cmd", class_id=class_id)
            self.store.add_sessions([s], class_id)
            return {"ok": True, "message": f"已添加 {class_id} 班 {day} {start}-{end} {name}", "uid": s.uid}

        async def api_update_session(body: dict) -> dict:
            class_id = str(body.get("class_id") or self._default_class_id())
            uid = str(body.get("uid") or "").strip()
            s = self.store.get_by_uid(uid, class_id)
            if not s:
                return {"ok": False, "message": "记录不存在或已被删除"}
            self.store.delete_by_uid(uid, class_id)
            new_s = Session(
                day=str(body.get("day") or s.day),
                start_time=str(body.get("start") or s.start_time),
                end_time=str(body.get("end") or s.end_time),
                name=str(body.get("name") or s.name),
                location=str(body.get("location") or s.location),
                teacher=str(body.get("teacher") or s.teacher),
                source=s.source,
                class_id=class_id,
            )
            self.store.add_sessions([new_s], class_id)
            return {"ok": True, "message": "修改成功", "uid": new_s.uid}

        async def api_delete_session(body: dict) -> dict:
            class_id = str(body.get("class_id") or self._default_class_id())
            uid = str(body.get("uid") or "").strip()
            if not uid:
                return {"ok": False, "message": "缺少 uid"}
            n = self.store.delete_by_uid(uid, class_id)
            return {"ok": n > 0, "message": f"已删除 {n} 条" if n else "记录不存在"}

        async def api_post_session() -> dict:
            """统一 POST 入口：按 body._action 分发新增/修改/删除。"""
            body = await plugin_request.json(default={}) or {}
            action = str(body.get("_action") or "add").lower()
            if action in ("update", "edit", "modify"):
                return await api_update_session(body)
            if action in ("delete", "del", "remove"):
                return await api_delete_session(body)
            return await api_add_session(body)

        # 注意：面板前端会把 endpoint 拼成 /api/v1/plugins/extensions/<插件名>/<endpoint>，
        # 因此注册的路由必须以插件名为前缀，否则匹配不到。
        base = "/astrbot_plugin_class_notice/class-notice/api"
        self.context.register_web_api(f"{base}/timetable", api_get_timetable, ["GET"], "获取班级按周课表数据")
        self.context.register_web_api(f"{base}/session", api_post_session, ["POST"], "新增/修改/删除课程（_action 分发）")
        self.context.register_web_api(f"{base}/classes", self._api_get_classes, ["GET"], "班级列表")
        self.context.register_web_api(f"{base}/classes", self._api_post_classes, ["POST"], "新增/修改/删除/复制/设默认班级（_action 分发）")
        self.context.register_web_api(f"{base}/settings", self._api_get_settings, ["GET"], "读取全局设置")
        self.context.register_web_api(f"{base}/settings", self._api_post_settings, ["POST"], "保存全局设置")

    # ------------------------------------------------------------ 班级管理 Web API

    def _save_classes_config(self) -> None:
        try:
            self.config.save_config()
            logger.info("[class_notice] 班级配置已保存")
        except Exception as exc:
            logger.warning("[class_notice] 保存班级配置失败: %s", exc)

    async def _api_get_classes(self) -> dict:
        from astrbot.api.web import request as plugin_request
        classes = []
        for c in self._classes():
            cid = str(c.get("id") or "")
            classes.append({
                "id": cid,
                "name": str(c.get("name") or cid),
                "is_default": cid == self._default_class_id(),
                "notify_sessions": split_targets(c.get("notify_sessions")),
                "confirm_session": str(c.get("confirm_session") or ""),
                "advance_minutes": int(c.get("advance_minutes") or 20),
                "end_advance_minutes": int(c.get("end_advance_minutes") or 5),
                "notify_class_start": bool(c.get("notify_class_start", True)),
                "notify_class_end": bool(c.get("notify_class_end", True)),
                "daily_push_enable": bool(c.get("daily_push_enable", False)),
                "daily_push_time": str(c.get("daily_push_time") or "08:00"),
                "daily_push_offset_days": int(c.get("daily_push_offset_days") or 0),
                "push_format": str(c.get("push_format") or "auto"),
                "push_image_title": str(c.get("push_image_title") or ""),
                "session_count": self.store.count(cid),
            })
        return {"classes": classes, "default_class_id": self._default_class_id()}

    async def _api_post_classes(self) -> dict:
        from astrbot.api.web import request as plugin_request
        body = await plugin_request.json(default={}) or {}
        action = str(body.get("_action") or "list").lower()
        classes = list(self._classes())

        def _find(cid):
            for c in classes:
                if str(c.get("id")) == str(cid):
                    return c
            return None

        if action in ("add", "new", "create"):
            cid = str(body.get("id") or "").strip()
            if not cid:
                return {"ok": False, "message": "缺少班级编号 id"}
            if _find(cid):
                return {"ok": False, "message": f"班级 {cid} 已存在"}
            new_cls = class_cfg_defaults()
            new_cls.update({
                "id": cid,
                "name": str(body.get("name") or cid),
                "notify_sessions": split_targets(body.get("notify_sessions")),
                "confirm_session": str(body.get("confirm_session") or ""),
                "advance_minutes": int(body.get("advance_minutes") or 20),
                "end_advance_minutes": int(body.get("end_advance_minutes") or 5),
                "daily_push_enable": bool(body.get("daily_push_enable", False)),
                "daily_push_time": str(body.get("daily_push_time") or "08:00"),
                "daily_push_offset_days": int(body.get("daily_push_offset_days") or 0),
                "push_format": str(body.get("push_format") or "auto"),
                "push_image_title": str(body.get("push_image_title") or ""),
                "__template_key": "class",
            })
            self.store._ensure_tables(cid)
            classes.append(new_cls)
            self.config["classes"] = classes
            self._save_classes_config()
            return {"ok": True, "message": f"已添加班级 {cid}"}

        if action in ("update", "edit"):
            cid = str(body.get("id") or "").strip()
            c = _find(cid)
            if not c:
                return {"ok": False, "message": f"班级 {cid} 不存在"}
            fields = {
                "name": str(body.get("name") or cid),
                "notify_sessions": split_targets(body.get("notify_sessions")),
                "confirm_session": str(body.get("confirm_session") or ""),
                "advance_minutes": int(body.get("advance_minutes") or 20),
                "end_advance_minutes": int(body.get("end_advance_minutes") or 5),
                "notify_class_start": bool(body.get("notify_class_start", c.get("notify_class_start", True))),
                "notify_class_end": bool(body.get("notify_class_end", c.get("notify_class_end", True))),
                "daily_push_enable": bool(body.get("daily_push_enable", c.get("daily_push_enable", False))),
                "daily_push_time": str(body.get("daily_push_time") or "08:00"),
                "daily_push_offset_days": int(body.get("daily_push_offset_days") or 0),
                "push_format": str(body.get("push_format") or "auto"),
                "push_image_title": str(body.get("push_image_title") or ""),
            }
            c.update(fields)
            self.config["classes"] = classes
            self._save_classes_config()
            return {"ok": True, "message": f"班级 {cid} 已更新"}

        if action in ("delete", "del", "remove"):
            cid = str(body.get("id") or "").strip()
            c = _find(cid)
            if not c:
                return {"ok": False, "message": f"班级 {cid} 不存在"}
            kept = [x for x in classes if str(x.get("id")) != str(cid)]
            self.config["classes"] = kept
            if self._default_class_id() == cid:
                self.config["default_class_id"] = kept[0].get("id") if kept else ""
            self.store.drop_class(cid)
            self._save_classes_config()
            return {"ok": True, "message": f"班级 {cid} 已删除（数据库同步清理）"}

        if action in ("copy", "clone", "duplicate"):
            src = str(body.get("src") or body.get("id") or "").strip()
            dst = str(body.get("dst") or body.get("new_id") or "").strip()
            if not src or not dst:
                return {"ok": False, "message": "需要 src 和 dst"}
            if not _find(src):
                return {"ok": False, "message": f"源班级 {src} 不存在"}
            if _find(dst):
                return {"ok": False, "message": f"目标班级 {dst} 已存在"}
            src_c = dict(_find(src))
            src_c["id"] = dst
            src_c["name"] = dst
            src_c["__template_key"] = "class"
            try:
                n = self.store.copy_class(src, dst)
                data_msg = f"，已复制 {n} 条课程" if n else ""
            except Exception as exc:
                data_msg = "（数据复制失败）"
                logger.warning("[class_notice] 复制班级数据失败: %s", exc)
            classes.append(src_c)
            self.config["classes"] = classes
            self._save_classes_config()
            return {"ok": True, "message": f"已复制 {src} → {dst}{data_msg}"}

        if action in ("default", "set-default"):
            cid = str(body.get("id") or "").strip()
            if not _find(cid):
                return {"ok": False, "message": f"班级 {cid} 不存在"}
            self.config["default_class_id"] = cid
            self._save_classes_config()
            return {"ok": True, "message": f"默认班级已设为 {cid}"}

        return {"ok": False, "message": f"未知操作 {action}"}

    # ------------------------------------------------------------ 全局设置 Web API

    async def _api_get_settings(self) -> dict:
        return {
            "enabled": bool(self.config.get("enabled", True)),
            "timezone": str(self.config.get("timezone") or "Asia/Shanghai"),
            "default_class_id": str(self.config.get("default_class_id") or ""),
            "confirm_session": str(self.config.get("confirm_session") or ""),
            "advance_minutes": int(self.config.get("advance_minutes") or 20),
            "end_advance_minutes": int(self.config.get("end_advance_minutes") or 5),
            "notify_class_start": bool(self.config.get("notify_class_start", True)),
            "notify_class_end": bool(self.config.get("notify_class_end", True)),
            "daily_push_time": str(self.config.get("daily_push_time") or "08:00"),
            "daily_push_enable": bool(self.config.get("daily_push_enable", False)),
            "push_format": str(self.config.get("push_format") or "auto"),
            "push_image_title": str(self.config.get("push_image_title") or ""),
        }

    async def _api_post_settings(self) -> dict:
        from astrbot.api.web import request as plugin_request
        body = await plugin_request.json(default={}) or {}
        allowed = ["enabled", "timezone", "default_class_id", "confirm_session",
                   "advance_minutes", "end_advance_minutes", "notify_class_start",
                   "notify_class_end", "daily_push_time", "daily_push_enable",
                   "push_format", "push_image_title"]
        for k in allowed:
            if k in body:
                self.config[k] = body[k]
        try:
            self.config.save_config()
        except Exception as exc:
            logger.warning("[class_notice] 保存全局设置失败: %s", exc)
            return {"ok": False, "message": f"保存失败: {exc}"}
        return {"ok": True, "message": "全局设置已保存（重载插件后完全生效）"}

    def _tz_name(self) -> str:
        try:
            value = self.config.get("timezone", importers.DEFAULT_TZ)
        except Exception:
            value = importers.DEFAULT_TZ
        return str(value or importers.DEFAULT_TZ)

    def now(self) -> datetime:
        return datetime.now(importers.get_tz(self._tz_name()))

    def _today(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    def notify_config(self, class_id: str = "") -> dict:
        """Class-level config; empty class_id means default class."""
        return self._class_config(class_id)

    def render_notify(self, session: Session, kind: str, minutes: int) -> str:
        return render.notify_text(session, kind, minutes)

    async def _render_daily_push_image(self, day: str, class_id: str = "") -> str | None:
        """将当天课程渲染为图片，返回图片路径/URL；失败返回 None。"""
        cfg = self.notify_config(class_id)
        title = cfg.get("push_image_title") or cfg.get("class_name") or "课程"
        text = self._render_day(day, class_id)
        # 添加标题
        image_text = f"{title}\n{text}"
        try:
            result = await self.text_to_image(image_text, return_url=False)
            if result:
                return str(result)
            return None
        except Exception as exc:
            logger.warning("[class_notice] 班级 %s 图片渲染失败，回退文本: %s", class_id or "默认", exc)
            return None

    async def _send_daily_push(self, day: str, class_id: str = "") -> list[str]:
        """执行每日推送（图片优先、失败自动回退文本），返回各目标发送结果。"""
        cfg = self.notify_config(class_id)
        targets = cfg["targets"]
        if not targets:
            return []
        text = self._render_day(day, class_id)
        fmt = str(cfg.get("push_format") or "auto").lower()

        # 尝试生成图片
        image_path = None
        if fmt in ("image", "auto"):
            image_path = await self._render_daily_push_image(day, class_id)

        lines = []
        for target in targets:
            sent_ok = False
            if image_path:
                try:
                    ok = await self.send_image(target, image_path)
                    sent_ok = bool(ok)
                    if not sent_ok:
                        logger.warning("[class_notice] 图片发送到 %s 失败，回退文本", target)
                except Exception as exc:
                    logger.warning("[class_notice] 图片发送到 %s 异常，回退文本: %s", target, exc)

            if not sent_ok:
                try:
                    ok = await self.send_text(target, text)
                    sent_ok = bool(ok)
                except Exception as exc:
                    logger.exception("[class_notice] 文本推送到 %s 失败: %s", target, exc)

            if image_path and sent_ok:
                lines.append(f"✅ {target}（图片）")
            elif sent_ok:
                lines.append(f"✅ {target}（文本）")
            else:
                lines.append(f"❌ 发送失败 {target}")
        return lines


    async def send_text(self, target: str, text: str) -> bool:
        chain = MessageChain([Plain(text)])
        chain.use_t2i_ = False
        return await self.context.send_message(target, chain)

    async def send_image(self, target: str, image_path: str) -> bool:
        chain = MessageChain([Image(file=image_path)])
        chain.use_t2i_ = False
        return await self.context.send_message(target, chain)

    def _push_cfg_summary(self, class_id: str, cfg: dict) -> str:
        lines = [
            "当前生效配置：",
            "  推送目标：",
        ]
        targets = cfg.get("targets") or []
        lines.extend(f"    {x}" for x in targets) if targets else lines.append("    （未配置）")
        lines.append(f"  确认会话：{cfg.get('confirm') or '随指令所在会话'}")
        lines.append(f"  每日推送：{'开 ' + str(cfg.get('daily_time')) if cfg.get('daily_on') else '关'}")
        lines.append(f"  推送格式：{cfg.get('push_format') or 'auto'}")
        lines.append(f"  图片标题：{cfg.get('push_image_title') or cfg.get('class_name') or '（默认）'}")
        off=int(cfg.get('daily_offset') or 0)
        if off==0:
            day_hint='当天课程'
        elif off==1:
            day_hint='明天课程'
        else:
            day_hint=f'第 {off+1} 天课程'
        lines.append(f"  推送内容：{day_hint}")
        return "\n".join(lines)

    def _trailing(self, event: AstrMessageEvent, keyword: str) -> str:
        raw = (event.message_str or "").strip()
        pos = raw.find(keyword)
        if pos < 0:
            return ""
        return raw[pos + len(keyword):].strip().strip('"').strip("'")

    def _render_day(self, day: str, class_id: str = "", with_hint: bool = False) -> str:
        sessions = self.store.day(day, class_id)
        if not sessions:
            body = f"📅 {render.cn_day(day)}\n没有课，好好休息～"
        else:
            body = render.day_block(day, sessions)
        if with_hint:
            return f"查询日期：{day}\n{body}"
        return body

    def _parse_day(self, text: str) -> str:
        return parse_day_input(text, self.now().date())

    def _resolve_day(self, token: str) -> str:
        """今天 / 明天 / 后天 / 各种日期写法 -> YYYY-MM-DD。"""
        token = (token or "").strip()
        offsets = {"今天": 0, "today": 0, "明天": 1, "tomorrow": 1, "后天": 2}
        if token in offsets:
            return (self.now().date() + timedelta(days=offsets[token])).isoformat()
        return parse_day_input(token, self.now().date())

    @staticmethod
    def _norm_hhmm(text: str) -> str:
        hit = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$", text or "")
        return f"{int(hit.group(1)):02d}:{hit.group(2)}" if hit else ""

    def _normalize_add(self, raw: str) -> str:
        """把行首的 今天/明天/后天 换成真实日期；日期词解析失败返回空串。"""
        head, _, rest = raw.strip().partition(" ")
        day = self._resolve_day(head)
        if day and rest.strip():
            return day + " " + rest.strip()
        date_like = (
            head in ("今天", "今日", "today", "明天", "明日", "tomorrow", "后天")
            or bool(re.match(r"^\d{1,4}\s*[-/.年]\s*\d{1,2}", head))
            or bool(re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", head))
            or bool(re.match(r"^\d{2,4}$", head))
        )
        if date_like:
            return ""
        return raw.strip()
    # ------------------------------------------------------------ 生命周期

    @filter.on_astrbot_loaded()
    async def on_loaded(self, *args, **kwargs):
        self._portal_start()
        for cls in self._classes():
            cfg = self.notify_config(cls.get("id"))
            logger.info(
                "[class_notice] 班级=%s 推送目标=%s | 确认=%s | 上课前=%s分钟 | 下课前=%s分钟 | 每日=%s/%s",
                cls.get("id"),
                cfg["targets"] or "(未配置)",
                cfg["confirm"] or "(随指令所在会话)",
                cfg["advance"],
                cfg["end_advance"],
                cfg["daily_on"], cfg["daily_time"],
            )
            self._sync_class_manual_entries(cls.get("id"), force=True)
        self.scheduler.start()

    async def initialize(self):
        """插件重载 / 激活时由 AstrBot 调用；确保调度器一定启动。"""
        logger.info("[class_notice] initialize() 被调用，启动调度器")
        self._portal_start()
        for cls in self._classes():
            self._sync_class_manual_entries(cls.get("id"), force=True)
        self.scheduler.start()

    async def terminate(self):
        self._portal_stop()
        await self.scheduler.stop()
        logger.info("[class_notice] 插件已停止。")

    # ------------------------------------------------------------ 导入

    async def _do_import(self, event: AstrMessageEvent, hint: str, is_admin: bool, class_id: str = ""):
        if not is_admin:
            yield event.plain_result("只有管理员可以导入课表。")
            return

        path = (hint or "").strip()
        if not path:
            path = str(self.config.get("import_path") or "").strip()
        if not path:
            for name in DEFAULT_FILES:
                candidate = self.data_dir / name
                if candidate.exists():
                    path = str(candidate)
                    break
        if not path:
            yield event.plain_result(
                "没找到课表文件。把课表放到下面这个目录后重试：\n"
                + str(self.data_dir)
                + "\n文件名用 timetable.ics / .csv / .json / .txt，"
                "或者直接发：/课表 导入 /绝对路径/timetable.ics"
            )
            return
        if not Path(path).exists() and not path.startswith("/"):
            alt = self.data_dir / path
            if alt.exists():
                path = str(alt)
        if not Path(path).exists():
            yield event.plain_result(f"文件不存在：{path}")
            return

        stats: dict = {}
        try:
            sessions, kind = importers.load_file(path, self._tz_name(), stats)
        except Exception as exc:
            logger.exception("[class_notice] 导入失败")
            yield event.plain_result(f"导入失败：{exc}")
            return

        if not sessions:
            yield event.plain_result(
                f"解析到 0 条课程（识别为 {kind}）。请检查文件格式，或看 /课表 帮助。"
            )
            return

        self.sync_manual_entries()
        count = self.store.replace_import(sessions, class_id)
        self.store.set_meta("imported_at", self.now().isoformat(timespec="seconds"), class_id)
        self.store.set_meta("imported_from", path, class_id)
        self.store.set_meta("imported_kind", kind, class_id)
        notes = []
        if stats.get("skipped"):
            notes.append(f"跳过 {stats['skipped']} 条提醒/无效条目")
        if stats.get("merged"):
            notes.append(f"合并 {stats['merged']} 条连堂")
        extra = ("\n" + "，".join(notes)) if notes else ""
        by_source = self.store.count_by_source(class_id)
        yield event.plain_result(
            f"✅ 班级 {class_id or '默认'} 导入成功（{kind}）\n"
            f"课表 {count} 条{extra}"
            f"\n手动日程 {by_source.get('manual', 0)} 条（未受影响）\n"
            f"日期范围：{self.store.first_day(class_id) or '—'} ~ {self.store.last_day(class_id) or '—'}\n"
            "试试 /课表 今天"
        )

    # ------------------------------------------------------------ 指令

    # priority=1000：排在「聊天类」插件(self_learning/context_aware/stealer, 优先级 0)之前，
    # 但仍低于 wakepro(99999) 的名单/阻塞判断，保证被拉黑的消息依旧被拦。
    # 处理完立刻 stop_event()，所以命令消息不会流到那些插件里去。
    @filter.command("课表", alias={"课程表"}, priority=1000)
    async def cmd_schedule(self, event: AstrMessageEvent, action: GreedyStr = GreedyStr):
        """课表查询、导入与提醒设置（终结命令，不进入聊天）。"""
        async for result in self._dispatch(event, action):
            yield result
        event.stop_event()

    async def _class_management_command(self, event: AstrMessageEvent, action: str, class_id: str = ""):
        """管理员：班级列表 / 添加 / 删除 / 复制 / 改默认 / 改推送目标。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员可以管理班级。")
            return

        parts = action.split(maxsplit=2)
        sub = (parts[1] if len(parts) > 1 else "").strip().lower()
        rest = (parts[2] if len(parts) > 2 else "").strip()

        def _save():
            try:
                self.config.save_config()
                logger.info("[class_notice] 班级配置已保存")
            except Exception as exc:
                logger.warning("[class_notice] 保存班级配置失败: %s", exc)

        def _classes_now():
            return self._classes()

        def _find_class(cid):
            for c in _classes_now():
                if str(c.get("id")) == str(cid):
                    return c
            return None

        def _create_class(cid, name=""):
            base = class_cfg_defaults()
            base["id"] = str(cid).strip()
            base["name"] = str(name).strip() or base["id"]
            default_id = self._default_class_id()
            src = _find_class(default_id)
            if src:
                base["notify_sessions"] = [x for x in split_targets(src.get("notify_sessions"))]
                base["daily_push_time"] = str(src.get("daily_push_time") or base.get("daily_push_time") or "08:00").strip()
                base["push_format"] = str(src.get("push_format") or base.get("push_format") or "auto").strip()
                base["advance_minutes"] = int(src.get("advance_minutes") or base.get("advance_minutes") or 20)
                base["end_advance_minutes"] = int(src.get("end_advance_minutes") or base.get("end_advance_minutes") or 5)
            return base

        if sub in ("列表", "list", "全部", "all", ""):
            classes = _classes_now()
            default_id = self._default_class_id()
            lines = []
            if not classes:
                lines.append("  （未配置任何班级）")
            else:
                for c in classes:
                    cid = str(c.get("id") or "")
                    name = str(c.get("name") or cid)
                    mark = "★" if cid == default_id else " "
                    daily = "开" if c.get("daily_push_enable") else "关"
                    push = c.get("push_format") or "auto"
                    lines.append(f"  {mark} {cid} {name} | 每日={daily} | 格式={push} | 目标数={len(split_targets(c.get('notify_sessions')))}")
            yield event.plain_result(
                "🏫 当前班级列表：\n"
                + "\n".join(lines)
                + f"\n默认班级：{default_id or '（未设置）'}"
            )
            return

        if sub in ("添加", "新建", "new", "add"):
            tokens = rest.split(maxsplit=3)
            if not tokens:
                yield event.plain_result(
                    "用法：\n"
                    "/课表 班级 添加 13 13班\n"
                    "/课表 班级 添加 13 13班 07:30\n"
                    "/课表 班级 添加 13 13班 auto\n"
                    "/课表 班级 添加 13 13班 沃:GroupMessage:xxx"
                )
                return
            cid = tokens[0].strip()
            name = tokens[1].strip() if len(tokens) > 1 else ""
            extra = (tokens[2] if len(tokens) > 2 else "").strip()
            target_extra = (tokens[3] if len(tokens) > 3 else "").strip()

            if _find_class(cid):
                yield event.plain_result(f"班级 {cid} 已存在。可用 /课表 班级 信息 {cid} 查看。")
                return

            new_cls = _create_class(cid, name or cid)
            self.store._ensure_tables(cid)
            classes = _classes_now()
            classes.append(new_cls)
            self.config["classes"] = classes

            fmt = "auto"
            if extra:
                if extra in ("auto", "text", "image", "图片", "文本", "文字"):
                    fmt = "auto" if extra in ("auto", "图片", "图") else ("text" if extra in ("text", "文本", "文字") else "image")
                else:
                    hhmm = self._norm_hhmm(extra)
                    if hhmm:
                        new_cls["daily_push_enable"] = True
                        new_cls["daily_push_time"] = hhmm

            if target_extra:
                new_cls["notify_sessions"] = split_targets(target_extra)
            new_cls["push_format"] = fmt
            _save()
            targets = split_targets(new_cls.get("notify_sessions")) or ["（未配置）"]
            yield event.plain_result(
                f"✅ 已添加班级 {cid}（{new_cls.get('name') or cid}）\n"
                f"推送格式：{new_cls.get('push_format')}\n"
                f"每日推送：{'开 ' + str(new_cls.get('daily_push_time')) if new_cls.get('daily_push_enable') else '关'}\n"
                f"当前目标：\n  " + "\n  ".join(targets)
            )
            return

        if sub in ("删除", "移除", "remove", "del"):
            cid = rest.strip()
            if not cid:
                yield event.plain_result("用法：/课表 班级 删除 13")
                return
            classes = _classes_now()
            kept = []
            found = False
            for c in classes:
                if str(c.get("id")) == str(cid):
                    found = True
                    continue
                kept.append(c)
            if not found:
                yield event.plain_result(f"班级 {cid} 不存在。")
                return
            self.config["classes"] = kept
            if self._default_class_id() == cid:
                self.config["default_class_id"] = kept[0].get("id") if kept else ""
            _save()
            try:
                self.store.drop_class(cid)
                data_state = "数据库课表已一并删除"
            except Exception as exc:
                logger.warning("[class_notice] 删除班级 %s 数据库失败: %s", cid, exc)
                data_state = "数据库课表清理失败（不影响配置删除）"
            yield event.plain_result(
                f"🗑 已删除班级 {cid}（{data_state}）。"
            )
            return

        if sub in ("复制", "clone", "copy"):
            tokens = rest.split(maxsplit=1)
            if len(tokens) < 2:
                yield event.plain_result("用法：/课表 班级 复制 12 13")
                return
            src_cid, dst_cid = tokens[0].strip(), tokens[1].strip()
            src = _find_class(src_cid)
            if not src:
                yield event.plain_result(f"源班级 {src_cid} 不存在。")
                return
            if _find_class(dst_cid):
                yield event.plain_result(f"目标班级 {dst_cid} 已存在。")
                return
            new_cls = dict(src)
            new_cls["id"] = dst_cid
            new_cls["name"] = dst_cid
            try:
                n = self.store.copy_class(src_cid, dst_cid)
                data_msg = f"，已复制 {n} 条课程数据" if n else "（源班级无课程数据）"
            except Exception as exc:
                logger.warning("[class_notice] 复制班级 %s->%s 数据失败: %s", src_cid, dst_cid, exc)
                data_msg = "（课程数据复制失败）"
            classes = _classes_now()
            classes.append(new_cls)
            self.config["classes"] = classes
            _save()
            yield event.plain_result(f"✅ 已将班级 {src_cid} 复制为 {dst_cid}{data_msg}。")
            return

        if sub in ("默认", "set-default", "default"):
            cid = rest.strip()
            if not cid:
                yield event.plain_result("用法：/课表 班级 默认 12")
                return
            if not _find_class(cid):
                yield event.plain_result(f"班级 {cid} 不存在，无法设为默认。")
                return
            self.config["default_class_id"] = cid
            _save()
            yield event.plain_result(f"✅ 默认班级已改为 {cid}。")
            return

        if sub in ("信息", "详情", "info", "detail"):
            cid = rest.strip()
            c = _find_class(cid)
            if not c:
                yield event.plain_result(f"班级 {cid or '（默认）'} 不存在。")
                return
            lines = [
                f"🏫 班级 {cid}（{c.get('name') or cid}）配置摘要",
                f"默认班级：{'是' if cid == self._default_class_id() else '否'}",
                f"每日推送：{'开 ' + str(c.get('daily_push_time')) if c.get('daily_push_enable') else '关'}",
                f"推送格式：{c.get('push_format') or 'auto'}",
                f"图片标题：{c.get('push_image_title') or c.get('name') or '（默认用班级名）'}",
                f"上课前提醒：{c.get('advance_minutes')} 分钟",
                f"下课前提醒：{c.get('end_advance_minutes')} 分钟",
                "推送目标：",
            ]
            targets = split_targets(c.get("notify_sessions"))
            lines.append("  " + ("\n  ".join(targets) if targets else "（未配置）"))
            yield event.plain_result("\n".join(lines))
            return

        if sub in ("目标", "notify", "targets", "sessions"):
            tokens = rest.split(maxsplit=1)
            if len(tokens) < 2:
                yield event.plain_result(
                    "用法：\n"
                    "/课表 班级 目标 12 沃:GroupMessage:xxx\n"
                    "/课表 班级 目标 12 删除 沃:GroupMessage:xxx"
                )
                return
            cid = tokens[0].strip()
            rest2 = tokens[1].strip()
            c = _find_class(cid)
            if not c:
                yield event.plain_result(f"班级 {cid} 不存在。")
                return
            classes = _classes_now()
            target_cls = None
            for x in classes:
                if str(x.get("id")) == str(cid):
                    target_cls = x
                    break
            if not target_cls:
                yield event.plain_result(f"班级 {cid} 不存在。")
                return
            current = split_targets(target_cls.get("notify_sessions"))

            if rest2.startswith("删除") or rest2.lower().startswith(("remove", "del", "-")):
                target = rest2.split(maxsplit=1)[1].strip() if len(rest2.split(maxsplit=1)) > 1 else ""
                if not target:
                    yield event.plain_result("用法：/课表 班级 目标 12 删除 沃:GroupMessage:xxx")
                    return
                if target not in current:
                    yield event.plain_result(f"目标 {target} 不在班级 {cid} 里。")
                    return
                current = [x for x in current if x != target]
                target_cls["notify_sessions"] = current
                self.config["classes"] = classes
                _save()
                remaining = current
                yield event.plain_result(
                    f"✅ 已从班级 {cid} 移除目标：\n  {target}\n"
                    f"剩余目标：\n  " + ("\n  ".join(remaining) if remaining else "（无）")
                )
                return

            new_targets = split_targets(rest2)
            if not new_targets:
                yield event.plain_result("没有解析到新的推送目标。")
                return
            merged = current + [x for x in new_targets if x not in current]
            target_cls["notify_sessions"] = merged
            self.config["classes"] = classes
            _save()
            yield event.plain_result(
                f"✅ 已为班级 {cid} 添加目标：\n  "
                + "\n  ".join(new_targets)
                + f"\n当前目标：\n  "
                + "\n  ".join(merged)
            )
            return

        yield event.plain_result(
            "班级子命令：\n"
            "/课表 班级 列表\n"
            "/课表 班级 添加 13 13班 [07:30] [auto] [沃:GroupMessage:xxx]\n"
            "/课表 班级 删除 13\n"
            "/课表 班级 复制 13 14\n"
            "/课表 班级 默认 13\n"
            "/课表 班级 信息 13\n"
            "/课表 班级 目标 13 沃:GroupMessage:xxx\n"
            "/课表 班级 目标 13 删除 沃:GroupMessage:xxx"
        )

    async def _daily_push_command(self, event: AstrMessageEvent, action: str, class_id: str = ""):
        """管理员指令：开关每日推送，或修改每天推送时间（自动持久化到配置文件）。"""
        if not event.is_admin():
            yield event.plain_result("只有管理员可以管理每日推送。")
            return
        trailing = self._trailing(event, action)
        token = (trailing or "").strip()
        cfg = self.notify_config(class_id)

        def _save() -> None:
            """立即把当前 config 写入磁盘，避免只改内存不落盘。"""
            try:
                self.config.save_config()
                logger.info("[class_notice] 配置已保存")
            except Exception as exc:
                logger.warning("[class_notice] 保存配置失败: %s", exc)

        def _class_by_id(cid: str) -> dict | None:
            for cls in self._classes():
                if str(cls.get("id")) == str(cid):
                    return cls
            return None

        def _update_class(cid: str, enable: bool | None = None, time_value: str | None = None, fmt_value: str | None = None) -> None:
            classes = self._classes()
            target = _class_by_id(cid)
            if target is None:
                target = class_cfg_defaults()
                target["id"] = cid
                target["name"] = cid
                classes.append(target)
            if enable is not None:
                target["daily_push_enable"] = enable
            if time_value:
                target["daily_push_time"] = time_value
            if fmt_value:
                target["push_format"] = fmt_value
            self.config["classes"] = classes
            if not cid:
                self.config["daily_push_enable"] = enable
                if time_value:
                    self.config["daily_push_time"] = time_value
                if fmt_value:
                    self.config["push_format"] = fmt_value

        if token in ("图片", "image", "图"):
            _update_class(class_id, None, None, "auto")
            _save()
            hhmm = cfg.get("daily_time") or str(self.config.get("daily_push_time") or "08:00").strip()
            yield event.plain_result(f"✅ 班级 {class_id or cfg.get('class_id') or '默认'} 已改为图片优先推送（每天 {hhmm}，失败自动回退文本）。")
        elif token in ("文本", "文字", "text"):
            _update_class(class_id, None, None, "text")
            _save()
            hhmm = cfg.get("daily_time") or str(self.config.get("daily_push_time") or "08:00").strip()
            yield event.plain_result(f"✅ 班级 {class_id or cfg.get('class_id') or '默认'} 已改为纯文本推送（每天 {hhmm}）。")
        elif token in ("明天", "次日", "明日", "明天推送"):
            # 改为推送明天课程
            target = _class_by_id(class_id) or {}
            if target:
                target["daily_push_offset_days"] = 1
            else:
                self.config["daily_push_offset_days"] = 1
            _save()
            hhmm = cfg.get("daily_time") or str(self.config.get("daily_push_time") or "08:00").strip()
            yield event.plain_result(f"✅ 班级 {class_id or cfg.get('class_id') or '默认'} 已改为每天 {hhmm} 推送【明天课程】。")
        elif token in ("今天", "当天", "今天推送"):
            target = _class_by_id(class_id) or {}
            if target:
                target["daily_push_offset_days"] = 0
            else:
                self.config["daily_push_offset_days"] = 0
            _save()
            hhmm = cfg.get("daily_time") or str(self.config.get("daily_push_time") or "08:00").strip()
            yield event.plain_result(f"✅ 班级 {class_id or cfg.get('class_id') or '默认'} 已改为每天 {hhmm} 推送【当天课程】。")
        if token in ("关", "off", "disable", "关闭"):
            _update_class(class_id, False, None)
            _save()
            yield event.plain_result(f"✅ 班级 {class_id or cfg['class_id'] or '默认'} 的每日推送已关闭。")
        elif token:
            hhmm = self._norm_hhmm(token)
            if not hhmm:
                yield event.plain_result(
                    f"时间格式看不懂：「{token}」，应该像 07:30 / 8:00。"
                )
                return
            _update_class(class_id, True, hhmm)
            _save()
            msg = "✅ 班级 {} 的每日推送已开启，每天 {} 推送当天课程（已保存）\n".format(class_id or cfg['class_id'] or '默认', hhmm)
            msg += "当前推送目标：\n  " + "\n  ".join(cfg["targets"])
            yield event.plain_result(msg)
        else:
            _update_class(class_id, True, None)
            _save()
            hhmm = cfg.get("daily_time") or str(self.config.get("daily_push_time") or "08:00").strip()
            msg = "✅ 班级 {} 的每日推送已开启，每天 {} 推送当天课程（已保存）\n".format(class_id or cfg['class_id'] or '默认', hhmm)
            msg += "当前推送目标：\n  " + "\n  ".join(cfg["targets"])
            yield event.plain_result(msg)
    async def _dispatch(self, event: AstrMessageEvent, action: str):
        action = (action or "").strip()
        today = self._today()
        class_id, action = split_class_action(action)
        self._sync_class_manual_entries(class_id)

        if action in ("", "今天", "今日", "today"):
            yield event.plain_result(self._render_day(today, class_id, with_hint=True))
        elif action in ("明天", "明日", "tomorrow"):
            tomorrow = (
                datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)
            ).strftime("%Y-%m-%d")
            yield event.plain_result(self._render_day(tomorrow, class_id, with_hint=True))
        elif action in ("本周", "周", "week"):
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            monday = today_dt - timedelta(days=today_dt.weekday())
            sunday = monday + timedelta(days=6)
            rows = self.store.between(
                monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d"), class_id
            )
            yield event.plain_result(render.week_block(today, rows))
        elif action in ("表格", "周表", "课表", "table"):
            week_anchor = today
            rest = action.split(maxsplit=1)[1].strip() if " " in action else ""
            if rest:
                parsed = self._resolve_day(rest) or self._parse_day(rest)
                if parsed:
                    week_anchor = parsed
            rows = self.store.between(
                (datetime.strptime(week_anchor, "%Y-%m-%d") - timedelta(days=datetime.strptime(week_anchor, "%Y-%m-%d").weekday())).strftime("%Y-%m-%d"),
                (datetime.strptime(week_anchor, "%Y-%m-%d") - timedelta(days=datetime.strptime(week_anchor, "%Y-%m-%d").weekday()) + timedelta(days=6)).strftime("%Y-%m-%d"),
                class_id,
            )
            yield event.plain_result(render.week_table(week_anchor, rows))
        elif action in ("立即推送", "立即每日推送", "手动推送", "daily_push_now"):
            if not event.is_admin():
                yield event.plain_result("只有管理员可以手动触发每日推送。")
                return
            today = self._today()
            cfg = self.notify_config(class_id)
            lines = await self._send_daily_push(today, class_id)
            text = self._render_day(today, class_id, with_hint=True)
            summary = (
                f"🚀 班级 {class_id or cfg.get('class_id') or '默认'} 立即每日推送结果\n"
                + f"推送日期：{today}（今天）\n"
                + self._push_cfg_summary(class_id, cfg)
                + "\n\n发送结果：\n"
                + "\n".join(lines)
                + "\n\n推送内容：\n"
                + text
            )
            dest = cfg["confirm"] or event.unified_msg_origin
            if dest == event.unified_msg_origin:
                yield event.plain_result(summary)
            else:
                ok = await self.send_text(dest, summary)
                logger.info("[class_notice] 立即推送确认已发往 %s ok=%s", dest, ok)
        elif action.startswith("每日推送") or action.startswith("daily_push"):
            async for result in self._daily_push_command(event, action, class_id):
                yield result
        elif action in ("状态", "status", "info"):
            cfg = self.notify_config(class_id)
            by_source = self.store.count_by_source(class_id)
            manual = self._manual_stat or {}
            if not cfg["targets"]:
                push = "未配置"
            elif not cfg["enabled"]:
                push = "已暂停"
            else:
                push = "\n  ".join(cfg["targets"])
            parts = []
            if cfg["start_on"]:
                parts.append(f"上课前 {cfg['advance']} 分钟")
            if cfg["end_on"]:
                parts.append(f"下课前 {cfg['end_advance']} 分钟")
            fail_note = ""
            if manual.get("failed"):
                fail_note = (
                    "\n⚠️ 手动日程有 "
                    + str(len(manual["failed"]))
                    + " 条没解析成功，请检查格式"
                )
            classes = self._classes()
            class_list = "\n".join(
                f"  {c.get('id') or '(未命名)'}  {c.get('name') or c.get('id') or ''}".strip()
                for c in classes
            ) or "  （未配置 classes）"
            yield event.plain_result(
                "📦 课表状态\n"
                f"班级：{class_id or cfg.get('class_id') or '默认'}（{cfg.get('class_name') or '默认'}）\n"
                f"班级列表：\n{class_list}\n"
                f"记录数：{self.store.count(class_id)}（导入 "
                f"{by_source.get('ics', 0) + by_source.get('csv', 0) + by_source.get('json', 0) + by_source.get('text', 0)}"
                f" / 手动 {by_source.get('manual', 0)}）{fail_note}\n"
                f"日期范围：{self.store.first_day(class_id) or '—'} ~ {self.store.last_day(class_id) or '—'}\n"
                f"最近导入：{self.store.get_meta('imported_at', '', class_id) or '从未'}\n"
                f"推送目标：\n  {push}\n"
                f"提醒时机：{' / '.join(parts) if parts else '仅查询'}\n"
                + (f"每日推送：每天 {cfg['daily_time']} 推送当天课程\n" if cfg.get("daily_on") else "每日推送：未开启\n")
                + f"确认会话：{cfg['confirm'] or '随指令所在会话'}\n"
                f"数据目录：{self.data_dir}"
            )
        elif action in ("测试推送", "测试", "test"):
            if not event.is_admin():
                yield event.plain_result("只有管理员可以测试推送。")
                return
            cfg = self.notify_config(class_id)
            if not cfg["targets"]:
                yield event.plain_result(
                    f"班级 {class_id or cfg.get('class_id') or '默认'} 还没配置推送目标（notify_sessions）。"
                )
                return
            demo = Session(
                day=today,
                start_time="08:00",
                end_time="09:40",
                name="测试课程",
                location="测试教室",
                class_id=class_id,
            )
            text = self.render_notify(demo, "start", cfg["advance"])
            lines = []
            image_path = None
            fmt = str(cfg.get("push_format") or "auto").lower()
            if fmt in ("image", "auto"):
                try:
                    image_path = await self._render_daily_push_image(today, class_id)
                except Exception as exc:
                    logger.warning("[class_notice] 测试推送图片渲染失败，回退文本: %s", exc)
            for target in cfg["targets"]:
                sent_ok = False
                if image_path:
                    try:
                        ok = await self.send_image(target, image_path)
                        sent_ok = bool(ok)
                    except Exception as exc:
                        logger.warning("[class_notice] 测试推送图片到 %s 失败，回退文本: %s", target, exc)
                if not sent_ok:
                    try:
                        ok = await self.send_text(target, text)
                        sent_ok = bool(ok)
                    except Exception as exc:
                        logger.exception("[class_notice] 测试推送文本到 %s 失败: %s", target, exc)
                if image_path and sent_ok:
                    lines.append(f"✅ {target}（图片）")
                elif sent_ok:
                    lines.append(f"✅ {target}（文本）")
                else:
                    lines.append(f"❌ 发送失败 {target}")
            summary = (
                f"🧪 班级 {class_id or cfg.get('class_id') or '默认'} 测试推送结果\n"
                + f"测试日期：{today}（今天）\n"
                + self._push_cfg_summary(class_id, cfg)
                + "\n\n发送结果：\n"
                + "\n".join(lines)
                + "\n\n推送内容：\n"
                + text
            )

            dest = cfg["confirm"] or event.unified_msg_origin
            if dest == event.unified_msg_origin:
                yield event.plain_result(summary)
            else:
                ok = await self.send_text(dest, summary)
                logger.info("[class_notice] 测试确认已发往 %s ok=%s", dest, ok)
        elif action in ("导入", "重载", "import", "reload"):
            async for result in self._do_import(
                event, self._trailing(event, action), event.is_admin(), class_id
            ):
                yield result
        elif action in ("删除", "del", "remove"):
            if not event.is_admin():
                yield event.plain_result("只有管理员可以删除记录。")
                return
            raw = self._trailing(event, action)
            if not raw:
                yield event.plain_result(
                    "用法：\n"
                    "/课表 删除 9/16 15:30    删掉该日期该时间的课\n"
                    "/课表 删除 9/16          删掉该日期全部\n"
                    "/课表 删除 <关键词>      按课程名 / 地点删"
                )
                return
            parts = raw.split()
            first = parts[0] if parts else ""
            day = self._resolve_day(first) if first else ""
            date_like = (
                first in ("今天", "今日", "today", "明天", "明日", "tomorrow", "后天")
                or bool(re.match(r"^\d{1,4}\s*[-/.年]\s*\d{1,2}", first))
                or bool(re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", first))
                or bool(re.match(r"^\d{2,4}$", first))
            )
            if first and date_like and not day:
                yield event.plain_result(
                    f"日期解析失败：「{first}」，已取消删除（不会误删数据）。\n"
                    "正确格式：/课表 6 删除 2026-10-13 15:30  或  /课表 6 删除 明天 15:30"
                )
                return
            if day:
                hhmm = self._norm_hhmm(parts[1]) if len(parts) > 1 else ""
                if len(parts) > 1 and not hhmm:
                    yield event.plain_result(
                        f"时间格式看不懂：「{parts[1]}」，应该像 15:30。"
                    )
                    return
                rows = self.store.delete_at(day, hhmm, class_id)
                if not rows:
                    yield event.plain_result(
                        f"没有找到 {day} {hhmm} 的班级 {class_id or '默认'} 记录。"
                    )
                    return
                detail = "\n".join("  " + s.line() for s in rows)
                yield event.plain_result(
                    f"\U0001F5D1 已删除 {len(rows)} 条（{day} {hhmm}）：\n{detail}\n"
                    f"当前班级剩余 {self.store.count(class_id)} 条。"
                )
                return
            removed = self.store.delete_matching(raw, class_id)
            yield event.plain_result(
                f"\U0001F5D1 已删除班级 {class_id or '默认'} 中包含「{raw}」的 {removed} 条记录。\n"
                f"当前班级剩余 {self.store.count(class_id)} 条。"
            )
        elif action in ("添加", "新增", "add"):
            if not event.is_admin():
                yield event.plain_result("只有管理员可以添加日程。")
                return
            raw = self._trailing(event, action)
            if not raw:
                yield event.plain_result(
                    "用法：/课表 添加 9/16 15:30-17:10 体育 @游泳馆\n"
                    "日期也支持 今天 / 明天 / 后天 / 2026-09-16；@地点 可省略。"
                )
                return
            line = self._normalize_add(raw)
            if not line:
                yield event.plain_result(
                    f"日期解析失败：「{raw.strip().split()[0] if raw.strip().split() else raw}」，已取消添加。\n"
                    "正确格式：/课表 6 添加 2026-10-13 15:30-17:10 体育 @游泳馆"
                )
                return
            sessions, _bad = importers.parse_manual_lines(
                [line], self._tz_name(), today=self.now().date()
            )
            if not sessions:
                yield event.plain_result(
                    f"没解析成功：「{raw}」\n"
                    "格式：日期 开始-结束 名称 @地点\n"
                    "例如：/课表 添加 9/16 15:30-17:10 体育 @游泳馆"
                )
                return
            for item in sessions:
                item.source = "cmd"
                item.class_id = class_id
            count = self.store.add_sessions(sessions, class_id)
            detail = "\n".join("  " + s.line() for s in sessions)
            yield event.plain_result(
                f"✅ 已添加 {count} 条：\n{detail}\n"
                "（命令添加的日程独立保存，重新导入课表不会覆盖）"
            )
        elif action in ("清理历史", "清理", "purge"):
            if not event.is_admin():
                yield event.plain_result("只有管理员可以清理历史数据。")
                return
            cutoff = self._trailing(event, action) or today
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", cutoff):
                yield event.plain_result(
                    "日期格式应为 YYYY-MM-DD，例如：/课表 清理历史 2026-09-01"
                )
                return
            removed = self.store.delete_before(cutoff, class_id)
            yield event.plain_result(
                f"🗑 已删除班级 {class_id or '默认'} 在 {cutoff} 之前的 {removed} 条记录。\n"
                f"当前班级剩余 {self.store.count(class_id)} 条。"
            )
        elif action in ("清空", "clear"):
            if not event.is_admin():
                yield event.plain_result("只有管理员可以清空课表。")
            else:
                self.store.clear(class_id)
                self._manual_key = None
                self._manual_class = None
                yield event.plain_result("🗑 课表已清空（手动日程下次同步会重新写入）。")
        elif action.startswith("班级") or action.lower().startswith(("class", "classes")):
            async for result in self._class_management_command(event, action, class_id):
                yield result
        elif action in ("帮助", "help", "?", "-h", "h"):
            yield event.plain_result(HELP)
        else:
            target_day = self._parse_day(action)
            if not target_day:
                yield event.plain_result(f"不认识「{action}」这个参数。\n\n" + HELP)
                return
            text = self._render_day(target_day, class_id, with_hint=True)
            first, last = self.store.first_day(class_id), self.store.last_day(class_id)
            if first and last and not (first <= target_day <= last):
                text += f"\n\n（提示：当前课表只覆盖 {first} ~ {last}）"
            yield event.plain_result(text)
