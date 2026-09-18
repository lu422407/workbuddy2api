# 本机部署清单（三网关 + 控制台）

这份文档记录**当前这台 Mac 上实际装了什么、怎么装的**，以便在另一台电脑上复现。
所有内容均按本机真实状态整理，未使用的方案不写入。

## 总览

四个常驻服务，全部由 macOS **LaunchAgent** 拉起（无 Docker）：

| 服务 | 目录 | 语言/运行时 | 监听 | LaunchAgent |
|---|---|---|---|---|
| WorkBuddy 网关 | `~/workbuddy2api` | Go 1.22+ | `*:7863` | `com.workbuddy.wb2api` |
| WorkBuddy 控制台 | `~/workbuddy2api` | Python 3.9（系统自带） | `127.0.0.1:7864` | `com.workbuddy.wb2api-agent` |
| QoderWork 网关 | `~/qoderwork2api` | Go 1.23 | `127.0.0.1:7865` | `com.qoderwork.qw2api` |
| MiMo 反代 | `~/xiaomi-mimo-desktop-api` | Python 3.14 + venv | `*:7870` | `com.mimo.desktop-api` |

控制台（`http://127.0.0.1:7864`）是入口，侧栏各视图分别只读展示这三个网关的状态。

## 0. 前置：Go 工具链

本机 Go 装在 `~/go-sdk`（**不是** Homebrew，也未装进 `/usr/local`）：

```bash
curl -L -o /tmp/go.tar.gz https://mirrors.aliyun.com/golang/go1.23.10.darwin-amd64.tar.gz
mkdir -p ~/go-sdk && tar -xzf /tmp/go.tar.gz -C ~/go-sdk --strip-components=1
export PATH="$HOME/go-sdk/bin:$PATH"        # 建议写入 ~/.zshrc
go version                                   # go1.23.10 darwin/amd64
```

> 版本按需调整：`workbuddy2api` 的 `go.mod` 要求 1.22.5，`qoderwork2api` 要求 1.23.10。
> 装 1.23.10 可同时满足两者。Intel Mac 用 `darwin-amd64`，Apple Silicon 用 `darwin-arm64`。

## 1. WorkBuddy 网关（wb2api）

```bash
git clone https://github.com/Sliverkiss/workbuddy2api.git ~/workbuddy2api
cd ~/workbuddy2api
cp config.example.json config.json            # 改 api_key；listen 默认 :7863

export PATH="$HOME/go-sdk/bin:$PATH"
export GOPROXY="https://goproxy.cn,direct"
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o wb2api     ./cmd/server
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o credit     ./cmd/credit
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o signin     ./cmd/signin
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o login      ./cmd/login
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o expiry     ./cmd/expiry

mkdir -p auths data
./login                    # OAuth 设备流，落盘 auths/
```

## 2. WorkBuddy 控制台（agent.py）

纯 Python 标准库，用**系统自带** `/usr/bin/python3` 即可，无需 venv：

```bash
cd ~/workbuddy2api
agent.py 随仓库提供，无需构建
```

它负责：账号池展示、签到、积分、登录、MiMo 面板、QoderWork 面板。

## 3. QoderWork 网关（qw2api）

```bash
git clone https://github.com/Sliverkiss/qoderwork2api.git ~/qoderwork2api
cd ~/qoderwork2api
cp config.example.json config.json
# 必须改：listen 改成 127.0.0.1:7865（默认 :7864 与控制台冲突）
# 必须填：api_key

export PATH="$HOME/go-sdk/bin:$PATH"
export GOPROXY="https://goproxy.cn,direct"
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o qw2api     ./cmd/server
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o credit     ./cmd/credit
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o login      ./cmd/login
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o signin_bin ./cmd/signin

mkdir -p auths data
```

### 登录注意（踩过的坑）

`./login url` 生成的授权链接**必须在「已登录 Qoder 的浏览器」里打开**。
未登录时访问该链接会被服务端 `302` 跳回 `/users/sign-in`，等你登录完授权码早已失效，
表现为反复提示「长时间未操作，当前页面已过期」。

正确做法：在**另一台已登录 Qoder 的电脑**上打开链接 → 页面直接是「选择账号」确认页 →
点确认（浏览器提示打不开 `qoder-work-cn://` 属正常）→ 回本机立刻执行：

```bash
./login poll      # 一次性，成功后会删除 /tmp/qw2api-login-state.json
```

拿到 token 后落盘为 `auths/qoderwork-<uid>.json`（可直接用 `./login.sh` 完成全流程）。

## 4. MiMo 反代

```bash
git clone https://github.com/Fly143/xiaomi-mimo-desktop-api.git ~/xiaomi-mimo-desktop-api
cd ~/xiaomi-mimo-desktop-api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # fastapi/uvicorn/httpx/pydantic/cryptography
cp config.example.json config.json            # 填 api_keys / admin_password
```

账号凭证来自 MiMo Desktop 的 `passToken`（在装了 Desktop 的机器上取 cookie），
用管理页或 `/api/desktop/import` 导入。**本机无需安装 MiMo Desktop**——
SSO 换 `serviceToken` 的链路由项目自己实现，UA 也是模拟的。

## 5. LaunchAgent 配置

四个 plist 放在 `~/Library/LaunchAgents/`，改完用：

```bash
launchctl load  ~/Library/LaunchAgents/<name>.plist
launchctl kickstart -k gui/$(id -u)/<Label>    # 重启
```

模板（`com.qoderwork.qw2api.plist` 为例，其余同理，注意 `ProgramArguments` 与 `WorkingDirectory`）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.qoderwork.qw2api</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/<你的用户名>/qoderwork2api/qw2api</string>
    <string>-config</string>
    <string>/Users/<你的用户名>/qoderwork2api/config.json</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/<你的用户名>/qoderwork2api</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/qw2api.log</string>
  <key>StandardErrorPath</key><string>/tmp/qw2api.log</string>
</dict>
</plist>
```

要点：

- **路径必须写绝对路径**（launchd 不读 `~/.zshrc`，`PATH` 与 `PORT` 等环境变量要写进 `EnvironmentVariables`）。
  上面的 `<你的用户名>` 是占位符，**launchd 不会展开它**，必须替换成实际的用户目录名再加载。
- MiMo 反代的 plist 里用 `EnvironmentVariables` 注入 `PORT=7870`（项目默认 8080，与其它服务冲突）。
- 控制台的 `com.workbuddy.wb2api-agent` 用 `/usr/bin/python3` + `agent.py`。

## 6. 环境变量敏感性（跨机迁移必看）

以下**不在 git 里**，换机器必须手动搬：

| 文件 | 内容 |
|---|---|
| `~/workbuddy2api/config.json` | wb2api 的 `api_key` |
| `~/workbuddy2api/auths/*.json` | WorkBuddy OAuth 凭证（含 refresh token） |
| `~/qoderwork2api/config.json` | qw2api 的 `api_key` |
| `~/qoderwork2api/auths/*.json` | Qoder 凭证（`dt-` 30 天 / `drt-` 1 年） |
| `~/qoderwork2api/data/state.json` | 账号池状态与机器指纹 |
| `~/xiaomi-mimo-desktop-api/config.json` | MiMo 账号与 `api_keys`（Fernet 加密） |
| `~/xiaomi-mimo-desktop-api/.secret_key` | 上者的解密密钥，**必须一起拷** |
| `~/xiaomi-mimo-desktop-api/.anthropic_batches/` | Anthropic batches 持久化 |

权限建议 `chmod 600`。凭证搬过去即可直接复用，无需重新授权（token 未过期时）。

## 7. 验证

```bash
# WorkBuddy 网关
curl -s http://127.0.0.1:7863/healthz
# QoderWork
curl -s http://127.0.0.1:7865/healthz
curl -s http://127.0.0.1:7865/status -H "Authorization: Bearer <qw2api api_key>"
cd ~/qoderwork2api && ./credit -json            # 积分（面板用的就是它）
# MiMo
curl -s http://127.0.0.1:7870/api/models
# 控制台
curl -s http://127.0.0.1:7864/health
```

真实对话（各自替换 key / 模型）：

```bash
curl -s http://127.0.0.1:7865/v1/chat/completions \
  -H "Authorization: Bearer <qw2api api_key>" -H "Content-Type: application/json" \
  -d '{"model":"qwen3.8-max","messages":[{"role":"user","content":"hi"}]}'
```

## 8. 已知注意事项

- **端口别撞**：`qoderwork2api` 默认 `:7864` 与控制台冲突，务必改成 7865。
- **模型名以 `/v1/models` 为准**：qoderwork2api 的 README 模型表已过时（例：现为 `qwen3.8-max`，非 `qwen3.8-max-preview`），实际有 14 个。
- **MiMo 的 `/v1/models` 会列出全部模型**，但其中生图（`Doubao-Seedream-5.0-pro`）和语音（`mimo-v2.5-asr/tts*`）不能走对话接口，只有 `mimo-x-flash-preview` / `mimo-x-pro-preview` 可对话。
- **局域网访问**：MiMo（`*:7870`）与 WorkBuddy 网关（`*:7863`）已绑 `0.0.0.0`，同网段可直接用 `http://<本机IP>:<端口>/v1`；控制台与 QoderWork 仅绑回环，需自行改 `listen` 才能外部访问。
- **三套凭证体系互不相通**：WorkBuddy OAuth、MiMo Desktop cookie/SSO、Qoder OAuth 设备流，积分/额度各自独立。
