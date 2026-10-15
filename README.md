# astrbot_plugin_class_notice

_✨ 上课下课提醒 · 多班级课表 · 独立管理门户 ✨_

把课表（`.ics` / `.csv` / `.json` / 文本）导入后，机器人会在**上课前**和**下课前**按点提醒到指定的私聊或群，支持**多班级**独立管理，每天定时推送当天课程（**图片 / 文本可切换**），并内置**独立 Web 管理门户**（与 AstrBot 面板隔离），手机 / 电脑均可使用。

> 所有 `/课表` 指令都是 **终结命令**：不会被送去 LLM，也不会进入聊天记忆与学习数据。

---

## ✨ 功能

| 功能 | 说明 |
| --- | --- |
| 🏫 多班级 | 每个班级独立课表、独立推送目标、独立提醒策略；`/课表 班级` 系列命令可在群里直接管理 |
| 🌐 独立管理门户 | 内置 HTTP 门户（默认 8321 端口），登录后即可管理课表/班级/设置，**不进入 AstrBot 面板** |
| 👥 双账号权限 | 超级管理员（全功能）+ 普通管理员（仅限指定班级），权限在门户 API 层强制校验 |
| 📱 手机端适配 | 课表页面自适应：宽屏表格、窄屏按天卡片列表，手机可增删改 |
| 📥 课表导入 | 支持 `.ics` / `.csv` / `.json` / 纯文本；自动处理 UTC 时区、`RRULE`、`EXDATE` |
| 🧹 脏数据过滤 | 自动跳过「N分钟后上课/下课」提醒条目、零时长/已取消事件 |
| 🔗 连堂合并 | 同天同名同地点、间隔 ≤ 20 分钟的相邻节次自动合并 |
| 🔍 课表查询 | `/课表 6` 今天 / 明天 / 本周 / 任意日期 / 表格（按周） |
| ✍️ 手动录日程 | 面板 / 门户 / 命令均可增删改课程 |
| 🖼️ 图片推送 | 每日推送支持 图片 / 文本 / auto（图片优先、失败自动回退文本） |
| ⏰ 定时提醒 | 上课前 N 分钟 / 下课前 M 分钟，可分别开关，支持按课程条件覆盖提醒分钟 |
| 📅 每日课程推送 | 每天固定时间推送当天或明天课程 |
| 🚀 测试/立即推送 | 门户一键测试与立即推送，回显结果 |
| 🛡️ 与聊天隔离 | 指令优先级 1000 + `stop_event()`，聊天类插件看不到 |

**无第三方依赖**，只用标准库。

---

## 📦 安装

1. 把 `astrbot_plugin_class_notice` 整个目录放进 `AstrBot/data/plugins/`
2. 面板 → 插件 → 重载（或重启 AstrBot）
3. 打开独立门户：`http://服务器IP:8321`（需在 Docker 映射端口 `-p 8321:8321`）

---

## 🌐 独立管理门户

插件内置门户服务（随插件启动/停止），**与 AstrBot 面板完全隔离**：

| 项 | 说明 |
| --- | --- |
| 地址 | `http://IP:8321`（`manage_port` 可改）|
| 页面 | 登录页 → 课表管理（📅 课表 / 🏫 班级 / ⚙️ 设置）|
| 隔离 | 页面无任何 AstrBot 入口，不暴露面板/聊天数据 |

### 门户账号（双账号权限）

| 角色 | 配置项 | 权限 |
| --- | --- | --- |
| 超级管理员 | `manage_full_user` / `manage_full_password` | 全部班级、全部功能（增删班级、全局设置等）|
| 普通管理员 | `manage_user` / `manage_password` + `manage_class_ids` | 仅限 `manage_class_ids` 列出的班级（课表/课程/推送配置），不能增删班级、不能改全局设置 |

示例（请替换为你的账号并修改为强密码）：

```json
{
  "manage_enable": true,
  "manage_port": 8321,
  "manage_user": "manager",
  "manage_password": "manager123",
  "manage_class_ids": ["6"],
  "manage_full_user": "admin",
  "manage_full_password": "admin123"
}
```

- 普通账号 `manager` 只能看到/管理 `manage_class_ids` 列出的班级（示例为 6 班）
- 超级账号 `admin` 管理所有班级与全局设置
- 越权操作（查其他班级、增删班级、改全局设置）返回 403

---

## ⚙️ 配置说明

### 全局配置

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | true | 关闭后查询可用，不再主动推送 |
| `timezone` | string | Asia/Shanghai | 时区 |
| `default_class_id` | string | "" | 不带班级参数时的默认班级 |
| `confirm_session` | string | "" | 测试/确认回执会话 |
| `advance_minutes` | int | 20 | 上课前提醒分钟（全局默认）|
| `end_advance_minutes` | int | 5 | 下课前提醒分钟（全局默认）|
| `daily_push_time` | string | 08:00 | 每日推送时间（全局默认）|
| `push_format` | string | auto | auto / text / image（全局默认）|
| `push_image_title` | string | "" | 图片标题（全局默认）|
| `manage_enable` | bool | true | 启用独立门户 |
| `manage_port` | int | 8321 | 门户端口 |
| `manage_user` / `manage_password` | string | admin/admin | 普通管理员账号（建议修改）|
| `manage_class_ids` | list | [] | 普通管理员可管理的班级（空=全部）|
| `manage_full_user` / `manage_full_password` | string | "" | 超级管理员账号（全功能）|
| `import_path` | string | "" | 课表文件路径 |
| `classes` | list | [] | 多班级配置（见下）|

### 班级配置 `classes`

```json
{
  "id": "6",
  "name": "6班",
  "notify_sessions": ["沃:GroupMessage:xxx"],
  "confirm_session": "沃:FriendMessage:xxx",
  "advance_minutes": 40,
  "end_advance_minutes": 5,
  "notify_class_start": true,
  "notify_class_end": true,
  "daily_push_enable": true,
  "daily_push_time": "22:00",
  "daily_push_offset_days": 1,
  "push_format": "auto",
  "push_image_title": "6班",
  "reminder_overrides": [
    {"match_location": "M", "match_start": "10:30", "advance_minutes": 15, "end_advance_minutes": 15}
  ]
}
```

- `daily_push_offset_days`：0=当天课程，1=明天课程
- `reminder_overrides`：特殊提醒规则（可选），按 `match_location`（教室包含文字）与 `match_start`（开始时间）匹配，覆盖该课程的上课/下课提醒分钟

### 存储（v0.9.0+）

课表以班级为单位存储在 SQLite（每班级独立表 `sessions_<class_id>` / `meta_<class_id>`）；配置文件只承担班级管理职责，不再内置课表数据。

---

## 💬 指令大全

> 以 6 班为例；不带班级 = 使用默认班级。

### 基础查询（所有人）

| 指令 | 说明 |
| --- | --- |
| `/课表` | 默认班级今天 |
| `/课表 明天` / `/课表 本周` | 明天 / 本周 |
| `/课表 2026-09-16` | 指定日期 |
| `/课表 6 表格` | 6 班按周可视化表格 |
| `/课表 6 状态` / `/课表 帮助` | 状态 / 帮助 |

### 推送管理（管理员）

| 指令 | 说明 |
| --- | --- |
| `/课表 6 测试推送` | 测试推送，回显当前生效配置 |
| `/课表 6 立即推送` | 立即推送当天课程 |
| `/课表 6 每日推送 07:30` / `关` / `图片` / `文本` / `明天` / `今天` | 每日推送设置 |

### 课程管理（管理员）

| 指令 | 说明 |
| --- | --- |
| `/课表 6 导入 [路径]` | 重新导入 |
| `/课表 6 添加 9/16 15:30-17:10 体育 @游泳馆` | 添加课程 |
| `/课表 6 删除 ...` / `清理历史` / `清空` | 删除/清理 |

### 班级管理（管理员）

| 指令 | 说明 |
| --- | --- |
| `/课表 班级 列表 / 添加 / 删除 / 复制 / 默认 / 信息 / 目标` | 班级管理 |

---

## 🔔 推送机制

- 上课前 / 下课前按点提醒（支持 `reminder_overrides` 按课程覆盖分钟）
- 每日课程推送（可推当天或明天，图片/文本切换）
- 调度器每 20 秒检查，同一条提醒不重复发
- 推送走 `Context.send_message()` 旁路，不进入对话历史

---

## 🗂 文件结构

```
astrbot_plugin_class_notice/
├── metadata.yaml         插件元数据
├── _conf_schema.json     面板配置项定义
├── main.py               插件主体：指令、多班级、推送、班级 API
├── portal.py             独立管理门户服务
├── portal/               门户页面（login.html / app.html）
├── core/
│   ├── store.py          按班级存储（sessions_<class_id> / meta_<class_id>）
│   ├── importers.py      解析层：ICS / CSV / JSON / 文本
│   ├── render.py         渲染层：日/周视图、周表格
│   └── scheduler.py      调度器：上下课提醒 + 每日推送
├── pages/timetable/      面板插件页面（可选）
├── examples/timetable.example.csv
├── LICENSE
└── README.md
```

---

## 📌 更新日志

### v0.9.0（当前）
- 🌐 独立管理门户（`portal.py` + `portal/`），登录后可管理课表/班级/设置，不进入 AstrBot 面板
- 👥 双账号权限：超级管理员（全功能）+ 普通管理员（`manage_class_ids` 限班级），API 层 403 校验
- 📱 页面手机端适配：宽屏表格 / 窄屏按天卡片列表
- ⏰ 特殊提醒规则 `reminder_overrides`（按教室/时间覆盖提醒分钟）
- 🚀 门户一键「测试推送 / 立即推送」（经主事件循环调度，跨线程安全）
- 🗄 课表按班级独立表存储（v3），配置瘦身只管班级
- 🐛 修复：登录响应缺失 Content-Length 导致浏览器卡"登录中"、portal 模块热重载、`__template_key` 丢失等

### v0.8.x
- 多班级、图片推送、班级管理命令、查询日期提示、防误删保护、GreedyStr 参数修复

---

## 📄 License

MIT License，见 [LICENSE](LICENSE)。
