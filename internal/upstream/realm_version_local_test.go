// 本地回归测试：global 域出站版本号必须与官方国际版分发包一致。
//
// 背景（2026-09-15 实测）：官方两个分发包版本号不同——
//
//	/Applications/WorkBuddy.app        (com.tencent.workbuddy.mac)      = 5.5.4
//	/Applications/WorkBuddy AI.app     (com.workbuddy.workbuddy-ai)    = 5.5.2
//
// 此前 global 域沿用 CN 的 5.5.4，使上游看到「版本比官方国际客户端还新」的
// WorkBuddy AI 流量（WAF 拦截集中在 global 域、CN 域零拦截）。
// 同步上游时保留本文件，防止回退成单版本常量。
package upstream

import (
	"strings"
	"testing"

	"workbuddy2api/internal/auth"
)

func TestLocalGlobalUsesIntlClientVersion(t *testing.T) {
	c := &Client{}
	global := &auth.Auth{Domain: "www.workbuddy.ai"}
	if got := c.defaultWorkBuddyUAFor(global); !strings.HasPrefix(got, "WorkBuddy/5.5.2 ") {
		t.Fatalf("global UA 应使用国际版 5.5.2: %q", got)
	}
	if !strings.Contains(c.defaultWorkBuddyUAFor(global), "WorkBuddy AI/5.5.2") {
		t.Fatalf("global UA 品牌段与版本段应同为国际版: %q", c.defaultWorkBuddyUAFor(global))
	}
	// CN 保持 5.5.4（官方国内分发包），零回归
	if got := c.defaultWorkBuddyUAFor(&auth.Auth{}); !strings.HasPrefix(got, "WorkBuddy/5.5.4 ") {
		t.Fatalf("CN UA 应保持 5.5.4: %q", got)
	}
	// 显式配置优先，且两域同值（用户配了就以用户值为准）
	c2 := &Client{ClientVersion: "9.9.9"}
	if got := c2.defaultWorkBuddyUAFor(global); !strings.Contains(got, "WorkBuddy AI/9.9.9") {
		t.Fatalf("显式版本应覆盖 realm 默认: %q", got)
	}
	if got := c2.defaultWorkBuddyUAFor(&auth.Auth{}); !strings.Contains(got, "WorkBuddy/9.9.9") {
		t.Fatalf("显式版本应覆盖 CN 默认: %q", got)
	}
}

func TestLocalGlobalIDeVersionMatchesUA(t *testing.T) {
	// X-IDE-Version 必须与 UA 版本段一致，否则同一请求里品牌说国际版、
	// 版本却说 CN 号，自相矛盾（可被风控识别）。
	global := &auth.Auth{AccessToken: "at", UID: "g1", Domain: "www.workbuddy.ai"}
	req := mustRequest(t)
	(&Client{}).ChatHeaders(req, global, "", ChatMeta{})
	ua := req.Header.Get("User-Agent")
	ide := req.Header.Get("X-IDE-Version")
	if ide != "5.5.2" {
		t.Fatalf("global X-IDE-Version = %q want 5.5.2", ide)
	}
	if !strings.Contains(ua, "/"+ide) {
		t.Fatalf("UA 与 X-IDE-Version 版本不一致: ua=%q ide=%q", ua, ide)
	}
}
