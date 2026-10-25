import csv
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/validate_imu.py'


class ValidateIMUTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='reframe-imu-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_csv(self, rows, *extra):
        path, report = self.root / 'imu.csv', self.root / 'report.json'
        with path.open('w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['t_video_s', 'gyro_x_rad_s', 'gyro_y_rad_s', 'gyro_z_rad_s'])
            writer.writerows(rows)
        result = subprocess.run([sys.executable, str(SCRIPT), str(path), '--gyro-units', 'rad/s',
                                 '--report', str(report), *extra], capture_output=True, text=True)
        self.assertTrue(report.exists(), result.stderr)
        return result, json.loads(report.read_text())

    def test_missing_quaternion_does_not_claim_orientation(self):
        result, report = self.run_csv([[0, 1, 2, 3], [.01, 1, 2, 3], [.02, 1, 2, 3]])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report['status'], 'imu_samples_valid_orientation_unverified')
        self.assertFalse(report['stabilization_proven'])

    def test_rejects_nan_nonmonotonic_and_gaps(self):
        for rows, check in [([[0, 1, 2, 3], [.01, 'NaN', 2, 3]], 'finite'),
                            ([[0, 1, 2, 3], [0, 1, 2, 3]], 'time_strictly_increasing'),
                            ([[0, 1, 2, 3], [.2, 1, 2, 3]], 'sampling_gaps')]:
            with self.subTest(check=check):
                result, report = self.run_csv(rows)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(report['checks'][check]['passed'])
                (self.root / 'report.json').unlink()

    def test_coverage_and_quaternion_sign_validation(self):
        quaternion = self.root / 'q.csv'
        quaternion.write_text('t_video_s,q_raw_w,q_raw_x,q_raw_y,q_raw_z\n0,1,0,0,0\n.01,-1,0,0,0\n')
        result, report = self.run_csv([[0, 1, 2, 3], [.01, 1, 2, 3]],
                                      '--quaternion-csv', str(quaternion), '--coverage-end', '1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report['checks']['coverage']['passed'])
        self.assertFalse(report['continuous_component_representation'])
        self.assertIn('quaternion_sign_normalization_required', report['warnings'])
        self.assertEqual(report['gyro_quaternion_correlation']['status'], 'not_requested_same_frame_unconfirmed')

    def test_explicit_frame_assertion_enables_only_descriptive_correlation(self):
        quaternion = self.root / 'q.csv'
        rows, qrows, angle = [], [], 0
        rates = [1 + index * .2 for index in range(12)]
        for index, rate in enumerate(rates):
            if index:
                angle += (rates[index-1] + rate) * .5 * .01
            rows.append([index * .01, 0, 0, rate * 180 / math.pi])
            sign = -1 if index >= 6 else 1
            qrows.append([index * .01, sign*math.cos(angle/2), 0, 0, sign*math.sin(angle/2)])
        with quaternion.open('w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['t_video_s', 'q_raw_w', 'q_raw_x', 'q_raw_y', 'q_raw_z'])
            writer.writerows(qrows)
        result, report = self.run_csv(rows, '--gyro-units', 'deg/s', '--quaternion-csv', str(quaternion),
                                      '--gyro-quat-same-frame')
        self.assertEqual(result.returncode, 0, result.stderr)
        relation = report['gyro_quaternion_correlation']
        self.assertEqual(relation['status'], 'computed_descriptive_only')
        self.assertAlmostEqual(relation['pearson_xyz'][2], 1, places=8)
        self.assertFalse(report['stabilization_proven'])

    def test_quaternion_sign_flip_warns_by_default_and_fails_only_when_required(self):
        quaternion = self.root / 'sign.csv'
        quaternion.write_text('t_video_s,q_raw_w,q_raw_x,q_raw_y,q_raw_z\n0,1,0,0,0\n.01,-1,0,0,0\n')
        rows = [[0, 0, 0, 0], [.01, 0, 0, 0]]
        before = quaternion.read_bytes()
        result, report = self.run_csv(rows, '--quaternion-csv', str(quaternion), '--gyro-quat-same-frame')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(report['status'], 'invalid')
        self.assertIn('quaternion_sign_normalization_required', report['warnings'])
        self.assertFalse(report['continuous_component_representation'])
        self.assertFalse(report['stabilization_proven'])
        self.assertEqual(report['gyro_quaternion_correlation']['status'], 'computed_descriptive_only')
        self.assertEqual(report['gyro_quaternion_correlation']['pearson_xyz'], [None, None, None])
        self.assertEqual(quaternion.read_bytes(), before)
        (self.root / 'report.json').unlink()
        result, report = self.run_csv(rows, '--quaternion-csv', str(quaternion),
                                      '--require-continuous-quaternion-sign')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(report['checks']['quaternion_sign_continuity']['passed'])
        self.assertEqual(quaternion.read_bytes(), before)

    def test_column_adapter_and_invalid_quaternion_norm(self):
        path = self.root / 'custom.csv'
        path.write_text('sec,gx,gy,gz,w,x,y,z\n0,1,2,3,2,0,0,0\n.01,1,2,3,1,0,0,0\n')
        report = self.root / 'custom.json'
        result = subprocess.run([sys.executable, str(SCRIPT), str(path), '--gyro-units', 'rad/s',
                                 '--time-column', 'sec', '--gyro-columns', 'gx,gy,gz',
                                 '--quat-columns', 'w,x,y,z', '--report', str(report)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(json.loads(report.read_text())['checks']['quaternion_unit_norm']['passed'])

    def test_malformed_csv_reports_line_and_column_without_touching_source(self):
        fixtures = [
            ('short', 't_video_s,gyro_x_rad_s,gyro_y_rad_s,gyro_z_rad_s\n0,1,2\n', 2, 'gyro_z_rad_s'),
            ('empty', 't_video_s,gyro_x_rad_s,gyro_y_rad_s,gyro_z_rad_s\n0,1,,3\n', 2, 'gyro_y_rad_s'),
            ('none', 't_video_s,gyro_x_rad_s,gyro_y_rad_s,gyro_z_rad_s\n0,1,None,3\n', 2, 'gyro_y_rad_s'),
            ('header', 't_video_s,gyro_x_rad_s,gyro_y_rad_s\n0,1,2\n', 1, 'gyro_z_rad_s'),
        ]
        for name, content, line, column in fixtures:
            with self.subTest(name=name):
                path, report_path = self.root / (name + '.csv'), self.root / (name + '.json')
                path.write_text(content)
                result = subprocess.run([sys.executable, str(SCRIPT), str(path), '--gyro-units', 'rad/s',
                                         '--report', str(report_path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertTrue(report_path.exists(), result.stderr)
                report = json.loads(report_path.read_text())
                self.assertEqual(report['status'], 'invalid')
                self.assertIn('line ' + str(line), report['errors'][0])
                self.assertIn(column, report['errors'][0])
                self.assertFalse(report['checks']['csv_input']['passed'])
                self.assertTrue(report['checks']['inputs_unchanged']['passed'])
                self.assertNotIn('Traceback', result.stderr)
                self.assertEqual(path.read_text(), content)

    def test_report_cannot_overwrite_csv(self):
        path = self.root / 'protected.csv'
        path.write_text('do not overwrite')
        result = subprocess.run([sys.executable, str(SCRIPT), str(path), '--gyro-units', 'rad/s',
                                 '--report', str(path)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(), 'do not overwrite')


if __name__ == '__main__':
    unittest.main()
