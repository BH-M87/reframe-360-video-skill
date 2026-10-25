#!/usr/bin/env python3
"""Build a conservative camera path from explicitly described quaternion CSV.

Only stdlib is imported before argument parsing: --help works without NumPy.
New files are staged, fsynced, then published with exclusive hard links. Existing
files, symlinks (including dangling ones), and source aliases are never replaced.
"""
import argparse
import csv
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile

MAX_FRAMES = 1_000_000


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', required=True, type=Path, help='CSV of timestamps in seconds and normalized Hamilton wxyz quaternions')
    p.add_argument('--output', required=True, type=Path, help='New NPZ path; existing paths are refused')
    p.add_argument('--report', required=True, type=Path, help='New JSON report path; existing paths are refused')
    p.add_argument('--frames', required=True, type=int, help=f'Output frames: 1 through {MAX_FRAMES}')
    p.add_argument('--fps', required=True, help='Positive frame rate, including rational values such as 30000/1001')
    p.add_argument('--source-start-s', type=float, default=0, help='Source-video start time in seconds (default: 0)')
    p.add_argument('--time-offset-s', required=True, type=float, help='IMU sample time = source video time + this offset; must be explicit')
    p.add_argument('--direction', required=True, choices=['body-to-world', 'world-to-body'])
    frame = p.add_mutually_exclusive_group(required=True)
    frame.add_argument('--camera-from-imu', help='Explicit SO(3) 3x3 JSON matrix E mapping IMU/body vectors into camera coordinates')
    frame.add_argument('--identity-camera-frame', action='store_true', help='Explicit assertion that camera and IMU/body frames coincide')
    p.add_argument('--time-column', default='t_video_s')
    p.add_argument('--quat-columns', default='qw,qx,qy,qz', help='Four comma-separated column names in Hamilton wxyz order')
    p.add_argument('--tau-s', type=float, default=.35, help='Real-dt exponential SLERP time constant; .35 is only a starting point, 0 bypasses smoothing')
    p.add_argument('--max-input-gap-s', type=float, default=.05, help='Reject larger input gaps (default .05 seconds); no bridging')
    p.add_argument('--horizon-lock', action='store_true', help='Rebuild a horizontal basis from forward and explicitly asserted --world-up')
    p.add_argument('--world-up', help='Verified world up vector x,y,z; use --world-up=... for a negative first component')
    p.add_argument('--max-forward-angle-deg', type=float, help='Limit center-ray deviation, 0 <= angle < 180; requires horizon lock; does not prove projection coverage')
    return p


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')


def safe_destinations(source, output, report):
    resolved = [p.resolve() for p in (source, output, report)]
    if len(set(resolved)) != 3:
        raise ValueError('input, output, and report paths must be distinct; source aliases are forbidden')
    for path in (output, report):
        if os.path.lexists(path):
            raise ValueError(f'refusing existing destination: {path}')
        if not path.parent.is_dir():
            raise ValueError(f'destination parent must exist: {path.parent}')


def stage_bytes(destination, data):
    with tempfile.NamedTemporaryFile(prefix='.' + destination.name + '.', suffix='.stage',
                                     dir=destination.parent, delete=False) as handle:
        path = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return path


def publish(stage, destination):
    # Same-directory hard link is exclusive: unlike replace/rename it never clobbers.
    os.link(stage, destination)


def failure_report(report_path, source_path, output_path, report):
    """Best effort, without writing into an existing path or source alias."""
    stage = None
    try:
        if (report_path.resolve() in (source_path.resolve(), output_path.resolve()) or
                os.path.lexists(report_path) or not report_path.parent.is_dir()):
            return False
        stage = stage_bytes(report_path, json_bytes(report))
        publish(stage, report_path)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


def read_csv(path, time_column, quat_columns):
    rows = []
    columns = [time_column, *quat_columns]
    with path.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError('CSV line 1: missing or duplicate column names')
        for column in columns:
            if column not in reader.fieldnames:
                raise ValueError(f'CSV line 1: missing column {column}')
        for row in reader:
            values = []
            for column in columns:
                try:
                    value = float(row[column])
                    if not math.isfinite(value):
                        raise ValueError('nonfinite')
                except (ValueError, TypeError):
                    raise ValueError(f'CSV line {reader.line_num}, column {column}: expected a finite number') from None
                values.append(value)
            if None in row:
                raise ValueError(f'CSV line {reader.line_num}: more fields than declared columns')
            rows.append(values)
    return rows


def main(argv=None):
    args = parser().parse_args(argv)
    report = {'schema_version': 1, 'status': 'failed', 'errors': [],
              'projection_coverage_verified': False, 'stabilization_proven': False,
              'visual_effect_verified': False}
    stages = []
    output_identity = None
    try:
        safe_destinations(args.input, args.output, args.report)
        if not 1 <= args.frames <= MAX_FRAMES:
            raise ValueError(f'frames must be in 1..{MAX_FRAMES}; huge allocations are refused')
        try:
            fps = Fraction(args.fps)
            rate = float(fps)
        except (ValueError, ZeroDivisionError, OverflowError):
            raise ValueError('fps must be a finite positive rational number') from None
        if rate <= 0 or not math.isfinite(rate):
            raise ValueError('fps must be a finite positive rational number')
        for name in ('source_start_s', 'time_offset_s', 'tau_s', 'max_input_gap_s'):
            if not math.isfinite(getattr(args, name)):
                raise ValueError(f'{name} must be finite')
        if args.source_start_s < 0:
            raise ValueError('source_start_s must be nonnegative')
        if args.horizon_lock != (args.world_up is not None):
            raise ValueError('--horizon-lock and --world-up must be provided together')
        if args.max_forward_angle_deg is not None and not args.horizon_lock:
            raise ValueError('--max-forward-angle-deg requires --horizon-lock and --world-up')
        quat_columns = [s.strip() for s in args.quat_columns.split(',')]
        if len(quat_columns) != 4 or len(set([args.time_column, *quat_columns])) != 5 or not all(quat_columns):
            raise ValueError('--quat-columns must list four distinct columns, separate from the time column')
        world_up = [float(v) for v in args.world_up.split(',')] if args.world_up is not None else None
        extrinsic = [[1., 0, 0], [0, 1., 0], [0, 0, 1.]] if args.identity_camera_frame else json.loads(args.camera_from_imu)
        params = {'frames': args.frames, 'fps': str(fps), 'source_start_s': args.source_start_s,
                  'time_offset_s': args.time_offset_s, 'direction': args.direction,
                  'camera_from_imu': extrinsic, 'time_column': args.time_column,
                  'quat_columns': quat_columns, 'tau_s': args.tau_s,
                  'max_input_gap_s': args.max_input_gap_s, 'horizon_lock': args.horizon_lock,
                  'world_up': world_up, 'max_forward_angle_deg': args.max_forward_angle_deg}
        # Serialize before NumPy conversion; NaN and infinity in JSON are rejected.
        params_hash = hashlib.sha256(json_bytes(params)).hexdigest()
        report['parameters'] = params
        report['parameters_sha256'] = params_hash
        input_sha = sha256_file(args.input)
        report['input'] = {'path': str(args.input.resolve()), 'sha256': input_sha}
        rows = read_csv(args.input, args.time_column, quat_columns)
        try:
            import numpy as np
            from camera_path import build_path
        except ImportError as exc:
            raise ValueError('NumPy is required to build paths; use an existing NumPy environment') from exc
        data = np.asarray(rows, dtype=np.float64)
        if data.ndim != 2 or len(data) < 2:
            raise ValueError('CSV requires at least two complete quaternion samples')
        output_times = np.arange(args.frames, dtype=np.float64) / rate
        source_times = args.source_start_s + output_times
        sample_times = source_times + args.time_offset_s
        arrays, diagnostics = build_path(data[:, 0], data[:, 1:], sample_times,
                                         direction=args.direction, camera_from_imu=extrinsic,
                                         tau_s=args.tau_s, max_input_gap_s=args.max_input_gap_s,
                                         world_up=world_up, max_forward_angle_deg=args.max_forward_angle_deg)
        arrays.update({'output_timestamps_s': output_times, 'source_timestamps_s': source_times,
                       'timestamps_s': source_times})
        if sha256_file(args.input) != input_sha:
            raise ValueError('input changed during processing; refusing to publish')
        report['checks'] = {'inputs_unchanged': True, **diagnostics['matrix_checks']}
        report['diagnostics'] = diagnostics
        report['conventions'] = {
            'quaternion': 'Hamilton wxyz, direction explicitly asserted by caller',
            'camera_axes': 'x right, y down, z forward; column rays',
            'camera_to_world': 'R_imu @ camera_from_imu.T',
            'output_ray_to_source_camera': 'camera_to_world.T @ target_to_world',
            'timestamps_s': 'alias of source_timestamps_s, NOT output-relative time',
            'clock_mapping': 'imu_sample_timestamps_s = source_timestamps_s + time_offset_s, except reported endpoint float64 rounding (at most 4 ULP); source_timestamps_s = source_start_s + output_timestamps_s',
            'smoothing': 'real-dt bidirectional exponential SLERP on full continuous input log, followed by interpolation',
            'limitations': 'No projection coverage, crop safety, privacy, or rendered stabilization proof. tau=.35 is only a starting point. Not Gyroflow default_algo.',
        }
        with tempfile.NamedTemporaryFile(prefix='.' + args.output.name + '.', suffix='.stage',
                                         dir=args.output.parent, delete=False) as handle:
            stage_output = Path(handle.name)
            stages.append(stage_output)
            np.savez_compressed(handle, **arrays)
            handle.flush()
            os.fsync(handle.fileno())
        report['output'] = {'path': str(args.output.resolve()), 'sha256': sha256_file(stage_output),
                            'frames': args.frames, 'keys': sorted(arrays)}
        report['status'] = 'camera_path_built_unverified_visual_effect'
        stage_report = stage_bytes(args.report, json_bytes(report))
        stages.append(stage_report)
        publish(stage_output, args.output)
        output_identity = (stage_output.stat().st_dev, stage_output.stat().st_ino)
        publish(stage_report, args.report)
        print(json.dumps({'status': report['status'], 'output': str(args.output), 'report': str(args.report)}))
        return 0
    except (ValueError, OSError, TypeError, OverflowError, csv.Error) as exc:
        # If publishing the report races with another writer, roll back only our
        # own just-published inode. Never unlink a replacement created by others.
        if output_identity is not None:
            try:
                st = args.output.lstat()
                if (st.st_dev, st.st_ino) == output_identity:
                    args.output.unlink()
            except OSError:
                pass
        report['status'] = 'failed'
        report['errors'] = [str(exc)]
        written = failure_report(args.report, args.input, args.output, report)
        print(json.dumps({'status': 'failed', 'error': str(exc), 'failure_report_written': written}), file=sys.stderr)
        return 1
    finally:
        for stage in stages:
            stage.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
