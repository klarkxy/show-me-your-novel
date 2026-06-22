# show-me-your-novel

**在线站点**：[https://klarkxy.github.io/show-me-your-novel/](https://klarkxy.github.io/show-me-your-novel/)

这个项目用来横向对比不同中文大模型的长篇小说写作能力。每部小说对应 `novels/` 下的一个目录，里面只有一份 `prompt.md`；`runner/generate.py` 会让 `config.yaml` 里配置的所有模型各生成一版 10 章小说；最后 `scripts/generate_site.py` 渲染成静态站点，推送到 GitHub Pages。

## 项目特点

- **直连 LLM API**：通过 `runner/generate.py` 直接调用 OpenAI-compatible API，不依赖任何 CLI 工具。
- **分章生成 + 本地质检**：先出 10 章大纲，再逐章生成；每章自动校验字数、标题格式、元评论与代码围栏，失败会修正。
- **幂等重跑**：已有的小说版本如果通过校验会自动跳过，删了再跑才会重新生成。
- **本地生成，CI 只部署**：API key 只在本地使用，推送到仓库的只有小说原文和静态站点。

## 目录结构

```
show-me-your-novel/
├── config.yaml              # 模型列表
├── .env                     # API key（已被 gitignore）
├── novels/
│   └── <story>/
│       ├── prompt.md        # 统一提示词
│       └── <model>.md       # 各模型生成的小说正文
├── runner/
│   ├── generate.py          # 核心：分章生成小说
│   ├── generate.ps1         # PowerShell 薄包装
│   └── generate.sh          # bash 薄包装
├── scripts/
│   ├── generate_site.py     # 核心：渲染 GitHub Pages
│   ├── generate-site.ps1    # PowerShell 薄包装
│   └── generate-site.sh     # bash 薄包装
├── docs/                    # Pages 静态站点根目录
└── .github/workflows/       # 仅部署的 CI
```

## 快速开始

### 1. 安装依赖

```bash
pip install pyyaml
```

### 2. 配置 API key

在仓库根目录创建 `.env`：

```bash
OPENCODE_API_KEY=sk-...
```

`.env` 已被 `.gitignore` 排除，不会提交。

### 3. 生成一部小说

```bash
python3 runner/generate.py --story sci-fi-uplink
```

要跑全部模型、全部小说：

```bash
python3 runner/generate.py
```

### 4. 生成站点

```bash
python3 scripts/generate_site.py
```

然后打开 `docs/index.html` 预览。

### 5. 部署

```bash
git add novels docs
git commit -m "novel: 生成新版本"
git push
```

GitHub Actions 会自动部署 `docs/` 到 Pages。

## 生成命令参考

| 命令 | 作用 |
|------|------|
| `python3 runner/generate.py` | 生成所有缺失的小说版本 |
| `python3 runner/generate.py --story <slug>` | 只处理某一部小说 |
| `python3 runner/generate.py --story <slug> --model <id>` | 只使用指定模型（可多次指定） |
| `python3 runner/generate.py --story <slug> --reset` | 清空中间产物并重新生成 |
| `bash runner/generate.sh <slug>` | bash 薄包装 |
| `.\runner\generate.ps1 -Story <slug>` | PowerShell 薄包装 |

## 配置模型

编辑 [config.yaml](config.yaml) 增减模型：

```yaml
models:
  - id: qwen3.7-max        # 文件名与 URL slug
    name: Qwen3.7 Max      # 页面展示名
    model: qwen3.7-max     # API 参数
    provider: opencode-go  # 对应 providers 配置（缺省 opencode-go）
```

provider 配置在 `config.yaml` 顶部的 `providers:` 段：

```yaml
providers:
  opencode-go:
    base_url: "https://opencode.ai/zen/go/v1"
    api_key_env: "OPENCODE_API_KEY"
```

## 生成流程

```
prompt.md → 大纲（outline.json）→ 10 章正文 → 质检 → 合并为 <model>.md
```

1. 读取 `prompt.md`，生成 10 章结构化大纲；
2. 逐章生成，把前文完整正文拼进上下文，保证连贯；
3. 单章质检：字数 ≥1500、标题格式正确、无元评论、无代码围栏；
4. 质检失败自动基于当前文本修正，最多重试 3 次；
5. 合并 10 章为 `novels/<story>/<model>.md`，追加 `【未完待续】`；
6. 全文校验：10 章、≥20000 字、结尾标记正确。

中间产物写在 `work/<story>/<model>/`，已加入 `.gitignore`。

## 添加新小说

1. 新建目录：

   ```bash
   mkdir novels/my-story
   ```

2. 写提示词 `novels/my-story/prompt.md`。建议包含：

   - 第一行 `# 小说标题`（作为页面标题）
   - `## 题材`
   - `## 世界观设定`
   - `## 主角`
   - `## 开篇要求`（注明 10 章、每章 2000–3000 字、第三人称限知视角等）

3. 生成并部署：

   ```bash
   python3 runner/generate.py --story my-story
   python3 scripts/generate_site.py
   git add novels docs
   git commit -m "novel: 新增《小说标题》"
   git push
   ```

## CI 部署

仓库推送到 GitHub 后：

1. 进入 **Settings → Pages**，把 Source 设为 **GitHub Actions**。
2. 不需要配置任何 secret；CI 只部署已提交的 `docs/`。
3. 每次 `docs/` 或 `.github/workflows/generate.yml` 推送到 `main`，都会触发 [generate.yml](.github/workflows/generate.yml)。

## 常见文件说明

| 路径 | 说明 |
|------|------|
| `novels/<story>/prompt.md` | 统一提示词，手工编写 |
| `novels/<story>/<model>.md` | 模型生成的小说正文 |
| `work/<story>/<model>/` | 大纲、分章、原始输出等中间产物 |
| `docs/` | Pages 静态站点根目录 |
