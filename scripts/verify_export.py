#!/usr/bin/env python3
"""Read-only full decode and export checks; visual inspection always remains required.

Requires PyAV and numpy; Pillow only for --contacts-dir. Install in your chosen
Python environment: python -m pip install av numpy Pillow. Contacts sample every
0.5s plus explicit privacy windows; they cannot prove absence of people or jitter.
Reports and contact directories must be new paths; media files are never written.
A passed status covers only reported checks. Color/audio metadata expectations and
faststart are optional; not_asserted and faststart.status identify unchecked scope.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import math
import struct
from pathlib import Path
import sys


def fingerprint(path):
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return {'path': str(path), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
            'device': stat.st_dev, 'inode': stat.st_ino, 'sha256': digest.hexdigest()}


def require_independent_media(args):
    for role, path in [('source', args.source), ('audio reference', args.audio_copy_from)]:
        if path is not None and (path == args.output or path.samefile(args.output)):
            raise ValueError(f'{role} and output must be distinct files; filesystem aliases are disallowed')


def audio_payload(av, path):
    digest, count = hashlib.sha256(), 0
    with av.open(str(path)) as container:
        if len(container.streams.audio) != 1:
            raise ValueError(f'{path}: audio identity needs exactly one audio stream')
        codec = container.streams.audio[0].codec_context.name
        for packet in container.demux(container.streams.audio[0]):
            if packet.size:
                data = bytes(packet)
                digest.update(len(data).to_bytes(8, 'big'))
                digest.update(data)
                count += 1
    return {'codec': codec, 'packets': count, 'size_prefixed_packet_payload_sha256': digest.hexdigest()}


def mp4_boxes(path):
    total, result = path.stat().st_size, []
    with path.open('rb') as handle:
        while handle.tell() < total:
            offset, header = handle.tell(), handle.read(8)
            if len(header) != 8:
                raise ValueError(f'Truncated MP4 atom header at {offset}')
            size, kind = struct.unpack('>I4s', header)
            header_size = 8
            if size == 1:
                extra = handle.read(8)
                if len(extra) != 8:
                    raise ValueError(f'Truncated MP4 extended size at {offset}')
                size, header_size = struct.unpack('>Q', extra)[0], 16
            elif size == 0:
                size = total - offset
            if size < header_size or offset + size > total:
                raise ValueError(f'MP4 atom bounds invalid at {offset}: size {size}')
            result.append({'type': kind.decode('ascii', errors='replace'), 'offset': offset, 'size': size})
            if len(result) > 100000:
                raise ValueError('Too many MP4 top-level atoms')
            handle.seek(offset + size)
    return result


class AudioLevels:
    def __init__(self, np, limit):
        self.np, self.limit = np, limit
        self.samples = self.nonfinite = self.overrange = self.finite_count = 0
        self.peak = self.ssq = 0.0

    def add(self, frame):
        np = self.np
        raw = frame.to_ndarray()
        data = raw.astype(np.float64).reshape(-1)
        if raw.dtype.kind in 'iu':
            scale = float(2 ** (np.iinfo(raw.dtype).bits - 1))
            data = (data - scale) / scale if raw.dtype.kind == 'u' else data / scale
        finite = np.isfinite(data)
        self.samples += data.size
        self.nonfinite += int((~finite).sum())
        values = data[finite]
        self.finite_count += values.size
        self.overrange += int((np.abs(values) > self.limit).sum())
        if values.size:
            peak = max(self.peak, float(np.abs(values).max()))
            if peak:
                self.ssq = self.ssq * (self.peak / peak)**2 + float(np.square(values / peak).sum())
                self.peak = peak

    def report(self):
        return {'samples': int(self.samples), 'nonfinite_samples': self.nonfinite,
                'overrange_samples': self.overrange, 'peak': self.peak, 'peak_limit': self.limit,
                'rms': self.peak * math.sqrt(self.ssq / self.finite_count) if self.finite_count else None,
                'normalization': 'signed PCM / 2^(bits-1); unsigned PCM centered; float amplitude unchanged'}


class Contacts:
    def __init__(self, path, targets):
        from PIL import Image, ImageDraw
        self.Image, self.ImageDraw = Image, ImageDraw
        self.path, self.targets, self.index = path, targets, 0
        self.cells, self.pages, self.samples = [], [], []
        path.mkdir(parents=True, exist_ok=False)

    def add(self, frame, seconds, index):
        while self.index < len(self.targets) and seconds + 1e-9 >= self.targets[self.index]:
            cell = self.Image.new('RGB', (320, 204), '#171717')
            image = frame.to_image()
            image.thumbnail((320, 180))
            cell.paste(image, ((320-image.width)//2, 0))
            self.ImageDraw.Draw(cell).text((5, 183), f'{seconds:.3f}s | frame {index}', fill='white')
            self.samples.append({'target_s': self.targets[self.index], 'actual_s': seconds, 'frame_index': index})
            self.cells.append(cell)
            self.index += 1
            if len(self.cells) == 16:
                self.flush()

    def flush(self):
        if self.cells:
            page = self.Image.new('RGB', (1280, math.ceil(len(self.cells)/4)*204), '#171717')
            for index, cell in enumerate(self.cells):
                page.paste(cell, ((index % 4)*320, (index // 4)*204))
            path = self.path / f'contact_{len(self.pages)+1:03d}.jpg'
            page.save(path, quality=90)
            self.pages.append(str(path))
            self.cells = []


def verify(args, av, np, targets, report):
    require_independent_media(args)
    checks, decoded = {}, {'video_frames': 0, 'audio_frames': 0, 'decode_errors': [], 'decode_error_count': 0}
    report.update({'technical_status': 'failed', 'visual_review_required': True, 'checks': checks, 'decoded': decoded,
              'expected': {'frames': args.expected_frames, 'fps': str(args.rate), 'width': args.width, 'height': args.height},
              'visual_limitations': 'Dark scenes and intentional borders can trigger heuristics. Technical checks and sampled contacts prove neither privacy nor stabilization/jello quality.'})

    def check(name, passed, **detail):
        checks[name] = {'passed': bool(passed), **detail}

    inputs = list(dict.fromkeys(p for p in (args.source, args.output, args.audio_copy_from) if p))
    before = {str(p): fingerprint(p) for p in inputs}
    report['files_before'] = before
    report['technical_scope'] = 'Passed covers checks only; not_asserted metrics and unsampled visual content remain unverified.'
    optional = {'color_primaries': args.color_primaries, 'color_trc': args.color_trc,
                'colorspace': args.colorspace, 'color_range': args.color_range,
                'audio_sample_rate': args.sample_rate, 'audio_channels': args.channels}
    report['not_asserted'] = [key for key, value in optional.items() if value is None]
    report['metadata_assertions'] = {key: {'status': 'recorded_not_asserted' if value is None else 'expected_value_requested',
                                            'expected': value} for key, value in optional.items()}
    report['faststart'] = {'status': 'not_checked'}
    if args.require_faststart:
        boxes = mp4_boxes(args.output)
        moov = [box['offset'] for box in boxes if box['type'] == 'moov']
        mdat = [box['offset'] for box in boxes if box['type'] == 'mdat']
        report['faststart'] = {'status': 'checked', 'top_level_boxes': boxes}
        check('faststart', bool(moov and mdat and min(moov) < min(mdat)))
    else:
        report['not_asserted'].append('faststart')
    levels = AudioLevels(np, args.audio_peak_limit)
    sheets = Contacts(args.contacts_dir, targets) if args.contacts_dir else None
    times = {'video': [], 'audio': []}
    missing = {'video': 0, 'audio': 0}
    audio_end, video_end, mismatched_shapes, previous_audio_rate = None, None, 0, None
    audio_continuity = {'gap_count': 0, 'overlap_count': 0, 'max_gap_s': 0.0,
                        'max_overlap_s': 0.0, 'invalid_chunks': 0, 'max_tolerance_s': .002}
    heuristics = {'all_black_count': 0, 'border_candidate_count': 0, 'all_black_times_s': [], 'border_candidate_times_s': []}
    with av.open(str(args.output)) as container:
        check('container_format', args.container_format in container.format.name.split(','), actual=container.format.name, expected=args.container_format)
        check('single_video_stream', len(container.streams.video) == 1, actual=len(container.streams.video))
        check('single_audio_stream', len(container.streams.audio) <= 1, actual=len(container.streams.audio))
        video = container.streams.video[0] if container.streams.video else None
        audio = container.streams.audio[0] if container.streams.audio else None
        check('audio_present', bool(audio) or args.allow_no_audio, allowed_absent=args.allow_no_audio)
        report['audio_status'] = 'present' if audio else 'absent_explicitly_allowed' if args.allow_no_audio else 'absent_required'
        if video:
            vc = video.codec_context
            metadata = {'video_codec': vc.name, 'pixel_format': vc.format.name if vc.format else None,
                        'width': video.width, 'height': video.height, 'fps': str(video.average_rate)}
            metadata['sample_aspect_ratio'] = str(video.sample_aspect_ratio) if video.sample_aspect_ratio is not None else None
            metadata['color_tags'] = {key: int(getattr(vc, key)) for key in ('color_primaries', 'color_trc', 'colorspace', 'color_range')}
            report['metadata'] = metadata
            check('sample_aspect_ratio', video.sample_aspect_ratio == args.sar, actual=metadata['sample_aspect_ratio'], expected=str(args.sar))
            for key, actual in metadata['color_tags'].items():
                if optional[key] is not None:
                    check(key, actual == optional[key], actual=actual, expected=optional[key])
            check('dimensions', video.width == args.width and video.height == args.height, actual=[video.width, video.height])
            check('video_codec', vc.name == args.video_codec, actual=vc.name, expected=args.video_codec)
            check('pixel_format', metadata['pixel_format'] == args.pixel_format, actual=metadata['pixel_format'], expected=args.pixel_format)
            check('fps', abs(float(video.average_rate or 0)-float(args.rate)) <= 1e-5, actual=str(video.average_rate))
        if audio:
            ac = audio.codec_context
            audio_meta = {'audio_sample_rate': ac.sample_rate, 'audio_channels': len(ac.layout.channels), 'audio_layout': ac.layout.name if hasattr(ac.layout, 'name') else None}
            report.setdefault('metadata', {}).update(audio_meta)
            for key in ('audio_sample_rate', 'audio_channels'):
                if optional[key] is not None:
                    check(key, audio_meta[key] == optional[key], actual=audio_meta[key], expected=optional[key])
            check('audio_codec', audio.codec_context.name == args.audio_codec, actual=audio.codec_context.name, expected=args.audio_codec)
        else:
            report.setdefault('metadata', {}).update(audio_sample_rate=None, audio_channels=None, audio_layout=None)
            for key in ('audio_sample_rate', 'audio_channels'):
                if optional[key] is not None:
                    check(key, False, actual=None, expected=optional[key], reason='audio stream absent')
            report['not_asserted'] += ['audio_codec', 'audio_pts', 'audio_finite', 'audio_peak', 'audio_chunk_continuity', 'av_endpoints']
        streams = [stream for stream in (video, audio) if stream is not None]
        for packet in container.demux(streams):
            try:
                frames = packet.decode()
            except Exception as error:
                decoded['decode_error_count'] += 1
                if len(decoded['decode_errors']) < 20:
                    decoded['decode_errors'].append(str(error))
                continue
            for frame in frames:
                kind = 'video' if isinstance(frame, av.VideoFrame) else 'audio'
                decoded[kind + '_frames'] += 1
                if kind == 'audio':
                    levels.add(frame)
                seconds = float(frame.pts * frame.time_base) if frame.pts is not None and frame.time_base is not None else None
                if seconds is None or not math.isfinite(seconds):
                    missing[kind] += 1
                    continue
                times[kind].append(seconds)
                if kind == 'audio':
                    if frame.sample_rate <= 0 or frame.samples <= 0:
                        audio_continuity['invalid_chunks'] += 1
                        continue
                    if audio_end is not None:
                        delta = seconds - audio_end
                        tolerance = max(2/frame.sample_rate, 2/previous_audio_rate, .002)
                        audio_continuity['max_tolerance_s'] = max(audio_continuity['max_tolerance_s'], tolerance)
                        audio_continuity['max_gap_s'] = max(audio_continuity['max_gap_s'], delta)
                        audio_continuity['max_overlap_s'] = max(audio_continuity['max_overlap_s'], -delta)
                        audio_continuity['gap_count'] += delta > tolerance + 1e-9
                        audio_continuity['overlap_count'] += delta < -tolerance - 1e-9
                    audio_end = seconds + frame.samples / frame.sample_rate
                    previous_audio_rate = frame.sample_rate
                    continue
                video_end = seconds + 1/float(args.rate)
                mismatched_shapes += frame.width != args.width or frame.height != args.height
                pixels = frame.reformat(width=160, height=90, format='gray').to_ndarray()
                dark = pixels <= 16
                all_black = float(pixels.mean()) < 5 and float(dark.mean()) > .995
                borders = [dark[:4, :], dark[-4:, :], dark[:, :8], dark[:, -8:]]
                border = float(pixels[5:-5, 9:-9].mean()) > 30 and any(float(b.mean()) > .99 for b in borders)
                for label, found in [('all_black', all_black), ('border_candidate', border)]:
                    if found:
                        heuristics[label + '_count'] += 1
                        if len(heuristics[label + '_times_s']) < 100:
                            heuristics[label + '_times_s'].append(seconds)
                if sheets:
                    sheets.add(frame, seconds, decoded['video_frames']-1)
    check('decode_errors', decoded['decode_error_count'] == 0)
    check('frame_count', decoded['video_frames'] == args.expected_frames, actual=decoded['video_frames'], expected=args.expected_frames)
    check('all_frame_dimensions', mismatched_shapes == 0, mismatches=mismatched_shapes)
    for kind in ('video', 'audio'):
        if kind == 'audio' and not times[kind] and args.allow_no_audio and report['audio_status'].startswith('absent'):
            continue
        check(kind + '_pts', bool(times[kind]) and missing[kind] == 0 and all(b > a for a, b in zip(times[kind], times[kind][1:])), missing=missing[kind])
    video_times = times['video']
    check('video_cadence', bool(video_times) and all(abs((b-a)-1/float(args.rate)) <= .001 for a, b in zip(video_times, video_times[1:])))
    check('video_endpoints', bool(video_times) and abs(video_times[0]) <= 1/float(args.rate) and abs((video_end or 0)-args.expected_frames/float(args.rate)) <= 1/float(args.rate), start_s=video_times[0] if video_times else None, end_s=video_end)
    if decoded['audio_frames']:
        report['decoded_audio'] = levels.report()
        check('audio_finite', levels.samples > 0 and levels.nonfinite == 0, nonfinite_samples=levels.nonfinite)
        check('audio_peak', levels.overrange == 0, actual=levels.peak, expected_max=args.audio_peak_limit)
    if times['audio']:
        check('audio_chunk_continuity', not any(audio_continuity[key] for key in ('gap_count', 'overlap_count', 'invalid_chunks')),
              **audio_continuity, tolerance_rule='max(2/current_sample_rate, 2/previous_sample_rate, .002s)')
        check('av_endpoints', bool(video_times) and abs(times['audio'][0]-video_times[0]) <= args.av_tolerance and abs((audio_end or 0)-(video_end or 0)) <= args.av_tolerance,
              audio_start_s=times['audio'][0], audio_end_s=audio_end, tolerance_s=args.av_tolerance)
    report['black_frame_border_heuristics'] = heuristics
    if args.audio_copy_from:
        copied, reference = audio_payload(av, args.output), audio_payload(av, args.audio_copy_from)
        check('audio_payload_identity', copied == reference and copied['packets'] > 0, output=copied, reference=reference)
    if sheets:
        sheets.flush()
        report['contact_sheets'] = {'pages': sheets.pages, 'samples': sheets.samples, 'privacy_windows': args.privacy_window, 'unreached_targets': targets[sheets.index:]}
        check('contact_targets_reached', sheets.index == len(targets))
    after = {str(p): fingerprint(p) for p in inputs}
    report['files_after'] = after
    check('inputs_unchanged', before == after)
    report['technical_status'] = 'passed' if all(item['passed'] for item in checks.values()) else 'failed'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'output', 'report'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--expected-frames', type=int, required=True)
    parser.add_argument('--fps', required=True, help='Positive integer, decimal or rational such as 30000/1001')
    parser.add_argument('--width', type=int, required=True)
    parser.add_argument('--height', type=int, required=True)
    parser.add_argument('--container-format', default='mp4', help='Expected demuxer format name, default mp4')
    parser.add_argument('--video-codec', default='h264')
    parser.add_argument('--audio-codec', default='aac')
    parser.add_argument('--pixel-format', default='yuv420p')
    parser.add_argument('--sar', default='1', help='Required declared sample aspect ratio; default 1')
    for color in ('color-primaries', 'color-trc', 'colorspace', 'color-range'):
        parser.add_argument('--' + color, type=int, help='Optional expected integer AV color enum')
    parser.add_argument('--sample-rate', type=int, help='Optional expected audio sample rate')
    parser.add_argument('--channels', type=int, help='Optional expected audio channel count')
    parser.add_argument('--audio-peak-limit', type=float, default=1.0, help='Maximum normalized decoded sample amplitude; default 1.0')
    parser.add_argument('--require-faststart', action='store_true', help='Require top-level MP4 moov before mdat')
    parser.add_argument('--av-tolerance', type=float, default=.15, help='Allowed A/V endpoint offset in seconds; default .15')
    parser.add_argument('--allow-no-audio', action='store_true', help='Explicit silent/diagnostic export allowance')
    parser.add_argument('--audio-copy-from', type=Path, help='Compare size-prefixed compressed audio packet payloads')
    parser.add_argument('--contacts-dir', type=Path)
    parser.add_argument('--privacy-window', action='append', default=[], metavar='START:END:STEP', help='Output timeline seconds, inclusive; repeatable, requires contacts')
    args = parser.parse_args()
    try:
        args.rate, args.sar = Fraction(args.fps), Fraction(args.sar)
        if args.sar <= 0 or not math.isfinite(args.audio_peak_limit) or args.audio_peak_limit <= 0 or any(value is not None and value <= 0 for value in (args.sample_rate, args.channels)):
            raise ValueError('SAR, audio peak limit, sample rate and channels must be positive; peak limit finite')
        if args.rate <= 0 or min(args.expected_frames, args.width, args.height) <= 0 or not math.isfinite(args.av_tolerance) or args.av_tolerance < 0:
            raise ValueError('Frame count, dimensions and fps must be positive; A/V tolerance finite and nonnegative')
        for name in ('source', 'output', 'report', 'audio_copy_from', 'contacts_dir'):
            if getattr(args, name):
                setattr(args, name, getattr(args, name).expanduser().resolve())
        require_independent_media(args)
        inputs = [p for p in (args.source, args.output, args.audio_copy_from) if p]
        if args.report in inputs or args.report.exists():
            raise ValueError('Report must be a new path distinct from every input media file')
        if args.contacts_dir and (args.contacts_dir.exists() or args.contacts_dir in inputs or args.contacts_dir == args.report or args.contacts_dir in args.report.parents):
            raise ValueError('Contacts directory must be a new path separate from inputs and report')
        last = (args.expected_frames-1)/float(args.rate)
        if last/.5 > 100000:
            raise ValueError('Too many contact targets; split the export')
        targets = [index*.5 for index in range(math.floor(last/.5)+1)] + [last]
        if args.privacy_window and not args.contacts_dir:
            raise ValueError('--privacy-window requires --contacts-dir')
        for window in args.privacy_window:
            start, end, step = [float(value) for value in window.split(':')]
            if not all(math.isfinite(value) for value in (start, end, step)) or start < 0 or end < start or end > last or step <= 0 or (end-start)/step > 100000:
                raise ValueError('Privacy window must satisfy 0 <= START <= END <= last frame and STEP > 0')
            targets += [start + index*step for index in range(math.floor((end-start)/step)+1)]
        targets = sorted(set(round(target, 9) for target in targets))
        try:
            import av
            import numpy as np
            if args.contacts_dir:
                import PIL
        except ImportError as error:
            raise ValueError(f'Missing optional dependency {error.name}; install in your active environment: python -m pip install av numpy Pillow') from error
        report = {'technical_status': 'failed', 'visual_review_required': True}
        try:
            report = verify(args, av, np, targets, report)
        except Exception as error:
            report['error'] = str(error)
            report.setdefault('checks', {})['verification_completed'] = {'passed': False}
            try:
                after = {str(p): fingerprint(p) for p in dict.fromkeys(inputs)}
                report['files_after'] = after
                report['checks']['inputs_unchanged'] = {'passed': report.get('files_before') == after}
            except OSError as integrity_error:
                report['checks']['inputs_unchanged'] = {'passed': False, 'error': str(integrity_error)}
        with args.report.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2, allow_nan=False)
            handle.write('\n')
        print(json.dumps({'technical_status': report['technical_status'], 'visual_review_required': True, 'report': str(args.report)}))
        return 0 if report['technical_status'] == 'passed' else 1
    except Exception as error:
        print(f'verify_export: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
