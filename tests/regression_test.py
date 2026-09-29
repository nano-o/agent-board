#!/usr/bin/env python3
"""Behavioral board regressions; all repositories and configuration are disposable."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import select
import shutil
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'bin/agent-board'
MARKER = '# agent-board guard: remove with agent-board uninstall-hook.'


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='board-regression-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('GIT_', 'AGENT_BOARD_', 'ISABELLE_BOARD_'))}
        self.env.update(HOME=str(self.base / 'home'), GIT_CONFIG_NOSYSTEM='1',
                        GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0',
                        GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.invalid',
                        PYTHONDONTWRITEBYTECODE='1')
        Path(self.env['HOME']).mkdir()
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'src').mkdir()
        (self.repo / 'src/x.txt').write_text('initial\n')
        (self.repo / 'PLAN.md').write_text('plan\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')
        self.board = self.repo / '.git/agent-board'

    def run_cmd(self, args, *, cwd=None, agent=None, input=None, check=True, **kwargs):
        env = dict(self.env)
        if agent:
            env['AGENT_BOARD_AGENT'] = agent
        result = subprocess.run([str(x) for x in args], cwd=cwd or self.repo,
                                env=env, input=input, text=True, capture_output=True,
                                timeout=20, **kwargs)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git(self, *args, **kwargs):
        return self.run_cmd(['git', *args], **kwargs)

    def b(self, *args, **kwargs):
        return self.run_cmd([CLI, *args], **kwargs)

    def claim(self, agent, *resources, **kwargs):
        return self.b('--as', agent, 'claim', '--reason', 'test lease', *resources, **kwargs)

    def stale(self, agent):
        os.utime(self.board / 'agents' / agent, (1, 1))

    def test_nested_nonexistent_and_root(self):
        self.claim('alice', 'new.txt', cwd=self.repo / 'src')
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'src/new.txt', check=False).returncode, 0)
        self.b('--as', 'alice', 'release', '--all')
        self.claim('alice', '.')
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'anything', check=False).returncode, 0)

    def test_overlap_acquisition_is_atomic(self):
        self.claim('alice', 'src/')
        self.assertNotEqual(self.claim('bob', 'PLAN.md', 'src/x.txt', check=False).returncode, 0)
        self.b('--as', 'carol', 'guard', 'PLAN.md')

    def test_explicit_observers_renew(self):
        self.claim('alice', 'PLAN.md')
        for args in [('guard', 'PLAN.md'), ('who',), ('claims',)]:
            self.stale('alice')
            self.b('--as', 'alice', *args)
            self.assertGreater((self.board / 'agents/alice').stat().st_mtime, time.time() - 10)
            self.assertNotEqual(self.b('--as', 'bob', 'guard', 'PLAN.md', check=False).returncode, 0)
        self.stale('alice')
        self.b('claims')
        self.assertEqual((self.board / 'agents/alice').stat().st_mtime, 1)

    def test_digest_first_read_does_not_omit_posts(self):
        for n in range(12):
            self.b('--as', 'alice', 'post', f'message-{n:02d}')
        out = self.b('digest', '--cursor', 'reader', '--mark').stdout
        for n in range(12):
            self.assertIn(f'message-{n:02d}', out)
        self.assertEqual(self.b('digest', '--cursor', 'reader').stdout, '')

    def test_failed_delivery_does_not_acknowledge(self):
        self.b('--as', 'alice', 'post', 'must be replayed')
        with open('/dev/full', 'w') as sink:
            result = subprocess.run([str(CLI), 'digest', '--cursor', 'reader', '--mark'],
                                    cwd=self.repo, env=self.env, stdout=sink,
                                    stderr=subprocess.PIPE, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must be replayed', self.b('digest', '--cursor', 'reader').stdout)

    def test_foreign_fast_forward_is_blocked(self):
        self.git('checkout', '-qb', 'feature')
        (self.repo / 'PLAN.md').write_text('new\n')
        self.git('commit', '-qam', 'feature')
        self.git('checkout', '-q', 'main')
        before = self.git('rev-parse', 'HEAD').stdout
        self.claim('alice', 'refs/heads/main')
        self.b('install-hook')
        self.assertNotEqual(self.git('merge', '--ff-only', 'feature', agent='bob', check=False).returncode, 0)
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout, before)

    def test_delegated_worker_commit_and_handoff(self):
        self.b('--as', 'coordinator', 'hello', '--task', 'delegating')
        wt = self.base / 'worker'
        self.git('worktree', 'add', '-qb', 'worker', wt)
        self.b('install-hook')
        self.b('--as', 'worker', 'hello', '--task', 'implement', cwd=wt)
        self.claim('worker', 'refs/heads/worker', 'src/x.txt', cwd=wt)
        (wt / 'src/x.txt').write_text('worker change\n')
        self.git('commit', '-qam', 'worker', agent='worker', cwd=wt)
        self.b('--as', 'worker', 'post', '--kind', 'handoff', 'worker ready', cwd=wt)
        self.b('--as', 'worker', 'bye', cwd=wt)
        self.claim('coordinator', 'refs/heads/main')
        self.git('merge', '--ff-only', 'worker', agent='coordinator')
        self.assertIn('worker ready', self.b('digest', '--cursor', 'coordinator', '--mark').stdout)

    def probe(self, args, boundary, suffix=''):
        """Pause at an IO boundary without adding test switches to production.

        Pipes acknowledge arrival and explicitly resume the child. Timeouts only
        detect a broken test; no sleep determines which operation wins.
        """
        ready_r, ready_w = os.pipe()
        resume_r, resume_w = os.pipe()
        code = r'''import importlib.util, json, os, pathlib, sys
spec = importlib.util.spec_from_file_location('agent_board', sys.argv[1])
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)
boundary, suffix, ready, resume = sys.argv[2:6]
ready, resume = int(ready), int(resume)
def pause():
    os.write(ready, b'R')
    if os.read(resume, 1) != b'G':
        raise RuntimeError('probe abandoned')
if boundary == 'replace':
    original = board.os.replace
    def replace(src, dst):
        if str(dst).endswith(suffix):
            pause()
        return original(src, dst)
    board.os.replace = replace
elif boundary == 'fail':
    # Fail the first matching replacement only, so rollback can still run.
    original = board.os.replace
    failed = []
    def replace(src, dst):
        if str(dst).endswith(suffix) and not failed:
            failed.append(dst)
            raise OSError('injected failure')
        return original(src, dst)
    board.os.replace = replace
elif boundary == 'lock':
    original = board.fcntl.flock
    def flock(fd, operation):
        if operation == board.fcntl.LOCK_EX:
            os.write(ready, b'R')
        return original(fd, operation)
    board.fcntl.flock = flock
elif boundary == 'output':
    original = sys.stdout
    class Output:
        def write(self, text):
            pause()
            return original.write(text)
        def flush(self):
            original.flush()
    sys.stdout = Output()
sys.argv = ['agent-board'] + json.loads(sys.argv[6])
sys.exit(board.main())
'''
        child = subprocess.Popen(['python3', '-c', code, str(ROOT / 'src/agent_board.py'),
                                  boundary, suffix, str(ready_w), str(resume_r), json.dumps(args)],
                                 cwd=self.repo, env=self.env, pass_fds=(ready_w, resume_r),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        os.close(ready_w)
        os.close(resume_r)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate()
            os.close(ready_r)
            os.close(resume_w)
        self.addCleanup(cleanup)
        return child, ready_r, resume_w

    def arrived(self, probe):
        self.assertTrue(select.select([probe[1]], [], [], 10)[0], 'child did not reach barrier')
        self.assertEqual(os.read(probe[1], 1), b'R')

    def resume(self, probe):
        os.write(probe[2], b'G')

    def finish(self, probe, expected=0):
        stdout, stderr = probe[0].communicate(timeout=20)
        self.assertEqual(probe[0].returncode, expected, stdout + stderr)
        return stdout

    def test_initial_and_stale_concurrent_claims_have_one_winner(self):
        self.b('--as', 'setup', 'hello', '--task', 'setup')
        for stale in (False, True):
            if stale:
                self.claim('old', 'PLAN.md')
                self.stale('old')
            with (self.board / '.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                contenders = [self.probe(['--as', f'agent{n}', 'claim', '--reason', 'race', 'PLAN.md'], 'lock')
                              for n in range(8)]
                for contender in contenders:
                    self.arrived(contender)
                fcntl.flock(lock, fcntl.LOCK_UN)
            results = [p[0].communicate(timeout=20) for p in contenders]
            codes = [p[0].returncode for p in contenders]
            self.assertEqual(codes.count(0), 1, results)
            self.assertEqual(codes.count(1), 7, results)
            winner = codes.index(0)
            self.b('--as', f'agent{winner}', 'release', '--all')

    def test_concurrent_directory_and_file_claims_have_one_winner(self):
        self.b('--as', 'setup', 'hello', '--task', 'setup')
        with (self.board / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            contenders = [self.probe(['--as', who, 'claim', '--reason', 'race', path], 'lock')
                          for who, path in [('alice', 'src/'), ('bob', 'src/x.txt')]]
            for contender in contenders:
                self.arrived(contender)
            fcntl.flock(lock, fcntl.LOCK_UN)
        for contender in contenders:
            contender[0].communicate(timeout=20)
        self.assertEqual(sorted(p[0].returncode for p in contenders), [0, 1])

    def test_killed_claim_leaves_complete_snapshot_and_unlocks(self):
        self.b('--as', 'setup', 'hello', '--task', 'setup')
        before = json.loads((self.board / 'state.json').read_text())['claims']
        dying = self.probe(['--as', 'alice', 'claim', '--reason', 'interrupted', 'PLAN.md', 'src/'],
                          'replace', 'state.json')
        self.arrived(dying)
        self.assertEqual(json.loads((self.board / 'state.json').read_text())['claims'], before)
        successor = self.probe(['--as', 'bob', 'claim', '--reason', 'after crash', 'PLAN.md'], 'lock')
        self.arrived(successor)
        dying[0].kill()
        dying[0].communicate(timeout=10)
        self.finish(successor)
        self.assertNotIn('held by alice', self.b('claims').stdout)
        self.b('--as', 'carol', 'guard', 'src/x.txt')

    def test_displaced_stale_ancestor_cannot_revive_or_release_successor(self):
        self.claim('alice', 'src/')
        self.stale('alice')
        self.claim('bob', 'src/x.txt')
        self.b('--as', 'alice', 'who')
        self.b('--as', 'bob', 'guard', 'src/x.txt')
        self.b('--as', 'alice', 'release', '--all')
        self.assertIn('held by bob', self.b('claims').stdout)
        self.assertNotEqual(self.b('--as', 'alice', 'release', 'src/x.txt', check=False).returncode, 0)

    def test_delayed_publisher_cannot_be_overtaken(self):
        self.b('--as', 'setup', 'hello', '--task', 'setup')
        delayed = self.probe(['--as', 'alice', 'post', 'first publisher'], 'replace', '.md')
        self.arrived(delayed)
        later = self.probe(['--as', 'bob', 'post', 'second publisher'], 'lock')
        self.arrived(later)
        self.resume(delayed)
        self.finish(delayed)
        self.finish(later)
        out = self.b('digest', '--cursor', 'reader', '--mark').stdout
        self.assertLess(out.index('first publisher'), out.index('second publisher'))
        self.assertEqual(self.b('digest', '--cursor', 'reader').stdout, '')

    def test_killed_publisher_leaves_gap_without_loss_or_reuse(self):
        self.b('--as', 'setup', 'hello', '--task', 'setup')
        delayed = self.probe(['--as', 'alice', 'post', 'never published'], 'replace', '.md')
        self.arrived(delayed)
        reserved = json.loads((self.board / 'state.json').read_text())['sequence']
        delayed[0].kill()
        delayed[0].communicate(timeout=10)
        self.b('--as', 'bob', 'post', 'after crash')
        numbers = [int(p.stem) for p in (self.board / 'messages').glob('*.md')]
        self.assertNotIn(reserved, numbers)
        self.assertIn(reserved + 1, numbers)
        out = self.b('digest', '--cursor', 'reader', '--mark').stdout
        self.assertIn('after crash', out)
        self.assertNotIn('never published', out)

    def test_arrival_during_digest_is_left_unread(self):
        self.b('--as', 'alice', 'post', 'snapshot post')
        digest = self.probe(['digest', '--cursor', 'reader', '--mark'], 'output')
        self.arrived(digest)
        # Completes while output is paused: stdout must not hold the board lock.
        self.b('--as', 'bob', 'post', 'arrived during output')
        self.resume(digest)
        first = self.finish(digest)
        self.assertIn('snapshot post', first)
        self.assertNotIn('arrived during output', first)
        self.assertIn('arrived during output', self.b('digest', '--cursor', 'reader', '--mark').stdout)

    def test_concurrent_digest_acknowledgements_never_go_backwards(self):
        self.b('--as', 'alice', 'post', 'first')
        older = self.probe(['digest', '--cursor', 'reader', '--mark'], 'output')
        self.arrived(older)
        self.b('--as', 'bob', 'post', 'second')
        self.assertIn('second', self.b('digest', '--cursor', 'reader', '--mark').stdout)
        self.resume(older)
        self.finish(older)
        self.assertEqual(self.b('digest', '--cursor', 'reader').stdout, '')

    def test_interrupted_digest_replays(self):
        self.b('--as', 'alice', 'post', 'replay after interruption')
        digest = self.probe(['digest', '--cursor', 'reader', '--mark'], 'output')
        self.arrived(digest)
        digest[0].kill()
        digest[0].communicate(timeout=10)
        self.assertIn('replay after interruption', self.b('digest', '--cursor', 'reader').stdout)

    def test_normalization_and_release_after_deletion(self):
        self.claim('alice', str(self.repo / 'src/x.txt'))
        self.assertNotEqual(self.b('--as', 'bob', 'guard', './x.txt', cwd=self.repo / 'src', check=False).returncode, 0)
        self.b('--as', 'alice', 'release', 'x.txt', cwd=self.repo / 'src')
        self.claim('alice', '..', cwd=self.repo / 'src')
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'PLAN.md', check=False).returncode, 0)
        self.b('--as', 'alice', 'release', '.')
        self.claim('alice', 'src')
        (self.repo / 'src/x.txt').unlink()
        (self.repo / 'src').rmdir()
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'src/new/deep', check=False).returncode, 0)
        self.b('--as', 'alice', 'release', 'src')
        self.b('--as', 'bob', 'guard', 'src/new/deep')
        self.claim('alice', 'future/')
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'future/new', check=False).returncode, 0)
        for path in ('../outside-new', str(self.base / 'outside-new'), '../../'):
            self.assertEqual(self.claim('bob', path, check=False).returncode, 2)
        self.b('--project-root', str(self.repo), '--as', 'bob', 'claim', '--reason', 'root override', 'New.md', cwd=self.base)
        self.assertIn('New.md  held by bob', self.b('claims').stdout)

    def test_symlinks_tokens_and_fragments_are_distinct(self):
        (self.repo / 'link').symlink_to('src')
        self.claim('alice', 'link')
        self.b('--as', 'bob', 'guard', 'src/x.txt')
        self.assertEqual(self.claim('bob', 'link/x.txt', check=False).returncode, 2)
        self.claim('alice', 'token:jedit', 'token:server', 'PLAN.md#intro')
        # A bare name is a path, even one that matches a token's name.
        self.claim('bob', 'jedit', 'PLAN.md#body', 'PLAN.md')
        self.b('--as', 'bob', 'guard', 'PLAN.md')
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'token:jedit', check=False).returncode, 0)
        self.assertNotEqual(self.b('--as', 'carol', 'guard', 'path:jedit', check=False).returncode, 0)
        claims = self.b('claims').stdout
        self.assertIn('token:jedit  held by alice', claims)
        self.assertIn('\n  jedit  held by bob', claims)
        self.assertIn('holds a passage', self.b('--as', 'carol', 'guard', 'PLAN.md#intro', check=False).stderr)

    def test_renewal_rules_validation_failure_conflict_and_bye(self):
        self.claim('alice', 'PLAN.md')
        self.b('--as', 'bob', 'hello', '--task', 'other')
        self.stale('alice')
        self.b('--as', 'alice', 'claim', '--reason', '', 'x', check=False)
        self.assertEqual((self.board / 'agents/alice').stat().st_mtime, 1)
        self.claim('bob', 'other')
        self.claim('alice', 'other', check=False)
        self.assertGreater((self.board / 'agents/alice').stat().st_mtime, 1)
        self.b('--as', 'bob', 'bye')
        self.assertFalse((self.board / 'agents/bob').exists())
        self.b('--as', 'bob', 'claims')
        self.assertFalse((self.board / 'agents/bob').exists())
        self.assertEqual(self.b('--as', '../bad', 'claims', check=False).returncode, 2)
        # Unique active inferred guard renews, anonymous observers do not.
        old = time.time() - 60
        os.utime(self.board / 'agents/alice', (old, old))
        self.b('guard', 'PLAN.md')
        self.assertGreater((self.board / 'agents/alice').stat().st_mtime, old)
        self.stale('alice')
        self.b('guard', 'PLAN.md')
        self.assertEqual((self.board / 'agents/alice').stat().st_mtime, 1)

    def history(self):
        base = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('checkout', '-qb', 'feature')
        (self.repo / 'PLAN.md').write_text('feature change\n')
        self.git('commit', '-qam', 'feature')
        feature = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('checkout', '-q', 'main')
        return base, feature

    def test_hook_ref_operations_owner_foreign_and_linked_worktree(self):
        base, feature = self.history()
        wt = self.base / 'linked'
        self.git('worktree', 'add', '-qb', 'linked', wt)
        self.b('install-hook')
        for cwd, branch in ((self.repo, 'main'), (wt, 'linked')):
            ref = 'refs/heads/' + branch
            self.claim('alice', ref, cwd=cwd)
            for operation in [('merge', '--ff-only', 'feature'), ('reset', '--hard', feature),
                              ('update-ref', ref, feature, base), ('rebase', 'feature')]:
                with self.subTest(branch=branch, operation=operation):
                    self.git('reset', '--hard', base, agent='alice', cwd=cwd)
                    result = self.git(*operation, agent='bob', cwd=cwd, check=False)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('refusing:', result.stderr)
                    self.assertEqual(self.git('rev-parse', ref).stdout.strip(), base)
                    # Test fixture cleanup is authorized here; production never
                    # performs this discard/recovery after rejection.
                    self.git('rebase', '--abort', cwd=cwd, agent='alice', check=False)
                    self.git('checkout', '-f', branch, cwd=cwd, agent='alice')
                    self.git('reset', '--hard', base, agent='alice', cwd=cwd)
                    self.git(*operation, agent='alice', cwd=cwd)
                    self.assertEqual(self.git('rev-parse', ref).stdout.strip(), feature)
            self.b('--as', 'alice', 'release', ref, cwd=cwd)
        self.git('update-ref', 'refs/heads/unrelated', feature, agent='bob')

    def test_hook_ref_creation_deletion_and_multiple_ref_transaction(self):
        base = self.git('rev-parse', 'HEAD').stdout.strip()
        self.b('install-hook')
        self.claim('alice', 'refs/heads/protected')
        self.assertNotEqual(self.git('branch', 'protected', agent='bob', check=False).returncode, 0)
        self.git('branch', 'protected', agent='alice')
        self.assertNotEqual(self.git('branch', '-D', 'protected', agent='bob', check=False).returncode, 0)
        self.assertEqual(self.git('rev-parse', 'protected').stdout.strip(), base)
        self.git('branch', '-D', 'protected', agent='alice')
        data = f'start\ncreate refs/heads/unrelated {base}\ncreate refs/heads/protected {base}\nprepare\ncommit\n'
        result = self.git('update-ref', '--stdin', input=data, agent='bob', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(self.git('rev-parse', '--verify', 'refs/heads/unrelated', check=False).returncode, 0)
        self.git('update-ref', '--stdin', input=data, agent='alice')
        self.stale('alice')
        self.git('update-ref', '-d', 'refs/heads/protected', base, agent='bob')

    def test_branch_rename_source_and_destination_boundary(self):
        self.b('install-hook')
        self.git('branch', 'source')
        self.claim('alice', 'refs/heads/source')
        result = self.git('branch', '-m', 'source', 'renamed', agent='bob', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.git('rev-parse', '--verify', 'refs/heads/source')
        self.git('branch', '-m', 'source', 'renamed', agent='alice')
        self.b('--as', 'alice', 'release', '--all')
        self.claim('alice', 'refs/heads/destination')
        # The cooperative check protects BOTH names. Git 2.43's files backend
        # bypasses the destination transaction event for branch -m itself.
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'refs/heads/renamed', 'refs/heads/destination', check=False).returncode, 0)
        result = self.git('branch', '-m', 'renamed', 'destination', agent='bob', check=False)
        version = self.git('--version').stdout
        if '2.43.' in version:
            self.assertEqual(result.returncode, 0, result.stderr)
        elif result.returncode:
            self.assertIn('refusing:', result.stderr)
        # Owner rename succeeds whether or not this Git emits a target event.
        self.git('branch', 'owner-source')
        self.claim('alice', 'refs/heads/owner-destination')
        self.git('branch', '-m', 'owner-source', 'owner-destination', agent='alice')

    def test_hook_chaining_reinstall_restore_stdin_args_and_exit(self):
        hooks = self.repo / '.git/hooks'
        ref_hook = hooks / 'reference-transaction'
        pre_hook = hooks / 'pre-commit'
        ref_body = f'''#!/usr/bin/env python3
import os, pathlib, sys
pathlib.Path({str(self.base / 'foreign-args')!r}).write_text('|'.join(sys.argv[1:]))
pathlib.Path({str(self.base / 'foreign-stdin')!r}).write_bytes(sys.stdin.buffer.read())
sys.exit(int(os.environ.get('FOREIGN_STATUS', '0')))
'''
        pre_body = '#!/usr/bin/env bash\nexit 17\n'
        ref_hook.write_text(ref_body)
        pre_hook.write_text(pre_body)
        ref_hook.chmod(0o755)
        pre_hook.chmod(0o755)
        self.assertEqual(self.b('install-hook', check=False).returncode, 2)
        self.assertEqual(ref_hook.read_text(), ref_body)
        self.assertEqual(pre_hook.read_text(), pre_body)
        self.b('install-hook', '--force')
        self.b('install-hook')
        self.claim('alice', 'refs/heads/protected')
        base = self.git('rev-parse', 'HEAD').stdout.strip()
        stdin = f'{base} {base} refs/heads/unrelated\n{base} {base} refs/heads/protected\n'
        self.assertEqual(self.run_cmd([ref_hook, 'prepared'], input=stdin, agent='bob', check=False).returncode, 1)
        self.assertEqual((self.base / 'foreign-stdin').read_text(), stdin)
        self.assertEqual((self.base / 'foreign-args').read_text(), 'prepared')
        for state in ('aborted', 'committed'):
            self.run_cmd([ref_hook, state], input=stdin, agent='bob')
            self.assertEqual((self.base / 'foreign-args').read_text(), state)
        self.env['FOREIGN_STATUS'] = '23'
        self.assertEqual(self.run_cmd([ref_hook, 'prepared'], input=stdin, agent='alice', check=False).returncode, 23)
        self.assertEqual(self.run_cmd([pre_hook], check=False).returncode, 17)
        self.b('uninstall-hook')
        self.assertEqual(ref_hook.read_text(), ref_body)
        self.assertEqual(pre_hook.read_text(), pre_body)
        self.assertTrue(os.access(ref_hook, os.X_OK))
        self.assertFalse((hooks / 'reference-transaction.pre-board').exists())
        self.b('uninstall-hook')

    def test_hooks_without_board_and_commit_no_verify_boundary(self):
        base, feature = self.history()
        self.b('install-hook')
        self.git('merge', '--ff-only', 'feature')
        self.assertFalse(self.board.exists())
        self.git('reset', '--hard', base)
        self.claim('alice', 'refs/heads/main')
        result = self.git('commit', '--allow-empty', '--no-verify', '-m', 'bypass', agent='bob', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing:', result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), base)
        self.git('commit', '--allow-empty', '--no-verify', '-m', 'owner', agent='alice')

    def test_corruption_is_rejected_with_python_optimization(self):
        self.claim('alice', 'PLAN.md')
        state = json.loads((self.board / 'state.json').read_text())
        state['version'] = 99
        (self.board / 'state.json').write_text(json.dumps(state))
        self.env['PYTHONOPTIMIZE'] = '1'
        result = self.b('--as', 'bob', 'guard', 'PLAN.md', check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unsupported format version', result.stderr)

    def test_ref_and_reserved_path_spellings_cannot_bypass_claims(self):
        self.claim('alice', 'refs//heads/main', 'path:jedit')
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'refs/heads/main', check=False).returncode, 0)
        self.assertNotEqual(self.b('--as', 'bob', 'guard', './jedit', check=False).returncode, 0)
        self.b('--as', 'alice', 'release', 'path:jedit')
        self.b('--as', 'bob', 'guard', './jedit')

    def test_release_batch_preserves_ownership_on_conflict(self):
        self.claim('alice', 'PLAN.md')
        self.claim('bob', 'src/')
        self.assertEqual(self.b('--as', 'alice', 'release', 'PLAN.md', 'src/', check=False).returncode, 2)
        self.assertNotEqual(self.b('--as', 'carol', 'guard', 'PLAN.md', check=False).returncode, 0)

    # --- state that is incomplete or corrupt -------------------------------------

    def assert_incomplete(self, before):
        for args in [('claims',), ('show',), ('who',), ('digest', '--cursor', 'reader'),
                     ('guard', 'PLAN.md'), ('--as', 'bob', 'hello', '--task', 'x'),
                     ('--as', 'bob', 'post', 'x'), ('--if-board', '--as', 'tool', 'post', 'x'),
                     ('--as', 'bob', 'claim', '--reason', 'x', 'PLAN.md')]:
            with self.subTest(args=args):
                result = self.b(*args, check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('agent-board: incomplete board state', result.stderr)
        self.assertFalse((self.board / 'state.json').exists())
        self.assertEqual(sorted(p.name for p in self.board.iterdir()), before)

    def test_any_entry_without_state_is_incomplete_and_never_restarted(self):
        for entry in ('format', 'agents', 'posts', 'messages', 'cursors-v2', 'unexpected'):
            with self.subTest(entry=entry):
                if self.board.exists():
                    shutil.rmtree(self.board)
                self.board.mkdir(parents=True)
                (self.board / '.lock').touch()
                if entry in ('format', 'unexpected'):
                    (self.board / entry).write_text('2\n')
                else:
                    (self.board / entry).mkdir()
                self.assert_incomplete(sorted(['.lock', entry]))

    def test_lost_state_of_a_used_board_is_incomplete(self):
        self.claim('alice', 'PLAN.md')
        (self.board / 'state.json').unlink()
        self.assert_incomplete(sorted(p.name for p in self.board.iterdir()))

    def test_lock_alone_is_no_board(self):
        self.board.mkdir(parents=True)
        (self.board / '.lock').touch()
        self.assertIn('no board yet', self.b('claims').stdout)
        self.b('--if-board', '--as', 'tool', 'post', 'not created')
        self.assertFalse((self.board / 'state.json').exists())
        self.b('--as', 'alice', 'post', 'first')
        self.assertEqual((self.board / 'format').read_text(), '2\n')
        self.assertIn('first', self.b('digest', '--cursor', 'reader').stdout)

    def test_malformed_state_and_wrong_format_fail_closed(self):
        self.claim('alice', 'PLAN.md')
        good = (self.board / 'state.json').read_text()
        for body in ('broken json', '[]', '{"version": 2, "claims": [null], "sequence": 0}',
                     '{"version": 2, "claims": [], "sequence": -1}'):
            with self.subTest(state=body):
                (self.board / 'state.json').write_text(body)
                result = self.b('--as', 'bob', 'guard', 'PLAN.md', check=False)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('agent-board: malformed authoritative board state', result.stderr)
                self.assertEqual((self.board / 'state.json').read_text(), body)
        (self.board / 'state.json').write_text(good)
        for marker in ('1\n', '2', 'two\n'):
            with self.subTest(format=marker):
                (self.board / 'format').write_text(marker)
                result = self.b('claims', check=False)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('unsupported or malformed board format marker', result.stderr)
        (self.board / 'format').write_text('2\n')
        self.assertIn('PLAN.md  held by alice', self.b('claims').stdout)

    # --- names, locations and version ---------------------------------------------

    def test_former_names_and_location_are_ignored(self):
        old = self.repo / '.git/isabelle-tooling/board'
        (old / 'messages').mkdir(parents=True)
        (old / 'state.json').write_text('{"version": 2, "claims": [], "sequence": 0}\n')
        self.assertIn('no board yet', self.b('show').stdout)
        self.env['ISABELLE_BOARD_AGENT'] = 'alice'
        self.env['ISABELLE_BOARD_DIR'] = str(self.base / 'elsewhere')
        self.env['ISABELLE_BOARD_STALE_MINUTES'] = '0'
        result = self.b('hello', '--task', 'x', check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('agent-board: this action needs an agent handle', result.stderr)
        self.claim('alice', 'PLAN.md')
        self.assertTrue((self.board / 'state.json').exists())
        self.assertFalse((self.base / 'elsewhere').exists())
        # With a zero lease alice would be stale and her claim ignored.
        self.assertEqual(self.b('--as', 'bob', 'guard', 'PLAN.md', check=False).returncode, 1)
        self.assertEqual(self.b('--as', 'bob', 'migrate', check=False).returncode, 2)

    def copy_board(self, destination):
        for folder in ('bin', 'src'):
            shutil.copytree(ROOT / folder, destination / folder,
                            ignore=shutil.ignore_patterns('__pycache__'))
        return destination / 'bin/agent-board'

    def version(self, cli, **kwargs):
        result = self.run_cmd([cli, 'version', '--json'], cwd=self.env['HOME'], **kwargs)
        return json.loads(result.stdout)

    def test_version_reports_interface_commit_and_executable(self):
        plain = self.copy_board(self.base / 'plain')
        info = self.version(plain)
        self.assertEqual((info['interface'], info['state_format']), (1, 2))
        self.assertIsInstance(info['capabilities'], list)
        self.assertEqual((info['commit'], info['clean']), (None, None))
        self.assertEqual(info['executable'], str(plain.resolve()))
        checkout = self.base / 'checkout'
        cli = self.copy_board(checkout)
        self.git('init', '-q', cwd=checkout)
        self.git('add', '.', cwd=checkout)
        self.git('commit', '-qm', 'board', cwd=checkout)
        head = self.git('rev-parse', 'HEAD', cwd=checkout).stdout.strip()
        link = self.base / 'path-bin/agent-board'
        link.parent.mkdir()
        link.symlink_to(cli)
        info = self.version(link)
        self.assertEqual((info['commit'], info['clean']), (head, True))
        self.assertEqual(info['executable'], str(cli.resolve()))
        # A hook's GIT_DIR names the calling repository, not the board's checkout.
        env = dict(self.env, GIT_DIR=str(self.repo / '.git'))
        result = subprocess.run([str(link), 'version', '--json'], env=env, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)['commit'], head)
        (checkout / 'untracked').write_text('x\n')
        self.assertEqual(self.version(cli)['clean'], False)
        nested = self.copy_board(self.repo / 'vendor')
        self.assertEqual(self.version(nested)['commit'], None)
        text = self.run_cmd([cli, 'version'], cwd=self.env['HOME']).stdout
        self.assertIn('interface 1, state format 2', text)
        self.assertIn(f'commit {head} (modified)', text)

    # --- Git guard installation -------------------------------------------------------

    def test_guards_only_in_the_repository_hooks_directory(self):
        tracked = self.repo / '.githooks'
        tracked.mkdir()
        (tracked / 'keep').write_text('tracked\n')
        for hooks_path in ('.githooks', str(self.base / 'shared-hooks')):
            with self.subTest(hooks_path=hooks_path):
                self.git('config', 'core.hooksPath', hooks_path)
                for action in ('install-hook', 'uninstall-hook'):
                    result = self.b(action, check=False)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn('outside the Git common directory', result.stderr)
        self.assertEqual(sorted(p.name for p in tracked.iterdir()), ['keep'])
        self.assertFalse((self.base / 'shared-hooks').exists())
        self.git('config', 'core.hooksPath', '.git/custom-hooks')
        self.b('install-hook')
        self.assertTrue((self.repo / '.git/custom-hooks/pre-commit').exists())
        self.claim('alice', 'PLAN.md')
        (self.repo / 'PLAN.md').write_text('changed\n')
        self.git('add', 'PLAN.md')
        self.assertNotEqual(self.git('commit', '-qm', 'x', agent='bob', check=False).returncode, 0)

    def test_guard_marker_line_and_recorded_executable(self):
        link = self.base / 'path-bin/agent-board'
        link.parent.mkdir()
        link.symlink_to(CLI)
        self.run_cmd([link, 'install-hook'])
        hooks = self.repo / '.git/hooks'
        for name in ('pre-commit', 'reference-transaction'):
            lines = (hooks / name).read_text().splitlines()
            self.assertEqual(lines[1], MARKER)
            self.assertIn(str(CLI.resolve()), (hooks / name).read_text())
            self.assertNotIn(str(link), (hooks / name).read_text())
        self.b('uninstall-hook')
        foreign = [
            '#!/usr/bin/env bash\n# isabelle-tooling board guard: remove with board.sh uninstall-hook.\nexit 0\n',
            f'#!/usr/bin/env bash\nexit 0\n{MARKER}\n',
        ]
        for body in foreign:
            with self.subTest(body=body):
                (hooks / 'pre-commit').write_text(body)
                self.assertIn('no board hook installed', self.b('uninstall-hook').stdout)
                self.assertEqual((hooks / 'pre-commit').read_text(), body)
                result = self.b('install-hook', check=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn('already exists', result.stderr)
                self.assertFalse((hooks / 'reference-transaction').exists())

    def hook_state(self):
        hooks = self.repo / '.git/hooks'
        return {p.name: p.read_text() for p in hooks.iterdir() if not p.name.endswith('.sample')}

    def test_install_writes_both_guards_or_neither(self):
        hooks = self.repo / '.git/hooks'
        cases = {
            'fresh': {},
            'foreign': {'pre-commit': '#!/usr/bin/env bash\nexit 0\n',
                        'reference-transaction': '#!/usr/bin/env bash\ncat >/dev/null\n'},
        }
        for case, files in cases.items():
            with self.subTest(case=case):
                for p in hooks.iterdir():
                    if not p.name.endswith('.sample'):
                        p.unlink()
                for name, body in files.items():
                    (hooks / name).write_text(body)
                    (hooks / name).chmod(0o755)
                before = self.hook_state()
                args = ['install-hook'] + (['--force'] if files else [])
                failing = self.probe(args, 'fail', 'reference-transaction')
                stdout, stderr = failing[0].communicate(timeout=20)
                self.assertEqual(failing[0].returncode, 2, stdout + stderr)
                self.assertIn('injected failure', stderr)
                self.assertEqual(self.hook_state(), before)
        # Reinstalling over current guards restores them on failure too.
        self.b('install-hook', '--force')
        before = self.hook_state()
        (hooks / 'pre-commit').write_text(before['pre-commit'].replace('guard --staged', 'guard --staged '))
        edited = self.hook_state()
        failing = self.probe(['install-hook'], 'fail', 'reference-transaction')
        failing[0].communicate(timeout=20)
        self.assertEqual(failing[0].returncode, 2)
        self.assertEqual(self.hook_state(), edited)
        self.b('install-hook')
        self.assertEqual(self.hook_state(), before)

if __name__ == '__main__':
    unittest.main()
