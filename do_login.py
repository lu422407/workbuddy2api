# -*- coding: utf-8 -*-
"""WorkBuddy2API 一键登录（Windows 版 login.sh）。

用法（在本机终端）:
    D:\\AI_Gateway_NewAPI\\.venv\\Scripts\\python.exe D:\\workbuddy2api\\do_login.py

流程: 生成授权链接 → 浏览器登录 CodeBuddy → 回车 → 拿 token → 签到 →
落盘 auths/workbuddy-<uid>.json → 重启 workbuddy2api 容器。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOGIN_EXE = os.path.join(HERE, "login.exe")
AUTH_DIR = os.path.join(HERE, "auths")
DOCKER_EXE = r"D:\Docker\Docker\resources\bin\docker.exe"
CONTAINER = "workbuddy2api"


def run_login(*args):
    out = subprocess.check_output([LOGIN_EXE, *args], text=True, timeout=60)
    return out.strip()


def main():
    if not os.path.exists(LOGIN_EXE):
        sys.exit("找不到 login.exe —— 先在 D:\\workbuddy2api 目录里确认它存在")

    url = run_login("url")
    print("=" * 60)
    print("请在浏览器打开下面的链接，登录你的 CodeBuddy 账号：")
    print()
    print(url)
    print()
    input("登录完成后按回车继续 > ")

    print("正在获取 token ...")
    try:
        result = json.loads(run_login("poll"))
    except Exception as e:
        sys.exit(f"获取 token 失败（可能登录还没完成就按了回车）：{e}\n重新运行本脚本再试一次。")

    token = result["access_token"]
    refresh = result["refresh_token"]
    expires_in = int(result["expires_in"])
    uid = result.get("uid", "")
    ent_id = result.get("enterprise_id", "")
    domain = result.get("domain", "")
    nickname = result.get("nickname", "")
    if not uid:
        sys.exit("无法获取 uid，token 可能无效")
    expires_at = int(time.time()) + expires_in

    # 签到（幂等；已签到会返回业务提示，不阻塞）
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-User-Id": uid,
    }
    if ent_id:
        headers["X-Enterprise-Id"] = ent_id
        headers["X-Tenant-Id"] = ent_id
    if domain:
        headers["X-Domain"] = domain
    try:
        req = urllib.request.Request(
            "https://www.codebuddy.cn/v2/billing/meter/daily-checkin",
            method="POST", data=b"{}", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode() or "{}")
        if body.get("code") == 0:
            print("签到: 成功", json.dumps(body.get("data") or {}, ensure_ascii=False)[:150])
        else:
            print("签到:", body.get("msg", json.dumps(body)[:150]))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
            print("签到:", body.get("msg", f"http {e.code}"))
        except Exception:
            print(f"签到: http {e.code}")
    except Exception as e:
        print("签到:", e)

    # 落盘 auth 文件（与 workbuddy2api internal/auth 读取格式一致）
    os.makedirs(AUTH_DIR, exist_ok=True)
    auth_path = os.path.join(AUTH_DIR, f"workbuddy-{uid}.json")
    action = "覆盖更新" if os.path.exists(auth_path) else "新增"
    with open(auth_path, "w", encoding="utf-8") as f:
        json.dump({
            "account": {"uid": uid, "enterpriseId": ent_id, "nickname": nickname},
            "auth": {"accessToken": token, "refreshToken": refresh,
                     "expiresAt": expires_at, "domain": domain},
        }, f, indent=1, ensure_ascii=False)
    print(f"已保存（{action}）: {auth_path}")

    # 重启容器加载新账号
    if os.path.exists(DOCKER_EXE):
        subprocess.run([DOCKER_EXE, "restart", CONTAINER],
                       capture_output=True, timeout=60)
        print(f"已重启 {CONTAINER}")
    else:
        print("未找到 docker.exe，请手动重启 workbuddy2api 容器")

    time.sleep(3)
    try:
        req = urllib.request.Request("http://127.0.0.1:7863/status", timeout=10)
        # /status 需要鉴权，这里只做可达性提示
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError:
        print("服务已响应（账号池状态到 Dashboard「WorkBuddy 池」页查看）")
    except Exception as e:
        print(f"状态查询: {e}（服务可能还在重启，稍等几秒刷新 Dashboard）")

    print()
    print("=" * 60)
    print("登录完成！")
    print("  UID:", uid)
    print("  昵称:", nickname or "（未获取到）")
    print("  到 Dashboard「WorkBuddy 池」页确认账号已入池。")


if __name__ == "__main__":
    main()
