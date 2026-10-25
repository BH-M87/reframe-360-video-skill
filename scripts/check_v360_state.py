#!/usr/bin/env python3
"""Compare repeated absolute v360 poses with a static pose using synthetic media only."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def find_ffmpeg(explicit):
    candidates = [explicit] if explicit else [shutil.which('ffmpeg'), os.environ.get('IMAGEIO_FFMPEG_EXE')]
    if not explicit:
        try:
            spec = importlib.util.find_spec('imageio_ffmpeg')
        except (ImportError, ValueError):
            spec = None
        for root in (spec.submodule_search_locations or []) if spec else []:
            candidates.extend(str(p) for p in sorted((Path(root) / 'binaries').glob('ffmpeg*'), reverse=True))
    for candidate in candidates:
        found = shutil.which(candidate) if candidate else None
        if found:
            return str(Path(found).resolve())
    raise FileNotFoundError('FFmpeg unavailable; provide --ffmpeg, PATH, or a local imageio_ffmpeg binary. Nothing installed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg', help='Executable path or name; otherwise discover PATH/imageio_ffmpeg')
    parser.add_argument('--work-dir', help='New directory only; default: a new temporary directory')
    parser.add_argument('--omit-reset', action='store_true', help='Measure the old accumulation mode without requiring it to fail')
    parser.add_argument('--timeout', type=float, default=30, help='Timeout for each FFmpeg command in seconds')
    args = parser.parse_args()
    setup_error = None
    try:
        work = Path(args.work_dir).expanduser().absolute() if args.work_dir else Path(tempfile.mkdtemp(prefix='v360-state-'))
        if args.work_dir:
            work.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        work = Path(tempfile.mkdtemp(prefix='v360-state-error-'))
        setup_error = f'--work-dir must be a new writable path: {exc}'
    report = {'status': 'error', 'work_dir': str(work), 'requested_work_dir': args.work_dir,
              'omit_reset': args.omit_reset, 'commands': [],
              'parameters': {'frames': 20, 'fps': 10, 'duration_seconds': 2,
                             'width': 640, 'height': 360, 'h_fov': 100, 'yaw': 20, 'pitch': 10, 'roll': 5}}

    def run(label, command):
        entry = {'label': label, 'argv': command, 'cwd': str(work), 'timeout_seconds': args.timeout}
        report['commands'].append(entry)
        try:
            result = subprocess.run(command, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=args.timeout, check=False, shell=False)
        except subprocess.TimeoutExpired as exc:
            (work / (label + '.stdout.txt')).write_bytes(exc.stdout or b'')
            (work / (label + '.stderr.txt')).write_bytes(exc.stderr or b'')
            entry['timed_out'] = True
            raise RuntimeError(f'{label} exceeded {args.timeout}s') from exc
        entry['returncode'] = result.returncode
        (work / (label + '.stdout.txt')).write_bytes(result.stdout)
        (work / (label + '.stderr.txt')).write_bytes(result.stderr)
        if result.returncode:
            raise RuntimeError(f'{label} exited {result.returncode}: {result.stderr.decode(errors="replace")[-1500:]}')
        return result.stdout

    code = 2
    try:
        if setup_error:
            raise ValueError(setup_error)
        if args.timeout <= 0:
            raise ValueError('--timeout must be positive')
        ffmpeg = find_ffmpeg(args.ffmpeg)
        report['ffmpeg'] = ffmpeg
        report['version'] = run('version', [ffmpeg, '-version']).decode(errors='replace').splitlines()[0]
        pixels = bytearray()
        for y in range(512):
            for x in range(512):
                color = ((x * 5 + y // 16 * 31) % 256, (y * 7 + x // 24 * 47) % 256, (x ^ y) % 256)
                pixels.extend(color if (x - 256) ** 2 + (y - 256) ** 2 <= 250 ** 2 else (0, 0, 0))
        (work / 'texture.ppm').write_bytes(b'P6\n512 512\n255\n' + pixels)
        (work / 'camera.cmd').write_text(''.join(
            f'{i / 10:.1f} v360@cam yaw 20, v360@cam pitch 10, v360@cam roll 5;\n' for i in range(20)))
        base = 'v360@cam=input=fisheye:output=flat:yaw=20:pitch=10:roll=5:h_fov=100:w=640:h=360'
        dynamic = 'sendcmd=f=camera.cmd,' + base + ('' if args.omit_reset else ':reset_rot=1')
        common = [ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-y', '-loop', '1',
                  '-framerate', '10', '-i', 'texture.ppm', '-frames:v', '20', '-t', '2']
        for label, filt in [('reference', base), ('dynamic', dynamic)]:
            run(label, common + ['-vf', filt, '-an', '-c:v', 'rawvideo', '-pix_fmt', 'yuv444p', '-f', 'rawvideo', label + '.raw'])
        a = (work / 'reference.raw').read_bytes()
        b = (work / 'dynamic.raw').read_bytes()
        frame_size = 640 * 360 * 3
        report['frame_counts'] = {'reference': len(a) // frame_size, 'dynamic': len(b) // frame_size}
        report['byte_counts'] = {'reference': len(a), 'dynamic': len(b), 'expected': 20 * frame_size}
        report['pixel_format'] = 'yuv444p'
        report['sha256'] = {'reference': hashlib.sha256(a).hexdigest(), 'dynamic': hashlib.sha256(b).hexdigest()}
        if len(a) != 20 * frame_size or len(b) != 20 * frame_size:
            raise ValueError('Expected exactly 20 complete rawvideo frames from each render')
        rows, total, maximum = [], 0, 0
        for i in range(20):
            start, end = i * frame_size, (i + 1) * frame_size
            differences = [abs(x - y) for x, y in zip(a[start:end], b[start:end])]
            frame_sum, frame_max = sum(differences), max(differences)
            rows.append({'frame': i, 'maxdiff': frame_max, 'MAE': frame_sum / frame_size,
                         'changed_samples': sum(v != 0 for v in differences)})
            total += frame_sum
            maximum = max(maximum, frame_max)
        report.update({'frames': rows, 'maxdiff': maximum, 'MAE': total / len(a),
                       'equal_frames': sum(row['maxdiff'] == 0 for row in rows)})
        report['status'] = 'pass' if a == b else 'fail'
        code = 0 if a == b else 1
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    report_path = work / 'report.json'
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'report': str(report_path)}))
    return code


if __name__ == '__main__':
    sys.exit(main())
