"""Synthetic geometry tests: no user imagery or telemetry is used."""
import importlib
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def axis_quats(axis, angles):
    angles = np.asarray(angles)
    return np.column_stack((np.cos(angles / 2),
                            np.sin(angles / 2)[:, None] * np.asarray(axis)))


class CameraPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module_present = importlib.util.find_spec('camera_path') is not None
        cls.cp = importlib.import_module('camera_path') if cls.module_present else None

    def setUp(self):
        self.assertTrue(self.module_present, 'camera_path math core must exist')

    def build(self, t, q, samples=None, **kwargs):
        return self.cp.build_path(t, q, t if samples is None else samples,
                                  direction='body-to-world', camera_from_imu=np.eye(3), **kwargs)

    def test_sign_changes_and_pi_crossing_follow_same_short_arc(self):
        t = np.array([0, .02, .04])
        q = axis_quats([0, 1, 0], np.deg2rad([179, 180, 181]))
        a, _ = self.build(t, q, [.01, .03], tau_s=0)
        q[1] *= -1
        b, diag = self.build(t, q, [.01, .03], tau_s=0)
        np.testing.assert_allclose(a['target_to_world'], b['target_to_world'], atol=1e-12)
        expected = self.cp.quat_to_matrix(axis_quats([0, 1, 0], np.deg2rad([179.5, 180.5])))
        np.testing.assert_allclose(a['target_to_world'], expected, atol=1e-12)
        self.assertEqual(diag['input_sign_flips_normalized'], 1)

    def test_tau_zero_and_static_pose(self):
        t = np.linspace(0, 1, 51)
        q = axis_quats([0, 0, 1], np.full(len(t), .7))
        arrays, _ = self.build(t, q, tau_s=.35)
        np.testing.assert_allclose(arrays['output_ray_to_source_camera'],
                                   np.broadcast_to(np.eye(3), (len(t), 3, 3)), atol=1e-12)
        moving = axis_quats([0, 1, 0], t)
        arrays, _ = self.build(t, moving, tau_s=0)
        np.testing.assert_allclose(arrays['target_to_world'], arrays['camera_to_world'], atol=1e-12)

    def test_irregular_intervals_use_the_next_interval_backward(self):
        t = np.array([0, .01, .045, .05])
        angle = np.array([0., .1, .3, .2])
        tau = .2
        alpha = -np.expm1(-np.diff(t) / tau)
        expected = angle.copy()
        for i in range(1, len(t)):
            expected[i] = expected[i-1] + alpha[i-1] * (angle[i] - expected[i-1])
        for i in range(len(t)-2, -1, -1):
            expected[i] = expected[i+1] + alpha[i] * (expected[i] - expected[i+1])
        actual = self.cp.smooth_quaternions(t, axis_quats([0, 1, 0], angle), tau)
        np.testing.assert_allclose(self.cp.quat_to_matrix(actual),
                                   self.cp.quat_to_matrix(axis_quats([0, 1, 0], expected)), atol=1e-11)

    def test_slow_yaw_and_fast_roll_are_smoothed(self):
        t = np.linspace(0, 4, 401)
        yaw, roll = .1 * t, .15 * np.sin(2 * np.pi * 9 * t)
        # Hamilton product: yaw around world y then local forward roll.
        q = np.column_stack((np.cos(yaw/2)*np.cos(roll/2),
                             np.sin(yaw/2)*np.sin(roll/2),
                             np.sin(yaw/2)*np.cos(roll/2),
                             np.cos(yaw/2)*np.sin(roll/2)))
        arrays, diag = self.build(t, q, tau_s=.2)
        self.assertLess(diag['angular_velocity_deg_s']['target']['rms'],
                        .2 * diag['angular_velocity_deg_s']['camera']['rms'])
        target_yaw = np.arctan2(arrays['target_to_world'][:, 0, 2],
                                arrays['target_to_world'][:, 2, 2])
        self.assertGreater(target_yaw[-1] - target_yaw[0], .3)

    def test_horizon_and_forward_cap_remove_raw_roll(self):
        t = np.linspace(0, 1, 101)
        yaw, roll = 1.3 * t, np.full(len(t), .7)
        q = np.column_stack((np.cos(yaw/2)*np.cos(roll/2),
                             np.sin(yaw/2)*np.sin(roll/2),
                             np.sin(yaw/2)*np.cos(roll/2),
                             np.cos(yaw/2)*np.sin(roll/2)))
        arrays, diag = self.build(t, q, tau_s=.4, world_up=[0, -1, 0], max_forward_angle_deg=2)
        target = arrays['target_to_world']
        np.testing.assert_allclose(np.linalg.det(target), 1., atol=1e-12)
        np.testing.assert_allclose(np.swapaxes(target, 1, 2) @ target,
                                   np.broadcast_to(np.eye(3), target.shape), atol=1e-12)
        np.testing.assert_allclose(target[:, :, 1], np.broadcast_to([0, 1, 0], (len(t), 3)), atol=1e-12)
        self.assertGreater(diag['forward_clamp_count'], 0)
        self.assertLessEqual(diag['forward_angle_deg']['after']['max'], 2 + 1e-8)
        self.assertFalse(diag['projection_coverage_verified'])
        np.testing.assert_allclose(arrays['output_ray_to_source_camera'],
                                   np.swapaxes(arrays['camera_to_world'], 1, 2) @ target)

    def test_explicit_frame_and_direction(self):
        t = np.array([0, .02])
        q = axis_quats([0, 1, 0], [0.4, .5])
        e = self.cp.quat_to_matrix(axis_quats([0, 0, 1], [.2]))[0]
        a, _ = self.cp.build_path(t, q, t, direction='body-to-world', camera_from_imu=e, tau_s=0)
        reverse = q * [1, -1, -1, -1]
        b, _ = self.cp.build_path(t, reverse, t, direction='world-to-body', camera_from_imu=e, tau_s=0)
        np.testing.assert_allclose(a['camera_to_world'], b['camera_to_world'], atol=1e-12)
        np.testing.assert_allclose(a['camera_to_world'], self.cp.quat_to_matrix(q) @ e.T, atol=1e-12)

    def test_fail_closed_invalid_inputs(self):
        t = np.array([0, .02, .04])
        q = axis_quats([0, 1, 0], [0, .01, .02])
        cases = [
            ([0, .1, .2], q, None, {}, 'gap'),
            (t, q, [-.01], {}, 'outside'),
            (t, q, [.05], {}, 'outside'),
            (t, q*2, None, {}, 'unit'),
            ([0, 0, .02], q, None, {}, 'increasing'),
            (t, q, None, {'tau_s': float('nan')}, 'finite'),
            (t, q, None, {'max_forward_angle_deg': 2}, 'world_up'),
            (t, q, None, {'world_up': [0, 0, 0]}, 'world_up'),
        ]
        for times, quats, samples, params, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.build(times, quats, samples, **params)
        with self.assertRaisesRegex(ValueError, 'SO\\(3\\)'):
            self.cp.build_path(t, q, t, direction='body-to-world', camera_from_imu=np.diag([1,1,-1]))

    def test_poles_and_antipodes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'pole'):
            self.cp.horizon_basis(np.array([[0, 1, 0]]), [0, -1, 0])
        with self.assertRaisesRegex(ValueError, 'antipodal'):
            self.cp.cap_forward(np.array([[0., 0, 1]]), np.array([[0., 0, -1]]), .2)
        with self.assertRaisesRegex(ValueError, '180'):
            self.build([0, .02], [[1.,0,0,0], [0,0,1.,0]])

    def test_arbitrary_gravity_and_one_sample_output(self):
        t = [0, .02, .04]
        q = axis_quats([0, 0, 1], [.5, .5, .5])
        arrays, diag = self.build(t, q, [.01], world_up=[.2, -1, .3])
        up = np.array([.2, -1, .3]) / np.linalg.norm([.2, -1, .3])
        down_local = arrays['target_to_world'][0].T @ -up
        self.assertAlmostEqual(down_local[0], 0, places=12)
        self.assertGreater(down_local[1], 0)
        self.assertEqual(diag['angular_velocity_deg_s']['target']['count'], 0)
        np.testing.assert_allclose(arrays['target_to_world'][0, :, 2], [0, 0, 1])

    def test_unrepresentable_motion_statistics_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'finite|representable'):
            self.build([0, 1e-309], axis_quats([0, 1, 0], [0., .1]), tau_s=0)

    def test_endpoint_rounding_is_bounded_to_four_ulps(self):
        t = np.linspace(0, .3, 31)
        q = axis_quats([0, 1, 0], t)
        arrays, diag = self.build(t, q, [.1, .2, .1+.2], tau_s=0)
        self.assertEqual(arrays['imu_sample_timestamps_s'][-1], t[-1])
        self.assertEqual(diag['endpoint_rounding']['adjusted_samples'], 1)
        outside = t[-1]
        for _ in range(5):
            outside = np.nextafter(outside, np.inf)
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.build(t, q, [.1, .2, outside], tau_s=0)


if __name__ == '__main__':
    unittest.main()
