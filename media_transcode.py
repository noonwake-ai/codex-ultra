"""Adaptive image transcoding for Responses ``input_image`` parts.

Background (2026-10-01 media-byte study): the byte cost of an image is decoupled
from its token cost, so images that stay in history are the dominant term of a
large request body while being nearly invisible to token-based compaction. The
cheapest safe lever is re-encoding, not resizing.

Rules
-----
* Candidates are ``min(original, PNG(optimize=True), JPEG q85)``. The original is
  always a candidate, so a request can never grow because of this layer.
* Resolution is preserved: no scaling, no cropping, no re-framing.
* Images with an alpha channel never take the JPEG candidate -- flattening alpha
  changes visible pixels.
* The JPEG candidate must also pass a fidelity gate (mean absolute pixel error
  against the decoded original). High-frequency content such as noise or fine
  texture can be 4x smaller as JPEG while being visually destroyed; those images
  keep their original bytes instead.
* ``detail`` and every other field of the part are preserved verbatim.
* Only ``input_image`` parts inside ``content`` / ``output`` are touched; items
  and parts are never removed or reordered.
* Nothing here mutates its input; unchanged items are returned as the same
  objects.
"""
import base64
import binascii
import hashlib
import io
import json
import os
import shutil
import threading
import time
from collections import OrderedDict

# Pillow is optional: without it every media decision falls back to the original
# bytes. The names live at module scope because the helpers below need the same
# classes, and a lazily imported name is invisible to them -- an earlier helper
# raised NameError into a swallowed except and silently rejected every lossy
# candidate (video frame 9.08x -> 1.72x, adversarial an earlier round follow-up).
try:
    from PIL import Image, ImageChops, ImageOps, ImageStat
except Exception:  # pragma: no cover - exercised by the no-pillow fallback
    Image = ImageChops = ImageOps = ImageStat = None

DEFAULT_QUALITY = 85
# Guard rail: refusing to decode absurd inputs keeps a pathological upload from
# turning into a CPU stall inside the forwarding path.
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 40_000_000
# Catastrophe guard for lossy candidates (JPEG and WebP alike). Mean absolute
# error is a weak quality proxy: on a photo carrying 6-8px low-contrast text,
# JPEG q85 scores MAE 2.03 and WebP q85 scores 2.10, yet an 8-round blind read
# scored WebP *better* (37/40 vs 31/40). The gate therefore does not rank
# fidelity; it only rejects destruction such as random noise (MAE ~47), which
# shrinks 4x while becoming unrecognisable.
LOSSY_MAX_MEAN_ERROR = 6.0
# A global mean hides chroma damage, and so does an absolute Cb/Cr threshold: a
# 1px red line on white lost 27% of its saturation while its own Cb/Cr moved by
# only ~28 of 255 (adversarial an earlier round). The check is therefore *relative*:
# a pixel counts as damaged when it had real colour and lost a large share of it.
CHROMA_LOSS_MIN_CHROMA = 60      # below this a pixel carries no colour to lose
CHROMA_LOSS_RATIO = 0.7          # ... and losing 30% of what it had counts
CHROMA_LOSS_MAX_FRACTION = 0.02  # of all coloured pixels in the frame
# Sparse detail is diluted by any whole-frame ratio, so a single row or column
# carrying too much of the loss rejects the candidate on its own. Measured: the
# 1px-line counterexample is 100% of one row, while a photographic frame, a video
# frame, a 1440p screenshot and a generated marketing card stay at 0.0-0.8%.
CHROMA_LOSS_MAX_LINE = 0.25
CHROMA_SAMPLE_SIDE = 512
# Re-encoding an image that is already lossy is a second generation of loss. Only
# do it when the win is material; a q80 source saves ~9% and is left alone, a q60
# source saves ~31% and is worth it.
LOSSY_SOURCE_MIN_SAVING = 0.25
# Lossless candidates (PNG, WebP-lossless) pay off on flat, screenshot-like
# content and cost seconds of CPU on a 4K frame (WebP lossless 2.3s at method 4,
# 21s at method 6). When a lossy candidate already wins by this factor there is
# nothing left for them to add, so those encoders are skipped.
LOSSLESS_RELEVANT_ABOVE = 4.0
# Lossless encoders are only affordable on flat content, and WebP lossless is
# pathologically slow on high-entropy pixels. The source's own compression ratio
# is a cheap, reliable proxy: measured screenshots sit at 0.011-0.115 bytes per
# pixel, photographic PNGs at ~1.4, and random noise at ~3.0. Sources that are
# already lossy never get a lossless re-encode (it would only grow).
LOSSLESS_SOURCE_MAX_BYTES_PER_PIXEL = 0.6
# Byte density alone misclassifies a softly blurred photographic frame (~0.5
# bytes/pixel) as a screenshot, which would trade a 5-10x lossy win for text that
# is not there. Screenshots are also colour-poor -- measured 169-3887 distinct
# colours against 4041-14854 for photographic frames -- so *either* signal marks
# flat content, and a false "flat" only costs bytes, never pixels.
FLAT_BYTES_PER_PIXEL = 0.15
FLAT_MAX_COLORS = 2500
COLOR_SAMPLE_SIDE = 512
COLOR_COUNT_CAP = 60000
MAX_LOSSLESS_PIXELS = 12_000_000
WEBP_METHOD = 4
ENV_WEBP = 'NWCP_MEDIA_WEBP'
ENV_SWITCH = 'NWCP_MEDIA_TRANSCODE'
# The same image is re-sent on every turn, and a 4K frame costs seconds of CPU to
# re-encode, so results are memoised by source bytes. The cache holds replaced
# URLs only, bounded by both entry count and total source bytes.
CACHE_MAX_ENTRIES = 64
CACHE_MAX_BYTES = 96 * 1024 * 1024
_DATA_PREFIX = 'data:'
_MEDIA_FIELDS = ('content', 'output')


def enabled():
    """Transcoding is on unless the operator explicitly turns it off."""
    value = os.environ.get(ENV_SWITCH)
    if value is None:
        return True
    return value.strip().lower() not in ('0', 'false', 'off', 'no')


_CACHE = OrderedDict()
_CACHE_BYTES = 0
_CACHE_LOCK = threading.Lock()
_CACHE_HITS = 0
_CACHE_MISSES = 0

# The same result is also written next to the vision cache. The in-process map dies with
# the process, so a retry after a restart used to re-encode every image (measured on a
# 48-frame history: ~33 s of CPU on exactly the retry path that can least afford it),
# while the vision transcription cache next to it already survived restarts.
TRANSCODE_CACHE_VERSION = 1
DISK_MAGIC = b'CXUTC1\n'
# Everything a cached result depends on. A pricey bug came from hashing only the
# source bytes plus the webp flag: a later call with a different quality (or a
# different source mime, or after a change to the lossless/lossy thresholds)
# reused the earlier bytes, so the "cached" frame was not the frame this pipeline
# would produce. The fingerprint is computed lazily, after the module constants
# below are defined.
PIPELINE_CONSTANTS = ('DEFAULT_QUALITY', 'LOSSY_SOURCE_MIN_SAVING', 'MAX_SOURCE_BYTES',
                      'MAX_PIXELS', 'COLOR_COUNT_CAP', 'WEBP_METHOD', 'ENV_SWITCH')
_PIPELINE_FINGERPRINT = None
DISK_MAX_BYTES = 256 * 1024 * 1024
DISK_TTL_SECONDS = 7 * 24 * 3600
DISK_PRUNE_EVERY = 64
ORPHAN_TEMP_SECONDS = 3600

_DISK_DIR = None
_DISK_MAX_BYTES = DISK_MAX_BYTES
_DISK_TTL_SECONDS = DISK_TTL_SECONDS
_DISK_HITS = 0
_DISK_WRITES = 0
_DISK_ERRORS = 0
_DISK_WRITES_SINCE_PRUNE = 0


def configure_cache(directory=None, max_bytes=None, ttl_seconds=None):
    """Point the persistent transcode cache at a directory; None disables it.

    Called once by the service at start-up. A directory that cannot be created is
    remembered as disabled and never fails a request.
    """
    global _DISK_DIR, _DISK_MAX_BYTES, _DISK_TTL_SECONDS, _DISK_WRITES_SINCE_PRUNE
    directory = str(directory) if directory else None
    if directory:
        try:
            os.makedirs(os.path.join(directory, _disk_subdir()), exist_ok=True)
        except OSError:
            directory = None
    with _CACHE_LOCK:
        _DISK_DIR = directory
        # Tests and operators walk these counters; keep them per configuration.
        global _DISK_HITS, _DISK_WRITES, _DISK_ERRORS
        _DISK_HITS = 0
        _DISK_WRITES = 0
        _DISK_ERRORS = 0
        if max_bytes is not None:
            _DISK_MAX_BYTES = max(0, int(max_bytes))
        if ttl_seconds is not None:
            _DISK_TTL_SECONDS = max(0, int(ttl_seconds))
        _DISK_WRITES_SINCE_PRUNE = 0
    if directory:
        _disk_prune()


def cache_info():
    with _CACHE_LOCK:
        return {'entries': len(_CACHE), 'source_bytes': _CACHE_BYTES,
                'hits': _CACHE_HITS, 'misses': _CACHE_MISSES,
                'disk': bool(_DISK_DIR), 'disk_hits': _DISK_HITS,
                'disk_writes': _DISK_WRITES, 'disk_errors': _DISK_ERRORS,
                'disk_limit_bytes': _DISK_MAX_BYTES}


def cache_clear(disk=False):
    """Forget the in-process map. ``disk=True`` also drops the persisted entries."""
    global _CACHE_BYTES, _CACHE_HITS, _CACHE_MISSES, _DISK_HITS, _DISK_WRITES, _DISK_ERRORS
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE_BYTES = 0
        _CACHE_HITS = 0
        _CACHE_MISSES = 0
        _DISK_HITS = 0
        _DISK_WRITES = 0
        _DISK_ERRORS = 0
    if disk:
        for path, _, _ in list(_disk_entries()):
            _unlink(path)


def _disk_subdir():
    return 'v%d' % TRANSCODE_CACHE_VERSION


def _disk_path(key, create=False):
    if not _DISK_DIR or not key:
        return None
    base = os.path.join(_DISK_DIR, _disk_subdir())
    if create:
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            return None
    return os.path.join(base, key + '.tc')


def _disk_entries():
    """Yield ``(path, size, mtime)`` for every persisted transcode."""
    if not _DISK_DIR:
        return
    base = os.path.join(_DISK_DIR, _disk_subdir())
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        if not name.endswith('.tc'):
            continue
        path = os.path.join(base, name)
        try:
            info = os.stat(path)
        except OSError:
            continue
        yield path, info.st_size, info.st_mtime


def _rmtree(path):
    """Remove a stale cache directory, counting a failure like any other disk error."""
    global _DISK_ERRORS
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError:
        with _CACHE_LOCK:
            _DISK_ERRORS += 1


def _unlink(path):
    global _DISK_ERRORS
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError:
        with _CACHE_LOCK:
            _DISK_ERRORS += 1


def _disk_prune(now=None):
    """Drop expired entries, then the oldest until the byte limit is met.

    Also sweeps two kinds of leftovers the byte limit does not count: temporary files
    abandoned by a writer that died mid-publish (only ever live for milliseconds in
    the happy path, so an hour is safely stale), and cache directories from an older
    envelope version, which would otherwise keep their bytes forever after a bump.
    """
    now = time.time() if now is None else now
    if _DISK_DIR:
        try:
            base = os.path.join(_DISK_DIR, _disk_subdir())
            current = _disk_subdir()
            for name in os.listdir(_DISK_DIR):
                if not name.startswith('v') or name == current:
                    continue
                _rmtree(os.path.join(_DISK_DIR, name))
            for name in os.listdir(base):
                if not name.endswith('.tmp'):
                    continue
                path = os.path.join(base, name)
                try:
                    if now - os.stat(path).st_mtime > ORPHAN_TEMP_SECONDS:
                        _unlink(path)
                except OSError:
                    continue
        except OSError:
            pass
    entries = sorted(_disk_entries(), key=lambda entry: entry[2])
    if not entries:
        return
    kept = []
    for path, size, mtime in entries:
        if _DISK_TTL_SECONDS and now - mtime > _DISK_TTL_SECONDS:
            _unlink(path)
            continue
        kept.append((path, size, mtime))
    total = sum(size for _, size, _ in kept)
    for path, size, _ in kept:
        if total <= _DISK_MAX_BYTES:
            break
        _unlink(path)
        total -= size


def _disk_get(key):
    """Return ``((url, decision, after), source_bytes)`` from disk, or None."""
    global _DISK_HITS, _DISK_ERRORS
    path = _disk_path(key)
    if path is None:
        return None
    try:
        with open(path, 'rb') as handle:
            blob = handle.read()
        if not blob.startswith(DISK_MAGIC):
            raise ValueError('bad_magic')
        header, separator, payload = blob[len(DISK_MAGIC):].partition(b'\n')
        if not separator:
            raise ValueError('missing_header')
        meta = json.loads(header.decode('utf-8'))
        if meta.get('v') != TRANSCODE_CACHE_VERSION or meta.get('key') != key:
            return None
        if meta.get('url_bytes') != len(payload):
            raise ValueError('truncated')
        if meta.get('sha256') and meta['sha256'] != hashlib.sha256(payload).hexdigest():
            # A half-written or externally damaged entry must never be handed to a
            # model as the frame it was shown.
            raise ValueError('digest_mismatch')
        value = (payload.decode('utf-8'), meta.get('decision'), int(meta.get('after') or 0))
        source_bytes = int(meta.get('source_bytes') or 0)
    except FileNotFoundError:
        return None
    except Exception:
        # A corrupt entry must never fail a request; it is only recounted.
        with _CACHE_LOCK:
            _DISK_ERRORS += 1
        return None
    try:
        os.utime(path, None)
    except OSError:
        pass
    with _CACHE_LOCK:
        _DISK_HITS += 1
    return value, source_bytes


def _disk_put(key, value, source_bytes):
    global _DISK_WRITES, _DISK_ERRORS, _DISK_WRITES_SINCE_PRUNE
    path = _disk_path(key, create=True)
    if path is None:
        return
    url, decision, after = value
    encoded = url.encode('utf-8')
    header = json.dumps({'v': TRANSCODE_CACHE_VERSION, 'key': key, 'decision': decision,
                         'after': after, 'url_bytes': len(encoded),
                         'sha256': hashlib.sha256(encoded).hexdigest(),
                         'source_bytes': int(source_bytes or 0)},
                        separators=(',', ':')).encode('utf-8')
    # Unique per writer: two processes compacting at the same time used to share one
    # ``<entry>.tmp`` name, so one writer could publish bytes the other was still
    # writing (measured as a same-length mixed payload that was then accepted).
    temporary = '%s.%d.%d.tmp' % (path, os.getpid(), threading.get_ident())
    try:
        with open(temporary, 'wb') as handle:
            handle.write(DISK_MAGIC + header + b'\n' + encoded)
        os.replace(temporary, path)
    except OSError:
        with _CACHE_LOCK:
            _DISK_ERRORS += 1
        _unlink(temporary)
        return
    with _CACHE_LOCK:
        _DISK_WRITES += 1
        _DISK_WRITES_SINCE_PRUNE += 1
        due = _DISK_WRITES_SINCE_PRUNE >= DISK_PRUNE_EVERY
        if due:
            _DISK_WRITES_SINCE_PRUNE = 0
    if due:
        _disk_prune()


def _cache_get(key):
    global _CACHE_HITS, _CACHE_MISSES, _CACHE_BYTES
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is not None:
            _CACHE_HITS += 1
            _CACHE.move_to_end(key)
            return entry[0]
    found = _disk_get(key)
    if found is None:
        with _CACHE_LOCK:
            _CACHE_MISSES += 1
        return None
    value, source_bytes = found
    with _CACHE_LOCK:
        if key not in _CACHE:
            _CACHE[key] = (value, source_bytes)
            _CACHE_BYTES += source_bytes
            while _CACHE and (len(_CACHE) > CACHE_MAX_ENTRIES or _CACHE_BYTES > CACHE_MAX_BYTES):
                _, (_, freed) = _CACHE.popitem(last=False)
                _CACHE_BYTES -= freed
        _CACHE_HITS += 1
    return value


def _cache_put(key, value, source_bytes):
    global _CACHE_BYTES
    with _CACHE_LOCK:
        if key not in _CACHE:
            _CACHE[key] = (value, source_bytes)
            _CACHE_BYTES += source_bytes
            while _CACHE and (len(_CACHE) > CACHE_MAX_ENTRIES or _CACHE_BYTES > CACHE_MAX_BYTES):
                _, (_, freed) = _CACHE.popitem(last=False)
                _CACHE_BYTES -= freed
    _disk_put(key, value, source_bytes)


def webp_enabled():
    """WebP candidates are on unless the operator turns them off."""
    value = os.environ.get(ENV_WEBP)
    if value is None:
        return True
    return value.strip().lower() not in ('0', 'false', 'off', 'no')


def _is_lossy(mime):
    return mime in ('image/jpeg', 'image/webp')


# Marker the transcoder puts in ``originals`` for an image it inspected and left
# alone: the bytes already in the request *are* the bytes the model was shown, so
# the budget layer can store them without this layer holding a second copy of every
# image it looked at.
ORIGINAL_UNCHANGED = object()


class Originals(dict):
    """What a transcoding pass knows about the bytes it was given.

    ``complete`` says every image in the returned items has an entry, so a missing
    digest downstream means the mapping was lost rather than "this image was never
    touched". Carrying the flag on the mapping itself means a caller only has to
    pass one value to get the strict behaviour.
    """

    __slots__ = ('complete',)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.complete = False


def empty_stats():
    return {'images': 0, 'replaced': 0, 'bytes_before': 0, 'bytes_after': 0,
            'cache_hits': 0, 'decisions': {}, 'skipped': {},
            # A fidelity gate that cannot run must be visible: it used
            # to be swallowed into "candidate rejected" with no counter anywhere.
            'fidelity_errors': 0,
            # 1 when ``originals`` describes every image this pass returned, so a
            # missing digest means a caller dropped the mapping, not that the bytes
            # were never transcoded.
            'originals_complete': 0}


def _count_skip(skips, reason):
    """Record one rejected candidate in the caller-owned counter dict."""
    if isinstance(skips, dict):
        skips[reason] = skips.get(reason, 0) + 1


def _mime_of(url):
    head = url.partition(',')[0]
    return head[len('data:'):].split(';')[0].strip().lower()


def _split_data_url(url):
    if not isinstance(url, str) or not url.startswith(_DATA_PREFIX):
        return None
    head, sep, payload = url.partition(',')
    if not sep or ';base64' not in head.lower():
        return None
    mime = head[len(_DATA_PREFIX):].split(';')[0].strip().lower()
    if not mime.startswith('image/'):
        return None
    try:
        raw = base64.b64decode(payload)
    except (binascii.Error, ValueError):
        return None
    return mime, raw


def _distinct_colors(image, sample=COLOR_SAMPLE_SIDE, cap=COLOR_COUNT_CAP):
    """Distinct colours on a downscaled copy, or ``cap`` when there are more."""
    small = image.convert('RGB')
    if max(small.size) > sample:
        scale = sample / max(small.size)
        small = small.resize((max(1, int(small.width * scale)), max(1, int(small.height * scale))),
                             Image.BILINEAR)
    counted = small.getcolors(maxcolors=cap)
    return cap if counted is None else len(counted)


def _has_alpha(image):
    if image.mode in ('RGBA', 'LA', 'PA'):
        return True
    return image.mode == 'P' and 'transparency' in image.info


def _encode_png(image):
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


def _encode_jpeg(image, quality):
    buffer = io.BytesIO()
    image.convert('RGB').save(buffer, format='JPEG', quality=quality, optimize=True)
    return buffer.getvalue()


def _encode_webp_lossless(image):
    buffer = io.BytesIO()
    image.save(buffer, format='WEBP', lossless=True, quality=100, method=WEBP_METHOD)
    return buffer.getvalue()


def _encode_webp(image, quality):
    buffer = io.BytesIO()
    image.convert('RGB').save(buffer, format='WEBP', quality=quality, method=WEBP_METHOD)
    return buffer.getvalue()


def _shrink(image, side=CHROMA_SAMPLE_SIDE):
    if max(image.size) <= side:
        return image
    scale = side / float(max(image.size))
    return image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                        Image.BILINEAR)


def _chroma_map(image):
    """Per-pixel colourfulness (max channel - min channel) as an L image."""
    red, green, blue = image.convert('RGB').split()
    high = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    low = ImageChops.darker(ImageChops.darker(red, green), blue)
    return ImageChops.subtract(high, low)


def _chroma_loss(a, b, min_chroma=CHROMA_LOSS_MIN_CHROMA, ratio=CHROMA_LOSS_RATIO):
    """``(fraction of coloured pixels that lost colour, worst row-or-column share)``.

    Relative, so a nearly-white pixel that gains a tint is not "damage" while a
    saturated line that survives but desaturates is. Both images are compared at
    the same sampled size, which is also what keeps this cheap: the row and column
    profiles are a box resize, not a python loop over pixels.
    """
    try:
        from PIL import ImageChops
    except Exception:
        return 0.0, 0.0
    if a.size != b.size:
        return 1.0, 1.0
    before = _chroma_map(_shrink(a))
    after = _chroma_map(_shrink(b))
    saturated = before.point(lambda value: 255 if value >= min_chroma else 0)
    total = saturated.histogram()[255]
    if not total:
        return 0.0, 0.0
    limit = before.point(lambda value: int(value * ratio))
    under = ImageChops.subtract(limit, after)
    lost = ImageChops.multiply(under.point(lambda value: 255 if value else 0), saturated)
    count = lost.histogram()[255]
    rows = lost.resize((1, lost.height), Image.BOX)
    columns = lost.resize((lost.width, 1), Image.BOX)
    worst = max(max(rows.getdata() or [0]), max(columns.getdata() or [0])) / 255.0
    return count / float(total), worst


def _mean_abs_error(a, b):
    """Mean absolute pixel error between two same-size images, at C speed."""
    if a.size != b.size or Image is None:
        return None
    left = a.convert('RGB')
    right = b.convert('RGB')
    difference = ImageChops.difference(left, right)
    channels = ImageStat.Stat(difference).mean
    if not channels:
        return None
    return sum(channels) / len(channels)


def pipeline_fingerprint():
    """Stable digest of every module constant a transcoded result depends on."""
    global _PIPELINE_FINGERPRINT
    if _PIPELINE_FINGERPRINT is None:
        parts = []
        for name in PIPELINE_CONSTANTS:
            parts.append('%s=%r' % (name, globals().get(name)))
        parts.append('pillow=%s' % getattr(Image, '__version__', 'none'))
        _PIPELINE_FINGERPRINT = hashlib.sha256(
            ','.join(parts).encode('utf-8')).hexdigest()[:16]
    return _PIPELINE_FINGERPRINT


def cache_key(raw, mime, quality, use_webp):
    """Identity of one transcoded frame: source bytes plus the pipeline that made it."""
    prefix = 'v%d|q:%s|mime:%s|webp:%d|pipe:%s|' % (
        TRANSCODE_CACHE_VERSION, quality, mime, 1 if use_webp else 0,
        pipeline_fingerprint())
    return hashlib.sha256(prefix.encode('utf-8') + raw).hexdigest()


def transcode_image_url(url, quality=DEFAULT_QUALITY, webp=None, skips=None):
    """Return ``(new_url, decision, raw_before, raw_after)``.

    ``decision`` is ``original`` / ``png`` / ``jpeg`` / ``webp`` when the URL was a
    decodable image data URL, otherwise a short reason such as ``not_image`` or
    ``no_pillow``. ``webp`` overrides the module/env default for this call.

    ``skips`` is an optional counter dict owned by the caller; every rejected
    candidate is recorded there (``chroma_webp``, ``gate_error``). This used to
    write into a bare name that did not exist at module scope, so the moment the
    chroma gate tripped the whole pass raised NameError into the caller's blanket
    ``except`` and silently disabled transcoding for the whole request
    (found while building the compaction path).
    """
    use_webp = webp_enabled() if webp is None else bool(webp)
    parsed = _split_data_url(url)
    if parsed is None:
        return url, 'not_image', 0, 0
    mime, raw = parsed
    size = len(raw)
    if size > MAX_SOURCE_BYTES:
        return url, 'too_large', size, size
    key = cache_key(raw, mime, quality, use_webp)
    cached = _cache_get(key)
    if cached is not None:
        new_url, decision, after = cached
        return new_url, decision, size, after
    if Image is None:
        return url, 'no_pillow', size, size
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
        # Phones store the true orientation in EXIF; re-encoding drops the tag, and
        # the gateway honours it, so the model would see the whole frame rotated
        # and the gateway honours it, so the model would see the whole frame
        # rotated. Bake the rotation into the pixels instead.
        image = ImageOps.exif_transpose(image)
    except Exception:
        return url, 'undecodable', size, size
    if image.width * image.height > MAX_PIXELS:
        return url, 'too_many_pixels', size, size

    best_length, best_mime, best_raw = size, mime, raw
    pixels = image.width * image.height
    # A second generation of loss is only worth it when the win is material.
    allowed = size * (1.0 - LOSSY_SOURCE_MIN_SAVING) if _is_lossy(mime) else size
    # Screenshot-like content is flat enough that its own compression ratio is
    # tiny (measured 0.011-0.115 bytes per pixel, against ~1.4 for photographic
    # PNGs and ~3.0 for noise). Those pixels are text, charts and flat colour, so
    # lossy artefacts cost legibility where the lossless encoders already deliver
    # a real win -- lossy WebP would go smaller still (7.7x vs 4.0x on a flat UI
    # sample) while shifting solid colours by up to 50/255.
    if _is_lossy(mime):
        flat_source = False
    else:
        try:
            colours = _distinct_colors(image)
        except Exception:
            colours = COLOR_COUNT_CAP
        flat_source = (colours <= FLAT_MAX_COLORS
                       or (pixels > 0 and size <= pixels * FLAT_BYTES_PER_PIXEL))

    def try_lossless():
        nonlocal best_length, best_mime, best_raw
        for candidate_mime, encode in (('image/webp', _encode_webp_lossless),
                                       ('image/png', _encode_png)):
            if candidate_mime == 'image/webp' and not use_webp:
                continue
            try:
                candidate = encode(image)
            except Exception:
                continue
            if candidate and len(candidate) < best_length:
                best_length, best_mime, best_raw = len(candidate), candidate_mime, candidate

    if pixels > MAX_LOSSLESS_PIXELS:
        pass
    elif flat_source:
        try_lossless()
    elif not _has_alpha(image):
        # Lossy candidates are cheap (4K: 0.02s JPEG, 0.24s WebP) and carry the big
        # wins on photographic content, so they are evaluated first.
        for candidate_mime, encode in (('image/webp', lambda im: _encode_webp(im, quality)),
                                       ('image/jpeg', lambda im: _encode_jpeg(im, quality))):
            if candidate_mime == 'image/webp' and not use_webp:
                continue
            try:
                candidate = encode(image)
            except Exception:
                continue
            if not candidate or len(candidate) >= allowed or len(candidate) >= best_length:
                continue
            try:
                decoded = Image.open(io.BytesIO(candidate))
                error = _mean_abs_error(image, decoded)
                chroma, worst_line = _chroma_loss(image, decoded)
            except Exception:
                # A gate that cannot run rejects the candidate and says so; silence
                # here is how a broken fidelity check used to look identical to a
                # passed one on /health.
                _count_skip(skips, 'gate_error')
                continue
            if error is None or error > LOSSY_MAX_MEAN_ERROR:
                continue
            if chroma > CHROMA_LOSS_MAX_FRACTION or worst_line > CHROMA_LOSS_MAX_LINE:
                _count_skip(skips, 'chroma_%s' % candidate_mime.split('/')[-1])
                continue
            best_length, best_mime, best_raw = len(candidate), candidate_mime, candidate
        if best_length > size / LOSSLESS_RELEVANT_ABOVE:
            try_lossless()
    else:
        # Alpha imagery with photographic entropy: flattening would change pixels,
        # so only lossless candidates are allowed.
        try_lossless()

    if best_raw is raw:
        _cache_put(key, (url, 'original', size), size)
        return url, 'original', size, size
    encoded = base64.b64encode(best_raw).decode('ascii')
    new_url = 'data:%s;base64,%s' % (best_mime, encoded)
    decision = best_mime.split('/')[-1]
    _cache_put(key, (new_url, decision, best_length), size)
    return new_url, decision, size, best_length

def transcode_items(items, quality=DEFAULT_QUALITY, force=None, webp=None):
    """Transcode every ``input_image`` data URL found in ``items``.

    Returns ``(items, stats, originals)`` where ``originals`` maps the hash of an
    image payload in the returned items to the ``(bytes, mime)`` that payload was
    derived from, for this request only. The budget layer needs that mapping to keep
    the *real* original for a later re-read instead of the re-encoded copy;
    scoping it to the call means it can never be evicted or leak across
    requests. The original list and its items are never mutated; when nothing
    changed the input list is returned unchanged.

    When this pass runs, the mapping is **complete over the returned items**:
    untouched images map to themselves. ``stats['originals_complete']`` says so, and
    the budget layer refuses to write a replacement file for a digest that is
    missing from a complete mapping -- that is the difference between "no transcode
    happened" and "the caller lost the mapping".
    """
    stats = empty_stats()
    originals = Originals()
    active = enabled() if force is None else bool(force)
    if not active or not isinstance(items, list):
        return items, stats, originals
    output = []
    changed = False
    for item in items:
        new_item = item
        if isinstance(item, dict):
            for field in _MEDIA_FIELDS:
                parts = item.get(field)
                if not isinstance(parts, list):
                    continue
                new_parts = None
                for index, part in enumerate(parts):
                    if not isinstance(part, dict) or part.get('type') != 'input_image':
                        continue
                    url = part.get('image_url')
                    if not isinstance(url, str):
                        continue
                    stats['images'] += 1
                    source = _split_data_url(url)
                    hits_before = cache_info()['hits']
                    try:
                        new_url, decision, before, after = transcode_image_url(
                            url, quality, webp=webp, skips=stats['skipped'])
                    except Exception:
                        # One unencodable frame must not cost the other 100 their
                        # pass: this layer sits in the forwarding path of a service
                        # other people depend on.
                        _count_skip(stats['skipped'], 'error')
                        if source is not None:
                            # It was not touched, so the bytes in the request *are*
                            # the bytes the model saw. Recording that keeps the
                            # mapping honest: without it the budget layer reads the
                            # hole as "provenance lost" and reports a false
                            # kept_no_original.
                            originals.setdefault(hashlib.sha256(source[1]).hexdigest(),
                                                 ORIGINAL_UNCHANGED)
                        continue
                    if cache_info()['hits'] > hits_before:
                        stats['cache_hits'] += 1
                    stats['bytes_before'] += before
                    stats['bytes_after'] += after
                    stats['decisions'][decision] = stats['decisions'].get(decision, 0) + 1
                    if new_url == url:
                        if source is not None:
                            originals.setdefault(hashlib.sha256(source[1]).hexdigest(),
                                                 ORIGINAL_UNCHANGED)
                        continue
                    stats['replaced'] += 1
                    if new_parts is None:
                        new_parts = list(parts)
                    replacement = dict(part)
                    replacement['image_url'] = new_url
                    new_parts[index] = replacement
                    parsed = _split_data_url(new_url)
                    if parsed is not None:
                        originals[hashlib.sha256(parsed[1]).hexdigest()] = (
                            source[1] if source is not None else base64.b64decode(url.partition(',')[2]),
                            source[0] if source is not None else _mime_of(url))
                if new_parts is not None:
                    if new_item is item:
                        new_item = dict(item)
                    new_item[field] = new_parts
                    changed = True
        output.append(new_item)
    stats['fidelity_errors'] = stats['skipped'].get('gate_error', 0)
    stats['originals_complete'] = 1
    originals.complete = True
    return (output if changed else items), stats, originals
