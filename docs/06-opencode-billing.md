# OpenCode Go 模型分组价格表（Doc 06）

> 事实源：OpenCode Go 官方计费页（2026-09-02 抓取）。
> 单次请求成本按官方「典型使用模式」（输入/缓存/输出 token 量）与每 1M token 价格推算；
> 配额 = 官方标注的每月美元额度；月请求数 = 配额 / 单次成本。
> 用途：ZCode/开发模型选型参考；本项目不依赖 OpenCode 计费（仅存档）。

## 价格常量（$/1M tokens）

DeepSeek V4 系列区分 Peak / Off-Peak：
- **Peak**：周一至周五 01:00-04:00 与 06:00-10:00 UTC
- **Off-Peak**：其余时间（含周末），价格约为 Peak 的 50%

## 四个调用端点分组

ZCode provider 注册了四个 opencode 端点，模型按价格从低到高排列（`~` 为按同系列插值估计，官方未列价）：

### 1. opencode Go（`https://opencode.ai/zen/go/v1`，OpenAI-compatible，33 模型）

| 模型 | 单次成本 | 配额 | 月请求 |
|---|---|---|---|
| muse-spark-1.2-contributor | $0.00026 | $60 | ~226,600 |
| mimo-v2.5 | $0.00040 | $60 | ~150,400 |
| deepseek-v4-flash (Off-Peak) | $0.00079 | $30 | ~37,800 |
| deepseek-v4-flash-vision-exp (Off-Peak) | $0.00079 | $15 | ~18,900 |
| mimo-v2.5-pro | $0.00092 | $15 | ~16,300 |
| longcat-2.0 | $0.00105 | $60 | ~57,200 |
| qwen3.8-flash | $0.00111 | $30 | ~27,000 |
| gpt-5.6-luna | $0.00146 | $15 | ~10,250 |
| glm-5.3-flash | $0.00190 | $15 | ~7,900 |
| mimo-v2-pro | ~$0.0020 | — | — |
| mimo-v2-omni | ~$0.0025 | — | — |
| qwen3.7-plus | $0.00278 | $60 | ~21,550 |
| hy3 | $0.00279 | $60 | ~21,500 |
| deepseek-v4-pro (Off-Peak) | $0.00287 | $15 | ~5,200 |
| qwen3.5-plus | ~$0.0030 | — | — |
| hy3-preview | ~$0.0030 | — | — |
| minimax-m2.7 | $0.00354 | $60 | ~17,000 |
| minimax-m2.5 | $0.00354 | $60 | ~17,000 |
| qwen3.6-plus | $0.00367 | $60 | ~16,300 |
| minimax-m3 | $0.00374 | $60 | ~16,000 |
| hy4-preview | $0.00443 | $30 | ~6,770 |
| kimi-k2.5 | ~$0.0095 | — | — |
| kimi-k2.6 | $0.01043 | $60 | ~5,750 |
| kimi-k2.7-code | $0.01208 | $60 | ~6,750 |
| grok-4.5 | ~$0.014 | — | — |
| glm-5.3 | $0.01516 | $15 | ~1,080 |
| glm-5.2 | $0.01516 | $60 | ~4,300 |
| glm-5.1 | $0.01516 | $60 | ~4,300 |
| glm-5 | ~$0.016 | — | — |
| grok-4.6 | $0.01775 | $15 | ~845 |
| qwen3.8-max | $0.01854 | $15 | ~810 |
| kimi-k3 | $0.03060 | $15 | ~490 |
| qwen3.7-max | $0.03555 | $30 | ~840 |

### 2. opencode Go - Anthropic（`https://opencode.ai/zen/go`，anthropic，8 模型）

qwen3.7-plus ($0.00278) → qwen3.5-plus (~$0.0030) → minimax-m2.7/m2.5 ($0.00354) → qwen3.6-plus ($0.00367) → minimax-m3 ($0.00374) → qwen3.8-max ($0.01854) → qwen3.7-max ($0.03555)

### 3. opencode Go - Responses（`https://opencode.ai/zen/go/v1`，openai，4 模型）

muse-spark-1.2-contributor ($0.00026) → gpt-5.6-luna ($0.00146) → grok-4.5 (~$0.014) → grok-4.6 ($0.01775)

### 4. opencode Free（`https://opencode.ai/zen/v1`，OpenAI-compatible，8 模型）

muse-spark-1.2-contributor-free ($0.00026，估) → mimo-v2.5-free ($0.00040，估) → ling-3.0-flash-fin-free (~$0.0006，估) → deepseek-v4-flash-free ($0.00079，估) → nemotron-3.5-lightning-free (~$0.0011，估) → nemotron-3-ultra-free (~$0.0015，估) → laguna-s-2.1-free (~$0.002，估) → big-pickle (~$0.003，估)

> Free 端点按 IP 限额；2026-09-02 实测：nemotron 系列与 ling 可用，
> `deepseek-v4-flash-free` 上游报 Model unavailable（与 IP 无关）。

## 选型建议

- **大量任务（批处理/抓取）** → Muse Spark、MiMo-V2.5（$60 额度 + 15 万次/月）
- **中文高质量 / 通用智能** → DeepSeek V4 Flash（Off-Peak 0.08 美分/次）——首选平衡点
- **Agent 编码** → Hy3、Qwen3.8 Flash
- **最强能力** → Grok 4.6 / Qwen3.7 Max（贵约 130 倍，按需使用）
- DeepSeek 系列避开 Peak 时段可省一半成本