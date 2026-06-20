# show-me-your-novel

> 同一段提示词，不同模型写的小说，展示在一个 GitHub Pages 站点上对比。

每一部「小说」对应 `novels/` 下的一个目录，里面有一份 `prompt.md`。这份提示词会交给 `config.yaml` 里配置的所有模型各写一遍，每个模型生成一个版本。最后由脚本渲染成一个静态站点，发布到 GitHub Pages。

- **统一工具**：所有模型都通过 [OpenCode](https://opencode.ai) CLI 调用，脚本模式（`opencode run`）。
- **锁死 opencode-go**：所有模型走 opencode-go 订阅服务，key 放在 `.env`，由仓库根目录的 `opencode.json` 注入。本地一键过一遍全部模型。
- **隔离工作区**：每个模型在独立的 `work/<story>/<model>/` 目录里运行，互不可见。
- **幂等生成**：`novels/<story>/<model>.md` 已存在就跳过；删掉再跑才会重新生成。
- **本地生成 + 推送部署**：在本地生成小说和站点，提交 `docs/` 后 CI 只负责部署 Pages，云端不碰 API key。

## 目录结构

```
show-me-your-novel/
├── config.yaml              # 模型列表（id / name / model）
├── opencode.json            # 把 opencode-go 的 key 指向 {env:OPENCODE_API_KEY}
├── .env                     # OPENCODE_API_KEY=sk-...（不入库）
├── novels/
│   └── sci-fi-uplink/
│       ├── prompt.md        # 这部小说的统一提示词
│       └── <model>.md       # 各模型生成的版本
├── runner/generate.ps1      # 遍历 小说 × 模型，缺啥补啥（Windows / PowerShell）
├── runner/generate.sh       # 同上（bash 版，CI 或 macOS/Linux 用）
├── scripts/generate-site.ps1 # 从 novels/ 渲染 GitHub Pages 到 docs/
├── scripts/generate-site.sh
├── docs/                    # Pages 根目录（生成后提交，CI 部署）
└── .github/workflows/       # deploy-only CI
```

## 本地使用（Windows / PowerShell）

### 前置依赖

- `opencode` CLI：`npm install -g opencode-ai`（或见 [opencode.ai/install](https://opencode.ai/install)）
- `python3` + `pyyaml`：`pip install pyyaml`
- PowerShell 7（`pwsh`）

### 配置 key

在仓库根目录创建 `.env`：

```
OPENCODE_API_KEY=sk-...
```

> `.env` 已在 `.gitignore` 里，不会提交。
> 仓库根目录的 `opencode.json` 把 opencode-go provider 的 key 设为 `{env:OPENCODE_API_KEY}`，确保 opencode 用 `.env` 里的 key，而不是本机 `~/.local/share/opencode/auth.json` 里可能残留的旧 key。若出现 `Invalid API key`，先确认 `.env` 的 key 有效，并检查本机 auth.json 是否有过期凭证（`opencode auth list`）。

### 配置模型

编辑 [config.yaml](config.yaml)，每条模型形如：

```yaml
models:
  - id: qwen3.7-max            # 文件名 / slug
    name: Qwen3.7 Max          # 页面展示名
    model: qwen3.7-max         # 模型 id（不含 provider 前缀，runner 自动拼成 opencode-go/<model>）
```

默认配置已包含 opencode-go 的全部 13 个模型。可用 `opencode models opencode-go` 查看（需先配好 key）。

### 生成小说

```powershell
# 生成全部缺失的小说版本（全部模型）
.\runner\generate.ps1

# 只处理某一部小说
.\runner\generate.ps1 -Story sci-fi-uplink

# 只用指定模型（便于先试一个）
.\runner\generate.ps1 -Story sci-fi-uplink -Models qwen3.7-max

# 重新生成某个版本：删掉对应文件再跑
Remove-Item novels/sci-fi-uplink/qwen3.7-max.md
.\runner\generate.ps1 -Story sci-fi-uplink
```

### 生成站点并预览

```powershell
.\scripts\generate-site.ps1
# 本地打开
Start-Process docs/index.html
```

### 推送部署

生成满意后，把 `novels/` 和 `docs/` 一起提交推送，CI 会自动部署 Pages：

```powershell
git add novels docs
git commit -m "novel: 生成《超时空链接》各模型版本"
git push
```

## macOS / Linux（bash）

同样提供了 bash 版脚本，`.env` 用 `export OPENCODE_API_KEY=...` 或 `set -a; . .env; set +a`：

```bash
export OPENCODE_API_KEY=sk-...
bash runner/generate.sh
bash scripts/generate-site.sh
open docs/index.html
```

## 添加一部新小说

1. 在 `novels/` 下新建目录：`mkdir novels/my-story`
2. 写一份 `novels/my-story/prompt.md`。第一行 `# 标题` 会作为小说名；建议包含 `## 题材` 和 `## 世界观设定` 两节，首页会据此生成简介。
3. 运行 `.\runner\generate.ps1 -Story my-story`，再 `.\scripts\generate-site.ps1`，提交推送即可。

## CI

仓库推送到 GitHub 后：

1. 在 **Settings → Pages** 里把 Source 设为 **GitHub Actions**。
2. 无需配置任何 secret —— 本地生成，CI 只部署 `docs/`。
3. 之后每次改动 `docs/`（或本工作流文件）并推送到 `main`，都会触发 [generate.yml](.github/workflows/generate.yml) 部署 Pages。也可手动触发（workflow_dispatch）。

## OpenCode 调用方式

每个版本由这样一次调用产出（`work/` 是隔离目录）：

```powershell
opencode run `
  --model opencode-go/qwen3.7-max `
  --dir work/sci-fi-uplink/qwen3.7-max `
  "$(Get-Content novels/sci-fi-uplink/prompt.md -Raw)"
```

provider 锁死 opencode-go、key 由 `opencode.json` 从 `OPENCODE_API_KEY` 注入；`--dir` 把工作区限制在隔离目录内，模型间互不可见。
