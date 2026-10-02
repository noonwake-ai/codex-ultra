# The image byte layer: what happens to pictures in a long conversation

> One page, one question: **what happens to images in a long session, when does it cost
> money, and how do you turn it off.**

## Why it exists

An image's *size* and its *token cost* are different things. A 4K screenshot is a few
thousand tokens but several megabytes on the wire. Video work, image dumps and screenshot
debugging push a request past 20-40 MB quickly: slow to upload, easy for a gateway to
truncate, and it wastes the long context you are paying a third-party model for. The byte
axis needs its own lever.

## Three layers, in order

| Layer | Default | Costs | What it does |
| --- | --- | --- | --- |
| **Transcode** | on | nothing | Re-encodes only: picks the smallest of lossless (lossless WebP / PNG) and lossy (WebP / JPEG). Resolution, cropping and `detail` never change, and a lossy candidate must pass two fidelity gates (mean pixel error, chroma/thin-line loss) or it is rejected. The original bytes are always a candidate, so the layer can only ever make a request smaller |
| **Budget transcription** | off | one vision call per replaced image | When the media still exceeds `media_budget_bytes` (4 MiB by default) the **oldest frames only** become a text description (timecode, shot, composition, colours, on-screen text) plus a local path to the original. The newest turn keeps real pixels |
| **Pre-warm** | off | same, paid early | Transcribes images in the background as soon as they arrive, so a later replacement never waits inside the forwarding path |

`media_max_image_bytes` (2 MiB of base64 payload, default) is separate: some upstream
accounts reject a single oversized image outright, so a history image above it is replaced
regardless of the budget.

## Why the paid layers are off by default

They call **your** gateway and spend **your** credits. An open-source project has no business
making that decision for you, so those two layers are opt-in. Transcoding is free, so it is on.

## Turning them on

Edit `config.json` in the service directory and restart:

```json
{
  "media_budget_enabled": true,
  "media_prewarm_enabled": true,
  "media_vision_model": "deepseek-flash"
}
```

```sh
launchctl kickstart -k "gui/$(id -u)/ai.codexultra.local-adapter"
curl -s http://127.0.0.1:15731/health
```

`media_vision_model` should be a cheap image-capable model on your gateway. The call uses the
`minimal` reasoning effort, JSON structured output and a 1600-token cap.

`/health` shows `media_images_seen`, `media_bytes_before` -> `media_bytes_after`,
`media_last_decisions`, `media_last_skips`, `media_transcribe_calls`,
`media_last_budget` / `media_last_budget_route`, `media_budget_no_original` and
`media_transcode_pruned`. A given image is paid for once: the cache key is the image's
SHA-256 plus the contract namespace (model, JSON mode, prompt schema).

## A restart no longer re-encodes anything

Transcode results are also written to `transcode-cache/` beside the config file (a *sibling* of
`media-cache/`, never inside it, so the originals store keeps owning every file under its own
root). After a restart or a SIGTERM, a retry hits the cached results instead of re-encoding the
whole history — measured on 2026-10-02: re-encoding 48 frames costs ~33 s of CPU, on exactly the
retry path that can least afford it. Entries carry a versioned envelope, writes are atomic, and a
corrupt entry only counts as an error without failing the request.

## The cache is bounded

Transcription index entries expire after 30 days and the index keeps at most 20 000 entries.
The original-image directory is trimmed to 512 MiB oldest-first, never touching a file younger
than 24 hours (the conversation being served must keep its own re-read source). Only files this
service wrote are counted or deleted; set both limits to 0 to keep everything.

## Known limits

- The transcription is a lossy stand-in: it preserves *what the frame was*, not pixel detail.
  Ask the model to read the original path when exact pixels matter.
- Originals live on this machine only; moving machines or clearing the cache ends that path
  (the text in history stays, so the conversation is not damaged).
- The wall clock (120 s) is soft: an in-flight call is not cancelled, but its result is cached
  for the next request.
- Pillow is required. Without it the whole layer is skipped and `media_last_decisions` reads
  `no_pillow`.
- The layer never changes tool declarations, instructions or any other protocol field.
