# mobo 群友化改造 — MaiBot 移植设计文档

状态:v3(已按 oracle 冷审修订 + 结论一致性修复,待用户批准)
日期:2026-08-25
决策依据:MaiBot v1.2.3 代码研究(本地克隆 `F:\Projects\maibot-study`)、mobo 现状代码核查、oracle 独立冷审一轮

---

## 0. 背景与目标

mobo 的定位从"陪伴型智能体"改为**群友**:一个活跃的、有记忆的群成员,不是带命令面板的服务。

三条设计原则(来自 MaiBot 研究结论,用户认可):

1. **无显式控制命令**——用户不能用斜杠命令让 bot 记住内容;记忆只通过观察对话被动形成(MaiBot README:"一个你无法完全掌控的个体才更能让你感觉到它的自主性")。
2. **"最像而不是好"**——拟人细节(错别字、碎句、打字节奏)是核心竞争力,不是装饰。
3. **有界**——所有 LLM 行为有轮数/预算/冷却上限,沿用 mobo 既有哲学。

硬需求:**bot 必须能通过内部 API 调用其他 bot**(Phase 3 工具桥)。

前置条件:项目未上线,无数据迁移、无兼容包袱,可以自由改表结构、删表、删代码。

## 1. 基线与不变量

### 1.1 mobo 现状基线(相关模块,已对照代码核实)

| 模块 | 位置 | 现状 |
|---|---|---|
| 回复决策 | `app/behavior.py` ProactiveService(概率内核 :253-258,外层闸门独立) | 基础概率 + 偏好/熟悉度缩放;冷却/日限/静音时段在外层 |
| 去抖/打字 | `app/conversation.py` + `discord_bot.py:1439` | 固定去抖 + typing 指示器 |
| 长消息拆分 | `discord_bot.py:88 _chunks`;两处调用点:`:1519`(zip 路径)与 `:1630 _send_public_reply` | 仅按 1980 字符硬拆,连发无间隔 |
| 提示词组装 | `app/cognition.py:380-527` ContextBuilder 单 f-string | 公聊剥离个人记忆,私聊注入 |
| 记忆 | `app/memory.py`;`memories` 表**已有** `guild_id`/`kind`/`confidence`/`importance`/`status('active','forgotten')`;重复内容已做置信度强化(`memory.py:81`);`expires_at` 过滤已有(`memory.py:262`) | 词法提取(第一人称,取 top-2,`memory.py:334`) |
| 学习钩子 | `discord_bot.py:1659 _learn_after_success`(:1677 已调 auto_extract) | 回复成功后统一学习入口 |
| 表情反应 | `discord_bot.py:2035/2076 on_raw_reaction_add/remove` | **仅入站**:用户点 mobo 的表情用于反馈学习;mobo 从不主动给别人点表情(本计划补上,见 §5.4) |
| 模型路由 | `app/llm.py:15` `ModelRole = Literal["chat","deep","utility"]`,已全链路接好(`discord_bot.py:1984` 已用 `role="utility"`) | 三档已存在,无需新增 |
| 公开命令 | `discord_bot.py:286-451` | /帮助 /记住 /我的记忆 /忘记我 /隐私 /关系 /喜好 |
| 审计 | `audit_log(actor, action, target, details_json)`(`database.py:149`,已有保留期清理 :669) | 通用审计表 |

### 1.2 不变量(任何阶段不得破坏)

- 单进程、单 SQLite 写者;不引入外部服务(Mongo/Redis/向量库)
- 输入/输出双向安全引擎(`app/safety.py`);输出安全检查必须在**拟人化变换之后**执行
- `/忘记我` 全量清除覆盖所有新表/新列
- Web 管理台是唯一运维面;Discord 侧管理命令保留但仅运维可见
- 现有 18 个测试文件保持绿

## 2. 总体决策

**保留 mobo 架构,外科手术式移植 MaiBot 三个可移植件;mobo 整体重许可为 GPL-3.0。**

| 移植件 | 源位置 | 方式 |
|---|---|---|
| 回复必要性闸门 | `src/maisaka/reply_necessity.py`(277 行,纯函数已核实) | 改写移植:删 QQ 噪声正则(CQ 码/合并转发/发言榜/其他 bot),保留评分核 |
| 拟人化后处理 | `src/chat/utils/typo_generator.py`(478 行,依赖 jieba+pypinyin)、`utils.py:567 process_llm_response_segments`、`math_utils.py:85 calculate_typing_time` | 精简拷贝:只取拆分+合并核、字符级错别字;不带 main/调试/纠错建议(~130 行)与引用上文机制;**必须修复** jieba 词典每次调用重读(`typo_generator.py:255-263`)与相对路径缓存(`:50`) |
| zh-CN 提示词结构 | `prompts/zh-CN/*.prompt` | 结构性借鉴(两个新区块内联进现有 build(),不重构、不引模板引擎) |

~~多模型任务路由~~(v1 计划项,冷审后删除:chat/deep/utility 三档已存在)。
不拿:A_Memorix(6.5 万行,需 embedding)、webui、plugin_runtime、maim_message 协议、Maisaka runtime 本体。

## 3. Phase 0 — 许可证切换(前置,半天)

在拷贝第一行 MaiBot 代码**之前**完成:

1. `LICENSE` 替换为 GPL-3.0 全文
2. `pyproject.toml` license 字段改为 `GPL-3.0-or-later`
3. `NOTICE` 追加:衍生自 CryptoJones/1812(Apache-2.0,沿用);拟人化模块与提示词结构衍生自 Mai-with-u/MaiBot(GPL-3.0)
4. README 许可证章节更新

**验证**:`git log` 确认 LICENSE 变更提交先于任何代码移植提交。

## 4. Phase 1 — 群友化裁剪(1-2 天)

### 4.1 命令砍留

| 命令 | 处置 |
|---|---|
| /记住 /我的记忆 /关系 /喜好 | **删**。自然语言路径已有:auto_extract 处理第一人称陈述("记一下,我怕辣") |
| /帮助 | 留,瘦身:只列 /隐私 /忘记我 + 一句"直接聊天就行" |
| /忘记我 /隐私 | **留**(数据卫生底线,不破坏群友体感) |
| 全部管理命令 | 留(运维面,用户不可见) |

### 4.2 manual_memories 整体移除

未上线、无迁移负担 → 表、注入路径(`cognition.py:460-470`)、`memory.py:181-227` 访问函数、`/忘记我` 对应清除分支一并删除。遗留关键词不再注入。

### 4.3 公聊记忆范围重设计(群友关键项)

现状:公聊提示词只注入风格信号,个人记忆仅私聊注入 → 群里"装不熟",与群友定位矛盾。

新规则(利用 `memories.guild_id`/`confidence` 既有字段,**零表结构改动**):

- `kind IN ('fact','preference')` 且 `guild_id = 当前服` 且 `confidence ≥ 0.8` → 本服公聊可注入(标注不可信数据,沿用现有包裹)
- 其他来源(`guild_id` 为其他服/DM)→ 仅私聊注入
- 注入数量上限:公聊 ≤3 条、私聊 ≤5 条(现有 retrieve 排序取前 N)

### 4.4 验证

- 命令注册测试更新;/帮助 输出断言
- 公聊注入规则单测:同服高置信可见、跨服/低置信仅私聊、上限生效
- `/忘记我` 集成测试更新(manual_memories 分支移除后仍全量清除)

**规模估计**:净 -200 行左右。

## 5. Phase 2 — 认知移植(3-4 天)

### 5.1 回复必要性闸门(替换概率内核)

移植 MaiBot 评分模型(`reply_necessity.py` 常量**硬编码为模块常量**,与 MaiBot 同做法——共同调参的权重暴露成单项配置没人会单独调,只暴露阈值):

| 分量 | 分值(常量) | mobo 数据来源 |
|---|---|---|
| @提及 bot | 100 | 现有 direct 判定 `discord_bot.py:1266` |
| 点名(名字出现) | 80 | bot_name 文本匹配 |
| DM / 会话连续窗口内 | 40 | 现有 conversation_window |
| 内容加分:疑问/请求/观点/长文 | 5-20 | 正则,复用 IntentService 风格 |
| 积压压力:等待消息数 | 对数增长 | BurstBuffer 队列长度 |
| 在场惩罚:近 5 分钟 bot 发言占比高 | -10~-40 | 频道历史查询 |

- `score ≥ gate_threshold`(默认 80,唯一新配置项)→ 触发回复
- **score < 阈值 → 原样落回现有概率路径**(`behavior.py:253-258` 不动),外层冷却/日限/静音/预算闸门原样保留。不做分数→概率映射公式,不迁移 proactive_base_probability 语义
- 接入点:ProactiveService.decide 内层,闸门作为前置判定
- **接线边界**:闸门只作用于**非 direct** 消息——@提及与 DM 在更早的 direct 判定(`discord_bot.py:1266`)已短路直达回复。评分表中 @提及 100 / DM 40 两行只为与 MaiBot 口径对齐;在 mobo 实装中,闸门真正起作用的是点名、内容加分、积压压力、在场惩罚四个分量,不得把 @提及再接进闸门造成双重触发

### 5.2 拟人化后处理

**顺序**(关键安全约束):模型输出 → 碎句拆分 → 错别字注入 → `safety.check_output` → 逐条发送。

1. **碎句拆分**:按标点/空格拆分、概率合并(移植 `split_strength` 机制);1980 硬拆保留为兜底;**替换 `_chunks` 的两个调用点**(`:1519` zip 路径与 `:1630`);代码块/URL/@提及不拆(mobo 新增的 Discord 适配)
2. **错别字注入**:精简版 ChineseTypoGenerator;跳过 URL、提及、表情、代码块、引号内;频率默认保守(约 2% 片段);**jieba 词典模块级加载一次**,缓存写入固定缓存目录(或禁用文件缓存)
3. **打字节奏**:每条碎片发送前 `sleep(f(长度, typing_speed) + jitter)`;typing 指示器贯穿;碎片数上限 4、总窗口 ≤12s
4. 配置:`humanization_enabled` 总开关 + `typo_rate`/`typing_speed`/`max_fragments`(管理台行为页,默认保守)

### 5.3 提示词新区块(内联,不重构)

在 `ContextBuilder.build()` 现有 f-string 内**直接加两个区块**,不拆分区块函数、不留占位:

- 群聊注意力引导句(引导模型参考近期频道摘要,复用现有 channel_summary 机制,只加引导文案)
- 闲置/活跃规则句(与闸门行为呼应:话多时收敛、被点名必应)

每服 `/人设` 覆盖机制不变。

### 5.4 轻量反应(不回复的应答)

群友结论清单明确项:不回消息时也会用表情应答。纯启发式、**零 LLM 调用**:

- 触发:闸门分数落在 `[reaction_min_score(40), gate_threshold)` 区间、非 bot 消息、非自己消息、该频道反应冷却已过 → 以 `reaction_probability` 概率点一个表情
- 表情集:`reaction_emoji_set` 配置(默认少量通用表情),随机选一
- 限流:每频道冷却(如 10 分钟)+ 全局日限;安静时段沿用主动发言的静音配置
- 与回复互斥:该消息一旦进入回复路径就不再反应
- 配置:`reaction_enabled`(默认开)、`reaction_probability`(默认保守 0.15)

### 5.5 验证

- 闸门单测:各分量独立打分、阈值触发、**阈值下落回概率路径**(桩测试验证旧路径未被改语义)
- 拟人化单测:拆分器对代码块/URL/提及免疫;错别字生成器同种子确定性;总开关关闭时字节级等价于现状;两个调用点都走新拆分器
- 顺序回归:输出安全检查在拟人化之后(构造会被错别字改写的敏感词用例)
- 节奏:碎片间隔服从上限;typing 生命周期
- 轻量反应:分数区间内触发、区间外不触发;冷却与日限;不反应 bot 与自身;与回复路径互斥
- 提示词:输出快照断言(防结构漂移)

**规模估计**:闸门 +200、拟人化 +550(含 jieba 缓存修复)、提示词 +40、轻量反应 +90;合计约 +880。

## 6. Phase 3 — 工具桥:内部 API 调用其他 bot(3-4 天)

落实硬需求。**只做 bot_bridge 一个工具**(web_read/web_search 等原生工具明确不做,需要时再加)。

### 6.1 模块与命名

新文件 `app/agent.py`(**不可叫 `app/tools/`,现有 `app/tools.py` 是 CLI 密钥生成器,名称冲突**)。包含:工具集定义 + 有界循环 + bridge 客户端。

- 工具集就是一个 `dict[str, (description, handler)]`——不做 ToolSpec/JSON-schema 校验层;模型端点自己强制它收到的 schema
- 每服开关挂到既有 guild policies 体系,默认关

### 6.2 bot_bridge

- 端点配置存 `app_settings`(JSON,鉴权头走既有 `is_secret` + `crypto.py` 加密):`[{名称, URL, method, 请求模板, 鉴权头, 响应字段路径, 超时}]`,1-3 个全局端点
- LLM 侧一个参数化工具 `bot_bridge(name, input)`;渲染请求、取响应字段、字符上限截断
- 输出按不可信数据包裹(复用现有注入标记);每次调用写**现有 `audit_log`**(action='tool_call',details 含端点/轮次/耗时/状态),享受既有保留期清理——不建新表
- 反方向(mobo 暴露 API)本期不做

### 6.3 有界 agent 循环

- 仅当模型输出 `tool_calls` 才进循环(纯聊天单次调用不变)
- ≤3 轮;token 计入现有预算;每用户冷却;总超时
- `app/llm.py` 透传 `tools` 参数;不做端点能力探测——首次调用失败即降级为无工具模式(test_model 已覆盖连通性)

### 6.4 验证

- bridge 单测:模板渲染(注入用例)、响应截断、鉴权头解密
- 循环单测:3 轮上限、无 tool_calls 不进循环、预算耗尽中断、不可信包裹
- 集成:桩 LLM 发起调用 → 桩 HTTP 端点 → audit_log 落库;每服开关关闭时工具不可见
- 安全:bridge 输出含"忽略之前指令" → 包裹标记存在(断言)

**规模估计**:约 +480。

## 7. Phase 4 — 记忆晋升调优(1 天,收尾)

**零表结构改动**(冷审结论:既有列已实现"捕获宽松、晋升严格"的生命周期——`memory.py:81` 重复内容即置信度强化,`expires_at` 即过期):

1. 公聊注入置信度门槛(已在 Phase 1 §4.3 规则中:confidence ≥ 0.8)确认为可配置项 `memory_public_confidence_floor`
2. 新提取事实的 `expires_at` 默认缩短(如 14 天),被强化(二次出现)后自动延展——调 auto_extract 落库参数 + 一个配置项
3. FTS5 + CJK 分词:不做,记为后续项

**验证**:过期与强化路径单测(现有 6h 维护任务消费 expires_at);`/忘记我` 覆盖不变(无新列)。

**规模估计**:约 +80。

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| GPL 合规(整体传染) | Phase 0 先行;NOTICE 双来源声明;用户已确认不商用 |
| 错别字/拆分误伤(URL、代码、@人) | 豁免清单硬编码 + 单测;总开关一键关闭 |
| 闸门阈值不合 mobo 社区体质 | 阈值进管理台;初期对照 proactive_log/audit 观察触发率;权重是常量,改代码即调 |
| 碎片连发触 Discord 限速 | 碎片 ≤4、窗口 ≤12s、打字间隔天然限速;超限回退单条 |
| jieba 词典反复重读拖慢发送路径 | 移植时强制模块级单次加载(冷审发现的源码缺陷) |
| 反应刷屏打扰 | 每频道冷却 + 概率 + 日限;不反应 bot 与自身;总开关 |
| bridge 端点不可用/慢 | 超时 + 失败降级无工具;audit_log 可观测 |
| utils 模型调用成本 | 全部走既有预算;Phase 4 调优纯本地无新 LLM 调用 |

## 9. 执行顺序与里程碑

Phase 0 → 1 → 2 → 3 → 4;每阶段独立可发布、测试绿、可回滚。Phase 2 与 3 可并行(互不依赖);Phase 4 收尾依赖 Phase 1 的注入规则。

**总规模估计:净 +1250 左右**(v1 估 +1830,冷审削减约 900 行过度设计;v3 补轻量反应 +90)。

## 10. 明确不做(非目标)

- A_Memorix / 向量库 / embedding / FTS5(本期)
- MaiBot webui、plugin_runtime、maim_message 协议、Maisaka runtime
- web_read / web_search 等额外原生工具(需要时单独加)
- 流式输出;代码执行类工具;多进程化;多平台网关
- 心情→文风强调制(现状:心情已注入提示词由模型自行体现,不加机制)
- 用户侧任何新命令

---

## 附:oracle 冷审修订记录(v1 → v2)

1. **基线纠错 ×3**:`_digest_after` 实为 `_learn_after_success`(:1659);模型三档 chat/deep/utility 已存在(llm.py:15)→ 删除整个 5.4 路由扩章节;`memories.status` 列已存在 → Phase 4 从"新增状态机"改为"既有列调优"
2. **删除**:模型路由扩展(-80)、web_read/web_search(-150)、ToolSpec schema 层(-60)、tool_bridges/tool_audit 新表×2(-200)、能力探测(-30)、Phase 4 状态机与第二提取路径(-160)
3. **收缩**:闸门权重保持常量只暴露阈值(-60);阈值下直接落回现有概率路径,不做映射公式(-25);提示词内联不重构(-70);manual_memories 整体删除而非只读保留(-60);typo 精简移植并修复 jieba 词典重读缺陷
4. **补漏**:拟人化须覆盖 `_chunks` 第二调用点(:1519);`app/tools.py` 名称冲突 → 新模块命名 `app/agent.py`

## 附:结论一致性修订(v2 → v3)

1. 修复交叉引用错误:§7 引用的"Phase 4.3"实为 Phase 1 §4.3
2. 补上群友结论清单遗漏的"给别人消息点表情":新增 §5.4 轻量反应(纯启发式零 LLM,与回复路径互斥;Phase 2 规模 +790 → +880)
3. §5.1 增加"接线边界"说明:@提及/DM 由 direct 路径短路,闸门不得重复接入,避免双重触发
4. §1.1 基线补表情反应现状行(仅入站反馈学习,出站缺失)
5. §10 明确"心情→文风强调制"不在本期(现状提示词注入已覆盖)
6. 总规模 +1150 → +1250
