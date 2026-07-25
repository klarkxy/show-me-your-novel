# show-me-your-novel

一个中文长篇小说模型评测项目。当前生成协议是 **自主长篇评测 V2.1**：19 个模型围绕同一方向独立完成规划与约 5 万字正文，再由五位固定评委按评分 V3 的八个维度盲评。

在线站点：[https://klarkxy.github.io/show-me-your-novel/](https://klarkxy.github.io/show-me-your-novel/)

## 评测协议

统一创作方向：

> 改革开放初期的中国现实主义长篇。

每个模型维持一条可重放的权威消息链：

```text
书名与简介 → 约 200 万字全书大纲 → 前约 5 万字细纲 → 16–18 章正文
```

关键约束：

- 书名、简介、人物和结局方向由模型自行决定。
- 全书大纲规划 10–20 卷、总规模 180–220 万字；本轮只生成开篇部分。
- 章节提示词只建议正文字数约 3,000–4,000 字，不把它做成 API 限制。
- 结构合格的首稿少于 3,000 个可计字符时，只追加一次隔离扩写请求；最终采用两稿中较长的结构有效稿。
- 最终纯正文只设置 48,000 个可计字符的最低完成线，**章节和整书都不设置字数上限**。
- JSON 或章节结构不合格时，候选稿留在私有审计目录；只有最终接受的完整稿进入权威消息链。
- 不摘要、不截断、不把 reasoning 回填给模型，也不人工修改作品。
- 生成请求的可选参数保持为空，不主动设置 `temperature`、思考、`top_p` 或 `response_format`。默认 O 口只发送 `model` 与 `messages`；原生 A 口除此之外只发送协议必填的高 `max_tokens`：MiniMax M3 为 `204800`，三种 Claude 生成模型为 `65536`。这些值是协议层输出容量，不是章节字数限制；章节和整书仍无字数上限。
- 中断后从最后一个接受阶段继续；已原子落盘的相同请求响应可以零调用恢复。

当前首部完整基线是 MiMo V2.5，`run_id=6767704f6322`：17 章、61,495 个可计字符，结果位于 `results/reform-era/mimo-v2.5/`，已经通过深校验和离线建站。

首轮生成模型：

```text
deepseek-v4-flash   deepseek-v4-pro      mimo-v2.5
mimo-v2.5-pro       minimax-m3           glm-5.2
gpt-5.6-luna        gpt-5.6-sol          gpt-5.6-terra
claude-haiku-4-5    claude-sonnet-5      gemini-3.1-pro
gemini-3.5-flash    gemini-3.6-flash     kimi-k2.7-code
kimi-k3             grok-4.5             claude-opus-4-8
agnes-2.0-flash
```

## 安装与配置

需要 Python 3.11+：

```bash
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

在仓库根目录创建不会被 Git 跟踪的 `.env`：

```dotenv
API_URL=https://your-api.example.com/v1
API_KEY=sk-...
```

活动流程默认调用 OpenAI-compatible O 口 `/v1/chat/completions`；MiniMax M3，以及 Claude Haiku 4.5、Claude Sonnet 5、Claude Opus 4.8 三种生成模型，改走原生 A 口 `/v1/messages`。两种协议运行前仍统一通过 `/v1/models` 精确校验 19 个生成模型和配置中保留的 3 个评委 wire model ID，不做模糊匹配或静默替换；评分 V3 实际只调用 Sol、Grok。

## 生成

命令必须显式指定模型或 `--all`；无参数不会误触全量调用。

```bash
# 单模型生成或断点续跑
python runner/generate.py --model deepseek-v4-flash

# 多模型
python runner/generate.py --model deepseek-v4-flash --model kimi-k3

# 全量就绪检查：不发 completion 请求
python runner/generate.py --all --dry-run

# 显式运行全部 19 个模型
python runner/generate.py --all

# 低成本烟测，提交第一章后暂停
python runner/generate.py --model deepseek-v4-flash --stop-after chapter:1

# prompt、配置或协议变化后授权新实验
python runner/generate.py --model deepseek-v4-flash --new-run
```

`--stop-after` 还接受 `book`、`macro-outline`、`opening-outline`。已经完成且哈希匹配的作品会在读取 API key 前离线跳过。

PowerShell 包装器：

```powershell
.\runner\generate.ps1 -Models deepseek-v4-flash -DryRun
.\runner\generate.ps1 -Models deepseek-v4-flash -StopAfter chapter:1
.\runner\generate.ps1 -All
```

Legacy 十章生成器仅用于维护旧静态数据，不属于 V2.1：

```bash
python runner/generate_legacy.py --help
```

## 五评委评分

| 评委 | 模型 |
|---|---|
| Sol | `gpt-5.6-sol` |
| Grok 4.5 | `grok-4.5` |
| DeepSeek V4 Pro | `deepseek-v4-pro` |
| MiMo V2.5 Pro | `mimo-v2.5-pro` |
| Gemini 3.1 Pro | `gemini-3.1-pro` |

五位活动评委收到相同、匿名且未截断的方向、规划和正文。评分 V3 不再要求评委给出笼统总分，而是让每位评委独立评价八个维度：

| 字段 | 维度 | 权重 | 方向 |
|---|---|---:|---|
| `theme_fulfillment` | 题材与主题兑现 | 10% | 越高越好 |
| `historical_grounding` | 时代与现实质感 | 15% | 越高越好 |
| `characters` | 人物与关系 | 15% | 越高越好 |
| `plot_causality` | 情节驱动与因果 | 15% | 越高越好 |
| `longform_structure` | 长篇结构与连续性 | 15% | 越高越好 |
| `scene_execution` | 场景与叙事效能 | 10% | 越高越好 |
| `style_control` | 文风管理 | 10% | 越高越好 |
| `ai_flavor` | AI 味 | 10% | **越低越好** |

每份评委响应只包含 `dimensions`；八个维度分别给出允许一位小数的分数和简评：

```json
{
  "dimensions": {
    "theme_fulfillment": {
      "score": 87.4,
      "comment": "改革进程切实改变了人物选择，主题兑现充分。"
    },
    "historical_grounding": {
      "score": 82.6,
      "comment": "制度与生活细节可信，少数时代信息仍偏说明。"
    },
    "characters": {
      "score": 84.1,
      "comment": "主要人物动机清楚，关系变化有连续铺垫。"
    },
    "plot_causality": {
      "score": 78.5,
      "comment": "主线因果成立，个别转折依赖巧合。"
    },
    "longform_structure": {
      "score": 80.3,
      "comment": "开篇结构稳定，支线与主线衔接尚可。"
    },
    "scene_execution": {
      "score": 85.7,
      "comment": "关键场景有动作、空间和情绪推进。"
    },
    "style_control": {
      "score": 81.9,
      "comment": "叙述语体总体统一，少量段落修辞偏满。"
    },
    "ai_flavor": {
      "score": 18.2,
      "comment": "偶见概括式收束，但模板化痕迹较轻。"
    }
  }
}
```

评分按一位小数写出，缓存、聚合和公开页面也统一保留并显示一位小数；解析器只会把模型偶发返回的整数规范为 `x.0`，不会让公开分数退回整数制。除 `ai_flavor` 越低越好外，其余七个维度均越高越好；只有五位活动评委的八维评分及其一致的完整聚合记录全部有效时，作品才进入榜单。

综合雷达的每一根轴分别取五位活动评委在该维度上的**中位数**。它不是旧版评委总分的平均值。雷达图为了保持“越外越好”的统一方向，将 AI 味轴绘制为 `100 - AI味中位数`，页面同时保留原始 AI 味数值并明确标注“越低越好”。

综合数值采用透明固定权重：七个正向维度分别乘以上表权重，AI 味项使用 `(100 - AI味中位数) × 10%`，最终保留一位小数。首页可按综合数值或任一维度排序，不再提供逐评委排名；作品详情展示综合中位数雷达，以及五位评委各自的雷达与逐维点评。

上一版已发布站点中的两评委（Sol + Grok）聚合属于过渡口径。扩展到五评委后，旧的两评委 `aggregate.json` 不再满足排名条件，需要补跑三位新评委并重写聚合。Kimi K3 仍不是 V3 排名的前置条件，其旧评分文件只保留为 V2 历史记录。

评分请求与小说创作请求彼此独立。配置中仍保留未启用的 Kimi 评委定义，便于追溯 V2 历史；它不会被 V3 评分命令、聚合或站点排名使用，也不影响小说创作请求。

`gpt-5.6-sol` 候选的 Sol 一票属于同模型自评；DeepSeek / MiMo / Gemini 候选若与同名评委重合，解读时也应交叉对照其余独立票。

```bash
python runner/score.py --model mimo-v2.5 --dry-run
python runner/score.py --model mimo-v2.5
python runner/score.py --model mimo-v2.5 --judge sol
python runner/score.py --all --dry-run
python runner/score.py --all
```

PowerShell：

```powershell
.\runner\score.ps1 -Model mimo-v2.5 -DryRun
.\runner\score.ps1 -All
```

评分缓存由评分 schema、完整作品哈希、评分 prompt、评委模型和请求参数共同决定。V2 缓存不会作为 V3 有效评分复用。

## 产物

公开、可提交的 V2.1 结果：

```text
benchmark/reform-era/direction.md
results/reform-era/<model>/
  book.json
  macro_outline.json
  opening_outline.json
  chapters/*.md
  novel.md
  manifest.json
  scores/{sol,grok,ds-v4-pro,mimo-v2.5-pro,gemini-3.1-pro,aggregate}.json
```

旧版 `kimi.json` 仍可能存在于历史结果目录，但不参与 V3 缓存判定、聚合或页面展示。

私有审计与断点位于被忽略的 `work/`：

```text
work/v2.1/reform-era/<model>/<run-id>/
  session.json
  state.json
  usage.jsonl
  usage-events/
  accepted/
  raw/
  failures/
```

`.env`、认证头、raw response 和 reasoning 不进入公开产物。

## 站点

站点由已提交的 `results/`、Legacy `novels/` 和 `site/assets/` 确定性构建。默认输出到被忽略的 `.site/preview/`：

```bash
python scripts/generate_site.py
python scripts/generate_site.py --docs-dir _site
bash scripts/generate-site.sh --docs-dir .site/preview
```

PowerShell：

```powershell
.\scripts\generate-site.ps1 -DocsDir .site/preview
```

GitHub Actions 在无 API key 环境运行测试、重建 `_site/` 并部署 Pages；CI 不执行生成或评分。旧小说来源保留在 `novels/`，原有 `/novels/...` 路由继续生成。线上 Pages 曾发布两评委评分 V3；当前代码已切换为五评委口径，完整排名需补齐三位新评委评分后重建站点。

## 目录

| 路径 | 说明 |
|---|---|
| `config.yaml` | provider、19 个生成模型、评委定义和上下文配置 |
| `benchmark/reform-era/` | V2.1 固定方向 |
| `runner/prompts/v2.1/` | 活动生成 prompt |
| `runner/prompts/v2/` | 共享的总纲修复 prompt 与评分 rubric |
| `runner/generate.py` | V2.1 可恢复生成状态机 |
| `runner/score.py` | 五评委评分与聚合 |
| `results/reform-era/` | 正式公开结果 |
| `work/` | 本地私有审计和恢复状态 |
| `novels/` | Legacy 站点来源 |
| `site/assets/` | 站点 CSS/JS 源码 |

## 全量运行提醒

`--all` 会生成约 19 × 5 万字正文；若 19 个模型均生成完成，当前五评委口径的完整评分还会产生 95 次携带全文的请求。开始付费全量前必须先执行测试、生成 dry-run、评分 dry-run 和离线建站，并核对实际计费。

当前完成度和下一步见 [TODO.md](TODO.md)。测试通过只表示流程就绪，不等于全量生成或评分已经完成。
