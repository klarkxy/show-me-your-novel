# TODO：小说开篇 LLM 主编盲审评分系统

> 状态：**待执行**（用户决定暂不实施，等以后再做）
> 来源：plan 文件 [llm-ai-ai-humanizer-zh-merry-flamingo.md](../plans/llm-ai-ai-humanizer-zh-merry-flamingo.md)

## 目标

建一套**主编盲审**系统，对每部小说的**开篇**用强推理 LLM 当"严厉主编"打分。

要点：
- **盲审**：主编看不到/不假设"这是 AI 写的"
- **从严**：模拟真实杂志主编 90% 退稿率的严苛口吻
- **AI 写作痕迹**是重要的一维评分依据，参考 `humanizer-zh` 的 16 条模式
- 复用现有 `runner/generate.py` 的 API 调用层与 `config.yaml` 模型表，不引入新依赖
- 评分仅在**详情页**呈现，**首页不动**

## 用户已确认的决策

| 决策 | 取值 |
|---|---|
| 主编模型选型 | 新增 `config.yaml` 的 `judges:` 段；默认 3 个：`deepseek-v4-pro` + `minimax-m3` + `qwen3.7-max`；`--judge` 可覆盖 |
| 分数粒度 | 6 维 × 0–10 整数，总分 /60 → 字母 A–F |
| 开篇范围 | **前 6000 中文字符**（≈一章完整） |
| 站点呈现 | **只详情页加评分卡**；首页卡片不动 |
| 元信息泄露 | **进 API 前剔除干净**：`[思考过程]…[/思考过程]` 块、顶部 `## 大纲` 段、`字数：N` 等元信息 |
| 聚合 | 只看总分 / 字母等级，不引入"拒签"门 |

## 6 维评分量表

| # | 维度 | 满分 |
|---|---|---|
| 1 | 开篇吸引力 (hook) | 10 |
| 2 | 人物与对话 | 10 |
| 3 | 场景与节奏 | 10 |
| 4 | 文笔与表达 | 10 |
| 5 | AI 写作痕迹 | 10 |
| 6 | 类型契合与设定兑现 | 10 |

**字母等级映射（脚本计算）**：
- A: ≥51/60 (≥85%)
- B: 42–50 (70–84%)
- C: 33–41 (55–69%)
- D: 24–32 (40–54%)
- F: <24 (<40%)

**打分基准（写进 system prompt）**：
- 6 分 = 杂志可用基线
- 7–8 = 真正不错
- 9+ = 罕见
- 5 = 读完觉得"还行"但不会签
- 3–4 = 退稿
- 0–2 = 不可读

## AI 写作痕迹（第 5 维）检查清单

照搬 humanizer-zh 的 16 条，每条命中扣 1–2 分（封顶 0）：

1. 直述情绪（"他很+情绪""心中涌起"）
2. 概述式叙事（"两人聊了很久""时间过得很快"）
3. 过滤词屏障（"他听到/看到/意识到/发现" 连续 3+）
4. 贴标签式人物（"他是一个…的人"）
5. 记叙文式过渡（"然后/接着/后来/于是/之后/随后" 连续）
6. 旁白式解释（"他之所以…是因为…""事实上""其实"）
7. 强行认同（"虽然…但是…/然而" 单段 >3 或单章 >5）
8. 短废话（"他开口了/他停住了/他转身走了" 动作前冗余）
9. AI 词汇（此外/至关重要/培养/获得/彰显/谱写/铸就/奠定/弘扬/承载/象征着/体现了/见证了）
10. 三段式法则（并列同类刻意凑三项）
11. 否定式排比（"不仅 X 而且 Y"）
12. 模糊归因（"据说/相传/有研究表明"无具体来源）
13. 同义词循环（同段对同一角色多次换名）
14. 肤浅总结（段尾以"象征着/见证了/标志着"收束）
15. 单字定性收束（段末用 平/静/凉/慢/轻/淡/脆/暗/粗/细 作结）
16. 情感扁平化（情感戏只用程式化身体反应，缺感官跳跃/认知中断/错位反应/时间扭曲）

## 待实施步骤

### 1. 改 [config.yaml](config.yaml)

在 `models:` 段后新增 `judges:` 段（与 `models:` 平级）：

```yaml
judges:
  - id: deepseek-v4-pro
    name: DeepSeek V4 Pro
    model: deepseek-v4-pro
    thinking: true
    provider: opencode-go

  - id: minimax-m3
    name: MiniMax M3
    model: minimax-m3
    thinking: adaptive
    api_format: anthropic
    provider: opencode-go

  - id: qwen3.7-max
    name: Qwen3.7 Max
    model: qwen3.7-max
    api_format: anthropic
    provider: opencode-go
```

### 2. 新建 [runner/prompts/judge_system.md](runner/prompts/judge_system.md)

完整 system prompt 文本，**字符级固定**。关键约束：
- 主编人设：退稿率 90%+ 的严苛中文文学杂志主编
- **盲审原则**：严禁在点评中出现"AI""模型""生成""机器""提示词"等暗示来源的字眼
- "开篇"范围 ≈6000 字
- 6 维评分（见上表）
- 16 条 AI 写作痕迹检查清单
- 默认 6 分起步，能拿到 7+ 才是真正有水准
- 输出严格 JSON（scores 6 项 + total + ai_trace_hits + highlights + issues + overall_verdict）

User 模板（脚本动态拼）：
```
请审阅以下投稿开篇（开篇正文从"=== 开篇开始 ==="到"=== 开篇结束 ==="之间）：

=== 开篇开始 ===
<opening text, 元信息已剔除>
=== 开篇结束 ===

请按 system 中的维度打分，并按指定 JSON 格式输出。
```

> 完整 prompt 文本见 plan 文件第 §"主编 System Prompt" 段。

### 3. 新建 [runner/score.py](runner/score.py) (~360 行)

**复用** `runner/generate.py` 的：
- [api_chat_completion()](runner/generate.py#L429) — HTTP 调用
- [load_config()](runner/generate.py#L103)、[get_model_config()](runner/generate.py#L111)、[get_provider_config()](runner/generate.py#L118)
- [load_env_file()](runner/generate.py#L88)、[find_repo_root()](runner/generate.py#L83)
- [count_chinese_chars()](runner/generate.py#L128)
- JSON 容错模板：[parse_outline()](runner/generate.py#L493)

通过 `sys.path.insert(0, …); import generate as gen` 复用，**不复制**。

**核心流程**：
1. `argparse` 解析：`--story / --model / --judge / --reset / --opening-chars (default 6000) / --judge-temperature (default 0.3) / --judge-max-tokens (default 4096) / --dry-run`
2. 加载 `config.yaml` 的 `models:` + `judges:` + `providers:`
3. 定位 `novels/<story>/<model>.md`
4. **开篇抽取 + 元信息剔除**（关键预处理）：
   - 删除 `[思考过程]…[/思考过程]` 整段（`re.sub` 非贪婪 + DOTALL）
   - 删除文件顶部 `## 大纲` 到下一个 `## 第1章` 之间的所有内容
   - 截到 `## 第2章` 出现为止；若无，取**前 6000 中文字符**（用 `count_chinese_chars` 边走边算）
   - 删除每章头部的 `字数：N` 这类元信息行
5. 对每个 (目标 model, 主编) 对：
   - 检查 `work/<story>/scores/<model>__<judge>.json` 是否存在且 mtime ≥ 源文件 → `[skip]`
   - 调 `gen.api_chat_completion(... temperature=0.3, max_tokens=4096, thinking=<judge.thinking>, response_format={"type":"json_object"} if openai else None)`
   - 解析：参考 `parse_outline` 的 fence 提取 + `json.loads`；失败 raw 写 `<file>.raw.txt` 并 warn 跳过
   - **校验**：`scores` 6 项都是 0–10 整数、`ai_trace_hits` 是 list、issue/verdict 是字符串
   - **脚本内重新算** `total = sum(scores.values())` 与字母等级，不信主编自己写的 `total`
   - 写 `<model>__<judge>.json`（含 `raw_response` 全文以便调试）
6. **聚合**：对每个目标 model，读所有主编 JSON，写 `<model>__aggregate.json`：
   - `averages`: 6 维均值
   - `average_total`: 主编 total 均值
   - `median_total`: 主编 total 中位数
   - `min_total / max_total`: 主编 total 区间
   - `judge_agreement`: `stdev(totals)` — ≤2 高一致(绿) / 2–4 有分歧(黄) / >4 争议大(红)
   - `final_grade`: 从 `median_total` 映射 A–F
   - `final_score_pct`: `median_total / 60 * 100`
   - `intersect_highlights`: 2+ 主编都提到的优点
   - `union_issues`: 所有 issue 去重（"前 20 字 normalize" 为 key）
7. 写 `work/<story>/scores/index.json`（全表，详情页用）
8. stdout 打印每个目标 model 一行：`model | D1 D2 D3 D4 D5 D6 | avg/median | 等级`

**幂等**：默认 skip 已评分的；`--reset` 删 `work/<story>/scores/`；`--model` / `--judge` 限缩范围。

### 4. 改 [scripts/generate_site.py](scripts/generate_site.py) (+~80 行)

**A. 数据加载**（详情页循环顶部，约 line 285）：
```python
scores_dir = repo_root / "work" / story / "scores"
agg = {}
if scores_dir.exists():
    for m in models:
        p = scores_dir / f"{m}__aggregate.json"
        if p.exists():
            agg[m] = json.loads(p.read_text(encoding="utf-8"))
```

**B. 详情页 评分卡**（[generate_site.py:431-463](scripts/generate_site.py#L431-L463) `<article class="novel">` 内部，`novel-header` 与 `novel-body` 之间插入）：
- 大字 `median_total / 60 · 等级`（带 A–F 颜色 class）
- 元信息：`平均 X · 区间 Y–Z · 一致性高/中/低 · 主编集合 N 人`
- 6 维表格：列 = 6 个主编 + 平均；行 = 6 维；**AI 写作痕迹行加红高亮 class**（视觉钩子）
- `<details>` 折叠 N 个主编各自的具体点评（highlights 绿、issues 红、overall_verdict 灰斜体）

**C. 首页卡片** 不动（按用户要求）。

### 5. 改 [docs/assets/style.css](docs/assets/style.css) (+~40 行)

新增段（见 plan 文件 §"docs/assets/style.css 新增段"）：
- `.score-panel / .score-summary / .score-big-num / .score-big-grade`
- 字母等级配色：`[data-grade="A"] { --g: #2d6a4f; }` 等
- `.score-grid` + `tr.ai-row` 红色高亮
- `.judge-critiques` 折叠点评
- 一致性：`.agree-high / .agree-mid / .agree-low`

> 颜色变量（`--green / --red / --yellow / --bg-soft / --border / --radius / --text / --text-dim`）已在现有 `style.css` 顶部定义。

### 6. 改 [.gitignore](.gitignore)

加 `work/`（当前只有 25 字节，几乎为空）。

### 7. 验证（端到端）

按 plan §验证 的 9 步走：
1. `--dry-run` 校验路径与配置
2. 单点 `--model deepseek-v4-pro --judge minimax-m3` 验证 JSON 结构、盲审检查（不含"AI/模型"字眼）
3. 全书 `--reset` 跑 3 主编 × 9 模型 = 27 次调用
4. 幂等性（再跑一遍应全 skip）
5. `touch` 源文件后自动重评
6. `generate_site.py` 渲染后详情页有 6 维评分卡，**首页无变化**
7. 聚合合理性：judge_agreement 颜色显示
8. 错误恢复：错的 `--judge` 报错并 exit 1
9. 全库 `python3 runner/score.py` 一次性评所有小说

### 8. 可选：改 [README.md](README.md)

加 "主编评分" 章节（~12 行），说明如何跑 `runner/score.py`、分数含义。

## 关键文件速查

| 用途 | 路径 |
|---|---|
| 主编 LLM 调用 | [runner/generate.py:429](runner/generate.py#L429) `api_chat_completion` |
| 配置加载 | [runner/generate.py:103](runner/generate.py#L103) `load_config` |
| 路径解析 | [runner/generate.py:83](runner/generate.py#L83) `find_repo_root` |
| 中文字符计数 | [runner/generate.py:128](runner/generate.py#L128) `count_chinese_chars` |
| JSON 容错解析模板 | [runner/generate.py:493](runner/generate.py#L493) `parse_outline` |
| 评分 prompt 全文 | plan 文件 §"主编 System Prompt"（待复制到 `runner/prompts/judge_system.md`） |
| 16 模式检测依据 | `C:\Users\27837\.claude\skills\humanizer-zh\SKILL.md` 第 16–200 行 |

## 风险与备选

- **主编偏松**（所有稿件都得 7+）：把 system prompt 里"默认 6 分起步"提为最末段；并要求 `ai_traces` 一项出现模式 ⑤ ⑨ ⑭ 任意 3+ 次直接 ≤ 4
- **JSON 解析失败率高**：`parse_score_response` 复制 `parse_outline` 的 fence 容错；raw 全文落盘以便回放
- **Anthropic 不支持 `response_format`**：只在 `api_format == "openai"` 时传，与 [generate.py:533](runner/generate.py#L533) 一致
- **work/ 体积膨胀**：把 `work/` 加进 `.gitignore`
- **温度统一**：评分默认 `temperature=0.3`；Kimi 那种 `temperature: 1.0` 不放进 `judges:` 段
