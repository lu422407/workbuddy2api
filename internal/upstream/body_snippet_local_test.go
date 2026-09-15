// bodySnippet 的本地回归测试（无网络）：
// WAF 拦截页是 HTML，正文（拦截原因）在文档中后段，前 200 字符只有样板。
// 按字节硬截断会让日志只剩 "WAF Block Page"，无法回答"为什么被拦"。
package upstream

import (
	"strings"
	"testing"
)

func TestLocalBodySnippetHTMLKeepsVisibleText(t *testing.T) {
	page := `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8" />
<meta name="requestId" content="req-abc-123" />
<style>body{color:#fff}</style></head><body>
<h1>WAF Block Page</h1>
<p>Your request has been blocked by the security policy.</p>
<p>Rule: rate-limit-4290 · Action: block</p>
<script>var x=1;</script>
</body></html>`
	got := bodySnippet(page, 600)

	// 原因文案必须保留（原实现会被前 200 字节的样板挤掉）
	for _, want := range []string{"blocked by the security policy", "rate-limit-4290"} {
		if !strings.Contains(got, want) {
			t.Fatalf("缺少关键信息 %q: %s", want, got)
		}
	}
	// meta 里的排查标识应保留
	if !strings.Contains(got, "requestId=req-abc-123") {
		t.Fatalf("未保留 meta 标识: %s", got)
	}
	// 不应残留标签/脚本
	for _, bad := range []string{"<script", "<style", "<p>", "var x=1"} {
		if strings.Contains(got, bad) {
			t.Fatalf("残留噪声 %q: %s", bad, got)
		}
	}
}

func TestLocalBodySnippetJSONUnchanged(t *testing.T) {
	// 非 HTML 走原 truncate 口径，行为不变（上游既有测试依赖）
	j := `{"code":11102,"msg":"model [x] service info not found"}`
	if got := bodySnippet(j, 600); got != j {
		t.Fatalf("JSON 不应被改写: %q", got)
	}
	if got := bodySnippet(strings.Repeat("a", 700), 200); len(got) != 200 {
		t.Fatalf("超长非 HTML 应按 n 截断: len=%d", len(got))
	}
}
