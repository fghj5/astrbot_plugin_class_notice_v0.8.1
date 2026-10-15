"""独立课表管理门户（不进入 AstrBot 面板）。

提供独立的 HTTP 服务：
  GET  /            -> 登录页
  GET  /app         -> 课表管理系统（登录后）
  POST /api/login   -> 校验账号，签发 Token
  GET/POST /api/*   -> 业务 API（需 X-Auth-Token）

与 AstrBot 面板完全隔离：页面不含任何进入面板的入口。
数据直接复用插件的 store（SQLite 班级表）与 config。
"""

from __future__ import annotations

import asyncio as _asyncio
import json as _json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs

_TOKEN_TTL = 7 * 24 * 3600  # 登录态有效期 7 天
_PORTAL_DIR = Path(__file__).resolve().parent / "portal"


class _PortalHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, portal_ctx):
        self.portal_ctx = portal_ctx
        super().__init__(addr, handler)


def run_portal_server(bind_host: str, port: int, username: str, password: str, plugin,
                        full_user: str = "", full_password: str = "", allowed=None):
    """启动门户服务，返回 (server, thread)。"""
    ctx = {
        "username": username,
        "password": password,
        "full_user": full_user or "",
        "full_password": full_password or "",
        "allowed": list(allowed or []),
        "plugin": plugin,
        "tokens": {},
    }
    server = _PortalHTTPServer((bind_host, port), _PortalHandler, ctx)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="class-notice-portal")
    thread.start()
    return server, thread


class _PortalHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------------- 工具 ----------------
    def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, _json.dumps(obj, ensure_ascii=False))

    def _html(self, text: str, code: int = 200):
        self._send(code, text, "text/html; charset=utf-8")

    def _check_class_perm(self, class_id: str) -> bool:
        allowed = self._allowed_ids()
        if not allowed:
            return True
        return str(class_id or "") in allowed

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            return _json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def _auth_info(self):
        token = self.headers.get("X-Auth-Token", "").strip()
        if not token:
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                part = part.strip()
                if part.startswith("cn_token="):
                    token = part[len("cn_token="):]
                    break
        ctx = self.server.portal_ctx
        info = ctx["tokens"].get(token)
        if not info:
            return None
        if time.time() > info.get("exp", 0):
            ctx["tokens"].pop(token, None)
            return None
        return info

    def _auth(self):
        return self._auth_info() is not None

    def _allowed_ids(self):
        """当前登录账号可管理的班级；空列表 = 全部。"""
        info = self._auth_info()
        if not info:
            return None
        return list(info.get("allowed") or [])

    def log_message(self, *args):
        # 写入门户访问日志（用于排查）
        try:
            logfile = Path(__file__).resolve().parent.parent.parent / "plugin_data" / "astrbot_plugin_class_notice" / "portal_access.log"
            logfile.parent.mkdir(parents=True, exist_ok=True)
            line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.command} {self.path} from {self.client_address[0] if self.client_address else '?'}"
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    # ---------------- 路由 ----------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        ctx = self.server.portal_ctx
        if path == "/":
            html = self._read_static("login.html")
            html = html.replace("{{USERNAME}}", ctx["username"])
            self._html(html)
            return
        if path == "/app":
            if not self._auth():
                self._json({"ok": False, "message": "未登录"}, 401)
                return
            self._html(self._read_static("app.html"))
            return
        if path.startswith("/api/"):
            if not self._auth():
                self._json({"ok": False, "message": "未登录"}, 401)
                return
            self._handle_business(path, {})
            return
        self._json({"ok": False, "message": "Not Found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        ctx = self.server.portal_ctx
        if path == "/api/login":
            body = self._read_body()
            user = str(body.get("user") or "")
            pwd = str(body.get("password") or "")
            allowed = None
            if user == ctx.get("full_user") and pwd == ctx.get("full_password"):
                allowed = []  # 超级账号：全部班级
                account = ctx.get("full_user") or user
            elif user == ctx["username"] and pwd == ctx["password"]:
                allowed = ctx.get("allowed") or []  # 普通账号：受限班级
                account = ctx["username"]
            if allowed is None:
                time.sleep(1)  # 限速防爆破
                self._json({"ok": False, "message": "用户名或密码错误"}, 401)
                return
            token = secrets.token_hex(24)
            ctx["tokens"][token] = {"exp": time.time() + _TOKEN_TTL, "allowed": allowed, "user": account}
            body_bytes = _json.dumps(
                {"ok": True, "token": token, "message": "登录成功"}, ensure_ascii=False
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Set-Cookie", f"cn_token={token}; Path=/; Max-Age={_TOKEN_TTL}; HttpOnly")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body_bytes)
            return
        if not self._auth():
            self._json({"ok": False, "message": "未登录"}, 401)
            return
        self._handle_business(path, self._read_body())

    def _handle_business(self, path: str, body: dict):
        plugin = self.server.portal_ctx["plugin"]
        allowed = self._allowed_ids()
        restricted = bool(allowed)  # 有限制时：settings 只读、班级结构不可增删

        def _deny():
            self._json({"ok": False, "message": "无权操作该班级（当前账号仅限部分班级）"}, 403)

        try:
            if path == "/api/timetable" and self.command == "GET":
                qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                class_id = qs.get("class_id", [""])[0]
                week = qs.get("week", [""])[0]
                if not self._check_class_perm(class_id):
                    _deny(); return
                data = plugin._svc_get_timetable(class_id, week)
                if restricted:
                    data["restricted"] = True
                    data["allowed_ids"] = allowed
                self._json(data)
                return
            if path == "/api/session":
                cid = str(body.get("class_id") or plugin._default_class_id())
                if not self._check_class_perm(cid):
                    _deny(); return
                self._json(plugin._svc_post_session(body))
                return
            if path == "/api/classes" and self.command == "GET":
                data = plugin._svc_get_classes()
                if restricted:
                    data["classes"] = [c for c in data.get("classes", []) if str(c.get("id")) in allowed]
                    data["restricted"] = True
                    data["allowed_ids"] = allowed
                self._json(data)
                return
            if path == "/api/classes":
                act = str(body.get("_action") or "list").lower()
                if restricted:
                    if act in ("add", "new", "create", "copy", "clone", "duplicate", "default", "set-default"):
                        _deny(); return
                    cid = str(body.get("id") or "")
                    if not self._check_class_perm(cid):
                        _deny(); return
                self._json(_asyncio.run(plugin._svc_post_classes(body)))
                return
            if path == "/api/settings" and self.command == "GET":
                self._json(plugin._svc_get_settings())
                return
            if path == "/api/settings":
                if restricted:
                    _deny(); return
                self._json(plugin._svc_post_settings(body))
                return
            self._json({"ok": False, "message": "Not Found"}, 404)
        except Exception as exc:
            self._json({"ok": False, "message": f"服务异常: {exc}"}, 500)

    # ---------------- 静态资源 ----------------
    def _read_static(self, name: str) -> str:
        try:
            return (_PORTAL_DIR / name).read_text(encoding="utf-8")
        except Exception as exc:
            return f"<h3>缺少门户页面文件：{name}（{exc}）</h3>"