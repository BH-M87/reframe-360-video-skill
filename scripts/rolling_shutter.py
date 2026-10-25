"""Optional model8 rolling-shutter inverse maps; no media I/O or auto calibration.

Hamilton wxyz; column rays; camera x-right/y-down/z-forward. E maps IMU to
camera and target_to_world is fixed for every row of an output frame. Explicit
offset_s, signed readout_s and row_reference describe verified source timing:
    sensor_time = source_time + offset_s + (native_y / native_height - row_reference) * readout_s
Positive readout means increasing native y is captured later. This module does
not infer that direction, synchronize video, integrate gyro, stitch, or render.
Only map coordinates are interpolated; the caller resamples the source once.
"""
import numbers
import numpy as np

from camera_path import normalized_quaternions, slerp, validate_rotation


class _CoverageError(ValueError):
    """A sensor-time request exceeds the supplied continuous log."""


def _array(value, name):
    result = np.asarray(value, dtype=np.float64)
    if not np.isfinite(result).all():
        raise ValueError(f'{name} must be finite')
    return result


def _scalar(value, name, *, positive=False):
    if isinstance(value, (bool, np.bool_)) or not np.isscalar(value):
        raise ValueError(f'{name} must be a finite scalar')
    result = float(value)
    if not np.isfinite(result) or (positive and result <= 0):
        raise ValueError(f'{name} must be finite' + (' and positive' if positive else ''))
    return result


def _integer(value, name, minimum=1, maximum=None):
    if (isinstance(value, (bool, np.bool_)) or not isinstance(value, numbers.Integral)
            or value < minimum or (maximum is not None and value > maximum)):
        raise ValueError(f'{name} must be an integer in the supported range')
    return int(value)


def _times(value, name, minimum):
    value = _array(value, name)
    if value.ndim != 1 or len(value) < minimum or np.any(np.diff(value) <= 0):
        raise ValueError(f'{name} must contain at least {minimum} strictly increasing timestamps')
    return value.copy()


def _calibration(value):
    if not isinstance(value, dict):
        raise ValueError('calibration must be a model8 dictionary')
    if 'lens_model' in value and value['lens_model'] != 8:
        raise ValueError('only lens_model 8 is implemented')
    try:
        result = {key: _scalar(value[key], f'calibration.{key}') for key in
                  ['fx', 'fy', 'cx', 'cy', 'k1', 'k2', 'k3', 'k4', 'k5']}
        for key in ('width', 'height'):
            # JSON metadata sometimes expresses integer dimensions as floats.
            size = _scalar(value[key], f'calibration.{key}', positive=True)
            if not size.is_integer():
                raise ValueError('native dimensions must be integers')
            result[key] = int(size)
        if 'p1' in value or 'p2' in value:
            result['p1'] = _scalar(value['p1'], 'calibration.p1')
            result['p2'] = _scalar(value['p2'], 'calibration.p2')
        else:
            p = _array(value['p'], 'calibration.p')
            if p.shape != (2,):
                raise ValueError('calibration.p must have two values')
            result['p1'], result['p2'] = map(float, p)
    except KeyError as error:
        raise ValueError(f'missing model8 calibration field: {error.args[0]}') from None
    if result['fx'] <= 0 or result['fy'] <= 0:
        raise ValueError('calibration focal lengths must be positive')
    return result


def _rays(value):
    rays = _array(value, 'rays')
    if rays.ndim < 1 or rays.shape[-1] != 3 or rays.size == 0:
        raise ValueError('rays must have nonempty shape (..., 3)')
    norms = np.linalg.norm(rays, axis=-1, keepdims=True)
    if not np.isfinite(norms).all() or np.any(norms <= 1e-12):
        raise ValueError('rays must have finite nonzero norm')
    return rays / norms


def _project(rays, cal):
    x, y, z = np.moveaxis(rays, -1, 0)
    rho = np.hypot(x, y)
    theta = np.arctan2(rho, z)
    square = theta * theta
    poly = cal['k5']
    for i in (4, 3, 2, 1):
        poly = poly * square + cal[f'k{i}']
    td = theta * (1 + poly * square)
    factor = np.divide(td, rho, out=np.zeros_like(rho), where=rho > 1e-12)
    xp, yp, r2 = x * factor, y * factor, td * td
    xd = xp + 2*cal['p1']*xp*yp + cal['p2']*(r2+2*xp*xp)
    yd = yp + cal['p1']*(r2+2*yp*yp) + 2*cal['p2']*xp*yp
    uv = np.stack((cal['fx']*xd+cal['cx'], cal['fy']*yd+cal['cy']), axis=-1)
    # The backward optical-axis pole has no unique fisheye direction.
    return np.where(((rho <= 1e-12) & (z <= 0))[..., None], np.nan, uv)


def project_model8(rays, calibration):
    """Return native pixel-center coordinates; backward-axis rays return NaN.

    This is the explicitly supplied theta polynomial plus tangential model,
    not proof of a camera's valid circular lens image or physical calibration.
    """
    return _project(_rays(rays), _calibration(calibration))


class RSMapper:
    """Build one frame's inverse map with immutable, explicitly timed arrays.

    All timestamps are seconds in the declared IMU/source clocks. A full
    continuous IMU log is required; gaps anywhere exceed max_input_gap_s fail.
    Targets and source timestamps must have the same length. No FPS is inferred.
    grid_step=1 is exact dense evaluation; opt-in powers of two up to 32 use
    sampled error-controlled coordinate interpolation. OpenCV is only needed
    for that optional grid path. No image is read, warped, or written here.
    """
    def __init__(self, imu_times_s, quaternions_wxyz, source_times_s, target_to_world, *,
                 direction, camera_from_imu, calibration, output_intrinsics,
                 width, height, offset_s, row_reference, readout_s=0.,
                 max_input_gap_s=.05, grid_step=1, max_grid_error_px=.12,
                 convergence_px=.01, max_iterations=12, check_points=256):
        self.imu_times = _times(imu_times_s, 'IMU times', 2)
        self.source_times = _times(source_times_s, 'source times', 1)
        self.quats, flips, norm_error = normalized_quaternions(quaternions_wxyz)
        if len(self.quats) != len(self.imu_times):
            raise ValueError('quaternion and IMU timestamp counts differ')
        if direction not in ('body-to-world', 'world-to-body'):
            raise ValueError('direction must explicitly be body-to-world or world-to-body')
        if direction == 'world-to-body':
            self.quats[:, 1:] *= -1
        self.max_gap = _scalar(max_input_gap_s, 'max_input_gap_s', positive=True)
        if np.max(np.diff(self.imu_times)) > self.max_gap + 1e-12:
            raise ValueError('IMU sampling gap exceeds max_input_gap_s; use a continuous log')
        self.e = validate_rotation(camera_from_imu).copy()
        self.targets = _array(target_to_world, 'target_to_world').copy()
        if self.targets.shape != (len(self.source_times), 3, 3):
            raise ValueError('target_to_world must have shape (number of source times, 3, 3)')
        for target in self.targets:
            validate_rotation(target)
        self.cal = _calibration(calibration)
        self.k = _array(output_intrinsics, 'output_intrinsics').copy()
        if (self.k.shape != (3, 3) or not np.allclose(self.k[2], [0, 0, 1], atol=1e-12, rtol=0)
                or self.k[0, 0] <= 0 or self.k[1, 1] <= 0 or abs(self.k[1, 0]) > 1e-12):
            raise ValueError('output_intrinsics must be a finite upper-triangular pinhole K')
        self.inv = np.linalg.inv(self.k)
        self.width = _integer(width, 'width')
        self.height = _integer(height, 'height')
        self.offset = _scalar(offset_s, 'offset_s')
        self.readout = _scalar(readout_s, 'readout_s')
        self.row_reference = _scalar(row_reference, 'row_reference')
        if not 0 <= self.row_reference <= 1:
            raise ValueError('row_reference must lie in [0, 1] and match the source clock')
        self.step = _integer(grid_step, 'grid_step', maximum=32)
        if self.step & (self.step-1):
            raise ValueError('grid_step must be a power of two')
        self.grid_budget = _scalar(max_grid_error_px, 'max_grid_error_px', positive=True)
        self.convergence = _scalar(convergence_px, 'convergence_px', positive=True)
        self.iterations = _integer(max_iterations, 'max_iterations', maximum=100)
        check_points = _integer(check_points, 'check_points', maximum=100000)
        self.max_endpoint_rounding = 0.
        self._covered(self.source_times + self.offset)
        self.input_diagnostics = {'quaternion_sign_flips': flips, 'quaternion_norm_error': norm_error,
                                  'max_input_gap_s': float(np.max(np.diff(self.imu_times)))}
        # Include every boundary pixel plus reproducible, interior random points.
        rng = np.random.default_rng(0)
        points = np.column_stack((np.r_[rng.integers(0, self.width, check_points),
                                         np.arange(self.width), np.arange(self.width),
                                         np.zeros(self.height, int), np.full(self.height, self.width-1)],
                                  np.r_[rng.integers(0, self.height, check_points),
                                         np.zeros(self.width, int), np.full(self.width, self.height-1),
                                         np.arange(self.height), np.arange(self.height)]))
        points = np.unique(points, axis=0)
        self.check_x, self.check_y = points.T.astype(int)
        self.check_rays = self.rays(self.check_x, self.check_y)
        self._grid_cache = {}
        for array in (self.imu_times, self.source_times, self.quats, self.e, self.targets, self.k, self.inv):
            array.flags.writeable = False

    def _covered(self, times):
        times = _array(times, 'sensor times')
        low, high = self.imu_times[0], self.imu_times[-1]
        if np.any(times < low-4*abs(np.spacing(low))) or np.any(times > high+4*abs(np.spacing(high))):
            raise _CoverageError('sensor time outside IMU coverage; extrapolation forbidden')
        adjusted = np.clip(times, low, high)
        self.max_endpoint_rounding = max(self.max_endpoint_rounding, float(np.max(np.abs(times-adjusted))))
        return adjusted

    def _inverse_apply(self, times, world):
        times = self._covered(times)
        left = np.clip(np.searchsorted(self.imu_times, times, side='right')-1, 0, len(self.imu_times)-2)
        fraction = (times-self.imu_times[left])/(self.imu_times[left+1]-self.imu_times[left])
        q = slerp(self.quats[left], self.quats[left+1], fraction)
        vector, scalar = -q[..., 1:], q[..., :1]
        cross = 2*np.cross(vector, world)
        return world + scalar*cross + np.cross(vector, cross)

    def _index(self, index):
        return _integer(index, 'frame index', minimum=0, maximum=len(self.source_times)-1)

    def rays(self, x, y):
        x, y = np.broadcast_arrays(_array(x, 'output x'), _array(y, 'output y'))
        return np.stack((x, y, np.ones_like(x)), axis=-1) @ self.inv.T

    def exact(self, frame_index, output_rays, *, readout_s=None):
        """Evaluate arbitrary output rays; return (native_uv, convergence_report).

        The optional readout override supports zero-readout control experiments.
        Every evaluated source ray must be well-defined and converge. Native UV
        may lie outside the rectangle; maps() labels such pixels as invalid.
        """
        index = self._index(frame_index)
        readout = self.readout if readout_s is None else _scalar(readout_s, 'readout_s')
        rays = _rays(output_rays)
        shape = rays.shape[:-1]
        world = rays.reshape(-1, 3) @ self.targets[index].T
        center = self.source_times[index] + self.offset
        uv = _project(self._inverse_apply(np.full(len(world), center), world) @ self.e.T, self.cal)
        if not np.isfinite(uv).all():
            raise ValueError('undefined source-ray projection at optical pole or invalid model')
        maximum, iterations = 0., 0
        if readout:
            for iterations in range(1, self.iterations+1):
                sensor_times = center + (uv[:, 1]/self.cal['height']-self.row_reference)*readout
                current = _project(self._inverse_apply(sensor_times, world) @ self.e.T, self.cal)
                if not np.isfinite(current).all():
                    raise ValueError('undefined source-ray projection during row iteration')
                maximum = float(np.max(np.linalg.norm(current-uv, axis=1)))
                uv = current
                if maximum <= self.convergence:
                    break
            else:
                raise ValueError(f'row iteration did not converge within {self.iterations} steps: {maximum} native pixels')
        return uv.reshape(shape+(2,)), {'iterations': iterations,
            'last_iteration_update_native_px_max': maximum, 'readout_s': readout,
            'convergence_scope': 'last fixed-point update over evaluated rays; not a certified solution error bound'}

    def _grid(self, step):
        if step not in self._grid_cache:
            yy, xx = np.mgrid[:self.height, :self.width].astype(np.float32)
            if step == 1:
                self._grid_cache[step] = self.rays(xx, yy), None, None
            else:
                gy, gx = np.meshgrid(np.arange(0, self.height+step, step),
                                     np.arange(0, self.width+step, step), indexing='ij')
                self._grid_cache[step] = self.rays(gx, gy), xx/step, yy/step
        return self._grid_cache[step]

    def maps(self, frame_index, source_width, source_height):
        """Return float32 (map_x, map_y, diagnostics); invalid rectangle pixels=-1.

        Coordinates target a resized *source image*, using pixel-center scaling.
        Grid error is in native sensor pixels and is sampled, not full-image proof.
        No source image is resampled by this function.
        """
        index = self._index(frame_index)
        source_width = _integer(source_width, 'source_width')
        source_height = _integer(source_height, 'source_height')
        reference, reference_report = self.exact(index, self.check_rays)
        step = self.step
        retries = []
        while True:
            rays, ix, iy = self._grid(step)
            try:
                coarse, grid_report = self.exact(index, rays)
            except _CoverageError:
                if step == 1:
                    raise
                # Interpolation support extends beyond the real output raster.
                # Only that artificial support may trigger a smaller grid: a
                # failure on an in-raster node must remain a genuine failure.
                inside = rays[:(self.height+step-1)//step, :(self.width+step-1)//step]
                self.exact(index, inside)
                retries.append({'grid_step': step, 'reason': 'coarse_support_time_coverage'})
                step //= 2
                continue
            coarse = coarse.astype(np.float32)
            if step == 1:
                dense = coarse
            else:
                import cv2
                dense = cv2.remap(coarse, ix, iy, cv2.INTER_LINEAR)
            error = np.linalg.norm(dense[self.check_y, self.check_x]-reference, axis=-1)
            maximum = float(np.max(error))
            if maximum <= self.grid_budget:
                break
            if step == 1:
                raise ValueError('float32 map precision exceeds max_grid_error_px')
            retries.append({'grid_step': step, 'reason': 'sampled_grid_error'})
            step //= 2
        if self.readout:
            # Also check every delivered map coordinate's final native row;
            # fixed-point termination may move it slightly past the last query.
            self._covered(self.source_times[index] + self.offset +
                          (dense[..., 1]/self.cal['height']-self.row_reference)*self.readout)
        x = ((dense[..., 0]+.5)*source_width/self.cal['width']-.5).astype(np.float32)
        y = ((dense[..., 1]+.5)*source_height/self.cal['height']-.5).astype(np.float32)
        valid = np.isfinite(x) & np.isfinite(y) & (x >= 0) & (x <= source_width-1) & (y >= 0) & (y <= source_height-1)
        invalid = int(valid.size-np.count_nonzero(valid))
        x[~valid], y[~valid] = -1, -1
        report = {'frame_index': index, 'source_time_s': float(self.source_times[index]),
            'offset_s': self.offset, 'readout_s': self.readout, 'row_reference': self.row_reference,
            'grid_step_requested': self.step, 'grid_step_used': step,
            'grid_retries': retries,
            'sampled_grid_error_native_px_max': maximum, 'grid_check_points': len(reference),
            'grid_error_scope': 'sampled random and all boundary pixels; not full-image error proof',
            'reference_convergence': reference_report, 'grid_convergence': grid_report,
            'invalid_pixel_count': invalid, 'invalid_fraction': invalid/valid.size,
            'rectangular_coverage_passed': invalid == 0,
            'coverage_scope': 'finite resized pixel rectangle only; physical lens mask must be verified separately',
            'max_endpoint_rounding_s': self.max_endpoint_rounding,
            'extrapolation_allowed': False, 'visual_effect_verified': False,
            'privacy_verified': False, 'image_resampled': False}
        return x, y, report
