"""Byte budget for history-carrying media: transcribe old images, never erase them.

Why this layer exists
---------------------
Transcoding (``media_transcode``) only helps where a better encoding exists. A
dense screenshot that is already efficient can still be half a megabyte, and a
long session sends every image again on every turn. Token-based compaction never
sees those bytes, so the request body grows without bound.

Why *transcription* and not a placeholder
-----------------------------------------
Measured, 8 rounds per arm: replacing an
old image with a bare placeholder made the model retract its own earlier, correct
description in **8/8** rounds; replacing it with a transcription of that image did
so in **0/8**. A placeholder does not say "you saw less", it says "what you said
was made up".

Which media may be replaced (adversarial an earlier round)
----------------------------------------------------
Only media the model has *already acted on* is replaceable. The newest item (what
the model is being asked about right now), the newest user item, and the newest
image itself are protected unconditionally, so a single-image request, a
multi-image request, and an explicit "read that file again" tool result all reach
the model as real pixels. A transcription of the current frame is not a
substitute for the current frame.

How much is replaced (F11)
--------------------------
The replacement set is the *smallest* oldest-first prefix whose estimated saving
covers the shortfall, verified after each batch against the real text size. The
earlier implementation transcribed every image outside the recent-keep window as
soon as the total crossed the budget: 11 images 4,459,328 bytes against a
4,194,304 budget replaced 6 images when 1 was enough.

Units (F09)
-----------
Every size here is a UTF-8 byte count of the serialised part. Mixing Python
character counts with base64 lengths made a Chinese transcription look like a
saving while the real request grew by 628 bytes.

Design rules
------------
* The newest media stays as real images.
* Only images are swapped, and only the payload of an ``input_image`` part becomes
  an ``input_text`` part. Items and parts are never removed or reordered.
* The original bytes are written next to the transcription so the model can ask to
  re-read the real pixels at a known path.
* If a transcription is unavailable, the image is **kept** and the request is
  allowed to stay over budget; a silent placeholder is never emitted.
* A whole-pass wall-clock deadline bounds how long a request can wait for vision
  calls (F12); the images are kept once it expires.
* Nothing here mutates its input.
"""
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import media_transcode

TRANSCRIPTION_PREFIX = '[earlier image, replaced by its transcription]'
# Short, and deliberately blunt: the receiver must know the text is machine read
# and that the path above is the only way to an exact value.
TRANSCRIPTION_CAVEAT = '[machine transcription; exact values may be misread - re-read the source file]'
# Below this a replacement text is usually bigger than the image itself, so a
# vision call would be paid for nothing. Measured fixture: a solid 320x200 PNG is
# ~666 bytes, smaller than any useful transcription. Counted in base64 payload
# bytes, i.e. the encoded size on the wire.
MIN_REPLACE_BYTES = 512
DEFAULT_WORKERS = 4
MAX_WORKERS = 12
# Items whose media is treated as "in use right now" and never replaced.
DEFAULT_PROTECT_ITEMS = 1
# Fallback transcription size for the replacement plan, used until enough real
# transcriptions exist to measure. 512 bytes is the measured middle of the
# distribution for both prose and json answers.
DEFAULT_TEXT_BYTES = 512
# Verification round size after the estimated first batch: a slightly larger
# chunk keeps the pool busy when the estimate was too optimistic.
VERIFY_CHUNK = 8
# The cache contract. Cached text is only reusable when it was produced under the
# same schema, model, prompt and output mode (F05).
MEDIA_SCHEMA_VERSION = 'json-v2'
REQUIRED_KEYS = ('timecode', 'slate', 'other_text', 'composition', 'colours')
LIST_KEYS = ('other_text', 'colours')
TEXT_KEYS = ('timecode', 'slate', 'composition')
MIN_PRESENT_KEYS = 3
PROMPT = (
    'Transcribe this frame for an editing log. Return ONLY a json object with these keys: '
    '"timecode" (verbatim string, empty if none), "slate" (verbatim string, empty if none), '
    '"other_text" (array of every other visible string, verbatim), "composition" (one sentence), '
    '"colours" (short array). Use "unclear" for anything you cannot read with confidence and '
    'never guess. No prose outside the json.'
)


def transcription_namespace(model, json_mode, prompt=PROMPT, schema=MEDIA_SCHEMA_VERSION):
    """Cache identity: a transcription is only reusable under the same contract."""
    digest = hashlib.sha256((prompt or '').encode('utf-8')).hexdigest()[:16]
    return '%s|%s|%s|%s' % (schema, model or '', 'json' if json_mode else 'text', digest)


def _strip_fence(text):
    candidate = text.strip()
    if not candidate.startswith('```'):
        return candidate
    body = candidate.strip('`')
    body = body.split('\n', 1)[-1] if '\n' in body else body
    candidate = body.rsplit('```', 1)[0].strip()
    if candidate.startswith('json'):
        candidate = candidate[4:].lstrip()
    return candidate.strip()


def transcription_object_is_usable(obj):
    """Is this parsed answer a transcription of the frame, or an empty shell?

    Syntax alone was not enough to trust an answer, so ``{}``,
    ``{"error": "cannot read image"}`` and ``{"timecode": 47}`` all qualified and
    the image was replaced by a source path plus nothing.
    """
    if not isinstance(obj, dict) or not obj:
        return False
    present = [key for key in REQUIRED_KEYS if key in obj]
    if len(present) < MIN_PRESENT_KEYS:
        return False
    for key in present:
        value = obj[key]
        if key in LIST_KEYS:
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                return False
        elif not isinstance(value, str):
            return False
    if any(isinstance(obj.get(key), str) and obj[key].strip() for key in TEXT_KEYS):
        return True
    return any(obj.get(key) for key in LIST_KEYS)


def normalise_transcription(text, json_mode=True):
    """One validator for new answers *and* for everything read back from cache.

    Returns the text that may be written into history, or ``None`` when the answer
    is unusable and the image has to be kept.
    """
    if not isinstance(text, str):
        return None
    if not json_mode:
        return text.strip() or None
    candidate = _strip_fence(text)
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    if not transcription_object_is_usable(parsed):
        return None
    # Re-serialise: whatever reaches the history is bare, canonical json.
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)


def image_parts(items):
    """Yield ``(item_index, field, part_index, part)`` for every input_image part."""
    if not isinstance(items, list):
        return
    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        for field in ('content', 'output'):
            parts = item.get(field)
            if not isinstance(parts, list):
                continue
            for part_index, part in enumerate(parts):
                if isinstance(part, dict) and part.get('type') == 'input_image':
                    yield item_index, field, part_index, part


def payload_bytes(part):
    """Base64 payload length of an image part, i.e. its size on the wire."""
    url = part.get('image_url') if isinstance(part, dict) else None
    if not isinstance(url, str):
        return 0
    head, _, payload = url.partition(',')
    return len(payload) if ';base64' in head else len(url)


def part_bytes(part):
    """UTF-8 bytes this part contributes to the serialised request (F09).

    Images are measured by their base64 payload and text by its UTF-8 length, both
    including the (tens of bytes) json envelope, so an image and its replacement
    text are compared in the same unit.

    The image envelope is a constant 48 bytes rather than a measured
    ``json.dumps``. Every decision is therefore made on a consistent estimate and the
    final size is verified against the real serialisation, but ``bytes_before`` /
    ``bytes_after`` in the stats are approximate, not exact.
    """
    if isinstance(part, dict) and part.get('type') == 'input_image':
        return payload_bytes(part) + 48
    try:
        return len(json.dumps(part, ensure_ascii=False).encode('utf-8'))
    except (TypeError, ValueError):
        return 0


def media_bytes(items):
    return sum(payload_bytes(part) for _, _, _, part in image_parts(items))


def total_bytes(items):
    """Estimated wire bytes of every image part, as ``part_bytes`` counts them."""
    return sum(part_bytes(part) for _, _, _, part in image_parts(items))


_PATH_LOCKS = {}
_PATH_LOCKS_LOCK = threading.Lock()


# Bounded-cache policy. The store used to grow without
# limit: every considered image that got replaced left a file behind and every
# transcription stayed in the index forever. A colleague who edits video would
# accumulate gigabytes they never look at.
DEFAULT_INDEX_TTL_SECONDS = 30 * 24 * 3600
DEFAULT_INDEX_MAX_ENTRIES = 20000
DEFAULT_ORIGINALS_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_ORIGINALS_MIN_AGE_SECONDS = 24 * 3600
PRUNE_INTERVAL_SECONDS = 600
# A write's temporary file only matters while the write is in flight; anything older
# is the corpse of a killed process.
TEMPORARY_GRACE_SECONDS = 3600
# Files this store wrote into its own directory: ``<32 hex>.<ext>`` for a re-read
# source, plus the ``.<pid>.<tid>.tmp`` name a write uses before ``os.replace``. A
# process killed mid-write leaves one of those behind, and a file that matches no
# pattern is neither counted nor deleted -- the index and an operator's backup in
# the same directory are not ours to remove.
ORIGINAL_NAME_RE = re.compile(
    r'^[0-9a-f]{32}\.(?:jpg|jpeg|png|webp|gif|bmp|bin)(?:\.\d+\.\d+\.tmp)?$')
# How long the post-deadline drain waits for calls the request gave up on before it
# writes whatever arrived and exits.
DEFAULT_LATE_CACHE_SECONDS = 300
_PRUNE_STATE = {}
_PRUNE_LOCK = threading.Lock()


def _due_for_prune(path, now):
    """Throttle the directory scan: at most one sweep per store, per 10 minutes."""
    with _PRUNE_LOCK:
        if now - _PRUNE_STATE.get(path, 0.0) < PRUNE_INTERVAL_SECONDS:
            return False
        _PRUNE_STATE[path] = now
        return True


def _path_lock(path):
    """One lock per store directory, shared by every MediaStore instance.

    Two adapters (or two requests) can build separate MediaStore objects over the
    same directory; without a shared lock they raced on the index temp file and
    the loser got FileNotFoundError.
    """
    with _PATH_LOCKS_LOCK:
        lock = _PATH_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[path] = lock
        return lock


_EXECUTORS = {}
_EXECUTOR_LOCK = threading.Lock()
_INFLIGHT = {}
_INFLIGHT_LOCK = threading.Lock()


def _executor(workers, purpose='request'):
    """Pool per purpose: pre-warm must never occupy the workers a request waits on.

    Sharing one pool makes background pre-warm delay the request that is waiting
    on its own transcriptions. Measured directly (8 pre-warm jobs queued, then a
    request needing 4 transcriptions, 4 workers, 2.0s per vision call): shared
    pool 5.81s / 5.81s, split pools 2.01s / 2.01s -- the floor. An end-to-end
    turn-by-turn A/B does *not* show it, because a 30s gap lets the pre-warm
    queue drain first, so this is deliberately measured at the pool instead.
    """
    workers = max(1, min(int(workers or DEFAULT_WORKERS), MAX_WORKERS))
    with _EXECUTOR_LOCK:
        pool = _EXECUTORS.get(purpose)
        if pool is None or getattr(pool, '_max_workers', 0) < workers:
            pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='media-%s' % purpose)
            _EXECUTORS[purpose] = pool
        return pool


def reset_executor():
    """Drop the shared pools (tests and operator restarts)."""
    with _EXECUTOR_LOCK:
        _EXECUTORS.clear()
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()


def _run_and_store(transcribe, raw, mime, digest, store, key, source):
    """Worker body: run one transcription, persist it, and clear its in-flight slot."""
    try:
        text = transcribe(raw, mime, digest)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(key, None)
    if store is not None and text:
        try:
            store.save_text(digest, text, source=source)
        except Exception:
            pass
    return text


def _upstream_future(purpose, digest):
    """Which already-started call, if any, this job may wait on.

    A request may share with another request -- both are on someone's critical
    path and they use the same pool. It may **not** share with a speculative
    pre-warm call: measured, a pre-warm call that hung for 10s made the request
    that shared it wait 9.70s instead of issuing its own 0.05s call. Background
    work must never be able to slow a request down.
    """
    if purpose == 'prewarm':
        return _INFLIGHT.get(('request', digest)) or _INFLIGHT.get(('prewarm', digest))
    return _INFLIGHT.get((purpose, digest))


def _submit(jobs, transcribe, workers, store, purpose='request'):
    """Start (or join) a future per digest; returns ``{digest: future}``."""
    pool = _executor(workers, purpose)
    futures = {}
    for digest, raw, mime in jobs:
        with _INFLIGHT_LOCK:
            future = _upstream_future(purpose, digest)
            if future is None:
                key = (purpose, digest)
                future = pool.submit(_run_and_store, transcribe, raw, mime, digest, store, key, purpose)
                _INFLIGHT[key] = future
        futures[digest] = future
    return futures


def schedule_transcriptions(jobs, transcribe, workers=DEFAULT_WORKERS, store=None, purpose='prewarm'):
    """Start transcriptions in the background and return immediately.

    This is the pre-warm path: images are transcribed while the turn is still
    being served, so a later request that has to replace them finds the text in
    the on-disk cache instead of paying for it inside the forwarding path.
    """
    if not jobs:
        return 0
    started = 0
    with _INFLIGHT_LOCK:
        pending = {digest for _, digest in _INFLIGHT}
    for digest, _, _ in jobs:
        if digest in pending:
            continue
        if store is not None:
            try:
                if store.load_text(digest):
                    continue
            except Exception:
                pass
        started += 1
    if started:
        _submit(jobs, transcribe, workers, store, purpose=purpose)
    return started


def _split_job(part):
    url = part.get('image_url')
    parsed = media_transcode._split_data_url(url) if isinstance(url, str) else None
    if parsed is None:
        return None
    mime, raw = parsed
    return mime, raw, hashlib.sha256(raw).hexdigest()


def prewarm_jobs(items, min_bytes=64 * 1024, limit=6, skip_newest=1, store=None,
                 protect_items=0):
    """Pick images worth transcribing ahead of time.

    The oldest frames are the ones a budget pass replaces first, so they are
    queued first; the newest image is skipped because it is the one most likely
    to stay as real pixels.

    Images that already have a transcription are dropped **before** ``limit`` is
    applied. Applying the limit first was a real defect: once the oldest few
    images were warm they filled the per-request budget on every later turn, so
    every newer image stayed cold forever (measured -- with images 1-6 cached,
    ``prewarm_jobs`` kept returning exactly those six and scheduling zero).
    """
    positions = list(image_parts(items))
    if protect_items:
        protected = protected_positions(items, protect_items)
        positions = [position for position in positions if position[:3] not in protected]
    if skip_newest:
        positions = positions[:-skip_newest] if len(positions) > skip_newest else []
    candidates = []
    seen = set()
    for _, _, _, part in positions:
        parsed = _split_job(part)
        if parsed is None:
            continue
        mime, raw, digest = parsed
        if len(raw) < min_bytes or digest in seen:
            continue
        seen.add(digest)
        candidates.append((digest, raw, mime))
    if store is not None:
        warm = store.usable_digests()
        candidates = [job for job in candidates if job[0] not in warm]
    return candidates[:limit]


def transcribe_many(jobs, transcribe, workers=DEFAULT_WORKERS, deadline=None, store=None):
    """Transcribe ``(digest, raw, mime)`` jobs concurrently.

    A digest already being transcribed by another request is *shared* rather than
    repeated, which is what keeps a burst of identical long conversations from
    multiplying upstream vision calls. Returns ``{digest: text}`` for the jobs
    that succeeded; failures are simply absent, and a pass that runs out of time
    returns what it already has (F12).

    ``store`` receives every finished answer, including calls the caller stopped
    waiting for. A running HTTP request cannot be cancelled, so without this the
    money spent on an in-flight call at the deadline was simply thrown away.
    """
    futures = _submit(jobs, transcribe, workers, None)
    results = {}
    for digest, future in futures.items():
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            break
        try:
            text = future.result(timeout=remaining)
        except Exception:
            continue
        if text:
            results[digest] = text
    if deadline is not None and store is not None:
        leftover = {digest: future for digest, future in futures.items()
                    if digest not in results}
        _cache_late_results(store, leftover)
    return results


def _cache_late_results(store, futures, timeout=DEFAULT_LATE_CACHE_SECONDS):
    """Persist answers the caller stopped waiting for, in ONE batched write.

    An in-flight HTTP call cannot be cancelled, so the honest answer to "the pass
    ran out of time" is to keep the answer for the next turn rather than pay for a
    transcription and drop it. The drain runs on a daemon thread and only writes to
    the cache, so it can never lengthen the request it belongs to; the batching
    keeps the "one index rewrite per pass" property the request path depends on.
    """
    if store is None or not futures:
        return None

    def drain():
        entries = {}
        deadline = time.monotonic() + timeout
        for digest, future in futures.items():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                text = future.result(timeout=remaining)
            except Exception:
                continue
            if text:
                entries[digest] = text
        if entries:
            try:
                store.save_texts(entries, source='request')
            except Exception:
                pass

    thread = threading.Thread(target=drain, name='media-late-cache', daemon=True)
    thread.start()
    return thread


def _newest_user_index(items):
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if isinstance(item, dict) and item.get('role') == 'user':
            return index
    return None


def protected_positions(items, protect_items=DEFAULT_PROTECT_ITEMS):
    """Positions that must keep their real pixels (see the module docstring)."""
    positions = list(image_parts(items))
    protected = set()
    if not positions:
        return protected
    protected.add(positions[-1][:3])
    if protect_items and protect_items > 0:
        cutoff = max(0, len(items) - int(protect_items))
        for position in positions:
            if position[0] >= cutoff:
                protected.add(position[:3])
    newest_user = _newest_user_index(items)
    if newest_user is not None:
        for position in positions:
            if position[0] == newest_user:
                protected.add(position[:3])
    return protected


class MediaStore:
    """Disk store for original image bytes plus cached transcriptions.

    The cache identity is ``namespace`` (schema + model + output mode + prompt) and
    every read is validated with the same checker that guards new answers (F05).

    The store is bounded: index entries expire after
    ``index_ttl_seconds`` (0 keeps them forever) and the newest
    ``index_max_entries`` are kept; the original-image directory is trimmed to
    ``originals_max_bytes`` oldest-file-first, never touching a file younger than
    ``originals_min_age_seconds`` so the conversation being served right now keeps
    its re-read source.
    """

    def __init__(self, directory, namespace=None, model='vision', json_mode=False,
                 index_ttl_seconds=DEFAULT_INDEX_TTL_SECONDS,
                 index_max_entries=DEFAULT_INDEX_MAX_ENTRIES,
                 originals_max_bytes=DEFAULT_ORIGINALS_MAX_BYTES,
                 originals_min_age_seconds=DEFAULT_ORIGINALS_MIN_AGE_SECONDS):
        self.directory = str(directory)
        self.index_path = os.path.join(self.directory, 'transcriptions.json')
        self.lock = threading.Lock()
        self.model = model
        self.json_mode = bool(json_mode)
        self.namespace = namespace or transcription_namespace(model, self.json_mode)
        self.index_ttl_seconds = max(0, int(index_ttl_seconds or 0))
        self.index_max_entries = max(0, int(index_max_entries or 0))
        self.originals_max_bytes = max(0, int(originals_max_bytes or 0))
        self.originals_min_age_seconds = max(0, int(originals_min_age_seconds or 0))
        self.prune_stats = {'index_pruned': 0, 'originals_pruned': 0,
                            'originals_bytes_freed': 0}

    def _ensure(self):
        os.makedirs(self.directory, exist_ok=True)

    def original_path(self, digest, mime):
        extension = {'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/png': 'png'}.get(mime, 'bin')
        return os.path.join(self.directory, '%s.%s' % (digest[:32], extension))

    def save_original(self, digest, raw, mime):
        self._ensure()
        path = self.original_path(digest, mime)
        if os.path.exists(path):
            return path
        # Unique temp name: two concurrent requests storing the same image used to
        # share '<path>.tmp' and the loser raised FileNotFoundError on replace.
        temporary = '%s.%d.%d.tmp' % (path, os.getpid(), threading.get_ident())
        try:
            with open(temporary, 'wb') as handle:
                handle.write(raw)
            os.replace(temporary, path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            if not os.path.exists(path):
                raise
        if _due_for_prune(self.directory, time.time()):
            try:
                self.prune_originals()
            except Exception:
                pass
        return path

    def prune_originals(self, now=None):
        """Trim the original-image directory back to ``originals_max_bytes``.

        Oldest file first, and never a file younger than
        ``originals_min_age_seconds``: a replacement text names a file for the model
        to re-read, so the store must not delete the frame the conversation being
        served right now is about to point at. Returns the number of files removed;
        ``originals_max_bytes = 0`` disables the trim.

        Only files this store wrote are candidates: ``<32 hex>.<ext>`` for a re-read
        source, plus the ``.<pid>.<tid>.tmp`` name a write uses before its
        ``os.replace``. Temporaries are swept first when they are older than
        ``TEMPORARY_GRACE_SECONDS`` and are **never counted** towards the cap: the
        corpse of a killed write must not push the store into deleting a frame that a
        conversation can still re-read. Everything else in the
        directory -- the index, an operator's backup, a hand-copied file -- is neither
        counted nor deleted.
        """
        cap = self.originals_max_bytes
        if cap <= 0:
            return 0
        now = time.time() if now is None else now
        entries = []
        total = 0
        removed = 0
        try:
            names = os.listdir(self.directory)
        except OSError:
            return 0
        for name in names:
            if not ORIGINAL_NAME_RE.match(name):
                continue
            path = os.path.join(self.directory, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            if name.endswith('.tmp'):
                if now - info.st_mtime >= TEMPORARY_GRACE_SECONDS:
                    try:
                        os.unlink(path)
                    except OSError:
                        continue
                    removed += 1
                    self.prune_stats['originals_bytes_freed'] += info.st_size
                continue
            entries.append((info.st_mtime, info.st_size, path))
            total += info.st_size
        if total > cap:
            for mtime, size, path in sorted(entries):
                if total <= cap:
                    break
                if now - mtime < self.originals_min_age_seconds:
                    continue
                try:
                    os.unlink(path)
                except OSError:
                    continue
                total -= size
                removed += 1
                self.prune_stats['originals_bytes_freed'] += size
        self.prune_stats['originals_pruned'] += removed
        return removed

    def load_index(self):
        """The whole transcription index in one read.

        Callers that ask about many digests at once (pre-warm selection over a
        100-image history) must not re-parse the file once per image.
        """
        with _path_lock(self.index_path):
            try:
                with open(self.index_path, 'r', encoding='utf-8') as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}

    def usable_text(self, index, digest):
        """Cached text for ``digest``, or ``None`` when it is not reusable.

        Stale entries are left on disk (a rollback must still find them) but never
        used: the namespace has to match and the stored text has to pass the same
        validator as a fresh answer.
        """
        entry = index.get(digest) if isinstance(index, dict) else None
        if not isinstance(entry, dict):
            return None
        if entry.get('ns') != self.namespace:
            return None
        return normalise_transcription(entry.get('text'), self.json_mode)

    def usable_digests(self):
        index = self.load_index()
        return {digest for digest in index if self.usable_text(index, digest)}

    def cached_digests(self):
        return set(self.load_index())

    def load_text(self, digest):
        return self.usable_text(self.load_index(), digest)

    def _write(self, index, entries, source):
        """Merge ``entries`` into ``index`` and replace the file atomically."""
        stamp = int(time.time())
        for digest, text in entries.items():
            existing = index.get(digest)
            if (isinstance(existing, dict) and existing.get('ns') == self.namespace
                    and existing.get('source') == 'request' and source != 'request'):
                # A late speculative pre-warm answer must never overwrite the value a
                # request already used and showed to the model (F10).
                continue
            index[digest] = {'text': text, 'model': self.model, 'ns': self.namespace,
                             'source': source, 'at': stamp}
        self.prune_index(index, stamp)
        temporary = '%s.%d.%d.tmp' % (self.index_path, os.getpid(), threading.get_ident())
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump(index, handle, ensure_ascii=False)
        os.replace(temporary, self.index_path)
        return index

    def prune_index(self, index, now=None):
        """Drop expired entries and everything past the newest ``index_max_entries``.

        Expired entries were already unusable (namespace mismatch or a failed
        content check), so this only stops the index from growing without limit.
        ``index_ttl_seconds = 0`` keeps entries forever, which is what a rollback
        across a namespace change would need. Returns the number removed.
        """
        now = int(time.time()) if now is None else int(now)
        removed = 0
        if self.index_ttl_seconds:
            cutoff = now - self.index_ttl_seconds
            for digest in [d for d, entry in index.items() if _entry_stamp(entry) < cutoff]:
                index.pop(digest, None)
                removed += 1
        cap = self.index_max_entries
        if cap and len(index) > cap:
            ordered = sorted(index.items(), key=lambda kv: _entry_stamp(kv[1]))
            for digest, _entry in ordered[:len(index) - cap]:
                index.pop(digest, None)
                removed += 1
        self.prune_stats['index_pruned'] += removed
        return removed

    def save_texts(self, entries, source='request'):
        """Persist several transcriptions with ONE index read and ONE rewrite.

        ``apply_budget`` used to call ``save_text`` per image, so a 120-image
        request rewrote the whole index 120 times, and each lookup re-parsed it.
        """
        if not entries:
            return
        self._ensure()
        with _path_lock(self.index_path):
            try:
                with open(self.index_path, 'r', encoding='utf-8') as handle:
                    index = json.load(handle)
                if not isinstance(index, dict):
                    index = {}
            except (OSError, ValueError):
                index = {}
            self._write(index, entries, source)

    def save_text(self, digest, text, source='request'):
        self.save_texts({digest: text}, source=source)


def empty_stats():
    return {'considered': 0, 'replaced': 0, 'kept_over_budget': 0, 'transcribe_failures': 0,
            'cache_hits': 0, 'bytes_before': 0, 'bytes_after': 0, 'over_budget': False,
            'over_image_cap': 0, 'transcribe_jobs': 0, 'kept_too_small': 0,
            'kept_not_smaller': 0, 'protected': 0, 'deadline_hit': 0, 'rounds': 0,
            # Replacements refused because the caller did not hand over the bytes the
            # model was actually shown. Non-zero here means a call site
            # dropped its ``originals`` mapping.
            'kept_no_original': 0}


def _replacement_text(transcription, path):
    return ('%s source: %s\n%s\n%s' % (TRANSCRIPTION_PREFIX, path, transcription,
                                       TRANSCRIPTION_CAVEAT)).strip()


def _entry_stamp(entry):
    """``at`` of one index entry, or 0 when the entry is malformed/legacy."""
    if isinstance(entry, dict):
        try:
            return int(entry.get('at') or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _estimate_text_bytes(index, default=DEFAULT_TEXT_BYTES):
    sizes = []
    for entry in (index or {}).values():
        if isinstance(entry, dict) and isinstance(entry.get('text'), str):
            sizes.append(len(entry['text'].encode('utf-8')) + len(TRANSCRIPTION_CAVEAT) + 96)
    if len(sizes) < 5:
        return default
    sizes.sort()
    return sizes[len(sizes) // 2]


class _Candidate(object):
    """One image that may be replaced, with everything the plan needs."""

    __slots__ = ('item_index', 'field', 'part_index', 'digest', 'raw', 'mime', 'size',
                 'text', 'forced')

    def __init__(self, item_index, field, part_index, digest, raw, mime, size, text, forced):
        self.item_index = item_index
        self.field = field
        self.part_index = part_index
        self.digest = digest
        self.raw = raw
        self.mime = mime
        self.size = size
        self.text = text
        self.forced = forced

    @property
    def position(self):
        return (self.item_index, self.field, self.part_index)


def apply_budget(items, store, transcribe, budget_bytes, keep_bytes, enabled=True,
                 max_image_bytes=0, workers=DEFAULT_WORKERS, min_replace_bytes=MIN_REPLACE_BYTES,
                 protect_items=DEFAULT_PROTECT_ITEMS, deadline_seconds=None, source='request',
                 originals=None, originals_required=True):
    """Replace the fewest old images needed to bring the media inside the budget.

    ``transcribe(raw, mime, digest)`` must return the transcription text or raise.
    Returns ``(items, stats)``; the input is never mutated.

    ``originals`` maps the hash of each image in ``items`` to the ``(bytes, mime)``
    it was derived from, or to :data:`media_transcode.ORIGINAL_UNCHANGED` when the
    transcoder left it alone (see ``transcode_items``).

    **The default is to refuse.** ``originals_required`` defaults to ``True``, so a
    caller that does not hand over provenance cannot have a replacement written to
    disk: a digest missing from the mapping is treated as "the mapping was lost",
    and the image is kept instead of filing a re-encoded copy under the name of the
    frame the model was shown. Two legitimate ways to
    say "these items never went through a transcoding pass":

    * pass the mapping ``transcode_items`` returned -- it carries
      ``complete = True``, which is what a caller usually has;
    * pass ``originals_required=False`` explicitly, as the budget-arithmetic tests
      and hand-built requests do.

    An image is only replaced when the text is actually smaller. Measured: 200
    tiny (8x8) images plus one large frame came out **90,600 bytes larger** than
    the original payload after replacement, and the vision calls were paid for
    nothing. Small images are therefore skipped before transcription, and the
    rest are re-checked against their real transcription length afterwards.
    """
    stats = empty_stats()
    if not enabled or not isinstance(items, list):
        return items, stats
    positions = list(image_parts(items))
    if not positions:
        return items, stats
    total = sum(part_bytes(part) for _, _, _, part in positions)
    stats['bytes_before'] = total
    stats['bytes_after'] = total
    # A mapping that came from ``transcode_items`` states its own completeness, so a
    # caller cannot lose the "this is authoritative" half of the contract while
    # keeping the mapping.
    if getattr(originals, 'complete', False):
        originals_required = True
    protected = protected_positions(items, protect_items)
    stats['protected'] = len([position for position in positions if position[:3] in protected])

    # Some upstream accounts reject a single oversized image outright, so an
    # over-cap image is a replacement candidate even when the total fits the
    # budget. It is never allowed to touch the images that are in use right now.
    over_cap = set()
    if max_image_bytes > 0:
        for position in positions:
            if payload_bytes(position[3]) > max_image_bytes and position[:3] not in protected:
                over_cap.add(position[:3])
    needed = max(0, total - max(0, budget_bytes))
    if needed == 0 and not over_cap:
        return items, stats
    stats['over_budget'] = total > budget_bytes
    stats['over_image_cap'] = len(over_cap)

    # Walk newest to oldest and keep real images until the keep window is full.
    keep = set()
    kept_bytes = 0
    for position in reversed(positions):
        if position[:3] in protected:
            keep.add(position[:3])
            kept_bytes += part_bytes(position[3])
            continue
        if position[:3] in over_cap:
            # An over-cap image is replaced whatever the window says, so it must not
            # consume the window's bytes either.
            continue
        size = part_bytes(position[3])
        if kept_bytes + size > keep_bytes and keep:
            break
        keep.add(position[:3])
        kept_bytes += size

    index = store.load_index() if store is not None else {}
    candidates = []
    planned = set()
    for item_index, field, part_index, part in positions:
        position = (item_index, field, part_index)
        if position in keep:
            continue
        stats['considered'] += 1
        forced = position in over_cap
        size = part_bytes(part)
        if payload_bytes(part) < min_replace_bytes and not forced:
            stats['kept_too_small'] += 1
            continue
        parsed = _split_job(part)
        if parsed is None:
            stats['kept_over_budget'] += 1
            continue
        mime, raw, digest = parsed
        text = store.usable_text(index, digest) if store is not None else None
        if text:
            stats['cache_hits'] += 1
        candidates.append(_Candidate(item_index, field, part_index, digest, raw, mime,
                                     size, text, forced))

    # Smallest oldest-first prefix whose *estimated* saving covers the shortfall,
    # plus every forced (over-cap) image. Verified against real text below.
    estimate = _estimate_text_bytes(index)
    chosen = []
    saved = 0
    for candidate in candidates:
        if saved >= needed and not candidate.forced:
            break
        chosen.append(candidate)
        saved += max(0, candidate.size - estimate)
    for candidate in candidates[len(chosen):]:
        if candidate.forced:
            chosen.append(candidate)

    planned_digests = set()
    missing = []
    for candidate in chosen:
        if candidate.text is None and candidate.digest not in planned_digests:
            planned_digests.add(candidate.digest)
            missing.append((candidate.digest, candidate.raw, candidate.mime))
    stats['transcribe_jobs'] = len(missing)

    deadline = None if not deadline_seconds else time.monotonic() + float(deadline_seconds)
    texts = {}
    # One first batch sized by the estimate, then verification batches: a 100-image
    # history must not pay for 100 vision calls when three would have been enough,
    # and a bad estimate must not leave the request over budget (F11).
    queue = list(missing)
    first = True
    while queue:
        if deadline is not None and time.monotonic() >= deadline:
            stats['deadline_hit'] = 1
            break
        stats['rounds'] += 1
        batch_size = max(1, len(chosen)) if first else VERIFY_CHUNK
        batch_size = min(batch_size, len(queue))
        first = False
        batch, queue = queue[:batch_size], queue[batch_size:]
        results = transcribe_many(batch, transcribe, workers=workers, deadline=deadline,
                                  store=store)
        if deadline is not None and time.monotonic() >= deadline:
            # Count the overrun here rather than only at the top of the loop: a batch
            # that blew the budget and still returned partial results used to leave
            # deadline_hit at 0 while the wall-clock cap was demonstrably exceeded
            #.
            stats['deadline_hit'] = 1
        chunk_texts = {}
        for digest, raw, mime in batch:
            text = results.get(digest)
            if not text:
                stats['transcribe_failures'] += 1
                continue
            clean = normalise_transcription(text, store.json_mode if store is not None else False)
            if not clean:
                stats['transcribe_failures'] += 1
                continue
            texts[digest] = clean
            chunk_texts[digest] = clean
        for candidate in chosen:
            if candidate.text is None:
                candidate.text = texts.get(candidate.digest)
        if store is not None and chunk_texts:
            try:
                store.save_texts(chunk_texts, source=source)
            except Exception:
                pass
        if not chunk_texts:
            # A batch that produced nothing usable means the vision route is failing
            # right now; spending the rest of the budget on it would only add
            # latency to a request that is going to keep its pixels anyway.
            break
        if _projected_bytes(positions, chosen) <= budget_bytes and not _pending_forced(chosen):
            break

    output = list(items)
    for candidate in chosen:
        text = candidate.text
        if not text:
            stats['kept_over_budget'] += 1
            continue
        body = _replacement_text(text, store.original_path(candidate.digest, candidate.mime)
                                 if store is not None else '')
        replacement = {'type': 'input_text', 'text': body}
        if part_bytes(replacement) >= candidate.size:
            # The decision uses the same labelled text that is emitted, so a
            # replacement can never grow the request (measured: a Chinese
            # transcription grew one by 628 bytes while the stats showed a drop).

            stats['kept_not_smaller'] += 1
            continue
        if store is not None:
            # Only the images that are actually replaced are written to disk (the
            # earlier order leaked the bytes of every image that was considered),
            # and the *pre-transcode* bytes are what the model can re-read (F07):
            # files that are named as a re-read source have to be the frame the
            # model was shown, not the smaller copy the transport made of it.
            recorded = originals.get(candidate.digest) if isinstance(originals, dict) else None
            if recorded is media_transcode.ORIGINAL_UNCHANGED:
                # Untouched by the transcoder, so the bytes in the request are the
                # bytes the model saw.
                source_bytes, source_mime = candidate.raw, candidate.mime
            elif isinstance(recorded, tuple) and len(recorded) == 2:
                source_bytes, source_mime = recorded
            elif originals_required:
                # Never write a file that claims to be the original while actually
                # holding the transport's lossy copy; keep the pixels instead.
                stats['kept_no_original'] += 1
                continue
            else:
                source_bytes, source_mime = candidate.raw, candidate.mime
            digest = hashlib.sha256(source_bytes).hexdigest()
            try:
                path = store.save_original(digest, source_bytes, source_mime)
            except OSError:
                stats['kept_over_budget'] += 1
                continue
            body = _replacement_text(text, path)
            replacement = {'type': 'input_text', 'text': body}
        item = output[candidate.item_index]
        clone = dict(item)
        parts = list(clone[candidate.field])
        parts[candidate.part_index] = replacement
        clone[candidate.field] = parts
        output[candidate.item_index] = clone
        stats['replaced'] += 1

    stats['bytes_after'] = sum(part_bytes(output[i][f][p]) for i, f, p, _ in positions)
    return output, stats


def _pending_forced(chosen):
    return any(candidate.forced and not candidate.text for candidate in chosen)


def _projected_bytes(positions, chosen):
    """Bytes the request would have if every usable replacement already landed.

    Images whose transcription is still missing are counted at full size, so the
    estimate is pessimistic and can only cause another verification batch.
    """
    replacements = {}
    for candidate in chosen:
        if not candidate.text:
            continue
        size = part_bytes({'type': 'input_text', 'text': _replacement_text(candidate.text, '')})
        if size < candidate.size:
            replacements[candidate.position] = candidate.size - size
    total = 0
    for item_index, field, part_index, part in positions:
        total += part_bytes(part) - replacements.get((item_index, field, part_index), 0)
    return total
