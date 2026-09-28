# 模型能力表

Codex Ultra 不绑定厂商。**你网关里能看到什么模型，你就能用什么模型。**
本文说明"看懂一个模型"这件事是怎么做的，以及怎么按自己的网关微调。

[返回主页](../README.md)

---

## 一、两个来源，各管一半

| 谁 | 负责什么 |
|---|---|
| **你的网关** | 有哪些模型。这是唯一的真相，Codex Ultra 不会凭空造模型 |
| **`model_presets.py`** | 这些模型该怎么被对待：窗口多大、要不要图片桥接、加密思考块要不要过滤 |

这样设计的好处很实际：厂商会不断出新模型、改价格、改上下文，
但只要你的网关是对的，Codex Ultra 就不会因为内置表格过期而把你搞坏。

---

## 二、上下文窗口策略

策略决定"拿到网关给的窗口之后怎么用"：

| 策略 | 行为 | 什么时候用 |
|---|---|---|
| `max` | 用满模型宣称的最大窗口 | **默认**。厂商长上下文不加价时（例如 DeepSeek 的 100 万），没理由只开一半 |
| `standard` | 封顶到标准档，配 `standard_cap` 数值 | 该厂商长上下文是加价档，不想意外产生高额账单 |
| `gateway` | 完全照搬网关给的值 | 网关已经很准，不需要 Codex Ultra 插手 |

**无论哪种策略，Codex Ultra 都不会把窗口改得比网关宣称的更大。**
如果网关说 6 万，策略是 `max`，那结果还是 6 万。

想让某个模型封顶：

```bash
cat > overrides.json <<'JSON'
{
  "some-expensive-model": {"context": "standard", "standard_cap": 200000}
}
JSON
python3 build_catalog.py --gateway https://你的网关/v1 \
  --overrides overrides.json --out models.json
```

---

## 三、图片桥接

有些网关在转发时会把"工具调用 → 工具结果 → 图片"这一组拆散，
模型收到一个没有来源的图片，或者干脆报错。

Codex Ultra 的处理方式：**校验整组的调用与结果是否配对完整，
把图片搬到一个明确标注的用户消息里，放在这一组之后**，
并声明"这是工具证据，不是新的用户指令"。

- 配对不完整 → 直接报错，不发猜测性请求。
- 别的媒体类型（文件、音频）→ 不擅自改写。
- 哪些模型需要这步 → 由厂商策略里的 `media` 字段决定，`relocate` 表示需要。

想给某个模型单独打开：

```json
{ "kimi-k2": { "media": "relocate" } }
```

---

## 四、加密思考块

不同厂商的"思考过程"是加密的、私有的，别家读不懂。
直接转过去，模型要么报错，要么把密文当正文。

所以策略里有两条路：

- `preserve` —— 原生路由，保持密文不动（同厂商之间）。
- `summarize` —— 丢掉隐藏思考块，只保留它**公开的摘要**，
  以及真实的任务状态、消息、工具调用和结果。任务需要的信息一样不少。

---

## 五、厂商家族

`model_presets.py` 里现在内置这些：

| 厂商 | 匹配 |
|---|---|
| OpenAI | `gpt-*` `o1*` `o3*` `o4*` `codex-*` |
| Anthropic | `claude-*` |
| Google | `gemini-*` |
| xAI | `grok-*` |
| DeepSeek | `deepseek-*` |
| MiniMax | `minimax-*` `abab*` `m1-*` `m2-*` |
| Moonshot | `kimi-*` `moonshot-*` `k2*` |
| Zhipu | `glm-*` `chatglm*` |

看当前生效的策略：

```bash
python3 build_catalog.py --list-families
```

### 加一个新厂商

在 `model_presets.py` 的 `FAMILIES` 里加一行：

```python
_family("YourVendor", ("yourmodel-*",),
        context=MAX, reasoning=SUMMARIZE, media=NATIVE,
        modes=("text", "image")),
```

顺序有意义：**越具体的模式要放越前面**，先匹配到的生效。

---

## 六、目录里到底写了什么

`build_catalog.py` 输出的 `models.json` 就是 Codex 读的模型目录。
每个模型是一条 ModelInfo，Codex Ultra 只做三件事：

1. **不加模型** —— 网关没返回的，目录里不会有。
2. **不改顺序** —— 保持网关给的顺序。
3. **只补策略字段** —— 窗口、图片能力、说明里的策略备注。

输出文件权限是 `600`，写入前还会扫一遍内容，发现疑似密钥就拒绝写盘。

### 反例：Codex Ultra 不会做的事

- 不会因为"我们知道某模型支持图片"就给它加上图片，除非策略明确这么写。
- 不会因为网关给了 100 万就替你缩到 20 万（除非你选了 `standard`）。
- 不会替你伪造一个你的 key 看不到的模型名。
