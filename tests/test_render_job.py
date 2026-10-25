import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/run_render_job.py'


class RenderJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='render-job-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'original test source')
        self.output = self.root / 'output.mp4'
        self.work = self.root / 'work'
        self.plan = {'source': str(self.source), 'output': str(self.output),
                     'argv': [sys.executable, '-c', 'pass', '{output}'],
                     'expected': {'frames': 48, 'fps': '24', 'width': 320, 'height': 180},
                     'timeout_s': 10, 'progress_format': 'none'}

    def command(self, *flags):
        path = self.root / 'input-plan.json'
        path.write_text(json.dumps(self.plan))
        return [sys.executable, str(SCRIPT), '--plan', str(path), '--work-dir', str(self.work), *flags]

    def run_job(self, *flags):
        result = subprocess.run(self.command(*flags), capture_output=True, text=True, timeout=20)
        receipt = json.loads((self.work / 'receipt.json').read_text()) if (self.work / 'receipt.json').exists() else {}
        return result, receipt

    def python_job(self, code):
        self.plan['argv'] = [sys.executable, '-c', code, '{output}', '{source}']

    def test_default_dry_run_never_starts_command(self):
        marker = self.root / 'marker'
        self.python_job(f'from pathlib import Path; Path({str(marker)!r}).touch()')
        result, receipt = self.run_job()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt['status'], 'DRY_RUN')
        self.assertFalse(marker.exists())
        self.assertFalse(self.output.exists())
        saved = json.loads((self.work / 'plan.json').read_text())
        self.assertEqual(saved['resolved_argv'][3], str(self.work.resolve() / 'staging.mp4'))
        self.assertEqual(receipt['source_before']['sha256'], hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_exit_zero_with_missing_or_garbage_output_fails(self):
        for code in ('pass', 'import sys; open(sys.argv[1], "wb").write(b"garbage")'):
            with self.subTest(code=code):
                self.work = self.root / ('work-garbage' if 'garbage' in code else 'work-missing')
                self.python_job(code)
                result, receipt = self.run_job('--execute')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(receipt['status'], 'FAILED')
                self.assertFalse(self.output.exists())
                self.assertTrue((self.work / 'render.log').exists())

    def test_nonzero_exit_keeps_staging_and_log(self):
        self.python_job('import sys; open(sys.argv[1], "wb").write(b"partial"); print("render failed"); sys.exit(7)')
        result, receipt = self.run_job('--execute')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(receipt['render']['returncode'], 7)
        self.assertEqual((self.work / 'staging.mp4').read_bytes(), b'partial')
        self.assertIn('render failed', (self.work / 'render.log').read_text())

    def test_timeout_and_stall_ignore_log_noise(self):
        for mode in ('none', 'ffmpeg'):
            with self.subTest(mode=mode):
                self.work = self.root / ('work-' + mode)
                self.plan.update(progress_format=mode, timeout_s=.7 if mode == 'none' else 5)
                if mode == 'ffmpeg':
                    self.plan['stall_timeout_s'] = .4
                self.python_job('import time\nwhile True:\n print("noise", flush=True); print("frame=0", flush=True); time.sleep(.05)')
                started = time.monotonic()
                result, receipt = self.run_job('--execute')
                self.assertNotEqual(result.returncode, 0)
                self.assertLess(time.monotonic() - started, 3)
                self.assertIn('stall' if mode == 'ffmpeg' else 'timeout', receipt['error'].lower())

    def test_source_hash_detects_same_size_mtime_preserved_rewrite(self):
        self.python_job('import os,sys\np=sys.argv[2]; st=os.stat(p); open(p,"wb").write(b"x"*st.st_size); os.utime(p, ns=(st.st_atime_ns,st.st_mtime_ns))')
        result, receipt = self.run_job('--execute')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(receipt['source_before']['size'], receipt['source_after']['size'])
        self.assertEqual(receipt['source_before']['mtime_ns'], receipt['source_after']['mtime_ns'])
        self.assertNotEqual(receipt['source_before']['sha256'], receipt['source_after']['sha256'])
        self.assertIn('source changed', receipt['error'].lower())

    def test_existing_output_and_filesystem_aliases_never_overwritten(self):
        existing = self.root / 'existing.mp4'
        existing.write_bytes(b'existing output')
        hardlink = self.root / 'hardlink.mp4'
        os.link(self.source, hardlink)
        symlink = self.root / 'symlink.mp4'
        symlink.symlink_to(self.source)
        dangling = self.root / 'dangling.mp4'
        dangling.symlink_to(self.root / 'future.mp4')
        for index, output in enumerate((existing, self.source, hardlink, symlink, dangling)):
            with self.subTest(output=output):
                self.work = self.root / f'alias-work-{index}'
                self.plan['output'] = str(output)
                result, _ = self.run_job('--execute')
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(existing.read_bytes(), b'existing output')
        self.assertEqual(self.source.read_bytes(), b'original test source')
        self.assertFalse((self.root / 'future.mp4').exists())

    def test_rejects_existing_workdir_and_invalid_plans(self):
        self.work.mkdir()
        result, _ = self.run_job()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.work.iterdir()), [])
        for index, patch in enumerate(({'timeout_s': 0}, {'timeout_s': float('nan')},
                                      {'stall_timeout_s': 1}, {'argv': [sys.executable, '{output}.mp4']},
                                      {'argv': 'echo unsafe'}, {'expected': {'frames': 48}})):
            with self.subTest(patch=patch):
                original = self.plan.copy()
                self.plan.update(patch)
                self.work = self.root / f'invalid-{index}'
                result, _ = self.run_job('--execute')
                self.assertNotEqual(result.returncode, 0)
                self.plan = original

    def test_log_output_is_bounded(self):
        self.plan['log_limit_bytes'] = 4096
        self.python_job('import sys; print("a"*200000); sys.exit(1)')
        result, receipt = self.run_job('--execute')
        self.assertNotEqual(result.returncode, 0)
        self.assertLessEqual((self.work / 'render.log').stat().st_size, 4096)
        self.assertTrue(receipt['render']['log_truncated'])
        self.assertLessEqual(len(receipt['render']['tail']), 32768)

    def test_interrupt_terminates_owned_child_and_retains_receipt(self):
        self.check_signal(signal.SIGINT)

    def test_sigterm_terminates_owned_child_and_retains_receipt(self):
        self.check_signal(signal.SIGTERM)

    def check_signal(self, interruption):
        pidfile = self.root / 'child.pid'
        self.python_job(f'import os,time; open({str(pidfile)!r}, "w").write(str(os.getpid())); time.sleep(20)')
        process = subprocess.Popen(self.command('--execute'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(.03)
            self.assertTrue(pidfile.exists())
            child = int(pidfile.read_text())
            process.send_signal(interruption)
            stdout, stderr = process.communicate(timeout=4)
            self.assertNotEqual(process.returncode, 0, stdout + stderr)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            receipt = json.loads((self.work / 'receipt.json').read_text())
            self.assertEqual(receipt['status'], 'FAILED')
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_relative_executable_and_paths_resolve_from_plan_directory(self):
        (self.root / 'local-python').symlink_to(sys.executable)
        self.plan.update(source='source.mp4', output='output.mp4', argv=['./local-python', '-c', 'pass', '{output}'])
        result, receipt = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(receipt['executable']['path'], str(Path(sys.executable).resolve()))

    def test_relative_renderer_and_config_use_default_or_explicit_cwd(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                directory = self.root / 'renderer-dir' if explicit else self.root
                directory.mkdir(exist_ok=True)
                self.work = self.root / f'cwd-work-{explicit}'
                (directory / 'config.json').write_text('{"camera": "test"}')
                (directory / 'renderer.py').write_text('import json,sys\nfrom pathlib import Path\nassert json.loads(Path("config.json").read_text())["camera"] == "test"\nPath(sys.argv[1]).write_bytes(b"rendered")\nprint("relative inputs resolved")\n')
                self.plan['argv'] = [sys.executable, 'renderer.py', '{output}']
                if explicit:
                    self.plan['cwd'] = 'renderer-dir'
                result, receipt = self.run_job('--execute')
                self.assertNotEqual(result.returncode, 0)  # Deliberately not media.
                self.assertEqual(receipt.get('render', {}).get('returncode'), 0, result.stdout)
                self.assertEqual((self.work / 'staging.mp4').read_bytes(), b'rendered')
                self.assertEqual(receipt['cwd'], str(directory.resolve()))

    def test_bound_extra_input_hash_detects_same_size_mtime_rewrite(self):
        config = self.root / 'config.json'
        config.write_bytes(b'configuration')
        self.plan['inputs'] = ['config.json']
        self.python_job('import os\nfrom pathlib import Path\np=Path("config.json"); st=p.stat(); p.write_bytes(b"x"*st.st_size); os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns))')
        result, receipt = self.run_job('--execute')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('input changed', receipt.get('error', ''))
        key = str(config.resolve())
        before, after = receipt['inputs_before'][key], receipt['inputs_after'][key]
        self.assertEqual(before['size'], after['size'])
        self.assertEqual(before['mtime_ns'], after['mtime_ns'])
        self.assertNotEqual(before['sha256'], after['sha256'])

    def test_bound_input_symlink_retarget_cannot_pass_with_old_target_hash(self):
        self.configure_real_render()
        original, replacement = self.root / 'original.json', self.root / 'replacement.json'
        original.write_text('{"value": "original"}')
        replacement.write_text('{"value": "replacement"}')
        alias = self.root / 'config.json'
        alias.symlink_to(original.name)
        self.plan['inputs'] = ['config.json']
        self.plan['progress_format'] = 'none'
        self.plan.pop('stall_timeout_s')
        self.plan['expected']['require_faststart'] = False
        self.python_job('import json,shutil,sys\nfrom pathlib import Path\np=Path("config.json"); p.unlink(); p.symlink_to("replacement.json")\nassert json.loads(p.read_text())["value"] == "replacement"\nshutil.copyfile(sys.argv[2],sys.argv[1])')
        result, receipt = self.run_job('--execute')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('input path changed', receipt.get('error', ''))
        self.assertFalse(self.output.exists())
        declared = str(self.root.resolve() / alias.name)
        self.assertEqual(receipt['input_bindings_before'][declared], str(original.resolve()))
        self.assertEqual(receipt['input_bindings_after'][declared], str(replacement.resolve()))

    def test_replaced_cwd_identity_is_rejected(self):
        directory = self.root / 'renderer-dir'
        directory.mkdir()
        self.plan['cwd'] = 'renderer-dir'
        self.python_job('from pathlib import Path\np=Path.cwd(); p.rename(p.with_name("old-renderer-dir")); p.mkdir()')
        result, receipt = self.run_job('--execute')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cwd changed', receipt.get('error', ''))
        self.assertNotEqual(receipt['cwd_before']['inode'], receipt['cwd_after']['inode'])

    def test_bound_executable_change_fails_using_only_temporary_stub(self):
        template, stub = self.root / 'template', self.root / 'local-runner'
        template.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        shutil.copyfile(template, stub)
        stub.chmod(0o700)
        code = 'import os\nfrom pathlib import Path\np=Path("local-runner"); st=p.stat(); p.write_bytes(b"x"*st.st_size); os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns))'
        self.plan['argv'] = ['./local-runner', '-c', code, '{output}']
        result, receipt = self.run_job('--execute')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('executable changed', receipt.get('error', ''))
        self.assertNotEqual(receipt['executable']['sha256'], receipt['executable_after']['sha256'])

    def test_progress_growth_keeps_job_alive_but_constant_progress_stalls(self):
        self.plan.update(progress_format='ffmpeg', timeout_s=3, stall_timeout_s=.25)
        self.python_job('import time\nfor n in range(1, 7):\n print("out_time_us="+str(n*100000),flush=True); time.sleep(.1)\nwhile True:\n print("out_time_us=600000",flush=True); time.sleep(.05)')
        started = time.monotonic()
        result, receipt = self.run_job('--execute')
        elapsed = time.monotonic() - started
        self.assertNotEqual(result.returncode, 0)
        self.assertGreater(elapsed, .7)
        self.assertLess(elapsed, 2)
        self.assertIn('stall', receipt['error'])
        self.assertEqual(receipt['render']['progress']['out_time_us'], 600000)

    def test_closed_progress_pipe_does_not_disable_stall_timeout(self):
        self.plan.update(progress_format='ffmpeg', timeout_s=5, stall_timeout_s=.25)
        self.python_job('import os,time; os.close(1); os.close(2); time.sleep(2)')
        started = time.monotonic()
        result, receipt = self.run_job('--execute')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('stall', receipt['error'])
        self.assertLess(time.monotonic() - started, 1.8)

    def test_timeout_kills_owned_descendant_ignoring_sigterm(self):
        marker = self.root / 'escaped-child-marker'
        code = f'import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(1.2); Path({str(marker)!r}).touch()'
        self.python_job(f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{code!r}]); time.sleep(5)')
        self.plan['timeout_s'] = .3
        result, receipt = self.run_job('--execute')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('timeout', receipt['error'])
        time.sleep(1.4)
        self.assertFalse(marker.exists(), 'owned descendant escaped timeout cleanup')

    def configure_real_render(self):
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        self.source.unlink()
        subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2',
                        '-c:v', 'libx264', '-c:a', 'aac', str(self.source)], check=True, capture_output=True)
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.plan['argv'] = [ffmpeg, '-v', 'error', '-nostdin', '-i', '{source}', '-c', 'copy',
                             '-movflags', '+faststart', '-progress', 'pipe:1', '{output}']
        self.plan.update(progress_format='ffmpeg', timeout_s=15, stall_timeout_s=5)
        self.plan['expected'].update(audio_copy_from=str(self.source), sample_rate=48000, channels=1,
                                     require_faststart=True, contacts=True, privacy_window=['.25:.75:.25'])
        return before

    def test_real_render_full_validation_and_atomic_no_clobber(self):
        before = self.configure_real_render()
        config = self.root / 'camera-config.json'
        config_target = self.root / 'camera-config-data.json'
        config_target.write_text('{"camera": "synthetic"}')
        config.symlink_to(config_target.name)
        self.plan['inputs'] = ['camera-config.json']
        result, receipt = self.run_job('--execute')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(receipt['status'], 'TECHNICAL_VERIFIED_VISUAL_REVIEW_REQUIRED')
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())
        verified = json.loads((self.work / 'verification.json').read_text())
        self.assertEqual(verified['decoded']['video_frames'], 48)
        self.assertTrue(verified['checks']['audio_payload_identity']['passed'])
        manifest = json.loads((self.work / 'manifest.json').read_text())
        self.assertEqual(manifest['output']['sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        for key in ('source_before', 'plan_sha256', 'executable', 'output'):
            self.assertIn(key, manifest)
        self.assertEqual(manifest['cwd'], str(self.root.resolve()))
        self.assertEqual(manifest['inputs_before'], manifest['inputs_after'])
        self.assertEqual(manifest['inputs_before'][str(config.resolve())]['sha256'], hashlib.sha256(config.read_bytes()).hexdigest())
        self.assertEqual(manifest['input_bindings_before'], manifest['input_bindings_after'])
        self.assertEqual(manifest['input_bindings_before'][str(self.root.resolve() / config.name)], str(config_target.resolve()))
        self.assertEqual(manifest['cwd_before'], manifest['cwd_after'])
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(any(event.get('event') == 'progress' for event in events))
        self.assertTrue(list((self.work / 'contacts').glob('*.jpg')))

    def test_delivery_race_never_overwrites_a_newly_created_output(self):
        self.configure_real_render()
        self.plan['expected']['require_faststart'] = False
        self.plan.pop('stall_timeout_s')
        self.plan['progress_format'] = 'none'
        self.python_job(f'import sys,shutil; from pathlib import Path; shutil.copyfile(sys.argv[2],sys.argv[1]); Path({str(self.output)!r}).write_bytes(b"race winner")')
        result, receipt = self.run_job('--execute')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertEqual(self.output.read_bytes(), b'race winner')
        self.assertEqual(json.loads((self.work / 'verification.json').read_text())['technical_status'], 'passed')
        self.assertFalse((self.work / 'manifest.json').exists())

    def test_real_media_with_wrong_expected_frame_count_fails_gate(self):
        self.configure_real_render()
        self.plan['expected']['frames'] = 49
        result, receipt = self.run_job('--execute')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(receipt['status'], 'FAILED')
        report = json.loads((self.work / 'verification.json').read_text())
        self.assertEqual(report['decoded']['video_frames'], 48)
        self.assertEqual(report['technical_status'], 'failed')
        self.assertFalse(self.output.exists())
        self.assertTrue((self.work / 'staging.mp4').exists())


if __name__ == '__main__':
    unittest.main()
