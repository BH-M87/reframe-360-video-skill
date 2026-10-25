"""Small synthetic contracts; no camera-specific data or image files."""
import importlib
import json
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from camera_path import quat_to_matrix


def quats(axis, angle):
    angle = np.asarray(angle)
    return np.column_stack((np.cos(angle/2), np.sin(angle/2)[:, None]*axis))


def calibration():
    return dict(width=640, height=480, fx=180., fy=180., cx=319.5, cy=239.5,
                k1=.01, k2=-.001, k3=0., k4=0., k5=0., p1=.0001, p2=-.0002)


class RollingShutterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.present = importlib.util.find_spec('rolling_shutter') is not None
        cls.rs = importlib.import_module('rolling_shutter') if cls.present else None

    def setUp(self):
        self.assertTrue(self.present, 'portable rolling shutter core must exist')
        self.t = np.linspace(0., 1., 101)
        self.q = quats(np.array([1., 0, 0]), self.t*.25)
        self.k = np.array([[24., 0, 15.5], [0, 24., 11.5], [0, 0, 1.]])

    def mapper(self, **kwargs):
        args = dict(imu_times_s=self.t, quaternions_wxyz=self.q,
                    source_times_s=[.4, .5, .6], target_to_world=np.tile(np.eye(3), (3, 1, 1)),
                    direction='body-to-world', camera_from_imu=np.eye(3),
                    calibration=calibration(), output_intrinsics=self.k,
                    width=32, height=24, offset_s=.003, row_reference=.5)
        args.update(kwargs)
        return self.rs.RSMapper(**args)

    def test_readout_defaults_off_and_static_pose_rs_matches_off(self):
        q = quats(np.array([0., 1, 0]), np.full(len(self.t), .13))
        off = self.mapper(quaternions_wxyz=q)
        on = self.mapper(quaternions_wxyz=q, readout_s=.07)
        x0, y0, d0 = off.maps(1, 640, 480)
        x1, y1, d1 = on.maps(1, 640, 480)
        np.testing.assert_allclose(x0, x1, atol=2e-5)
        np.testing.assert_allclose(y0, y1, atol=2e-5)
        self.assertEqual(d0['readout_s'], 0.)
        self.assertEqual(d1['invalid_pixel_count'], 0)
        self.assertFalse(d1['visual_effect_verified'])
        self.assertFalse(d1['privacy_verified'])
        json.dumps(d1, allow_nan=False)

    def test_zero_readout_matches_independent_matrix_mapping_and_resize_centers(self):
        e = quat_to_matrix(quats(np.array([0., 0, 1]), [.2]))[0]
        target = quat_to_matrix(quats(np.array([0., 1, 0]), [.3]))[0]
        m = self.mapper(camera_from_imu=e, target_to_world=np.tile(target, (3, 1, 1)))
        rays = m.rays(np.array([0., 7., 31.]), np.array([0., 18., 23.]))
        imu = quat_to_matrix(quats(np.array([1., 0, 0]), [.503*.25]))[0]
        expected = self.rs.project_model8(rays @ (e @ imu.T @ target).T, calibration())
        actual, report = m.exact(1, rays)
        np.testing.assert_allclose(actual, expected, atol=1e-10)
        self.assertEqual(report['iterations'], 0)
        x, y, _ = m.maps(1, 320, 240)
        # Native coordinates are centers: resized u=(u+.5)*scale-.5.
        for px, py in [(0, 0), (7, 18), (31, 23)]:
            uv, _ = m.exact(1, m.rays(np.array([px]), np.array([py])))
            np.testing.assert_allclose([x[py, px], y[py, px]], (uv[0]+.5)*.5-.5, atol=2e-5)

    def test_direction_and_sign_equivalence_without_mutating_inputs(self):
        q = self.q.copy(); q[::2] *= -1
        saved = q.copy()
        first = self.mapper(quaternions_wxyz=q, readout_s=.04)
        second = self.mapper(quaternions_wxyz=q*[1, -1, -1, -1], direction='world-to-body', readout_s=.04)
        a = first.maps(0, 640, 480); b = second.maps(0, 640, 480)
        np.testing.assert_array_equal(q, saved)
        np.testing.assert_allclose(a[0], b[0], atol=1e-5)
        np.testing.assert_allclose(a[1], b[1], atol=1e-5)

    def test_model8_polynomial_tangential_and_front_axis_against_formula(self):
        c=calibration();c.update(k1=.04,k2=-.003,k3=.0002,k4=-.00001,k5=.000001,p1=.002,p2=-.003)
        theta=.61;azimuth=.37
        ray=[np.sin(theta)*np.cos(azimuth),np.sin(theta)*np.sin(azimuth),np.cos(theta)]
        td=theta*(1+sum(c[f'k{i}']*theta**(2*i) for i in range(1,6)))
        xp,yp=td*np.cos(azimuth),td*np.sin(azimuth)
        xd=xp+2*c['p1']*xp*yp+c['p2']*(td*td+2*xp*xp)
        yd=yp+c['p1']*(td*td+2*yp*yp)+2*c['p2']*xp*yp
        actual=self.rs.project_model8([ray,[0.,0,2.]],c)
        np.testing.assert_allclose(actual[0],[c['fx']*xd+c['cx'],c['fy']*yd+c['cy']],atol=1e-11)
        np.testing.assert_allclose(actual[1],[c['cx'],c['cy']],atol=1e-11)

    def test_row_fixed_point_against_analytic_rotation_and_bisection(self):
        omega = .7
        c = calibration(); c.update({f'k{i}': 0. for i in range(1, 6)}); c.update(p1=0., p2=0.)
        ray = np.array([.1, .2, 1.]); readout = .08; reference = .25
        m = self.mapper(quaternions_wxyz=quats(np.array([1., 0, 0]), self.t*omega),
                        calibration=c, readout_s=readout, row_reference=reference,
                        convergence_px=1e-8, max_iterations=30)
        def project_at_row(v):
            angle=omega*(.503+(v/c['height']-reference)*readout)
            x,y,z=ray; y,z=y*np.cos(angle)+z*np.sin(angle), -y*np.sin(angle)+z*np.cos(angle)
            theta=np.arctan2(np.hypot(x,y),z)
            return np.array([c['fx']*theta*x/np.hypot(x,y)+c['cx'],
                             c['fy']*theta*y/np.hypot(x,y)+c['cy']])
        lo, hi = 0., c['height']-1.
        for _ in range(70):
            mid=(lo+hi)/2
            if project_at_row(mid)[1]>mid:lo=mid
            else:hi=mid
        expected=project_at_row((lo+hi)/2)
        actual, diag=m.exact(1, ray[None])
        np.testing.assert_allclose(actual[0], expected, atol=1e-7)
        self.assertGreater(diag['iterations'], 1)

    def test_readout_sign_changes_source_rows_but_not_target(self):
        a=self.mapper(readout_s=.08); b=self.mapper(readout_s=-.08)
        rays=a.rays(np.array([8., 24.]),np.array([2., 20.]))
        uv1,_=a.exact(1,rays); uv2,_=b.exact(1,rays)
        self.assertGreater(np.max(np.abs(uv1-uv2)), .01)

    def test_single_ray_matches_batched_ray(self):
        m=self.mapper()
        one,_=m.exact(1,m.rays(7.,9.))
        many,_=m.exact(1,m.rays([7.],[9.]))
        self.assertEqual(one.shape,(2,))
        np.testing.assert_allclose(one,many[0])
        np.testing.assert_allclose(self.rs.project_model8([0.,0,1.],calibration()),[319.5,239.5])

    def test_grid_budget_adapts_and_reports_sample_scope(self):
        k=np.array([[9.,0,15.5],[0,9.,11.5],[0,0,1.]])
        coarse=self.mapper(output_intrinsics=k, grid_step=16, max_grid_error_px=.0001)
        exact=self.mapper(output_intrinsics=k)
        x,y,d=coarse.maps(1,640,480); ex,ey,_=exact.maps(1,640,480)
        self.assertLess(d['grid_step_used'],16)
        self.assertLessEqual(d['sampled_grid_error_native_px_max'], .0001)
        self.assertIn('sampled', d['grid_error_scope'])
        np.testing.assert_allclose(x,ex,atol=.0002)
        np.testing.assert_allclose(y,ey,atol=.0002)

    def test_coarse_support_coverage_falls_back_without_extrapolation(self):
        t=np.linspace(.496,.504,17)
        q=quats(np.array([1.,0,0]),np.zeros(len(t)))
        c=calibration();c.update({f'k{i}':0. for i in range(1,6)});c.update(p1=0.,p2=0.)
        args=dict(imu_times_s=t,quaternions_wxyz=q,source_times_s=[.5],
                  target_to_world=np.eye(3)[None],offset_s=0.,readout_s=.02,
                  max_input_gap_s=.001,calibration=c)
        dense=self.mapper(**args)
        dx,dy,dd=dense.maps(0,640,480)
        grid=self.mapper(**args,grid_step=16)
        x,y,diag=grid.maps(0,640,480)
        self.assertTrue(dd['rectangular_coverage_passed'] and diag['rectangular_coverage_passed'])
        self.assertLess(diag['grid_step_used'],16)
        self.assertTrue(any(r['reason']=='coarse_support_time_coverage' for r in diag['grid_retries']))
        self.assertLessEqual(np.max(np.hypot(x-dx,y-dy)),grid.grid_budget)
        self.assertEqual(diag['max_endpoint_rounding_s'],0.)
        self.assertFalse(diag['extrapolation_allowed'])

    def test_grid_fallback_does_not_hide_real_pixel_coverage_or_solver_errors(self):
        t=np.linspace(.498,.502,17)
        q=quats(np.array([1.,0,0]),np.zeros(len(t)))
        for step in [1,16]:
            m=self.mapper(imu_times_s=t,quaternions_wxyz=q,source_times_s=[.5],
                          target_to_world=np.eye(3)[None],offset_s=0.,readout_s=.02,
                          max_input_gap_s=.001,grid_step=step)
            with self.assertRaisesRegex(ValueError,'coverage|extrapolat'):
                m.maps(0,640,480)
        m=self.mapper(quaternions_wxyz=quats(np.array([1.,0,0]),self.t*2.),readout_s=.15,
                      grid_step=16,max_iterations=1,convergence_px=1e-12)
        with self.assertRaisesRegex(ValueError,'converg'):
            m.maps(1,640,480)

    def test_gap_and_ambiguous_orientation_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'gap'):
            self.mapper(imu_times_s=[0.,.02,.2],quaternions_wxyz=quats(np.array([1.,0,0]),[0.,.01,.02]))
        with self.assertRaisesRegex(ValueError, '180'):
            self.mapper(imu_times_s=[0.,.02],quaternions_wxyz=[[1,0,0,0],[0,1,0,0]])

    def test_center_and_sensor_row_extrapolation_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'coverage|extrapolat'):
            self.mapper(source_times_s=[0.],target_to_world=np.eye(3)[None],offset_s=-.001)
        m=self.mapper(source_times_s=[0.],target_to_world=np.eye(3)[None],offset_s=0.,readout_s=.1)
        with self.assertRaisesRegex(ValueError, 'coverage|extrapolat'):
            m.maps(0,640,480)

    def test_only_four_ulp_endpoint_roundoff_is_accepted_and_reported(self):
        q=quats(np.array([1.,0,0]),self.t*.0)
        t=np.nextafter(1.,np.inf)
        m=self.mapper(quaternions_wxyz=q,source_times_s=[t],target_to_world=np.eye(3)[None],offset_s=0.)
        _,_,d=m.maps(0,640,480)
        self.assertGreater(d['max_endpoint_rounding_s'],0)
        for _ in range(4):t=np.nextafter(t,np.inf)
        with self.assertRaisesRegex(ValueError,'coverage|extrapolat'):
            self.mapper(source_times_s=[t],target_to_world=np.eye(3)[None],offset_s=0.)

    def test_explicit_frame_index_refuses_negative_bool_and_noninteger(self):
        m=self.mapper()
        for index in [-1,3,True,.5]:
            with self.subTest(index=index):
                with self.assertRaisesRegex(ValueError,'frame index'):m.maps(index,640,480)
                with self.assertRaisesRegex(ValueError,'frame index'):m.exact(index,np.array([[0.,0,1.]]))

    def test_nonconvergence_fails_closed(self):
        m=self.mapper(quaternions_wxyz=quats(np.array([1.,0,0]),self.t*2.),readout_s=.15,
                      max_iterations=1,convergence_px=1e-12)
        with self.assertRaisesRegex(ValueError,'converg'):
            m.maps(1,640,480)

    def test_backward_optical_axis_and_zero_ray_cannot_map_to_center(self):
        c=calibration()
        self.assertTrue(np.isnan(self.rs.project_model8([[0.,0,-1.]],c)).all())
        with self.assertRaisesRegex(ValueError,'nonzero|ray'):
            self.rs.project_model8([[0.,0,0.]],c)
        m=self.mapper(quaternions_wxyz=quats(np.array([1.,0,0]),self.t*0.))
        with self.assertRaisesRegex(ValueError,'projection|ray'):
            m.exact(1,np.array([[0.,0,-1.]]))

    def test_outside_rectangle_is_invalid_and_not_privacy_approval(self):
        c=calibration();c['cx']=2000.
        m=self.mapper(calibration=c)
        x,y,d=m.maps(1,640,480)
        self.assertTrue((x==-1).all() and (y==-1).all())
        self.assertEqual(d['invalid_pixel_count'],32*24)
        self.assertFalse(d['rectangular_coverage_passed'])
        self.assertFalse(d['privacy_verified'])

    def test_invalid_contracts_are_rejected(self):
        bad=[dict(offset_s=float('nan')),dict(readout_s=float('inf')),
             dict(direction='auto'),dict(row_reference=2),dict(max_input_gap_s=0),
             dict(width=0),dict(grid_step=3),dict(max_grid_error_px=0),
             dict(convergence_px=0),dict(max_iterations=True),
             dict(camera_from_imu=np.diag([1,1,-1])),
             dict(target_to_world=np.tile(np.diag([1,1,-1]),(3,1,1))),
             dict(output_intrinsics=np.zeros((3,3))),dict(quaternions_wxyz=self.q*2),
             dict(source_times_s=[.5,.4,.6])]
        for params in bad:
            with self.subTest(params=params):
                with self.assertRaises((ValueError,TypeError)):self.mapper(**params)
        for key,value in [('fx',-1.),('p1',float('nan')),('lens_model',4)]:
            c=calibration();c[key]=value
            with self.assertRaises(ValueError):self.mapper(calibration=c)
        c=calibration();del c['k5']
        with self.assertRaises(ValueError):self.mapper(calibration=c)


if __name__=='__main__':unittest.main()
