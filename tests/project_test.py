#!/usr/bin/env python3
"""Project files and doctor: init, sync, update, remove, sync --check and doctor.

Every test runs a scratch runtime checkout built from this working tree
(stable at the first commit, `next` one commit later with an extra skill
reference), against disposable projects with isolated Git configuration.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1]
DRIVER = '''
import sys
sys.path.insert(0, sys.argv[1])
import project_files
import agent_board
limit = int(sys.argv[2])
def fail(step, op):
    if step == limit:
        raise OSError(f'injected failure after step {step}')
project_files.after_step = fail
sys.argv = ['agent-board'] + sys.argv[3:]
sys.exit(agent_board.main())
'''


def snapshot(root, skip_git=True):
    """Every entry below ROOT: type, content or link target, and mode."""
    found = {}
    for directory, dirs, files in os.walk(root):
        if skip_git and '.git' in dirs and Path(directory) == Path(root):
            dirs.remove('.git')
        for name in dirs + files:
            path = Path(directory) / name
            rel = str(path.relative_to(root))
            info = os.lstat(path)
            if path.is_symlink():
                found[rel] = ('link', os.readlink(path))
            elif path.is_dir():
                found[rel] = ('dir', oct(info.st_mode))
            else:
                found[rel] = ('file', hashlib.sha256(path.read_bytes()).hexdigest(), oct(info.st_mode),
                              info.st_mtime_ns)
    return found


class ProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='board-project-')
        cls.base = Path(cls.tmp.name)
        home = cls.base / 'home'
        home.mkdir()
        bindir = cls.base / 'bin'
        bindir.mkdir()
        cls.env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'AGENT_BOARD_'))}
        cls.env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                       GIT_TERMINAL_PROMPT='0', GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.invalid',
                       GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.invalid',
                       PYTHONDONTWRITEBYTECODE='1', PATH=f"{bindir}:{os.environ['PATH']}")
        cls.runtime = cls.base / 'runtime'
        cls.git_in(cls.base, 'init', '-q', '-b', 'main', str(cls.runtime))
        listed = subprocess.run(['git', '-C', str(SOURCE), 'ls-files', '-z', '-co', '--exclude-standard'],
                                capture_output=True, check=True).stdout.decode().split('\0')
        for rel in filter(None, listed):
            source, target = SOURCE / rel, cls.runtime / rel
            if not os.path.lexists(source):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                os.symlink(os.readlink(source), target)
            else:
                shutil.copy2(source, target)
        cls.git_in(cls.runtime, 'add', '-A')
        cls.git_in(cls.runtime, 'commit', '-qm', 'scratch runtime')
        cls.rev1 = cls.git_in(cls.runtime, 'rev-parse', 'HEAD').strip()
        cls.git_in(cls.runtime, 'branch', 'stable')
        cls.git_in(cls.runtime, 'checkout', '-q', '-b', 'next')
        extra = cls.runtime / 'skills/agent-coordination/references/extra-next.md'
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text('A reference that only the next revision has.\n')
        cls.git_in(cls.runtime, 'add', '-A')
        cls.git_in(cls.runtime, 'commit', '-qm', 'next')
        cls.rev2 = cls.git_in(cls.runtime, 'rev-parse', 'HEAD').strip()
        cls.git_in(cls.runtime, 'checkout', '-q', 'main')
        cls.cli = cls.runtime / 'bin/agent-board'
        os.symlink(cls.cli, bindir / 'agent-board')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def git_in(cls, cwd, *args):
        return subprocess.run(['git', *args], cwd=cwd, env=cls.env, capture_output=True, text=True,
                              check=True).stdout

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix='project-', dir=self.base))
        self.project = self.work / 'project'
        self.project.mkdir()
        self.git('init', '-q', '-b', 'main')

    def tearDown(self):
        shutil.rmtree(self.work)
        if self.git_in(self.runtime, 'rev-parse', 'HEAD').strip() != self.rev1:
            self.git_in(self.runtime, 'checkout', '-q', 'main')

    def git(self, *args, cwd=None):
        return self.git_in(cwd or self.project, *args)

    def commit_all(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'state', '--allow-empty')

    def ab(self, *args, code=0, cwd=None, env=None):
        result = subprocess.run([str(self.cli), *args], cwd=cwd or self.project, env=env or self.env,
                                capture_output=True, text=True, timeout=60)
        if code is not None:
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def check(self, code=0):
        return self.ab('sync', '--check', code=code)

    def read(self, rel):
        return (self.project / rel).read_text()

    def settings(self):
        return json.loads(self.read('.claude/settings.json'))

    def inject(self, limit, *args):
        return subprocess.run(['python3', '-c', DRIVER, str(self.runtime / 'src'), str(limit), *args],
                              cwd=self.project, env=self.env, capture_output=True, text=True, timeout=60)

    # --- installing -------------------------------------------------------------------

    def test_init_in_a_new_repository(self):
        out = self.ab('init').stdout
        self.assertIn(f'agent-board {self.rev1} is installed (copy mode)', out)
        changed = out.split('Changed:\n')[1].split('Review them')[0].split()
        self.assertEqual(changed[-2:], ['.agent-board/inventory.json', 'agent-board.conf'])  # descriptor last
        self.assertIn('git add -A -- ', out)
        self.assertEqual(self.read('agent-board.conf'), '# agent-board project descriptor. Data only: key=value, '
                         f'never sourced.\nformat_version=1\nboard_revision={self.rev1}\n')
        skill = self.project / '.agents/skills/agent-coordination/SKILL.md'
        self.assertEqual(skill.read_bytes(), (self.runtime / 'skills/agent-coordination/SKILL.md').read_bytes())
        self.assertEqual(os.readlink(self.project / '.claude/skills/agent-coordination'),
                         '../../.agents/skills/agent-coordination')
        self.assertTrue(os.access(self.project / '.agent-board/claude-hook.sh', os.X_OK))
        self.assertEqual(os.readlink(self.project / 'CLAUDE.md'), 'AGENTS.md')
        agents = self.read('AGENTS.md')
        self.assertTrue(agents.startswith('<!-- BEGIN agent-board -->\n<!-- Managed by agent-board'))
        self.assertTrue(agents.endswith('<!-- END agent-board -->\n'))
        hooks = self.settings()['hooks']
        for event in ('SessionStart', 'UserPromptSubmit'):
            self.assertEqual(hooks[event], [{'hooks': [{'type': 'command', 'timeout': 15,
                             'command': '"$CLAUDE_PROJECT_DIR/.agent-board/claude-hook.sh"'}]}])
        inventory = json.loads(self.read('.agent-board/inventory.json'))
        self.assertEqual(inventory['revision'], self.rev1)
        self.assertEqual(inventory['directories'], ['.agents/skills/agent-coordination'])
        self.assertEqual(inventory['blocks'][0]['file'], 'AGENTS.md')
        self.assertNotIn('.agent-board/inventory.json', inventory['files'])
        self.check()
        # Nothing staged or committed by the installer.
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
        self.assertIn('already exists', self.ab('init', code=1).stderr)
        self.commit_all()
        self.assertIn('Nothing changed', self.ab('sync').stdout)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_init_merges_with_the_project_files(self):
        (self.project / '.claude').mkdir()
        (self.project / '.claude/settings.json').write_text(json.dumps(
            {'permissions': {'allow': ['Bash(ls)']},
             'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo mine'}]}]}}))
        (self.project / 'AGENTS.md').write_text('# Project\n\nOur rules.\n')
        (self.project / 'CLAUDE.md').write_text('Claude-only notes.\n')
        self.commit_all()
        self.ab('init')
        settings = self.settings()
        self.assertEqual(settings['permissions'], {'allow': ['Bash(ls)']})
        self.assertEqual(settings['hooks']['SessionStart'][0]['hooks'][0]['command'], 'echo mine')
        self.assertIn('.agent-board/claude-hook.sh', json.dumps(settings['hooks']['SessionStart'][1]))
        self.assertTrue(self.read('AGENTS.md').startswith('# Project\n\nOur rules.\n\n<!-- BEGIN agent-board -->'))
        self.assertTrue(self.read('CLAUDE.md').startswith('Claude-only notes.\n\n<!-- BEGIN agent-board -->'))
        self.assertEqual(len(json.loads(self.read('.agent-board/inventory.json'))['blocks']), 2)
        self.check()
        self.commit_all()
        self.ab('remove')
        self.assertEqual(self.read('AGENTS.md'), '# Project\n\nOur rules.\n')
        self.assertEqual(self.read('CLAUDE.md'), 'Claude-only notes.\n')
        self.assertEqual(json.loads(self.read('.claude/settings.json')),
                         {'permissions': {'allow': ['Bash(ls)']},
                          'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo mine'}]}]}})
        for gone in ('agent-board.conf', '.agent-board', '.agents', '.claude/skills'):
            self.assertFalse(os.path.lexists(self.project / gone), gone)

    def test_instruction_file_cases(self):
        (self.project / 'CLAUDE.md').write_text('only Claude\n')
        self.commit_all()
        self.ab('init')
        self.assertIn('<!-- BEGIN agent-board -->', self.read('CLAUDE.md'))
        self.assertTrue(self.read('AGENTS.md').startswith('<!-- BEGIN agent-board -->'))
        self.assertFalse(os.path.islink(self.project / 'AGENTS.md'))

    def test_existing_agents_md_gets_a_claude_symlink(self):
        (self.project / 'AGENTS.md').write_text('rules\n')
        self.commit_all()
        self.ab('init')
        self.assertEqual(os.readlink(self.project / 'CLAUDE.md'), 'AGENTS.md')
        self.ab('remove', code=1)  # the new files are not staged yet
        self.commit_all()
        self.ab('remove')
        self.assertEqual(self.read('AGENTS.md'), 'rules\n')
        self.assertEqual(os.readlink(self.project / 'CLAUDE.md'), 'AGENTS.md')

    def test_shared_claude_skills_alias(self):
        (self.project / '.agents/skills').mkdir(parents=True)
        (self.project / '.agents/skills/theirs').mkdir()
        (self.project / '.agents/skills/theirs/SKILL.md').write_text('theirs\n')
        (self.project / '.claude').mkdir()
        os.symlink('../.agents/skills', self.project / '.claude/skills')
        self.commit_all()
        self.ab('init')
        inventory = json.loads(self.read('.agent-board/inventory.json'))
        self.assertEqual(inventory['claude_skills'], 'shared')
        self.assertEqual(inventory['symlinks'], {})
        self.assertTrue((self.project / '.claude/skills/agent-coordination/SKILL.md').is_file())
        self.check()

    # --- refusals ----------------------------------------------------------------------

    def test_collisions_and_edits_refuse(self):
        (self.project / '.claude').mkdir()
        (self.project / '.claude/settings.json').write_text(json.dumps(
            {'hooks': {'UserPromptSubmit': [{'hooks': [{'type': 'command',
                                                        'command': 'x/.agent-board/claude-hook.sh'}]}]}}))
        self.commit_all()
        out = self.ab('init', code=1).stderr
        self.assertIn('collision: .claude/settings.json /hooks/UserPromptSubmit', out)
        self.assertIn('nothing was changed', out)
        self.assertFalse((self.project / 'agent-board.conf').exists())
        (self.project / '.claude/settings.json').unlink()
        (self.project / '.agents/skills/agent-coordination').mkdir(parents=True)
        (self.project / '.agents/skills/agent-coordination/SKILL.md').write_text('mine\n')
        self.commit_all()
        self.assertIn('collision: .agents/skills/agent-coordination', self.ab('init', code=1).stderr)
        self.git('rm', '-rq', '.agents')
        self.ab('init')
        self.commit_all()
        skill = self.project / '.agents/skills/agent-coordination/SKILL.md'
        skill.write_text(skill.read_text() + 'edited\n')
        self.git('add', '-A')
        self.assertIn('edited managed file: .agents/skills/agent-coordination/SKILL.md', self.ab('sync', code=1).stderr)
        self.assertIn('edited managed file', self.check(1).stdout)
        self.git('checkout', 'HEAD', '--', '.')
        (self.project / '.agents/skills/agent-coordination/stray.md').write_text('stray\n')
        self.git('add', '-A')
        self.assertIn('unmanaged file in the managed directory', self.ab('sync', code=1).stderr)
        self.git('rm', '-q', '--cached', '.agents/skills/agent-coordination/stray.md')
        (self.project / '.agents/skills/agent-coordination/stray.md').unlink()
        settings = self.settings()
        settings['hooks']['SessionStart'][0]['hooks'][0]['timeout'] = 99
        (self.project / '.claude/settings.json').write_text(json.dumps(settings))
        self.git('add', '-A')
        self.assertIn('edited owned entry: .claude/settings.json /hooks/SessionStart', self.ab('remove', code=1).stderr)
        self.git('checkout', 'HEAD', '--', '.')
        (self.project / 'AGENTS.md').write_text(self.read('AGENTS.md').replace('Several agents', 'Many agents'))
        self.git('add', '-A')
        self.assertIn('edited owned block: AGENTS.md', self.ab('update', 'next', code=1).stderr)
        self.git('checkout', 'HEAD', '--', '.')
        self.check()

    def test_preflight_uses_the_index(self):
        (self.project / 'AGENTS.md').write_text('rules\n')
        self.commit_all()
        (self.project / 'AGENTS.md').write_text('rules, edited\n')
        self.assertIn('AGENTS.md has unstaged changes', self.ab('init', code=1).stderr)
        self.git('add', 'AGENTS.md')  # staged changes are accepted
        self.ab('init')
        self.commit_all()
        self.ab('remove')
        self.commit_all()
        (self.project / '.claude').mkdir()
        (self.project / '.claude/settings.json').write_text('{}\n')
        self.assertIn('.claude/settings.json is untracked', self.ab('init', code=1).stderr)
        (self.project / '.claude/settings.json').unlink()
        (self.project / '.gitignore').write_text('.agent-board/\n')
        self.commit_all()
        self.assertIn('.agent-board/claude-hook.sh is ignored by Git', self.ab('init', code=1).stderr)
        self.assertFalse((self.project / '.agent-board').exists())

    def test_symlinked_skill_parent_outside_is_refused(self):
        outside = self.work / 'elsewhere'
        outside.mkdir()
        os.symlink(outside, self.project / '.agents')
        self.commit_all()
        self.assertIn('.agents is a symlink resolving outside the checkout', self.ab('init', code=1).stderr)

    def test_malformed_descriptor_and_missing_inventory(self):
        (self.project / 'agent-board.conf').write_text('format_version=1\nboard_revision=abc\n')
        self.commit_all()
        self.assertIn('full 40-hex commit', self.ab('sync', code=1).stderr)
        (self.project / 'agent-board.conf').write_text(f'format_version=1\nboard_revision={self.rev1}\nextra=1\n')
        self.assertIn('unknown key: extra', self.ab('sync', code=1).stderr)
        (self.project / 'agent-board.conf').write_text(f'format_version=1\nboard_revision={self.rev1}\n')
        self.commit_all()
        self.assertIn('run `agent-board update REV`', self.ab('sync', code=1).stderr)
        self.assertIn('no .agent-board/inventory.json', self.check(1).stdout)
        self.ab('update', 'stable')  # adoption: a descriptor without an inventory
        self.check()
        self.commit_all()
        (self.project / 'agent-board.conf').write_text(f'format_version=1\nboard_revision={"0" * 40}\n')
        self.assertIn('missing from', self.check(1).stdout)
        self.assertIn('missing from', self.ab('sync', code=2).stderr)

    def test_manifestless_revision_cannot_be_installed(self):
        first = self.git_in(self.runtime, 'rev-list', '--max-parents=0', 'HEAD').strip()
        self.git_in(self.runtime, 'checkout', '-q', '--orphan', 'bare')
        self.git_in(self.runtime, 'rm', '-rqf', 'integrations/project')
        self.git_in(self.runtime, 'commit', '-qm', 'no manifest')
        bare = self.git_in(self.runtime, 'rev-parse', 'HEAD').strip()
        self.git_in(self.runtime, 'checkout', '-q', 'main')
        self.assertNotEqual(first, bare)
        self.assertIn('cannot be installed', self.ab('init', '--revision', bare, code=2).stderr)
        self.assertIn('does not name a commit', self.ab('init', '--revision', 'no-such-ref', code=2).stderr)

    # --- revisions, link mode ------------------------------------------------------------

    def test_sync_keeps_the_pin_and_update_moves_it(self):
        self.ab('init')
        self.commit_all()
        self.git_in(self.runtime, 'checkout', '-q', 'next')
        self.assertIn('Nothing changed', self.ab('sync').stdout)
        self.assertIn(self.rev1, self.read('agent-board.conf'))
        self.assertIn('is at', self.check(1).stdout)  # the runtime is not at the pin
        out = self.ab('update', 'next').stdout
        self.assertIn('.agents/skills/agent-coordination/references/extra-next.md', out)
        self.assertIn(f'board_revision={self.rev2}\n', self.read('agent-board.conf'))
        self.check()
        self.commit_all()
        self.git_in(self.runtime, 'checkout', '-q', 'main')
        self.ab('update', 'stable')
        self.assertFalse((self.project / '.agents/skills/agent-coordination/references/extra-next.md').exists())
        self.check()

    def test_link_mode(self):
        self.ab('init')
        self.commit_all()
        dev = self.work / 'dev'
        self.git_in(self.runtime, 'worktree', 'add', '-q', '--detach', str(dev), 'HEAD')
        self.addCleanup(self.git_in, self.runtime, 'worktree', 'remove', '--force', str(dev))
        self.ab('sync', '--link', '--source', str(dev))
        link = self.project / '.agents/skills/agent-coordination'
        self.assertEqual(os.readlink(link), f'{os.path.realpath(dev)}/skills/agent-coordination')
        self.assertTrue((self.project / '.claude/skills/agent-coordination/SKILL.md').is_file())
        self.assertIn('link mode', self.check(1).stdout)
        self.assertIn('[NOTE] link mode', self.ab('sync', '--check', '--allow-dirty').stdout)
        (dev / 'skills/agent-coordination/SKILL.md').write_text('edited in development\n')
        self.assertEqual((self.project / '.claude/skills/agent-coordination/SKILL.md').read_text(),
                         'edited in development\n')
        self.assertIn('link mode', self.ab('update', 'next', code=1).stderr)
        self.ab('sync')
        self.assertFalse(link.is_symlink())
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.check()

    # --- concurrency and failures -------------------------------------------------------------

    def test_two_installers_at_once_are_serialized(self):
        lock = Path(self.git('rev-parse', '--path-format=absolute', '--git-path', 'project-files.lock').strip())
        with open(lock, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            runs = [subprocess.Popen([str(self.cli), 'init'], cwd=self.project, env=self.env, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
            time.sleep(0.5)
            self.assertEqual([r.poll() for r in runs], [None, None])
            self.assertFalse((self.project / 'AGENTS.md').exists())
        results = sorted(r.communicate(timeout=120) and r.returncode for r in runs)
        self.assertEqual(results, [0, 1])
        self.check()

    def test_publication_across_filesystems(self):
        shm = Path('/dev/shm')
        if not shm.is_dir() or os.stat(shm).st_dev == os.stat(self.project).st_dev:
            self.skipTest('no second filesystem for TMPDIR')
        env = dict(self.env, TMPDIR=str(shm))
        self.ab('init', env=env)
        self.check()

    def failure_loop(self, args, finished_code=0):
        """Inject a failure after every publication step; the printed commands restore the tree."""
        template = self.work / 'template'
        shutil.rmtree(template, ignore_errors=True)
        shutil.copytree(self.project, template, symlinks=True)
        checks = []
        for limit in range(1, 100):
            shutil.rmtree(self.project)
            shutil.copytree(template, self.project, symlinks=True)
            before = snapshot(self.project)
            status = self.git('status', '--porcelain', '--ignored', '-uall')
            result = self.inject(limit, *args)
            if result.returncode == 0:
                # Only a failure after the last step, the descriptor, leaves a tree that checks.
                self.assertEqual(checks[:-1], [1] * (limit - 2))
                self.assertEqual(checks[-1], finished_code)
                return limit - 1
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(f'injected failure after step {limit}', result.stderr)
            checks.append(self.check(None).returncode)
            lines = result.stderr.splitlines()
            start = next(i for i, line in enumerate(lines) if line.startswith('Restore them from'))
            commands = []
            for line in lines[start + 1:]:
                if not line.startswith('  '):
                    break
                commands.append(line.strip())
            subprocess.run(['bash', '-ec', '\n'.join(commands)], cwd=self.project, env=self.env, check=True)
            after = snapshot(self.project)
            self.assertEqual({k: v[:3] for k, v in after.items()}, {k: v[:3] for k, v in before.items()},
                             f'step {limit}: {commands}')
            self.assertEqual(self.git('status', '--porcelain', '--ignored', '-uall'), status)
            self.ab(*args)
            self.check(finished_code)
        self.fail('the operation never completed')

    def test_failure_after_each_step_of_init(self):
        (self.project / 'AGENTS.md').write_text('rules\n')
        (self.project / '.claude').mkdir()
        (self.project / '.claude/settings.json').write_text('{"model": "x"}\n')
        self.commit_all()
        self.assertGreaterEqual(self.failure_loop(['init']), 7)

    def test_failure_after_each_step_of_update_and_remove(self):
        self.ab('init')
        self.commit_all()
        self.git_in(self.runtime, 'checkout', '-q', 'next')
        self.assertGreaterEqual(self.failure_loop(['update', 'next']), 3)
        self.git_in(self.runtime, 'checkout', '-q', 'main')
        self.commit_all()
        self.assertGreaterEqual(self.failure_loop(['remove'], finished_code=1), 7)

    # --- doctor -------------------------------------------------------------------------------

    def doctor(self, *args, code=None, env=None):
        result = self.ab('doctor', '--json', *args, code=code, env=env)
        data = json.loads(result.stdout)
        return result.returncode, data, {c['id']: (c['status'], c['message']) for c in data['checks']}

    def test_doctor_reports_every_check(self):
        self.ab('init')
        self.commit_all()
        code, data, checks = self.doctor()
        self.assertEqual(code, 0, data)
        self.assertEqual([c['id'] for c in data['checks']],
                         ['executable.resolved', 'executable.revision', 'executable.clean', 'descriptor', 'files',
                          'storage', 'guard.pre-commit', 'guard.reference-transaction'])
        self.assertEqual(data['interface'], 1)
        self.assertEqual({k: v[0] for k, v in checks.items()},
                         {'executable.resolved': 'ok', 'executable.revision': 'ok', 'executable.clean': 'ok',
                          'descriptor': 'ok', 'files': 'ok', 'storage': 'ok',
                          'guard.pre-commit': 'note', 'guard.reference-transaction': 'note'})
        self.assertIn('no board yet', checks['storage'][1])
        human = self.ab('doctor').stdout
        self.assertIn('[NOTE] guard.pre-commit: no pre-commit guard', human)
        self.assertIn('all checks passed', human)

        self.ab('install-hook')
        self.ab('--as', 'alice', 'claim', '--reason', 'r', 'AGENTS.md')
        code, _, checks = self.doctor()
        self.assertEqual((code, checks['guard.pre-commit'][0], checks['guard.reference-transaction'][0]), (0, 'ok', 'ok'))
        self.assertIn('1 agent(s), 1 claim(s) (0 stale)', checks['storage'][1])
        hooks = Path(self.git('rev-parse', '--path-format=absolute', '--git-path', 'hooks').strip())
        ref = (hooks / 'reference-transaction').read_text()
        (hooks / 'reference-transaction').unlink()
        _, _, checks = self.doctor(code=1)
        self.assertEqual(checks['guard.pre-commit'][0], 'fail')
        self.assertEqual(checks['guard.reference-transaction'][0], 'fail')
        (hooks / 'reference-transaction').write_text(ref.replace('guard-refs', 'guard-refs '))
        self.assertIn('edited or outdated', self.doctor(code=1)[2]['guard.reference-transaction'][1])
        self.ab('uninstall-hook')
        (hooks / 'pre-commit.pre-board').write_text('#!/bin/sh\n')
        self.assertIn('without its guard', self.doctor(code=1)[2]['guard.pre-commit'][1])
        (hooks / 'pre-commit.pre-board').unlink()

    def test_doctor_failures(self):
        _, _, checks = self.doctor(code=1)
        self.assertEqual((checks['descriptor'][0], checks['files'][0]), ('fail', 'fail'))
        self.assertIn('not set up', checks['descriptor'][1])
        self.ab('init')
        self.commit_all()
        other = self.work / 'other-agent-board'
        other.write_text('#!/bin/sh\n')
        other.chmod(0o755)
        env = dict(self.env, AGENT_BOARD_COMMAND=str(other))
        self.assertEqual(self.doctor(code=1, env=env)[2]['executable.resolved'][0], 'fail')
        env = dict(self.env, AGENT_BOARD_COMMAND='relative/agent-board')
        self.assertIn('not the absolute path', self.doctor(code=1, env=env)[2]['executable.resolved'][1])
        env = dict(self.env, PATH='/usr/bin:/bin')
        self.assertIn('not on PATH', self.doctor(code=1, env=env)[2]['executable.resolved'][1])
        self.git_in(self.runtime, 'checkout', '-q', 'next')
        _, _, checks = self.doctor(code=1)
        self.assertEqual((checks['executable.revision'][0], checks['files'][0]), ('fail', 'ok'))
        self.git_in(self.runtime, 'checkout', '-q', 'main')
        (self.runtime / 'README.md').write_text('dirty\n')
        self.addCleanup(self.git_in, self.runtime, 'checkout', '--', 'README.md')
        self.assertEqual(self.doctor(code=1)[2]['executable.clean'][0], 'fail')
        self.assertEqual(self.doctor('--allow-dirty', code=0)[2]['executable.clean'][0], 'note')
        self.git_in(self.runtime, 'checkout', '--', 'README.md')
        self.ab('--as', 'alice', 'hello', '--task', 'x')
        (Path(self.git('rev-parse', '--path-format=absolute', '--git-common-dir').strip())
         / 'agent-board/state.json').unlink()
        self.assertIn('incomplete board state', self.doctor(code=1)[2]['storage'][1])

    def test_doctor_is_read_only(self):
        self.ab('init')
        self.commit_all()
        self.ab('--as', 'alice', 'hello', '--task', 'x')
        self.ab('install-hook')
        status = lambda: (self.git('--no-optional-locks', 'status', '--porcelain', '--ignored'),
                          self.git_in(self.runtime, '--no-optional-locks', 'status', '--porcelain'))
        # Stale stat information: a status that may refresh the index would rewrite it.
        for tree in (self.project, self.runtime):
            os.utime(tree / ('AGENTS.md' if tree == self.project else 'README.md'), (1, 1))
        before_status = status()
        before = snapshot(self.project, skip_git=False), snapshot(self.runtime, skip_git=False)
        self.doctor()
        self.ab('doctor', '--allow-dirty')
        self.check()
        after = snapshot(self.project, skip_git=False), snapshot(self.runtime, skip_git=False)
        for old, new in zip(before, after):
            self.assertEqual({k for k in old.keys() ^ new.keys()}, set())
            self.assertEqual({k for k in old if old[k] != new[k]}, set())
        self.assertEqual(status(), before_status)

    def test_project_root_after_the_verb(self):
        """Isabelle doctor runs `doctor --json --project-root ROOT` from anywhere."""
        elsewhere = self.work / 'elsewhere'
        elsewhere.mkdir()
        self.ab('init', '--project-root', str(self.project), cwd=elsewhere)
        self.commit_all()
        result = self.ab('doctor', '--json', '--project-root', str(self.project), cwd=elsewhere)
        self.assertTrue(json.loads(result.stdout)['ok'])
        self.ab('--project-root', str(self.project), 'sync', '--check', cwd=elsewhere)
        self.ab('sync', '--check', '--project-root', str(self.project), cwd=elsewhere)

    def test_doctor_that_cannot_run_still_prints_an_object(self):
        outside = self.work / 'not-a-repository'
        outside.mkdir()
        result = self.ab('doctor', '--json', cwd=outside, code=2)
        data = json.loads(result.stdout)
        self.assertEqual((data['ok'], [c['id'] for c in data['checks']]), (False, ['doctor']))

    def test_version_capabilities_and_remove_notes(self):
        version = json.loads(self.ab('version', '--json').stdout)
        self.assertEqual(version['capabilities'], ['bounded-digest', 'doctor', 'project'])
        self.assertEqual((version['commit'], version['clean']), (self.rev1, True))
        self.ab('init')
        self.commit_all()
        self.ab('--as', 'alice', 'hello', '--task', 'x')
        self.ab('install-hook')
        out = self.ab('remove').stdout
        self.assertIn('The board data at', out)
        self.assertIn('The Git guards stay installed', out)


if __name__ == '__main__':
    unittest.main()
