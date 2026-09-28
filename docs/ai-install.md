# 帮我安装 Codex Ultra

> **写给 AI：** 这份文件是一份完整的执行任务。请你读完全文再动手，按顺序执行，
> 不要跳步。你面对的是一位真人用户，他不知道内部实现，也不需要知道——
> 你要替他做决定、替他检查、用大白话跟他汇报。

---

## 这是什么

Codex Ultra 是一个跑在用户本机的适配层，让 Codex 能用上 GPT 以外的模型
（DeepSeek、Gemini、Claude、Grok、MiniMax、Kimi、GLM 等），
并给这些模型补齐 Codex 的原生能力：自动上下文压缩、工具调用、图片桥接。

**它不是模型网关。** 模型来自用户自己的网关（比如自建的 Sub2API）。
Codex Ultra 只做本机侧的适配和转发。

---

## 动手前，先确认三件事

```bash
sw_vers -productVersion          # 需要 macOS 13 或更新
python3 --version                # 需要 3.11 或更新
git --version
```

任何一项不满足，**停下来告诉用户缺什么**，不要试图绕过去。
特别提醒：非 macOS 只能手动跑适配层，没有一键安装，如实说明。

---

## 然后，问用户两个问题

**问题一：你的网关地址是什么？**

形如 `https://xxx.example/v1`。用户可能不知道，让他去网关后台找。
**不要猜，不要编，不要拿你见过的任何地址试。**

**问题二：用哪个模型做上下文压缩？**

这个模型必须真实存在于他的网关里。判断标准和推荐方式见下面第 3 步。

---

## 执行步骤

### 1. 把源码拉到本地

```bash
cd ~/Downloads
git clone https://github.com/noonwake-ai/codex-ultra.git
cd codex-ultra
```

### 2. 帮用户设置密钥（这一条必须小心）

**绝对不要**把密钥写进任何文件、任何命令历史、任何日志，也不要 `echo` 出来。

让用户自己在当前终端里执行：

```bash
export CODEX_ULTRA_API_KEY="你的网关密钥"
```

如果用户已经把密钥贴在聊天窗口里发给你了，**明确提醒他**：
密钥已经留在聊天记录里了，建议去后台轮换一次，以后改用环境变量。

### 3. 生成模型目录

```bash
python3 build_catalog.py --gateway <用户给的网关地址> --out models.json
```

它会返回一份摘要：读到多少模型、分属哪些厂商、哪些需要图片桥接。

- 如果返回 `gateway_http_401` / `403`：密钥不对或没权限，让用户核对，**不要换别的密钥试**。
- 如果返回 `gateway_returned_non_json`：这个地址不是 Codex Ultra 认的接口，
  让用户确认地址（常见错误是漏了 `/v1` 或者进到了登录页）。
- 如果返回 `requested_model_not_offered:xxx`：用户的 key 看不到这个模型。

拿到模型列表后，**由你来推荐压缩模型**：挑一个推理档位齐全、名字里带
`flash` / `mini` / `lite` 这类轻量标记的；如果分不清，就把候选列给用户让他选。
把推荐理由用一句话说清楚。

### 4. 跑一遍项目自带的测试（不要跳过）

```bash
python3 -m unittest discover -s . -p 'test_*.py'
```

应该是 `OK`。这一步完全离线，不会调用任何付费模型。
如果不通过，把失败信息原样告诉用户，并停下来——**不要在测试失败的情况下继续安装**。

### 5. 安装

```bash
python3 install.py \
  --upstream <网关地址> \
  --compactor-model <第 3 步选定的模型> \
  --compactor-effort medium
```

如果用户在 macOS 上用 CC Switch 管理供应商，加上：

```bash
  --cc-switch-db ~/.cc-switch/cc-switch.db
```

这一步会做四件事（你要提前跟用户讲明白）：

1. 在 `~/Library/Application Support/Codex Ultra/` 建一个独立运行环境
2. 建一个登录时自启的后台服务
3. 往系统钥匙串写入一个**加密用**的密钥（不是他的 API key）
4. **只改配置里的一个 `base_url`**，把请求指到本机适配层

系统可能会弹钥匙串授权框，让用户点允许。**这是预期的，不是异常。**

### 6. 验证

```bash
python3 configure.py status
```

关键看 `routed_locally` 是不是 `true`。

然后再确认服务是活的：

```bash
curl -s http://127.0.0.1:15731/health
```

返回里应该有 `"status":"ok"`，以及当前生效的 `media_models` 列表。

---

## 装完要跟用户说什么

**用大白话说这四件事：**

1. **装好了，需要重开 Codex。** 现有对话不会丢，新开一个对话就能在模型列表里看到他的模型。
2. **想反悔随时可以，一条命令：**
   ```bash
   python3 configure.py rollback
   ```
   只还原那一行网络地址，他之后改的其它配置一个都不会动。
   安装器结束时会打印一条带完整路径的 `rollback_command`——**把那条原样转述给用户**，
   他以后就不用记路径了。
3. **密钥和聊天记录没被动过。** Codex Ultra 只做转发，不改密钥、不碰历史。
4. **出问题找谁**：把报错贴回 GitHub issue。

---

## 几条硬规矩（违反会伤害用户）

- **不要打印、不要上传、不要提交任何密钥。** 遇到疑似密钥的文件，停下来说清楚。
- **不要替用户改网关配置、DNS、代理。** 本项目只动本机一个 `base_url`。
- **不要静默降级。** 某个模型不支持图片、某个路由不稳定，直说，不要假装成功。
- **不要在测试失败时继续。** 先修，或者如实报告卡在哪一步。
- **不要改用户的模型选择、思考等级、技能、插件、MCP 设置。** 安装器不会碰，你也不要碰。
- **不要为了让流程跑通而修改项目源码。** 如果真的遇到 bug，请用户去提 issue，
  而不是在他机器上打补丁。

---

## 卡住了怎么办

| 现象 | 说明 |
|---|---|
| `upstream_must_be_https` | 网关地址必须是 `https://` 开头 |
| `upstream_must_not_be_loopback` | 网关地址不能填 `localhost` / `127.0.0.1`，会形成代理环路 |
| `service_not_healthy_route_unchanged` | 服务没起来。**客户端配置没被改动**，是安全的失败。看 `~/Library/Application Support/Codex Ultra/service-error.log` |
| `already_installed_use_configure_status` | 之前装过。让用户先 `python3 configure.py status` 看状态，不要覆盖 |
| 端口 15731 被占用 | 换端口：`--port 15741`，同时 `configure.py apply --local http://127.0.0.1:15741` |

完整说明见 [安装与部署](INSTALL.md)，模型能力策略见 [模型能力表](MODELS.md)。

---

**现在开始。** 第一步是问用户那两个问题。
