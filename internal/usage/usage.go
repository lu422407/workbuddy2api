// Package usage 把每次成功请求的用量（token 与扣费）追加到本地 JSONL，
// 供宿主机管理助手按天/模型/账号聚合出趋势图。
//
// 为什么独立成模块而不是改 handler/logging：
// 上游 sync 时这两处是热点（本仓库已有多轮 rebase），把落盘逻辑收在一个新文件里，
// 冲突面最小；handler 侧只加一行 Record() 调用。
//
// 落盘位置由 Config.UsageFile 决定（空 = 关闭采集，零开销、不创建文件）。
// 写入失败一律忽略（观测数据不得影响请求主链路）。
package usage

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Entry 一次成功请求的用量记录（字段刻意精简：够画趋势图即可，不含任何提示词内容）。
type Entry struct {
	TS         int64   `json:"ts"`               // Unix 秒（完成时刻）
	Model      string  `json:"model"`            // 出站裸模型名
	Realm      string  `json:"realm"`            // cn / global
	UID        string  `json:"uid"`              // 账号，用于按账号聚合
	Prompt     int     `json:"prompt,omitempty"` // usage.prompt_tokens
	Completion int     `json:"completion,omitempty"`
	Total      int     `json:"total,omitempty"`
	Credit     float64 `json:"credit,omitempty"` // usage.credit（真实扣费）
	Stream     bool    `json:"stream,omitempty"`
	DurationMS int64   `json:"dur_ms,omitempty"`
}

// Recorder 串行化 JSONL 追加（多请求并发下保证行不交错）。
type Recorder struct {
	mu   sync.Mutex
	path string
	f    *os.File
}

var (
	globalMu sync.RWMutex
	global   *Recorder
)

// Configure 设置全局落盘路径（空串 = 关闭采集）。启动时调用一次。
func Configure(path string) {
	globalMu.Lock()
	defer globalMu.Unlock()
	if global != nil {
		global.Close()
		global = nil
	}
	if path == "" {
		return
	}
	global = &Recorder{path: path}
}

// Record 记录一次请求用量；未配置或写入失败时静默返回。
func Record(e Entry) {
	globalMu.RLock()
	r := global
	globalMu.RUnlock()
	if r == nil {
		return
	}
	if e.TS == 0 {
		e.TS = time.Now().Unix()
	}
	r.append(e)
}

func (r *Recorder) append(e Entry) {
	line, err := json.Marshal(e)
	if err != nil {
		return
	}
	line = append(line, '\n')

	r.mu.Lock()
	defer r.mu.Unlock()
	if r.f == nil {
		if err := os.MkdirAll(filepath.Dir(r.path), 0o755); err != nil {
			return
		}
		f, err := os.OpenFile(r.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
		if err != nil {
			return
		}
		r.f = f
	}
	_, _ = r.f.Write(line) // 观测数据：写失败不重试、不影响请求
}

// Close 关闭当前文件句柄（配置变更/进程退出时调用）。
func (r *Recorder) Close() {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.f != nil {
		_ = r.f.Close()
		r.f = nil
	}
}
