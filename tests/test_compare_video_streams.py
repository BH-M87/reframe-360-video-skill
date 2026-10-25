import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from fractions import Fraction
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/compare_video_streams.py'


class CompareVideoStreamsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import imageio_ffmpeg
        cls.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        cls.temp = tempfile.TemporaryDirectory(prefix='reframe-stream-identity-', dir='/tmp')
        cls.root = Path(cls.temp.name)
        cls.before = cls.root / 'before.mp4'
        cls.audio_changed = cls.root / 'audio-changed.mp4'
        cls.run_ffmpeg('-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=24',
                       '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                       '-t', '2', '-vf', 'setsar=1', '-c:v', 'libx264', '-threads', '1',
                       '-pix_fmt', 'yuv420p', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                       '-colorspace', 'bt709', '-color_range', 'tv', '-c:a', 'aac', str(cls.before))
        cls.run_ffmpeg('-i', str(cls.before), '-map', '0:v:0', '-map', '0:a:0',
                       '-c:v', 'copy', '-af', 'volume=0.1', '-c:a', 'aac', str(cls.audio_changed))

    @classmethod
    def run_ffmpeg(cls, *args):
        subprocess.run([cls.ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-n',
                        *args], check=True, capture_output=True, timeout=40)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def module(self):
        spec = importlib.util.spec_from_file_location('stream_identity', SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def compare(self, name, after=None, extra=()):
        report = self.root / f'{name}.json'
        result = subprocess.run([sys.executable, str(SCRIPT), '--before', str(self.before),
                                 '--after', str(after or self.audio_changed), '--report', str(report),
                                 *extra], capture_output=True, text=True, timeout=40)
        self.assertTrue(report.exists(), result.stdout + result.stderr)
        return result, json.loads(report.read_text())

    def test_changed_audio_keeps_exact_video_evidence(self):
        result, report = self.compare('audio')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report['status'], 'exact_video_equivalent')
        self.assertEqual(report['before']['packet_count'], 48)
        self.assertTrue(report['checks']['packet_payloads_equal'])
        self.assertTrue(report['checks']['packet_timing_equal'])
        self.assertTrue(report['checks']['inputs_unchanged'])
        self.assertTrue(report['audio_and_container_reverification_required'])
        self.assertFalse(report['visual_acceptance_granted'])
        self.assertNotEqual(report['files_before']['before']['sha256'],
                            report['files_before']['after']['sha256'])

    def test_different_time_base_with_exact_equal_timing_passes(self):
        after = self.root / 'new-timescale.mp4'
        self.run_ffmpeg('-i', str(self.before), '-c', 'copy', '-video_track_timescale', '24000', str(after))
        result, report = self.compare('timescale', after)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(report['before']['stream_time_base'], report['after']['stream_time_base'])
        self.assertTrue(report['checks']['packet_timing_equal'])

    def test_changed_pixels_fail(self):
        after = self.root / 'pixels.mp4'
        self.run_ffmpeg('-i', str(self.before), '-vf', 'hflip', '-c:v', 'libx264',
                        '-threads', '1', '-c:a', 'copy', str(after))
        result, report = self.compare('pixels', after)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['status'], 'different')
        self.assertFalse(report['checks']['packet_payloads_equal'])

    def test_pts_offset_fails_even_with_identical_payload(self):
        after = self.root / 'shifted.mp4'
        self.run_ffmpeg('-i', str(self.before), '-c', 'copy', '-output_ts_offset', '0.25', str(after))
        result, report = self.compare('shifted', after)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(report['checks']['packet_payloads_equal'])
        self.assertFalse(report['checks']['packet_timing_equal'])

    def test_sar_color_and_rotation_are_not_ignored(self):
        module = self.module()
        original = {'codec': 'h264', 'extradata_sha256': 'a', 'width': 160, 'height': 90,
                    'pixel_format': 'yuv420p', 'sar': '1/1',
                    'color': {'primaries': 1, 'trc': 1, 'space': 1, 'range': 1},
                    'display_metadata': {}}
        for field, value in [('codec', 'hevc'), ('sar', '2/1'),
                             ('color', {'primaries': 9, 'trc': 16, 'space': 9, 'range': 1}),
                             ('display_metadata', {'rotate': '90'})]:
            with self.subTest(field=field):
                changed = dict(original, **{field: value})
                self.assertIn(field, module.metadata_differences(original, changed))
        after = self.root / 'sar.mp4'
        self.run_ffmpeg('-i', str(self.before), '-c', 'copy', '-bsf:v', 'h264_metadata=sample_aspect_ratio=2/1', str(after))
        result, report = self.compare('sar', after)
        self.assertEqual(result.returncode, 1)
        self.assertIn('codec_sar', report['metadata_differences'])
        recolor = self.root / 'recolor.mp4'
        self.run_ffmpeg('-i', str(self.before), '-c', 'copy', '-bsf:v',
                        'h264_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9', str(recolor))
        result, report = self.compare('recolor', recolor)
        self.assertEqual(result.returncode, 1)
        self.assertIn('color', report['metadata_differences'])
        rotate = self.root / 'rotation.mp4'
        self.run_ffmpeg('-display_rotation', '90', '-i', str(self.before), '-c', 'copy', str(rotate))
        result, report = self.compare('rotation', rotate)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(report['checks']['packet_payloads_equal'])
        self.assertIn('first_frame_side_data', report['metadata_differences'])

    def test_missing_or_invalid_packet_time_is_inconclusive(self):
        module = self.module()
        for field, value in [('pts', None), ('dts', None), ('duration', None),
                             ('duration', 0), ('time_base', None), ('time_base', Fraction(0))]:
            with self.subTest(field=field, value=value):
                data = dict(pts=5, dts=4, duration=1, time_base=Fraction(1, 24))
                data[field] = value
                timing, reason = module.packet_timing(SimpleNamespace(**data))
                self.assertIsNone(timing)
                self.assertTrue(reason)
        t1, reason = module.packet_timing(SimpleNamespace(pts=3, dts=-1, duration=1, time_base=Fraction(1,24)))
        t2, _ = module.packet_timing(SimpleNamespace(pts=3000, dts=-1000, duration=1000, time_base=Fraction(1,24000)))
        self.assertIsNone(reason)
        self.assertEqual(t1, t2)

    def test_container_color_override_cannot_be_hidden_by_decoder(self):
        after = self.root / 'container-color.mp4'
        self.run_ffmpeg('-i', str(self.before), '-c', 'copy', '-color_primaries', '9',
                        '-color_trc', '16', '-colorspace', '9', str(after))
        result, report = self.compare('container-color', after)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(report['checks']['packet_payloads_equal'])
        self.assertIn('container_codec_parameters', report['metadata_differences'])

    def test_existing_report_input_alias_and_dangling_link_are_preserved(self):
        original = self.before.read_bytes()
        for kind in ('existing', 'input', 'hardlink', 'symlink', 'dangling'):
            report = self.root / f'refuse-{kind}.json'
            if kind == 'existing': report.write_text('keep')
            elif kind == 'input': report = self.before
            elif kind == 'hardlink': os.link(self.before, report)
            elif kind == 'symlink': report.symlink_to(self.before)
            else: report.symlink_to(self.root / 'absent')
            result = subprocess.run([sys.executable, str(SCRIPT), '--before', str(self.before),
                                     '--after', str(self.audio_changed), '--report', str(report)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(self.before.read_bytes(), original)
            if kind == 'existing': self.assertEqual(report.read_text(), 'keep')
            if kind == 'dangling': self.assertTrue(report.is_symlink())

    def test_invalid_stream_and_malformed_media_produce_inconclusive_report(self):
        result, report = self.compare('bad-stream', extra=('--video-stream', '4'))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report['status'], 'inconclusive')
        malformed = self.root / 'not-media.mp4'
        malformed.write_bytes(b'not media')
        result, report = self.compare('malformed', malformed)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report['status'], 'inconclusive')

    def test_help_has_no_dependency_requirement(self):
        result = subprocess.run([sys.executable, '-S', str(SCRIPT), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--before', result.stdout)

    def test_source_change_during_comparison_cannot_inherit_evidence(self):
        module = self.module()
        mutable = self.root / 'mutable.mp4'
        mutable.write_bytes(self.audio_changed.read_bytes())
        report = self.root / 'mutated-during-check.json'
        original = module.compare
        def mutate_after_read(*args):
            result = original(*args)
            with mutable.open('ab') as handle:
                handle.write(b'changed after packet inspection')
            return result
        with mock.patch.object(module, 'compare', side_effect=mutate_after_read):
            status = module.main(['--before', str(self.before), '--after', str(mutable), '--report', str(report)])
        self.assertEqual(status, 2)
        data = json.loads(report.read_text())
        self.assertEqual(data['status'], 'inconclusive')
        self.assertFalse(data['checks']['inputs_unchanged'])

    def test_report_publication_race_preserves_competitor(self):
        module = self.module()
        report = self.root / 'racing-report.json'
        link = module.os.link
        def race(stage, destination):
            Path(destination).write_text('competitor')
            return link(stage, destination)
        with mock.patch.object(module.os, 'link', side_effect=race):
            status = module.main(['--before', str(self.before), '--after', str(self.audio_changed), '--report', str(report)])
        self.assertEqual(status, 2)
        self.assertEqual(report.read_text(), 'competitor')


if __name__ == '__main__':
    unittest.main()
