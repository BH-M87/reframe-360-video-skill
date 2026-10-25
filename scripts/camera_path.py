"""Independent NumPy implementation of quaternion camera-path mathematics.

Hamilton quaternions are w,x,y,z; matrices act on column rays. Camera axes
are x right, y down, z forward. This implements a real-dt bidirectional
exponential SLERP filter, not Gyroflow's default algorithm. It verifies
geometry only; rendered stability and projection coverage need separate tests.
"""
import numpy as np

UNIT_NORM_TOLERANCE = 1e-3
AMBIGUOUS_DOT = 1e-8
POLE_TOLERANCE = 1e-6


def _finite(values, name):
    values = np.asarray(values, dtype=np.float64)
    if not np.isfinite(values).all():
        raise ValueError(f'{name} must be finite')
    return values


def _unit_vectors(values, name):
    values = _finite(values, name)
    if values.ndim == 0 or values.shape[-1] != 3:
        raise ValueError(f'{name} must contain 3-component vectors')
    norms = np.linalg.norm(values, axis=-1, keepdims=True)
    if np.any(norms < 1e-12) or not np.isfinite(norms).all():
        raise ValueError(f'{name} must have finite nonzero norm')
    return values / norms


def _times(times, name, minimum=2):
    times = _finite(times, name)
    if times.ndim != 1 or len(times) < minimum:
        raise ValueError(f'{name} needs at least {minimum} timestamps')
    if np.any(np.diff(times) <= 0):
        raise ValueError(f'{name} must be strictly increasing')
    return times


def normalized_quaternions(quaternions):
    """Return a fresh sign-continuous unit array, sign flip count, norm error."""
    q = _finite(quaternions, 'quaternions')
    if q.ndim != 2 or q.shape[1] != 4 or len(q) == 0:
        raise ValueError('quaternions must have shape (N, 4), Hamilton wxyz')
    norms = np.linalg.norm(q, axis=1)
    error = float(np.max(np.abs(norms - 1)))
    if not np.isfinite(norms).all() or error > UNIT_NORM_TOLERANCE:
        raise ValueError(f'quaternions must be unit norm within {UNIT_NORM_TOLERANCE}')
    q = q / norms[:, None]
    flips = 0
    for i in range(1, len(q)):
        dot = float(np.dot(q[i-1], q[i]))
        if abs(dot) <= AMBIGUOUS_DOT:
            raise ValueError(f'near 180-degree orientation step at sample {i}: shortest arc is ambiguous')
        if dot < 0:
            q[i] *= -1
            flips += 1
    return q, flips, error


def quat_to_matrix(quaternions):
    """Convert unit Hamilton quaternions (..., 4) to rotation matrices."""
    q = _finite(quaternions, 'quaternions')
    if q.shape[-1:] != (4,):
        raise ValueError('quaternions require four components')
    if not np.allclose(np.linalg.norm(q, axis=-1), 1, rtol=0, atol=1e-10):
        raise ValueError('quat_to_matrix requires unit quaternions')
    w, x, y, z = np.moveaxis(q, -1, 0)
    entries = [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w),
               2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w),
               2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]
    return np.stack(entries, axis=-1).reshape(q.shape[:-1] + (3, 3))


def slerp(start, end, amount):
    """Shortest-arc SLERP; exact antipodal orientations fail closed.

    Quaternion sign antipodes q/-q are equivalent and harmless. A zero dot
    instead describes a 180-degree *orientation* difference with two arcs.
    """
    start, end = np.broadcast_arrays(np.asarray(start, dtype=float), np.asarray(end, dtype=float))
    dot = np.sum(start * end, axis=-1)
    if np.any(np.abs(dot) <= AMBIGUOUS_DOT):
        raise ValueError('near 180-degree orientation difference: SLERP arc is ambiguous')
    end = np.where((dot < 0)[..., None], -end, end)
    dot = np.clip(np.abs(dot), 0, 1)
    amount = np.asarray(amount, dtype=float)
    theta = np.arccos(dot)
    # sinc avoids division by sin(theta) near zero and retains true SLERP.
    denom = np.sinc(theta / np.pi)
    a = (1-amount) * np.sinc((1-amount) * theta / np.pi) / denom
    b = amount * np.sinc(amount * theta / np.pi) / denom
    result = a[..., None] * start + b[..., None] * end
    return result / np.linalg.norm(result, axis=-1, keepdims=True)


def smooth_quaternions(times, quaternions, tau_s):
    """Offline forward/backward exponential SLERP using each real interval.

    The reverse step at i uses t[i+1]-t[i]. Finite boundaries and nonlinear
    rotations mean this must not be advertised as strictly zero-phase.
    """
    times = _times(times, 'input time')
    q, _, _ = normalized_quaternions(quaternions)
    if len(q) != len(times):
        raise ValueError('quaternion count must match input time')
    tau = float(_finite(tau_s, 'tau_s'))
    if tau < 0:
        raise ValueError('tau_s must be nonnegative')
    if tau == 0:
        return q
    with np.errstate(over='ignore', under='ignore'):
        alpha = -np.expm1(-np.diff(times) / tau)
    result = q.copy()
    for i in range(1, len(q)):
        result[i] = slerp(result[i-1], q[i], alpha[i-1])
    for i in range(len(q)-2, -1, -1):
        result[i] = slerp(result[i+1], result[i], alpha[i])
    return result


def sample_quaternions(times, quaternions, sample_times):
    """Interpolate within covered time only; never extrapolate or bridge gaps."""
    if sample_times[0] < times[0] or sample_times[-1] > times[-1]:
        raise ValueError('requested sample time is outside input coverage; extrapolation is forbidden')
    left = np.clip(np.searchsorted(times, sample_times, side='right') - 1, 0, len(times)-2)
    amount = (sample_times-times[left]) / (times[left+1]-times[left])
    return slerp(quaternions[left], quaternions[left+1], amount)


def _covered_times(times, requested):
    """Accept only endpoint roundoff of at most four float64 ULPs.

    Decimal clocks such as .1+.2 can land one ULP past a CSV endpoint .3.
    This is not permission to extrapolate: interpolation uses the exact endpoint
    and both the adjusted timestamps and the correction magnitude are returned.
    """
    lower_slack = 4 * abs(np.spacing(times[0]))
    upper_slack = 4 * abs(np.spacing(times[-1]))
    if (times[0] - requested[0] > lower_slack or
            requested[-1] - times[-1] > upper_slack):
        raise ValueError('requested sample time is outside input coverage; extrapolation is forbidden')
    adjusted = np.clip(requested, times[0], times[-1])
    _times(adjusted, 'adjusted sample time', minimum=1)
    difference = np.abs(adjusted - requested)
    return adjusted, {'adjusted_samples': int(np.count_nonzero(difference)),
                      'max_adjustment_s': float(np.max(difference)), 'maximum_endpoint_ulps': 4}


def validate_rotation(matrix):
    matrix = _finite(matrix, 'camera_from_imu')
    if (matrix.shape != (3, 3) or
            not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-8, rtol=0) or
            not np.isclose(np.linalg.det(matrix), 1, atol=1e-8, rtol=0)):
        raise ValueError('camera_from_imu must be a proper SO(3) 3x3 rotation, not a reflection or axis guess')
    return matrix


def horizon_basis(forward, world_up):
    """Keep forward and rebuild right/down using explicitly asserted world up."""
    forward = _unit_vectors(forward, 'forward')
    up = _unit_vectors(world_up, 'world_up')
    if up.shape != (3,):
        raise ValueError('world_up requires exactly three components')
    right = np.cross(-up, forward)
    norms = np.linalg.norm(right, axis=-1, keepdims=True)
    if np.any(norms <= POLE_TOLERANCE):
        raise ValueError('forward is near a world-up pole: horizon heading is undefined')
    right /= norms
    down = np.cross(forward, right)
    return np.stack((right, down, forward), axis=-1)


def forward_angles(source, target):
    return np.arctan2(np.linalg.norm(np.cross(source, target), axis=-1),
                      np.clip(np.sum(source * target, axis=-1), -1, 1))


def cap_forward(source, target, limit_rad):
    """Limit forward direction only; caller must rebuild the horizontal basis."""
    source = _unit_vectors(source, 'source forward')
    target = _unit_vectors(target, 'target forward')
    theta = forward_angles(source, target)
    if np.any(np.pi-theta <= 1e-6):
        raise ValueError('near antipodal forward directions: limiting arc is ambiguous')
    fraction = np.ones_like(theta)
    active = theta > limit_rad
    fraction[active] = limit_rad / theta[active]
    # Stable spherical interpolation of vectors; theta is strictly below pi.
    denom = np.sinc(theta / np.pi)
    a = (1-fraction) * np.sinc((1-fraction)*theta / np.pi) / denom
    b = fraction * np.sinc(fraction*theta / np.pi) / denom
    result = a[..., None] * source + b[..., None] * target
    return _unit_vectors(result, 'limited forward'), int(np.count_nonzero(active))


def _summary(values):
    values = _finite(values, 'motion statistics')
    if not len(values):
        return {'count': 0, 'max': None, 'mean': None, 'rms': None}
    scale = float(np.max(np.abs(values)))
    normalized = values / scale if scale else values
    return {'count': int(len(values)), 'max': float(np.max(values)),
            'mean': float(np.mean(normalized) * scale),
            'rms': float(np.sqrt(np.mean(normalized**2)) * scale)}


def _angular_motion(matrices, times):
    relative = np.swapaxes(matrices[:-1], 1, 2) @ matrices[1:]
    cosine = np.clip((np.trace(relative, axis1=1, axis2=2) - 1)/2, -1, 1)
    skew = np.stack((relative[:, 2, 1]-relative[:, 1, 2],
                     relative[:, 0, 2]-relative[:, 2, 0],
                     relative[:, 1, 0]-relative[:, 0, 1]), axis=1)
    angles = np.arctan2(np.linalg.norm(skew, axis=1)/2, cosine)
    with np.errstate(over='ignore', divide='ignore', invalid='ignore'):
        velocity = np.rad2deg(angles) / np.diff(times)
    midpoint = (times[1:] + times[:-1])/2
    with np.errstate(over='ignore', divide='ignore', invalid='ignore'):
        acceleration = np.abs(np.diff(velocity) / np.diff(midpoint))
    return _summary(velocity), _summary(acceleration)


def _matrix_checks(matrix):
    return {'finite': bool(np.isfinite(matrix).all()),
            'max_orthogonality_error': float(np.max(np.abs(np.swapaxes(matrix, 1, 2) @ matrix - np.eye(3)))),
            'max_determinant_error': float(np.max(np.abs(np.linalg.det(matrix)-1)))}


def build_path(times, quaternions, sample_times, *, direction, camera_from_imu,
               tau_s=.35, max_input_gap_s=.05, world_up=None, max_forward_angle_deg=None):
    """Smooth the covered input log first, then sample requested IMU times.

    C = R_imu @ E.T, where E=camera_from_imu. The output ray mapping is
    C.T @ T. All rotations and timestamps are float64; no source data is edited.
    """
    times = _times(times, 'input time')
    sample_times = _times(sample_times, 'sample time', minimum=1)
    sample_times, endpoint_rounding = _covered_times(times, sample_times)
    q, flips, norm_error = normalized_quaternions(quaternions)
    if len(q) != len(times):
        raise ValueError('quaternion count must match input time')
    gap = float(_finite(max_input_gap_s, 'max_input_gap_s'))
    if gap <= 0:
        raise ValueError('max_input_gap_s must be positive')
    maximum_gap = float(np.max(np.diff(times)))
    if maximum_gap > gap + 1e-12:
        raise ValueError(f'input sampling gap {maximum_gap:.9g}s exceeds max_input_gap_s={gap:.9g}; split into covered continuous logs')
    e = validate_rotation(camera_from_imu)
    if direction not in ('body-to-world', 'world-to-body'):
        raise ValueError('direction must explicitly be body-to-world or world-to-body')
    if direction == 'world-to-body':
        q[:, 1:] *= -1
    if max_forward_angle_deg is not None:
        limit = float(_finite(max_forward_angle_deg, 'max_forward_angle_deg'))
        if not 0 <= limit < 180:
            raise ValueError('max_forward_angle_deg must be at least 0 and less than 180')
        if world_up is None:
            raise ValueError('forward cap requires explicit world_up and horizon lock')
    smooth = smooth_quaternions(times, q, tau_s)
    camera = quat_to_matrix(sample_quaternions(times, q, sample_times)) @ e.T
    target = quat_to_matrix(sample_quaternions(times, smooth, sample_times)) @ e.T
    raw_forward = camera[:, :, 2]
    if world_up is not None:
        up = _unit_vectors(world_up, 'world_up')
        target = horizon_basis(target[:, :, 2], up)
    before = np.rad2deg(forward_angles(raw_forward, target[:, :, 2]))
    clamp_count = 0
    if max_forward_angle_deg is not None:
        limited, clamp_count = cap_forward(raw_forward, target[:, :, 2], np.deg2rad(limit))
        target = horizon_basis(limited, up)
    arrays = {'imu_sample_timestamps_s': sample_times,
              'camera_to_world': camera, 'target_to_world': target,
              'output_ray_to_source_camera': np.swapaxes(camera, 1, 2) @ target}
    if world_up is not None:
        arrays['world_up'] = up
    checks = {name: _matrix_checks(arrays[name]) for name in
              ('camera_to_world', 'target_to_world', 'output_ray_to_source_camera')}
    for name, check in checks.items():
        if (not check['finite'] or check['max_orthogonality_error'] > 1e-7 or
                check['max_determinant_error'] > 1e-7):
            raise ValueError(f'output rotation numerical validation failed: {name}')
    camera_velocity, camera_acceleration = _angular_motion(camera, sample_times)
    target_velocity, target_acceleration = _angular_motion(target, sample_times)
    diagnostics = {
        'input_samples': len(times), 'output_frames': len(sample_times),
        'input_sign_flips_normalized': flips, 'input_max_unit_norm_error': norm_error,
        'input_max_gap_s': maximum_gap, 'max_input_gap_s': gap,
        'endpoint_rounding': endpoint_rounding,
        'input_time_range_s': [float(times[0]), float(times[-1])],
        'imu_sample_time_range_s': [float(sample_times[0]), float(sample_times[-1])],
        'matrix_checks': checks, 'forward_clamp_count': clamp_count,
        'forward_clamp_fraction': clamp_count/len(sample_times),
        'forward_angle_deg': {'before': _summary(before),
                              'after': _summary(np.rad2deg(forward_angles(raw_forward, target[:, :, 2])))},
        'angular_velocity_deg_s': {'camera': camera_velocity, 'target': target_velocity},
        'angular_speed_change_deg_s2': {'camera': camera_acceleration, 'target': target_acceleration},
        'projection_coverage_verified': False, 'stabilization_proven': False,
        'visual_effect_verified': False,
    }
    if world_up is not None:
        gravity_camera = np.swapaxes(target, 1, 2) @ (-up)
        diagnostics['horizon_geometry'] = {
            'max_abs_roll_deg': float(np.max(np.abs(np.rad2deg(np.arctan2(gravity_camera[:, 0], gravity_camera[:, 1]))))),
            'interpretation': 'geometric relation to asserted world_up only; not rendered horizon or stabilization proof',
        }
    return arrays, diagnostics
