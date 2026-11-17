# Pulsara 网页工具合同与使用教程草案

共用规则见[原生工具索引](../PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 2 章；完整保存、原生 12k／NEXT、JS 完整返回与 Cell 输出沿[统一交付草案](../PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)，存储沿[数据库 hardcut 草案](../PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md)。

状态：本文件是新增能力的文档合同，尚未接入生产代码。当前确定 web_search、web_fetch 的参数、返回值和原生／JS 用法，以及模型按需交错搜索与阅读的使用方式。默认使用 Firecrawl → Tavily 无 Key 免费池；可选配置四家之一及自有 API Key，账户计费与静默回退见第 4 章。

## 1. web_search：搜索网页来源与相关摘要

### 1.1 用途与调用边界

按查询词搜索网页，返回有序的来源地址、标题和相关摘要。搜索结果用于选择来源，不表示已经读取页面正文。摘要可能是服务生成的摘要或页面摘录，不承诺可直接作为逐字引文。

web_search 是固定的 Pulsara 内置接口，拟支持 ROOT／Subagent 原生调用，以及 ROOT Code Mode 的 tools.web_search。四家后端为 DeepSeek、Firecrawl、Tavily、Exa；免费池与可选自有 Key 由用户级服务管理，模型每次使用相同参数和返回类型，无须先发现 MCP 工具或取得调用引用。web_search、web_fetch 均为外部只读操作，可在只读权限与 Plan 中使用，继续经过既有工具准入与权限判断，不因供应商切换另设审批；Subagent 不提供 JS API。

适配层负责请求映射、结果归一化和实际错误说明；搜索引擎负责检索、排名及摘要生成。网络、认证与请求生命周期复用适合的维护中 SDK 或现有 HTTP 客户端，工具准入、取消和结果结算继续使用 Pulsara 的共同 owner。一次逻辑调用接纳时固定参数、供应商／凭据选择与候选顺序；后端配置变化影响后续调用，不改写既有 epoch 的 SYSTEM、provider tools 或已安装消息前缀。

### 1.2 参数

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| query | string | 必填，不能全为空白 | 本次搜索词或自然语言查询 |
| count | integer | 可选，默认 10；1–20 | 本次最多请求的结果数，是搜索业务范围，不是展示页长 |
| include_domains | string[] | 可选，省略或空数组表示不限制 | 只返回列表中域名范围内的结果；多个域名之间是“或” |
| exclude_domains | string[] | 可选，省略或空数组表示不排除 | 排除列表中域名范围内的结果；与包含条件同时填写时，排除优先 |
| after | string | 可选，YYYY-MM-DD | 搜索指定日期之后的网页 |
| before | string | 可选，YYYY-MM-DD | 搜索指定日期之前的网页 |

只接受上述六个字段，不接受 provider、API key、原始请求选项、cursor 或展示预算。可选字段不接受 null；域名数组中的每项须为非空 hostname。count 的共同上限 20 来自 Tavily 的实际单次接口上限，不限制累计搜索或会话寿命。实际返回少于 count 可以正常成功。

域名不带协议、端口、路径或通配符。例如 deepseek.com 匹配自身及其子域名，api-docs.deepseek.com 也匹配其自身及子域名。按 URL 解析出的 hostname 做不区分大小写的域名匹配，不用任意字符串包含判断。

after／before 使用合法日历日期，可以只填写一侧；同时填写时 after 必须早于 before。它们是搜索引擎的日期检索条件，由引擎判断网页时间，不代表 Pulsara 验证了每页的真实发布日期，也不表示缓存新鲜度。Pulsara 不额外以记录缺少日期或日期表面不符为由判定整次搜索失败，不增加严格日期开关或证明字段。

query 保留调用者原文；适配层只为表达明确填写的筛选条件使用后端正式参数或搜索语法，不另行扩写查询。高级查询语法按实际服务解释，不将某一家语法当作跨服务标准。DeepSeek 组装后的完整查询最多 128 个 Unicode 码点，包含空格、运算符和筛选条件，不按 UTF-16 代码单元计数；Firecrawl 查询最多 500 字符。超出某后端真实限制时，该候选不执行并记录原因，可按第 4 章尝试其他候选；不裁剪查询、删掉筛选条件或拆成多个查询。域名数组等其他输入同样按服务真实边界处理，不另设任意总量限制。

```typescript
interface WebSearchArgs {
  query: string;
  count?: number;                 // 整数，1–20，默认 10
  include_domains?: string[];
  exclude_domains?: string[];
  after?: string;                 // YYYY-MM-DD
  before?: string;                // YYYY-MM-DD
}
```

### 1.3 四家后端映射与用户配置

| 后端 | 查询与数量 | 域名条件 | 日期条件 | 搜索预览 |
| --- | --- | --- | --- | --- |
| DeepSeek | query、count | 在查询中生成 site:、OR、-site: 条件 | after:、before: | 优先使用非空 summary，否则使用 snippet；need_content:false |
| Firecrawl | query、limit；来源固定为 web | includeDomains／excludeDomains | 转成 tbs 对应日期范围，仅用于 web 搜索 | 使用 highlights 对应的相关描述，缺失时保留实际描述；不附 scrapeOptions |
| Tavily | query、max_results；默认 general／basic | include_domains／exclude_domains，包含模式使用 restrict | start_date／end_date | 使用 content；include_published_date:true；不请求生成答案或附带全文 |
| Exa | query、numResults；默认 auto | includeDomains／excludeDomains | startPublishedDate／endPublishedDate | 请求 contents.highlights，不请求页面全文或合成答案 |

域名条件除交给搜索引擎外，适配层还须核对返回 URL，保证交付记录满足上述包含／排除规则。Firecrawl 的两组域名参数互斥：同时填写时向服务下发包含列表，再按排除列表过滤返回记录；只填排除列表时使用原生排除参数。过滤保留原相对顺序，结果可能少于 count，不为凑满数量自动补搜。

日期使用各家正式支持的条件；不把发布日期窗口换成缓存年龄，也不以向 query 加一句自然语言要求冒充后端筛选。DeepSeek 使用原日期字符串；Tavily 使用日期字段；Exa 的日期时间字段由输入日期转换为 UTC 当日 00:00:00，不受运行机器时区影响。Firecrawl 的双边范围使用 tbs 的 cdr:1、cd_min、cd_max，日期转为 MM/DD/YYYY；单边日期映射须通过正式接口验证，未确认能表达时跳过该候选，不补造另一侧日期。各家边界包含方式、日期估计与未知日期处理可能不同，不承诺逐页时间完全等价，也不新增页面逐一抓取、日期验证或额外检索流程。

查询中 OR 等语法的优先级仍由服务决定；返回 URL 的域名核对保证交付范围，不保证各引擎召回完全相同。每个实际执行的候选都必须保留本次全部筛选条件；某候选无法表达时按第 4 章跳过，不能为了回退而放宽条件。

模型参数之外的分工为：

| 内容 | 归属 |
| --- | --- |
| 默认免费池、可选供应商与凭据，以及可复用的已保存连接 | 第 4 章的最简用户级配置；不随当前聊天模型切换，不进入工具参数或返回值 |
| 国家／语言、安全过滤、搜索深度等非公开选项 | 使用本节明确的适配默认，未明确覆盖的沿供应商默认；不要求用户填写额外高级字段 |
| 摘要及相关片段的取得、来源固定为网页 | 适配层策略，模型不填写各家的输出控制参数 |
| 搜索附带全文、生成综合答案、图片／图标、工具发现 | 本工具不请求；网页正文不混入搜索预览 |

DeepSeek 的 ori_question、Firecrawl 的 domainTools／toolDetail、Tavily 的 auto_parameters、Exa 的 additionalQueries／systemPrompt／outputSchema 等不进入统一模型参数。Tavily 在适配层请求 include_published_date:true，保留 filter_by_published_date:false，不为取得日期而剔除日期未知的来源；实际返回的页面日期有则保留，模型不填写日期输出开关。

公开 API 依据（核对日期：2026-11-17）：

- DeepSeek：[搜索请求与结果](https://api-docs.deepseek.com/zh-cn/api/search/)、[搜索语法与抓取指南](https://api-docs.deepseek.com/zh-cn/guides/search/)。
- Firecrawl：[Search API](https://docs.firecrawl.dev/api-reference/endpoint/search)，包括域名参数互斥和 tbs 日期条件。
- Tavily：[Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)，包括数量上限、域名限制模式、日期估计及缓存新鲜度的区别。
- Exa：[Search API](https://exa.ai/docs/reference/search)，包括正式域名参数、发布日期和 highlights。

### 1.4 返回值

业务顶层只保留 meta 和 content；meta 只含 status。每个命中占一个 object 内容项，其内部保留：

| 字段 | 类型 | 明确含义 |
| --- | --- | --- |
| target | URL Target，必有 | {kind:"URL",url:string}，实际搜索来源的完整 HTTP(S) 地址，不含用户名或密码 |
| title | string，可选 | 服务实际提供的非空标题 |
| snippet | string，可选 | 服务提供的相关摘要或摘录，不作为已读取全文或逐字引用的证明 |
| date | string，可选 | 服务报告的网页日期，可能为日期或日期时间，也可能是估计的发布或更新时间 |

title、snippet、date 未知、缺失、null 或为空时省略，不填假标题、假日期或未知占位。date 不统一命名为 published_at；不把抓取时间、请求时间补成网页日期，也不猜测未提供的时区。多个相关片段按原顺序用明确分隔拼成 snippet，不让不相邻摘录看起来连续；不由 Pulsara 另调模型改写摘要。

按服务返回的排名交付通过域名核对的记录，不用分数重新排序。URL 只表示该来源，不推测未提供的最终重定向地址。标题和摘要中的 URL 或图片链接保持文字，不自动成为额外 Target、图片资产或后续读取。

不返回顶层 target、results 数组、查询副本、供应商信息、评分、计费、请求 ID、总量／返回量统计或额外 structuredContent，不另建网页结果 ID、引用登记或持久搜索目录。来源 URL Target 可直接交给第 2 章的 web_fetch，也可用于其他已支持 URL 的操作。

```typescript
type WebUrlTarget = {kind: "URL"; url: string};
type WebSearchTextPart = {type: "text"; value: string};

interface WebSearchRecord {
  target: WebUrlTarget;
  title?: string;
  snippet?: string;
  date?: string;
}

type WebSearchObjectPart = {type: "object"; value: WebSearchRecord};
type WebSearchPart = WebSearchTextPart | WebSearchObjectPart;

interface WebSearchSuccess {
  meta: {status: "SUCCESS"};
  content: WebSearchPart[];
}

interface WebSearchFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: WebSearchPart[];
}

type WebSearchResult = WebSearchSuccess | WebSearchFailure;

interface WebSearchToolError extends Error {
  result?: WebSearchFailure;
}

declare function web_search(args: WebSearchArgs): Promise<WebSearchSuccess>;
```

WebSearchResult 描述保存的完整业务值；JS 正常返回只使用 WebSearchSuccess。失败或取消沿异常通道提供实际存在的完整 err.result，没有业务值时不伪造 result。正常命中使用 object；text 只承载有用的实际说明或诊断。

### 1.5 状态、完整保存与冻结分页

| 情况 | 状态与内容 |
| --- | --- |
| 成功取得结果，或实际命中少于 count | SUCCESS，按实际记录返回 |
| 没有匹配结果，或域名核对后无结果 | SUCCESS，content:[] |
| 按第 4 章尝试后仍未完成请求 | ERROR，text 写清实际配置、认证、额度、服务、请求或响应原因；不能伪装成搜索无匹配 |
| 最终只能接纳一部分结果 | ERROR，保留实际可用 object，并说明未完成部分和原因 |
| 真实取消 | CANCELLED，保留已经确定的事实；不以取消请求到达改写已经确定的成功或错误结果 |

日期未知、缺少可选标题或摘要、数量未满，本身不表示调用失败。域名不符的来源不交付为命中，也不因此把正常过滤包装成部分失败。真实响应错误、记录无法可靠接纳等候选失败沿第 4 章处理；最终失败时保留按该章选定尝试中已取得的可用内容，并说明未完成部分和原因。

完整归一化业务值保存并接纳后，才交付原生第一页或返回完整 JS 值；不再保存第二份手工正文或整份供应商原始 JSON 备份。原生模型消息另含实际 observed_at_utc、observation_duration_microseconds、tool_result 和 next。JS 不带这些公共交付字段，也不套用原生页预算。

原生使用共同冻结 12k／NEXT；NEXT 只续读同一已保存结果，不重新搜索、刷新结果或追加获取更多命中。tool_result 指向原完整结果，next 指向其未交付后缀；object 可按共同投影跨页。JS 的 console.log／显式 return 只交付实际输出的值，不把内存中尚未输出的数据当作模型已看过。

### 1.6 原生示例

以下调用及结果为模拟，不是实际付费请求。时间和结果身份使用说明占位，不伪造真实观测。

```text
工具：web_search
参数：
{
  "query": "DeepSeek 搜索 API",
  "count": 2,
  "include_domains": ["deepseek.com"],
  "exclude_domains": ["status.deepseek.com"],
  "after": "2026-01-01",
  "before": "2027-01-01"
}

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "URL",
      "url": "https://api-docs.deepseek.com/zh-cn/guides/search/"
    },
    "title": "网页搜索与抓取",
    "snippet": "介绍独立搜索和网页读取接口，以及请求参数和返回内容。"
  }

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>
tool_result: {"kind":"SESSION_ENTRY","entry_id":"entry:<本次完整结果ID>"}
next: null
```

示例只取得一条来源，仍为 SUCCESS；没有可用网页日期时省略 date。原生正文是 object 的文字投影，不把保存值的 type/value 包装重复展示给模型。

假设自有 Key 的服务返回余额不足，两家免费候选也均受限，最终失败业务值的原生正文为：

```text
meta:
  status: ERROR

content:
  自有 Key 的搜索服务余额不足，Firecrawl 和 Tavily 的免费请求也均达到服务限制。本次未取得搜索结果。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>
tool_result: <本次完整失败结果的 SESSION_ENTRY Target>
next: null
```

### 1.7 JS 示例

与原生调用使用相同的参数与筛选规则：

```javascript
const result = await tools.web_search({
  query: "DeepSeek 搜索 API",
  count: 2,
  include_domains: ["deepseek.com"],
  exclude_domains: ["status.deepseek.com"],
  after: "2026-01-01",
  before: "2027-01-01"
});
console.log(result);
```

对应的完整业务值为：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {
      "type": "object",
      "value": {
        "target": {
          "kind": "URL",
          "url": "https://api-docs.deepseek.com/zh-cn/guides/search/"
        },
        "title": "网页搜索与抓取",
        "snippet": "介绍独立搜索和网页读取接口，以及请求参数和返回内容。"
      }
    }
  ]
}
```

脚本可以直接组合已知的结构化记录；失败时输出实际存在的失败值：

```javascript
try {
  const result = await tools.web_search({query: "DeepSeek 搜索 API", count: 5});
  const sources = result.content
    .filter(part => part.type === "object")
    .map(part => part.value);
  console.log(sources);
} catch (err) {
  console.log(err.result !== undefined ? err.result : String(err.message ?? err));
}
```

## 2. web_fetch：读取指定网页的提取正文

### 2.1 参数与调用边界

读取一个指定 HTTP(S) 地址，返回服务实际提取的 Markdown／Markdown 风格正文。与 web_search 复用用户级供应商／凭据管理和共同执行边界，拟支持 ROOT／Subagent 原生调用，以及 ROOT Code Mode 的 tools.web_fetch。可直接使用搜索结果中的 URL Target，也可读取用户或模型已经知道的链接。

| 参数 | 类型 | 必填与含义 |
| --- | --- | --- |
| target | URL Target | 必填，{kind:"URL",url:string}，本次请求读取的完整 HTTP(S) 地址，不含用户名或密码 |

外层只接受 target；target 只接受 kind、url。地址按现有 URL Target 规则校验，供应商的实际地址长度等边界如实处理，不裁剪地址。一次读取一个目标；多个目标可组合独立调用，各自保存和结算。

正文格式固定，模型不填写 provider、凭据、query、limit、format、fresh、headers 或页面操作。query 相关提取会改变正文范围，长度展示交给共同分页；缓存、提取深度等使用本节明确的适配默认，其余沿供应商默认，不新增必填高级配置，也不将不同服务的刷新行为合并成无法兑现的实时保证。只读／Plan／Subagent 边界与第 1.1 节相同，免费池及回退沿第 4 章。

```typescript
interface WebFetchArgs {
  target: WebUrlTarget;
}
```

### 2.2 后端映射与正文范围

| 后端 | 最小请求映射 | 取得正文与标题 |
| --- | --- | --- |
| DeepSeek | {url} | content 为 Markdown，非空 title 作为标题 |
| Firecrawl | {url,formats:["markdown"],onlyMainContent:true} | data.markdown，非空 metadata.title；分别检查 API 与目标页的实际状态 |
| Tavily | {urls:[url],format:"markdown"}，省略 query | 对应结果的 raw_content；检查 failed_results；无独立标题时省略 |
| Exa | {urls:[url],text:{linkStyle:"full"}} | 对应结果的 text、非空 title；检查该输入对应的 statuses |

只请求正文所需的输出，不附加综合答案、截图、子页面抓取或交互动作。Tavily 省略 query，避免按问题重排和限制相关片段；Exa 请求 linkStyle:"full"，保留正文提取到的 Markdown 链接。服务给出的正文顺序和文字保持原样，不另调模型概括、重排或人为增加标题、行号。

各服务的提取范围与缓存可能不同。Firecrawl 默认提取主要内容并可使用两天内缓存；Exa 的 text 默认使用 compact，/contents 的 maxCharacters 默认及上限为 5,000,000 字符，另有缓存及抓取回退行为；DeepSeek 和 Tavily 此入口未提供可统一兑现的强制刷新控制。再次显式调用 web_fetch 是新的读取请求，但也可能取得供应商缓存，不因调用次数增加就宣称内容已刷新。

Firecrawl 的 storeInCache 沿其默认 true，服务可能将结果写入自身缓存及索引；这与读取已有缓存是不同事实。本稿不增加缓存设置开关，也不作零保留或实时抓取承诺。

Tavily 即使返回 HTTP 200，也须检查 failed_results；只有失败项且没有成功结果时为提取失败。Exa 使用输入对应的 statuses 判断是否成功：statuses[].id 关联请求 URL／输入 ID，results[].id 可以是服务生成的文档 ID，不要求两个 ID 或返回 URL 与原请求逐字相同。关联输入及检查状态不改变第 2.3 节的原请求 target。

“完整保存”指保存本次实际取得并接纳的提取结果，不保证服务覆盖网页全部原始内容。服务没有报告的完整性、截断、缓存命中或最终重定向事实保持未知；既不编造完整标记，也不因为未知就自动失败。供应商的真实提取上限与 Pulsara 的结果保存和展示分页分别处理。

依据：[DeepSeek Fetch](https://api-docs.deepseek.com/zh-cn/api/fetch/)、[Firecrawl Scrape](https://docs.firecrawl.dev/api-reference/endpoint/scrape)及[缓存说明](https://docs.firecrawl.dev/features/scrape)、[Tavily Extract](https://docs.tavily.com/documentation/api-reference/endpoint/extract)、[Exa Contents](https://exa.ai/docs/reference/get-contents)及[OpenAPI 的 ContentsRequest](https://exa.ai/docs/exa-spec.yaml)。

### 2.3 精简返回值与类型

| 字段 | 明确含义 |
| --- | --- |
| target | 本次请求的 URL Target；表示读取目标，不冒称已确认的最终重定向地址 |
| meta.status | SUCCESS／ERROR／CANCELLED，沿实际调用结算投影 |
| meta.title | 成功时可选，服务实际返回的非空标题；缺失、null 或空字符串时省略 |
| content | 有序 text 内容项；成功时只承载提取正文，失败时保留实际已取得内容及具体原因 |

标题放在 meta.title，便于 JS 分别使用标题和正文。服务没有独立标题时不从搜索结果或正文猜造；页面正文原有标题仍属于正文，不为避免文字重复而删除。明确取得成功空正文时使用 content:[]；服务报告提取失败、缺少对应结果或缺失请求的正文字段时，不能伪装为空页成功。

Markdown 中的链接和图片链接保持文字，不自动读取、递归抓取或转换成实际图片。顶层目标是原请求地址；服务返回普通 url／sourceURL 不足以证明重定向后的最终地址，不据此改写 target。

```typescript
type WebFetchTextPart = {type: "text"; value: string};

interface WebFetchSuccess {
  target: WebUrlTarget;
  meta: {status: "SUCCESS"; title?: string};
  content: WebFetchTextPart[];
}

interface WebFetchFailure {
  target?: WebUrlTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: WebFetchTextPart[];
}

type WebFetchResult = WebFetchSuccess | WebFetchFailure;

interface WebFetchToolError extends Error {
  result?: WebFetchFailure;
}

declare function web_fetch(args: WebFetchArgs): Promise<WebFetchSuccess>;
```

WebFetchResult 描述保存值；JS 正常返回只使用 WebFetchSuccess。失败或取消走异常通道，实际存在的完整失败值通过 err.result 提供。失败 target 仅在本次目标可合法表达时保留，表示尝试读取的地址，不证明读取成功；没有失败业务值时不伪造 err.result。失败时已知且有用的标题等事实写入文字说明，meta 只保留 status。

### 2.4 状态、保存与 NEXT

成功取得提取正文为 SUCCESS。配置、认证、服务拒绝、目标页访问或正文提取失败，在第 4 章的候选尝试后仍未完成时为 ERROR；说明实际原因，不能把供应商的所有 404 统一解释为原网站不存在。Firecrawl 的 API 成功不自动等于目标页成功；Tavily／Exa 的单目标读取也须核对对应失败记录或状态。

只有明确知道正文被截断、提取中断或本次仅完成一部分，且后续候选也未成功时，才按实际 ERROR 保留已取得正文，并用 text 说明未完成部分和原因。正常主内容提取、已知存在供应商长度边界或可能使用缓存，本身不作为部分失败。供应商的 warning 不一律等于正文不完整；与实际提取失败无关的诊断不混入成功正文。实际取消沿 CANCELLED 结算，保留已知事实；后来到达的取消不覆盖已确定的 SUCCESS／ERROR。

完整业务值保存、接纳后，才交付原生第一页或返回完整 JS 值。target 和可选标题沿数据库草案的已声明 public_fields 保存，正文使用有序 content，status 从现有结算事实投影；同一份结果承接历史、续读和导出。

原生沿共同冻结 12k／NEXT。tool_result 指向本次原完整结果，next 指向该结果的未交付后缀。NEXT 不重新抓取、刷新缓存、重新提取、跟随链接，也不能补取供应商没有返回的网页内容；保存或交付失败不借补页重做读取。

JS 一次取得完整业务值，不带原生观察时间、tool_result 或 next；正文不因原生页预算而截短。Cell 只将实际 console.log／显式 return 的内容交给模型。模型后续需要理解正文再决定行动时，必须先实际收到相应内容，不能把脚本内存中已有值当作模型已读。

### 2.5 原生示例

以下为模拟值，不是实际付费请求；正文完整值在本例中可用一页交付。

```text
工具：web_fetch
参数：
{
  "target": {
    "kind": "URL",
    "url": "https://api-docs.deepseek.com/zh-cn/guides/search/"
  }
}

target:
  {"kind":"URL","url":"https://api-docs.deepseek.com/zh-cn/guides/search/"}

meta:
  status: SUCCESS
  title: 网页搜索与抓取

content:
  # 网页搜索与抓取

  Search 用于检索网页来源，Fetch 用于读取指定页面。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>
tool_result: {"kind":"SESSION_ENTRY","entry_id":"entry:<本次完整读取结果ID>"}
next: null
```

长正文仍先完整保存，第一页可返回非空 NEXT；后续原生 read_file(target:<NEXT>) 按既有文件工具合同续读同一份正文，实际返回新的 next，末页为 null。

### 2.6 JS 示例

```javascript
const page = await tools.web_fetch({
  target: {
    kind: "URL",
    url: "https://api-docs.deepseek.com/zh-cn/guides/search/"
  }
});
console.log(page);
```

对应的完整业务值为：

```json
{
  "target": {
    "kind": "URL",
    "url": "https://api-docs.deepseek.com/zh-cn/guides/search/"
  },
  "meta": {
    "status": "SUCCESS",
    "title": "网页搜索与抓取"
  },
  "content": [
    {
      "type": "text",
      "value": "# 网页搜索与抓取\n\nSearch 用于检索网页来源，Fetch 用于读取指定页面。\n"
    }
  ]
}
```

脚本按已知类型取得正文或交付实际失败值：

```javascript
try {
  const page = await tools.web_fetch({
    target: {kind: "URL", url: "https://api-docs.deepseek.com/zh-cn/guides/search/"}
  });
  console.log({
    target: page.target,
    title: page.meta.title,
    text: page.content.map(part => part.value).join("")
  });
} catch (err) {
  console.log(err.result !== undefined ? err.result : String(err.message ?? err));
}
```

## 3. 模型按需交错使用 Search／Fetch

模型通过 web_search 发现来源，通过 web_fetch 阅读选中的页面，再依据实际收到的内容决定继续搜索、读取其他链接，或整理结论与来源引用。可以连续搜索、并行读取多个页面，也可以直接从已知 URL 开始；不要求严格交替。

```text
search：找到相关来源
→ fetch：阅读选中的页面
→ 发现还缺一项细节
→ search：针对该细节补充检索
→ fetch：阅读更合适的页面
→ 资料足够后整理答案和来源
```

上述选择由模型承担，runtime 执行每次明确调用，不因为搜索命中多个页面就自动抓取全部页面。原生与 JS 共用 URL Target，可以直接传递搜索记录中的 target，无须新建网页引用或登记读取资格。

脚本也可执行预先明确的选择规则。例如取得排名靠前的一条来源并读取，将页面实际输出给模型：

```javascript
const hits = await tools.web_search({query: "DeepSeek 搜索 API", count: 3});
const first = hits.content.find(part => part.type === "object");
if (first) {
  const page = await tools.web_fetch({target: first.value.target});
  console.log(page);
} else {
  console.log(hits);
}
```

这段脚本的选择规则是“读取第一条”，不表示模型编写脚本时已经看过搜索结果。需要模型阅读摘要后选择来源时，应先输出摘要，在模型实际收到后再决定下一次调用；读取页面后的后续判断同理。

## 4. 用户配置、默认免费池与静默回退

### 4.1 最简用户配置

未配置自有 Key 时即可使用两个工具，无须注册、选择供应商或收到配置提醒。设置页只需提供可选的供应商四选一和 API Key：

| 配置 | 当前合同 |
| --- | --- |
| 默认 | 自动免费，按 Firecrawl → Tavily 顺序使用官方无 Key 入口 |
| 可选供应商 | DeepSeek／Firecrawl／Tavily／Exa，选择一个，供两个工具共用 |
| 可选 API Key | 配置后优先使用所选供应商的账户；不配置则使用默认免费池 |

供应商选择决定协议与适配器，不通过 Key 外形猜测供应商，也不把一份 Key 发给多个服务。无需用户填写模型名、协议类型、请求头、代理地址或高级检索／提取参数。搜索与读取可以由不同免费候选完成，不要求它们固定落到同一家。

用户选择使用 DeepSeek 自有 Key 时，可以识别已保存的官方 DeepSeek 模型连接，供用户选用其凭据引用；不复制或展示秘密，不把自定义兼容端点的 Key 当作官方 Key。默认免费模式不因本地存在模型 Key 就自动使用该账户。凭据沿现有用户级设置与秘密管理 owner 保存，不进入工具参数、结果、模型上下文或诊断日志。

自有 Key 按对应账户的正常计费规则使用，可能消耗赠送额度、已购余额，或触发用户在供应商处设置的按量计费／自动充值；额度不足时才回退不等于只使用该账户的免费额度。Pulsara 不替用户购买额度或开启充值，也不自动借用其他已保存的付费 Key。

### 4.2 免费池与请求形状

| 候选 | Search／Fetch 的官方入口 | 无 Key 请求 |
| --- | --- | --- |
| Firecrawl，第一顺位 | POST https://api.firecrawl.dev/v2/search；POST https://api.firecrawl.dev/v2/scrape | 不发送 Authorization，使用上述统一参数的适配结果 |
| Tavily，第二顺位 | POST https://api.tavily.com/search；POST https://api.tavily.com/extract | 发送 X-Tavily-Access-Mode:keyless，不发送 Authorization，使用同一响应映射 |

两者使用直接 API，不安装或查询 MCP 服务。匿名请求须显式隔离认证；不能让 SDK 从环境变量、默认客户端或已保存连接中自动拾取 Key，导致所谓免费请求实际按账户计费。维护中的 SDK 能明确选择匿名模式时复用其入口，否则沿现有 HTTP 客户端作最小适配，不复制网络栈。

免费请求受服务自己的限制。Firecrawl 公开了按 IP、每日请求数及每日 credits 的边界；Tavily 无 Key 入口有速率／公平使用限制。未公开的具体可用量与恢复时间保持未知，不承诺固定免费次数、无限可用或每日某时必定恢复。Exa、DeepSeek 不进入本免费池；它们的自有 Key 适配仍按前述合同使用。

依据：[Firecrawl 无 Key 与速率限制](https://docs.firecrawl.dev/rate-limits)、[Tavily 无 Key Search／Extract](https://docs.tavily.com/documentation/keyless.md)。账户计费另见 [Firecrawl Billing](https://docs.firecrawl.dev/billing)、[Tavily API Credits](https://docs.tavily.com/documentation/api-credits)、[DeepSeek 搜索与抓取指南](https://api-docs.deepseek.com/zh-cn/guides/search/)。

### 4.3 一次逻辑调用的候选顺序与结算

```text
未配置自有 Key：
  Firecrawl 无 Key → Tavily 无 Key

已配置自有 Key：
  所选供应商＋自有 Key → Firecrawl 无 Key → Tavily 无 Key
```

候选以“供应商＋访问方式”区分。例如 Firecrawl 账户额度不足后，仍可尝试 Firecrawl 无 Key 入口；两者不会混用认证或额度事实。参数、选择的账户凭据及候选顺序在本次调用接纳时取得同一份快照，执行中更改设置只影响后续调用。

先校验共同参数，再按顺序使用当前可用且能表达完整请求的候选。一次逻辑调用中每个候选只进行一次业务层派发，不循环轮换；这不改变 SDK／客户端既有传输行为，也不限制后续调用、聊天轮次或会话寿命。传输层复用依赖既有机制，不另建重试框架。

| 情况 | 当前调用如何处理 |
| --- | --- |
| 未配置 Key | 直接进入免费池，不作为错误或弹出配置提示 |
| 共同参数无效，如非法日期、count 或 URL | 派发前 ERROR，不靠换服务绕过校验 |
| 某候选不能表达条件或超出其真实请求限制 | 跳过并保留原因，尝试后续候选；不丢筛选、不截查询／URL、不降低请求数量 |
| 账户认证失败、额度不足、服务限流、暂时网络／服务失败，或响应无法接纳 | 静默尝试后续可用候选；错误分类依据服务实际响应 |
| Fetch 的目标网页访问／提取失败，或只取得已知不完整正文 | 可继续尝试后续读取候选；不据此认定该供应商账户额度耗尽或全局不可用 |
| 搜索成功，包括零命中、少于 count、域名核对后为空 | 立即以 SUCCESS 结算，不为数量或质量追加搜索 |
| 正常提取成功，包括明确成功空正文 | 立即以 SUCCESS 结算；未知缓存／完整性不触发回退 |
| 真实取消 | 停止候选推进，按既有取消语义结算，不为完成请求改用另一家 |

所有尝试表达同一业务请求，不拆查询、不自动扩写、不补搜凑数。取首个成功候选的完整归一化结果，不合并多家排名或拼接多份页面正文。前面失败、后面成功时最终为 SUCCESS，正常结果中不夹带前面尝试的失败记录或路由说明。

全部候选均未成功时为 ERROR，content 简短说明实际原因；有可用部分内容时，保留候选顺序中首个具有可用部分内容的尝试及其未完成原因，其他尝试只补充有用的失败说明，不把它们拼成一份看似完整的结果。不把限流、余额不足或访问失败伪装成零命中。已确定的正常结算不被后来到达的取消改写。

回退仅发生在本次业务结果确定之前。随后完整保存、接纳同一份最终业务值，原生分页、JS、历史与导出均读取这份值。结果保存或交付失败不触发换服务；NEXT 不重新联网、更新缓存或补取另一家内容。

### 4.4 可用性、发送边界与权限

候选选择与轻量可用性事实归用户级服务 owner，原生、ROOT JS 和各 Subagent 共用，不依赖当前选中的会话。可在进程内复用真实 Retry-After 或明确的额度恢复时刻，避免同一限制期间反复派发；不把普通 429 一概当作额度耗尽，也不自造固定恢复时间。没有明确恢复时刻的失败不永久封锁后续调用；目标网站级失败与服务账户级限制分别处理，必要时按实际操作范围区分。无需维护精确剩余额度、持久额度账本、变更事件、后台补偿或新的数据库表。

正常免费使用、缺少自有 Key 和成功回退不弹窗、不请求配置／升级、不主动唤醒模型，也不发送 runtime observation 或 steer。候选均失败时只沿本次工具结果说明实际失败，不额外制造提醒；不增加 provider、计费、尝试次数等公开结果字段。

两个工具均按外部只读请求准入：ROOT／Subagent 原生以及 ROOT JS 共用相同参数、路由与结算；只读模式和 Plan 中可用，仍遵守既有权限与取消 owner。实际请求会将查询或目标 URL 交给本次选中的供应商，回退可涉及下一家；更换候选不扩张用户请求的业务范围，不另建信任／启停／审批流程。

工具定义固定，不因 Key、免费额度或后端可用性变化改写同 epoch 的 SYSTEM／provider tools 或已有消息。冷启动首个模型请求不等待免费池预连接或探测，实际调用再按上述边界请求服务；不增加目录、调用引用或启动握手门槛。
