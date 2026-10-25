#!/usr/bin/env python3
"""Compare one encoded video stream before/after an audio-only or container edit.

Exit 0: exact_video_equivalent within the reported video-stream contract;
1: different; 2: inconclusive or an input/report error. This does not grant
visual/privacy acceptance: it only permits considering reuse of already-bound
video-specific evidence. Audio, container and delivery checks must run again.
Requires PyAV after argument parsing. Pre-probe unfamiliar media and use a job
supervisor for a wall timeout; this reader is not an OSV discovery tool.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from fractions import Fraction
import hashlib
from itertools import zip_longest
import json
import os
from pathlib import Path
import sys
import tempfile


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--before', type=Path, required=True)
    p.add_argument('--after', type=Path, required=True)
    p.add_argument('--report', type=Path, required=True, help='New JSON report; no overwrite or aliases')
    p.add_argument('--video-stream', type=int, default=0, help='Zero-based video-stream ordinal, not absolute track index')
    return p


def fraction_text(value):
    if value is None:
        return None
    value = Fraction(value)
    return f'{value.numerator}/{value.denominator}'


def fingerprint(path):
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    if not resolved.is_file():
        raise ValueError(f'Input must be a regular file: {path}')
    digest = hashlib.sha256()
    with resolved.open('rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(block)
    end = resolved.stat()
    identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    if identity(stat) != identity(end) or path.resolve(strict=True) != resolved:
        raise ValueError(f'Input changed while hashing: {path}')
    return {'declared_path': str(path.absolute()), 'path': str(resolved), 'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns, 'device': stat.st_dev, 'inode': stat.st_ino,
            'sha256': digest.hexdigest()}


def side_data_digest(values):
    result = []
    for value in values:
        kind = getattr(value, 'data_type', None)
        if kind is None:
            kind = getattr(getattr(value, 'type', None), 'name', None)
        if kind is None:
            raise ValueError('Unidentified video side data cannot be compared safely')
        payload = bytes(value)
        result.append({'type': str(kind), 'size': len(payload),
                       'sha256': hashlib.sha256(payload).hexdigest()})
    return sorted(result, key=lambda item: (item['type'], item['sha256'], item['size']))


def stream_metadata(av, path, ordinal):
    with av.open(str(path)) as container:
        if ordinal >= len(container.streams.video):
            raise ValueError(f'Video stream {ordinal} does not exist in {path}')
        stream = container.streams.video[ordinal]
        cc = stream.codec_context
        color_fields = [('primaries', 'color_primaries'), ('trc', 'color_trc'),
                        ('space', 'colorspace'), ('range', 'color_range')]
        # Decoder initialization can replace container declarations with SPS
        # values. Preserve both, including a conflicting container-only edit.
        header = {'color': {name: int(getattr(cc, attr)) for name, attr in color_fields},
                  'sar': fraction_text(stream.sample_aspect_ratio),
                  'codec_sar': fraction_text(cc.sample_aspect_ratio),
                  'width': stream.width, 'height': stream.height,
                  'extradata_sha256': hashlib.sha256(cc.extradata or b'').hexdigest()}
        # A first-frame metadata inspection also exposes display matrices which
        # some PyAV versions do not expose directly on AVStream. No pixel arrays
        # are exported, and this is not a full decode/visual validation.
        frame = next(container.decode(stream), None)
        if frame is None:
            raise ValueError(f'Selected stream has no decodable frame: {path}')
        administrative = {'creation_time', 'handler_name', 'vendor_id', 'encoder', 'language',
                          'DURATION', 'BPS', 'NUMBER_OF_FRAMES', 'NUMBER_OF_BYTES',
                          '_STATISTICS_WRITING_APP', '_STATISTICS_WRITING_DATE_UTC', '_STATISTICS_TAGS'}
        metadata = {
            'codec': cc.name, 'profile': cc.profile,
            'extradata_sha256': hashlib.sha256(cc.extradata or b'').hexdigest(),
            'extradata_size': len(cc.extradata or b''),
            'width': stream.width, 'height': stream.height,
            'pixel_format': cc.format.name if cc.format else None,
            'sar': fraction_text(stream.sample_aspect_ratio),
            'codec_sar': fraction_text(cc.sample_aspect_ratio),
            'color': {name: int(getattr(cc, attr)) for name, attr in color_fields},
            'container_codec_parameters': header,
            'display_metadata': {k: v for k, v in stream.metadata.items() if k not in administrative},
            'first_frame_side_data': side_data_digest(frame.side_data),
        }
        if not metadata['codec'] or not metadata['pixel_format'] or min(stream.width, stream.height) <= 0:
            raise ValueError('Incomplete video codec/dimension metadata')
        return metadata, fraction_text(stream.time_base)


def metadata_differences(before, after):
    return [key for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]


def packet_timing(packet):
    for key in ('pts', 'dts', 'duration', 'time_base'):
        if getattr(packet, key, None) is None:
            return None, f'missing {key}'
    base = Fraction(packet.time_base)
    if base <= 0 or packet.duration <= 0:
        return None, 'nonpositive time_base or duration'
    return tuple(fraction_text(Fraction(getattr(packet, key)) * base)
                 for key in ('pts', 'dts', 'duration')), None


def selected_packets(container, ordinal):
    for packet in container.demux(container.streams.video[ordinal]):
        if packet.size:
            yield packet


def compare(av, before, after, ordinal):
    metadata_a, tb_a = stream_metadata(av, before, ordinal)
    metadata_b, tb_b = stream_metadata(av, after, ordinal)
    summaries = [{'metadata': metadata_a, 'stream_time_base': tb_a},
                 {'metadata': metadata_b, 'stream_time_base': tb_b}]
    digests = [hashlib.sha256(), hashlib.sha256()]
    timing_digests = [hashlib.sha256(), hashlib.sha256()]
    counts, sizes, unknowns, first_differences = [0, 0], [0, 0], [], {}
    equality = {'packet_payloads_equal': True, 'packet_timing_equal': True,
                'packet_side_data_equal': True, 'packet_counts_equal': True}
    sentinel = object()
    with ExitStack() as stack:
        containers = [stack.enter_context(av.open(str(path))) for path in (before, after)]
        packets = [selected_packets(container, ordinal) for container in containers]
        for index, pair in enumerate(zip_longest(*packets, fillvalue=sentinel)):
            payloads, timings, sides = [], [], []
            for side, packet in enumerate(pair):
                if packet is sentinel:
                    payloads.append(None); timings.append(None); sides.append(None)
                    continue
                data = bytes(packet)
                digests[side].update(len(data).to_bytes(8, 'big'))
                digests[side].update(data)
                counts[side] += 1
                sizes[side] += len(data)
                timing, reason = packet_timing(packet)
                if reason and len(unknowns) < 20:
                    unknowns.append({'side': 'before' if side == 0 else 'after',
                                     'packet_index': index, 'reason': reason})
                timing_digests[side].update((json.dumps(timing, separators=(',', ':')) + '\n').encode())
                if not hasattr(packet, 'iter_sidedata'):
                    raise ValueError('PyAV packet side-data API unavailable; upgrade PyAV for exact comparison')
                payloads.append(data); timings.append(timing)
                sides.append(side_data_digest(packet.iter_sidedata()))
            for key, values in [('packet_payloads_equal', payloads), ('packet_side_data_equal', sides)]:
                if values[0] != values[1]:
                    equality[key] = False
                    first_differences.setdefault(key, index)
            if all(t is not None for t in timings) and timings[0] != timings[1]:
                equality['packet_timing_equal'] = False
                first_differences.setdefault('packet_timing_equal', index)
            if sentinel in pair:
                equality['packet_counts_equal'] = False
                first_differences.setdefault('packet_counts_equal', index)
    differences = metadata_differences(metadata_a, metadata_b)
    equality['metadata_equal'] = not differences
    if not all(counts):
        unknowns.append({'reason': 'no nonempty video packets'})
    for side in range(2):
        summaries[side].update(packet_count=counts[side], payload_bytes=sizes[side],
                               size_prefixed_payload_sha256=digests[side].hexdigest(),
                               normalized_timing_sha256=timing_digests[side].hexdigest())
    status = 'different' if not all(equality.values()) else ('inconclusive' if unknowns else 'exact_video_equivalent')
    if unknowns and equality['packet_timing_equal']:
        equality['packet_timing_equal'] = None
    return {'status': status, 'before': summaries[0], 'after': summaries[1], 'checks': equality,
            'metadata_differences': differences, 'first_differing_packet': first_differences,
            'unknown_timing_examples': unknowns}


def new_report_path(report, inputs):
    if os.path.lexists(report):
        raise ValueError(f'Report path already exists: {report}')
    if report.resolve() in [path.resolve() for path in inputs]:
        raise ValueError('Report must not alias an input')
    if not report.parent.is_dir():
        raise ValueError('Report parent directory must exist')


def publish_report(path, report):
    stage = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', delete=False) as handle:
            stage = Path(handle.name)
            json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        os.link(stage, path)
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


def main(argv=None):
    args = parser().parse_args(argv)
    paths = {'before': args.before.expanduser().absolute(), 'after': args.after.expanduser().absolute()}
    destination = args.report.expanduser().absolute()
    try:
        new_report_path(destination, paths.values())
        if args.video_stream < 0:
            raise ValueError('video-stream must be a nonnegative ordinal')
    except (OSError, ValueError) as exc:
        print(f'compare_video_streams: {exc}', file=sys.stderr)
        return 2
    report = {'schema_version': 1, 'status': 'inconclusive', 'video_stream_ordinal': args.video_stream,
              'audio_and_container_reverification_required': True, 'visual_acceptance_granted': False,
              'scope': 'Selected encoded video packet payloads, codec/presentation metadata and exact rational packet timing. Prior video-specific evidence must be bound to the before file/stream; no new privacy or visual approval is granted.',
              'decode_scope': 'First decoded frame metadata inspected for display side data; full media decoding and visual inspection are separate.',
              'errors': []}
    try:
        report['files_before'] = {name: fingerprint(path) for name, path in paths.items()}
        import av
        report.update(compare(av, paths['before'], paths['after'], args.video_stream))
        report['files_after'] = {name: fingerprint(path) for name, path in paths.items()}
        unchanged = report['files_before'] == report['files_after']
        report['checks']['inputs_unchanged'] = unchanged
        if not unchanged:
            report['status'] = 'inconclusive'
            report['errors'].append('Input fingerprint or declared-path binding changed during comparison')
    except Exception as exc:
        report['status'] = 'inconclusive'
        report['errors'].append(f'{type(exc).__name__}: {exc}')
    try:
        publish_report(destination, report)
    except (OSError, ValueError) as exc:
        print(f'compare_video_streams: refusing report publication: {exc}', file=sys.stderr)
        return 2
    print(json.dumps({'status': report['status'], 'report': str(destination)}))
    return {'exact_video_equivalent': 0, 'different': 1, 'inconclusive': 2}[report['status']]


if __name__ == '__main__':
    raise SystemExit(main())
