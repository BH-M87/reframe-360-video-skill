import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/verify_export.py'


class VerifyExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import av
        import imageio_ffmpeg
        cls.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        cls.temp = tempfile.TemporaryDirectory(prefix='reframe-export-', dir='/tmp')
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / 'source.mp4'
        cls.output = cls.root / 'output.mp4'
        command = [cls.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24',
                   '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2',
                   '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(cls.source)]
        subprocess.run(command, check=True, capture_output=True)
        subprocess.run([cls.ffmpeg, '-v', 'error', '-i', str(cls.source), '-c', 'copy', str(cls.output)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_verify(self, name, *extra, output=None, expected=48):
        report = self.root / (name + '.json')
        command = [sys.executable, str(SCRIPT), '--source', str(self.source), '--output', str(output or self.output),
                   '--expected-frames', str(expected), '--fps', '24', '--width', '320', '--height', '180',
                   '--report', str(report), *extra]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertTrue(report.exists(), result.stderr)
        return result, json.loads(report.read_text())

    def test_arbitrary_export_full_decode_audio_identity_and_contacts(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result, report = self.run_verify('valid', '--audio-copy-from', str(self.source), '--contacts-dir',
                                         str(self.root / 'contacts'), '--privacy-window', '.25:.75:.25')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report['technical_status'], 'passed')
        self.assertTrue(report['checks']['container_format']['passed'])
        self.assertTrue(report['checks']['audio_chunk_continuity']['passed'])
        self.assertTrue(report['visual_review_required'])
        self.assertEqual(report['decoded']['video_frames'], 48)
        self.assertTrue(report['checks']['audio_payload_identity']['passed'])
        self.assertTrue(report['contact_sheets']['pages'])
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(report['faststart']['status'], 'not_checked')
        self.assertIn('color_primaries', report['not_asserted'])
        self.assertEqual(report['metadata_assertions']['color_primaries']['status'], 'recorded_not_asserted')
        self.assertTrue(report['checks']['audio_finite']['passed'])
        self.assertGreater(report['decoded_audio']['rms'], 0)
        self.assertIn('color_tags', report['metadata'])
        self.assertEqual(report['metadata']['audio_sample_rate'], 48000)

    def test_contact_label_strip_preserves_all_resized_image_pixels(self):
        import av
        import importlib.util
        import numpy as np
        spec = importlib.util.spec_from_file_location('portable_verify_export', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pixels = np.full((180, 320, 3), [80, 150, 190], dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(pixels, format='rgb24')
        contacts = module.Contacts(self.root / 'strip-contacts', [0])
        contacts.add(frame, 0, 0)
        cell = contacts.cells[0]
        self.assertEqual(cell.size, (320, 204))
        self.assertEqual(cell.crop((0, 0, 320, 180)).tobytes(), frame.to_image().tobytes())
        self.assertEqual(cell.getpixel((0, 180)), (23, 23, 23))

    def test_output_cannot_be_its_own_source_or_audio_reference_via_aliases(self):
        aliases = [self.output, self.root / 'output-hardlink.mp4', self.root / 'output-symlink.mp4']
        os.link(self.output, aliases[1])
        aliases[2].symlink_to(self.output)
        before = self.output.read_bytes()
        for role in ('source', 'audio-reference'):
            for index, alias in enumerate(aliases):
                with self.subTest(role=role, alias=index):
                    report = self.root / f'alias-{role}-{index}.json'
                    command = [sys.executable, str(SCRIPT), '--source', str(alias if role == 'source' else self.source),
                               '--output', str(self.output), '--expected-frames', '48', '--fps', '24',
                               '--width', '320', '--height', '180', '--report', str(report)]
                    if role == 'audio-reference':
                        command += ['--audio-copy-from', str(alias)]
                    result = subprocess.run(command, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn('distinct', result.stderr)
                    self.assertFalse(report.exists())
        self.assertEqual(before, self.output.read_bytes())

    def test_internal_audio_gaps_and_overlaps_fail_even_when_endpoints_match(self):
        import av
        import importlib.util
        from fractions import Fraction
        from types import SimpleNamespace as NS
        import numpy as np
        spec = importlib.util.spec_from_file_location('audio_gap_verify_export', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        video_frames = []
        for index in range(48):
            frame = av.VideoFrame.from_ndarray(np.full((180, 320, 3), 100, dtype=np.uint8), format='rgb24')
            frame.pts, frame.time_base = index, Fraction(1, 24)
            video_frames.append(frame)
        video = NS(codec_context=NS(name='h264', format=NS(name='yuv420p'), color_primaries=2, color_trc=2, colorspace=2, color_range=0), width=320, height=180, average_rate=Fraction(24), sample_aspect_ratio=Fraction(1))
        audio = NS(codec_context=NS(name='aac', sample_rate=1000, layout=NS(channels=[None])))
        args = NS(source=self.source, output=self.output, audio_copy_from=None, contacts_dir=None,
                  expected_frames=48, width=320, height=180, rate=Fraction(24), av_tolerance=.15,
                  allow_no_audio=False, video_codec='h264', audio_codec='aac', pixel_format='yuv420p',
                  container_format='mp4', privacy_window=[], sar=Fraction(1), color_primaries=None, color_trc=None,
                  colorspace=None, color_range=None, sample_rate=None, channels=None, audio_peak_limit=1.0, require_faststart=False)
        for name, chunks, statistic in [('gap', [(0, 100), (100, 100), (1900, 100)], 'max_gap_s'),
                                         ('overlap', [(0, 100), (50, 100), (150, 1850)], 'max_overlap_s'),
                                         ('rounding', [(0, 100), (101, 100), (201, 1799)], None)]:
            with self.subTest(name=name):
                audio_frames = [NS(pts=pts, time_base=Fraction(1, 1000), samples=samples, sample_rate=1000, to_ndarray=lambda count=samples: np.zeros((1, count), dtype=np.float32))
                                for pts, samples in chunks]
                class Container:
                    streams, format = NS(video=[video], audio=[audio]), NS(name='mp4')
                    def __enter__(self): return self
                    def __exit__(self, *args): return False
                    def demux(self, streams):
                        return [NS(decode=lambda: video_frames), NS(decode=lambda: audio_frames)]
                fake_av = NS(VideoFrame=av.VideoFrame, open=lambda path: Container())
                report = module.verify(args, fake_av, np, [], {})
                self.assertTrue(report['checks']['av_endpoints']['passed'])
                self.assertTrue(report['checks']['audio_pts']['passed'])
                check = report['checks']['audio_chunk_continuity']
                if statistic is None:
                    self.assertEqual(report['technical_status'], 'passed')
                    self.assertTrue(check['passed'])
                    self.assertAlmostEqual(check['max_gap_s'], .001)
                else:
                    self.assertEqual(report['technical_status'], 'failed')
                    self.assertFalse(check['passed'])
                    self.assertAlmostEqual(check[statistic], 1.7 if name == 'gap' else .05)
                    self.assertEqual(check['gap_count' if name == 'gap' else 'overlap_count'], 1)

    def test_color_sar_and_audio_metadata_expectations(self):
        result, report = self.run_verify('metadata-ok', '--sar', '1', '--sample-rate', '48000', '--channels', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report['metadata']['sample_aspect_ratio'], '1')
        self.assertEqual(report['metadata']['audio_channels'], 1)
        self.assertNotIn('audio_sample_rate', report['not_asserted'])
        result, report = self.run_verify('unknown-color', '--color-primaries', '999')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(report['checks']['color_primaries']['passed'])
        result, report = self.run_verify('wrong-sar', '--sar', '2')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(report['checks']['sample_aspect_ratio']['passed'])

    def test_real_float_pcm_nonfinite_overload_and_silence(self):
        import numpy as np
        for name, value in [('nonfinite', float('nan')), ('overload', 1.2), ('silence', 0)]:
            with self.subTest(name=name):
                raw, media = self.root / (name + '.f32'), self.root / (name + '.mkv')
                raw.write_bytes(np.full(96000, value, dtype='<f4').tobytes())
                subprocess.run([self.ffmpeg, '-v', 'error', '-i', str(self.output), '-f', 'f32le', '-ar', '48000',
                                '-ac', '1', '-i', str(raw), '-map', '0:v', '-map', '1:a', '-c', 'copy', str(media)],
                               check=True, capture_output=True)
                result, report = self.run_verify('audio-' + name, '--container-format', 'matroska',
                                                 '--audio-codec', 'pcm_f32le', output=media)
                self.assertEqual(result.returncode, 0 if name == 'silence' else 1, result.stderr)
                if name == 'nonfinite':
                    self.assertFalse(report['checks']['audio_finite']['passed'])
                    self.assertGreater(report['decoded_audio']['nonfinite_samples'], 0)
                elif name == 'overload':
                    self.assertFalse(report['checks']['audio_peak']['passed'])
                    self.assertGreater(report['decoded_audio']['overrange_samples'], 0)
                    result, report = self.run_verify('audio-relaxed', '--container-format', 'matroska',
                                                     '--audio-codec', 'pcm_f32le', '--audio-peak-limit', '1.3', output=media)
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.assertEqual(report['decoded_audio']['peak'], 0)
                    self.assertEqual(report['decoded_audio']['rms'], 0)

    def test_integer_pcm_is_normalized_before_peak_check(self):
        import numpy as np
        raw, media = self.root / 'integer.s16', self.root / 'integer.mkv'
        raw.write_bytes(np.resize(np.array([-32768, 32767], dtype='<i2'), 96000).tobytes())
        subprocess.run([self.ffmpeg, '-v', 'error', '-i', str(self.output), '-f', 's16le', '-ar', '48000',
                        '-ac', '1', '-i', str(raw), '-map', '0:v', '-map', '1:a', '-c', 'copy', str(media)],
                       check=True, capture_output=True)
        result, report = self.run_verify('integer-normalized', '--container-format', 'matroska',
                                         '--audio-codec', 'pcm_s16le', output=media)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report['decoded_audio']['peak'], 1)

    def test_faststart_is_optional_and_distinguishes_late_moov(self):
        result, report = self.run_verify('late-moov', '--require-faststart')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(report['checks']['faststart']['passed'])
        fast = self.root / 'faststart.mp4'
        subprocess.run([self.ffmpeg, '-v', 'error', '-i', str(self.output), '-c', 'copy',
                        '-movflags', '+faststart', str(fast)], check=True, capture_output=True)
        result, report = self.run_verify('faststart', '--require-faststart', output=fast)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report['checks']['faststart']['passed'])
        self.assertEqual(report['faststart']['status'], 'checked')

    def test_mp4_extended_sizes_and_bounds(self):
        import importlib.util
        import struct
        spec = importlib.util.spec_from_file_location('mp4_verify_export', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        path = self.root / 'atoms.mp4'
        path.write_bytes(struct.pack('>I4sQ', 1, b'moov', 16) + struct.pack('>I4s', 8, b'mdat'))
        self.assertEqual([box['offset'] for box in module.mp4_boxes(path)], [0, 16])
        path.write_bytes(struct.pack('>I4s', 40, b'mdat'))
        with self.assertRaises(ValueError):
            module.mp4_boxes(path)

    def test_requested_container_format_is_checked(self):
        result, report = self.run_verify('wrong-container', '--container-format', 'matroska')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report['checks']['container_format']['passed'])

    def test_wrong_frame_count_fails(self):
        result, report = self.run_verify('wrong-frames', expected=49)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report['checks']['frame_count']['passed'])

    def test_no_audio_needs_explicit_diagnostic_flag(self):
        silent = self.root / 'silent.mp4'
        subprocess.run([self.ffmpeg, '-v', 'error', '-i', str(self.output), '-an', '-c:v', 'copy', str(silent)],
                       check=True, capture_output=True)
        result, report = self.run_verify('missing-audio', output=silent)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report['checks']['audio_present']['passed'])
        result, report = self.run_verify('allowed-silent', '--allow-no-audio', output=silent)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report['audio_status'], 'absent_explicitly_allowed')
        result, report = self.run_verify('silent-with-channel-expectation', '--allow-no-audio', '--channels', '1', output=silent)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(report['checks']['audio_channels']['passed'])

    def test_report_cannot_overwrite_source(self):
        before = self.source.read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPT), '--source', str(self.source), '--output', str(self.output),
                                 '--expected-frames', '48', '--fps', '24', '--width', '320', '--height', '180',
                                 '--report', str(self.source)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.source.read_bytes(), before)

    def test_missing_dependency_is_actionable(self):
        result = subprocess.run([sys.executable, '-S', str(SCRIPT), '--source', str(self.source),
                                 '--output', str(self.output), '--expected-frames', '48', '--fps', '24',
                                 '--width', '320', '--height', '180', '--report', str(self.root / 'missing-dep.json')],
                                capture_output=True, text=True, env={**os.environ, 'PYTHONPATH': ''})
        self.assertEqual(result.returncode, 2)
        self.assertIn('python -m pip install av numpy Pillow', result.stderr)

    def test_corrupt_export_writes_failed_diagnostic_report(self):
        corrupt = self.root / 'corrupt.mp4'
        corrupt.write_bytes(b'not an mp4 file')
        result, report = self.run_verify('corrupt', output=corrupt)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report['technical_status'], 'failed')
        self.assertTrue(report['visual_review_required'])
        self.assertIn('error', report)

    def test_dark_scene_is_visual_heuristic_not_privacy_verdict(self):
        dark = self.root / 'dark.mp4'
        subprocess.run([self.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=black:size=320x180:rate=24',
                        '-i', str(self.source), '-map', '0:v', '-map', '1:a', '-t', '2', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', '-c:a', 'copy', str(dark)], check=True, capture_output=True)
        result, report = self.run_verify('dark', output=dark)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report['black_frame_border_heuristics']['all_black_count'], 48)
        self.assertTrue(report['visual_review_required'])
        self.assertIn('Dark scenes', report['visual_limitations'])

    def test_contact_paths_cannot_target_media(self):
        before = self.source.read_bytes()
        result = subprocess.run([sys.executable, str(SCRIPT), '--source', str(self.source), '--output', str(self.output),
                                 '--expected-frames', '48', '--fps', '24', '--width', '320', '--height', '180',
                                 '--report', str(self.root / 'unsafe-contact.json'), '--contacts-dir', str(self.source)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(before, self.source.read_bytes())

    def test_help_needs_no_optional_dependencies(self):
        result = subprocess.run([sys.executable, '-S', str(SCRIPT), '--help'], capture_output=True, text=True,
                                env={**os.environ, 'PYTHONPATH': ''})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--expected-frames', result.stdout)


if __name__ == '__main__':
    unittest.main()
