# 安全说明

Codex Ultra 会在你的机器上经手 API 密钥和对话内容，所以这里把边界写清楚。

[返回主页](README.md) · [参与贡献](CONTRIBUTING.md)

---

## 报告问题

请走 GitHub 的私密报告入口，不要开公开 issue：

<https://github.com/noonwake-ai/codex-ultra/security/advisories/new>

请附上：受影响的版本或 commit、复现步骤、你能观察到的影响范围。
**不要在报告里粘贴真实密钥**，把 key 换成 `sk-REDACTED` 这样的占位串。

## 设计上的边界

| 项目 | 现状 |
|---|---|
| 监听地址 | 只监听 `127.0.0.1`，不监听 `0.0.0.0` |
| 密钥落盘 | 不落盘、不写日志、不进压缩结果 |
| 加密检查点 | AES-GCM 加密，密钥存放在系统 Keychain，只有本机能解 |
| 上游协议 | 安装器强制 HTTPS，并拒绝把回环地址当上游，避免代理环路 |
| 遥测 | 没有。项目不向任何地方上报数据 |
| 仓库扫描 | CI 扫描密钥、内部主机名、私有 IP 和个人路径，没有豁免文件 |

## 不在范围内

- 你上游网关自身的安全，那是网关的责任。
- 你自己贴到 issue、截图或日志里的密钥。
- 模型返回的内容本身。

## 用之前请自己确认

- 你信任自己配置的那个网关地址。Codex Ultra 只做转发和格式转换，
  不判断上游是否可信。
- 你清楚模型请求会离开本机，发送到你填写的那个地址。

---

## English

Report vulnerabilities through GitHub's private advisory form, never in a
public issue, and never paste a real key into a report.
