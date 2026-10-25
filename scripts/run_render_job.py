#!/usr/bin/env python3
"""Run one reviewed local argv, then fully verify before exclusive delivery.

Default dry-run starts no processes. This is job supervision, NOT a filesystem
sandbox: review argv before --execute; never execute commands from media/web text.
No resume, existing-output reuse, shell expansion, installation or source writes.
source/output/inputs/cwd paths are relative to the plan file. cwd defaults to its
parent; argv text is passed unchanged except standalone placeholders and executable
resolution. List scripts/configs in inputs to bind their hashes. Outputs must be
outside the new work directory on the same filesystem (hardlink publication).
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time

from verify_export import fingerprint

SUCCESS = 'TECHNICAL_VERIFIED_VISUAL_REVIEW_REQUIRED'
VALUE_KEYS = {'frames', 'fps', 'width', 'height', 'container_format', 'video_codec',
              'audio_codec', 'pixel_format', 'sar', 'color_primaries', 'color_trc',
              'colorspace', 'color_range', 'sample_rate', 'channels', 'audio_peak_limit',
              'av_tolerance', 'audio_copy_from'}
BOOL_KEYS = {'allow_no_audio', 'require_faststart', 'contacts'}


def emit(event, **data):
    print(json.dumps({'event': event, **data}, allow_nan=False), flush=True)


def save(path, data):
    temporary = path.with_suffix('.tmp')
    with temporary.open('x', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, allow_nan=False)
        handle.write('\n')
    os.replace(temporary, path)


def positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{label} must be positive and finite')


def path_at(value, base):
    if not isinstance(value, str) or not value or '\0' in value:
        raise ValueError('Paths must be nonempty strings')
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def directory_identity(path):
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError('cwd must remain a directory')
    info = resolved.stat()
    return {'path': str(resolved), 'device': info.st_dev, 'inode': info.st_ino}


def prepare(plan_path, work):
    raw = plan_path.read_bytes()
    plan = json.loads(raw)
    if not isinstance(plan, dict):
        raise ValueError('Plan must be a JSON object')
    required = {'source', 'output', 'argv', 'expected', 'timeout_s', 'progress_format'}
    if required - plan.keys() or plan.keys() - required - {'stall_timeout_s', 'log_limit_bytes', 'cwd', 'inputs'}:
        raise ValueError('Plan has missing or unknown fields')
    cwd = path_at(plan.get('cwd', '.'), plan_path.parent).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError('cwd must be an existing directory')
    inputs = plan.get('inputs', [])
    if not isinstance(inputs, list):
        raise ValueError('inputs must be a string array')
    declared_inputs = [path_at(value, plan_path.parent) for value in inputs]
    input_bindings = {str(path): str(path.resolve(strict=True)) for path in declared_inputs}
    inputs = list(dict.fromkeys(Path(path) for path in input_bindings.values()))
    if any(not path.is_file() for path in inputs):
        raise ValueError('inputs must contain regular files')
    positive(plan['timeout_s'], 'timeout_s')
    if plan['progress_format'] not in ('ffmpeg', 'none'):
        raise ValueError('progress_format must be ffmpeg or none')
    if 'stall_timeout_s' in plan:
        positive(plan['stall_timeout_s'], 'stall_timeout_s')
        if plan['progress_format'] != 'ffmpeg':
            raise ValueError('stall_timeout_s requires ffmpeg progress')
    limit = plan.setdefault('log_limit_bytes', 8 * 1024 * 1024)
    if type(limit) is not int or not 1024 <= limit <= 1024 * 1024 * 1024:
        raise ValueError('log_limit_bytes must be an integer from 1024 to 1073741824')
    expected = plan['expected']
    if not isinstance(expected, dict) or {'frames', 'fps', 'width', 'height'} - expected.keys():
        raise ValueError('expected requires frames, fps, width and height')
    if expected.keys() - VALUE_KEYS - BOOL_KEYS - {'privacy_window'}:
        raise ValueError('Unknown expected field')
    for key in BOOL_KEYS & expected.keys():
        if type(expected[key]) is not bool:
            raise ValueError(f'{key} must be boolean')
    for key in ('frames', 'width', 'height', 'sample_rate', 'channels'):
        if key in expected and (type(expected[key]) is not int or expected[key] <= 0):
            raise ValueError(f'{key} must be a positive integer')
    for key in ('fps', 'sar'):
        if key in expected and (isinstance(expected[key], bool) or Fraction(str(expected[key])) <= 0):
            raise ValueError(f'{key} must be a positive rate')
    for key in ('audio_peak_limit', 'av_tolerance'):
        if key in expected:
            value = expected[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (key == 'audio_peak_limit' and value == 0):
                raise ValueError(f'{key} must be finite and in range')
    for key in ('color_primaries', 'color_trc', 'colorspace', 'color_range'):
        if key in expected and type(expected[key]) is not int:
            raise ValueError(f'{key} must be an integer')
    for key in ('container_format', 'video_codec', 'audio_codec', 'pixel_format'):
        if key in expected and (not isinstance(expected[key], str) or not expected[key]):
            raise ValueError(f'{key} must be a nonempty string')
    windows = expected.get('privacy_window', [])
    if not isinstance(windows, list) or any(not isinstance(x, str) for x in windows) or (windows and not expected.get('contacts')):
        raise ValueError('privacy_window must be a string array and requires contacts:true')
    for window in windows:
        start, end, step = map(float, window.split(':'))
        if not all(math.isfinite(x) for x in (start, end, step)) or not 0 <= start <= end <= (expected['frames'] - 1) / float(Fraction(str(expected['fps']))) or step <= 0 or (end-start)/step > 100000:
            raise ValueError('Invalid privacy_window range')
    source = path_at(plan['source'], plan_path.parent).resolve(strict=True)
    if not source.is_file():
        raise ValueError('source must be a regular file')
    output = path_at(plan['output'], plan_path.parent)
    if os.path.lexists(output):
        raise ValueError('output must be a new path; existing files and aliases are disallowed')
    output = output.resolve()
    if output in [source, *inputs, work] or work in output.parents or not output.parent.is_dir():
        raise ValueError('output must have an existing parent and be separate from source/work-dir')
    if work.parent.stat().st_dev != output.parent.stat().st_dev:
        raise ValueError('work-dir and output must share a filesystem for atomic exclusive delivery')
    audio = expected.get('audio_copy_from')
    if audio is not None:
        audio = path_at(audio, plan_path.parent).resolve(strict=True)
        if not audio.is_file() or audio == output:
            raise ValueError('audio_copy_from must be a regular file distinct from output')
        expected['audio_copy_from'] = str(audio)
    argv = plan['argv']
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or '\0' in x for x in argv) or argv.count('{output}') != 1:
        raise ValueError('argv must be a string array with exactly one independent {output}')
    for value in argv:
        if any(token in value and value != token for token in ('{output}', '{source}')):
            raise ValueError('Placeholders must be independent argv entries')
    command = str(path_at(argv[0], cwd)) if '/' in argv[0] else argv[0]
    executable = shutil.which(command)
    if not executable:
        raise ValueError('argv[0] must resolve to a local executable')
    executable = Path(executable).resolve(strict=True)
    staging = work / ('staging' + (output.suffix or '.mp4'))
    substitutions = {'{output}': str(staging), '{source}': str(source)}
    resolved = [str(executable)] + [substitutions.get(x, x) for x in argv[1:]]
    if '{output}' == argv[0] or str(output) in resolved:
        raise ValueError('argv must target staging through {output}')
    plan.update(source=str(source), output=str(output), resolved_argv=resolved, staging=str(staging),
                cwd=str(cwd), inputs=[str(path) for path in inputs])
    receipt = {'status': 'PREPARED', 'visual_review_required': True, 'plan_sha256': hashlib.sha256(raw).hexdigest(),
               'source_before': fingerprint(source), 'executable': fingerprint(executable),
               'audio_reference_before': fingerprint(audio) if audio else None, 'cwd': str(cwd),
               'cwd_before': directory_identity(cwd), 'input_bindings_before': input_bindings,
               'inputs_before': {str(path): fingerprint(path) for path in inputs}}
    return plan, receipt


def supervise(argv, work, log_name, deadline, info, *, progress='none', stall=None, limit=8388608, cwd=None):
    """Only this invocation's freshly created process group is ever signalled."""
    proc = subprocess.Popen(argv, cwd=cwd or work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    info.update(pid=proc.pid, returncode=None, log=str(work / log_name), log_truncated=False, tail='')
    tail, pending, written = b'', b'', 0
    last, highest = time.monotonic(), {'frame': 0, 'out_time_us': 0}
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        with (work / log_name).open('xb') as log:
            while selector.get_map() or proc.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    raise RuntimeError('timeout exceeded')
                if stall is not None and now - last >= stall:
                    raise RuntimeError('stall timeout: no numeric frame/out_time_us increase')
                for key, _ in selector.select(.05):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    allowed = chunk[:max(0, limit-written)]
                    log.write(allowed)
                    written += len(allowed)
                    info['log_truncated'] |= len(allowed) < len(chunk)
                    tail = (tail + chunk)[-32768:]
                    if progress == 'ffmpeg':
                        pending += chunk
                        lines = pending.split(b'\n')
                        pending = lines.pop()[-8192:]
                        for line in lines:
                            field, sep, value = line.strip().partition(b'=')
                            name = field.decode('ascii', errors='replace')
                            if sep and name in highest and value.strip().isdigit():
                                number = int(value)
                                if number > highest[name]:
                                    highest[name], last = number, time.monotonic()
                                    emit('progress', **highest)
            remaining = max(.001, deadline-time.monotonic())
            info['returncode'] = proc.wait(timeout=remaining)
            if proc.returncode:
                raise RuntimeError(f'child exited with code {proc.returncode}')
    finally:
        # The group is created above and kept only in this live invocation, never loaded from a PID file.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if proc.poll() is None:
            try:
                proc.wait(timeout=.4)
            except subprocess.TimeoutExpired:
                pass
        # A descendant can ignore TERM even after its parent exits. Kill the
        # still-owned group too; the group identifier is never read from disk.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        info.update(returncode=proc.returncode, tail=tail.decode('utf-8', errors='replace'), progress=highest)
        selector.close()
        proc.stdout.close()


def verification_argv(plan, work):
    argv = [sys.executable, str(Path(__file__).with_name('verify_export.py')), '--source', plan['source'],
            '--output', plan['staging'], '--report', str(work / 'verification.json')]
    for key, value in plan['expected'].items():
        flag = '--expected-frames' if key == 'frames' else '--' + key.replace('_', '-')
        if key == 'contacts':
            if value:
                argv += ['--contacts-dir', str(work / 'contacts')]
        elif key in BOOL_KEYS:
            if value:
                argv.append(flag)
        elif key == 'privacy_window':
            for window in value:
                argv += [flag, window]
        else:
            argv += [flag, str(value)]
    return argv


def unchanged(plan, receipt):
    # Relative renderer arguments still use their declared paths. Hashing only a
    # symlink's old resolved target would miss a config switched to another file.
    receipt['input_bindings_after'] = {path: str(Path(path).resolve(strict=True))
                                       for path in receipt['input_bindings_before']}
    if receipt['input_bindings_before'] != receipt['input_bindings_after']:
        raise RuntimeError('input path changed during job (declared-to-resolved binding)')
    receipt['cwd_after'] = directory_identity(plan['cwd'])
    if receipt['cwd_before'] != receipt['cwd_after']:
        raise RuntimeError('cwd changed during job (resolved path and directory identity)')
    receipt['source_after'] = fingerprint(Path(plan['source']))
    receipt['executable_after'] = fingerprint(Path(receipt['executable']['path']))
    receipt['inputs_after'] = {path: fingerprint(Path(path)) for path in plan['inputs']}
    if receipt['source_before'] != receipt['source_after']:
        raise RuntimeError('source changed during job (content hash and fingerprint compared)')
    if receipt['audio_reference_before']:
        receipt['audio_reference_after'] = fingerprint(Path(plan['expected']['audio_copy_from']))
        if receipt['audio_reference_before'] != receipt['audio_reference_after']:
            raise RuntimeError('audio reference changed during job')
    if receipt['executable'] != receipt['executable_after']:
        raise RuntimeError('executable changed during job')
    if receipt['inputs_before'] != receipt['inputs_after']:
        raise RuntimeError('input changed during job')


def main():
    def interrupt(signum, _frame):
        raise KeyboardInterrupt(f'interrupted by signal {signum}')

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupt)
    work, owned, plan, receipt = args.work_dir.expanduser().absolute(), False, None, {'status': 'FAILED'}
    try:
        if os.path.lexists(work):
            raise ValueError('work-dir must be new; no automatic resume or overwrite')
        work = work.resolve()
        plan, receipt = prepare(args.plan.expanduser().resolve(strict=True), work)
        work.mkdir(mode=0o700)
        owned = True
        save(work / 'plan.json', plan)
        receipt['verification_argv'] = verification_argv(plan, work)
        if not args.execute:
            receipt['status'] = 'DRY_RUN'
        else:
            deadline = time.monotonic() + plan['timeout_s']
            unchanged(plan, receipt)
            receipt['status'], receipt['render'] = 'RENDERING', {}
            save(work / 'receipt.json', receipt)
            emit('render_started', work_dir=str(work))
            try:
                supervise(plan['resolved_argv'], work, 'render.log', deadline, receipt['render'],
                          progress=plan['progress_format'], stall=plan.get('stall_timeout_s'), limit=plan['log_limit_bytes'], cwd=plan['cwd'])
            finally:
                unchanged(plan, receipt)
            staging = Path(plan['staging'])
            if staging.is_symlink() or not staging.is_file() or staging.stat().st_size == 0:
                raise RuntimeError('staging must be a nonempty regular file, not a symlink')
            if staging.samefile(plan['source']) or (plan['expected'].get('audio_copy_from') and staging.samefile(plan['expected']['audio_copy_from'])):
                raise RuntimeError('staging aliases a protected input')
            receipt['verification'] = {}
            emit('verification_started')
            supervise(receipt['verification_argv'], work, 'verify.log', deadline, receipt['verification'], limit=plan['log_limit_bytes'])
            report = json.loads((work / 'verification.json').read_text())
            if report.get('technical_status') != 'passed':
                raise RuntimeError('full verification failed')
            unchanged(plan, receipt)
            verified = fingerprint(staging)
            if verified != report['files_after'][str(staging)]:
                raise RuntimeError('staging changed after verification')
            os.link(staging, plan['output'], follow_symlinks=False)
            receipt['output'] = fingerprint(Path(plan['output']))
            if receipt['output']['sha256'] != verified['sha256']:
                raise RuntimeError('delivered output hash mismatch')
            receipt['status'] = SUCCESS
            save(work / 'manifest.json', receipt)
        save(work / 'receipt.json', receipt)
        emit('result', status=receipt['status'], receipt=str(work / 'receipt.json'))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        receipt.update(status='FAILED', error=str(error) or 'interrupted', visual_review_required=True)
        if owned:
            try:
                unchanged(plan, receipt)
            except Exception as integrity_error:
                receipt['error'] += '; ' + str(integrity_error)
            save(work / 'receipt.json', receipt)
        emit('result', **receipt)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
