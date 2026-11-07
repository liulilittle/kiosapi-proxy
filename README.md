# kiosapi-proxy

单端口多上游、多 KEY 轮换的 OpenAI/Anthropic 兼容代理。

- 一个 key 被限流 → 冷却该 key → 换下一个 key 重试同一请求；下一个也限流就继续换；全部限流才返回最后一次限流响应。
- 单端口按路径前缀路由到不同上游（kenari、taotoken、openrouter、deepseek、智谱、kimi、gemini、grok……），新增上游只加配置。
- 支持 `0.0.0.0`（IPv4）与 `::`（IPv6）监听；双栈需同时列出 `0.0.0.0, [::]`，并强制客户端认证 key，可安全暴露公网（建议前置 TLS）。
- 改配置文件增删 key 或上游自动热更新，无需重启。

## 配置

复制 `proxy.ini.example` 为 `proxy.ini`（已在 `.gitignore`）。

```ini
[server]
host = 127.0.0.1          ; 多地址用逗号分隔，如 0.0.0.0, [::] 或 [::1]
port = 8787
auth_keys = pk-aaa, pk-bbb ; 绑非回环地址时必须非空且不能是占位符
reload_interval = 2        ; 配置热更新轮询秒数
log_requests = false       ; true 时打印每次转发的 upstream/key尾4位/状态码
admin_key =                ; 留空关闭管理端点；设置随机串以启用 manage.py 立即热更新
admin_allow_remote = false ; 管理端点默认只接受本机回环
rotate_auth_errors = true  ; 401/403 视为该 key 失效并换下一个
max_body_size = 67108864   ; 请求体上限（默认 64 MiB）
; state_file = proxy.ini.state.json ; key 顺序持久化文件，off 可关闭
timeout = 120
max_response_size = 67108864 ; 上游响应体大小上限（默认 64 MiB），防止内存无限增长

base_url = https://kiosapi.com/v1   ; 默认上游（无前缀路径走这里）

[keys]
key1 = sk-xxxx
key2 = sk-xxxx

[upstream:kenari]
base_url = https://kenari.id/v1
keys = kn-aaaa, kn-bbbb

[upstream:zhipu]
base_url = https://open.bigmodel.cn/api/paas/v4
keys = xxxx
; 也可改为 key1 = ..., key2 = ...；同一 section 里 keys = 与 keyN = 二选一
```

上游可选字段：`prefix`（默认 `/上游名`）、`model`（强制覆盖请求里的 model）、`user_agent`、`timeout`、`auth_style`（`bearer` 默认 / `x-api-key` / `api-key` / `both`）、`session_header`（需要稳定会话头的上游，如 `x-opencode-session`）。

### opencode 的两个上游

| 用途 | base_url | 说明 |
|---|---|---|
| Go 订阅 | `https://opencode.ai/zen/go/v1` | 付费/订阅模型；免费模型在此端点返回 400 |
| Zen 按量 | `https://opencode.ai/zen/v1` | 付费模型 + `-free` 免费模型 |

两个端点自 2026-09-06 起要求 `x-opencode-session`，缺失返回 `400 MissingSessionID`。代理在 `session_header = x-opencode-session` 时会自动补齐：优先复用客户端的 `x-session-id`/`x-opencode-session`（同一会话稳定），否则按代理 key 派生进程内稳定值（进程重启后会变化；需跨重启稳定请显式发送会话头）。

实测（2026-11-07）：`opencode-go` 下 key1 触发 `429 GoUsageLimitError`（月度限额）后，代理自动切到 key2 并返回 200，非流式与流式均成功。

## 路由

`/<前缀>/<上游路径>` → 对应上游；无前缀 → 默认上游。前缀会被剥离，再与上游 base_url 去重拼接。

| 客户端 baseURL | 上游 |
|---|---|
| `http://proxy:8787/v1` | 默认（kiosapi） |
| `http://proxy:8787/kiosapi/v1` | 默认（kiosapi 别名） |
| `http://proxy:8787/kenari/v1` | kenari |
| `http://proxy:8787/taotoken/v1` | taoToken Coding Plan |
| `http://proxy:8787/openrouter/api/v1` | OpenRouter |
| `http://proxy:8787/deepseek/v1` | DeepSeek 官方 |
| `http://proxy:8787/zhipu/api/paas/v4` | 智谱 GLM |
| `http://proxy:8787/kimi/v1` | Kimi |
| `http://proxy:8787/minimax/v1` | MiniMax |
| `http://proxy:8787/hunyuan/v1` | 腾讯混元 |
| `http://proxy:8787/qwen/compatible-mode/v1` | 阿里云百炼 |
| `http://proxy:8787/ark/api/v3` | 火山方舟/豆包 |
| `http://proxy:8787/qianfan/v2` | 百度千帆/文心 |
| `http://proxy:8787/jd/v1` | 京东云模型服务 |
| `http://proxy:8787/kuaishou/api/gateway/v1/endpoints` | 快手万擎 |
| `http://proxy:8787/longcat/openai/v1` | 美团 LongCat |
| `http://proxy:8787/stepfun/v1` | 阶跃星辰 |
| `http://proxy:8787/sensenova/v1` | 商汤日日新 |
| `http://proxy:8787/csdn/v1` | CSDN 星图 |
| `http://proxy:8787/xiaomi/v1` | 小米 MiMo |
| `http://proxy:8787/scnet/api/llm/v1` | 国家超算互联网 SCNet |
| `http://proxy:8787/gemini/v1beta/openai` | Gemini OpenAI 兼容层 |
| `http://proxy:8787/xai/v1` | xAI Grok |
| `http://proxy:8787/vapi/v1` | V-API |
| `http://proxy:8787/engy/v1` | Engy |
| `http://proxy:8787/opencode-go/v1` | opencode 推理 |
| `http://proxy:8787/commandcode/v1` | Command Code |
| `http://proxy:8787/opendesign/v1` | OpenDesign |
| `http://proxy:8787/naga/v1` | Naga |
| `http://proxy:8787/unorouter/v1` | UNOROUTER |

## 客户端认证

配置 `auth_keys` 后，每个请求必须带其中之一：

```
Authorization: Bearer <proxy-key>
x-api-key: <proxy-key>
```

- 客户端 key 只用于认证，转发到上游时会被替换成池内 key。
- `auth_keys` 为空且所有绑定地址都是回环（127.0.0.1/::1）时放行，保持本地免认证用法。
- 绑定 `0.0.0.0`、`::` 或具体网卡地址却未配 `auth_keys` 时启动直接报错。

## 轮换与限流判定

- 命中限额：HTTP 429、402，或 4xx 响应体出现限额关键字（`rate_limit`、`free_quota`、`plan_limit_reached`、`insufficient_balance`、`FreeUsageLimitError`、`GoUsageLimitError` 等）；5xx 一律透传，不按 body 关键字轮换。
- 401/403 默认视为该 key 失效（吊销/无权限），冷却 1 小时后换下一个 key；全部失效才返回最后一次 401/403。`rotate_auth_errors = false` 可恢复原样透传。
- 2xx 响应永不按响应体关键字判定，避免正常输出里的 "rate limit" 触发换 key。
- 冷却时长取 `Retry-After`（上限 3600 秒），缺省 60 秒；冷却中的 key 排到候选末尾，全部冷却时仍然尝试。
- 证书错误换下一个 key 重试；其它连接错误直接返回 502。
- 上游没有配置任何 key 时返回 503 `proxy_config_error`（不会误报 502）。
- 请求体默认上限 64 MiB（`max_body_size`），支持多模态大图/视频。
- 上游非流式响应体默认上限 64 MiB（`max_response_size`，0 表示不限制），超限返回 502 `proxy_error` 并关闭上游连接，避免被异常上游拖垮内存。
- `base_url` 自带 query（如 Azure `?api-version=...`）时，客户端 query 优先，其余 base query 参数自动追加。

## 各上游错误码差异（官方文档 + 实测）

代理按“是否与 key 相关”决定换 key，而不是只看 HTTP 状态：

| 情况 | 代理行为 |
|---|---|
| 401 / 403（非内容审核） | 视为 key 失效，冷却 3600 秒并换下一个 |
| 400 / 422 且 body 含 `invalid api key`、`incorrect api key`、`invalid_iam_token` 等 | 视为 key 失效并换下一个（Gemini、xAI 用 400 报坏 key） |
| 402 或 body 含 `insufficient_quota`、`insufficient_balance`、`quota_exceeded`、`plan_limit_reached`、`free_quota_daily`、`credit_limit_exceeded`、`欠费` 等 | 换 key，冷却 3600 秒（余额/额度不会 60 秒恢复） |
| 429 且无额度关键字 | 换 key，按 `Retry-After`，缺省 60 秒 |
| 200 + 配置的 `error_body_path` 命中 rotate/auth 码 | 换 key（MiniMax `base_resp.status_code`） |
| 403 内容审核（`inappropriate_content` 等） | 原样透传，不换 key |
| 400 参数错误 / 404 模型不存在 / 5xx 过载 | 原样透传，不换 key |

各上游要点：

- **taotoken**：401 `AuthenticationError`；403 `InvalidSubscription`/`AccountOverdueError`；429 `RateLimitExceeded.RPM`/`TPM`/`QuotaExceeded`。
- **naga**：401；402 `insufficient_quota`；403 `credit_limit_exceeded`（换 key）；403 `inappropriate_content`（不换）；429 `rate_limit_exceeded`；503 `server_error`（透传，客户端退避）。
- **unorouter**：401；402 key 支出上限；403 余额/模型/IP；429；503 `get_channel_failed`（透传）。
- **kiosapi**：401；403 token/模型权限；429 额度/余额/并发；502/503 上游不可用（透传）。
- **opencode**：401；402 `Insufficient account funds`；429 `GoUsageLimitError`/`FreeUsageLimitError`；400 `MissingSessionID`（代理自动补 `x-opencode-session`）。
- **kenari**：401 纯文本 `invalid key`；402 `insufficient_balance`；429 `rate_limit_exceeded`/`free_quota_rpm`/`free_quota_daily`/`plan_limit_reached`/`upstream_error`；503 `all_providers_failed`（透传）。
- **deepseek**：401；402 余额；422 参数；429 并发；500/503（透传）。
- **智谱 GLM**：401（1000/1001/1003/1005）；429 一族：1113 欠费、1302 并发、1305 过载、1308/1310/1316-1321 额度窗口、1309/1311/1313/1314/1315 套餐（全部换 key；1305 换 key 可能无效但会尝试）。
- **kimi**：401；429 同时表示过载/余额/RPM/TPM；500（透传）。
- **minimax**：OpenAI 兼容端点用 **HTTP 200 + `base_resp.status_code`**（1002 频率、1004 鉴权、1008 余额、1039 TPM、2045-2047 增长限速、2056/2062/2067 套餐）；已用 `error_body_path` 配置识别并换 key。Anthropic 端点用标准 429/529。
- **阿里百炼**：401 `InvalidApiKey`；403 `AccessDenied`；429 `Throttling.RateQuota`/`insufficient_quota`；5xx（透传）。
- **腾讯混元**：401；429 带 `Retry-After`；5xx（透传）。
- **火山方舟 / 豆包**：401；429；5xx（透传）。
- **百度千帆**：401 `invalid_iam_token`；429；5xx（透传）。
- **Gemini**：坏 key 返回 **400** `Please pass a valid API key`（按 auth 关键字换 key）；429 `RESOURCE_EXHAUSTED`；5xx（透传）。
- **xAI Grok**：坏 key 返回 **400** `Incorrect API key provided`（按 auth 关键字换 key）；429；5xx（透传）。
- **OpenRouter**：401；402 余额/在途预算（带 `Retry-After` 时按它退避，否则长冷却）；403 预算/权限/审核；429；502/503（透传）。**200 也可能携带 `error.code` 业务错误**，已配置 `error_body_path = error.code` 识别。
- **Kimi**：401；429 同时表示 `engine_overloaded_error`（退避）、`exceeded_current_quota_error`（余额，换 key）、`rate_limit_reached_error`（并发/RPM/TPM/TPD，换 key）；504 网关超时（透传）。
- **阿里百炼**：401 `InvalidApiKey`；403 `AccessDenied`/欠费/免费额度耗尽；429 `Throttling.RateQuota`/`AllocationQuota`/`insufficient_quota`/`BurstRate`/`Concurrency`/`ServiceOverloaded`；5xx（透传）。
- **腾讯混元/TokenHub**：401（401001-401005）；402（401007/401008/403004 免费额度/欠费）；403（403001-403006 权限/模型/IP/账号）；429（429001-429006，带 `Retry-After`）；451 内容过滤（透传）；5xx（透传）。
- **火山方舟/豆包**：401；403（`AccountOverdueError` 欠费、`OperationDenied.ServiceOverdue` 等）；429 一族（`RateLimitExceeded.*`、`QuotaExceeded`、`SetLimitExceeded`、`ServerOverloaded`、`RequestBurstTooFast`）；5xx（透传）。
- 其余 OpenAI 兼容上游（engy/stepfun/sensenova/xiaomi/longcat/vapi/opendesign 等）均为标准 401/402/429/5xx；已按同一规则处理。

> 说明：5xx 与 503 通常是上游整体过载，换 key 未必有用，代理原样透传并回传 `Retry-After`，由客户端退避或切换模型。
>
> 代理不跟随上游 3xx 重定向，原样透传（含 `Location`），避免把池 key 带到重定向目标。

## 协议覆盖

- OpenAI Chat Completions：所有上游。
- OpenAI Responses：kenari、commandcode、kiosapi 实测 200；taotoken Coding Plan 不支持 Responses（上游返回 400 no_available_provider），请用 Chat Completions 或按量入口。
- Anthropic Messages：kenari `/v1/messages` 实测 200；其它支持 Messages 的上游可另建 `[upstream:xxx]` 指向其 Anthropic base。
- 流式 SSE、查询串、`anthropic-version`/`anthropic-beta` 均透传。

## 热更新

热更新就是不用重启：

- 代理每 `reload_interval` 秒检查配置文件 mtime/size，变化才重新解析。
- 已存在的上游原地更新 base_url/prefix/model/user_agent/timeout/auth_style/session_header；key 列表合并并保留运行时顺序与冷却状态。
- 新增上游立即生效；删除上游从路由中移除。
- 配置写坏时保留旧配置继续服务并打印错误。
- `host` / `port` / `max_body_size` 改动需重启（会打印提示）。

### 立即生效（命令行/管理端点）

配置 `admin_key` 后，代理提供 `POST /_admin/reload`（默认只接受本机回环）。`manage.py` 修改配置后会自动调用它，脚本执行完立即生效，无需等待轮询：

```powershell
python manage.py add-key kenari kn-new
python manage.py remove-keys kenari old --yes
python manage.py clear-all-keys --yes
```

`--no-reload` 可跳过立即重载（仍会被文件轮询在 `reload_interval` 内应用）。

## 密钥顺序持久化

- 运行时每次选 key 后自动调整顺序（未冷却优先、冷却中的排后），并写入 `<配置文件>.state.json`。
- 状态文件只保存 key 的 SHA-256 指纹，不保存明文 key；已在 `.gitignore`。
- 下次启动读取该文件，按调整后的顺序继续轮换；新 key 追加到末尾，已删除的 key 自动忽略。
- 状态文件写坏/缺失时回退到配置顺序，不影响启动。
- 写入有 1 秒防抖；正常退出时会强制落盘。`state_file = off` 可关闭。

## 命令行管理

```powershell
python manage.py list                    # 列出上游、key 数量、掩码 key
python manage.py show kenari             # 查看单个上游详情与 key 序号
python manage.py add-key kenari kn-new   # 添加一个 key
python manage.py add-keys kenari kn-a kn-b
python manage.py set-key kenari 2 kn-new # 按 1-based 序号替换
python manage.py remove-key kenari 2     # 按序号或 key 尾匹配删除
python manage.py remove-keys kenari abc --yes   # 批量模糊匹配删除
python manage.py clear-keys kenari --yes        # 清空单个上游
python manage.py clear-all-keys --yes           # 清空全部
python manage.py state                   # 查看持久化的 key 顺序
python manage.py check [kenari]          # 探测上游 /models 连通性
python manage.py check --chat-model m    # 用最小 chat 请求做真实鉴权探测
python manage.py validate                # 校验配置并汇总
python manage.py reload                  # 手动触发立即热更新
python manage.py --port 9999 reload      # 代理用 --port 覆盖时指定实际端口
```

- 默认只显示掩码 key（`kn-aaa...a111`）；`--reveal` 才显示完整 key。
- 批量删除多个匹配必须加 `--yes`；`clear-keys`/`clear-all-keys` 必须加 `--yes`。
- 文本编辑保留 ini 注释、字段与 CRLF 换行，原子写入（临时文件 + 替换）。
- 默认不记录详细流量；`log_requests = true` 才打印每次转发的 upstream、key 尾 4 位与状态码。
- `host` 支持 `0.0.0.0`、`::`、`[::]`、`[::1]`、`[::]:8787`、`127.0.0.1:8787`；端口以 `[server] port` 为准。单写 `::` 仅监听 IPv6；需要双栈时写 `host = 0.0.0.0, [::]`。
- 绑非回环地址且 `auth_keys` 为空或含占位符（`xxxx`/`REPLACE_ME`）时拒绝启动；`admin_allow_remote = true` 时 `admin_key` 也不能是占位符。

## 运行

```bat
start "" /min pythonw kiosapi.py --config proxy.ini --port 8787
```

或前台运行以便看日志：

```powershell
python kiosapi.py --config proxy.ini
```

确认监听：

```powershell
netstat -ano | findstr ":8787"
```

## 接入 opencode

```json
"provider": {
  "kenari": {
    "npm": "@ai-sdk/openai-compatible",
    "options": {
      "apiKey": "pk-aaa",
      "baseURL": "http://127.0.0.1:8787/kenari/v1"
    }
  },
  "zhipu": {
    "npm": "@ai-sdk/openai-compatible",
    "options": {
      "apiKey": "pk-aaa",
      "baseURL": "http://127.0.0.1:8787/zhipu/api/paas/v4"
    }
  }
}
```

`apiKey` 填代理的 `auth_keys` 之一，不再是上游 key。

## 公网部署

1. 代理只提供 HTTP。公网请用 Caddy/Nginx 反代加 TLS，或走 Cloudflare Tunnel / Tailscale，否则客户端 key 会明文传输。
2. `host = 0.0.0.0, [::]` 并配置 `auth_keys`。
3. 建议在反代层再做 IP 白名单和速率限制。
4. 反代层必须屏蔽 `/_admin/`；管理端点只应由本机访问（默认仅回环 + `admin_key`）。
5. `proxy.ini` 含真实密钥，POSIX 下建议 `chmod 600 proxy.ini`；Windows 请限制文件 ACL。

## 测试

```bash
python -m unittest discover -s tests -p "test_*.py"
```

- `test_kiosapi.py`：旧单上游行为（轮换、429、流式、URL 拼接、证书错误）。
- `test_multi_upstream.py`：配置解析、冷却池、认证、前缀路由、头部透传、双栈绑定、热更新。
- `test_scale.py`：限额假上游验证轮换提升吞吐。
