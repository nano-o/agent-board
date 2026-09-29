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
import shutil
import subprocess
import sys
import tempfile
import time

if sys.version_info < (3, 9):
    sys.exit('agent-board: needs Python 3.9 or later')

# Doctor creates no file, not even a bytecode cache in this checkout.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_files  # noqa: E402
from project_files import Broken, Refused  # noqa: E402

VERSION = 2  # the on-disk state format
INTERFACE = 1  # verbs, options, exit codes, environment, machine output
CAPABILITIES = ['bounded-digest', 'doctor', 'project']  # additive features beyond the interface
DIGEST_LIMIT = 20
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

    def inspect(self):
        """Doctor's read-only view: (status, message). Creates nothing, not even the lock."""
        if not self.exists():
            return 'ok', f'no board yet at {self.path}; the first hello, post or claim creates it'
        state = self.path / 'state.json'
        if not state.exists():
            return 'fail', (f'incomplete board state at {self.path}: state.json is missing. Restore it from a '
                            'backup with clients stopped, or remove the directory if the board held nothing '
                            'worth keeping')
        lock = self.path / '.lock'
        fd = os.open(lock, os.O_RDONLY) if lock.exists() else None
        try:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_SH)
            try:
                self.state = json.loads(state.read_text())
            except ValueError as exc:
                return 'fail', f'malformed authoritative board state; repair from backup: {exc}'
            self.validate()
            marker = self.path / 'format'
            if not marker.exists() or marker.read_text() != f'{VERSION}\n':
                return 'fail', f'unsupported, malformed or missing board format marker at {marker}'
            agents, posts = self.agents(), self.posts()
            claims = self.state['claims']
            stale = sum(1 for c in claims if self.stale(c['owner'], agents))
            return 'ok', (f'board at {self.path}, format {VERSION}: {len(agents)} agent(s), {len(claims)} claim(s) '
                          f'({stale} stale), {len(posts)} post(s)')
        except BoardError as exc:
            return 'fail', str(exc)
        finally:
            if fd is not None:
                os.close(fd)

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

    def has_cursor(self, name):
        return (self.path / 'cursors-v2' / name).exists()

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


def digest(board, args, agents, posts):
    """(code, output, the post to acknowledge through, or None). Bounded by --limit.

    A cursor never moves past an unread post that was not printed. A new
    cursor, or --full after compaction or resume, gets who holds what and
    the latest posts; a new reader starts there, and older history stays
    available through show --all."""
    known = board.has_cursor(args.cursor)
    seen = board.cursor(args.cursor)
    unread = [p for p in posts if p[0] > seen]
    limit, cursor = args.limit, args.cursor
    state = '\nAgents\n' + board.render_agents(agents) + '\nClaims\n' + board.render_claims(agents)
    if args.full or not known:
        shown = posts[-limit:] if limit else posts
        omitted = [p for p in unread if not shown or p[0] < shown[0][0]]
        out = (f'Coordination board ({board.path}): {len(unread)} unread post(s); who holds what, and the latest '
               f'{len(shown)} of {len(posts)} post(s)\n' + state)
        if shown:
            out += '\nPosts\n' + render_posts(shown)
        if known and omitted:
            out += (f'\n{len(omitted)} older unread post(s) are not shown: run digest --cursor {cursor} --mark '
                    'to read them.\n')
        elif len(posts) > len(shown):
            out += f'\n{len(posts) - len(shown)} earlier post(s) are not shown; show --all lists them.\n'
        mark = posts[-1][0] if posts and (not known or (unread and not omitted)) else None
        return 0, out, mark
    shown = unread[:limit] if limit else unread
    if not shown:
        return 0, '', None
    more = len(unread) - len(shown)
    out = (f'Coordination board ({board.path}): {len(shown)} new post(s)' +
           (f', {more} more unread' if more else '') + '\n' + state + '\nPosts\n' + render_posts(shown))
    if more:
        out += f'\n{more} more unread post(s): run digest --cursor {cursor} --mark again to read them.\n'
    return 0, out, shown[-1][0]


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
    q = sub.add_parser('digest', help='Print unread posts, oldest first and at most --limit (default 20; 0: no limit); '
                       'with --full or a new cursor, who holds what and the latest posts. --mark acknowledges only '
                       'what was printed, after output succeeds')
    q.add_argument('--cursor'); q.add_argument('--mark', action='store_true'); q.add_argument('--full', action='store_true')
    q.add_argument('--limit', type=int, default=DIGEST_LIMIT)
    q = sub.add_parser('claim'); q.add_argument('--force', action='store_true'); q.add_argument('--reason'); q.add_argument('resources', nargs='*')
    q = sub.add_parser('release'); q.add_argument('--all', action='store_true'); q.add_argument('resources', nargs='*')
    q = sub.add_parser('guard'); q.add_argument('--staged', action='store_true'); q.add_argument('resources', nargs='*')
    q = sub.add_parser('guard-refs', help='Internal reference-transaction guard; reads full stdin before locking'); q.add_argument('state')
    q = sub.add_parser('install-hook', help='Install pre-commit and reference-transaction hooks'); q.add_argument('--force', action='store_true')
    q = sub.add_parser('version', help='Print the interface, state format, capabilities and executable'); q.add_argument('--json', action='store_true')
    q = sub.add_parser('init', help='Install the project files, pinned at --revision (default stable)'); q.add_argument('--revision', default='stable')
    q = sub.add_parser('sync', help='Reinstall the pinned project files; --link symlinks the skill to --source; --check only checks')
    q.add_argument('--link', action='store_true'); q.add_argument('--source'); q.add_argument('--check', action='store_true')
    q.add_argument('--allow-dirty', action='store_true', help='with --check: link mode is a note, not a failure')
    q = sub.add_parser('update', help='Pin REV and install its project files'); q.add_argument('revision', metavar='REV')
    sub.add_parser('remove', help='Remove the unchanged project files, the inventory and agent-board.conf')
    q = sub.add_parser('doctor', help='Read-only check of the executable, project files, storage and Git guards')
    q.add_argument('--allow-dirty', action='store_true'); q.add_argument('--json', action='store_true')
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
    env['GIT_OPTIONAL_LOCKS'] = '0'  # status must not refresh the index: doctor writes nothing
    result = subprocess.run(['git', '-C', str(EXECUTABLE.parents[1]), *args], capture_output=True, env=env)
    return result.returncode, result.stdout.decode(errors='surrogateescape').rstrip('\n')


def checkout_state():
    """(commit, clean) of the executable's own checkout, or (None, None)."""
    checkout = EXECUTABLE.parents[1]
    code, top = checkout_git('rev-parse', '--show-toplevel')
    # Only a checkout of agent-board itself has a commit, not a copy inside another repository.
    if not code and Path(top).resolve() == checkout:
        code, head = checkout_git('rev-parse', '--verify', '-q', 'HEAD')
        if not code:
            code, status = checkout_git('status', '--porcelain', '--ignore-submodules=none')
            if code:
                raise BoardError(f'git status failed in {checkout}')
            return head, not status
    return None, None


def version(as_json):
    commit, clean = checkout_state()
    if as_json:
        return json.dumps(dict(interface=INTERFACE, state_format=VERSION, capabilities=CAPABILITIES,
                               commit=commit, clean=clean, executable=str(EXECUTABLE))) + '\n'
    where = f"commit {commit} ({'clean' if clean else 'modified'})" if commit else 'not a Git checkout'
    return (f"agent-board interface {INTERFACE}, state format {VERSION}, "
            f"capabilities: {' '.join(CAPABILITIES) or 'none'}; {where}; executable {EXECUTABLE}\n")


PROJECT_VERBS = ('init', 'sync', 'update', 'remove', 'doctor')
GUARDS = ('pre-commit', 'reference-transaction')
CHECK_ORDER = ('executable.resolved', 'executable.revision', 'executable.clean', 'descriptor', 'files', 'storage',
               'guard.pre-commit', 'guard.reference-transaction')
TAGS = dict(ok='[OK]  ', note='[NOTE]', fail='[FAIL]')


class BoardSpec:
    """agent-board's part of the project files; project_files holds the shared rules."""
    name = command = 'agent-board'
    descriptor = 'agent-board.conf'
    revision_key = 'board_revision'
    inventory_dir = '.agent-board'
    manifest = 'integrations/project/manifest.json'

    def __init__(self):
        self.runtime = project_files.Runtime(EXECUTABLE.parents[1])

    def read_descriptor(self, text):
        values = project_files.parse_key_values(text, self.descriptor)
        for key in values:
            if key not in ('format_version', 'board_revision'):
                raise Refused(f'{self.descriptor}: unknown key: {key}')
        if values.get('format_version') != '1':
            raise Refused(f"{self.descriptor}: unsupported format_version {values.get('format_version')!r} (expected 1)")
        if not project_files.FULL_REVISION.fullmatch(values.get('board_revision', '')):
            raise Refused(f'{self.descriptor}: board_revision must be a full 40-hex commit')
        return values

    def substitutions(self, values):
        return {}


def descriptor_text(revision):
    return ('# agent-board project descriptor. Data only: key=value, never sourced.\n'
            f'format_version=1\nboard_revision={revision}\n')


def resolve_executable():
    """The executable every caller but a Git guard uses: AGENT_BOARD_COMMAND, else PATH."""
    cmd = os.environ.get('AGENT_BOARD_COMMAND', '')
    if cmd:
        if not (cmd.startswith('/') and os.path.isfile(cmd) and os.access(cmd, os.X_OK)):
            return None, f'AGENT_BOARD_COMMAND={cmd} is not the absolute path of an executable file'
    else:
        cmd = shutil.which('agent-board') or ''
        if not cmd.startswith('/'):
            return None, 'agent-board is not on PATH and AGENT_BOARD_COMMAND is not set'
    return Path(os.path.realpath(cmd)), None


def guard_checks(root, executable):
    directory = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks'))
    kinds = {}
    for name in GUARDS:
        path = directory / name
        if owned(path):
            kinds[name] = 'current' if path.read_text(errors='replace') == hook_text(name, executable) else 'stale'
        else:
            kinds[name] = 'foreign' if path.exists() else 'absent'
    installed = [n for n in GUARDS if kinds[n] in ('current', 'stale')]
    checks = []
    for name in GUARDS:
        other = GUARDS[1 - GUARDS.index(name)]
        path, backup = directory / name, directory / (name + '.pre-board')
        if kinds[name] == 'current':
            if kinds[other] in ('current', 'stale'):
                checks.append(('ok', f'{path} is current and calls {executable}'))
            else:
                checks.append(('fail', f'{path} is installed without the {other} guard; run agent-board install-hook'))
        elif kinds[name] == 'stale':
            checks.append(('fail', f'{path} is an edited or outdated guard, or calls another executable than '
                                   f'{executable}; run agent-board install-hook'))
        elif backup.exists():
            checks.append(('fail', f'{backup} exists without its guard; restore it as {name}, or run agent-board '
                                   'install-hook'))
        elif installed:
            checks.append(('fail', f'no {name} guard, but the {other} guard is installed; run agent-board install-hook'
                                   + (' --force to chain the existing hook' if kinds[name] == 'foreign' else '')))
        else:
            checks.append(('note', f'no {name} guard' + (' (a foreign hook is there)' if kinds[name] == 'foreign' else '')
                                   + '; agent-board install-hook installs both guards'))
    return checks


def doctor(args, root, board, spec):
    checks = []

    def add(check_id, status, message):
        checks.append(dict(id=check_id, status=status, message=message))
    resolved, why = resolve_executable()
    if resolved is None:
        add('executable.resolved', 'fail', why)
    elif resolved != EXECUTABLE:
        add('executable.resolved', 'fail', f'agent-board resolves to {resolved}, not to {EXECUTABLE}, which runs '
                                           'this doctor; the digest hook and the ic2 notes use the resolved one')
    else:
        add('executable.resolved', 'ok', f'agent-board resolves to {EXECUTABLE}')
    values = None
    try:
        _, values = project_files.read_descriptor(project_files.Tree(root), spec)
        add('descriptor', 'ok', f"{spec.descriptor} pins {values['board_revision']}")
    except Refused as exc:
        add('descriptor', 'fail', str(exc))
    commit, clean = checkout_state()
    checkout = EXECUTABLE.parents[1]
    if commit is None:
        add('executable.revision', 'fail', f'{checkout} is not a Git checkout of agent-board; its revision is unknown')
        add('executable.clean', 'fail', f'{checkout} is not a Git checkout of agent-board')
    else:
        if values is None:
            add('executable.revision', 'fail', f'{checkout} is at {commit}; there is no valid pin to compare it with')
        elif commit != values['board_revision']:
            add('executable.revision', 'fail', f"{checkout} is at {commit}, but this project pins "
                                               f"{values['board_revision']}; check that revision out there, or move "
                                               'the project with agent-board update')
        else:
            add('executable.revision', 'ok', f'{checkout} is at the pin')
        if clean:
            add('executable.clean', 'ok', f'{checkout} is clean')
        elif args.allow_dirty:
            add('executable.clean', 'note', f'{checkout} is modified (--allow-dirty)')
        else:
            add('executable.clean', 'fail', f'{checkout} is modified; commit or discard the changes, or pass --allow-dirty')
    if values is None:
        add('files', 'fail', 'not checked: the project is not set up')
    else:
        findings = [f for f in project_files.check(root, spec, args.allow_dirty)[0] if f[1] == 'files']
        statuses = {status for status, _, _ in findings}
        worst = 'fail' if 'fail' in statuses else 'note' if 'note' in statuses else 'ok'
        add('files', worst, '; '.join(m for s, _, m in findings if s != 'ok') or
            '; '.join(m for s, _, m in findings))
    add('storage', *board.inspect())
    for name, (status, message) in zip(GUARDS, guard_checks(root, resolved or EXECUTABLE)):
        add(f'guard.{name}', status, message)
    checks.sort(key=lambda c: CHECK_ORDER.index(c['id']))
    ok = not any(c['status'] == 'fail' for c in checks)
    if args.json:
        return int(not ok), json.dumps(dict(interface=INTERFACE, ok=ok, checks=checks)) + '\n', '', None
    out = ''.join(f"{TAGS[c['status']]} {c['id']}: {c['message']}\n" for c in checks)
    out += 'agent-board doctor: ' + ('all checks passed.\n' if ok else 'problems found.\n')
    return int(not ok), out, '', None


def project(args, root, board):
    spec = BoardSpec()
    action = args.action
    if action == 'doctor':
        return doctor(args, root, board, spec)
    if action == 'init':
        revision = spec.runtime.commit(args.revision)
        return 0, project_files.init(root, spec, revision, descriptor_text(revision)), '', None
    if action == 'sync':
        if args.check:
            if args.link or args.source:
                raise Broken('sync --check takes neither --link nor --source')
            findings, _ = project_files.check(root, spec, args.allow_dirty)
            return int(any(f[0] == 'fail' for f in findings)), project_files.render_findings(findings), '', None
        if args.allow_dirty:
            raise Broken('--allow-dirty goes with sync --check')
        if args.source and not args.link:
            raise Broken('--source goes with sync --link')
        return 0, project_files.sync(root, spec, args.link, args.source), '', None
    if action == 'update':
        return 0, project_files.update(root, spec, args.revision), '', None
    out = project_files.remove(root, spec)
    if board.exists():
        out += f'The board data at {board.path} stays; delete it by hand once nothing on it is worth keeping.\n'
    hooks_dir = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks'))
    if any(owned(hooks_dir / name) for name in GUARDS):
        out += 'The Git guards stay installed; agent-board uninstall-hook removes them.\n'
    return 0, out, '', None


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
    if action in PROJECT_VERBS:
        if not common:
            raise BoardError(f'{action} needs a Git repository')
        return project(args, root, board)
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
        if args.limit < 0:
            raise BoardError('--limit must be non-negative')
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
                code, out, mark = digest(board, args, agents, posts)
                if args.mark and mark:
                    ack = (board, args.cursor, mark)
    return code, out, err, ack


def main():
    args = parser().parse_args()
    try:
        code, out, err, ack = execute(args)
        sys.stderr.write(err)
        sys.stderr.flush()
        sys.stdout.write(out)
        sys.stdout.flush()
        if ack:
            ack[0].acknowledge(ack[1], ack[2])
        return code
    except Refused as exc:
        print(exc if str(exc).startswith('agent-board') else f'agent-board: {exc}', file=sys.stderr)
        return 1
    except (BoardError, Broken, OSError, ValueError) as exc:
        message = str(exc) if str(exc).startswith('agent-board') else f'agent-board: {exc}'
        if args.action == 'doctor' and args.json:
            # Callers treat exit 2 as a failure; still give them an object when possible.
            print(json.dumps(dict(interface=INTERFACE, ok=False,
                                  checks=[dict(id='doctor', status='fail', message=message)])))
        print(message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    # Retain the original CLI's help alias.
    if sys.argv[1:] == ['help']:
        sys.argv[1] = '--help'
    sys.exit(main())
