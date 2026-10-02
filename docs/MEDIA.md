# 图片字节层：长会话里的图片怎么处理

> 这一页讲一件事：**图片在长会话里怎么被处理、什么时候会花钱、怎么关掉。**

## 它解决什么

图片的「体积」和「token」是两回事。一张 4K 截图在 token 上只有几千，在请求体里却是好几兆。
剪视频、传素材、截图调试这类场景，一个会话很容易把请求体堆到 20–40 MB：上传慢、容易被
网关截断，也让第三方模型的长上下文白买。所以字节这一轴要有自己的杠杆。

## 三层，按顺序

| 层 | 默认 | 花钱吗 | 做什么 |
| --- | --- | --- | --- |
| **转码** | 开 | 不花钱 | 只换更小的编码：无损（WebP 无损 / PNG）与有损（WebP / JPEG）里挑最小的。分辨率、裁剪、`detail` 字段都不动；有损候选必须先过两道保真闸门（平均像素误差、色度/细线损失），过不了就退回无损或原图。原图永远是一个候选，所以这一层只会更小，不会更大 |
| **预算转写** | 关 | 每张图一次视觉调用 | 转码之后仍超过 `media_budget_bytes`（默认 4 MiB）时，**只替换最老的几张**，直到落进预算；最新一轮的图永远保留真实像素。被替换的图变成一段文字（时间码、镜头、构图、颜色、画面文字）+ 本机原图路径，模型需要时可以再读原图 |
| **后台预热** | 关 | 同上，提前付 | 图片一进请求就在后台转写，等它将来变「老图」时缓存里已有文字，转发路径不用现场等 |

**单图安全阈值 `media_max_image_bytes`（默认 2 MiB）**是另一件事：某些上游账号会直接拒收过大的
单张图，所以超过它的历史图片会被无条件转写。它按 base64 载荷计，2 MiB 载荷约等于 1.5 MiB 原文件。

## 为什么后两层默认是关的

它们会调用**你自己的网关**、花**你自己的额度**。开源项目没有理由替你决定这笔支出，所以默认关闭，
想用就打开——见下面「怎么开」。转码不花钱，所以默认开着。

## 怎么开

编辑服务目录下的 `config.json`，把要用的层打开，再重启服务：

```json
{
  "media_budget_enabled": true,
  "media_prewarm_enabled": true,
  "media_vision_model": "deepseek-flash"
}
```

`media_vision_model` 用你网关里**便宜且支持图片**的模型；视觉调用默认走 `minimal` 思考档、JSON
结构化输出、1600 tokens 上限，是这条链路上最便宜的一档。

重启：

```sh
launchctl kickstart -k "gui/$(id -u)/ai.codexultra.local-adapter"
curl -s http://127.0.0.1:15731/health
```

`/health` 上看这些数：

| 计数 | 含义 |
| --- | --- |
| `media_images_seen` / `media_images_replaced` | 见过多少张图 / 其中多少张被转码 |
| `media_bytes_before` → `media_bytes_after` | 字节层的累计效果 |
| `media_last_decisions` | 最近一次转码决策（`webp` / `png` / `original` / `no_pillow` …） |
| `media_last_skips` | 被保真闸门拒绝的候选（`chroma_webp` / `chroma_jpeg` / `gate_error` / `error`） |
| `media_transcode_errors` | 媒体层自身的异常次数（不等于「没有拒绝」，拒绝在 `media_last_skips`） |
| `media_transcode_fidelity_errors` | 保真闸门**自己跑不起来**的次数，正常应长期为 0 |
| `media_transcribe_calls` | 累计付费视觉调用次数 |
| `media_last_budget` / `media_last_budget_route` | 最近一次预算评估，以及它发生在哪条路径 |
| `media_budget_no_original` | 因为拿不到「模型当时看到的原图」而**拒绝写盘、保留图片**的次数，正常应长期为 0 |
| `media_budget_deadline_hits` | 撞到墙钟上限（默认 120 秒）的次数 |
| `media_transcode_pruned` | 缓存清理计数（索引条数 / 原图个数 / 释放字节） |

一张图只付一次：缓存键是图片内容的 SHA-256 + 契约命名空间（模型、是否 JSON 模式、提示词 schema），
同一张图跨会话复用；换成另一个视觉模型才会重新付费。

## 缓存不会无限长大

- 转写索引超过 `media_cache_index_ttl_days`（默认 30 天）在下次写入时清理，最多保留
  `media_cache_index_max_entries`（默认 20000）条。
- 原图目录超过 `media_cache_originals_max_mb`（默认 512 MiB）时，从最老的文件开始删，
  但**不删 `media_cache_originals_min_age_hours`（默认 24 小时）以内的文件**——正在被使用的
  那次会话必须还能读回它的原图。
- 只有本服务自己写下的文件参与计数和清理；索引、备份、手工放进来的文件既不占额度也不会被删。
- 想永不删除：把这两个上限写 0。

## 已知边界

- 转写是**有损**的信息替代：它保住「这帧是什么」，保不住像素级细节。需要精确像素时让模型读回原图路径。
- 原图只在本机、本目录里；换电脑或清缓存后这条路失效（历史里那段文字仍在，不会破坏会话）。
- 墙钟是**软**上限：到点后请求不再等待还没回来的调用，但在飞的调用无法取消；它们的结果会被后台
  写进缓存，下一次请求直接命中。
- 需要 Pillow。装了 `requirements.txt` 就有；没装的话这一层会整体静默跳过，
  `/health` 的 `media_last_decisions` 会全是 `no_pillow`。
- 这一层**不会**改动 Codex 下发的工具声明、指令或其它协议字段。
