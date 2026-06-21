# show-me-your-novel

> 同一段提示词，不同模型写的小说，展示在一个 GitHub Pages 站点上对比。

每一部「小说」对应 `novels/` 下的一个目录，里面有一份 `prompt.md`。这份提示词会交给 `config.yaml` 里配置的所有模型各写一遍，每个模型生成一个版本。最后由脚本渲染成一个静态站点，发布到 GitHub Pages。

- **直连 API**：通过 `runner/generate.py` 直接调用 OpenAI-compatible API，不依赖任何 CLI 工具。
- **统一 Provider**：所有模型走同一个 API 端点（[OpenCode Go](https://opencode.ai)），key 放在 `.env`。
- **分章生成 + 质检**：每次先生成大纲，再逐章生成，每章自动检验字数、格式、元评论，失败自动修正。
- **幂等**：`novels/<story>/<model>.md` 已存在且校验通过则跳过。
- **本地生成 + 推送部署**：在本地生成小说和站点，提交 `docs/` 后 CI 只负责部署 Pages，云端不碰 API key。

## 目录结构

```
show-me-your-novel/
├── config.yaml              # 模型列表（id / name / model）
├── .env                     # OPENCODE_API_KEY=sk-...（不入库）
├── novels/
│   └── <story>/
│       ├── prompt.md        # 这部小说的统一提示词
│       └── <model>.md       # 各模型生成的版本
├── runner/
│   ├── generate.py          # 核心：分章调用 API 生成小说
│   ├── generate.ps1         # 薄包装（PowerShell）
│   └── generate.sh          # 薄包装（bash）
├── scripts/
│   ├── generate_site.py     # 核心：从 novels/ 渲染站点到 docs/
│   ├── generate-site.ps1    # 薄包装（PowerShell）
│   └── generate-site.sh     # 薄包装（bash）
├── docs/                    # Pages 根目录（生成后提交，CI 部署）
└── .github/workflows/       # deploy-only CI
```

## 本地使用

### 前置依赖

- python3 + pyyaml：`pip install pyyaml`

### 配置 key

在仓库根目录创建 `.env`：

```
OPENCODE_API_KEY=sk-...
```

> `.env` 已在 `.gitignore` 里，不会提交。

### 配置模型

编辑 [config.yaml](config.yaml)，每条模型形如：

```yaml
models:
  - id: qwen3.7-max        # 文件名 / slug
    name: Qwen3.7 Max      # 页面展示名
    model: qwen3.7-max     # 传给 API 的 model 参数
    provider: opencode-go  # 对应 providers 下的 key（缺省为 opencode-go）
```

默认配置已包含 opencode-go 的全部 13 个模型。

### 生成小说

```bash
# 生成全部缺失的小说版本
python3 runner/generate.py

# 只处理某一部小说
python3 runner/generate.py --story sci-fi-uplink

# 只用指定模型（可多次指定）
python3 runner/generate.py --story sci-fi-uplink --model qwen3.7-max

# 强制重新生成（会清空中间产物）
python3 runner/generate.py --story sci-fi-uplink --reset

# bash / PowerShell 薄包装
bash runner/generate.sh sci-fi-uplink
.\runner\generate.ps1 -Story sci-fi-uplink
```

#### 生成流程

1. 读取 `prompt.md`，调用 API 生成 10 章结构化大纲（`outline.json`）
2. 逐章生成，每章把前文完整正文拼进上下文，保持连贯
3. 每章自动质检：字数 1500+、标题格式正确、无元评论、无代码围栏
4. 质检失败自动修正（最多 3 次）
5. 合并 10 章为 `<model>.md`，追加 `【未完待续】`
6. 最终校验：10 章、20000+ 字、结尾标记正确

### 生成站点并预览

```bash
# 独立 Python 脚本（推荐）
python3 scripts/generate_site.py

# 或通过薄包装
bash scripts/generate-site.sh
.\scripts\generate-site.ps1
```

### 推送部署

```bash
git add novels docs
git commit -m "生成新版本"
git push
```

### 添加一部新小说

1. 新建目录：`mkdir novels/my-story`
2. 写一份 `novels/my-story/prompt.md`。第一行 `# 标题` 会作为小说名；建议包含 `## 题材` 和 `## 世界观设定` 两节。
3. 运行 `python3 runner/generate.py --story my-story`，再 `python3 scripts/generate_site.py`，提交推送即可。

## CI

仓库推送到 GitHub 后：

1. 在 **Settings → Pages** 里把 Source 设为 **GitHub Actions**
2. 无需配置任何 secret —— 本地生成，CI 只部署 `docs/`
3. 之后每次改动 `docs/` 并推送到 `main`，都会触发 [generate.yml](.github/workflows/generate.yml) 部署 Pages

## 目录对照

| 目录/文件 | 作用 |
|-----------|------|
| `novels/<story>/prompt.md` | 统一提示词（手工编写） |
| `novels/<story>/<model>.md` | 各模型生成的小说正文 |
| `work/<story>/<model>/` | 中间产物（大纲、分章、原始输出），已 gitignore |
| `docs/` | 静态站点根目录（提交后 CI 部署） |
