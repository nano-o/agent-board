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
CLI = ROOT / 'scripts/board.sh'


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='board-regression-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('GIT_', 'ISABELLE_BOARD_'))}
        self.env.update(HOME=str(self.base / 'home'), GIT_CONFIG_NOSYSTEM='1',
                        GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0',
                        GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.invalid',
                        PYTHONDONTWRITEBYTECODE='1')
        Path(self.env['HOME']).mkdir()
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'formal').mkdir()
        (self.repo / 'formal/X.thy').write_text('initial\n')
        (self.repo / 'PLAN.md').write_text('plan\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')
        self.board = self.repo / '.git/isabelle-tooling/board'

    def run_cmd(self, args, *, cwd=None, agent=None, input=None, check=True, **kwargs):
        env = dict(self.env)
        if agent:
            env['ISABELLE_BOARD_AGENT'] = agent
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
        self.claim('alice', 'New.thy', cwd=self.repo / 'formal')
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'formal/New.thy', check=False).returncode, 0)
        self.b('--as', 'alice', 'release', '--all')
        self.claim('alice', '.')
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'anything', check=False).returncode, 0)

    def test_overlap_acquisition_is_atomic(self):
        self.claim('alice', 'formal/')
        self.assertNotEqual(self.claim('bob', 'PLAN.md', 'formal/X.thy', check=False).returncode, 0)
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
        self.claim('worker', 'refs/heads/worker', 'formal/X.thy', cwd=wt)
        (wt / 'formal/X.thy').write_text('worker change\n')
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
spec = importlib.util.spec_from_file_location('board', sys.argv[1])
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
sys.argv = ['board.sh'] + json.loads(sys.argv[6])
sys.exit(board.main())
'''
        child = subprocess.Popen(['python3', '-c', code, str(ROOT / 'scripts/board.py'),
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
                          for who, path in [('alice', 'formal/'), ('bob', 'formal/X.thy')]]
            for contender in contenders:
                self.arrived(contender)
            fcntl.flock(lock, fcntl.LOCK_UN)
        for contender in contenders:
            contender[0].communicate(timeout=20)
        self.assertEqual(sorted(p[0].returncode for p in contenders), [0, 1])

    def test_killed_claim_leaves_complete_snapshot_and_unlocks(self):
        self.b('--as', 'setup', 'hello', '--task', 'setup')
        before = json.loads((self.board / 'state.json').read_text())['claims']
        dying = self.probe(['--as', 'alice', 'claim', '--reason', 'interrupted', 'PLAN.md', 'formal/'],
                          'replace', 'state.json')
        self.arrived(dying)
        self.assertEqual(json.loads((self.board / 'state.json').read_text())['claims'], before)
        successor = self.probe(['--as', 'bob', 'claim', '--reason', 'after crash', 'PLAN.md'], 'lock')
        self.arrived(successor)
        dying[0].kill()
        dying[0].communicate(timeout=10)
        self.finish(successor)
        self.assertNotIn('held by alice', self.b('claims').stdout)
        self.b('--as', 'carol', 'guard', 'formal/X.thy')

    def test_displaced_stale_ancestor_cannot_revive_or_release_successor(self):
        self.claim('alice', 'formal/')
        self.stale('alice')
        self.claim('bob', 'formal/X.thy')
        self.b('--as', 'alice', 'who')
        self.b('--as', 'bob', 'guard', 'formal/X.thy')
        self.b('--as', 'alice', 'release', '--all')
        self.assertIn('held by bob', self.b('claims').stdout)
        self.assertNotEqual(self.b('--as', 'alice', 'release', 'formal/X.thy', check=False).returncode, 0)

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
        self.claim('alice', str(self.repo / 'formal/X.thy'))
        self.assertNotEqual(self.b('--as', 'bob', 'guard', './X.thy', cwd=self.repo / 'formal', check=False).returncode, 0)
        self.b('--as', 'alice', 'release', 'X.thy', cwd=self.repo / 'formal')
        self.claim('alice', '..', cwd=self.repo / 'formal')
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'PLAN.md', check=False).returncode, 0)
        self.b('--as', 'alice', 'release', '.')
        self.claim('alice', 'formal')
        (self.repo / 'formal/X.thy').unlink()
        (self.repo / 'formal').rmdir()
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'formal/new/deep', check=False).returncode, 0)
        self.b('--as', 'alice', 'release', 'formal')
        self.b('--as', 'bob', 'guard', 'formal/new/deep')
        self.claim('alice', 'future/')
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'future/new', check=False).returncode, 0)
        for path in ('../outside-new', str(self.base / 'outside-new'), '../../'):
            self.assertEqual(self.claim('bob', path, check=False).returncode, 2)
        self.b('--project-root', str(self.repo), '--as', 'bob', 'claim', '--reason', 'root override', 'New.md', cwd=self.base)
        self.assertIn('New.md  held by bob', self.b('claims').stdout)

    def test_symlinks_tokens_and_fragments_are_distinct(self):
        (self.repo / 'link').symlink_to('formal')
        self.claim('alice', 'link')
        self.b('--as', 'bob', 'guard', 'formal/X.thy')
        self.assertEqual(self.claim('bob', 'link/X.thy', check=False).returncode, 2)
        self.claim('alice', 'jedit', 'token:server', 'PLAN.md#intro')
        self.claim('bob', 'path:jedit', 'PLAN.md#body', 'PLAN.md')
        self.b('--as', 'bob', 'guard', 'PLAN.md')
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'token:jedit', check=False).returncode, 0)
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

    def test_migration_recovers_killed_legacy_claim_and_replays_cursors(self):
        for name in ('agents', 'claims', 'posts', 'cursors'):
            (self.board / name).mkdir(parents=True)
        (self.board / 'agents/alice').write_text(f'handle=alice\nworktree={self.repo}\nbranch=main\ntask=legacy\nsince=then\n')
        complete = self.board / 'claims/complete'
        complete.mkdir()
        for key, value in dict(resource='PLAN.md', owner='alice', reason='old lease', since='then').items():
            (complete / key).write_text(value + '\n')
        incomplete = self.board / 'claims/interrupted'
        # Terminate a real writer after mkdir but before it can finish fields.
        writer = subprocess.Popen(['python3', '-c',
            "import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir(); print('ready', flush=True); sys.stdin.read()", str(incomplete)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(writer.stdout.readline(), 'ready\n')
        writer.kill()
        writer.communicate(timeout=10)
        body = 'time=then\nfrom=alice\nkind=note\nre=\n\nlegacy message\n'
        (self.board / 'posts/20000101-alice.md').write_text(body)
        (self.board / 'cursors/reader').write_text('99999999-alice.md\n')
        self.assertIn('legacy board', self.b('claims', check=False).stderr)
        self.assertEqual(self.b('migrate', check=False).returncode, 2)
        out = self.b('migrate', '--writers-stopped').stdout
        self.assertIn('recovered incomplete legacy claim', out)
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'PLAN.md', check=False).returncode, 0)
        self.assertIn('legacy message', self.b('digest', '--cursor', 'reader', '--mark').stdout)
        self.claim('bob', 'free')
        self.b('migrate', '--writers-stopped')
        self.assertEqual(self.b('digest', '--cursor', 'reader').stdout.count('legacy message'), 0)
        self.assertEqual((self.board / 'posts/20000101-alice.md').read_text(), body)
        (self.board / 'state.json').write_text('{"version": 2, "claims": [null], "sequence": 0}')
        self.assertIn('malformed authoritative', self.b('guard', 'PLAN.md', check=False).stderr)

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

    def test_interrupted_migration_can_be_retried(self):
        for folder in ('agents', 'claims', 'posts', 'cursors'):
            (self.board / folder).mkdir(parents=True)
        (self.board / 'posts/old.md').write_text('time=then\nfrom=alice\nkind=note\nre=\n\nold message\n')
        migrating = self.probe(['migrate', '--writers-stopped'], 'replace', 'state.json')
        self.arrived(migrating)
        migrating[0].kill()
        migrating[0].communicate(timeout=10)
        self.b('migrate', '--writers-stopped')
        out = self.b('digest', '--cursor', 'reader', '--mark').stdout
        self.assertEqual(out.count('old message'), 1)
        self.b('--as', 'bob', 'post', 'new message')
        out = self.b('digest', '--cursor', 'reader', '--mark').stdout
        self.assertIn('new message', out)
        self.assertNotIn('old message', out)

    def test_corruption_is_rejected_with_python_optimization(self):
        self.claim('alice', 'PLAN.md')
        state = json.loads((self.board / 'state.json').read_text())
        state['version'] = 99
        (self.board / 'state.json').write_text(json.dumps(state))
        self.env['PYTHONOPTIMIZE'] = '1'
        result = self.b('--as', 'bob', 'guard', 'PLAN.md', check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unsupported format version', result.stderr)

    def test_missing_authoritative_snapshot_cannot_be_remigrated(self):
        self.claim('alice', 'PLAN.md')
        (self.board / 'state.json').unlink()
        result = self.b('migrate', '--writers-stopped', check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('missing authoritative state', result.stderr)
        self.assertFalse((self.board / 'state.json').exists())

    def test_ref_and_reserved_path_spellings_cannot_bypass_claims(self):
        self.claim('alice', 'refs//heads/main', 'path:jedit')
        self.assertNotEqual(self.b('--as', 'bob', 'guard', 'refs/heads/main', check=False).returncode, 0)
        self.assertNotEqual(self.b('--as', 'bob', 'guard', './jedit', check=False).returncode, 0)
        self.b('--as', 'alice', 'release', 'path:jedit')
        self.b('--as', 'bob', 'guard', './jedit')

    def test_release_batch_preserves_ownership_on_conflict(self):
        self.claim('alice', 'PLAN.md')
        self.claim('bob', 'formal/')
        self.assertEqual(self.b('--as', 'alice', 'release', 'PLAN.md', 'formal/', check=False).returncode, 2)
        self.assertNotEqual(self.b('--as', 'carol', 'guard', 'PLAN.md', check=False).returncode, 0)

    def test_doctor_reports_both_hooks_and_errors_without_renewal(self):
        # Copy the tiny tooling surface: doctor's temporary files and all
        # diagnoses stay in the fixture, never in the development checkout.
        tooling = self.base / 'tooling'
        scripts = tooling / 'scripts'
        scripts.mkdir(parents=True)
        for name in ('doctor.sh', 'common.sh', 'board.sh', 'board.py'):
            shutil.copy2(ROOT / 'scripts' / name, scripts / name)
        self.env['ISABELLE_TOOLING_ISABELLE'] = '/nonexistent/isabelle'
        self.env['XDG_CONFIG_HOME'] = str(self.base / 'config')
        self.env['CODEX_HOME'] = str(self.base / 'codex')
        self.env['CLAUDE_CONFIG_DIR'] = str(self.base / 'claude')
        (self.repo / 'isabelle-tooling.conf').write_text(
            'format_version=1\nsource_rel=.\nformal_rel=formal\nbuild_session=Test\n'
            'session_dir=.\nic2_base_session=HOL\nisabelle_version=Isabelle2025-2\n')
        (self.repo / 'formal/ROOT').write_text('')
        self.claim('alice', 'PLAN.md')
        self.run_cmd([scripts / 'board.sh', 'install-hook'])
        self.stale('alice')
        result = self.run_cmd([scripts / 'doctor.sh', '--project-root', self.repo], agent='alice', check=False)
        self.assertIn('[OK]   Coordination board format 2', result.stdout)
        self.assertIn('[OK]   Board pre-commit guard installed:', result.stdout)
        self.assertIn('[OK]   Board reference-transaction guard installed:', result.stdout)
        self.assertEqual((self.board / 'agents/alice').stat().st_mtime, 1)
        (self.board / 'state.json').write_text('broken json')
        result = self.run_cmd([scripts / 'doctor.sh', '--project-root', self.repo], check=False)
        self.assertIn('[FAIL] Coordination board needs attention:', result.stdout)
        self.assertNotIn('[OK]   Coordination board', result.stdout)


if __name__ == '__main__':
    unittest.main()
