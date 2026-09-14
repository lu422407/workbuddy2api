// 到期解析的本地回归测试（无网络，纯函数）：锁死实测响应形态，
// 上游若改字段/格式这些用例会立刻失败提示重新对账。2026-09-14 实测：
// CycleEndTime 为 "YYYY-MM-DD HH:MM:SS" 本地串，DeductionEndTime 为 epoch
// 毫秒浮点（JSON 里呈科学计数法），EndTime 多为 null。
package upstream

import (
	"encoding/json"
	"testing"
	"time"
)

func TestLocalPickPackageEnd(t *testing.T) {
	cst := time.FixedZone("CST", 8*3600)
	// CycleEndTime 优先（服务器北京时间，机器时区无关）
	end := pickPackageEnd("2026-09-30 23:59:59", json.RawMessage("1.791780701e+12"), json.RawMessage("null"))
	want := time.Date(2026, 9, 30, 23, 59, 59, 0, cst).Format(time.RFC3339)
	if end != want {
		t.Fatalf("CycleEndTime 优先失败: got=%q want=%q", end, want)
	}
	// CycleEndTime 缺失 → DeductionEndTime（毫秒浮点，归一化输出恒为 +08 形态）
	end = pickPackageEnd("", json.RawMessage("1.791780701e+12"), json.RawMessage("null"))
	want = time.Unix(1791780701, 0).In(cst).Format(time.RFC3339)
	if end != want {
		t.Fatalf("DeductionEndTime 回落失败: got=%q want=%q", end, want)
	}
	// 全缺失 → 空串（永久额度，不猜测）
	end = pickPackageEnd("", json.RawMessage("null"), json.RawMessage("null"))
	if end != "" {
		t.Fatalf("全空应返回空串: got=%q", end)
	}
}

func TestLocalNormalizeEndRaw(t *testing.T) {
	cases := map[string]bool{
		`"2026-10-12 12:51:41"`: true,  // 本地串
		`"null"`:                 false, // 信封缺失
		`""`:                     false, // 空
	}
	for raw, wantNonEmpty := range cases {
		got := normalizeEndRaw(json.RawMessage(raw))
		if (got != "") != wantNonEmpty {
			t.Fatalf("normalizeEndRaw(%s): got=%q, wantNonEmpty=%v", raw, got, wantNonEmpty)
		}
	}
}
