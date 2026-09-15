# -*- coding: utf-8 -*-
"""workbuddy2api 宿主机管理助手。

为什么需要它：workbuddy2api 只暴露 chat/models/status/healthz 四个只读接口，
签到、查积分、添加账号都在它自带的 CLI 工具里，只能由宿主机进程执行；
而网关跑在容器里执行不了宿主机 exe。

设计约束：
- 只绑 127.0.0.1（绝不暴露局域网）；Dashboard 页面由本机浏览器访问，直连本服务，
  CORS 仅放行控制台来源。
- 只调用 workbuddy2api 自己的二进制（credit/signin/login），不复制其代码，
  也不改它的数据格式。
- 单账号签到：signin 只吃目录，所以为单个账号开一个临时目录再跑（不改它的源码）。

用法:
    D:\\AI_Gateway_NewAPI\\.venv\\Scripts\\python.exe D:\\workbuddy2api\\agent.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IS_WINDOWS = sys.platform == "win32"
EXE = ".exe" if IS_WINDOWS else ""      # 同一份脚本：Mac/Linux 上用无后缀的编译产物

BASE = os.path.dirname(os.path.abspath(__file__))
AUTH_DIR = os.path.join(BASE, "auths")
CONFIG = os.path.join(BASE, "config.json")
CREDIT = os.path.join(BASE, f"credit{EXE}")
SIGNIN = os.path.join(BASE, f"signin{EXE}")
LOGIN = os.path.join(BASE, f"login{EXE}")
EXPIRY = os.path.join(BASE, f"expiry{EXE}")
WB2API = os.path.join(BASE, f"wb2api{EXE}")
CHECKIN_LOG = os.path.join(BASE, "data", "checkin-log.jsonl")
POOL_HTML = os.path.join(BASE, "pool.html")   # 本机控制台页面（同源伺服，无需 CORS）
LISTEN_HOST, LISTEN_PORT = "127.0.0.1", 7864
ALLOWED_ORIGINS = {
    "http://127.0.0.1:8081", "http://localhost:8081",
    "http://127.0.0.1:8080", "http://localhost:8080",
}
_lock = threading.Lock()   # 串行化对 auths/ 与二进制的操作
_login_lock = threading.Lock()
LOGIN_TTL = 900            # 一次授权链接的有效守望时长（秒）
_login: dict = {"pending": False, "url": "", "started_at": 0, "result": None, "error": ""}


def _run(exe, *args, timeout=90):
    if not os.path.exists(exe):
        return 1, "", f"找不到 {os.path.basename(exe)}，请先在本目录编译各 CLI 工具"
    try:
        p = subprocess.run([exe, *args], cwd=BASE, capture_output=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 1, "", f"{os.path.basename(exe)} 执行超时（{timeout}s）"


def read_auths():
    out = []
    if not os.path.isdir(AUTH_DIR):
        return out
    for name in sorted(os.listdir(AUTH_DIR)):
        if not (name.startswith("workbuddy-") and name.endswith(".json")):
            continue
        path = os.path.join(AUTH_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            acct = doc.get("account") or {}
            auth = doc.get("auth") or {}
            out.append({
                "uid": acct.get("uid", ""),
                "nickname": acct.get("nickname", ""),
                "enterprise_id": acct.get("enterpriseId", ""),
                "file": name,
                "expires_at": auth.get("expiresAt"),
            })
        except Exception as e:  # noqa: BLE001 — 坏文件不该让整页挂掉
            out.append({"uid": "", "nickname": "", "file": name,
                        "error": f"读取失败: {e}"})
    return out


def credits():
    """credit -json → {uid: {remain, used, size, ok, error}}"""
    code, out, err = _run(CREDIT, timeout=60)
    if code != 0 and not out.strip():
        return {}, err or "credit 执行失败"
    try:
        doc = json.loads(out.strip())
    except Exception as e:  # noqa: BLE001
        return {}, f"credit 输出无法解析: {e}"
    per = {}
    for a in doc.get("accounts") or []:
        uid = a.get("uid") or ""
        if uid:
            per[uid] = {
                "remain": a.get("remain"), "used": a.get("used"),
                "size": a.get("size"), "ok": bool(a.get("ok")),
                "error": a.get("error") or "",
            }
    total = doc.get("total") or {}
    return {"per_account": per, "total": total}, None


def accounts_view():
    accounts = read_auths()
    cr, cr_err = credits()
    per, total = (cr.get("per_account", {}), cr.get("total", {})) if cr else ({}, {})
    for a in accounts:
        c = per.get(a["uid"]) or {}
        a.update({
            "remain": c.get("remain"), "used": c.get("used"), "size": c.get("size"),
            "credit_ok": c.get("ok"), "credit_error": c.get("error", ""),
        })
        size, remain = a.get("size"), a.get("remain")
        a["pct"] = (round(remain * 100.0 / size, 1)
                    if isinstance(size, int) and isinstance(remain, int) and size > 0
                    else None)
    return {"accounts": accounts, "total": total, "credit_error": cr_err,
            "auth_dir": AUTH_DIR}


STATIC_MODELS = ["deepseek-v4.1-flash", "deepseek-v4-flash"]


def lan_ip():
    """本机主局域网 IP：UDP connect 不发包，只问内核「去公网走哪个源地址」。
    无默认路由/断网时返回空串。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return ""
    finally:
        s.close()


def _lan_url(listen, port):
    """局域网接入 URL：仅 listen 绑了非回环（0.0.0.0 / 裸 :port）时给出。"""
    exposed = (listen == "" or listen.startswith(":")
               or listen.startswith("0.0.0.0") or listen.startswith("*"))
    ip = lan_ip() if exposed else ""
    return f"http://{ip}:{port}/v1" if ip else ""


def connection_view(fetch: bool = False):
    """第三方工具（如 Deepseek-Harness-Desktop）接入 wb2api 所需的三件套。

    base_url/api_key 来自本目录 config.json。模型清单**默认不自动拉上游**：
    fetch=False 只返回 STATIC_MODELS + source="manual"（页面显示「获取上游模型」按钮），
    由用户点按钮触发 fetch=True 才调 wb2api 的 /v1/models（wb2api 内部已做 1h 缓存 +
    失败回落静态表，这里只是透传）。取不到回退静态 + source="static"（上游未连通）。
    该端点仅绑 127.0.0.1，不发到任何地方。
    """
    port, key, listen = 7863, "", ""
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        key = cfg.get("api_key") or ""
        listen = str(cfg.get("listen", ":7863"))
        if ":" in listen:
            port = int(listen.rsplit(":", 1)[1])
    except Exception:  # noqa: BLE001 — 读不到配置时给默认端口
        pass
    if not fetch:
        return {"ok": True, "base_url": f"http://127.0.0.1:{port}/v1",
                "lan_url": _lan_url(listen, port),
                "api_key": key, "models": list(STATIC_MODELS), "models_source": "manual"}
    models, source = list(STATIC_MODELS), "static"
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=6) as r:
            ids = [m.get("id") for m in (json.loads(r.read().decode()).get("data") or [])
                   if m.get("id")]
        if ids:
            # 常用默认模型排前（页面徽章优先展示），其余按上游顺序
            head = [m for m in ("cn:auto", "auto", *STATIC_MODELS) if m in ids]
            models = head + [m for m in ids if m not in head]
            source = "upstream"
    except Exception:  # noqa: BLE001 — 上游不可达用静态表，不报错
        pass
    return {"ok": True, "base_url": f"http://127.0.0.1:{port}/v1",
            "lan_url": _lan_url(listen, port),
            "api_key": key, "models": models, "models_source": source}


LOG_TAIL_BYTES = 256 * 1024


def prompt_state():
    """从网关日志探测「降级」状态（prompt 被替换成中性提示词）。

    为什么读日志：降级状态在网关进程内存（degradeGate），只绑 127.0.0.1 的
    /status 未透出该字段，也没有可查询端点。日志里的标记是唯一外部可见信号。

    判定：以最后一次「降级触发」和最后一次「网关启动/降级重置」谁更晚为准。
    passthrough 模式下一旦触发，提示词被换成 Degraded 直到 CST 次日 00:00 或
    进程重启——期间客户端的人设/工具指令全部失效，所以值得显式告警。
    """
    try:
        with open("/tmp/wb2api.log", "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            if size > LOG_TAIL_BYTES:
                f.readline()  # 丢掉可能被截断的首行
            text = f.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — 日志不可读不该让状态查询失败
        return {"degraded": None, "reason": "日志不可读"}

    last_trigger = last_reset = -1
    for i, line in enumerate(text.splitlines()):
        if "content-blocked" in line and "degraded prompt retry" in line:
            last_trigger = i
        elif "listening on" in line:  # 进程重启 → 降级状态清零
            last_reset = i
    if last_trigger < 0:
        return {"degraded": False, "reason": "未触发过降级"}
    if last_reset > last_trigger:
        return {"degraded": False, "reason": "本次启动后未触发（或已重启清零）"}
    return {"degraded": True,
            "reason": "客户端 system 提示词已被替换为中性提示词（持续到北京时间次日 00:00 或网关重启）"}


def wb2api_status():
    cfg = {}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:  # noqa: BLE001
        pass
    key = cfg.get("api_key", "")
    listen = cfg.get("listen", ":7863")
    port = 7863
    if ":" in listen:
        try:
            port = int(listen.rsplit(":", 1)[1])
        except ValueError:
            pass
    req = urllib.request.Request(f"http://127.0.0.1:{port}/status",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return {"up": True, "port": port, "status": json.loads(r.read().decode() or "{}")}
    except Exception as e:  # noqa: BLE001
        return {"up": False, "port": port, "error": str(e)}


def export_auths():
    """导出全部账号凭证（含 token）为一份迁移包 JSON。

    刻意返回明文 token：这就是"把账号搬到另一台机器"的载体，与 auths/ 目录同密级。
    端点仅绑 127.0.0.1，且只有本机浏览器能取；界面上另有"不含 token"的仅导出
    账号清单模式（见 include_tokens=False），便于只做备份/交接说明。
    """
    out = []
    for name in sorted(os.listdir(AUTH_DIR)) if os.path.isdir(AUTH_DIR) else []:
        if not (name.startswith("workbuddy-") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(AUTH_DIR, name), encoding="utf-8") as f:
                out.append({"file": name, "doc": json.load(f)})
        except Exception as e:  # noqa: BLE001
            out.append({"file": name, "error": f"{type(e).__name__}: {e}"})
    return {"ok": True, "service": "workbuddy2api", "kind": "auths-export",
            "version": 1, "exported_at": int(time.time()), "count": len(out),
            "accounts": out}


def import_auths(items, overwrite=True, dry_run=False):
    """导入凭证数组（导出包里的 accounts，或裸的单个 auth 文档数组）。

    校验从严（导入的是要长期使用的凭证，写坏比拒绝更糟）：
    - 必须能解析出 accessToken（auth.Parse 同口径）；缺失即拒绝该条并说明；
    - uid 从 account.uid 取；取不到时保留原文件名（或按内容哈希兜底命名）；
    - 已存在且 overwrite=False → 跳过并如实报告（不静默覆盖你的现有会话）。
    """
    results = []
    for it in items or []:
        doc = it.get("doc") if isinstance(it, dict) and "doc" in it else it
        name = (it.get("file") if isinstance(it, dict) else None) or ""
        if not isinstance(doc, dict):
            results.append({"file": name, "ok": False, "error": "不是合法的 auth 文档"})
            continue
        auth = doc.get("auth") if isinstance(doc.get("auth"), dict) else doc
        token = auth.get("accessToken") or auth.get("access_token") or ""
        acct = doc.get("account") if isinstance(doc.get("account"), dict) else doc
        uid = (acct.get("uid") or doc.get("uid") or "").strip()
        if not token:
            results.append({"file": name, "uid": uid, "ok": False,
                            "error": "缺少 accessToken（不是有效的凭证文件）"})
            continue
        if not name:
            name = f"workbuddy-{uid}.json" if uid else ""
        if not (name.startswith("workbuddy-") and name.endswith(".json")):
            name = f"workbuddy-{uid or hashlib.sha1(token.encode()).hexdigest()[:16]}.json"
        path = os.path.join(AUTH_DIR, os.path.basename(name))
        nickname = acct.get("nickname") or doc.get("nickname") or ""
        if os.path.exists(path) and not overwrite:
            results.append({"file": os.path.basename(name), "uid": uid, "ok": False,
                            "skipped": True, "error": "已存在（未勾选覆盖）"})
            continue
        if dry_run:
            results.append({"file": os.path.basename(name), "uid": uid,
                            "nickname": nickname, "ok": True, "dry_run": True})
            continue
        try:
            os.makedirs(AUTH_DIR, exist_ok=True)
            write_auth_file(path, doc)
            results.append({"file": os.path.basename(name), "uid": uid,
                            "nickname": nickname, "ok": True})
        except Exception as e:  # noqa: BLE001
            results.append({"file": os.path.basename(name), "uid": uid, "ok": False,
                            "error": f"{type(e).__name__}: {e}"})
    imported = [r for r in results if r.get("ok")]
    return {"ok": True, "imported": len(imported), "total": len(results),
            "results": results, "dry_run": bool(dry_run)}


USAGE_FILE = os.path.join(BASE, "data", "usage.jsonl")


def usage_stats(days: int = 30):
    """聚合网关落盘的用量 JSONL → 按天/模型/账号的消耗与 token 统计。

    数据源是网关自己写的 data/usage.jsonl（internal/usage 模块），每次成功请求
    一行。这里只做读+聚合，不写。文件读尾部若干 MB 足够覆盖 days 天。
    """
    if not os.path.exists(USAGE_FILE):
        return {"ok": True, "empty": True, "days": days,
                "note": "还没有用量记录（网关需配置 usage_file 并产生请求）",
                "daily": [], "by_model": [], "by_account": [], "totals": {}}
    try:
        with open(USAGE_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            cap = 8 * 1024 * 1024          # 只读尾部 8MB
            f.seek(max(0, size - cap))
            if size > cap:
                f.readline()
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "daily": [], "by_model": [], "by_account": [], "totals": {}}

    cutoff = time.time() - days * 86400
    daily, by_model, by_account = {}, {}, {}
    totals = {"requests": 0, "credit": 0.0, "prompt": 0, "completion": 0, "total": 0}
    for ln in lines:
        try:
            e = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        ts = e.get("ts") or 0
        if ts < cutoff:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        credit = float(e.get("credit") or 0)
        prompt = int(e.get("prompt") or 0)
        comp = int(e.get("completion") or 0)
        tot = int(e.get("total") or (prompt + comp))
        model = e.get("model") or "?"
        realm = e.get("realm") or "cn"

        d = daily.setdefault(day, {"day": day, "requests": 0, "credit": 0.0,
                                   "prompt": 0, "completion": 0, "total": 0, "by_model": {}})
        d["requests"] += 1; d["credit"] += credit
        d["prompt"] += prompt; d["completion"] += comp; d["total"] += tot
        dm = d["by_model"].setdefault(model, {"credit": 0.0, "total": 0, "requests": 0})
        dm["credit"] += credit; dm["total"] += tot; dm["requests"] += 1

        m = by_model.setdefault(model, {"model": model, "realm": realm, "requests": 0,
                                        "credit": 0.0, "prompt": 0, "completion": 0, "total": 0})
        m["requests"] += 1; m["credit"] += credit
        m["prompt"] += prompt; m["completion"] += comp; m["total"] += tot

        a = by_account.setdefault(e.get("uid") or "?", {"uid": e.get("uid") or "?",
                                                         "requests": 0, "credit": 0.0,
                                                         "prompt": 0, "completion": 0, "total": 0})
        a["requests"] += 1; a["credit"] += credit
        a["prompt"] += prompt; a["completion"] += comp; a["total"] += tot

        totals["requests"] += 1; totals["credit"] += credit
        totals["prompt"] += prompt; totals["completion"] += comp; totals["total"] += tot

    # 昵称映射（uid → nickname），让前端显示可读名称
    nick = {a["uid"]: (a.get("nickname") or a["uid"][:8]) for a in read_auths()}
    accs = list(by_account.values())
    for a in accs:
        a["nickname"] = nick.get(a["uid"], a["uid"][:8])
    accs.sort(key=lambda x: -x["credit"])

    daily_list = sorted(daily.values(), key=lambda x: x["day"])
    for d in daily_list:
        d["by_model"] = [{"model": k, **v} for k, v in
                         sorted(d["by_model"].items(), key=lambda kv: -kv[1]["credit"])]
    models = sorted(by_model.values(), key=lambda x: -x["credit"])
    totals["credit"] = round(totals["credit"], 2)

    today = time.strftime("%Y-%m-%d")
    d7 = time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400))
    month = today[:7] + "-01"
    def sum_since(since):
        return round(sum(d["credit"] for d in daily_list if d["day"] >= since), 2)
    return {"ok": True, "empty": not daily_list, "days": days,
            "today_credit": sum_since(today),
            "week_credit": sum_since(d7),
            "month_credit": sum_since(month),
            "daily": daily_list, "by_model": models,
            "by_account": accs, "totals": totals}


# ---------- 设置读写（白名单 + 原子写 + 需重启提示） ----------

# 可在线调整的配置项：(键路径, 类型, 说明)。刻意只暴露常用项——
# 全量 config 在线编辑风险太高（写坏 upstream 段会直接打不通上游），
# 需要冷门项时改 config.json 再重启。
SETTINGS_SCHEMA = [
    ("schedule.checkin_enabled",   bool, "自动签到（每日 09:00 / 21:00，国际号自动跳过）"),
    ("schedule.travel_hours",      list, "猫猫旅行时点"),
    ("schedule.activity_enabled",  bool, "活跃上报（每日 10:00，用于连登/领养前置）"),
    ("schedule.keepalive_enabled", bool, "Token 保活（每日 22:00 全账号刷新）"),
    ("schedule.school_enabled",    bool, "开学季任务"),
    ("schedule.cat_enabled",       bool, "夜猫子任务"),
]


def _get_path(doc, dotted):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def _set_path(doc, dotted, value):
    parts = dotted.split(".")
    cur = doc
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def read_settings():
    """读当前设置（只回白名单项 + 只读展示项）。"""
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读取 config.json 失败: {e}"}
    items = []
    # 未显式配置时的默认值（与 internal/config.DefaultSchedule 对齐）：
    # 不填默认会让 list 项显示成空数组——用户一保存就把该任务关掉（如 travel_hours=[]）。
    defaults = {
        "schedule.checkin_enabled": True, "schedule.activity_enabled": True,
        "schedule.keepalive_enabled": True, "schedule.school_enabled": True,
        "schedule.cat_enabled": True,
        "schedule.travel_hours": [9, 21],
    }
    for key, typ, label in SETTINGS_SCHEMA:
        val, present = _get_path(cfg, key)
        if not present or val in (None, []):
            val = defaults.get(key, [] if typ is list else False)
        items.append({"key": key, "type": typ.__name__, "label": label,
                      "value": val, "explicit": present})
    # 只读展示项（改这些要动 config.json，不放开关）
    ro = {
        "listen": cfg.get("listen", ""),
        "auth_dir": cfg.get("auth_dir", ""),
        "usage_file": cfg.get("usage_file", ""),
        "prompt_mode": (cfg.get("prompt") or {}).get("mode", "passthrough"),
        "max_in_flight": (cfg.get("pool") or {}).get("max_in_flight"),
        "soft_rate": (cfg.get("cooldown") or {}).get("soft_rate"),
        "sanitize": (cfg.get("features") or {}).get("sanitize_blacklist_fingerprints"),
    }
    return {"ok": True, "items": items, "readonly": ro}


def write_settings(changes):
    """按白名单写入设置项；bool 直接改、list 需为合法小时数组。

    原子写（tmp + rename）避免写坏配置；写前先备份一份 .bak。
    """
    if not isinstance(changes, dict) or not changes:
        return {"ok": False, "error": "没有要修改的项"}
    allowed = {k: t for k, t, _ in SETTINGS_SCHEMA}
    unknown = [k for k in changes if k not in allowed]
    if unknown:
        return {"ok": False, "error": f"不支持的配置项: {', '.join(unknown)}"}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读取 config.json 失败: {e}"}

    applied = []
    for key, val in changes.items():
        typ = allowed[key]
        if typ is bool:
            if not isinstance(val, bool):
                return {"ok": False, "error": f"{key} 需要布尔值"}
        else:  # list：小时数组
            if not isinstance(val, list) or any(
                    not isinstance(h, int) or h < 0 or h > 23 for h in val):
                return {"ok": False, "error": f"{key} 需要 0-23 的小时数组，如 [9, 21]"}
            if not val:
                # 空数组 = 该任务无时点可跑（等于悄悄关掉）。要停任务请用对应的
                # *_enabled 开关，语义明确、可回滚。
                return {"ok": False, "error": f"{key} 不能为空；要停该任务请用对应的开关（如 travel_enabled）"}
            val = sorted(set(val))
        _set_path(cfg, key, val)
        applied.append(key)

    with _lock:
        try:
            shutil.copy2(CONFIG, CONFIG + ".bak")
            tmp = CONFIG + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"写入失败: {type(e).__name__}: {e}"}
    restart = restart_wb2api()
    return {"ok": True, "applied": applied, "restart": restart,
            "note": "已重启网关使配置生效"}


def write_auth_file(path, doc):
    """原子写凭证文件并强制 0600（仅本人可读）。

    普通 open(..., "w") 会按进程 umask 落盘（实测 644 = 同机其他用户可读），
    而这里存的是可用的账号 token。先写临时文件再 os.replace，避免写一半被读到；
    权限在写盘时就用 os.open 的 mode 指定（不依赖后续 chmod，避免窗口期）。
    """
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, ensure_ascii=False)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def signin_one(uid):
    """单账号签到：临时目录里只放它一个 auth 文件，再跑官方 signin。"""
    target = None
    for a in read_auths():
        if a["uid"] == uid:
            target = a
            break
    if not target:
        return {"ok": False, "error": f"账号 {uid} 不在 auths 目录"}
    tmp = os.path.join(AUTH_DIR, f".single-{uid}")
    with _lock:
        try:
            os.makedirs(tmp, exist_ok=True)
            shutil.copy2(os.path.join(AUTH_DIR, target["file"]),
                         os.path.join(tmp, target["file"]))
            code, out, err = _run(SIGNIN, tmp, timeout=90)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    text = (out + err).strip()
    log_checkin(uid, target.get("nickname", ""), code == 0, text)
    return {"ok": code == 0, "uid": uid, "output": text[-1500:] or f"exit={code}"}


def restart_wb2api():
    """原生进程改了 auths 需要重启才会加载（容器模式同理）。

    Windows 优先用已注册的计划任务 `schtasks /Run`（那是任务自己的启动方式，
    最不容易出"进程被父进程带走"的问题），没有任务时退回直接 Popen；
    POSIX（Mac）用 pkill -x 精确匹配进程名 + start_new_session 脱离父进程。
    """
    with _lock:
        if IS_WINDOWS:
            # taskkill 可能不可用（被安全策略拦、或进程已退出）——绝不能因此
            # 让整个重启流程中断，后面 Popen 才是关键一步。
            try:
                subprocess.run(["taskkill", "/IM", "wb2api.exe", "/F"],
                               capture_output=True, timeout=30)
            except Exception as e:  # noqa: BLE001
                print(f"[restart] taskkill 跳过: {type(e).__name__}: {e}", flush=True)
            time.sleep(1)
            ran = None
            try:
                ran = subprocess.run(["schtasks", "/Run", "/TN", "workbuddy2api"],
                                     capture_output=True, timeout=30)
            except Exception as e:  # noqa: BLE001
                print(f"[restart] schtasks 不可用: {type(e).__name__}", flush=True)
            if ran is None or ran.returncode != 0:
                if not os.path.exists(WB2API):
                    return {"ok": False, "error": "找不到 wb2api.exe"}
                DETACHED = 0x00000008 | 0x00000200
                subprocess.Popen([WB2API, "-config", CONFIG], cwd=BASE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, creationflags=DETACHED)
        else:
            try:
                # -x 只匹配进程名本身（wb2api），不会误杀路径里带
                # workbuddy2api 的本助手（comm 是 python3）
                subprocess.run(["pkill", "-x", "wb2api"],
                               capture_output=True, timeout=15)
            except Exception as e:  # noqa: BLE001
                print(f"[restart] pkill 跳过: {type(e).__name__}: {e}", flush=True)
            time.sleep(1)
            if not os.path.exists(WB2API):
                return {"ok": False, "error": f"找不到 {os.path.basename(WB2API)}"}
            subprocess.Popen([WB2API, "-config", CONFIG], cwd=BASE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(4)
    return {"ok": True, "status": wb2api_status()}


def login_start(realm: str = "cn"):
    """生成授权链接并**在后台守望**直到登录完成。

    realm："cn"（copilot.tencent.com）/ "global"（www.workbuddy.ai）。
    login 二进制的 url 与 poll 必须同域（state 文件带 realm 校验），所以 realm
    随守望状态一起记住，守望线程与手动认领都复用它，绝不中途换域。
    state 文件全局唯一（login url 每次覆盖）→ 已有另一域的进行中登录时直接挡下，
    等它完成或过期，避免互相踩掉 state。

    多账号场景的两个硬约束决定了这个设计：
    - `login poll` 是单次 GET，不是轮询；
    - `login url` 每次都覆盖硬编码的 state 文件。
    所以"点一次按钮 → 用户慢慢在无痕窗口里登录 → 页面自动认领"是唯一不撞车的形态：
    守望线程挂在生成出来的那一个 state 上重试，期间不会再生成新 state。
    """
    global _login
    if realm not in ("cn", "global"):
        return {"ok": False, "error": f"未知 realm: {realm}"}
    with _login_lock:
        cur = _login
        if cur.get("pending") and time.time() - cur.get("started_at", 0) < LOGIN_TTL:
            if cur.get("realm", "cn") != realm:
                return {"ok": False, "error": "已有另一版本（%s）的登录进行中；"
                            "登录状态文件全局唯一，请先完成它或等其过期（15 分钟）"
                            % ("国际版" if cur.get("realm") == "global" else "国内版")}
            # 已有进行中的登录：复用它的链接，绝不覆盖 state
            return {"ok": True, "url": cur["url"], "pending": True, "reused": True,
                    "realm": realm}
    code, out, err = _run(LOGIN, f"--realm={realm}", "url", timeout=45)
    if code != 0 and not out.strip():
        return {"ok": False, "error": (err or "生成授权链接失败").strip()}
    url = out.strip()
    with _login_lock:
        _login = {"pending": True, "url": url, "started_at": time.time(),
                  "result": None, "error": "", "realm": realm}
    threading.Thread(target=_watch_login, args=(url, realm), daemon=True).start()
    return {"ok": True, "url": url, "pending": True, "reused": False, "realm": realm}


def _watch_login(expected_url: str, realm: str = "cn") -> None:
    global _login
    deadline = time.time() + LOGIN_TTL
    while time.time() < deadline:
        time.sleep(5)
        with _login_lock:
            if not _login.get("pending") or _login.get("url") != expected_url:
                return  # 被新的登录流程取代
        code, out, err = _run(LOGIN, f"--realm={realm}", "poll", timeout=60)
        if code == 0 and out.strip().startswith("{"):
            try:
                doc = json.loads(out.strip())
            except ValueError:
                continue
            uid = doc.get("uid") or ""
            if not uid:
                continue
            expires_in = int(doc.get("expires_in") or 0)
            auth = {
                "account": {"uid": uid, "enterpriseId": doc.get("enterprise_id", ""),
                            "nickname": doc.get("nickname", "")},
                # realm 恒写（login poll 输出保证非空）：global 账号若只靠 domain
                # 回落推断，域判定会晚一拍；显式落盘与 login.sh 官方流程对齐。
                "auth": {"accessToken": doc.get("access_token", ""),
                         "refreshToken": doc.get("refresh_token", ""),
                         "expiresAt": int(time.time()) + expires_in,
                         "domain": doc.get("domain", ""),
                         "realm": doc.get("realm") or realm},
            }
            os.makedirs(AUTH_DIR, exist_ok=True)
            path = os.path.join(AUTH_DIR, f"workbuddy-{uid}.json")
            existed = os.path.exists(path)
            write_auth_file(path, auth)
            restart = restart_wb2api()
            with _login_lock:
                _login = {"pending": False, "url": expected_url,
                          "started_at": time.time(),
                          "result": {"uid": uid, "nickname": doc.get("nickname", ""),
                                     "updated": existed, "restart": restart,
                                     "realm": auth["auth"]["realm"]},
                          "error": "", "realm": realm}
            return
    with _login_lock:
        if _login.get("pending") and _login.get("url") == expected_url:
            _login = {"pending": False, "url": expected_url,
                      "started_at": time.time(), "result": None,
                      "error": "超时未完成登录（链接已失效，请重新生成）", "realm": realm}


def login_status():
    with _login_lock:
        cur = dict(_login)
    cur.setdefault("realm", "cn")
    cur["ok"] = True
    cur["accounts"] = len(read_auths())
    return cur


def login_complete():
    """手动触发：用户点"我已登录"时的兜底（守望线程失败时仍可自救）。

    realm 取进行中登录记住的域（url 与 poll 必须同域，state 文件里也有校验）。
    """
    with _login_lock:
        realm = _login.get("realm") or "cn"
    code, out, err = _run(LOGIN, f"--realm={realm}", "poll", timeout=60)
    if code != 0:
        return {"ok": False, "error": (err or "获取 token 失败（登录可能还没完成）").strip()}
    try:
        doc = json.loads(out.strip())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"poll 输出无法解析: {e}"}
    uid = doc.get("uid") or ""
    if not uid:
        return {"ok": False, "error": "未获取到 uid，token 可能无效"}
    expires_in = int(doc.get("expires_in") or 0)
    auth = {
        "account": {"uid": uid, "enterpriseId": doc.get("enterprise_id", ""),
                    "nickname": doc.get("nickname", "")},
        "auth": {"accessToken": doc.get("access_token", ""),
                 "refreshToken": doc.get("refresh_token", ""),
                 "expiresAt": int(time.time()) + expires_in,
                 "domain": doc.get("domain", ""),
                 "realm": doc.get("realm") or realm},
    }
    os.makedirs(AUTH_DIR, exist_ok=True)
    path = os.path.join(AUTH_DIR, f"workbuddy-{uid}.json")
    existed = os.path.exists(path)
    write_auth_file(path, auth)
    restarted = restart_wb2api()
    return {"ok": True, "uid": uid, "nickname": auth["account"]["nickname"],
            "realm": auth["auth"]["realm"],
            "updated": existed, "file": os.path.basename(path),
            "restart": restarted}


# ---------- 积分到期巡检（expiry 二进制 → 15min 缓存，UI 显示“更新于”） ----------
_expiry_cache: dict = {"ts": 0, "data": None}
EXPIRY_TTL = 900
_expiry_lock = threading.Lock()


def expiries(fresh: bool = False):
    with _expiry_lock:
        if not fresh and _expiry_cache["data"] is not None \
                and time.time() - _expiry_cache["ts"] < EXPIRY_TTL:
            return {**_expiry_cache["data"], "cached": True}
        code, out, err = _run(EXPIRY, timeout=120)
        if code != 0 and not out.strip():
            doc = {"ok": False, "error": (err or "expiry 执行失败").strip()[:400],
                   "accounts": {}, "fetched_at": int(time.time())}
        else:
            try:
                raw = json.loads(out.strip())
                per = {}
                for a in raw.get("accounts") or []:
                    per[a.get("uid") or a.get("file") or ""] = a
                doc = {"ok": True, "accounts": per,
                       "fetched_at": int(raw.get("ts") or time.time())}
            except Exception as e:  # noqa: BLE001
                doc = {"ok": False, "error": f"expiry 输出无法解析: {e}",
                       "accounts": {}, "fetched_at": int(time.time())}
        _expiry_cache["ts"] = time.time()
        _expiry_cache["data"] = doc
        return {**doc, "cached": False}


# ---------- 签到日志（agent 发起的手动/批量签到落一行 JSONL） ----------
def log_checkin(uid, nickname, ok, output):
    try:
        os.makedirs(os.path.dirname(CHECKIN_LOG), exist_ok=True)
        with open(CHECKIN_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "uid": uid, "nickname": nickname,
                                "ok": bool(ok), "output": (output or "")[:300]},
                               ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 日志失败不能拖累签到
        pass


def checkin_log(limit_days=30, tail=300):
    if not os.path.exists(CHECKIN_LOG):
        return {"ok": True, "entries": [], "by_day": {}}
    try:
        with open(CHECKIN_LOG, encoding="utf-8") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))  # 只读尾部 64KB，日志无限增长也不怕
            if size > 64 * 1024:
                f.readline()  # 丢掉可能被截断的首行
            lines = f.read().splitlines()
        cutoff = time.time() - limit_days * 86400
        entries, by_day = [], {}
        for ln in reversed(lines):
            try:
                e = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            if e.get("ts", 0) < cutoff:
                continue
            entries.append(e)
            day = time.strftime("%Y-%m-%d", time.localtime(e["ts"]))
            b = by_day.setdefault(day, {"ok": 0, "fail": 0})
            b["ok" if e.get("ok") else "fail"] += 1
        return {"ok": True, "entries": entries[:tail], "by_day": by_day}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "entries": [], "by_day": {}}


def upstream_cfg():
    """(port, api_key) —— 转发控制页请求到 wb2api 时复用 config.json 的鉴权。"""
    port, key = 7863, ""
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        key = cfg.get("api_key") or ""
        listen = cfg.get("listen", ":7863")
        if ":" in listen:
            port = int(listen.rsplit(":", 1)[1])
    except Exception:  # noqa: BLE001
        pass
    return port, key


class Handler(BaseHTTPRequestHandler):
    server_version = "wb2api-agent/1.0"

    def log_message(self, fmt, *args):  # 静默：助手日志走 stdout
        pass

    def _cors(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_page(self):
        with open(POOL_HTML, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_upstream(self, method, subpath, body=None):
        """把控制页的 API 调用转发给 wb2api（同源，绕开浏览器 CORS）。

        响应逐块透传：流式 SSE 保持实时性，非流式等价普通 JSON。
        """
        port, key = upstream_cfg()
        headers = {"Authorization": f"Bearer {key}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{port}{subpath}",
                                     data=body, method=method, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=300)  # noqa: S310 — 固定本机回环
        except urllib.error.HTTPError as e:
            resp = e          # 上游 4xx/5xx 原样透传给页面展示
        except Exception as e:  # noqa: BLE001
            self._send(502, {"ok": False, "error": f"wb2api 转发失败: {e}"})
            return
        try:
            self.send_response(resp.status)
            self.send_header("Content-Type",
                             resp.headers.get("Content-Type", "application/json"))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/pool", "/pool.html"):
                if os.path.exists(POOL_HTML):
                    self._send_page()
                elif path == "/":
                    self._send(200, {"ok": True, "service": "wb2api-agent",
                                     "wb2api": wb2api_status(),
                                     "auth_count": len(read_auths())})
                else:
                    self._send(404, {"ok": False, "error": "缺少 pool.html"})
            elif path == "/health":
                self._send(200, {"ok": True, "service": "wb2api-agent",
                                 "wb2api": wb2api_status(),
                                 "prompt": prompt_state(),
                                 "auth_count": len(read_auths())})
            elif path == "/accounts":
                self._send(200, {"ok": True, **accounts_view()})
            elif path == "/accounts/export":
                self._send(200, export_auths())
            elif path == "/settings":
                self._send(200, read_settings())
            elif path == "/connection":
                self._send(200, connection_view(fetch="fetch=1" in self.path))
            elif path == "/login/status":
                self._send(200, login_status())
            elif path == "/status":
                self._send(200, {"ok": True, **wb2api_status()})
            elif path.startswith("/expiries"):
                self._send(200, expiries(fresh="fresh=1" in self.path))
            elif path == "/checkin-log":
                self._send(200, checkin_log())
            elif path == "/usage":
                days = 30
                if "days=" in self.path:
                    try:
                        days = max(1, min(365, int(self.path.split("days=")[1].split("&")[0])))
                    except Exception:  # noqa: BLE001
                        pass
                self._send(200, usage_stats(days))
            elif path == "/upstream/models":
                self._proxy_upstream("GET", "/v1/models")
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path == "/login/start":
                realm = "cn"
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        realm = str(json.loads(self.rfile.read(length)).get("realm") or "cn").lower()
                    except Exception:  # noqa: BLE001 — 坏 body 按默认 cn
                        pass
                self._send(200, login_start(realm))
            elif path == "/login/complete":
                self._send(200, login_complete())
            elif path == "/wb2api/restart":
                self._send(200, restart_wb2api())
            elif path == "/settings":
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length)) if length else {}
                except Exception as e:  # noqa: BLE001
                    self._send(400, {"ok": False, "error": f"请求体不是合法 JSON: {e}"})
                    return
                self._send(200, write_settings(body.get("changes") if isinstance(body, dict) else None))
            elif path == "/accounts/import":
                length = int(self.headers.get("Content-Length") or 0)
                body = {}
                if length:
                    try:
                        body = json.loads(self.rfile.read(length))
                    except Exception as e:  # noqa: BLE001
                        self._send(400, {"ok": False, "error": f"请求体不是合法 JSON: {e}"})
                        return
                items = body.get("accounts") if isinstance(body, dict) else body
                if not isinstance(items, list):
                    self._send(400, {"ok": False,
                                     "error": "需要 accounts 数组（导出包或裸凭证数组）"})
                    return
                res = import_auths(items, overwrite=bool(body.get("overwrite", True)),
                                   dry_run=bool(body.get("dry_run", False)))
                # 真正写入后需重启才加载新账号（池在启动时扫描目录）；dry_run 不动。
                if res["imported"] and not res["dry_run"]:
                    res["restart"] = restart_wb2api()
                self._send(200, res)
            elif path == "/upstream/chat":
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                self._proxy_upstream("POST", "/v1/chat/completions", body)
            elif path.startswith("/accounts/") and path.endswith("/checkin"):
                uid = path[len("/accounts/"):-len("/checkin")].strip("/")
                self._send(200, signin_one(uid))
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def watchdog_loop(tick: int = 30) -> None:
    """原生模式下 wb2api 没有自愈：它挂了就一直挂着（容器才有 restart 策略）。

    助手本来就在跑，让它顺手把这件事补上 —— 每 30 秒探一次 /healthz，
    连续两次不通就重启（用自己的计划任务优先，退回直接拉起）。
    只做"拉活"，不改配置、不清状态。
    """
    misses = 0
    while True:
        time.sleep(tick)
        try:
            st = wb2api_status()
            if st.get("up"):
                misses = 0
                continue
            misses += 1
            if misses < 2:
                continue
            print(f"[watchdog] wb2api 无响应（连续 {misses} 次），重启中…", flush=True)
            res = restart_wb2api()
            ok = bool((res.get("status") or {}).get("up"))
            print(f"[watchdog] 重启结果: {'成功' if ok else '失败'} {res.get('error', '')}",
                  flush=True)
            misses = 0
        except Exception as e:  # noqa: BLE001 — 看门狗自己不能死
            print(f"[watchdog] 异常: {type(e).__name__}: {e}", flush=True)


def main():
    if not os.path.isdir(AUTH_DIR):
        os.makedirs(AUTH_DIR, exist_ok=True)
    threading.Thread(target=watchdog_loop, daemon=True).start()
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"wb2api-agent listening on http://{LISTEN_HOST}:{LISTEN_PORT} "
          f"(auths={AUTH_DIR})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
