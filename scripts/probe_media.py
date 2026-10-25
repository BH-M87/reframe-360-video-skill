#!/usr/bin/env python3
"""Read-only bounded media inventory; JSON output must use a new path."""
import argparse
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess

MAX_META = 64 * 1024 * 1024

def unpack(fmt, data, offset=0):
    if offset < 0 or offset + struct.calcsize(fmt) > len(data):
        raise ValueError('truncated metadata field')
    return struct.unpack_from(fmt, data, offset)

def header(data, offset, end):
    size, kind = unpack('>I4s', data)
    head = 8
    if size == 1:
        size, = unpack('>Q', data, 8)
        head = 16
    if size == 0:
        size = end - offset
    if size < head or offset + size > end:
        raise ValueError('invalid box size or file boundary')
    return size, kind, head

def boxes(data):
    offset, count = 0, 0
    while offset < len(data):
        size, kind, head = header(data[offset:offset + 16], offset, len(data))
        count += 1
        if count > 100000:
            raise ValueError('metadata traversal limit exceeded')
        yield kind, memoryview(data)[offset + head:offset + size]
        offset += size

def children(data):
    result = {}
    for kind, value in boxes(data):
        if kind in result:
            raise ValueError('duplicate metadata box')
        result[kind] = value
    return result

def text(data):
    return bytes(data).rstrip(b'\0').decode('utf-8', 'replace')

def classify(tracks):
    candidates = {kind: [] for kind in ('video', 'audio', 'data', 'cover')}
    for t in tracks:
        evidence = t.setdefault('cover_evidence', [])
        if t['attached_pic']:
            evidence.append('ffprobe disposition.attached_pic=1')
        if t['codec'] in ('mjpg', 'jpeg', 'mjpeg') and t['sample_count'] == 1:
            evidence.append('single-sample JPEG stream; cover candidate only')
        t['cover_candidate'] = bool(evidence)
        t['role'] = 'cover' if evidence else {'vide': 'video', 'soun': 'audio',
            'meta': 'data', 'mdta': 'data', 'data': 'data', 'tmcd': 'timecode'}.get(t['handler_type'], 'unknown')
        if t['role'] == 'video' and t.get('timeline_status', 'valid') != 'valid':
            t['role'] = 'unknown'
        if t['codec'] == 'tmcd':
            t['role'] = 'timecode'
        if t['role'] in candidates:
            candidates[t['role']].append(t['index'])
    return candidates

def iso_probe(path, result):
    moov, top = None, []
    with path.open('rb') as source:
        end, offset = path.stat().st_size, 0
        while offset < end:
            source.seek(offset)
            size, kind, head = header(source.read(16), offset, end)
            top.append(text(kind))
            if len(top) > 100000:
                raise ValueError('top-level box limit exceeded')
            if kind == b'moov':
                if moov is not None or size - head > MAX_META:
                    raise ValueError('duplicate or oversized moov')
                source.seek(offset + head)
                moov = source.read(size - head)
                if len(moov) != size - head:
                    raise ValueError('truncated moov')
            offset += size
    if moov is None:
        if 'moof' in top:
            result['unsupported'].append('fragmented_mp4')
        raise ValueError('no supported ISO BMFF initialization moov')
    roots = list(boxes(moov))
    fragmented = 'moof' in top or any(k == b'mvex' for k, _ in roots)
    if fragmented:
        result['unsupported'].append('fragmented_mp4')
    tracks = []
    for kind, payload in roots:
        if kind != b'trak':
            continue
        tr = children(payload)
        tk = tr[b'tkhd']
        version = unpack('>B', tk)[0]
        if version not in (0, 1):
            raise ValueError('unsupported tkhd version')
        tid, = unpack('>I', tk, 20 if version else 12)
        md = children(tr[b'mdia'])
        hd, mh = md[b'hdlr'], md[b'mdhd']
        unpack('>24s', hd)
        version = unpack('>B', mh)[0]
        if version not in (0, 1):
            raise ValueError('unsupported mdhd version')
        scale, duration = unpack('>IQ' if version else '>II', mh, 20 if version else 12)
        unknown_duration = duration == (2 ** (64 if version else 32) - 1)
        table = children(children(md[b'minf'])[b'stbl'])
        stsd = table[b'stsd']
        count, = unpack('>I', stsd, 4)
        entries = list(boxes(stsd[8:]))
        if count != len(entries) or not entries:
            raise ValueError('invalid stsd entry count')
        codec, entry = entries[0]
        if count != 1:
            result['unsupported'].append('multiple_sample_descriptions_track_' + str(tid))
        handler = text(hd[8:12])
        width, height = unpack('>HH', entry, 24) if handler == 'vide' else (None, None)
        samples, fps, timing_valid = None, None, True
        if b'stz2' in table:
            result['unsupported'].append('stz2_track_' + str(tid))
        elif b'stsz' in table:
            fixed, samples = unpack('>II', table[b'stsz'], 4)
            if not fixed and len(table[b'stsz']) < 12 + samples * 4:
                raise ValueError('truncated stsz sample table')
        else:
            result['unsupported'].append('missing_stsz_track_' + str(tid))
        if b'stts' in table:
            timing = table[b'stts']
            n, = unpack('>I', timing, 4)
            if len(timing) != 8 + n * 8:
                raise ValueError('invalid stts table length')
            total, ticks = 0, 0
            for pos in range(8, len(timing), 8):
                num, delta = unpack('>II', timing, pos)
                timing_valid = timing_valid and (delta > 0 or num == 0)
                total += num
                ticks += num * delta
            if samples is not None and samples != total:
                raise ValueError('sample count disagrees with timing table')
            fps = total * scale / ticks if handler == 'vide' and ticks and scale else None
        if fragmented:
            samples, fps = None, None
        timeline = 'unknown_fragmented' if fragmented else ('unknown_empty' if samples == 0 else
            ('unknown' if samples is None or unknown_duration else ('valid' if scale and duration and fps and timing_valid else 'invalid')))
        if handler == 'vide' and timeline != 'valid':
            result['unsupported'].append('unusable_video_track_' + str(tid))
        tracks.append({'timeline_status': timeline if handler == 'vide' else 'not_evaluated', 'index': len(tracks), 'track_id': tid, 'handler_type': handler,
            'handler_name': text(hd[24:]), 'codec': text(codec), 'width': width,
            'height': height, 'duration_seconds': duration / scale if scale and not unknown_duration and not fragmented else None,
            'timescale': scale, 'sample_count': samples, 'avg_frame_rate': fps,
            'attached_pic': None})
    if not tracks:
        raise ValueError('no supported tracks')
    result.update(selected_probe='stdlib_iso_bmff', tracks=tracks, top_level_boxes=top,
        cover_detection_scope='track_samples_only', metadata_cover_status='unknown_not_inspected')

def numeric(value, integer=False):
    try:
        n = int(value, 0) if integer and isinstance(value, str) and value.startswith('0x') else (int(value) if integer else float(value))
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None

def ratio(value):
    try:
        a, b = str(value).split('/')
        return numeric(float(a) / float(b))
    except (ValueError, ZeroDivisionError):
        return None

def ffprobe(path, executable, timeout, result):
    info = result['tools']['ffprobe']
    info['bounded_probe'] = {'probesize_bytes': 8388608, 'analyzeduration_microseconds': 3000000, 'max_alloc_bytes': MAX_META}
    command = [executable, '-v', 'error', '-probesize', '8388608', '-analyzeduration',
        '3000000', '-max_alloc', str(MAX_META), '-protocol_whitelist', 'file,pipe',
        '-show_streams', '-show_format', '-of', 'json', '-i', str(path)]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if run.returncode:
            info.update(status='failed', returncode=run.returncode)
            return
        data = json.loads(run.stdout)
        tracks = []
        for stream in data.get('streams', []):
            time_base = stream.get('time_base', '').split('/')
            tracks.append({'index': stream['index'], 'track_id': numeric(stream.get('id'), True),
                'handler_type': {'video': 'vide', 'audio': 'soun', 'data': 'data'}.get(stream.get('codec_type'), 'unknown'),
                'handler_name': stream.get('tags', {}).get('handler_name'), 'codec': stream.get('codec_name'),
                'width': stream.get('width'), 'height': stream.get('height'),
                'duration_seconds': numeric(stream.get('duration')), 'timescale': numeric(time_base[-1], True) if len(time_base) == 2 and time_base[0] == '1' else None,
                'sample_count': numeric(stream.get('nb_frames'), True), 'avg_frame_rate': ratio(stream.get('avg_frame_rate')),
                'attached_pic': bool(stream['disposition']['attached_pic']) if 'attached_pic' in stream.get('disposition', {}) else None})
        if not tracks:
            raise ValueError('no streams')
        info['status'] = 'ok'
        result.update(selected_probe='ffprobe', tracks=tracks,
            cover_detection_scope='ffprobe_stream_dispositions', metadata_cover_status='unknown_not_inspected')
    except subprocess.TimeoutExpired:
        info['status'] = 'timeout'
    except (OSError, ValueError, KeyError, TypeError):
        info['status'] = 'failed'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='new JSON report path; existing paths are never overwritten')
    parser.add_argument('--ffprobe', help='ffprobe executable; otherwise discover from PATH')
    parser.add_argument('--timeout', type=float, default=8.0, help='ffprobe wall timeout in seconds (default: 8)')
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    if args.timeout <= 0 or args.timeout > 300:
        parser.error('--timeout must be in (0, 300] seconds')
    output = args.output.expanduser()
    if output.exists() or output.is_symlink() or output.resolve() == source:
        parser.error('--output must be a new path; existing files are never overwritten')
    executable = shutil.which(args.ffprobe or 'ffprobe')
    result = {'status': 'unprobeable', 'selected_probe': None, 'source': {'path': str(source), 'size_bytes': None},
        'tools': {'ffprobe': {'status': 'not_found' if not executable else 'available',
        'discovery': 'explicit' if args.ffprobe else 'PATH', 'path': executable, 'timeout_seconds': args.timeout}},
        'tracks': [], 'candidate_tracks': {}, 'unsupported': [], 'warnings': [], 'gyro_status': 'unknown_unverified'}
    result['tools']['stdlib_iso_bmff'] = {'status': 'not_run', 'max_moov_bytes': MAX_META}
    try:
        if not source.is_file():
            raise ValueError('source is not a regular file')
        result['source']['size_bytes'] = source.stat().st_size
        if executable:
            ffprobe(source, executable, args.timeout, result)
        if not result['tracks']:
            result['tools']['stdlib_iso_bmff']['status'] = 'failed'
            iso_probe(source, result)
            result['tools']['stdlib_iso_bmff']['status'] = 'ok'
        result['candidate_tracks'] = classify(result['tracks'])
        result['status'] = 'partial' if result['unsupported'] else 'ok'
    except (OSError, ValueError, KeyError, struct.error) as error:
        result['warnings'].append(str(error))
    encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    try:
        with output.open('x', encoding='utf-8') as report:
            report.write(encoded)
    except OSError as error:
        parser.exit(2, 'cannot exclusively create --output: ' + str(error.strerror) + '\n')
    print(encoded, end='')
    unusable_video = any(t.get('timeline_status') in ('invalid', 'unknown', 'unknown_empty', 'unknown_fragmented') for t in result['tracks'])
    return 2 if result['status'] == 'unprobeable' or (unusable_video and not result['candidate_tracks'].get('video')) else 0

if __name__ == '__main__':
    raise SystemExit(main())
