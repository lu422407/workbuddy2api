// resource_expiry.go — 按套餐的积分到期明细（运维到期监控）。
//
// 与 ResourceSummary 同一请求逐字复用 billingMeterJSON（全套 CodeBuddy 请求头/
// realm 路由/错误语义），仅解析层保留 PackageEndTime —— 上游字段名与请求过滤器
// PackageEndTimeRange* 同源。独立成文件（不改 client.go），降低上游 rebase 冲突面。
package upstream

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"workbuddy2api/internal/auth"
)

// ResourcePackage 单个积分套餐：remain 沿用 packageRemainUsed 的聚合口径，
// End 归一化为 RFC3339（无法解析 → 空串，不猜测）。
type ResourcePackage struct {
	PackageName string `json:"name"`
	Remain      int64  `json:"remain"`
	Size        int64  `json:"size"`
	End         string `json:"end"`
}

// ResourcePackages 查询账号全部套餐明细（含到期时间）。只读，不签到、不刷 token。
//
// 到期字段实测（2026-09-14）：响应无 PackageEndTime（那是请求过滤器键名），实际形态：
//   - CycleEndTime："2026-09-30 23:59:59" 本地串，Cycle 期套餐（可花的就是它）的失效时刻 → 首选
//   - DeductionEndTime：epoch 毫秒浮点（科学计数法）→ 回落
//   - EndTime：多为 null；有值时同样参与回落
func (c *Client) ResourcePackages(a *auth.Auth) ([]ResourcePackage, error) {
	now := time.Now()
	body := map[string]any{
		"PageNumber":               1,
		"PageSize":                 100,
		"ProductCode":              "p_tcaca",
		"Status":                   []int{0, 3},
		"PackageEndTimeRangeBegin": now.Format("2006-01-02 15:04:05"),
		"PackageEndTimeRangeEnd":   now.Add(365 * 101 * 24 * time.Hour).Format("2006-01-02 15:04:05"),
	}
	data, err := c.billingMeterJSON(a, c.billingMeterPaths(a), http.MethodPost, body)
	if err != nil {
		return nil, err
	}
	var resp struct {
		Response struct {
			Data struct {
				Accounts []struct {
					PackageName         string          `json:"PackageName"`
					EndTime             json.RawMessage `json:"EndTime"`
					CycleEndTime        string          `json:"CycleEndTime"`
					DeductionEndTime    json.RawMessage `json:"DeductionEndTime"`
					CapacitySize        int64           `json:"CapacitySize"`
					CapacityRemain      int64           `json:"CapacityRemain"`
					CapacityUsed        int64           `json:"CapacityUsed"`
					CycleCapacitySize   int64           `json:"CycleCapacitySize"`
					CycleCapacityRemain int64           `json:"CycleCapacityRemain"`
					CycleCapacityUsed   int64           `json:"CycleCapacityUsed"`
				} `json:"Accounts"`
			} `json:"Data"`
		} `json:"Response"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return nil, fmt.Errorf("resource parse: %w", err)
	}
	out := make([]ResourcePackage, 0, len(resp.Response.Data.Accounts))
	for _, acct := range resp.Response.Data.Accounts {
		r, _, s := packageRemainUsed(respAccount{
			CapacityRemain:      acct.CapacityRemain,
			CapacityUsed:        acct.CapacityUsed,
			CapacitySize:        acct.CapacitySize,
			CycleCapacityRemain: acct.CycleCapacityRemain,
			CycleCapacityUsed:   acct.CycleCapacityUsed,
			CycleCapacitySize:   acct.CycleCapacitySize,
		})
		end := pickPackageEnd(acct.CycleEndTime, acct.DeductionEndTime, acct.EndTime)
		out = append(out, ResourcePackage{
			PackageName: acct.PackageName,
			Remain:      r,
			Size:        s,
			End:         end,
		})
	}
	return out, nil
}

// cstZone 服务器时区（Asia/Shanghai +08:00，实测 2026-09-14：CycleEndTime 串与
// DeductionEndTime epoch 毫秒互验吻合）。运行机器可能不在 +08（本机即设了美西），
// 必须固定区解析，否则临期倒计时错 15 小时。
var cstZone = time.FixedZone("CST", 8*3600)

// pickPackageEnd 到期取值优先级：CycleEndTime（周期失效，Cycle 期套餐真正可花的窗口）
// → DeductionEndTime（epoch 毫秒）→ EndTime。全部解析失败 → 空串。
func pickPackageEnd(cycleEnd string, deductEnd, endRaw json.RawMessage) string {
	if t, err := time.ParseInLocation("2006-01-02 15:04:05", strings.TrimSpace(cycleEnd), cstZone); err == nil {
		return t.Format(time.RFC3339)
	}
	if s := normalizeEndRaw(deductEnd); s != "" {
		return s
	}
	return normalizeEndRaw(endRaw)
}

// normalizeEndRaw 接受 "YYYY-MM-DD HH:MM:SS"（按 CST 固定区）、RFC3339、
// epoch 秒/毫秒（数字、数字串或科学计数法浮点如 1.791780701e+12）。
// 统一输出 RFC3339（+08:00 偏移）；全部失败返回空串（不猜测）。
func normalizeEndRaw(raw json.RawMessage) string {
	s := strings.Trim(strings.TrimSpace(string(raw)), `"`)
	if s == "" || s == "null" {
		return ""
	}
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t.In(cstZone).Format(time.RFC3339)
	}
	if t, err := time.ParseInLocation("2006-01-02 15:04:05", s, cstZone); err == nil {
		return t.Format(time.RFC3339)
	}
	if f, err := strconv.ParseFloat(s, 64); err == nil && f > 1e9 {
		n := int64(f)
		if n > 1e12 {
			n /= 1000 // 毫秒形态
		}
		return time.Unix(n, 0).In(cstZone).Format(time.RFC3339)
	}
	return ""
}
