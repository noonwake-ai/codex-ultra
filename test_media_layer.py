"""The media byte layer: what it does, what it costs, and what it refuses to do.

Three independent concerns live in one place, and each has a way of failing
silently, so each gets its own test:

* transcoding (free) may only ever make a request smaller -- a failure must leave
  the request untouched rather than break forwarding;
* the paid layers are off until an operator asks for them, because the vision
  calls are billed to that operator's own gateway;
* a file written as a re-read source must be the frame the model was shown, not the
  smaller copy the transport made of it, and a hole in that provenance must be a
  refusal rather than a plausible-looking wrong file.
"""
import base64
import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from PIL import Image

import media_budget as mb
import media_transcode as tc
from adapter import Adapter

GOOD_JSON = json.dumps({'timecode': '00:12:34:07', 'slate': 'TAKE 07',
                        'other_text': ['SUB-1042'], 'composition': 'a dark frame',
                        'colours': ['black', 'orange']})


def photo_png(side=256, amplitude=4):
    """Smooth gradient plus dither: large as PNG, big win for a lossy encoder."""
    import random
    rng = random.Random(20261002)
    pixels = bytearray()
    for y in range(side):
        for x in range(side):
            jitter = rng.randint(-amplitude, amplitude)
            pixels += bytes((min(255, max(0, (x * 255) // side + jitter)),
                             min(255, max(0, (y * 255) // side + jitter)),
                             min(255, max(0, 128 + jitter))))
    buffer = io.BytesIO()
    Image.frombytes('RGB', (side, side), bytes(pixels)).save(buffer, format='PNG')
    return buffer.getvalue(), 'image/png'


def data_url(raw, mime):
    return 'data:%s;base64,%s' % (mime, base64.b64encode(raw).decode())


def image_item(url, role='user'):
    return {'type': 'message', 'role': role,
            'content': [{'type': 'input_image', 'image_url': url, 'detail': 'high'}]}


def text_item(text, role='user'):
    return {'type': 'message', 'role': role, 'content': [{'type': 'input_text', 'text': text}]}


def sha256_file(path):
    with open(path, 'rb') as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class _Response:
    def __init__(self, text):
        self.status_code = 200
        self.headers = {'Content-Type': 'application/json'}
        self.text = json.dumps({'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]})

    def json(self):
        return json.loads(self.text)


class _Transport:
    """Records every outbound call; answers the vision prompt with a canned object."""

    def __init__(self, text=GOOD_JSON):
        self.text = text
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json)
        return _Response(self.text)


def config(**over):
    cfg = {'upstream': 'https://gateway.example.test', 'compactor_model': 'gpt-6-sol',
           'compactor_effort': 'medium', 'port': 0, 'upstream_encoding': 'identity',
           'media_models': ()}
    cfg.update(over)
    return cfg


class MediaLayerTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='media-layer-')
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.addCleanup(mb.reset_executor)

    def adapter(self, transport=None, **over):
        over.setdefault('media_store_dir', os.path.join(self.directory, 'cache'))
        over.setdefault('native_cache_dir', os.path.join(self.directory, 'native'))
        transport = transport or _Transport()
        return Adapter(config(**over), b'7' * 32, transport=transport,
                       credential=lambda: 'synthetic-token'), transport

    # ---------------------------------------------------------------- free layer
    def test_transcoding_shrinks_a_screenshot_without_touching_its_geometry(self):
        raw, mime = photo_png(256)
        url = data_url(raw, mime)
        new_url, decision, before, after = tc.transcode_image_url(url)
        self.assertIn(decision, ('webp', 'jpeg', 'png'))
        self.assertLessEqual(after, before)
        if new_url != url:
            decoded = Image.open(io.BytesIO(base64.b64decode(new_url.partition(',')[2])))
            original = Image.open(io.BytesIO(raw))
            self.assertEqual(original.size, decoded.size, 'the layer never resizes')

    def test_a_request_without_images_costs_nothing(self):
        adapter, transport = self.adapter()
        items = [text_item('hello')]
        out, originals, complete = adapter.transcode_media(items)
        self.assertEqual(items, out)
        self.assertEqual({}, dict(originals))
        self.assertTrue(complete, 'a pass with nothing to record still describes its items')
        self.assertEqual(0, adapter.stats['media_transcode_errors'])
        self.assertEqual([], transport.calls, 'transcoding never calls a model')

    def test_a_broken_transcoder_is_counted_and_leaves_the_request_alone(self):
        """This runs in the forwarding path: a failure may never cost the request."""
        adapter, transport = self.adapter()
        items = [image_item(data_url(*photo_png(64)))]
        original = tc.transcode_items
        tc.transcode_items = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom'))
        try:
            out, originals, complete = adapter.transcode_media(items)
        finally:
            tc.transcode_items = original
        self.assertEqual(items, out)
        self.assertEqual({}, dict(originals))
        self.assertFalse(complete)
        self.assertEqual(1, adapter.stats['media_transcode_errors'])
        self.assertEqual([], transport.calls)

    # ---------------------------------------------------------------- paid layers
    def test_the_paid_layers_are_off_by_default(self):
        """An install that nobody configured must not spend the operator's credits."""
        adapter, transport = self.adapter(media_budget_enabled=False, media_prewarm_enabled=False)
        body = {'model': 'deepseek-flash', 'input': [image_item(data_url(*photo_png(128)))]}
        prepared = adapter.prepare_forward(body)
        self.assertEqual('input_image', prepared['input'][0]['content'][0]['type'])
        self.assertEqual([], transport.calls)
        self.assertEqual(0, adapter.stats['media_budget_evaluations'])

    def test_enabling_the_budget_turns_old_frames_into_text_not_into_a_missing_image(self):
        raws = [photo_png(256)[0], photo_png(320)[0], photo_png(256, amplitude=6)[0]]
        items = [image_item(data_url(raw, 'image/png')) for raw in raws]
        adapter, transport = self.adapter(media_transcode=True, media_budget_enabled=True,
                                          media_budget_bytes=1024, media_keep_bytes=0,
                                          media_max_image_bytes=0, media_protect_recent_items=0,
                                          media_prewarm_enabled=False)
        body = {'model': 'deepseek-flash', 'input': items}
        prepared = adapter.prepare_forward(body)
        parts = prepared['input']
        self.assertEqual('input_text', parts[0]['content'][0]['type'])
        self.assertIn('source: ', parts[0]['content'][0]['text'])
        self.assertEqual('input_image', parts[-1]['content'][0]['type'],
                         'the newest frame keeps its pixels')
        self.assertTrue(transport.calls, 'the replacement is paid for exactly once per image')
        self.assertGreaterEqual(adapter.stats['media_budget_replaced'], 1)
        self.assertEqual('request', adapter.stats['media_last_budget_route'])

    def test_a_re_read_file_is_the_frame_the_model_was_shown(self):
        """The named file must be the pre-transcode bytes, not the smaller copy."""
        raw, mime = photo_png(256)
        items = [image_item(data_url(raw, mime)),
                 image_item(data_url(*photo_png(320))),
                 image_item(data_url(*photo_png(256, amplitude=6)))]
        adapter, _transport = self.adapter(media_budget_enabled=True, media_budget_bytes=1024,
                                           media_keep_bytes=0, media_max_image_bytes=0,
                                           media_protect_recent_items=0)
        prepared = adapter.prepare_forward({'model': 'deepseek-flash', 'input': items})
        written = [os.path.join(adapter.media_store.directory, name)
                   for name in os.listdir(adapter.media_store.directory)
                   if not name.startswith('transcriptions.json')]
        self.assertTrue(written)
        self.assertIn(hashlib.sha256(raw).hexdigest(),
                      [sha256_file(path) for path in written],
                      'the oldest frame was replaced, so its file has to be the real one')

    def test_a_hole_in_the_provenance_is_a_refusal_not_a_wrong_file(self):
        raws = [photo_png(256)[0], photo_png(320)[0], photo_png(256, amplitude=6)[0]]
        items = [image_item(data_url(raw, 'image/png')) for raw in raws]
        transcoded, stats, originals = tc.transcode_items(items, force=True, webp=True)
        self.assertTrue(stats['originals_complete'])
        newest = list(mb.image_parts(transcoded))[-1][3]
        newest_digest = hashlib.sha256(base64.b64decode(newest['image_url'].partition(',')[2])).hexdigest()
        partial = tc.Originals({newest_digest: originals[newest_digest]})
        partial.complete = True
        store = mb.MediaStore(os.path.join(self.directory, 'holes'))
        out, budget_stats = mb.apply_budget(transcoded, store, lambda raw, mime, digest: GOOD_JSON,
                                            16, 0, max_image_bytes=0, protect_items=0,
                                            originals=partial)
        self.assertGreaterEqual(budget_stats['kept_no_original'], 1)
        transcoded_shas = {hashlib.sha256(base64.b64decode(part['image_url'].partition(',')[2])).hexdigest()
                           for _, _, _, part in mb.image_parts(transcoded)}
        for name in os.listdir(store.directory):
            if name.startswith('transcriptions.json'):
                continue
            self.assertNotIn(sha256_file(os.path.join(store.directory, name)), transcoded_shas)
        self.assertEqual(transcoded, out, 'refused replacements keep the images in place')

    # ---------------------------------------------------------------- failure shapes
    def test_a_rejected_candidate_is_counted_and_does_not_disable_the_pass(self):
        """A gate that says "no" must reject one candidate, not the whole request."""
        import random
        rng = random.Random(7)
        image = Image.new('RGB', (192, 192))
        pixels = image.load()
        for y in range(192):
            for x in range(192):
                pixels[x, y] = (rng.randint(0, 200), rng.randint(0, 200), rng.randint(0, 200))
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        items = [image_item(data_url(buffer.getvalue(), 'image/png')),
                 image_item(data_url(*photo_png(256)))]
        mean, chroma = tc._mean_abs_error, tc._chroma_loss
        tc._mean_abs_error = lambda a, b: 0.0
        tc._chroma_loss = lambda a, b: (2.0, 2.0)
        try:
            out, stats, _originals = tc.transcode_items(items, force=True, webp=True)
        finally:
            tc._mean_abs_error, tc._chroma_loss = mean, chroma
        self.assertEqual(2, stats['images'])
        self.assertGreaterEqual(stats['skipped'].get('chroma_webp', 0), 1)
        self.assertEqual(2, len(list(mb.image_parts(out))))

    def test_the_cache_is_bounded(self):
        store = mb.MediaStore(os.path.join(self.directory, 'bounded'),
                              index_ttl_seconds=3600, index_max_entries=2,
                              originals_max_bytes=10 ** 6, originals_min_age_seconds=0)
        os.makedirs(store.directory, exist_ok=True)
        stale = int(time.time()) - 7200
        with open(store.index_path, 'w', encoding='utf-8') as handle:
            json.dump({'a' * 64: {'text': GOOD_JSON, 'ns': store.namespace, 'at': stale}}, handle)
        store.save_text('b' * 64, GOOD_JSON, source='request')
        self.assertNotIn('a' * 64, store.load_index(), 'expired transcriptions are dropped')
        for digest in ('c' * 64, 'd' * 64, 'e' * 64):
            store.save_text(digest, GOOD_JSON, source='request')
        self.assertEqual(2, len(store.load_index()), 'the index stops growing')

        keep = hashlib.sha256(b'live').hexdigest()[:32] + '.jpg'
        with open(os.path.join(store.directory, keep), 'wb') as handle:
            handle.write(b'k' * 600)
        for index in range(3):
            name = hashlib.sha256(b'%d' % index).hexdigest()[:32] + '.jpg.%d.%d.tmp' % (index, index)
            path = os.path.join(store.directory, name)
            with open(path, 'wb') as handle:
                handle.write(b'z' * 600)
            os.utime(path, (stale, stale))
        tight = mb.MediaStore(store.directory, originals_max_bytes=900, originals_min_age_seconds=3600)
        tight.prune_originals()
        self.assertTrue(os.path.exists(os.path.join(store.directory, keep)),
                        'a failed write may not push the store into deleting a live frame')

    def test_a_foreign_file_in_the_cache_directory_is_never_deleted(self):
        root = os.path.join(self.directory, 'foreign')
        store = mb.MediaStore(root, originals_max_bytes=1, originals_min_age_seconds=0)
        os.makedirs(root, exist_ok=True)
        keepers = ('transcriptions.json', 'transcriptions-old-format-1759000000.json', 'notes.txt')
        for name in keepers:
            with open(os.path.join(root, name), 'wb') as handle:
                handle.write(b'x')
        store.prune_originals()
        self.assertEqual(0, store.prune_stats['originals_pruned'])
        for name in keepers:
            self.assertTrue(os.path.exists(os.path.join(root, name)), name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
