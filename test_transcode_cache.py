"""The transcode result cache must survive a service restart.

The in-process map alone meant that after the adapter restarted (SIGTERM during the
2026-10-02 incident, and again while retrying), the next attempt re-encoded every image
in the history: ~33 s of CPU on a 48-frame request, spent on exactly the retry path that
can least afford it. The vision transcription cache next to it already survives restarts.
These tests pin the persisted layer: restart reuse, corrupt-entry tolerance, limits, and
the "no directory configured" default.
"""
import base64
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import time
import unittest

import media_transcode
from test_media_layer import photo_png

PNG, _ = photo_png(256)
# Derived from the module, not re-implemented here: the key gained the quality,
# source mime and a pipeline fingerprint after an earlier version let a q40 call
# reuse a q85 result (adversarial review v2).
SOURCE_KEY = media_transcode.cache_key(PNG, 'image/png',
                                       media_transcode.DEFAULT_QUALITY, True)


def data_url(payload, mime='image/png'):
    return 'data:%s;base64,%s' % (mime, base64.b64encode(payload).decode())


def entry_path(directory, key=SOURCE_KEY):
    base = os.path.join(directory, 'transcode',
                        'v%d' % media_transcode.TRANSCODE_CACHE_VERSION)
    return os.path.join(base, key + '.tc')


class DiskCacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='transcode-cache-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        media_transcode.cache_clear(disk=True)
        media_transcode.configure_cache(os.path.join(self.dir, 'transcode'))

    def tearDown(self):
        media_transcode.configure_cache(None)
        media_transcode.cache_clear(disk=True)

    def transcode(self):
        return media_transcode.transcode_image_url(data_url(PNG))

    def test_result_survives_restart_without_re_encoding(self):
        url, decision, before, after = self.transcode()
        self.assertNotIn(decision, ('not_image', 'no_pillow', 'undecodable'))
        self.assertGreater(media_transcode.cache_info()['disk_writes'], 0)

        # A restart keeps the directory and loses only the process-local map.
        media_transcode.cache_clear()
        self.assertEqual(media_transcode.cache_info()['entries'], 0)

        again = self.transcode()
        self.assertEqual(again, (url, decision, before, after))
        info = media_transcode.cache_info()
        self.assertGreaterEqual(info['disk_hits'], 1)
        self.assertEqual(info['disk_errors'], 0)

    def test_disabled_cache_does_not_create_a_directory(self):
        spare = tempfile.mkdtemp(prefix='transcode-off-')
        self.addCleanup(shutil.rmtree, spare, True)
        media_transcode.configure_cache(None)
        self.transcode()
        info = media_transcode.cache_info()
        self.assertFalse(info['disk'])
        self.assertEqual(info['disk_writes'], 0)
        self.assertFalse(os.path.exists(os.path.join(spare, 'transcode')))

    def test_corrupt_entry_is_ignored_and_counted(self):
        os.makedirs(os.path.dirname(entry_path(self.dir)), exist_ok=True)
        with open(entry_path(self.dir), 'wb') as handle:
            handle.write(b'CXUTC1\nnot json\n')
        _, decision, _, _ = self.transcode()
        self.assertNotIn(decision, ('not_image', 'no_pillow'))
        self.assertGreaterEqual(media_transcode.cache_info()['disk_errors'], 1)

    def test_foreign_version_envelope_is_a_miss_not_an_error(self):
        os.makedirs(os.path.dirname(entry_path(self.dir)), exist_ok=True)
        header = json.dumps({'v': 999, 'key': SOURCE_KEY, 'decision': 'png', 'after': 1,
                             'url_bytes': 1, 'source_bytes': 1}).encode()
        with open(entry_path(self.dir), 'wb') as handle:
            handle.write(media_transcode.DISK_MAGIC + header + b'\n' + b'x')
        _, decision, _, _ = self.transcode()
        self.assertNotIn(decision, ('not_image', 'no_pillow'))
        self.assertEqual(media_transcode.cache_info()['disk_errors'], 0)

    def test_prune_drops_entries_over_the_byte_limit(self):
        media_transcode.cache_clear(disk=True)
        media_transcode.configure_cache(os.path.join(self.dir, 'transcode'), max_bytes=1)
        self.transcode()
        media_transcode._disk_prune()
        base = os.path.dirname(entry_path(self.dir))
        self.assertEqual([n for n in os.listdir(base) if n.endswith('.tc')], [])

    def test_a_different_quality_or_mime_is_a_different_entry(self):
        # A q40 request used to reuse the q85 result because only the source bytes and
        # the webp flag were hashed: the cached frame was not the frame this pipeline
        # would produce.
        base = media_transcode.cache_key(PNG, 'image/png', 85, True)
        self.assertNotEqual(base, media_transcode.cache_key(PNG, 'image/png', 40, True))
        self.assertNotEqual(base, media_transcode.cache_key(PNG, 'image/jpeg', 85, True))
        self.assertNotEqual(base, media_transcode.cache_key(PNG, 'image/png', 85, False))
        before = media_transcode.pipeline_fingerprint()
        media_transcode._PIPELINE_FINGERPRINT = None
        media_transcode.LOSSY_SOURCE_MIN_SAVING = 0.99
        try:
            self.assertNotEqual(before, media_transcode.pipeline_fingerprint())
            self.assertNotEqual(base, media_transcode.cache_key(PNG, 'image/png', 85, True))
        finally:
            media_transcode.LOSSY_SOURCE_MIN_SAVING = 0.25
            media_transcode._PIPELINE_FINGERPRINT = None
        self.assertEqual(before, media_transcode.pipeline_fingerprint())

    def test_a_tampered_payload_is_a_miss_and_is_counted(self):
        self.transcode()
        path = entry_path(self.dir)
        with open(path, 'rb') as handle:
            raw = handle.read()
        header, _, _ = raw[len(media_transcode.DISK_MAGIC):].partition(b'\n')
        envelope = json.loads(header.decode())
        payload = base64.b64encode(b'not the frame that was cached').decode().encode()
        envelope['url_bytes'] = len(payload)
        with open(path, 'wb') as handle:
            handle.write(media_transcode.DISK_MAGIC
                         + json.dumps(envelope).encode() + b'\n' + payload)
        media_transcode.cache_clear()
        _, decision, _, _ = self.transcode()
        self.assertNotIn(decision, ('not_image', 'no_pillow'))
        self.assertGreaterEqual(media_transcode.cache_info()['disk_errors'], 1)

    def test_prune_sweeps_orphan_temps_and_old_version_directories(self):
        self.transcode()
        base = os.path.dirname(entry_path(self.dir))
        orphan = os.path.join(base, 'deadbeef.tc.999.1.tmp')
        with open(orphan, 'wb') as handle:
            handle.write(b'x')
        old_version = os.path.join(self.dir, 'transcode', 'v0')
        os.makedirs(old_version, exist_ok=True)
        with open(os.path.join(old_version, 'stale.tc'), 'wb') as handle:
            handle.write(b'y')
        media_transcode._disk_prune(now=time.time() + 2 * media_transcode.ORPHAN_TEMP_SECONDS)
        self.assertFalse(os.path.exists(orphan))
        self.assertFalse(os.path.exists(old_version))

    def test_clear_with_disk_removes_entries(self):
        self.transcode()
        media_transcode.cache_clear(disk=True)
        base = os.path.dirname(entry_path(self.dir))
        self.assertEqual([n for n in os.listdir(base) if n.endswith('.tc')], [])


class AdapterWiringTests(unittest.TestCase):
    def setUp(self):
        from adapter import Adapter
        self.Adapter = Adapter
        self.addCleanup(media_transcode.configure_cache, None)
        self.addCleanup(media_transcode.cache_clear, True)

    def build(self, cfg):
        return self.Adapter({'upstream': 'https://gateway.invalid/v1', 'port': 0,
                             'compactor_model': 'gpt-6-sol', 'compactor_effort': 'medium',
                             **cfg},
                            secrets.token_bytes(32), transport=None, credential=lambda: 'x')

    def test_disabled_without_a_configured_directory(self):
        media_transcode.configure_cache(None)
        self.build({})
        self.assertFalse(media_transcode.cache_info()['disk'])

    def test_enabled_and_created_when_the_config_names_a_directory(self):
        directory = tempfile.mkdtemp(prefix='adapter-cache-')
        self.addCleanup(shutil.rmtree, directory, True)
        target = os.path.join(directory, 'transcode-cache')
        self.build({'transcode_cache_dir': target})
        self.assertTrue(media_transcode.cache_info()['disk'])
        self.assertTrue(os.path.isdir(target))

    def test_defaults_to_a_sibling_of_the_media_cache(self):
        root = tempfile.mkdtemp(prefix='adapter-media-')
        self.addCleanup(shutil.rmtree, root, True)
        self.build({'media_cache_dir': os.path.join(root, 'media-cache')})
        self.assertTrue(media_transcode.cache_info()['disk'])
        self.assertTrue(os.path.isdir(os.path.join(root, 'transcode-cache')))


if __name__ == '__main__':
    unittest.main()
