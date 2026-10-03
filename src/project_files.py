"""Project files: the installer rules that agent-board and the Isabelle tooling share.

This implements the "Shared rules" of the project-integration contracts
(agent-board's docs/project-integration.md, the Isabelle tooling's
docs/delivery-contracts.md): descriptors and the inventory, owned JSON
entries, TOML and Markdown blocks, root instruction files, skill
directories and the project kinds that select them, link mode, the
installer lock, index-based preflight, and publication that prints restore
commands when a write fails.

Both repositories keep an identical copy of this file, agent-board as
src/project_files.py and the tooling as scripts/project_files.py; change
both together. Standard library only, Python 3.9 or later; TOML blocks need
Python 3.11 for tomllib.
"""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile

FORMAT = 1
FULL_REVISION = re.compile(r'[0-9a-f]{40}\Z')
SKILL_NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\Z')
PLACEHOLDER = re.compile(r'@[A-Z][A-Z0-9_]*@')
GIT_VARIABLES = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
                 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE', 'GIT_PREFIX')
ABSENT = object()

# Tests set this to a function called after each publication step with the
# step number and operation, to inject a failure there.
after_step = None


class Refused(Exception):
    """The operation refuses, or a check finds a problem: exit status 1."""


class Broken(Exception):
    """A usage error, a missing prerequisite or a failed write: exit status 2."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def entry_hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))


def git(cwd, *args, stdin=None, check=True, literal=True):
    """Run Git in cwd, ignoring a caller's repository variables and never taking optional locks."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_VARIABLES}
    env['GIT_OPTIONAL_LOCKS'] = '0'
    result = subprocess.run(['git', *(['--literal-pathspecs'] if literal else []), '-C', str(cwd), *args],
                            input=stdin, capture_output=True, env=env)
    if check is True and result.returncode or check == 'status' and result.returncode > 1:
        raise Broken(f"git {args[0]} failed in {cwd}: {result.stderr.decode(errors='replace').strip()}")
    return result


def checkout_root(start):
    start = Path(start or os.getcwd())
    if not start.is_dir():
        raise Broken(f'not a directory: {start}')
    result = git(start, 'rev-parse', '--show-toplevel', check=False)
    if result.returncode:
        raise Broken(f'not inside a Git checkout: {start}')
    return Path(result.stdout.decode(errors='surrogateescape').strip())


class Runtime:
    """The component's runtime checkout: every installed file is read from its Git objects."""

    def __init__(self, path):
        self.path = Path(path)

    def commit(self, rev):
        if not rev or rev.startswith('-'):
            raise Broken(f'not a revision: {rev!r}')
        result = git(self.path, 'rev-parse', '--verify', '--quiet', rev + '^{commit}', check=False)
        if result.returncode:
            raise Broken(f'{rev} does not name a commit in {self.path}')
        return result.stdout.decode().strip()

    def has(self, rev):
        return bool(FULL_REVISION.fullmatch(rev or '')) and not git(
            self.path, 'cat-file', '-e', rev + '^{commit}', check=False).returncode

    def blob(self, rev, path):
        result = git(self.path, 'cat-file', 'blob', f'{rev}:{path}', check=False)
        if result.returncode:
            raise Broken(f'{path} does not exist at {rev} in {self.path}')
        return result.stdout

    def tree(self, rev, path):
        """(mode, path relative to PATH, object) for every entry below PATH at REV."""
        output = git(self.path, 'ls-tree', '-r', '-z', '--full-tree', rev, '--', path + '/').stdout
        entries = []
        for record in output.split(b'\0'):
            if not record:
                continue
            meta, name = record.split(b'\t', 1)
            mode, _, obj = meta.decode().split(' ')
            entries.append((mode, name.decode(errors='surrogateescape')[len(path) + 1:], obj))
        return entries

    def object(self, obj):
        return git(self.path, 'cat-file', 'blob', obj).stdout

    def head(self):
        result = git(self.path, 'rev-parse', '--verify', '--quiet', 'HEAD', check=False)
        return result.stdout.decode().strip() if not result.returncode else None


# --- descriptors ---------------------------------------------------------------------

def parse_key_values(text, rel):
    """Data only: one key=value per line, # comment lines and blank lines allowed."""
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if '=' not in line:
            raise Refused(f'{rel}:{number}: expected key=value, found: {line}')
        key, value = line.split('=', 1)
        if not re.fullmatch(r'[a-z][a-z0-9_]*', key):
            raise Refused(f'{rel}:{number}: malformed key: {key}')
        if key in values:
            raise Refused(f'{rel}:{number}: duplicate key: {key}')
        values[key] = value
    return values


def set_key(text, key, value):
    """Change only KEY's line, keeping every other byte; append the line when absent."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if not line.lstrip().startswith('#') and line.split('=', 1)[0] == key and '=' in line:
            lines[i] = f'{key}={value}' + line[len(line.rstrip('\r\n')):]
            return ''.join(lines)
    if text and not text.endswith('\n'):
        text += '\n'
    return text + f'{key}={value}\n'


# --- the target revision's manifest and the state it asks for ----------------------------

def relative_path(value, what):
    if (not isinstance(value, str) or not value or value.startswith('/') or '\\' in value or
            any(part in ('', '.', '..', '.git') for part in value.split('/'))):
        raise Broken(f'manifest: invalid {what}: {value!r}')
    return value


def substitute(text, substitutions):
    for key, value in substitutions.items():
        text = text.replace(f'@{key}@', value)
    leftover = PLACEHOLDER.search(text)
    if leftover:
        raise Broken(f'unsubstituted placeholder {leftover.group(0)} in a template')
    return text


def manifest_kinds(value, what):
    if not isinstance(value, list) or not all(isinstance(k, str) and k for k in value):
        raise Broken(f'manifest: invalid {what}: {value!r}')
    return value


def project_kind(spec, revision, values, manifest):
    """The project's kind, which the revision must support; None when the spec or the project names none.

    Only skill entries carry kinds; without a project kind every skill is installed."""
    for key in ('files', 'json_entries', 'toml_blocks'):
        for item in manifest.get(key, []):
            if isinstance(item, dict) and 'kinds' in item:
                raise Broken(f'manifest: only skill entries may carry kinds, but a {key} entry does')
    supported = manifest_kinds(manifest.get('kinds', []), 'kinds')
    kind = spec.kind(values) if hasattr(spec, 'kind') else None
    if kind is not None and kind not in supported:
        raise Refused(f'{spec.descriptor}: this project is of kind {kind}, which {spec.name} {revision} does not '
                      'support' + (f" (it supports {', '.join(supported)})" if supported else
                                   ' (it predates project kinds)'))
    return kind


class Desired:
    def __init__(self, revision, mode, link_source, shared):
        self.revision, self.mode, self.link_source, self.shared = revision, mode, link_source, shared
        self.files = {}         # rel -> (bytes, executable)
        self.directories = []   # wholly owned directories
        self.symlinks = {}      # rel -> target
        self.json_entries = []  # dict(file, pointer, element, marker, value)
        self.toml_blocks = []   # dict(file, table, inner)
        self.markdown = None    # the inner text of the root instruction block
        self.skills = []        # (name, source directory)


def desired_state(spec, revision, values, mode='copy', link_source=None, shared=False):
    runtime = spec.runtime
    try:
        manifest = json.loads(runtime.blob(revision, spec.manifest))
    except Broken:
        raise Broken(f'{revision} has no {spec.manifest}; that revision cannot be installed') from None
    except ValueError as exc:
        raise Broken(f'{spec.manifest} at {revision} is not valid JSON: {exc}') from None
    if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('component') != spec.name:
        raise Broken(f'{spec.manifest} at {revision} is not a format-{FORMAT} {spec.name} manifest')
    kind = project_kind(spec, revision, values, manifest)
    substitutions = spec.substitutions(values)
    desired = Desired(revision, mode, link_source, shared)
    for skill in manifest.get('skills', []):
        name, source = skill.get('name'), relative_path(skill.get('source'), 'skill source')
        if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
            raise Broken(f'manifest: invalid skill name: {name!r}')
        if 'kinds' in skill:
            kinds = manifest_kinds(skill['kinds'], f'the kinds of the skill {name}')
            if not kinds:
                raise Broken(f'manifest: the skill {name} lists no kinds')
            if kind is not None and kind not in kinds:
                continue
        base = f'.agents/skills/{name}'
        entries = runtime.tree(revision, source)
        if not any(path == 'SKILL.md' for _, path, _ in entries):
            raise Broken(f'{source}/SKILL.md does not exist at {revision}')
        desired.skills.append((name, source))
        if mode == 'link':
            desired.symlinks[base] = f'{link_source}/{source}'
        else:
            desired.directories.append(base)
            for file_mode, path, obj in entries:
                if file_mode not in ('100644', '100755'):
                    raise Broken(f'{source}/{path} at {revision} is not a regular file; it cannot be installed')
                desired.files[f'{base}/{path}'] = (runtime.object(obj), file_mode == '100755')
        if not shared:
            desired.symlinks[f'.claude/skills/{name}'] = f'../../.agents/skills/{name}'
    for item in manifest.get('files', []):
        path = relative_path(item.get('path'), 'file path')
        desired.files[path] = (runtime.blob(revision, relative_path(item.get('source'), 'file source')),
                               bool(item.get('executable', False)))
    for item in manifest.get('json_entries', []):
        text = substitute(runtime.blob(revision, relative_path(item.get('source'), 'entry source')).decode(), substitutions)
        pointer = item.get('pointer')
        if not isinstance(pointer, str) or not pointer.startswith('/'):
            raise Broken(f'manifest: invalid JSON pointer: {pointer!r}')
        element = bool(item.get('element', False))
        if element and not item.get('marker'):
            raise Broken(f'manifest: the array entry at {pointer} names no marker')
        desired.json_entries.append(dict(file=relative_path(item.get('file'), 'JSON file'), pointer=pointer,
                                         element=element, marker=item.get('marker'), value=json.loads(text)))
    for item in manifest.get('toml_blocks', []):
        text = substitute(runtime.blob(revision, relative_path(item.get('source'), 'block source')).decode(), substitutions)
        desired.toml_blocks.append(dict(file=relative_path(item.get('file'), 'TOML file'), table=item['table'],
                                        inner=text if text.endswith('\n') else text + '\n'))
    if manifest.get('markdown_block'):
        text = substitute(runtime.blob(revision, relative_path(manifest['markdown_block'], 'block source')).decode(),
                          substitutions)
        desired.markdown = text if text.endswith('\n') else text + '\n'
    return desired


# --- the project tree ---------------------------------------------------------------------

def within(path, root):
    path, root = str(path), str(root)
    return path == root or path.startswith(root.rstrip('/') + '/')


def beneath(rel, parent):
    return rel.startswith(parent + '/')


class Tree:
    def __init__(self, root):
        self.root = Path(root)
        self.real = Path(os.path.realpath(root))
        self.shadowed = set()  # symlinks this install replaces: what lies beneath them is not ours

    def path(self, rel):
        return self.root / rel

    def state(self, rel):
        if any(beneath(rel, s) for s in self.shadowed):
            return None
        return fingerprint(self.path(rel))

    def text(self, rel):
        return self.path(rel).read_bytes().decode('utf-8')

    def git_path(self, rel):
        """The path Git knows: existing parent directories resolved, the leaf itself not."""
        parent, _, name = rel.rpartition('/')
        if not parent:
            return rel
        real = os.path.realpath(self.path(parent))
        if not within(real, self.real):
            return None
        where = os.path.relpath(real, self.real)
        return name if where == '.' else f'{where}/{name}'


def fingerprint(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return ('link', os.readlink(path))
    if stat.S_ISREG(info.st_mode):
        return ('file', sha256(Path(path).read_bytes()), bool(info.st_mode & 0o100))
    if stat.S_ISDIR(info.st_mode):
        return ('dir',)
    return ('other',)


def files_below(tree, rel):
    """Every file and symlink below a directory, as checkout-relative paths."""
    found = []
    for directory, dirs, files in os.walk(tree.path(rel)):
        where = os.path.relpath(directory, tree.root).replace(os.sep, '/')
        found.extend(f'{where}/{name}' for name in files)
        found.extend(f'{where}/{name}' for name in dirs if os.path.islink(os.path.join(directory, name)))
    return found


# --- owned JSON entries -------------------------------------------------------------------

def pointer_keys(pointer):
    return [part.replace('~1', '/').replace('~0', '~') for part in pointer[1:].split('/')]


def load_json_object(text, rel):
    def no_duplicates(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise Refused(f'{rel}: duplicate key in a JSON object')
        return dict(pairs)
    try:
        data = json.loads(text, object_pairs_hook=no_duplicates)
    except ValueError as exc:
        raise Refused(f'{rel}: not valid JSON: {exc}') from None
    if not isinstance(data, dict):
        raise Refused(f'{rel}: the top level is not a JSON object')
    return data


def dump_json(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + '\n'


def carries(element, marker):
    return marker in json.dumps(element, ensure_ascii=False)


def json_container(data, entry, create=False):
    """The object holding the member, or the array holding the element; None when absent."""
    keys = pointer_keys(entry['pointer'])
    path = keys if entry['element'] else keys[:-1]
    node = data
    for depth, key in enumerate(path):
        if not isinstance(node, dict):
            raise Refused(f"{entry['file']}: {'/' + '/'.join(path[:depth])} is not a JSON object")
        if key not in node:
            if not create:
                return None
            node[key] = [] if entry['element'] and depth == len(path) - 1 else {}
        node = node[key]
    if entry['element'] and not isinstance(node, list):
        raise Refused(f"{entry['file']}: {entry['pointer']} is not a JSON array")
    if not entry['element'] and not isinstance(node, dict):
        raise Refused(f"{entry['file']}: the parent of {entry['pointer']} is not a JSON object")
    return node


def json_get(data, entry):
    node = json_container(data, entry)
    if node is None:
        return ABSENT
    if entry['element']:
        hits = [e for e in node if carries(e, entry['marker'])]
        if len(hits) > 1:
            raise Refused(f"{entry['file']}: {len(hits)} elements of {entry['pointer']} carry {entry['marker']}")
        return hits[0] if hits else ABSENT
    return node.get(pointer_keys(entry['pointer'])[-1], ABSENT)


def json_set(data, entry, value):
    node = json_container(data, entry, create=True)
    if entry['element']:
        hits = [i for i, e in enumerate(node) if carries(e, entry['marker'])]
        if hits:
            node[hits[0]] = value
        else:
            node.append(value)
    else:
        node[pointer_keys(entry['pointer'])[-1]] = value


def json_remove(data, entry):
    keys = pointer_keys(entry['pointer'])
    path = keys if entry['element'] else keys[:-1]
    chain = [data]
    for key in path:
        chain.append(chain[-1][key])
    if entry['element']:
        chain[-1][:] = [e for e in chain[-1] if not carries(e, entry['marker'])]
    else:
        del chain[-1][keys[-1]]
    # Prune containers left empty along the pointer.
    for depth in range(len(path), 0, -1):
        if chain[depth]:
            break
        del chain[depth - 1][path[depth - 1]]


# --- delimited text blocks ------------------------------------------------------------------

def markers(syntax, name):
    if syntax == 'markdown':
        return f'<!-- BEGIN {name} -->', f'<!-- END {name} -->'
    return f'# BEGIN {name}', f'# END {name}'


def find_block(text, begin, end, rel):
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip('\r\n') == begin]
    ends = [i for i, line in enumerate(lines) if line.rstrip('\r\n') == end]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise Refused(f'{rel}: malformed block markers: expected one "{begin}" followed by one "{end}"')
    return lines, starts[0], ends[0]


def block_inner(found):
    lines, start, end = found
    return ''.join(lines[start + 1:end])


def put_block(text, found, begin, end, inner):
    if found:
        lines, start, stop = found
        return ''.join(lines[:start + 1]) + inner + ''.join(lines[stop:])
    if text and not text.endswith('\n'):
        text += '\n'
    if text and not text.endswith('\n\n'):
        text += '\n'
    return text + f'{begin}\n{inner}{end}\n'


def drop_block(found):
    lines, start, stop = found
    before, after = lines[:start], lines[stop + 1:]
    # Undo the blank separator the first install added.
    if before and not before[-1].strip() and (not after or not after[0].strip()):
        before = before[:-1]
    return ''.join(before + after)


def toml_table(data, table):
    for key in table.split('.'):
        if not isinstance(data, dict) or key not in data:
            return ABSENT
        data = data[key]
    return data


def toml_loads(text, rel):
    try:
        import tomllib
    except ImportError:
        raise Broken('TOML blocks need Python 3.11 or later (tomllib)') from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Refused(f'{rel}: not valid TOML: {exc}') from None


# --- root instruction files -------------------------------------------------------------------

def instruction_files(tree, overlay):
    """The files that carry the block, and whether to create AGENTS.md and the CLAUDE.md symlink.

    OVERLAY maps paths this install creates first (the Isabelle session
    scaffold) to ('file', bytes) or ('link', target)."""
    def resolve(rel, depth=0):
        if rel in overlay:
            kind, value = overlay[rel]
            if kind == 'file':
                return rel
            target = os.path.normpath(os.path.join(os.path.dirname(rel), value)).replace(os.sep, '/')
            if depth > 8 or target.startswith('..'):
                raise Refused(f'{rel} does not resolve to a regular file inside the checkout')
            return resolve(target, depth + 1)
        path = tree.path(rel)
        if not os.path.lexists(path):
            return None
        real = os.path.realpath(path)
        if not within(real, tree.real) or not os.path.isfile(real):
            raise Refused(f'{rel} does not resolve to a regular file inside the checkout')
        return os.path.relpath(real, tree.real).replace(os.sep, '/')
    agents, claude = resolve('AGENTS.md'), resolve('CLAUDE.md')
    if agents is None and claude is None:
        return ['AGENTS.md'], True, True
    if agents and claude:
        return sorted({agents, claude}), False, False
    if agents:
        return [agents], False, True
    return sorted({claude, 'AGENTS.md'}), True, False


# --- planning --------------------------------------------------------------------------------

MANAGED, SHARED, INVENTORY, DESCRIPTOR = range(4)


class Plan:
    def __init__(self):
        self.ops = []        # (phase, kind, rel, payload); kind: write, link, delete, rmdirs
        self.refusals = []
        self.inventory = None

    def refuse(self, message):
        self.refusals.append(message)

    def add(self, phase, kind, rel, payload=None):
        self.ops.append((phase, kind, rel, payload))

    def ordered(self):
        return sorted(self.ops, key=lambda op: op[0])

    def paths(self):
        seen = []
        for _, kind, rel, _ in self.ordered():
            if kind != 'rmdirs' and rel not in seen:
                seen.append(rel)
        return seen


def check_parents(tree, rel, plan):
    parts = rel.split('/')
    for i in range(1, len(parts)):
        sub = '/'.join(parts[:i])
        if any(sub == s or beneath(sub, s) for s in tree.shadowed):
            return
        path = tree.path(sub)
        if os.path.islink(path):
            real = os.path.realpath(path)
            if not within(real, tree.real):
                plan.refuse(f'{sub} is a symlink resolving outside the checkout')
                return
            if not os.path.isdir(real):
                plan.refuse(f'{sub} exists and is not a directory')
                return
        elif os.path.lexists(path):
            if not os.path.isdir(path):
                plan.refuse(f'{sub} exists and is not a directory')
                return
        else:
            return


def claude_shared(tree):
    alias = tree.path('.claude/skills')
    return os.path.lexists(alias) and os.path.realpath(alias) == os.path.realpath(tree.path('.agents/skills'))


def build_inventory(spec, desired):
    blocks = []
    return dict(
        format=FORMAT, component=spec.name, revision=desired.revision, mode=desired.mode,
        link_source=desired.link_source, claude_skills='shared' if desired.shared else 'aliases',
        directories=sorted(desired.directories),
        files={rel: dict(sha256=sha256(data), executable=executable)
               for rel, (data, executable) in sorted(desired.files.items())},
        symlinks=dict(sorted(desired.symlinks.items())),
        json_entries=[dict(file=e['file'], pointer=e['pointer'], element=e['element'], sha256=entry_hash(e['value']),
                           **({'marker': e['marker']} if e['element'] else {}))
                      for e in desired.json_entries],
        blocks=blocks)


def make_plan(tree, spec, desired, prior, *, scaffold=None, descriptor=None, remove_descriptor=False):
    """Everything an install of DESIRED (None: removal) over PRIOR (the inventory, or None) would do."""
    plan = Plan()
    scaffold = scaffold or {}
    prior = prior or {}
    prior_files = prior.get('files', {})
    prior_dirs = prior.get('directories', [])
    prior_links = prior.get('symlinks', {})
    prior_json = prior.get('json_entries', [])
    prior_blocks = prior.get('blocks', [])
    want_files = desired.files if desired else {}
    want_dirs = desired.directories if desired else []
    want_links = desired.symlinks if desired else {}
    want_json = desired.json_entries if desired else []
    want_toml = desired.toml_blocks if desired else []
    inventory_rel = f'{spec.inventory_dir}/inventory.json'

    for rel in ('.agents', '.claude', '.agents/skills', '.claude/skills'):
        path = tree.path(rel)
        if os.path.islink(path) and not within(os.path.realpath(path), tree.real):
            plan.refuse(f'{rel} is a symlink resolving outside the checkout')
    tree.shadowed = {rel for rel, target in prior_links.items() if want_links.get(rel) != target}

    # Wholly managed files, directories and symlinks: what the inventory records must be unchanged.
    for rel, record in prior_files.items():
        state = tree.state(rel)
        if state is None:
            plan.refuse(f'missing managed file: {rel}')
        elif state != ('file', record['sha256'], record['executable']):
            plan.refuse(f'edited managed file: {rel}')
    for rel in prior_dirs:
        state = tree.state(rel)
        if state == ('dir',):
            extra = [f for f in files_below(tree, rel) if f not in prior_files]
            if extra:
                plan.refuse(f'unmanaged file in the managed directory {rel}: ' + ', '.join(extra))
        elif state is not None:
            plan.refuse(f'managed directory replaced: {rel}')
    for rel, target in prior_links.items():
        if tree.state(rel) != ('link', target):
            plan.refuse(f'edited or missing managed symlink: {rel}')
    for rel in list(want_files) + list(want_links) + list(scaffold):
        check_parents(tree, rel, plan)
    for rel in want_files:
        if rel not in prior_files and tree.state(rel) is not None:
            plan.refuse(f'collision: {rel} exists and is not managed by {spec.name}')
    for rel in want_dirs:
        if rel not in prior_dirs and rel not in prior_links and tree.state(rel) is not None:
            plan.refuse(f'collision: {rel} exists and is not managed by {spec.name}')
    for rel in want_links:
        if rel not in prior_links and rel not in prior_dirs and tree.state(rel) is not None:
            plan.refuse(f'collision: {rel} exists and is not managed by {spec.name}')
    for rel in scaffold:
        if tree.state(rel) is not None:
            plan.refuse(f'collision: {rel} exists; init never overwrites project files')

    for rel in prior_files:
        if rel not in want_files:
            plan.add(MANAGED, 'delete', rel)
    for rel in prior_dirs:
        if rel in want_links:
            plan.add(MANAGED, 'rmdirs', rel)
    for rel, target in prior_links.items():
        if rel not in want_links:
            plan.add(MANAGED, 'delete', rel)
    for rel, (kind, value) in scaffold.items():
        plan.add(MANAGED, 'write' if kind == 'file' else 'link', rel, (value, False) if kind == 'file' else value)
    for rel, (data, executable) in want_files.items():
        if tree.state(rel) != ('file', sha256(data), executable):
            plan.add(MANAGED, 'write', rel, (data, executable))
    for rel, target in want_links.items():
        if tree.state(rel) != ('link', target):
            plan.add(MANAGED, 'link', rel, target)

    # Owned JSON entries.
    for rel in sorted({e['file'] for e in prior_json} | {e['file'] for e in want_json}):
        check_parents(tree, rel, plan)
        state = tree.state(rel)
        if state is not None and state[0] != 'file':
            plan.refuse(f'{rel} is not a regular file')
            continue
        try:
            original = load_json_object(tree.text(rel), rel) if state else None
            data = copy.deepcopy(original) if original is not None else {}
            mine = [e for e in prior_json if e['file'] == rel]
            wanted = [e for e in want_json if e['file'] == rel]
            key = lambda e: (e['pointer'], e['element'])
            failed = False
            for entry in mine:
                current = json_get(data, entry)
                if current is ABSENT or entry_hash(current) != entry['sha256']:
                    plan.refuse(f"{'missing' if current is ABSENT else 'edited'} owned entry: {rel} {entry['pointer']}")
                    failed = True
            for entry in wanted:
                if key(entry) not in {key(e) for e in mine} and json_get(data, entry) is not ABSENT:
                    plan.refuse(f"collision: {rel} {entry['pointer']} already has an entry not managed by {spec.name}")
                    failed = True
            if failed:
                continue
            for entry in mine:
                if key(entry) not in {key(e) for e in wanted}:
                    json_remove(data, entry)
            for entry in wanted:
                json_set(data, entry, entry['value'])
        except Refused as exc:
            plan.refuse(str(exc))
            continue
        if not data:
            if original is not None:
                plan.add(SHARED, 'delete', rel)
        elif data != original:
            plan.add(SHARED, 'write', rel, (dump_json(data).encode('utf-8'), bool(state and state[2])))

    blocks = []
    # The owned TOML block.
    for rel in sorted({b['file'] for b in prior_blocks if b['syntax'] == 'toml'} | {b['file'] for b in want_toml}):
        check_parents(tree, rel, plan)
        begin, end = markers('toml', spec.name)
        state = tree.state(rel)
        if state is not None and state[0] != 'file':
            plan.refuse(f'{rel} is not a regular file')
            continue
        try:
            text = tree.text(rel) if state else ''
            found = find_block(text, begin, end, rel)
            record = next((b for b in prior_blocks if b['file'] == rel and b['syntax'] == 'toml'), None)
            wanted = next((b for b in want_toml if b['file'] == rel), None)
            if record and (not found or sha256(block_inner(found).encode()) != record['sha256']):
                plan.refuse(f"{'missing' if not found else 'edited'} owned block: {rel}")
                continue
            if not record and found:
                plan.refuse(f'collision: {rel} already has an unrecorded {spec.name} block')
                continue
            if wanted:
                outside = drop_block(found) if found else text
                if toml_table(toml_loads(outside, rel), wanted['table']) is not ABSENT:
                    plan.refuse(f"collision: {rel} defines [{wanted['table']}] outside the {spec.name} block")
                    continue
                new = put_block(text, found, begin, end, wanted['inner'])
                if toml_table(toml_loads(new, rel), wanted['table']) != toml_table(toml_loads(wanted['inner'], rel), wanted['table']):
                    plan.refuse(f"{rel}: text outside the {spec.name} block changes [{wanted['table']}]; "
                                'keys after the block attach to its last table')
                    continue
                blocks.append(dict(file=rel, syntax='toml', sha256=sha256(wanted['inner'].encode())))
            else:
                new = drop_block(found)
        except Refused as exc:
            plan.refuse(str(exc))
            continue
        if new != text:
            if not new.strip() and not wanted:
                plan.add(SHARED, 'delete', rel)
            else:
                plan.add(SHARED, 'write', rel, (new.encode('utf-8'), bool(state and state[2])))

    # The Markdown block in the root instruction files.
    try:
        overlay = dict(scaffold)
        if desired and desired.markdown is not None:
            targets, create_agents, link_claude = instruction_files(tree, overlay)
        else:
            targets, create_agents, link_claude = [], False, False
        begin, end = markers('markdown', spec.name)
        recorded = {b['file']: b for b in prior_blocks if b['syntax'] == 'markdown'}
        for rel in sorted(set(targets) | set(recorded)):
            if rel in overlay:
                text = overlay[rel][1].decode('utf-8')
                state = None
            elif create_agents and rel == 'AGENTS.md':
                text, state = '', None
            else:
                state = tree.state(rel)
                if state is None and rel in recorded:
                    plan.refuse(f'missing owned block: {rel} no longer exists')
                    continue
                text = tree.text(rel) if state else ''
            found = find_block(text, begin, end, rel)
            record = recorded.get(rel)
            if record and (not found or sha256(block_inner(found).encode()) != record['sha256']):
                plan.refuse(f"{'missing' if not found else 'edited'} owned block: {rel}")
                continue
            if not record and found:
                plan.refuse(f'collision: {rel} already has an unrecorded {spec.name} block')
                continue
            if rel in targets:
                new = put_block(text, found, begin, end, desired.markdown)
                blocks.append(dict(file=rel, syntax='markdown', sha256=sha256(desired.markdown.encode())))
            else:
                new = drop_block(found)
            if new != text:
                plan.add(SHARED, 'write', rel, (new.encode('utf-8'), bool(state and state[2])))
        if link_claude:
            plan.add(SHARED, 'link', 'CLAUDE.md', 'AGENTS.md')
    except Refused as exc:
        plan.refuse(str(exc))

    # The inventory, then the descriptor.
    state = tree.state(inventory_rel)
    if not prior and state is not None:
        plan.refuse(f'collision: {inventory_rel} exists but was not read as this project\'s inventory')
    if desired:
        plan.inventory = build_inventory(spec, desired)
        plan.inventory['blocks'] = sorted(blocks, key=lambda b: (b['file'], b['syntax']))
        body = (json.dumps(plan.inventory, indent=2, sort_keys=True) + '\n').encode()
        if state != ('file', sha256(body), False):
            plan.add(INVENTORY, 'write', inventory_rel, (body, False))
    elif state is not None:
        plan.add(INVENTORY, 'delete', inventory_rel)
    if descriptor is not None:
        body = descriptor.encode('utf-8')
        current = tree.state(spec.descriptor)
        if current is None or current[:2] != ('file', sha256(body)):
            plan.add(DESCRIPTOR, 'write', spec.descriptor, (body, bool(current and current[2])))
    if remove_descriptor:
        plan.add(DESCRIPTOR, 'delete', spec.descriptor)
    return plan


# --- preflight, the lock and publication ---------------------------------------------------------

def preflight(tree, plan, exempt=()):
    """The index is the checkpoint: refuse unstaged, untracked or ignored paths."""
    def is_exempt(rel):
        return any(rel == e or beneath(rel, e) for e in exempt)
    checked = {}
    for phase, kind, rel, _ in plan.ops:
        if kind == 'rmdirs' or is_exempt(rel):
            continue
        location = tree.git_path(rel)
        if location is None:
            plan.refuse(f'{rel} lies outside the checkout')
            continue
        checked[location] = rel
    if not checked:
        return
    paths = sorted(checked)
    split = lambda out: {p.decode(errors='surrogateescape') for p in out.split(b'\0') if p}
    index = split(git(tree.root, 'ls-files', '-z', '--cached', '--', *paths).stdout)
    unstaged = split(git(tree.root, 'diff', '-z', '--name-only', '--', *paths).stdout)
    fresh = [p for p in paths if p not in index]
    # check-ignore takes no pathspec magic; it reads plain paths, and exits 1 when none is ignored.
    ignored = split(git(tree.root, 'check-ignore', '-z', '--stdin', literal=False, check='status',
                        stdin=b''.join(p.encode(errors='surrogateescape') + b'\0' for p in fresh)).stdout
                    ) if fresh else set()
    for location, rel in sorted(checked.items()):
        path = tree.root / location
        if location in unstaged:
            plan.refuse(f'{rel} has unstaged changes; stage or discard them first')
        elif location not in index and os.path.lexists(path) and not os.path.isdir(path):
            plan.refuse(f'{rel} is untracked; stage or remove it first')
        elif location in ignored:
            plan.refuse(f'{rel} is ignored by Git, so it could not be committed')


@contextmanager
def installer_lock(root):
    """Serialize the writing verbs of both components in this worktree."""
    path = git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'project-files.lock').stdout.decode().strip()
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('waiting for another installer in this worktree', file=sys.stderr)
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def file_mode(executable):
    """What Git's checkout would give the file: 0777 or 0666 less the umask."""
    mask = os.umask(0)
    os.umask(mask)
    return (0o777 if executable else 0o666) & ~mask


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Publisher:
    def __init__(self, tree, plan):
        self.tree, self.plan = tree, plan
        self.expected = {rel: fingerprint(tree.path(rel)) for _, kind, rel, _ in plan.ops if kind != 'rmdirs'}
        self.touched = []       # (rel, fingerprint before the first change)
        self.created = []       # directories created, in creation order
        self.step = 0

    def forget_below(self, rel):
        for key in self.expected:
            if beneath(key, rel):
                self.expected[key] = None

    def mark(self, rel):
        if rel not in (t[0] for t in self.touched):
            self.touched.append((rel, self.expected.get(rel)))

    def parents(self, rel):
        missing = []
        parent = self.tree.path(rel).parent
        while not os.path.lexists(parent):
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            os.mkdir(directory)
            self.created.append(os.path.relpath(directory, self.tree.root).replace(os.sep, '/'))

    def run(self):
        for op in self.plan.ordered():
            self.apply(*op)
            self.step += 1
            if after_step:
                after_step(self.step, op)

    def apply(self, phase, kind, rel, payload):
        path = self.tree.path(rel)
        if kind == 'rmdirs':
            for directory, _, _ in sorted(os.walk(path), key=lambda w: -len(w[0])):
                os.rmdir(directory)
            self.forget_below(rel)
            if rel in self.expected:
                self.expected[rel] = None
            return
        if fingerprint(path) != self.expected[rel]:
            raise Broken(f'{rel} changed during installation')
        self.mark(rel)
        if kind == 'delete':
            os.unlink(path)
            fsync_directory(path.parent)
            self.forget_below(rel)
            self.expected[rel] = None
            return
        self.parents(rel)
        if kind == 'link':
            temporary = path.parent / f'.{path.name}.{os.getpid()}.tmp'
            os.symlink(payload, temporary)
            try:
                os.replace(temporary, path)
            finally:
                if os.path.lexists(temporary):
                    os.unlink(temporary)
        else:
            data, executable = payload
            fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), file_mode(executable))
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        fsync_directory(path.parent)
        self.expected[rel] = fingerprint(path)

    def prune(self):
        """Remove directories left empty by deletions, never the checkout root."""
        for _, kind, rel, _ in self.plan.ops:
            if kind != 'delete':
                continue
            parent = self.tree.path(rel).parent
            while within(parent, self.tree.root) and parent != self.tree.root:
                try:
                    if os.path.islink(parent):
                        break
                    os.rmdir(parent)
                except OSError:
                    break
                parent = parent.parent

    def restore_commands(self):
        tree = self.tree
        locations = {rel: tree.git_path(rel) or rel for rel, _ in self.touched}
        split = lambda out: {p.decode(errors='surrogateescape') for p in out.split(b'\0') if p}
        index = split(git(tree.root, 'ls-files', '-z', '--cached', '--', *locations.values(),
                          check=False).stdout) if locations else set()
        restore = [locations[rel] for rel, _ in self.touched if locations[rel] in index]
        links = [rel for rel, before in self.touched if locations[rel] not in index and before and before[0] == 'link']
        remove = [rel for rel, before in self.touched if locations[rel] not in index and not (before and before[0] == 'link')]
        directories = [d for d in reversed(self.created) if not any(beneath(p, d) for p in index)]
        quote = lambda items: ' '.join(shlex.quote(i) for i in items)
        commands = []
        if remove:
            commands.append('rm -f -- ' + quote(remove))
        if directories:
            commands.append('rmdir -- ' + quote(directories))
        if restore:
            commands.append('git restore -- ' + quote(restore))
        return commands, links


def publish(tree, spec, plan, verb):
    publisher = Publisher(tree, plan)
    try:
        publisher.run()
    except BaseException as exc:
        commands, links = publisher.restore_commands()
        lines = [f'{spec.command} {verb}: failed after {publisher.step} step(s): {exc}']
        if publisher.touched:
            lines.append('It changed:')
            lines += ['  ' + shlex.quote(rel) for rel, _ in publisher.touched]
            lines.append(f'Restore them from {shlex.quote(str(tree.root))} with:')
            lines += ['  ' + c for c in commands]
            if links:
                lines.append(f'then rerun `{spec.command} sync --link` for the link-mode symlinks: ' + ' '.join(links))
        lines.append(f'Until the tree is restored, `{spec.command} sync --check` and doctor fail.')
        raise Broken('\n'.join(lines)) from exc
    publisher.prune()
    return publisher


# --- the project operations ---------------------------------------------------------------------

def refuse(spec, verb, refusals):
    raise Refused('\n'.join([f'{spec.command} {verb}: refusing; nothing was changed:'] +
                            ['  - ' + r for r in refusals]))


def read_inventory(tree, spec):
    rel = f'{spec.inventory_dir}/inventory.json'
    state = tree.state(rel)
    if state is None:
        return None
    if state[0] != 'file':
        raise Refused(f'{rel} is not a regular file')
    try:
        inventory = json.loads(tree.text(rel))
    except ValueError as exc:
        raise Refused(f'{rel}: not valid JSON: {exc}') from None
    if not isinstance(inventory, dict) or inventory.get('format') != FORMAT or inventory.get('component') != spec.name:
        raise Refused(f'{rel}: not a format-{FORMAT} {spec.name} inventory')
    for key, kind in (('files', dict), ('directories', list), ('symlinks', dict), ('json_entries', list), ('blocks', list)):
        if not isinstance(inventory.get(key), kind):
            raise Refused(f'{rel}: malformed {key}')
    if inventory.get('mode') not in ('copy', 'link'):
        raise Refused(f'{rel}: malformed mode')
    return inventory


def read_descriptor(tree, spec, required=True):
    state = tree.state(spec.descriptor)
    if state is None:
        if required:
            raise Refused(f'not set up: no {spec.descriptor} at {tree.root}; run `{spec.command} init`')
        return None, None
    if state[0] != 'file':
        raise Refused(f'{spec.descriptor} is not a regular file')
    text = tree.text(spec.descriptor)
    return text, spec.read_descriptor(text)


def pinned(spec, values):
    revision = values.get(spec.revision_key, '')
    if not FULL_REVISION.fullmatch(revision):
        raise Refused(f'{spec.descriptor}: {spec.revision_key} must be a full 40-hex commit, found {revision!r}')
    if not spec.runtime.has(revision):
        raise Broken(f'the pinned commit {revision} is missing from {spec.runtime.path}; fetch it there')
    return revision


def summarize(tree, spec, verb, plan, link_mode=False):
    paths = plan.paths()
    revision = plan.inventory['revision'] if plan.inventory else None
    mode = plan.inventory['mode'] if plan.inventory else None
    if verb == 'remove':
        head = f'{spec.command} remove: removed the {spec.name} project files.'
    else:
        head = f'{spec.command} {verb}: {spec.name} {revision} is installed ({mode} mode).'
    lines = [head]
    if not paths:
        lines.append('Nothing changed.')
        return '\n'.join(lines) + '\n'
    lines.append('Changed:')
    lines += ['  ' + shlex.quote(p) for p in paths]
    if link_mode:
        lines.append(f'Link mode is development state: do not stage or commit it; `{spec.command} sync` restores '
                     'the committed copies.')
    else:
        locations = [tree.git_path(p) or p for p in paths]
        lines.append('Review them, then stage them:')
        lines.append('  git add -A -- ' + ' '.join(shlex.quote(p) for p in locations))
    return '\n'.join(lines) + '\n'


def execute(tree, spec, verb, plan, exempt=(), link_mode=False):
    if plan.refusals:
        refuse(spec, verb, plan.refusals)
    preflight(tree, plan, exempt)
    if plan.refusals:
        refuse(spec, verb, plan.refusals)
    publish(tree, spec, plan, verb)
    return summarize(tree, spec, verb, plan, link_mode)


def init(root, spec, revision, descriptor, scaffold=None):
    tree = Tree(root)
    with installer_lock(tree.root):
        if tree.state(spec.descriptor) is not None:
            raise Refused(f'{spec.command} init: {spec.descriptor} already exists; use `{spec.command} sync` '
                          f'or `{spec.command} update REV`')
        values = spec.read_descriptor(descriptor)
        desired = desired_state(spec, revision, values, shared=claude_shared(tree))
        plan = make_plan(tree, spec, desired, None, scaffold=scaffold, descriptor=descriptor)
        return execute(tree, spec, 'init', plan)


def sync(root, spec, link=False, source=None):
    tree = Tree(root)
    with installer_lock(tree.root):
        _, values = read_descriptor(tree, spec)
        inventory = read_inventory(tree, spec)
        if inventory is None:
            raise Refused(f'{spec.command} sync: no {spec.inventory_dir}/inventory.json; for a project whose '
                          f'{spec.descriptor} predates project delivery, run `{spec.command} update REV`')
        revision = pinned(spec, values)
        link_source = None
        if link:
            link_source = os.path.realpath(source or spec.runtime.path)
            if not os.path.isdir(link_source):
                raise Broken(f'--source: not a directory: {link_source}')
        desired = desired_state(spec, revision, values, 'link' if link else 'copy', link_source,
                                inventory.get('claude_skills') == 'shared')
        if link:
            for name, skill in desired.skills:
                if not os.path.isfile(os.path.join(link_source, skill, 'SKILL.md')):
                    raise Broken(f'--source {link_source} has no {skill}/SKILL.md')
        plan = make_plan(tree, spec, desired, inventory)
        exempt = []
        if inventory['mode'] == 'link':
            exempt = [rel for rel, target in inventory['symlinks'].items() if target.startswith('/')]
            exempt.append(f'{spec.inventory_dir}/inventory.json')
        return execute(tree, spec, 'sync', plan, exempt, link_mode=link)


def update(root, spec, target):
    tree = Tree(root)
    with installer_lock(tree.root):
        text, _ = read_descriptor(tree, spec)
        inventory = read_inventory(tree, spec)
        if inventory and inventory['mode'] == 'link':
            raise Refused(f'{spec.command} update: the skills are in link mode; run `{spec.command} sync` first')
        revision = spec.runtime.commit(target)
        text = set_key(text, spec.revision_key, revision)
        values = spec.read_descriptor(text)
        shared = inventory.get('claude_skills') == 'shared' if inventory else claude_shared(tree)
        desired = desired_state(spec, revision, values, shared=shared)
        plan = make_plan(tree, spec, desired, inventory, descriptor=text)
        return execute(tree, spec, 'update', plan)


def remove(root, spec):
    tree = Tree(root)
    with installer_lock(tree.root):
        read_descriptor(tree, spec)
        inventory = read_inventory(tree, spec)
        if inventory is None:
            raise Refused(f'{spec.command} remove: no {spec.inventory_dir}/inventory.json, so nothing is installed; '
                          f'delete {spec.descriptor} by hand if you mean to')
        plan = make_plan(tree, spec, None, inventory, remove_descriptor=True)
        owned = set(inventory['files']) | {f'{spec.inventory_dir}/inventory.json'}
        if tree.state(spec.inventory_dir) == ('dir',):
            extra = [f for f in files_below(tree, spec.inventory_dir) if f not in owned]
            if extra:
                plan.refuse(f'unmanaged files in {spec.inventory_dir}/: ' + ', '.join(extra))
        exempt = []
        if inventory['mode'] == 'link':
            exempt = [rel for rel, target in inventory['symlinks'].items() if target.startswith('/')]
            exempt.append(f'{spec.inventory_dir}/inventory.json')
        return execute(tree, spec, 'remove', plan, exempt)


def check(root, spec, allow_dirty=False):
    """Read-only: (findings, link_mode) with findings as (status, kind, message).

    STATUS is ok, note or fail; KIND is files or runtime."""
    tree = Tree(root)
    findings = []
    try:
        text, values = read_descriptor(tree, spec)
        revision = values.get(spec.revision_key, '')
        if not FULL_REVISION.fullmatch(revision):
            return [('fail', 'files', f'{spec.descriptor}: {spec.revision_key} must be a full 40-hex commit, '
                                      f'found {revision!r}')], False
        inventory = read_inventory(tree, spec)
        if inventory is None:
            return [('fail', 'files', f'no {spec.inventory_dir}/inventory.json: the project files are not '
                                      f'installed; run `{spec.command} update REV`')], False
        if not spec.runtime.has(revision):
            return [('fail', 'files', f'the pinned commit {revision} is missing from {spec.runtime.path}')], False
        head = spec.runtime.head()
        if head == revision:
            findings.append(('ok', 'runtime', f'the runtime checkout {spec.runtime.path} is at the pin {revision}'))
        else:
            findings.append(('fail', 'runtime', f'the runtime checkout {spec.runtime.path} is at {head}, but this '
                                                f'project pins {revision}; check that revision out there'))
        if inventory.get('revision') != revision:
            findings.append(('fail', 'files', f"the inventory records {inventory.get('revision')}, but "
                                              f'{spec.descriptor} pins {revision}: an install did not finish'))
        link = inventory['mode'] == 'link'
        desired = desired_state(spec, revision, values, inventory['mode'], inventory.get('link_source'),
                                inventory.get('claude_skills') == 'shared')
        plan = make_plan(tree, spec, desired, inventory)
        for refusal in plan.refusals:
            findings.append(('fail', 'files', refusal))
        for rel in plan.paths():
            findings.append(('fail', 'files', f'differs from what {revision[:12]} installs: {rel}'))
        if link:
            for rel, target in desired.symlinks.items():
                if target.startswith('/') and not os.path.isdir(target):
                    findings.append(('fail', 'files', f'{rel} links to a missing directory {target}'))
            message = (f"link mode: the skills are linked to {inventory.get('link_source')} (development state; "
                       f'`{spec.command} sync` restores the copies)')
            findings.append(('note' if allow_dirty else 'fail', 'files', message))
        if not any(s == 'fail' and k == 'files' for s, k, _ in findings):
            findings.append(('ok', 'files', f"the {spec.name} project files match {revision} ({inventory['mode']} mode)"))
        return findings, link
    except (Refused, Broken) as exc:
        findings.append(('fail', 'files', str(exc)))
        return findings, False


def render_findings(findings):
    tags = dict(ok='[OK]  ', note='[NOTE]', fail='[FAIL]')
    return ''.join(f'{tags[status]} {message}\n' for status, _, message in findings)
