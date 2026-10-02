"""A checkpoint the client cannot upload is worse than no checkpoint at all.

The compaction result travels back on every following turn, so it has to fit the
same request limit the client uploads through. The private edition enforced that
before returning a checkpoint; the open-source edition sealed whatever it had, and
an image-heavy history produced an 88 MB checkpoint against a 64 MiB request limit
(adversarial review v2, F01). These tests pin the guard on the public edition.
"""
import base64
import io
import os
import shutil
import tempfile
import unittest

from PIL import Image

import media_transcode as tc
from adapter import Adapter
from test_media_layer import _Transport, config, data_url, image_item, photo_png


class CheckpointGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='checkpoint-guard-')
        self.addCleanup(shutil.rmtree, self.directory, True)

    def adapter(self, cap, **over):
        over.setdefault('media_store_dir', os.path.join(self.directory, 'cache'))
        over.setdefault('native_cache_dir', os.path.join(self.directory, 'native'))
        over.setdefault('media_budget_enabled', True)
        adapter = Adapter(config(compaction_checkpoint_bytes=cap, **over), b'7' * 32,
                          transport=_Transport(), credential=lambda: 'synthetic-token')
        # Offline transcription. It has to satisfy the same validator the real vision
        # route feeds (>=3 required keys), otherwise the frame is kept, not replaced.
        note = '{"timecode": "00:01", "slate": "synthetic", "composition": "console with two panes"}'
        adapter.transcribe_image = lambda raw, mime, digest, _note=note: _note
        return adapter

    def media_items(self, count=6, side=192):
        items = []
        for index in range(count):
            url = data_url(*photo_png(side, amplitude=6 + index))
            items.append(image_item(url))
        return items

    def test_a_checkpoint_over_the_cap_is_shrunk_rather_than_returned(self):
        # Transcoding alone gets these frames to ~39 KB, so the cap has to sit below
        # that for the transcription trade to be the only way through.
        cap = 25_000
        adapter = self.adapter(cap)
        items = self.media_items()
        before = Adapter(config(compaction_checkpoint_bytes=0), b'7' * 32,
                         transport=_Transport(), credential=lambda: 'synthetic-token')
        naive = before.seal({'version': 2, 'summary': 'state', 'retained': items})
        self.assertGreater(len(naive), cap, 'fixture must actually exceed the cap')
        sealed = adapter.seal_checkpoint('state', items)
        self.assertLessEqual(len(sealed), cap)
        opened = adapter.open(sealed)
        self.assertEqual(opened['summary'], 'state')
        self.assertTrue(opened['retained'])
        self.assertGreater(adapter.stats.get('media_budget_replaced', 0), 0)

    def test_a_checkpoint_that_is_already_small_is_sealed_unchanged(self):
        adapter = self.adapter(4 * 1024 * 1024)
        items = [image_item(data_url(*(photo_png(64))))]
        sealed = adapter.seal_checkpoint('small state', items)
        opened = adapter.open(sealed)
        self.assertEqual(opened['summary'], 'small state')
        self.assertEqual(adapter.stats.get('media_budget_replaced', 0), 0)

    def test_no_cap_configured_keeps_the_previous_behaviour(self):
        adapter = self.adapter(0)
        items = self.media_items(count=3)
        sealed = adapter.seal_checkpoint('uncapped state', items)
        self.assertEqual(adapter.open(sealed)['summary'], 'uncapped state')


if __name__ == '__main__':
    unittest.main(verbosity=2)
