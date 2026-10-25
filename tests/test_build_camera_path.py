import csv
from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/build_camera_path.py'


class BuildCameraPathTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'camera path CLI must exist')
        temp = tempfile.TemporaryDirectory(prefix='camera-path-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'input.csv'
        self.output = self.root / 'path.npz'
        self.report = self.root / 'report.json'
        self.write_input()

    def write_input(self, hz=100, duration=2):
        with self.source.open('w', newline='') as h:
            writer = csv.writer(h)
            writer.writerow(['t_video_s', 'qw', 'qx', 'qy', 'qz'])
            writer.writerows([[i/hz, 1., 0., 0., 0.] for i in range(int(duration*hz)+1)])

    def command(self, *extra):
        return [sys.executable, str(SCRIPT), '--input', str(self.source), '--output', str(self.output),
                '--report', str(self.report), '--frames', '10', '--fps', '25', '--time-offset-s', '.1',
                '--direction', 'body-to-world', '--identity-camera-frame', *extra]

    def run_cli(self, *extra):
        return subprocess.run(self.command(*extra), capture_output=True, text=True)

    def test_help_without_site_packages(self):
        result = subprocess.run([sys.executable, '-S', str(SCRIPT), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--time-offset-s', result.stdout)

    def test_clocks_keys_and_hashes(self):
        before = self.source.read_bytes()
        result = self.run_cli('--source-start-s', '.5', '--fps', '30000/1001')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.report.read_text())
        with np.load(self.output, allow_pickle=False) as data:
            output_t = np.arange(10) * 1001 / 30000
            np.testing.assert_allclose(data['output_timestamps_s'], output_t)
            np.testing.assert_allclose(data['source_timestamps_s'], .5 + output_t)
            np.testing.assert_allclose(data['imu_sample_timestamps_s'], .6 + output_t)
            np.testing.assert_array_equal(data['timestamps_s'], data['source_timestamps_s'])
            self.assertEqual(data['target_to_world'].shape, (10, 3, 3))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(report['input']['sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(report['output']['sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(report['status'], 'camera_path_built_unverified_visual_effect')
        self.assertFalse(report['projection_coverage_verified'])
        self.assertFalse(report['stabilization_proven'])

    def test_sampling_rates_and_frame_counts_are_not_material_defaults(self):
        for hz, frames, fps in [(50, 5, '7'), (125, 39, '24000/1001')]:
            with self.subTest(hz=hz):
                self.write_input(hz=hz)
                result = self.run_cli('--frames', str(frames), '--fps', fps)
                self.assertEqual(result.returncode, 0, result.stderr)
                with np.load(self.output) as data:
                    self.assertEqual(len(data['source_timestamps_s']), frames)
                self.output.unlink()
                self.report.unlink()

    def test_bad_inputs_leave_structured_report_without_npz(self):
        for params, message in [(['--frames', '1000001'], 'frames'),
                                (['--frames', '1000'], 'outside'),
                                (['--max-input-gap-s', '.001'], 'gap'),
                                (['--tau-s', 'nan'], 'finite'),
                                (['--horizon-lock'], 'world-up')]:
            with self.subTest(params=params):
                result = self.run_cli(*params)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())
                report = json.loads(self.report.read_text())
                self.assertEqual(report['status'], 'failed')
                self.assertIn(message, report['errors'][0])
                self.assertFalse(report['stabilization_proven'])
                self.report.unlink()

    def test_overwrite_and_source_aliases_rejected(self):
        self.output.write_bytes(b'keep output')
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_bytes(), b'keep output')
        self.output.unlink()
        if self.report.exists():
            self.report.unlink()
        self.report.write_bytes(b'keep report')
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.report.read_bytes(), b'keep report')
        self.assertFalse(self.output.exists())
        self.report.unlink()
        before = self.source.read_bytes()
        for flag in ('--output', '--report'):
            result = self.run_cli(flag, str(self.source))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.source.read_bytes(), before)
            if self.report.exists():
                self.report.unlink()
        self.output.symlink_to(self.source)
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.source.read_bytes(), before)

    def test_direction_frame_and_offset_are_mandatory(self):
        command = self.command()
        for option, count in [('--direction', 2), ('--time-offset-s', 2), ('--identity-camera-frame', 1)]:
            with self.subTest(option=option):
                candidate = command.copy()
                index = candidate.index(option)
                del candidate[index:index+count]
                result = subprocess.run(candidate, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(option if count == 2 else '--camera-from-imu', result.stderr)

    def test_invalid_extrinsic_and_csv(self):
        command = self.command()
        command.remove('--identity-camera-frame')
        result = subprocess.run(command + ['--camera-from-imu', '[[1,0,0],[0,1,0],[0,0,-1]]'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SO(3)', json.loads(self.report.read_text())['errors'][0])
        self.report.unlink()
        self.source.write_text('t_video_s,qw,qx,qy,qz\n0,1,0,,0\n')
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('line 2', json.loads(self.report.read_text())['errors'][0])

    def test_report_publication_race_rolls_back_only_our_output(self):
        spec = importlib.util.spec_from_file_location('camera_path_cli', SCRIPT)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        original = cli.publish
        def racing_publish(stage, destination):
            if destination == self.report:
                destination.write_bytes(b'competing report')
            original(stage, destination)
        with patch.object(cli, 'publish', side_effect=racing_publish) as publish_mock, redirect_stderr(io.StringIO()), patch.object(sys, 'path', [str(SCRIPT.parent), *sys.path]):
            code = cli.main(self.command()[2:])
        self.assertEqual(code, 1)
        self.assertEqual([call.args[1] for call in publish_mock.call_args_list], [self.output, self.report])
        self.assertFalse(self.output.exists())
        self.assertEqual(self.report.read_bytes(), b'competing report')
        self.assertEqual(list(self.root.glob('*.stage')), [])

    def test_hardlink_and_dangling_symlink_destinations_are_never_overwritten(self):
        import os
        before = self.source.read_bytes()
        os.link(self.source, self.output)
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.source.read_bytes(), before)
        self.output.unlink()
        self.report.unlink()
        self.output.symlink_to(self.root / 'absent.npz')
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertTrue(self.output.is_symlink())
        self.assertFalse((self.root / 'absent.npz').exists())

    def test_oversize_csv_field_produces_failure_report(self):
        self.source.write_text('t_video_s,qw,qx,qy,qz\n0,' + '1' * 140000 + ',0,0,0\n')
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(json.loads(self.report.read_text())['status'], 'failed')
        self.assertFalse(self.output.exists())

    def test_decimal_boundary_rounding_does_not_request_extrapolation(self):
        self.write_input(duration=.3)
        result = self.run_cli('--frames', '3', '--fps', '10', '--source-start-s', '.1', '--time-offset-s', '0')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['diagnostics']['endpoint_rounding']['adjusted_samples'], 1)
        with np.load(self.output) as data:
            self.assertEqual(data['imu_sample_timestamps_s'][-1], .3)


if __name__ == '__main__':
    unittest.main()
