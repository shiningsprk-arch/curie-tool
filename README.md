# Curie 无剧透导读（curie-tool）

用 LLM（Claude / DeepSeek 等）为书籍生成**角色与地点的无剧透简介**，以 EPUB 3 弹窗脚注形式注入，生成一本新书并重新入库——原文件零改动，删除新书即可还原。

基于 [Fank1/curie](https://github.com/Fank1/curie)（Fank1 的 Calibre 插件）移植，**已获作者明确许可**。适用于 [PoxenStudio/mybooks](https://github.com/PoxenStudio/mybooks) 的 Toolbox 插件体系。

## 功能特性

- **无剧透防泄漏（双重机制）**
  - 程序化重算：角色的首现章节与出现次数由代码从正文统计，绝不信任 LLM 填写的值；
  - Step 4「读者视角审查」：抽取角色首章之后的正文窗口作为剧透源，由模型重写任何泄露后文信息的描述。
- **渐进式时间线脚注**：审查时同时为每个实体构建 2-5 个阶段（模型自定），正文链接按当前章节自动选择对应阶段文件——读者读到哪，脚注就只显示"读到那为止"的信息。
- **同名消歧**：去重合并重复条目 + 活跃区间排序（同一章内谁"在场"谁优先抢注共享短名）+ enrich 单遍产出段落级 `mentions` 表（如《百年孤独》里"奥雷里亚诺"逐段裁决归属上校/巴比伦/阿玛多），零额外 API 调用。
- **双 provider 格式**
  - `anthropic 格式`（推荐）：支持内置联网搜索 `web_search`，资料准确；
  - `openai 格式`：OpenAI 兼容接口（DeepSeek / Qwen 等），无联网搜索。
- **CJK 友好的名称匹配**：`张三` 可匹配 `张三说` / `说张三`；单字名（`三`）不误匹配 `刘三`；`Alice` 不误匹配 `MyAlice`，且可匹配 `Alice笑着说`。
- **脚注密度可选**：每次出现 / 每 10 段一次 / 每章一次。
- **重新生成**：复用已生成的角色数据，只按新密度重注脚注，不消耗 API 额度。
- **语言跟随书籍元数据**，页面可覆盖（zh / en / sv / ja / fr / de / es / ko 等）。

## 工作原理

| 步骤 | 做什么 |
|---|---|
| Step 1 | 联网搜索（anthropic 格式）或模型知识生成角色/地点清单 |
| Step 2 | 解析 EPUB → 程序化统计首现章节与出现次数 → 分块交给模型做增量增强（修正拼写、补充昵称、去剧透）并逐段裁决模糊名归属（`mentions`）|
| Step 4 | 读者视角剧透审查 + 渐进式时间线构建 |
| Step 3 | 注入 EPUB 3 弹窗脚注（每个实体独立 `linear="no"` 文档）→ 生成新 EPUB |
| 入库 | 以 `《书名（Curie 导读版）》` 重新入库，原文件不动 |

## 安装

将本仓库文件并入 MyBooks 插件目录：

```
webserver/toolbox/curie_tool.py        # 工具类（BaseTool）
webserver/toolbox/curie/               # 核心包（epub_utils / epub_injector / api_client / pipeline）
webserver/toolbox/toolset.py           # 注册（1 处 import + 1 处 register）
webserver/handlers/toolbox.py          # 6 个接口 + 6 条路由
app/src/pages/toolbox/curie.vue        # Vue 页面（Vuetify 2.6 语法）
app/locales/{en,zh,zh-TW}.json         # i18n "curie" 块
```

API 接口（`/api/toolbox/curie/*`）：`convert`（生成）、`progress`（进度）、`config`（加密配置读写）、`test`（测试连接）、`preview`（查看已生成数据）、`regenerate`（按新密度重注）。

## 使用

1. 在工具箱打开「Curie 无剧透导读」，搜索并选择一本书（需含 EPUB 格式）；
2. 选择 **API 格式**（推荐 `anthropic 格式`，可开启联网搜索 web_search）并填写 API 地址、模型、API Key；
3. 点「测试连接」验证（成功后配置加密保存）；
4. 选择提示语言与脚注密度，点「生成导读」；
5. 完成后书库出现 `《书名（Curie 导读版）》`，阅读器中点击名字查看无剧透脚注。

### API 地址参考

| 格式 | 地址 | 模型示例 |
|---|---|---|
| anthropic | `https://api.anthropic.com` | `claude-sonnet-4-6` |
| anthropic（DeepSeek 兼容端点） | `https://api.deepseek.com/anthropic` | `deepseek-v4-flash` |
| openai | `https://api.deepseek.com` | `deepseek-chat` |

> DeepSeek 等基础地址会自动补全 `/anthropic`；API Key 通过 PBKDF2 派生密钥加密落盘（`{work_dir}/api_config.enc`），代码中不保存任何明文密钥。

## 测试

```bash
python tests/test_curie_core.py    # 34 项单元测试（核心解析/注入/流水线/容错）
python tests/test_deepseek_live.py <输入.epub> <输出.epub> --url <endpoint> --model <model> --lang Chinese
```

`test_deepseek_live.py` 需要环境变量 `DEEPSEEK_API_KEY`（真实 API 端到端验证）。

## 目录结构

```
webserver/toolbox/curie/
  epub_utils.py      EPUB 解析、CJK 名称边界、段落编号（与注入端严格对齐）
  epub_injector.py   EPUB 3 弹窗脚注注入 / 移除 / OPF 清单维护
  api_client.py      双 provider（anthropic / openai 兼容）+ 提示词 + 容错
  pipeline.py        流水线编排、防剧透审查、时间线、去重消歧、mentions
webserver/toolbox/curie_tool.py    MyBooks 工具类（任务、进度、加密配置、重新入库）
webserver/toolbox/curie_tool.md    PR 说明文档（与上游差异、关键设计点）
app/src/pages/toolbox/curie.vue    前端页面
tests/test_curie_core.py           单元测试
```

## 许可与致谢

- 本项目以 **AGPL-3.0** 开源（见 [LICENSE](LICENSE)）；
- 上游：[Fank1/curie](https://github.com/Fank1/curie)，已获作者许可移植；
- 适配（MyBooks）：shiningsprk-arch。

> 演示页说明：仓库根目录提供 `curie导读-前端演示.html` 可选预览，但完整功能需在 MyBooks 环境运行。
