#!/usr/bin/env python3
"""Validate normalized IMU CSV without claiming stabilization.

Time is seconds; --gyro-units is mandatory. Default maximum sample gap is 0.05s.
Quaternions use w,x,y,z. --gyro-quat-same-frame explicitly asserts body gyro axes,
body-to-world quaternions and identical timestamps; no axis or time search occurs.
Quaternion sign flips warn by default; --require-continuous-quaternion-sign fails.
CSV validity and descriptive correlation do not prove orientation alignment or visual stabilization.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys


class CSVInputError(ValueError):
    """Expected CSV shape or numeric-data failure, distinct from implementation bugs."""


def read_csv(path, columns):
    with path.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        missing = set(columns) - set(reader.fieldnames or [])
        if missing:
            raise CSVInputError(f'{path}: line 1: missing required columns {sorted(missing)}')
        result = []
        for row in reader:
            values = []
            for key in columns:
                value = row[key]
                if value is None or not value.strip():
                    raise CSVInputError(f'{path}: line {reader.line_num}: column {key!r} has no value; supply a numeric field')
                try:
                    values.append(float(value))
                except ValueError as error:
                    raise CSVInputError(f'{path}: line {reader.line_num}: column {key!r} requires a number, got {value!r}') from error
            result.append(values)
        return result


def fingerprint(path):
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sha256': digest.hexdigest()}


def multiply(a, b):
    w, x, y, z = a
    v, i, j, k = b
    return (w*v-x*i-y*j-z*k, w*i+x*v+y*k-z*j,
            w*j-x*k+y*v+z*i, w*k+x*j-y*i+z*v)


def correlation(rows, quaternions, units):
    if len(rows) != len(quaternions) or any(abs(a[0]-b[0]) > 1e-9 for a, b in zip(rows, quaternions)):
        return {'status': 'unavailable_timestamp_grids_differ', 'reason': 'Resample explicitly upstream; no time shift inferred.'}
    raw, derived = [], []
    scale = math.pi / 180 if units == 'deg/s' else 1
    for first, last, g0, g1 in zip(quaternions, quaternions[1:], rows, rows[1:]):
        q0, q1 = [row[1:] for row in (first, last)]
        q0, q1 = [[v / math.sqrt(sum(x*x for x in q)) for v in q] for q in (q0, q1)]
        # q and -q describe the same rotation; use the equivalent shortest arc in memory.
        if sum(a*b for a, b in zip(q0, q1)) < 0:
            q1 = [-v for v in q1]
        delta = multiply([q0[0], *[-v for v in q0[1:]]], q1)
        length = math.sqrt(sum(x*x for x in delta[1:]))
        factor = 2 * math.atan2(length, delta[0]) / ((last[0]-first[0]) * length) if length > 1e-12 else 0
        derived.append([v * factor for v in delta[1:]])
        raw.append([(a+b) * .5 * scale for a, b in zip(g0[1:], g1[1:])])
    values = []
    for axis in range(3):
        x, y = [row[axis] for row in raw], [row[axis] for row in derived]
        mx, my = statistics.mean(x), statistics.mean(y)
        denom = math.sqrt(sum((v-mx)**2 for v in x) * sum((v-my)**2 for v in y))
        values.append(sum((a-mx)*(b-my) for a, b in zip(x, y)) / denom if denom else None)
    return {'status': 'computed_descriptive_only', 'pearson_xyz': values,
            'assumption': 'body-to-world wxyz and body gyro; same timestamps; shortest equivalent quaternion arc; no axis/time inference'}


def validate(args):
    gyro_names = args.gyro_columns.split(',')
    if len(gyro_names) != 3:
        raise ValueError('--gyro-columns requires three comma-separated names')
    rows = read_csv(args.csv, [args.time_column, *gyro_names])
    checks = {}

    def check(name, passed, **details):
        checks[name] = {'passed': bool(passed), **details}

    def timing(data, prefix=''):
        finite = all(math.isfinite(v) for row in data for v in row)
        check(prefix + 'finite', finite)
        check(prefix + 'sample_count', len(data) >= 2, actual=len(data))
        times = [row[0] for row in data]
        deltas = [b-a for a, b in zip(times, times[1:])]
        ordered = finite and len(data) >= 2 and all(d > 0 for d in deltas)
        check(prefix + 'time_strictly_increasing', ordered)
        check(prefix + 'sampling_gaps', ordered and max(deltas) <= args.max_gap,
              max_allowed_s=args.max_gap, largest_s=max(deltas) if finite and deltas else None)
        coverage = finite and bool(times)
        if args.coverage_start is not None:
            coverage = coverage and times[0] <= args.coverage_start
        if args.coverage_end is not None:
            coverage = coverage and times[-1] >= args.coverage_end
        check(prefix + 'coverage', coverage, requested_start=args.coverage_start, requested_end=args.coverage_end,
              actual_start=times[0] if finite and times else None, actual_end=times[-1] if finite and times else None)
        return finite and ordered

    timing_ok = timing(rows)
    qrows = None
    if args.quaternion_csv or args.quat_columns:
        columns = (args.quat_columns or 'q_raw_w,q_raw_x,q_raw_y,q_raw_z').split(',')
        if len(columns) != 4:
            raise ValueError('--quat-columns requires four comma-separated wxyz names')
        qrows = read_csv(args.quaternion_csv or args.csv, [args.quat_time_column or args.time_column, *columns])
        qtime_ok = timing(qrows, 'quaternion_')
        norms = [math.sqrt(sum(v*v for v in row[1:])) for row in qrows]
        unit = all(math.isfinite(v) and abs(v-1) <= args.quat_norm_tolerance for v in norms)
        check('quaternion_unit_norm', unit and bool(qrows), tolerance=args.quat_norm_tolerance)
        continuous = all(sum(a*b for a, b in zip(first[1:], last[1:])) >= 0 for first, last in zip(qrows, qrows[1:]))
        check('quaternion_sign_continuity', (continuous and bool(qrows)) or not args.require_continuous_quaternion_sign,
              continuous_component_representation=continuous and bool(qrows),
              required=args.require_continuous_quaternion_sign)
    else:
        qtime_ok = unit = continuous = False
    relation = {'status': 'not_requested_same_frame_unconfirmed'}
    if args.gyro_quat_same_frame:
        relation = correlation(rows, qrows, args.gyro_units) if qrows and timing_ok and qtime_ok and unit else {'status': 'unavailable_invalid_or_missing_orientation'}
    valid = all(entry['passed'] for entry in checks.values())
    status = 'invalid'
    if valid:
        status = 'imu_samples_and_quaternion_samples_valid_orientation_unverified' if qrows else 'imu_samples_valid_orientation_unverified'
    return {'status': status, 'stabilization_proven': False, 'orientation_visual_alignment_verified': False,
            'continuous_component_representation': continuous if qrows else None,
            'warnings': ['quaternion_sign_normalization_required'] if qrows and not continuous else [], 'checks': checks,
            'gyro_units': args.gyro_units, 'gyro_quaternion_correlation': relation,
            'coverage_note': 'Without requested boundaries, coverage records observed range only.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', type=Path)
    parser.add_argument('--gyro-units', choices=['rad/s', 'deg/s'], required=True)
    parser.add_argument('--time-column', default='t_video_s')
    parser.add_argument('--gyro-columns', default='gyro_x_rad_s,gyro_y_rad_s,gyro_z_rad_s')
    parser.add_argument('--quaternion-csv', type=Path)
    parser.add_argument('--quat-columns', help='Comma-separated w,x,y,z; default q_raw_w,q_raw_x,q_raw_y,q_raw_z with --quaternion-csv')
    parser.add_argument('--quat-time-column')
    parser.add_argument('--gyro-quat-same-frame', action='store_true')
    parser.add_argument('--require-continuous-quaternion-sign', action='store_true',
                        help='Fail on q/-q sign flips; default records a representation warning only')
    parser.add_argument('--max-gap', type=float, default=.05)
    parser.add_argument('--coverage-start', type=float)
    parser.add_argument('--coverage-end', type=float)
    parser.add_argument('--quat-norm-tolerance', type=float, default=.01)
    parser.add_argument('--report', type=Path, required=True, help='New JSON path; existing files are never overwritten')
    args = parser.parse_args()
    inputs = [p.expanduser().resolve() for p in [args.csv, args.quaternion_csv] if p]
    args.csv = inputs[0]
    if args.quaternion_csv:
        args.quaternion_csv = inputs[1]
    report_path = args.report.expanduser().resolve()
    try:
        if report_path in inputs or report_path.exists():
            raise ValueError('Report must be a new path distinct from every input')
        limits = [args.max_gap, args.quat_norm_tolerance, *[v for v in (args.coverage_start, args.coverage_end) if v is not None]]
        if not all(math.isfinite(v) for v in limits) or args.max_gap <= 0 or args.quat_norm_tolerance < 0:
            raise ValueError('Thresholds must be finite; max gap positive and norm tolerance nonnegative')
        if args.coverage_start is not None and args.coverage_end is not None and args.coverage_start > args.coverage_end:
            raise ValueError('Coverage start must not exceed end')
        before = {str(p): fingerprint(p) for p in inputs}
        try:
            report = validate(args)
        except CSVInputError as error:
            report = {'status': 'invalid', 'stabilization_proven': False,
                      'orientation_visual_alignment_verified': False, 'errors': [str(error)],
                      'checks': {'csv_input': {'passed': False, 'message': str(error)}}}
        after = {str(p): fingerprint(p) for p in inputs}
        report['inputs'] = before
        report['checks']['inputs_unchanged'] = {'passed': before == after}
        if before != after:
            report['status'] = 'invalid'
        with report_path.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2, allow_nan=False)
            handle.write('\n')
        print(json.dumps({'status': report['status'], 'report': str(report_path)}))
        return 1 if report['status'] == 'invalid' else 0
    except (OSError, ValueError, OverflowError) as error:
        print(f'validate_imu: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
