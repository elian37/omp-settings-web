# omp 设置中心 (omp-settings-web)

**[English](README.md) | [中文](README_CN.md)**

## 截图

**桌面小窗**——贴在桌面层显示各供应商剩余额度（不遮挡其他应用）：

![桌面小窗](docs/widget.png)

**用量面板**——按供应商的额度卡，支持强刷和用券重置：

![用量面板](docs/zh/usage.png)

**模型角色**——每个角色附新手说明，两级选择切换模型：

![模型角色](docs/zh/roles.png)

**全部设置**——529 项按大类侧栏导航，逐项中文说明：

![全部设置](docs/zh/settings.png)

**模型显示**——勾选哪些模型出现在 `/model` 选择器：

![模型显示](docs/zh/models.png)

## 功能

**模型角色**

- 两级选择（供应商分组→组内模型），自动定位当前值，别名兜底。
- 每个角色下方显示详细新手向说明（该角色被哪些功能用、配什么模型、未配置时的回退行为），页顶有角色机制通用说明（@别名、*、逗号候选、自定义角色）。
- 窄窗口（≤700px）角色卡片自动分行：名称与切换按钮置顶，模型值、说明及编辑下拉框使用整行宽度，避免说明被横向挤成细长列。
- Ctrl+P 轮换顺序编辑器（`cycleOrder`）：上移、下移、移出，加入已有角色或输入自定义角色名；未绑定模型的角色可直接选择模型并写入 `modelRoles`。保存/取消/恢复默认，恢复默认会删除 `cycleOrder`，回到 omp 默认 `smol → default → slow`。拒绝空列表、重复名、`@别名`和非法字符。
- 故障兜底编辑器：按角色或现有模型/供应商链配置有序备用模型，支持添加、删除、上移、下移和取消。区分「未配置」（删除键，沿用 omp 默认规则）、「显式禁用」（`[]`）和「自定义链」。供应商选项只来自当前可见模型目录；已有隐藏或离线模型仅在所属条目中作为「当前值」保留，不产生空供应商分组。新增条目使用明确的 `provider/model-id`，不使用 `@角色`。
- 回退链条目和具体模型链键支持模型版本标签（如 `ollama-cloud/deepseek-v4.1-flash:1017`），也可在标签后追加思考档（如 `:1017:high`）；版本标签不会作为非法思考档被拒绝。
- 可编辑自动模型回退 `retry.modelFallback`、触发条件 `retry.fallbackOn` 和恢复策略 `retry.fallbackRevertPolicy`。具体模型键优先于供应商通配符，再到当前角色和 `default`；非聊天模型种类角色使用自己的链，不继承聊天 `default`。规则说明与页顶角色说明统一使用紧凑卡片排版。

**全部设置**

- 全量设置目录，含当前值/类型/描述。
- 全部 529 项均有面向初学者的详细中文说明（每个取值的效果、默认值、何时值得改、相关设置），上游英文原文悬停可见。
- 标量项可直接改，数组/嵌套块在本页只读展示；`retry.fallbackChains` 在「模型角色」页使用专用编辑器。
- 设置页按 ~35 个语义大类做侧栏导航（按项数排序），搜索时侧栏显示各组命中数、零命中置灰。
- 搜索 + 只看已自定义；徽章区分自定义项与默认值。

**模型显示**

- 按模型/按供应商勾选哪些模型出现在 omp 的 `/model` 选择器、Ctrl+P 轮换和本页角色下拉中；未勾选只是隐藏、不是禁用，已分配给角色的模型照常可用。写入 `enabledModels`（自动备份，omp 即时生效）。
- 「模型显示」里每个供应商分组的标题上都有「删除供应商」按钮——只要该供应商在本地 `models.yml`（或 `models.yaml`）的 `providers` 下有定义。定义了但 omp 目录里没有模型的供应商（空 provider）也会单列一个分组，否则没有入口能删掉它；只有 `modelOverrides` 的内置覆盖块不显示按钮，只标一个「内置覆盖块」标签。接口只回传名称、模型 id 以及将被清空的引用，绝不回传 `baseUrl`/`apiKey`/`headers`。
- 「删除供应商」删除该定义块（缩进更深的块内注释一并删除；写第一层缩进的注释属于下一个供应商，保留），并在同一事务里清掉指向它的引用——但**只动 omp 真正当作模型选择器的字段**：`modelRoles.*`、`retry.fallbackChains`（键与值都算）、`enabledModels`（块列表与 flow 写法）、`task.agentModelOverrides.*`、`modelPresets.*.modelRoles.*`、`sharpshooter.model`、`edit.modelVariants.*`。其余字段（路径、URL、`bash.patterns`、任意嵌套 flow 配置）逐字节保留，也不会因为它们而禁用删除。只改写**块写法**：`modelRoles`/`modelPresets.*.modelRoles`/`task.agentModelOverrides` 下的标量值（含逗号候选）、`retry.fallbackChains` 的块键与块列表项、`enabledModels` 的块列表项；这种值内部只摘掉属于该供应商的候选，角色或链被摘空就删除该键，`modelRoles`/`fallbackChains`/`agentModelOverrides`/`modelPresets` 收敛成 `{}`，`enabledModels` 收敛成 `[]`。任何 **flow 容器**（`{…}` / `[…]`）一律不改写：在模型相关顶层块（`modelRoles`、`retry`、`task`、`modelPresets`、`enabledModels`、`sharpshooter`、`edit`）内任意深度，只要某一行的值以 `{` 或 `[` 开头且其文本按词边界提到本供应商名或其声明的任一模型 ID，就返回 `unsupported`（保守文本判定：允许误拦、不允许漏拦），按钮禁用并在原因里给出位置与「请先改成块写法」的提示；都没提到的 flow 行原样保留、不影响删除。**多行** flow 容器按区域处理——区域只能由「值以 `{`/`[` 开头」的行启动（`key:` 之后或列表项 `-` 之后），因此普通字符串里的括号（`match: echo [`）与块标量正文（`description: |`）不会把后续行吞进区域；从打开 `{`/`[` 的行到闭合行（跳过引号与注释）整段视为一个 flow 值，区域内任何一行都不会被当成块写法改写；只要区域内任一行按词边界提到本供应商或其声明模型，就返回 `unsupported` 并在原因里给出容器起始路径。裸模型 ID 只有在 omp **完整**目录（`omp models --kind all`，image/tts/stt/judge/search 等非 chat 模型同样算提供者）里**唯一**由该供应商提供时才清除（omp 用 `pickPreferredModel()` 解析同名 ID），同名模型仍由别家提供时原样保留；需要判定归属但目录读不到（omp 缺失、退出码非零、输出无法解析或结果为空）时一律拒绝删除并返回 `unavailable`，按钮禁用，绝不猜归属、绝不写入。被改写的行保留行尾注释、缩进与行尾风格，只替换值所在区间。引用不再阻断删除；确认框逐条列出「位置 → 当前值 → 处理后值」，并在名单会被摘空时警示（空名单在 omp 里等于显示全部模型）。无法逐字节安全改写的选择器形态返回 `unsupported`，按钮禁用并给出具体位置。写入前先把两个文件各备份成 `<文件名>.bak-settings-web-<时间戳>`，任一写入失败按备份恢复，恢复也失败时如实返回 partial（HTTP 500）。请求同时携带 `models.yml` 与「生成确认清单时那份 config.yml」的 SHA-256，视图与磁盘不一致时以 stale 拒绝，不会静默清掉用户没看到过的引用。
- 每个模型旁提供「测试」：复用 omp 已登录凭据，通过 `omp bench` 对指定模型发一次真实短请求（请求关闭 thinking、输出上限 256 tokens、不走备用链），显示成功/失败、总耗时、首 token 与模型请求耗时，失败详情可展开。一次只测试一个，目录查询和请求合计限时 60 秒；不改配置或模型勾选，不自动重试。测试消耗少量额度，仅说明本次短文本请求的结果，不保证工具调用、图片或长上下文能力；结果只存当前页面，刷新清空。

**用量**

- 复刻 omp `/usage` 的可视面板：按供应商分卡的限额进度条（5 小时/7 天/周/月窗口）、已用与剩余量、重置倒计时实时跳动、账号/套餐信息与重置额度券；共享窗口自动去重。手动刷新 + 单供应商/全局「强刷」（`omp usage invalidate`，白名单校验）+ 可选 60 秒自动轮询，数据为空或拉取失败时给出提示而非白屏。anthropic / openai-codex 卡片支持「用券重置」——确认后经 omp `/usage reset` 同款上游协议消耗一张额度券（服务端复刻 `resets.ts`；OAuth token 从 `~/.omp/agent/agent.db` 读取，永不出进程）。

**账号**

- 按供应商列出 omp 已登录的 OAuth 账号（身份取自 `omp usage accounts`，不含 token；剩余额度取自 `omp usage`；可注销的条目来自 omp RPC `get_logout_accounts`）。OAuth 登录与 API Key 凭据都会列出并可注销；omp 没给身份标识的登录（如 devin）无法设策略，但仍可在该行注销。每个账号可设**优先级**、**保留额度%** 与**切回阈值%**（`reclaimAbovePct`，omp ≥18.12.4：会话被降级到低优先级账号后，该账号剩余额度恢复到该百分比以上时自动切回，必须大于保留额度），写入 omp 原生的 `config.yml` `auth.accountPolicies`（自动备份、只整段重写该块，块外逐字节不变），三项都留空即清除。同页可改全局默认 `retry.usageReservePct`。匹配不到任何已登录账号的策略单独列出并可删除。优先级只在健康、额度可测的账号之间排序，只影响新会话，不会越过限流与保留额度保护。新增账号请在 omp 中执行 `/login`。策略块含注释或 flow 写法时只读展示。
- 每行有「**注销登录**」按钮。行内与确认框都显示 omp 自己给出的 label、detail 与 `#credentialId`，因此同邮箱不同组织的两条登录可以区分。
  确认后调用 omp 自己的 `logout` RPC（等价 TUI 的 `/logout`）：按该 ID 删除一条已存凭据（OAuth 或 API Key），
  不写 SQLite、不取消供应商那边的付费订阅、不动其他账号，也不改 `modelRoles`/`models`/选号策略。删除前服务端会核对：
  该 ID 仍在 omp 的凭据列表中、本地凭据库对 provider 的说法一致、身份规范化后**完全相等**（字段出现或消失都算变更，
  所以旧页面不能删掉一条已被 omp 升级为组织作用域身份的登录）；归属无法核验时一律拒绝而不是猜。失败保留条目并报错，
  成功后重新拉取列表，且只回报「是否还有其他认证源」。当 omp 把凭据托管在 auth-broker（删除会作用到共享远程库）
  或无法确定 agent 目录时，删除会被关闭并给出说明。
- 每个账号有「**停用（仅兜底）**/启用」按钮。停用**不是完全停用**：它把该账号的保留额度写成 100%（omp 原生配置里没有停用字段），平时不会被选中，已钉在它上面的会话也会在下次请求时换到其他账号；只有其他账号都被限流、额度用完或进入保留额度时，才会拿它兜底。启用会清除该账号的保留额度（回到全局默认，原先自定义的保留额度不保留），优先级不变。

**桌面小窗** (`widget.py`)

- 贴桌面层的无边框暗色小窗（始终在所有应用窗口之下），直接读 `omp usage --json` 展示各供应商额度窗口（按剩余量着色的进度条、剩余百分比、重置倒计时、额度券角标）；独立运行不依赖网页服务，60 秒自动刷新，任意处拖动且记住位置（始终完整限制在所在显示器的工作区内，切换分辨率/显示器/任务栏后自动拉回），右键可刷新/强制刷新/打开网页面板/退出；防双开、Win11 圆角、高分屏清晰、调用 omp 不弹控制台。
- 供应商顺序可自定义：右键某个供应商标题 →「Move up / Move down / Reset order」（上移/下移/恢复默认）。顺序按机器保存在 `widget-state.json` 的 `"order": ["openai-codex", "google-antigravity", ...]`（也可在小窗关闭时手改）；列出的供应商按此顺序置前，未列出的保持 omp 原顺序排在后面。
- 在网页面板里「停用（仅兜底）」的账号（策略 `reservePct: 100`，每次刷新经 `omp config get auth.accountPolicies` 读取），标题变灰并带「停用·兜底」标签；它仍可能被 omp 用于兜底，所以额度照常显示。供应商名旁显示账号邮箱；没有数值剩余额度的行和供应商不显示。
- 额度列表本身是 `widget.py` 里的 `QuotaView`（取数 + 绘制，可嵌入任意 Tk 容器），独立小窗只是它外面的窗口外壳。[desktop-widgets](https://github.com/libanxian003/desktop-widgets) 组件包的 Model Quota 页直接加载本文件复用它，额度相关改动只需改这里。

## 依赖

- Python 3.8+（纯标准库，零第三方依赖）
- [omp](https://github.com/can1357/oh-my-pi) 已安装并在 `PATH` 上（模型目录使用 `omp models --json`；连通性测试需要支持 `omp bench --json` 的版本）
- 桌面小窗（`widget.py`）**仅支持 Windows**：它靠 Win32 调用把窗口固定在桌面层、启用 Win11 圆角并按显示器做 DPI 适配，这些接口其他系统都没有。网页面板（`server.py`）是纯 Python，任何系统都能跑。

## 使用

```bash
python server.py            # 启动 http://127.0.0.1:8788 并自动打开浏览器
python server.py --no-browser
```

桌面小窗（Windows）：

```bash
pythonw widget.py           # 无控制台窗口；右键小窗调出菜单
```

隔离档位 / 端口（对着一次性的 omp 配置档做测试时用）：

```bash
set OMP_PROFILE=my-test&& set OMP_SETTINGS_PORT=8799&& python server.py --no-browser
```

本页不注入 `--profile`：所有 `omp` 调用都继承你的环境，由 omp 自己解析档位
（`--profile` > `OMP_PROFILE` > `PI_PROFILE`，空值或 `default` 都表示默认档）。
凭据与配置目录随后取自 `omp config path`，因此测试不会碰到你真实的凭据和配置。
两个变量默认都不设（即 `~/.omp/agent`，端口 8788）。

打开页面 → 角色行点「切换」→ 选分组 → 选模型 → 保存。配置写入后 omp **即时生效**，运行中的会话无需重启。

## 安全设计

`omp config set` 会重写整个文件并抹掉你的注释，本工具从不调用它。而是：

1. **精准行编辑**——读取 `~/.omp/agent/config.yml`，只正则替换目标键的那一行；尚未存在的键按正确嵌套层级插入。
2. **字节级 IO**——`read_bytes`/`write_bytes` 并检测原文件行尾（LF vs CRLF），除被编辑/插入的行外其他内容逐字节不变。
3. **自动备份**——每次写入前先把配置复制为 `config.yml.bak-settings-web-<时间戳>`。
4. **类型校验**——按 `omp config list` 上报的类型做值转换（boolean/number/string/enum）；兜底链专用接口校验键名和模型选择器，支持块映射/列表及单行映射，不支持的复杂 YAML 拒绝写入。

服务只绑定 `127.0.0.1`。所有写入前的值/角色输入都先过白名单正则。

注销账号从不改 `agent.db`：面板只从 `auth_credentials` 读 `provider` 与 `identity_key` 两列做身份交叉核对
（从不读 `data`），真正的删除由 omp 自己的 `logout` RPC 完成，它同时会刷新模型注册表与会话固定。每个 RPC 子进程
调用完必被清理（限时 25 秒）且 stderr 直接丢弃，凭据文本不会进入任何日志。

破坏性接口 `/api/accounts/logout` 在读请求体之前就拒绝跨站 `Origin`/`Referer`，因此浏览器里打开的其他网站
无法借 localhost 权限删掉你的凭据。

> ⚠️ 值得一提的坑（也是 #2 存在的原因）：Windows 上 Python 的 `Path.write_text()` 会静默把 LF 翻译成 CRLF，整个配置文件的行尾都会被改写。

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/state` | GET | 当前角色（从 config.yml 解析）+ 全量模型目录（`omp models --json`） |
| `/api/models/test` | POST | `{"selector":"provider/model-id"}` — 单次真实请求，返回 `{ok, selector, code, elapsedMs, model?, durationMs?, ttftMs?, error?}`；仅接受当前目录的精确模型项，400 表示非法模型、409 表示已有测试，其他测试结果返回 200；错误脱敏 |
| `/api/set` | POST | `{"role": "...", "value": "..."}` — 备份 + 精准写入 |
| `/api/settings` | GET | 全量设置目录（`omp config list --json`）+ 中文注释 + `__groupmap` |
| `/api/set-key` | POST | `{"key": "...", "value": "..."}` — 类型化标量写入，备份 + 精准行编辑 |
| `/api/fallback` | GET | 读取兜底链及 `modelFallback` / `fallbackOn` / `fallbackRevertPolicy` |
| `/api/fallback` | POST | `{"key":"default","chain":["provider/model"]}`；`chain: []` 禁用，`chain: null` 删除配置，仅修改该链并自动备份 |
| `/api/cycle-order` | GET | Ctrl+P 轮换顺序 `{order, custom, default}`；未配置时返回 omp 默认顺序 |
| `/api/cycle-order` | POST | `{"order":["smol","default","e"]}` 写入；`{"order": null}` 删除配置恢复默认；自动备份 |
| `/api/usage` | GET | `omp usage --json` 透传 — 按供应商的额度/用量报告 |
| `/api/usage/invalidate` | POST | `{"provider": "all"|<provider id>}` — 白名单校验的 `omp usage invalidate`，强制重抓 |
| `/api/usage/redeem` | POST | `{"provider": "anthropic"\|"openai-codex"}` — 消耗一张额度券（anthropic 两步 request_id 确认、openai-codex 单步幂等），结构化 `{ok, code, error}` 响应，token 永不外泄 |
| `/api/accounts` | GET | 按供应商的账号（身份、额度、命中的策略）、未匹配的策略、默认保留额度 |
| `/api/accounts/policy` | POST | `{"provider", "identityKey", "priority", "reservePct", "reclaimAbovePct"}` — 为已登录 OAuth 账号新增/更新/清除策略；校验 + 备份 |
| `/api/accounts/policy-delete` | POST | `{"index", "provider", "account"}` — 删除一条策略；条目已变化则拒绝 |
| `/api/accounts/logout` | POST | `{"provider", "credentialId", "identityKey"?}` — 按 omp 的 `credentialId` 删除一条已存凭据（OAuth 或 API Key，软删，不动订阅）。要求同源 `Host` 且 `Origin`/`Referer` 一致；凭据已消失或已变化返回 409，身份不完全相等返回 409，归属无法核验或 omp 使用 auth-broker 返回 503/409，RPC 不可用返回 503 |

## License

[MIT](LICENSE) — 与 omp 项目无关。
