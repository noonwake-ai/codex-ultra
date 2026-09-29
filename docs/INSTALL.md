# 安装与部署

本文面向第一次部署的人，一步一步来，不需要懂 Codex 内部实现。

> **懒得自己敲命令？** 把这段发给你的 AI，它会照着一份专门写给 AI 的任务书帮你装完：
>
> ```
> 帮我安装 Codex Ultra：https://raw.githubusercontent.com/noonwake-ai/codex-ultra/main/docs/ai-install.md
> ```
>
> 剩下的手动流程在下面，两种方式装出来是一样的。

[返回主页](../README.md)

---

## 一、先确认你的条件

| 项目 | 要求 | 怎么检查 |
|---|---|---|
| 系统 | macOS 13 或更新 | 左上角 ︎ → 关于本机 |
| Python | 3.11 或更新 | `python3 --version` |
| 网关 | 提供 OpenAI Responses 接口和 `/models` 目录 | 见下方「网关自检」 |
| 压缩模型 | 至少一个你额度充足的模型 | 在网关里能看到即可 |
| 磁盘 | 约 300 MB（含依赖和 tokenizer 缓存） | 安装脚本会告诉你 |

> **macOS 自带的 Python 是 3.9，不够用。** 先跑 `python3 --version` 看一眼。
> 如果显示 3.9，用下面任一方式装个新的，然后用新解释器执行后面的命令：
>
> ```bash
> ls /opt/homebrew/bin/python3.* /usr/local/bin/python3.*   # 说不定你已经有了
> brew install python@3.12                                   # 或者用 Homebrew 装
> ```
>
> 没有 Homebrew 就去 <https://www.python.org/downloads/> 下载安装包。
> 装好后命令里的 `python3` 换成 `python3.12` 即可。

### 网关自检

在装之前先确认你的网关是这个形状的：

```bash
export CODEX_ULTRA_API_KEY="你的网关密钥"
curl -sS -H "Authorization: Bearer $CODEX_ULTRA_API_KEY" \
  "https://你的网关地址/v1/models?client_version=0.158.0" | head -c 300
```

看到类似于 `{"models":[{"slug":...` 的 JSON，就说明对上了。
如果返回的是 HTML、401 或者别家的结构，请先在网关侧修好，Codex Ultra 不会替你猜接口。

> **CC Switch 用户**：不要换供应商，也不要新建。你现有的供应商和 key 就是对的，
> 安装时加上 `--cc-switch-db` 让 Codex Ultra 同步更新那条记录即可。

---

## 二、三行命令

```bash
git clone https://github.com/noonwake-ai/codex-ultra.git
cd codex-ultra
export CODEX_ULTRA_API_KEY="你的网关密钥"
```

**第一步：生成模型目录。**

```bash
python3 build_catalog.py \
  --gateway https://你的网关地址/v1 \
  --out models.json
```

它会打印一份摘要：读到多少个模型、分别属于哪些厂商、哪些模型需要图片桥接。
下面的数字只是示例形状，实际以你网关读到的为准。

```json
{
  "ok": true,
  "models": 8,
  "by_vendor": {"Anthropic": 1, "DeepSeek": 2, "Google": 1, "OpenAI": 3, "xAI": 1},
  "media_bridge_models": ["gemini-3.8-flash"]
}
```

**第二步：安装本地服务。**

```bash
python3 install.py \
  --upstream https://你的网关地址/v1 \
  --compactor-model deepseek-v4-flash \
  --compactor-effort medium
```

`--compactor-model` 就是以后帮你压缩上下文的模型，**要填你网关里真实存在的模型名**。
建议选一个快、便宜、额度足的；写错了会在安装时直接报错，不会静默降级。

CC Switch 用户加一个参数，避免下次切供应商时把适配层切掉：

```bash
  --cc-switch-db ~/.cc-switch/cc-switch.db
```

**第三步：重开一个 Codex 对话。**

模型列表里会出现你网关里的所有模型，直接选就行。

---

## 三、安装器究竟改了什么

它只做四件事，而且都是可逆的：

```text
~/Library/Application Support/Codex Ultra/     服务源码、虚拟环境、tokenizer 缓存（权限 700）
~/.codex/config.toml                          只改一个 base_url
~/Library/LaunchAgents/ai.codexultra.local-adapter.plist   随登录自启
Keychain 条目 codex-ultra / checkpoint-key-v1   本机加密检查点的密钥
```

**永远不会动的东西**：你的 API key、聊天记录、模型目录之外的配置、
技能、插件、MCP 设置、别的供应商记录。

### 验证安装

```bash
python3 configure.py status
```

```json
{
  "provider": "你的供应商",
  "endpoint": "http://127.0.0.1:15731",
  "expected_local_endpoint": "http://127.0.0.1:15731",
  "routed_locally": true,
  "rollback_record": true
}
```

`routed_locally` 为 `true` 就说明请求已经走本机适配层了。
如果你安装时换了端口，这里显示的是你那个端口，不影响判断。

再打开 Codex 新开一个对话，随便问一句。能正常回复就说明链路通了。

---

## 四、日常维护

```bash
PY="$HOME/Library/Application Support/Codex Ultra/runtime/bin/python"

# 看服务日志（不含请求正文和密钥）
tail -f "$HOME/Library/Application Support/Codex Ultra/service.log"

# 重启服务
launchctl kickstart -k "gui/$(id -u)/ai.codexultra.local-adapter"

# 换了模型想更新目录
python3 build_catalog.py --gateway https://你的网关地址/v1 --out models.json

# 重启一下，新的图片桥接名单就会生效（不需要重装）
launchctl kickstart -k "gui/$(id -u)/ai.codexultra.local-adapter"
```

> 服务每次启动都会重新读取 `models.json` 来决定哪些模型需要图片桥接，
> 所以**加了新模型只要重新生成目录再重启服务**，不用重跑 `install.py`。
> 当前生效的名单可以直接看健康检查：
>
> ```bash
> curl -s http://127.0.0.1:15731/health
> ```

### 升级一个已经装好的 Codex Ultra

升级不换供应商、不动 key、不动聊天记录，只替换服务源码再重启：

```bash
cd codex-ultra && git pull --ff-only
ROOT="$HOME/Library/Application Support/Codex Ultra"
for f in adapter.py tool_image_bridge.py model_presets.py build_catalog.py \
         direct_handoff.py native_checkpoint.py configure.py requirements.txt; do
  cp "$f" "$ROOT/$f"
done
"$ROOT/runtime/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt"
launchctl kickstart -k "gui/$(id -u)/ai.codexultra.local-adapter"
curl -s http://127.0.0.1:15731/health
```

> `install.py` 遇到已存在的 `config.json` 会拒绝重装，这是故意的：它不会覆盖你的配置。
> 所以升级走上面这条路径，`config.json` 里你自己改过的东西一律保留。

### 上传压缩（`upstream_encoding`）

Codex 本来就把请求体压缩后再发给适配层。适配层要把它解开、适配成网关能吃的形状，
所以**发出去之前会重新压缩**，而不是把几 MB 的明文再上传一遍——慢网络上，正是这种大
上传最容易断在半路，被网关记成「读不到请求体」。

- 默认 `zstd`。老配置里没有这个字段也一样生效，不需要手动迁移。
- 当前用的是哪种，直接看 `/health` 里的 `upstream_encoding`。
- 万一你的网关不认 zstd：适配层发现压缩请求被拒时，会自动换成不压缩的方式重发一次，
  你不会看到失败；连续几次之后它会暂停压缩，冷却 5 分钟起、每次翻倍、最多 1 小时，
  到点自动再试一次。临时抖动不会把上传永久放大。
- 想彻底关掉：把 `config.json` 改成 `"upstream_encoding": "identity"`，重启服务。

---

## 五、回滚

```bash
python3 configure.py rollback
```

它只还原那一个 `base_url`，**之后你对配置做的其它改动一律保留**。

回滚记录写在 `~/Library/Application Support/Codex Ultra/routing-rollback.json`，
里面只有供应商名、两个地址和一个校验值——**不含任何密钥**。
如果你安装时用了自定义 `--root`，安装结束时会直接打印出带路径的准确回滚命令，
照抄即可：

```json
{
  "rollback_command": ".../configure.py rollback --rollback-record .../routing-rollback.json"
}
```

> 注意：回滚**不会删除**本机的加密检查点。含有检查点的老对话仍然需要这个服务来读取，
> 所以不要急着删服务目录。想彻底清理的话，先确认不再需要那些老对话。

---

## 六、卸载

```bash
launchctl bootout "gui/$(id -u)/ai.codexultra.local-adapter"
python3 configure.py rollback
```

然后自行删除：

```text
~/Library/Application Support/Codex Ultra/
~/Library/LaunchAgents/ai.codexultra.local-adapter.plist
```

Keychain 里的 `codex-ultra` 条目可以保留（老对话需要它），确认不需要再删。

---

## 七、遇到问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `service_not_healthy_route_unchanged` | 服务没起来 | 看 `service-error.log`；常见是端口被占或 Keychain 被拒 |
| 请求返回 502 | 上游网关拒绝 | 用上面的 `curl` 自检网关；Codex Ultra 会把失败原样报出来，不伪造成功 |
| `api_key_env_not_set` | 没导出密钥 | `export CODEX_ULTRA_API_KEY=...` |
| `requested_model_not_offered:xxx` | 你的 key 看不到这个模型 | 找管理员开权限；Codex Ultra 不会替你编一个 |
| `compactor_model_not_in_catalog:xxx` | 压缩模型不在你网关返回的目录里 | 换一个 `models.json` 里真实存在的模型名，或先重新生成目录 |
| 模型列表里少了模型 | 网关没返回 | `build_catalog.py` 的摘要会列出实际读到的厂商 |
| 图片在 Gemini 那里丢了 | 该路由没被识别为需要桥接 | 见 [模型能力表](MODELS.md)，加一条 override |
| 切了供应商之后不生效 | CC Switch 的记录覆盖了配置 | 安装时加 `--cc-switch-db`，或重新 `configure.py apply` |
| 新加的模型图片不对 | 服务还没重新读目录 | 重新生成 `models.json`，然后重启服务；看 `/health` 的 `media_models` |

### 端口被占用

默认使用 `15731`。换端口：

```bash
python3 install.py --port 15741 ...
```

注意 `configure.py apply --local` 要写同一个端口。

### 其它系统

适配层本身是纯 Python，在 Linux / Windows 上也能跑：

```bash
python3 adapter.py --config config.json      # 首次加 --init-key
```

但当前安装器用的是 macOS Keychain 和 launchd，所以一键安装只覆盖 macOS。
欢迎提交 Windows 服务 / systemd 的 PR。

---

## 八、安全边界

- 密钥只在内存中传递，不落盘、不进日志、不进压缩结果。
- 加密检查点用 AES-GCM，密钥存在系统 Keychain，只有本机能解。
- 服务只监听 `127.0.0.1`，不监听 `0.0.0.0`。
- 安装器强制上游是 HTTPS，并且拒绝把回环地址当上游，避免代理环路。
- `test_secret_scan.py` 会在 CI 里扫描整个仓库，出现疑似密钥或内部标识直接失败。
