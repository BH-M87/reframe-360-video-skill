"""Synthetic ISO BMFF fixtures; no user media is copied into this repository."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'probe_media.py'

def box(kind, payload=b'', extended=False):
    return (struct.pack('>I4sQ', 1, kind, len(payload) + 16) if extended else
            struct.pack('>I4s', len(payload) + 8, kind)) + payload

def track(tid, handler=b'vide', name=b'Video', codec=b'hvc1', width=3840,
          height=3840, count=71, scale=24000, delta=1001, compact=False):
    tkhd = bytearray(84)
    struct.pack_into('>I', tkhd, 12, tid)
    struct.pack_into('>II', tkhd, 76, width << 16, height << 16)
    mdhd = b'\0' * 12 + struct.pack('>II', scale, count * delta) + b'\0' * 4
    hdlr = b'\0' * 8 + handler + b'\0' * 12 + name + b'\0'
    entry = bytearray(78 if handler == b'vide' else 28)
    if handler == b'vide':
        struct.pack_into('>HH', entry, 24, width, height)
    stsd = b'\0' * 4 + struct.pack('>I', 1) + box(codec, entry)
    stsz = b'\0' * 4 + struct.pack('>II', 10, count)
    stts = b'\0' * 4 + struct.pack('>III', 1, count, delta)
    sizes = box(b'stz2', b'\0' * 8) if compact else box(b'stsz', stsz)
    stbl = box(b'stbl', box(b'stsd', stsd) + sizes + box(b'stts', stts))
    return box(b'trak', box(b'tkhd', tkhd) + box(b'mdia', box(b'mdhd', mdhd) +
               box(b'hdlr', hdlr) + box(b'minf', stbl)))

def movie(tail=False, fragmented=False, compact=False):
    moov = box(b'moov', track(11) + track(12) + track(20, codec=b'mjpg',
        width=1440, height=720, count=1) + track(30, b'soun', b'Audio', b'mp4a') +
        track(31, b'meta', b'CameraMetadata', b'djmd', compact=compact) +
        (box(b'mvex') if fragmented else b''), extended=True)
    ftyp, mdat = box(b'ftyp', b'isom\0\0\0\0'), box(b'mdat', b'1234')
    return ftyp + (mdat + moov if tail else moov + mdat) + (box(b'moof') if fragmented else b'')

class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='reframe-probe-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = 0

    def probe(self, payload, extra=(), expected=0):
        self.assertTrue(SCRIPT.is_file(), 'media probe CLI must be implemented')
        source = self.root / 'clip.OSV'
        output = self.root / ('probe-' + str(self.calls) + '.json')
        self.calls += 1
        source.write_bytes(payload)
        run = subprocess.run([sys.executable, str(SCRIPT), str(source), '--output',
            str(output), '--ffprobe', str(self.root / 'missing'), *extra],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, expected, run.stderr + run.stdout)
        result = json.loads(output.read_text())
        self.assertEqual(json.loads(run.stdout), result)
        self.assertEqual(source.read_bytes(), payload)
        return result

    def test_dual_video_cover_data_and_moov_positions(self):
        for tail in (False, True):
            with self.subTest(tail=tail):
                r = self.probe(movie(tail))
                self.assertEqual(r['status'], 'ok')
                self.assertEqual(r['selected_probe'], 'stdlib_iso_bmff')
                self.assertEqual(r['candidate_tracks'], {'video': [0, 1], 'audio': [3], 'data': [4], 'cover': [2]})
                self.assertEqual(r['tracks'][0]['track_id'], 11)
                self.assertEqual(r['tracks'][4]['handler_name'], 'CameraMetadata')
                self.assertEqual(r['tracks'][0]['sample_count'], 71)
                self.assertAlmostEqual(r['tracks'][0]['duration_seconds'], 71 * 1001 / 24000)
                self.assertAlmostEqual(r['tracks'][0]['avg_frame_rate'], 24000 / 1001)
                self.assertIsNone(r['tracks'][2]['attached_pic'])
                self.assertTrue(r['tracks'][2]['cover_evidence'])
                self.assertEqual(r['gyro_status'], 'unknown_unverified')

    def test_rejects_truncated_malformed_and_oversize_boxes(self):
        bad = [movie()[:-1], box(b'ftyp') + struct.pack('>I4s', 7, b'moov'),
               box(b'ftyp') + struct.pack('>I4sQ', 1, b'moov', 1000),
               box(b'ftyp') + box(b'moov', b'abc'), b'\0\0\0\x01moov']
        for payload in bad:
            with self.subTest(payload=payload[-20:]):
                r = self.probe(payload, expected=2)
                self.assertEqual(r['status'], 'unprobeable')
                self.assertTrue(r['warnings'])

    def test_fragmented_tables_are_not_reported_as_complete(self):
        r = self.probe(movie(fragmented=True), expected=2)
        self.assertEqual(r['status'], 'partial')
        self.assertIn('fragmented_mp4', r['unsupported'])
        self.assertIsNone(r['tracks'][0]['sample_count'])

    def test_stz2_is_explicitly_unsupported(self):
        r = self.probe(movie(compact=True))
        self.assertEqual(r['status'], 'partial')
        self.assertIn('stz2_track_31', r['unsupported'])
        self.assertIsNone(r['tracks'][4]['sample_count'])

    def test_non_mp4_without_ffprobe_is_unprobeable(self):
        r = self.probe(b'not a media file', expected=2)
        self.assertEqual(r['tools']['ffprobe']['status'], 'not_found')
        self.assertEqual(r['status'], 'unprobeable')

    def test_ffprobe_has_timeout_and_explicit_probe_limits(self):
        tool = self.root / 'fake-ffprobe'
        args_file = self.root / 'arguments.json'
        tool.write_text('#!' + sys.executable + '\nimport json,sys,time\n'
            'from pathlib import Path\nPath(' + repr(str(args_file)) + ').write_text(json.dumps(sys.argv))\ntime.sleep(10)\n')
        tool.chmod(0o700)
        r = self.probe(movie(), ('--ffprobe', str(tool), '--timeout', '2'))
        self.assertEqual(r['tools']['ffprobe']['status'], 'timeout')
        args = json.loads(args_file.read_text())
        for flag in ('-probesize', '-analyzeduration', '-max_alloc'):
            self.assertIn(flag, args)
        self.assertEqual(r['selected_probe'], 'stdlib_iso_bmff')

    def test_ffprobe_can_probe_non_bmff_and_preserves_disposition(self):
        tool = self.root / 'fake-ffprobe'
        data = {'format': {'duration': '2.5'}, 'streams': [
            {'index': 2, 'id': '0x3', 'codec_type': 'video', 'codec_name': 'mjpeg',
             'width': 1440, 'height': 720, 'disposition': {'attached_pic': 1}},
            {'index': 3, 'codec_type': 'video', 'codec_name': 'hevc', 'width': 5000,
             'height': 2500, 'avg_frame_rate': '25/1', 'nb_frames': '63'}]}
        tool.write_text('#!' + sys.executable + '\nprint(' + repr(json.dumps(data)) + ')\n')
        tool.chmod(0o700)
        r = self.probe(b'RIFFnot-bmff', ('--ffprobe', str(tool)))
        self.assertEqual(r['selected_probe'], 'ffprobe')
        self.assertEqual(r['candidate_tracks']['video'], [3])
        self.assertTrue(r['tracks'][0]['attached_pic'])
        self.assertEqual(r['tracks'][1]['sample_count'], 63)

    def test_hardlinked_output_cannot_overwrite_source(self):
        source, output = self.root / 'clip.mp4', self.root / 'same.mp4'
        source.write_bytes(movie())
        os.link(source, output)
        run = subprocess.run([sys.executable, str(SCRIPT), str(source), '--output',
            str(output)], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(source.read_bytes(), movie())

    def test_fragment_without_initialization_is_explicitly_unsupported(self):
        r = self.probe(box(b'moof') + box(b'mdat'), expected=2)
        self.assertIn('fragmented_mp4', r['unsupported'])

    def test_nonfinite_ffprobe_numbers_remain_unknown(self):
        tool = self.root / 'fake-ffprobe'
        data = {'streams': [{'index': 0, 'codec_type': 'video',
            'duration': 'NaN', 'avg_frame_rate': 'Infinity/1'}]}
        tool.write_text('#!' + sys.executable + '\nprint(' + repr(json.dumps(data)) + ')\n')
        tool.chmod(0o700)
        r = self.probe(b'RIFFnot-bmff', ('--ffprobe', str(tool)))
        self.assertIsNone(r['tracks'][0]['duration_seconds'])
        self.assertIsNone(r['tracks'][0]['avg_frame_rate'])

    def test_ffprobe_failure_preserves_status_and_falls_back(self):
        tool = self.root / 'fake-ffprobe'
        tool.write_text('#!' + sys.executable + '\nimport sys\nsys.exit(3)\n')
        tool.chmod(0o700)
        r = self.probe(movie(), ('--ffprobe', str(tool)))
        self.assertEqual(r['tools']['ffprobe']['status'], 'failed')
        self.assertEqual(r['tools']['ffprobe']['returncode'], 3)
        self.assertEqual(r['selected_probe'], 'stdlib_iso_bmff')

    def test_large_metadata_box_does_not_count_as_many_boxes(self):
        r = self.probe(box(b'ftyp') + box(b'moov',
            box(b'free', b'0' * 900000) + track(72)))
        self.assertEqual(r['tracks'][0]['track_id'], 72)

    def test_truncated_variable_sample_sizes_are_rejected(self):
        mdhd = b'\0' * 12 + struct.pack('>II', 48000, 2048) + b'\0' * 4
        tkhd = b'\0' * 12 + struct.pack('>I', 9) + b'\0' * 68
        hdlr = b'\0' * 8 + b'soun' + b'\0' * 12 + b'Audio'
        sizes = box(b'stsz', b'\0' * 4 + struct.pack('>II', 0, 3) + struct.pack('>I', 5))
        stsd = box(b'stsd', b'\0' * 4 + struct.pack('>I', 1) + box(b'mp4a', b'\0' * 28))
        stbl = box(b'stbl', stsd + sizes)
        payload = box(b'moov', box(b'trak', box(b'tkhd', tkhd) + box(b'mdia',
            box(b'mdhd', mdhd) + box(b'hdlr', hdlr) + box(b'minf', stbl))))
        r = self.probe(payload, expected=2)
        self.assertIn('truncated stsz', r['warnings'][0])

    def test_cover_scope_and_timecode_are_explicit(self):
        r = self.probe(box(b'ftyp') + box(b'moov', track(8, b'tmcd',
            b'Timecode', b'tmcd') + box(b'udta', box(b'covr', b'uninspected'))))
        self.assertEqual(r['cover_detection_scope'], 'track_samples_only')
        self.assertEqual(r['metadata_cover_status'], 'unknown_not_inspected')
        self.assertEqual(r['tracks'][0]['role'], 'timecode')
        self.assertEqual(r['candidate_tracks']['data'], [])

    def test_zero_timescale_is_not_a_clippable_video(self):
        r = self.probe(box(b'moov', track(71, scale=0)), expected=2)
        self.assertNotIn(0, r['candidate_tracks'].get('video', []))
        self.assertNotEqual(r['status'], 'ok')

    def test_zero_sample_duration_is_not_a_clippable_video(self):
        r = self.probe(box(b'moov', track(71, delta=0)), expected=2)
        self.assertNotIn(0, r['candidate_tracks'].get('video', []))
        self.assertNotEqual(r['status'], 'ok')

    def test_empty_video_is_inventory_only_with_unknown_timeline(self):
        r = self.probe(box(b'moov', track(71, count=0)), expected=2)
        self.assertEqual(r['status'], 'partial')
        self.assertEqual(r['tracks'][0]['sample_count'], 0)
        self.assertEqual(r['tracks'][0]['timeline_status'], 'unknown_empty')
        self.assertEqual(r['candidate_tracks']['video'], [])

    def test_existing_other_media_output_is_unchanged(self):
        self.assert_existing_output_unchanged(movie())

    def test_existing_json_output_is_unchanged(self):
        self.assert_existing_output_unchanged(b'{"existing":true}')

    def assert_existing_output_unchanged(self, original):
        source, output = self.root / 'input.mp4', self.root / 'existing-report'
        source.write_bytes(movie())
        output.write_bytes(original)
        run = subprocess.run([sys.executable, str(SCRIPT), str(source), '--output',
            str(output), '--ffprobe', str(self.root / 'missing')],
            capture_output=True, text=True, timeout=5)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(source.read_bytes(), movie())

    def test_output_hardlink_created_during_probe_is_unchanged(self):
        source, output, tool = self.root / 'input.mp4', self.root / 'late-report', self.root / 'fake-ffprobe'
        source.write_bytes(movie())
        tool.write_text('#!' + sys.executable + '\nimport os\nos.link(' +
            repr(str(source)) + ', ' + repr(str(output)) + ')\n' +
            'print(\'{"streams":[{"index":0,"codec_type":"video"}]}\')\n')
        tool.chmod(0o700)
        run = subprocess.run([sys.executable, str(SCRIPT), str(source), '--output',
            str(output), '--ffprobe', str(tool)], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(run.returncode, 0)
        self.assertTrue(output.exists())
        self.assertEqual(output.read_bytes(), movie())
        self.assertEqual(source.read_bytes(), movie())

if __name__ == '__main__':
    unittest.main()
