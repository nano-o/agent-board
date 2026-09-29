#!/usr/bin/env python3
"""Local coordination board. All authoritative decisions use the stable flock.

bin/agent-board is the public entry point. Only stdlib and Git are required.
Output and stdin delivery happen outside the lock; a digest acknowledges after
flush.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

if sys.version_info < (3, 9):
    sys.exit('agent-board: needs Python 3.9 or later')

VERSION = 2  # the on-disk state format
INTERFACE = 1  # verbs, options, exit codes, environment, machine output
CAPABILITIES = []  # additive features beyond the interface
MARKER = '# agent-board guard: remove with agent-board uninstall-hook.'
EXECUTABLE = Path(__file__).resolve().parents[1] / 'bin' / 'agent-board'
HANDLE = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}\Z')
CURSOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,80}\Z')
KIND = re.compile(r'[a-z][a-z-]{0,23}\Z')


class BoardError(Exception):
    pass


def git(root, *args, optional=False):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True)
    if result.returncode and not optional:
        raise BoardError(result.stderr.decode(errors='replace').strip())
    return result.stdout.decode(errors='surrogateescape').rstrip('\n') if not result.returncode else ''


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def one_line(value):
    return value.replace('\n', ' ').replace('\r', ' ')


def atomic_write(path, body, mode=0o600):
    """Replace only complete files; fsync data and directory before returning."""
    fd, name = tempfile.mkstemp(prefix='.tmp.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', errors='surrogateescape') as stream:
            stream.write(body)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def fields(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)


def resource(raw, start, root):
    if not raw or '\n' in raw or '\r' in raw or '\x00' in raw:
        raise BoardError('empty resource or unsupported control character in resource')
    raw, sep, fragment = raw.partition('#')
    fragment = fragment if sep else None
    if sep and not fragment:
        raise BoardError('empty resource fragment')
    if raw.startswith('refs/'):
        normalized = git(root, 'check-ref-format', '--normalize', raw, optional=True)
        if not normalized:
            raise BoardError(f'invalid ref: {raw}')
        return dict(type='ref', name=normalized, directory=False, fragment=fragment)
    if raw.startswith('token:'):
        name = raw.removeprefix('token:')
        if not HANDLE.fullmatch(name):
            raise BoardError(f'invalid token: {name}')
        return dict(type='token', name=name, directory=False, fragment=fragment)
    raw = raw.removeprefix('path:')
    candidate = Path(os.path.abspath(os.path.join(start, raw)))
    try:
        rel = candidate.relative_to(root)
    except ValueError:
        raise BoardError(f'resource lies outside the worktree {root}: {raw}') from None
    # Git tracks the symlink itself, never a path reached through it.
    for parent in candidate.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise BoardError(f'resource traverses a symlink: {raw}')
    name = '' if rel == Path('.') else rel.as_posix()
    directory = not name or raw.endswith('/') or (candidate.is_dir() and not candidate.is_symlink())
    return dict(type='path', name=name, directory=directory, fragment=fragment)


def label(res):
    if res['type'] == 'path':
        text = res['name'] or '.'
        if text.startswith(('refs/', 'token:', 'path:')):
            text = 'path:' + text
        if res['directory']:
            text += '/'
    elif res['type'] == 'token':
        text = 'token:' + res['name']
    else:
        text = res['name']
    return text + ('#' + res['fragment'] if res['fragment'] is not None else '')


def overlaps(a, b):
    if a['type'] != b['type']:
        return False
    if a['name'] == b['name']:
        return True
    return a['type'] == 'path' and (
        (a['directory'] and (not a['name'] or b['name'].startswith(a['name'] + '/'))) or
        (b['directory'] and (not b['name'] or a['name'].startswith(b['name'] + '/'))))


def same_release(a, b):
    # Directory syntax can be lost after deletion; exact path still identifies it.
    return all(a[key] == b[key] for key in ('type', 'name', 'fragment'))


class Board:
    def __init__(self, path, root, stale_minutes=180):
        self.path, self.root = Path(path), Path(root)
        self.stale_seconds = stale_minutes * 60
        self.state = None

    def exists(self):
        # Any entry besides the lock inode means a board, complete or not.
        return self.path.is_dir() and any(e.name != '.lock' for e in self.path.iterdir())

    @contextmanager
    def locked(self, *, create=False):
        if create:
            self.path.mkdir(parents=True, exist_ok=True)
        # This inode is stable for the life of the board. Never unlink it.
        with (self.path / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = self.path / 'state.json'
                if state.exists():
                    try:
                        self.state = json.loads(state.read_text())
                    except ValueError as exc:
                        raise BoardError(f'malformed authoritative board state; repair from backup: {exc}') from exc
                    self.validate()
                elif self.exists():
                    # Never repaired or restarted automatically: what remains may be real state.
                    raise BoardError(f'incomplete board state at {self.path}: state.json is missing. '
                                     'Restore it from a backup with clients stopped, or remove the '
                                     'directory if the board held nothing worth keeping')
                else:
                    for folder in ('agents', 'posts', 'messages', 'cursors-v2'):
                        (self.path / folder).mkdir(exist_ok=True)
                    self.state = dict(version=VERSION, claims=[], sequence=0)
                    self.save()
                marker = self.path / 'format'
                if marker.exists() and marker.read_text() != f'{VERSION}\n':
                    raise BoardError('unsupported or malformed board format marker')
                if not marker.exists():
                    atomic_write(marker, f'{VERSION}\n')
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def validate(self):
        # Never use assertions for on-disk validation: Python -O removes them.
        def require(condition, detail):
            if not condition:
                raise ValueError(detail)
        try:
            require(type(self.state['version']) is int and self.state['version'] == VERSION,
                    'unsupported format version')
            require(type(self.state['sequence']) is int and self.state['sequence'] >= 0,
                    'invalid sequence')
            require(isinstance(self.state['claims'], list), 'invalid claims list')
            for claim in self.state['claims']:
                require(HANDLE.fullmatch(claim['owner']), 'invalid claim owner')
                require(isinstance(claim['reason'], str) and claim['reason'], 'invalid reason')
                require(isinstance(claim['since'], str) and claim['since'], 'invalid claim time')
                r = claim['resource']
                require(r['type'] in ('path', 'ref', 'token') and isinstance(r['name'], str),
                        'invalid resource type or name')
                require(type(r['directory']) is bool, 'invalid directory flag')
                require(r['fragment'] is None or isinstance(r['fragment'], str) and r['fragment'],
                        'invalid fragment')
                if r['type'] == 'path':
                    require(not r['name'].startswith('/') and
                            not any(x in ('.', '..') for x in r['name'].split('/')),
                            'noncanonical path')
                    require((r['name'] == '' and r['directory']) or
                            (r['name'] != '' and all(r['name'].split('/'))), 'invalid path')
                else:
                    require(r['name'] and not r['directory'], 'invalid ref/token')
                    if r['type'] == 'token':
                        require(HANDLE.fullmatch(r['name']), 'invalid token')
                    else:
                        require(r['name'].startswith('refs/') and
                                git(self.root, 'check-ref-format', '--normalize', r['name'], optional=True) == r['name'],
                                'invalid ref')
            for folder in ('agents', 'posts', 'messages', 'cursors-v2'):
                require((self.path / folder).is_dir(), f'missing {folder}')
        except (ValueError, KeyError, TypeError) as exc:
            raise BoardError(f'malformed authoritative board state; repair from backup: {exc}') from exc

    def save(self):
        atomic_write(self.path / 'state.json', json.dumps(self.state, indent=2) + '\n')

    def agents(self):
        result = {}
        for path in sorted((self.path / 'agents').iterdir()):
            if path.name.startswith('.'):
                continue
            data = fields(path)
            if not HANDLE.fullmatch(path.name) or not all(k in data for k in ('worktree', 'task', 'branch', 'since')):
                raise BoardError(f'malformed presence: {path}')
            data['mtime'] = path.stat().st_mtime
            result[path.name] = data
        return result

    def stale(self, owner, agents):
        return owner not in agents or time.time() - agents[owner]['mtime'] > self.stale_seconds

    def presence(self, handle, worktree, branch, task):
        body = f'handle={handle}\nworktree={worktree}\nbranch={branch}\ntask={one_line(task)}\nsince={now()}\n'
        atomic_write(self.path / 'agents' / handle, body)

    def renew(self, handle):
        if handle and (self.path / 'agents' / handle).exists():
            os.utime(self.path / 'agents' / handle, None)

    def infer(self, agents):
        candidates = [h for h, a in agents.items() if a['worktree'] == str(self.root) and not self.stale(h, agents)]
        return candidates[0] if len(candidates) == 1 else ''

    def post(self, handle, kind, re_resource, message):
        # Reserve durably BEFORE publishing. Death may leave a gap, never reuse.
        self.state['sequence'] += 1
        self.save()
        name = f"{self.state['sequence']:020d}.md"
        body = f'time={now()}\nfrom={handle}\nkind={kind}\nre={re_resource}\n\n{message}\n'
        atomic_write(self.path / 'messages' / name, body)
        return name

    def posts(self):
        result = []
        for path in sorted((self.path / 'messages').iterdir()):
            if path.name.startswith('.'):
                continue
            if not re.fullmatch(r'[0-9]{20}\.md', path.name) or int(path.stem) > self.state['sequence']:
                raise BoardError(f'malformed post sequence: {path.name}')
            result.append((int(path.stem), path.read_text()))
        return result

    def cursor(self, name):
        path = self.path / 'cursors-v2' / name
        if not path.exists():
            return 0
        try:
            value = int(path.read_text())
            if value < 0 or value > self.state['sequence']:
                raise ValueError()
            return value
        except ValueError:
            raise BoardError(f'malformed cursor: {name}') from None

    def acknowledge(self, name, number):
        with self.locked():
            atomic_write(self.path / 'cursors-v2' / name, str(max(self.cursor(name), number)) + '\n')

    def render_agents(self, agents):
        text = ''
        for h, a in agents.items():
            active = datetime.fromtimestamp(a['mtime'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            mark = '  [stale]' if self.stale(h, agents) else ''
            text += f"  {h}  {a['worktree']} ({a['branch']})  last active {active}{mark}\n    {a['task']}\n"
        return text

    def render_claims(self, agents):
        text = ''
        for c in self.state['claims']:
            mark = '  [stale]' if self.stale(c['owner'], agents) else ''
            text += f"  {label(c['resource'])}  held by {c['owner']} since {c['since']}{mark}\n    {c['reason']}\n"
        return text

    def claim(self, handle, resources, reason, force, agents):
        displaced = []
        for c in self.state['claims']:
            if c['owner'] == handle or c['resource']['fragment'] is not None:
                continue
            if any(r['fragment'] is None and overlaps(c['resource'], r) for r in resources):
                if not force and not self.stale(c['owner'], agents):
                    return 1, f"HELD: {label(c['resource'])} is held by {c['owner']}: {c['reason']}\n"
                displaced.append(c)
        # Retire overlapping predecessors, including ancestors, permanently.
        self.state['claims'] = [c for c in self.state['claims'] if c not in displaced]
        output = ''
        for r in resources:
            previous = next((c for c in self.state['claims'] if c['owner'] == handle and c['resource'] == r), None)
            if previous:
                previous['reason'] = reason
                output += f'{label(r)}: already held by you; reason updated\n'
            else:
                self.state['claims'].append(dict(resource=r, owner=handle, reason=reason, since=now()))
                output += f'claimed {label(r)}\n'
        for c in displaced:
            how = 'stale owner' if self.stale(c['owner'], agents) else 'forced'
            output += f"took over {label(c['resource'])} from {c['owner']} ({how})\n"
        self.save()
        self.post(handle, 'claim', ','.join(map(label, resources)), output.strip() + ': ' + reason)
        return 0, output

    def release(self, handle, resources, all_resources=False):
        selected = [c for c in self.state['claims'] if
                    (all_resources and c['owner'] == handle) or
                    any(same_release(c['resource'], r) for r in resources)]
        for c in selected:
            if c['owner'] != handle:
                raise BoardError(f"{label(c['resource'])} is held by {c['owner']}, not by you")
        self.state['claims'] = [c for c in self.state['claims'] if c not in selected]
        self.save()
        return [label(c['resource']) for c in selected]

    def guard(self, handle, targets, agents, inferred):
        blocked, messages = False, ''
        for c in self.state['claims']:
            if c['owner'] == handle:
                continue
            target = next((r for r in targets if overlaps(c['resource'], r)), None)
            if target is None:
                continue
            res = label(c['resource'])
            if c['resource']['fragment'] is not None:
                messages += f"agent-board: note: {c['owner']} holds a passage of {label(target)} ({c['resource']['fragment']}): {c['reason']}\n"
            elif self.stale(c['owner'], agents):
                messages += f"agent-board: ignoring stale claim on {res} by {c['owner']}\n"
            else:
                messages += f"agent-board: refusing: {res} is claimed by {c['owner']} since {c['since']}: {c['reason']}\n"
                blocked = True
        if blocked:
            if not handle:
                messages += 'agent-board: your handle is unknown; set AGENT_BOARD_AGENT or pass --as\n'
            elif inferred:
                messages += f'agent-board: you are taken to be {handle}, the agent registered for this worktree\n'
            messages += 'agent-board: wait for release or coordinate with a post. Ref rejection may leave index/worktree changes; inspect them before continuing.\n'
        return int(blocked), messages


def render_posts(posts):
    output = ''
    for _, contents in posts:
        header, _, body = contents.partition('\n\n')
        meta = dict(line.split('=', 1) for line in header.splitlines() if '=' in line)
        re_text = '  re: ' + meta['re'] if meta.get('re') else ''
        output += f"- {meta.get('time', '')}  {meta.get('from', '')}  [{meta.get('kind', '')}]{re_text}\n"
        output += ''.join('    ' + line + '\n' for line in body.splitlines())
    return output


def parser():
    p = argparse.ArgumentParser(prog='agent-board', description='Repository coordination board: presence, posts and atomic leases.',
        epilog='Paths are relative to the invocation directory (or --project-root). Use the worktree root for whole-worktree coverage, '
        'trailing / for directories, refs/heads/NAME, token:NAME, path:NAME to disambiguate, '
        'and path#fragment for advisory passages. Leases expire after AGENT_BOARD_STALE_MINUTES (180). '
        'Explicit valid handles renew existing presence after argument validation, even on conflicts; '
        'anonymous observers do not. Git guards infer and renew a unique active worktree owner. '
        'Hooks guard commits and prepared ref transactions; edits and branch rename destinations need cooperative guards.')
    p.add_argument('--project-root')
    p.add_argument('--as', dest='handle', default=os.environ.get('AGENT_BOARD_AGENT', ''))
    p.add_argument('--if-board', action='store_true')
    sub = p.add_subparsers(dest='action', required=True)
    for name in ('path', 'who', 'claims', 'uninstall-hook'):
        sub.add_parser(name)
    q = sub.add_parser('hello'); q.add_argument('--task'); q.add_argument('--worktree')
    q = sub.add_parser('bye'); q.add_argument('message', nargs='*')
    q = sub.add_parser('post'); q.add_argument('--kind', default='note'); q.add_argument('--re', default=''); q.add_argument('message', nargs='*')
    q = sub.add_parser('show'); q.add_argument('--last', type=int, default=20); q.add_argument('--all', action='store_true')
    q = sub.add_parser('digest', help='Emit ALL unread posts, or all posts with --full; mark only after successful output')
    q.add_argument('--cursor'); q.add_argument('--mark', action='store_true'); q.add_argument('--full', action='store_true')
    q = sub.add_parser('claim'); q.add_argument('--force', action='store_true'); q.add_argument('--reason'); q.add_argument('resources', nargs='*')
    q = sub.add_parser('release'); q.add_argument('--all', action='store_true'); q.add_argument('resources', nargs='*')
    q = sub.add_parser('guard'); q.add_argument('--staged', action='store_true'); q.add_argument('resources', nargs='*')
    q = sub.add_parser('guard-refs', help='Internal reference-transaction guard; reads full stdin before locking'); q.add_argument('state')
    q = sub.add_parser('install-hook', help='Install pre-commit and reference-transaction hooks'); q.add_argument('--force', action='store_true')
    q = sub.add_parser('version', help='Print the interface, state format, capabilities and executable'); q.add_argument('--json', action='store_true')
    return p


def hook_text(name, executable=EXECUTABLE):
    cli = shlex.quote(str(executable))
    preamble = f'''#!/usr/bin/env bash
{MARKER}
hook_dir="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd -P)"
'''
    if name == 'pre-commit':
        return preamble + f'''if [[ -x "$hook_dir/pre-commit.pre-board" ]]; then
  "$hook_dir/pre-commit.pre-board" "$@" || exit $?
fi
exec {cli} guard --staged
'''
    # Both consumers receive identical bytes, arguments and environment. Do
    # not hold a board lock while the foreign hook runs or stdin is read.
    return preamble + f'''input=$(mktemp) || exit 2
trap 'rm -f -- "$input"' EXIT
cat >"$input" || exit 2
if [[ -x "$hook_dir/reference-transaction.pre-board" ]]; then
  "$hook_dir/reference-transaction.pre-board" "$@" <"$input" || exit $?
fi
{cli} guard-refs "$@" <"$input"
'''


def owned(path):
    # The marker line identifies a guard; any other hook is foreign.
    if not path.is_file():
        return False
    lines = path.read_text(errors='replace').splitlines()
    return len(lines) > 1 and lines[1] == MARKER


def hooks(root, action, force=False):
    directory = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks'))
    common = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    # Never touch a shared, global or tracked hooks directory (core.hooksPath).
    if common not in directory.resolve().parents:
        raise BoardError(f'the hooks directory {directory} lies outside the Git common directory {common}; '
                         'agent-board changes only the repository\'s own hooks')
    directory.mkdir(parents=True, exist_ok=True)
    names = ('pre-commit', 'reference-transaction')
    # Preflight both hooks before moving either one.
    if action == 'install-hook':
        for name in names:
            path = directory / name
            if path.exists() and not owned(path):
                if not force:
                    raise BoardError(f'a {name} hook already exists at {path}; use --force to preserve and chain it')
                if path.with_name(name + '.pre-board').exists():
                    raise BoardError(f'cannot chain: {name}.pre-board already exists')
    output = ''
    if action == 'install-hook':
        # Both guards or neither: undo the first if the second cannot be written.
        done = []
        try:
            for name in names:
                path, backup = directory / name, directory / (name + '.pre-board')
                chained = path.exists() and not owned(path)
                previous = path.read_text() if owned(path) else None
                if chained:
                    path.rename(backup)
                    output += f'chained the previous hook as {backup}\n'
                done.append((path, backup, chained, previous))
                atomic_write(path, hook_text(name), 0o755)
                output += f'installed {path}\n'
        except BaseException:
            for path, backup, chained, previous in reversed(done):
                if chained:
                    backup.replace(path)
                elif previous is not None:
                    atomic_write(path, previous, 0o755)
                else:
                    path.unlink(missing_ok=True)
            raise
        return output
    for name in names:
        path, backup = directory / name, directory / (name + '.pre-board')
        if owned(path):
            if backup.exists():
                backup.replace(path)
                output += f'removed the board hook and restored the previous {path}\n'
            else:
                path.unlink()
                output += f'removed {path}\n'
        else:
            output += f'no board hook installed at {path}\n'
    return output


def checkout_git(*args):
    # Ignore the calling repository's variables, e.g. GIT_DIR inside a hook.
    env = {k: v for k, v in os.environ.items() if k not in (
        'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
        'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE')}
    result = subprocess.run(['git', '-C', str(EXECUTABLE.parents[1]), *args], capture_output=True, env=env)
    return result.returncode, result.stdout.decode(errors='surrogateescape').rstrip('\n')


def version(as_json):
    checkout = EXECUTABLE.parents[1]
    commit = clean = None
    code, top = checkout_git('rev-parse', '--show-toplevel')
    # Only a checkout of agent-board itself has a commit, not a copy inside another repository.
    if not code and Path(top).resolve() == checkout:
        code, head = checkout_git('rev-parse', '--verify', '-q', 'HEAD')
        if not code:
            code, status = checkout_git('status', '--porcelain', '--ignore-submodules=none')
            if code:
                raise BoardError(f'git status failed in {checkout}')
            commit, clean = head, not status
    if as_json:
        return json.dumps(dict(interface=INTERFACE, state_format=VERSION, capabilities=CAPABILITIES,
                               commit=commit, clean=clean, executable=str(EXECUTABLE))) + '\n'
    where = f"commit {commit} ({'clean' if clean else 'modified'})" if commit else 'not a Git checkout'
    return (f"agent-board interface {INTERFACE}, state format {VERSION}, "
            f"capabilities: {' '.join(CAPABILITIES) or 'none'}; {where}; executable {EXECUTABLE}\n")


def execute(args):
    action, handle = args.action, args.handle
    if action == 'version':
        return 0, version(args.json), '', None
    if handle and not HANDLE.fullmatch(handle):
        raise BoardError(f'invalid handle: {handle}')
    start = Path(args.project_root or os.getcwd()).resolve()
    if not start.is_dir():
        raise BoardError(f'--project-root: not a directory: {start}')
    top = git(start, 'rev-parse', '--show-toplevel', optional=True)
    common = git(start, 'rev-parse', '--path-format=absolute', '--git-common-dir', optional=True)
    root = Path(top) if top else start
    override = os.environ.get('AGENT_BOARD_DIR', '')
    if not override and not common:
        raise BoardError(f'not inside a Git worktree: {start}; pass --project-root or set AGENT_BOARD_DIR')
    path = Path(override) if override else Path(common) / 'agent-board'
    stale_minutes = os.environ.get('AGENT_BOARD_STALE_MINUTES', '180')
    if not re.fullmatch('[0-9]+', stale_minutes):
        raise BoardError('AGENT_BOARD_STALE_MINUTES must be a non-negative integer')
    board = Board(path, root, int(stale_minutes))
    if action in ('install-hook', 'uninstall-hook'):
        if not common:
            raise BoardError('hook installation needs a Git repository')
        return 0, hooks(root, action, getattr(args, 'force', False)), '', None
    if action in ('hello', 'bye', 'post', 'claim', 'release') and not handle:
        raise BoardError('this action needs an agent handle: --as HANDLE or AGENT_BOARD_AGENT')
    # Argument/path/stdin validation is deliberately before locking or renewal.
    resources = [resource(r, str(start), root) for r in getattr(args, 'resources', [])]
    message = ' '.join(getattr(args, 'message', []))
    if action == 'post':
        if not KIND.fullmatch(args.kind):
            raise BoardError(f'invalid kind: {args.kind}')
        if args.message == ['-']:
            message = sys.stdin.read()
        if not message.strip():
            raise BoardError('post needs a message (or - to read stdin)')
        re_resource = label(resource(args.re, str(start), root)) if args.re else ''
    if action == 'hello' and not args.task:
        raise BoardError('hello needs --task TEXT')
    if action == 'claim':
        if not args.reason or not args.reason.strip():
            raise BoardError('claim needs --reason TEXT')
        if not resources:
            raise BoardError('claim needs at least one RESOURCE')
    if action == 'release' and (bool(resources) == args.all):
        raise BoardError('release needs RESOURCE... or --all')
    if action == 'show' and args.last < 0:
        raise BoardError('--last must be non-negative')
    if action == 'digest':
        args.cursor = args.cursor or handle
        if not args.cursor or not CURSOR.fullmatch(args.cursor):
            raise BoardError('invalid cursor name: use --cursor NAME or an agent handle')
    if action == 'guard-refs':
        lines = sys.stdin.read().splitlines()
        if args.state != 'prepared':
            return 0, '', '', None
        for line in lines:
            parts = line.split()
            if len(parts) != 3:
                raise BoardError('malformed reference transaction')
            ref = parts[2]
            if ref.startswith('refs/'):
                resources.append(resource(ref, str(root), root))
            # HEAD is per-worktree, not a shared branch resource.
    if action == 'guard' and args.staged and board.exists():
        staged = git(root, 'diff', '--cached', '--name-only', '--no-renames', '-z')
        resources.extend(resource('path:' + p, str(root), root) for p in staged.split('\x00') if p)
        ref = git(root, 'symbolic-ref', '-q', 'HEAD', optional=True)
        if ref:
            resources.append(resource(ref, str(root), root))
    create = action in ('hello', 'post', 'claim')
    if not board.exists():
        if args.if_board:
            return 0, '', '', None
        if action == 'path':
            return 0, str(path) + '\n', '', None
        if not create:
            if action in ('who', 'show', 'claims'):
                return 0, f'no board yet at {path}\n', '', None
            if action in ('bye', 'release'):
                raise BoardError(f'no board at {path}')
            return 0, '', '', None
    worktree = Path(getattr(args, 'worktree', None) or root).resolve()
    if action == 'hello' and not worktree.is_dir():
        raise BoardError(f'--worktree: not a directory: {worktree}')
    branch = git(worktree, 'symbolic-ref', '-q', '--short', 'HEAD', optional=True) or 'detached'
    out, err, code, ack = '', '', 0, None
    with board.locked(create=create):
        agents = board.agents()
        inferred = action in ('guard', 'guard-refs') and not handle
        if inferred:
            handle = board.infer(agents)
        if action != 'bye':
            board.renew(handle)
        if action == 'hello' or action == 'claim' and handle not in agents:
            board.presence(handle, worktree, branch, args.task if action == 'hello' else '(no task recorded; use hello --task)')
        agents = board.agents()
        if action == 'path':
            out = str(path) + '\n'
        elif action == 'hello':
            board.post(handle, 'hello', '', f'{one_line(args.task)} (worktree {worktree}, branch {branch})')
            out = f'hello {handle}: {one_line(args.task)} ({worktree}, branch {branch})\n'
        elif action == 'bye':
            released = board.release(handle, [], True)
            (path / 'agents' / handle).unlink(missing_ok=True)
            board.post(handle, 'bye', '', (message or 'leaving') + f" (released: {','.join(released)})")
            out = f"bye {handle}; released: {','.join(released)}\n"
        elif action == 'post':
            out = 'posted ' + board.post(handle, args.kind, re_resource, message) + '\n'
        elif action == 'who':
            out = board.render_agents(agents)
        elif action == 'claims':
            out = board.render_claims(agents)
        elif action == 'claim':
            code, out = board.claim(handle, resources, one_line(args.reason), args.force, agents)
        elif action == 'release':
            released = board.release(handle, resources, args.all)
            if released:
                board.post(handle, 'release', '', 'released: ' + ','.join(released))
            if args.all:
                out = 'released: ' + (','.join(released) or 'nothing held') + '\n'
            else:
                out = ''.join('released ' + r + '\n' for r in released)
                out += ''.join('not claimed: ' + label(r) + '\n' for r in resources if not any(same_release(r, resource(x, str(root), root)) for x in released))
        elif action in ('guard', 'guard-refs'):
            code, err = board.guard(handle, resources, agents, inferred)
        elif action in ('show', 'digest'):
            posts = board.posts()  # One immutable snapshot, also used for marking.
            if action == 'show':
                selected = posts if args.all else (posts[-args.last:] if args.last else [])
                out = f'Coordination board: {path}\n\nAgents ({len(agents)})\n' + board.render_agents(agents)
                out += f"\nClaims ({len(board.state['claims'])})\n" + board.render_claims(agents)
                out += f'\nPosts ({len(selected)} of {len(posts)})\n' + render_posts(selected)
            else:
                seen = 0 if args.full else board.cursor(args.cursor)
                selected = [(n, body) for n, body in posts if n > seen]
                if selected or not seen:
                    out = f'Coordination board ({path}): {len(selected)} new post(s)\n\nAgents\n'
                    out += board.render_agents(agents) + '\nClaims\n' + board.render_claims(agents)
                    if selected:
                        out += '\nPosts\n' + render_posts(selected)
                if args.mark and selected:
                    ack = (board, args.cursor, selected[-1][0])
    return code, out, err, ack


def main():
    try:
        code, out, err, ack = execute(parser().parse_args())
        sys.stderr.write(err)
        sys.stderr.flush()
        sys.stdout.write(out)
        sys.stdout.flush()
        if ack:
            ack[0].acknowledge(ack[1], ack[2])
        return code
    except (BoardError, OSError, ValueError) as exc:
        print(f'agent-board: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    # Retain the original CLI's help alias.
    if sys.argv[1:] == ['help']:
        sys.argv[1] = '--help'
    sys.exit(main())
