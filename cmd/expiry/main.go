// expiry — 按套餐的积分到期明细（全部账号），JSON 输出到 stdout，供本机管理助手/
// 控制台做临期监控。与 cmd/credit 的区别：本工具不下钻聚合 used/size，专攻
// 每套餐的到期时间（最近到期 + 明细列表）。
//
// 只读原则：不签到、不刷 token —— accessToken 已过期时直接报 token_expired 跳过
// （避免与管理服务抢刷新写回，令牌的续期归服务端 22:00 保活与 login 流程管）。
//
// 用法:
//
//	go run ./cmd/expiry        # 或编译后 ./expiry
//	输出: {"service":"workbuddy-expiry","ts":N,
//	      "accounts":[{"uid","nickname","realm","ok","error?","next_expire",
//	                   "packages":[{"name","remain","size","end"}]}]}
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/upstream"
)

type packageItem struct {
	Name   string `json:"name"`
	Remain int64  `json:"remain"`
	Size   int64  `json:"size"`
	End    string `json:"end"`
}

type accountResult struct {
	UID        string        `json:"uid"`
	Nickname   string        `json:"nickname"`
	Realm      string        `json:"realm"`
	OK         bool          `json:"ok"`
	Error      string        `json:"error,omitempty"`
	NextExpire string        `json:"next_expire,omitempty"`
	Packages   []packageItem `json:"packages,omitempty"`
}

func main() {
	authDir := "./auths"
	if v := os.Getenv("WB2A_AUTH_DIR"); v != "" {
		authDir = v
	}
	up := upstream.New()
	up.GlobalEnabled = true // 与 cmd/credit 同口径：按 realm 路由 billing 域
	accounts := collect(authDir, up)
	out := map[string]any{
		"service":  "workbuddy-expiry",
		"ts":       time.Now().Unix(),
		"accounts": accounts,
	}
	raw, _ := json.Marshal(out)
	fmt.Println(string(raw))
}

func collect(authDir string, up *upstream.Client) []accountResult {
	files, _ := filepath.Glob(filepath.Join(authDir, "workbuddy-*.json"))
	sort.Strings(files)
	accounts := make([]accountResult, 0, len(files))
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		a, err := auth.Parse(raw)
		if err != nil {
			continue
		}
		res := accountResult{UID: a.UID, Nickname: a.Nickname, Realm: a.Realm()}
		switch {
		case a.AccessToken == "":
			res.Error = "no accessToken"
		case time.Now().Unix() >= a.ExpiresAt:
			res.Error = "token_expired"
		default:
			packs, err := up.ResourcePackages(a)
			if err != nil {
				res.Error = err.Error()
			} else {
				res.OK = true
				for _, p := range packs {
					res.Packages = append(res.Packages, packageItem{
						Name: p.PackageName, Remain: p.Remain, Size: p.Size, End: p.End,
					})
					// RFC3339 字典序即时间序；无到期（永久/解析失败）不参与“最近”竞争。
					if p.End != "" && (res.NextExpire == "" || p.End < res.NextExpire) {
						res.NextExpire = p.End
					}
				}
			}
		}
		accounts = append(accounts, res)
		time.Sleep(200 * time.Millisecond) // 与 cmd/credit 同节奏，别连发风控
	}
	return accounts
}
