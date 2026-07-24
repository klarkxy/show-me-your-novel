# TODO：V2.1 全量运行清单

## 当前基线

- [x] 活动协议只保留 48,000 字最终最低完成线；章节和整书均无字数上限。
- [x] 章节提示词建议约 3,000–4,000 字；不足 3,000 字只额外扩写一次。
- [x] 生成 API 只发送 `model` 与 `messages`，思考、温度和输出长度使用服务端默认。
- [x] MiMo V2.5 已完成：`run_id=6767704f6322`，17 章、61,495 字，公开结果深校验通过。

## 付费全量前置检查

- [x] 全部 pytest：`96 passed`（2026-07-23）。
- [x] 生成 dry-run：15 个生成模型和 3 个评委的 wire ID 全部存在，API 可选参数为空，未发 completion（2026-07-23）。
- [x] `python runner/score.py --model mimo-v2.5 --dry-run` 正确识别成品，三位评委均为 `would-score`（2026-07-23）。
- [x] 默认离线建站成功输出到 `.site/preview`，V2.1 首页、MiMo 详情页和 Legacy 路由存在，旧 `docs/` 未重新生成（2026-07-23）。
- [x] 工作树分组审查完成；`.env`、`work/`、缓存和构建目录均未进入提交（2026-07-23）。

本机保留了 DeepSeek V4 Flash 的旧私有断点，因此单命令 `--all --dry-run` 会在离线跳过 MiMo 后要求 `--new-run`。已通过等价的分组检查：

```bash
python runner/generate.py --model deepseek-v4-flash --new-run --dry-run
python runner/generate.py \
  --model deepseek-v4-pro --model mimo-v2.5-pro --model minimax-m3 \
  --model glm-5.2 --model gpt-5.6-luna --model claude-haiku-4-5 \
  --model claude-sonnet-5 --model gemini-3.1-pro --model gemini-3.5-flash \
  --model kimi-k3 --model grok-4.5 --model claude-opus-4-8 \
  --model agnes-2.0-flash --dry-run
```

正式运行也沿用相同分组并去掉 `--dry-run`；这样保留旧审计数据，同时不会重跑已完成的 MiMo。

## 全量生成与评分

- [ ] 运行剩余 14 个生成模型，并逐部核对相同方向、独立会话、规划规模、章节数和至少 48,000 字正文。
- [ ] 对完整作品运行 Sol、Grok 4.5、Kimi 三份独立评分。
- [ ] 缺任一评委的作品保持 `incomplete`，不得获得名次。
- [ ] 核对综合榜按三者 `score` 平均值降序，AI 味榜按 `ai_flavor` 平均值升序；同分保持配置顺序。
- [ ] 最终重新运行全部测试、评分 dry-run、深校验和离线建站。

## 发布

- [ ] 只提交通过校验的 `results/` 与评分文件。
- [ ] 推送后确认 GitHub Actions 在无 API key 环境成功重建 Pages。
- [ ] 检查公开 HTTPS 首页、至少一个 V2.1 详情页和 Legacy 旧链接。
- [ ] 确认公开页面及 Git 历史不包含 `.env`、认证头、raw response、reasoning 或 `work/` 内容。

## 操作原则

- 先 dry-run，再第一章烟测，再完成单部，最后才全量运行。
- 不因为单元测试或模拟通过而宣称真实 API 全量完成。
- 不手改模型作品或评分；失败后从 accepted-only 权威会话恢复。
- 不设置章节或整书字数上限，不通过 API 参数限制输出长度。
- 真实 usage 与费用单独记录，不把成本混入文学评分。
