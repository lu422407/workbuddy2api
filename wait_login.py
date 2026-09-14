# -*- coding: utf-8 -*-
"""等待 OAuth 登录完成 → 自动落盘 → 重启 wb2api。

为什么需要它：`login.exe poll` 是**单次 GET**（不是轮询），而且每次
`login.exe url` 都会覆盖 state 文件 —— 手工流程很容易"在错误的 state 上 poll"。
本脚本只在**同一个** state 上反复 poll，一旦登录完成就自动完成后续步骤。

用法:
    python wait_login.py [最长等待秒数，默认 900]
"""
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = sys.platform == "win32"
LOGIN = os.path.join(BASE, "login.exe" if IS_WINDOWS else "login")
AUTH_DIR = os.path.join(BASE, "auths")
DOCKER = r"D:\Docker\Docker\resources\bin\docker.exe"


def poll_once():
    try:
        p = subprocess.run([LOGIN, "poll"], cwd=BASE, capture_output=True,
                           timeout=60, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None, "poll 超时"
    text = (p.stdout or "").strip()
    if p.returncode == 0 and text.startswith("{"):
        try:
            return json.loads(text), None
        except Exception as e:  # noqa: BLE001
            return None, f"解析失败: {e}"
    return None, (p.stderr or text or "未知错误").strip()


def restart_wb2api():
    win = sys.platform == "win32"
    exe = "wb2api.exe" if win else "wb2api"
    if os.path.exists(os.path.join(BASE, exe)):
        try:
            if win:
                subprocess.run(["taskkill", "/IM", "wb2api.exe", "/F"],
                               capture_output=True, timeout=30)
            else:
                subprocess.run(["pkill", "-x", "wb2api"],
                               capture_output=True, timeout=15)
        except Exception:  # noqa: BLE001 — 杀进程失败不该中断重启
            pass
        time.sleep(1)
        detached = ({"creationflags": 0x00000008 | 0x00000200} if win
                    else {"start_new_session": True})
        subprocess.Popen([os.path.join(BASE, exe),
                          "-config", os.path.join(BASE, "config.json")],
                         cwd=BASE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, **detached)
        time.sleep(3)
        return "native"
    if os.path.exists(DOCKER):
        subprocess.run([DOCKER, "restart", "workbuddy2api"],
                       capture_output=True, timeout=60)
        return "docker"
    return "none"


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 900
    deadline = time.time() + limit
    print(f"等待登录完成（最多 {limit} 秒，每 5 秒检查一次）…", flush=True)
    n = 0
    while time.time() < deadline:
        n += 1
        doc, err = poll_once()
        if doc:
            uid = doc.get("uid") or ""
            if not uid:
                print(f"[{n}] token 拿到但缺 uid，继续等：{doc}", flush=True)
            else:
                expires_in = int(doc.get("expires_in") or 0)
                auth = {
                    "account": {"uid": uid,
                                "enterpriseId": doc.get("enterprise_id", ""),
                                "nickname": doc.get("nickname", "")},
                    "auth": {"accessToken": doc.get("access_token", ""),
                             "refreshToken": doc.get("refresh_token", ""),
                             "expiresAt": int(time.time()) + expires_in,
                             "domain": doc.get("domain", "")},
                }
                os.makedirs(AUTH_DIR, exist_ok=True)
                path = os.path.join(AUTH_DIR, f"workbuddy-{uid}.json")
                existed = os.path.exists(path)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(auth, f, indent=1, ensure_ascii=False)
                mode = restart_wb2api()
                print(f"✅ 登录完成：{'更新' if existed else '新增'} {path}", flush=True)
                print(f"   昵称: {doc.get('nickname', '')}", flush=True)
                print(f"   重启方式: {mode}", flush=True)
                return 0
        else:
            if n % 6 == 1:
                print(f"[{n}] {err}", flush=True)
        time.sleep(5)
    print("⏱ 超时未完成登录。重新生成授权链接后再试。", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
