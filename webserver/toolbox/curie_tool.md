# Curie 无剧透导读（curie-tool）— PR 说明文档

## 项目来源

- **上游项目**：[Fank1/curie](https://github.com/Fank1/curie) —— Erik Fanki 开发的 Calibre 插件，
  用 Claude 为书籍生成角色/地点的**无剧透**简介，并以 EPUB 3 弹窗脚注形式注入。
- **授权**：作者已通过 GitHub 明确许可（原话 "Borrow or steal the code you want"）。
- **署名**：核心逻辑保留原注释署名 Erik Fanki；MyBooks 适配作者 shiningsprk-arch。

## 与上游的差异

| 项 | Fank1/curie | 本工具（MyBooks） |
|---|---|---|
| 运行环境 | Calibre GUI 插件（QThread） | MyBooks Toolbox 后台任务（AsyncService daemon 线程） |
| 注入对象 | 直接改写库内 EPUB（带移除功能） | **生成新 EPUB 重新入库**，原文件零改动，删除新书即还原 |
| LLM | 仅 Anthropic Claude（web_search 工具） | 双模式：Claude（联网研究，推荐）+ OpenAI 兼容（DeepSeek/Qwen，无搜索降级） |
| API Key | Calibre JSONConfig | 仿 mimo_tts 的 PBKDF2 加密配置（`{work_dir}/api_config.enc`） |
| 进度 | QThread 信号 | `BackgroundTask` + 轮询进度接口 |
| 语言 | 手动选择 | 默认跟随书籍元数据语言，页面可覆盖 |
| 名称匹配 | `\b...\b` 词边界（仅适用于英/瑞典语） | **CJK 友好边界**：`张三` 可匹配 `张三说`/`说张三`，单字名（`三`）不误匹配 `刘三`，`Alice` 不误匹配 `MyAlice` 且可匹配 `Alice笑着说`；计数（`_scan`）与注入使用同一规则 |
| 昵称/别名 | 仅保留大写字母开头 | 中文/日文昵称（`三哥`）与多字地点别名（`兔子洞`）保留；Latin 规则不变 |
| 防剧透 | 提示词自觉 + 首章不注入 | **双重机制**：(1) Step4「读者视角审查」——抽取实体首章之后的名字出现窗口作剧透源，重写泄露后文信息的描述；(2) 首章不注入（上游）|
| 脚注内容 | 静态描述 | **渐进式时间线**：Step4 同时为实体构建 2-5 阶段 `timeline`（阶段数模型自定，按剧情节点），正文链接按当前章节选择阶段文件（`curie-char-1-v2.xhtml`），读者读到哪，脚注就显示"读到那为止"的信息 |
| 同名消歧 | 无（重复条目/共享短名易错标） | **去重合并**（`_dedupe_entities`：同名条目合并 nickname/description/timeline，occurrences 重算）+ **活跃区间**（`_add_active_ranges`：按名字实际出现位置生成 `active_from/to`，注入时每章按"该章活跃者优先"排序，共享短名（如两代"阿玛兰妲"）归属到当前活跃的实体，实体 id 跨章保持稳定）+ **段落级指代裁决**（`mentions`：enrich 单遍输出，见下） |
| 指代消歧（新）| 裸名（"奥雷里亚诺"）按注入顺序先到先得 | **enrich 单遍产出 `mentions` 表**：段落标记文本（`[pN]`，与注入端计数共用 `_BLOCK_OPEN_RE` 严格对齐），模型对模糊名（共享名或子串名，程序化 `_find_ambiguous_names` 检测）逐段裁决归属（`{"name","chapter","para","entity"}`）；注入时按段查表归属，无裁决段回退活跃区间。**零新增调用**——enrich 原本就读全书 |

## 交付文件（相对仓库根）

```
webserver/toolbox/curie_tool.py        ← 工具类（新增）
webserver/toolbox/curie/__init__.py    ← 核心包（新增）
webserver/toolbox/curie/epub_utils.py  ← 移植（仅去 calibre_plugins 依赖）
webserver/toolbox/curie/epub_injector.py ← 移植（仅去 calibre_plugins 依赖）
webserver/toolbox/curie/api_client.py  ← 双模式 provider（改造）
webserver/toolbox/curie/pipeline.py    ← worker.py 流水线移植（改造）
webserver/toolbox/toolset.py           ← 注册（+2 行）
webserver/handlers/toolbox.py          ← 6 个接口 + 6 条路由（+~130 行）
app/src/pages/toolbox/curie.vue        ← Vue 页面（新增，Vuetify 2.6 语法）
app/locales/{en,zh,zh-TW}.json         ← i18n `curie` 块（三文件同步）
```

## API 接口

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/toolbox/curie/convert` | POST | 启动生成（book_id, provider, api_key, model, api_url, include_characters, include_places, language, hint_density） |
| `/api/toolbox/curie/progress` | GET | 进度查询（stage / new_book_id / characters / locations） |
| `/api/toolbox/curie/config` | GET/DELETE | 读取/清除加密配置 |
| `/api/toolbox/curie/test` | POST | 测试连接（成功自动保存配置） |
| `/api/toolbox/curie/preview` | POST | 查看已生成的角色/地点数据 |
| `/api/toolbox/curie/regenerate` | POST | 用缓存 JSON 按新密度重新注入（不消耗 API 额度） |

## 关键设计点

1. **新书入库**：`{title}（Curie 导读版）`，作者沿用原书，标题保留原书名方便搜索；
   用 `BaseTool.import_file(..., delete_after_import=False)` 导入，work dir 保留产物
   （`source.epub` / `book_data.json` / `curie_out.epub`）。
2. **双模式**：`api_client.make_provider()` 工厂；Claude 模式完整保留 web_search 工具调用、
   prompt cache（cache_control）与限速等待；OpenAI 兼容模式无搜索、无 cache，分块预算降到 40k token。
3. **成本透明**：Claude 模式按真实 usage 计费展示（原版 calc_cost）；OpenAI 兼容按 token 数估价。
4. **幂等**：重复注入前先剥离旧注入；`remove_injections` 可完整还原（保留 xmlns:epub 声明，与上游一致）。
5. **并发保护**：单实例锁 + `is_running()`（仿 mimo_tts）。

## 测试

`tests/test_curie_core.py`（17 项，纯本地无需 API key）：

```
PASS test_add_chapter_and_occurrences
PASS test_chunking
PASS test_cjk_name_boundaries        # CJK 边界规则
PASS test_extract_json_tolerates_noise  # 多 JSON/杂讯容错
PASS test_inject_cjk_book            # 中文书全流程注入
PASS test_inject_footnotes           # 注入/OPF/幂等/还原
PASS test_inject_timeline_progressive  # 阶段文件选择
PASS test_pipeline_e2e_with_mock     # 全流水线 mock 端到端
PASS test_post_processing
PASS test_remove_without_injection_is_noop
PASS test_review_for_spoilers_applies_fixes  # 审查修正 + timeline 应用
PASS test_review_for_spoilers_no_material_noop
PASS test_spine_extraction
PASS test_timeline_stage_selection   # 章节→阶段映射
PASS test_dedupe_entities            # 同名重复条目合并
PASS test_active_ranges              # 活跃区间计算
PASS test_inject_shared_short_name_active_preference  # 共享短名按章节归属
```

## 待办（需要真实 API key 验证）

- [ ] 用真实 Claude API key 跑《三体》等中文书全流程，检查角色识别与脚注弹窗
- [ ] 用 DeepSeek（OpenAI 兼容模式）跑同一本书对比质量
- [ ] 在 epubjs / readium / candle-reader 三个阅读器验证脚注弹窗表现
- [ ] flake8 / eslint 检查（CI 同款命令）
