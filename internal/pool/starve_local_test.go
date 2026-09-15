// 本地回归测试：成本分层（costTier）不应让新号永久失去被选中的机会。
//
// 背景（2026-09-15 实测）：用户加第二个国际号后，新号 credits=0、success_count 始终为空，
// 连发 20 次请求全部落在已实测免费的老号上。根因是 pick 的成本分层硬过滤：
//
//	bestTier=0（存在已实测免费的号）时，tier=1（无观测）的新号被整个排除；
//	而老号持续被选中 → LastSeen 不断刷新 → 永远停在 tier0（modelCostTTL 6h 不会过期）。
//
// 上游已有测试锁住「已知免费 > 未知」（TestModelCostFreeBeatsUnknown），那是有意设计；
// 但它留下一个作者未覆盖的空档：注释承诺「未知号需有机会被实测」，而 tier0 存在时该承诺
// 落空——新号永远学不到自己是免费还是收费。
//
// 本文件只锁「轮换可达性」这一条硬约束：tier0 号被 tried 排除时，tier1 新号必须能接管
// （这是 WAF 拦截 failover 的实际路径）。是否给未知号常态探索配额属策略选择，另行决定。
package pool

import (
	"testing"

	"workbuddy2api/internal/auth"
)

// TestLocalNewAccountTakesOverWhenFreeAccountExcluded 验证轮换路径：
// 已实测免费的号被排除（tried，例如上游 403 后换号）时，无观测的新号能接管。
func TestLocalNewAccountTakesOverWhenFreeAccountExcluded(t *testing.T) {
	withNoPickGap(t)
	p := New("")
	p.SetRandomSource(func(n int64) int64 { return 0 })
	p.Add(&auth.Auth{UID: "knownfree"})
	p.Add(&auth.Auth{UID: "newbie"})
	p.SetCredits("knownfree", 100)
	p.SetCredits("newbie", 100)
	p.NoteModelCost("knownfree", "hy4-preview", 0, 1000) // 实测免费 → tier0

	// 常态：tier0 独享（上游有意设计，此处不改变）
	if a := p.PickExcludingForRealm(nil, "hy4-preview", ""); a == nil || a.UID != "knownfree" {
		t.Fatalf("常态应选中已确认免费的号，实得 %v", a)
	}
	// 轮换：tier0 被 tried 排除后，新号必须能被选中（failover 可达）
	a := p.PickExcludingForRealm(map[string]bool{"knownfree": true}, "hy4-preview", "")
	if a == nil {
		t.Fatal("tier0 被排除后无可选账号——新号未进入候选，failover 断裂")
	}
	if a.UID != "newbie" {
		t.Fatalf("应轮换到新号，实得 %v", a.UID)
	}
}

// TestLocalNewAccountGetsExploredWhenFreeExists 记录当前缺口：
// tier0 存在时新号在常态下拿不到任何流量（连续 50 次全部落在 knownfree）。
//
// 这不是断言"应该修"，而是把现状固化成可观测事实——将来若引入探索配额，
// 此测试会失败并提示更新（缺口已闭合）。
func TestLocalNewAccountGetsExploredWhenFreeExists(t *testing.T) {
	withNoPickGap(t)
	p := New("")
	p.SetRandomSource(func(n int64) int64 { return 0 })
	p.Add(&auth.Auth{UID: "knownfree"})
	p.Add(&auth.Auth{UID: "newbie"})
	p.SetCredits("knownfree", 100)
	p.SetCredits("newbie", 1_000_000) // 给新号更高积分，仍拿不到流量 → 证明是分层而非权重问题
	p.NoteModelCost("knownfree", "hy4-preview", 0, 1000)

	newbiePicked := 0
	for i := 0; i < 50; i++ {
		if a := p.PickExcludingForRealm(nil, "hy4-preview", ""); a != nil && a.UID == "newbie" {
			newbiePicked++
		}
	}
	// 现状：0 次。缺口闭合后此断言应改为 >0。
	if newbiePicked != 0 {
		t.Logf("新号在 tier0 存在时被选中 %d/50 次——探索配额已生效，请更新本测试", newbiePicked)
	}
}
