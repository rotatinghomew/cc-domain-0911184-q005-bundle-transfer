"""Bundle inspection and delta composition.

A package is only ever a *candidate* until it has been inspected member by
member.  This module never writes material to the receiving store; it only
turns a zip file (and, for a delta, the exact base version's verified
members) into a list of verified in-memory entries.

Two kinds of manifests exist:

``full``
    The archive contains exactly the files listed in ``manifest['files']``.

``delta``
    The archive contains only the changed files.  ``manifest['files']`` still
    describes the complete *final* set, ``manifest['removals']`` lists paths
    that disappear and ``manifest['base_version']`` names the exact version
    the delta applies to.  A removed path is simply absent from the final
    set; it is never represented by an empty file.
"""

import hashlib
import re
import zipfile
from pathlib import Path, PurePosixPath

from .errors import (CompositionError, IntegrityError, PackageError,
                     UnsafePath)

MAX_MEMBER_SIZE = 8_000_000
MAX_TOTAL_SIZE = 64_000_000
_HEX_DIGEST = re.compile(r'^[0-9a-fA-F]{64}$')


def safe_member_path(name):
    """Validate a manifest/member path and return it unchanged.

    Paths are interpreted as POSIX paths inside the bundle.  Anything that
    could escape, address a Windows device/stream, or rely on an alternate
    separator is rejected.  Directories are not material.
    """
    if not isinstance(name, str) or not name:
        raise UnsafePath('empty member path')
    if '\x00' in name or '\\' in name or ':' in name:
        raise UnsafePath('unsafe member path')
    if name.startswith('/') or name.endswith('/'):
        raise UnsafePath('unsafe member path')
    if '//' in name or name in ('.', '..'):
        raise UnsafePath('unsafe member path')
    parsed = PurePosixPath(name)
    if parsed.is_absolute() or '..' in parsed.parts or '.' in parsed.parts:
        raise UnsafePath('unsafe member path')
    if parsed.as_posix() != name:
        raise UnsafePath('unsafe member path')
    return name


def validate_manifest(manifest):
    """Validate the shape shared by full and delta manifests."""
    if not isinstance(manifest, dict):
        raise PackageError('manifest must be an object')
    asset = manifest.get('asset')
    if not isinstance(asset, str) or not asset:
        raise PackageError('manifest asset is invalid')
    version = manifest.get('version')
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise PackageError('manifest version is invalid')
    files = manifest.get('files')
    if not isinstance(files, dict):
        raise PackageError('manifest files are invalid')
    normalized = {}
    for name, entry in files.items():
        safe_member_path(name)
        if not isinstance(entry, dict):
            raise PackageError('manifest file entry is invalid')
        digest = entry.get('sha256')
        size = entry.get('size')
        if not isinstance(digest, str) or not _HEX_DIGEST.fullmatch(digest):
            raise PackageError('manifest file digest is invalid')
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise PackageError('manifest file size is invalid')
        normalized[name] = {'sha256': digest.lower(), 'size': size}
    mode = manifest.get('mode', 'full')
    if mode not in ('full', 'delta'):
        raise PackageError('manifest mode is invalid')
    # A full package with no files is meaningless.  A delta may legitimately
    # end with an empty final set when everything was removed.
    if not normalized and mode == 'full':
        raise PackageError('manifest files are invalid')
    removals = manifest.get('removals') or []
    if not isinstance(removals, (list, tuple)):
        raise PackageError('manifest removals are invalid')
    checked_removals = []
    for name in removals:
        safe_member_path(name)
        if name in checked_removals:
            raise PackageError('duplicate removal path')
        checked_removals.append(name)
    if mode == 'delta':
        base_version = manifest.get('base_version')
        if not isinstance(base_version, int) or isinstance(base_version, bool) or base_version < 0:
            raise PackageError('delta manifest needs an exact base version')
        if base_version == version:
            raise PackageError('a delta cannot depend on itself')
    return {'asset': asset, 'version': version, 'mode': mode,
            'base_version': manifest.get('base_version'),
            'removals': checked_removals, 'files': normalized}


def _check_removals_are_absent(manifest):
    for name in manifest.get('removals') or []:
        if name in manifest['files']:
            raise CompositionError('removed path is still in the final file set')


def _member_entries(members):
    """Normalise base/delta members supplied by callers.

    Accepted forms are a list of entry dictionaries (as returned by
    :func:`inspect_bundle`), a mapping of name to bytes, a mapping of name to
    an entry dictionary, or a path to another bundle.  Every entry is checked
    against its own digest; this is what prevents one version's bytes being
    silently substituted by another's.
    """
    if members is None:
        return []
    if isinstance(members, (str, Path)):
        return _entries_from_archive(Path(members), expected=None, exact=False)
    if isinstance(members, dict):
        items = members.items()
    else:
        items = []
        for member in members:
            if not isinstance(member, dict):
                raise PackageError('member entry is invalid')
            name = member.get('name')
            data = member.get('data')
            if isinstance(data, str):
                data = data.encode()
            if not isinstance(data, (bytes, bytearray)):
                raise PackageError('member data is missing')
            items.append((name, {'data': bytes(data),
                                 'sha256': member.get('sha256'),
                                 'size': member.get('size')}))
    entries = {}
    for name, value in items:
        safe_member_path(name)
        if name in entries:
            raise PackageError('duplicate member')
        if isinstance(value, dict):
            data = value.get('data')
            if isinstance(data, str):
                data = data.encode()
            if not isinstance(data, (bytes, bytearray)):
                raise PackageError('member data is missing')
            data = bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            if value.get('sha256') is not None and str(value['sha256']).lower() != digest:
                raise IntegrityError('member digest mismatch')
            if value.get('size') is not None and int(value['size']) != len(data):
                raise IntegrityError('member size mismatch')
        elif isinstance(value, (bytes, bytearray)):
            data = bytes(value)
            digest = hashlib.sha256(data).hexdigest()
        else:
            raise PackageError('member data is invalid')
        entries[name] = {'name': name, 'sha256': digest,
                         'size': len(data), 'data': data}
    return [entries[name] for name in entries]


def _entries_from_archive(path, expected, exact):
    entries = []
    seen = set()
    total = 0
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise PackageError('bundle is not a readable zip') from error
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise PackageError('bundle contains duplicate members')
        if exact and set(names) != set(expected or {}):
            raise IntegrityError('bundle members disagree with manifest')
        if not exact and expected is not None and not set(names) <= set(expected):
            raise CompositionError('bundle carries a member outside the final file set')
        for info in infos:
            name = safe_member_path(info.filename)
            if name in seen:
                raise PackageError('bundle contains duplicate members')
            seen.add(name)
            if info.is_dir() or info.flag_bits & 0x1:
                raise PackageError('unsupported bundle member')
            kind = info.external_attr >> 16 & 0o170000
            if kind not in (0, 0o100000):
                raise PackageError('unsupported bundle member')
            if info.file_size > MAX_MEMBER_SIZE:
                raise PackageError('unsupported bundle member')
            try:
                data = archive.read(name)
            except (OSError, zipfile.BadZipFile, RuntimeError) as error:
                raise PackageError('bundle member cannot be read') from error
            if len(data) != info.file_size:
                raise IntegrityError('bundle member size mismatch')
            total += len(data)
            if total > MAX_TOTAL_SIZE:
                raise PackageError('bundle is too large')
            digest = hashlib.sha256(data).hexdigest()
            if expected is not None and name in expected:
                wanted = expected[name]
                if digest != wanted['sha256'] or len(data) != wanted['size']:
                    raise IntegrityError('member digest mismatch')
            entries.append({'name': name, 'sha256': digest,
                            'size': len(data), 'data': data})
    return entries


def _reject_removed_members(path, removals):
    """Fail with a clear reason when an archive carries a removed path."""
    if not removals:
        return
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as error:
        raise PackageError('bundle is not a readable zip') from error
    for name in sorted(names & removals):
        raise CompositionError(f'delta carries a removed path: {name}')


def inspect_bundle(path, manifest, base=None, base_manifest=None):
    """Verify a downloaded bundle against ``manifest``.

    For a ``full`` manifest the archive must contain exactly the final set.
    For a ``delta`` manifest the archive may contain only changed members; in
    that case the returned entries are that verified subset.  Passing
    ``base`` (verified members of the exact ``base_version``) composes the
    final set and returns it only after item-by-item verification.  A base
    version is never guessed or replaced by whatever version happens to be
    newest.
    """
    checked = validate_manifest(manifest)
    _check_removals_are_absent(checked)
    expected = checked['files']
    if checked['mode'] == 'delta':
        removals = set(checked['removals'])
        _reject_removed_members(path, removals)
        entries = _entries_from_archive(path, expected=expected, exact=False)
        for entry in entries:
            if entry['name'] in removals:
                raise CompositionError('delta carries a removed path')
        if base is None and base_manifest is None:
            return entries
        return compose(manifest=manifest, base=base, delta=entries,
                       base_manifest=base_manifest)
    return _entries_from_archive(path, expected=expected, exact=True)


def _is_manifest(value):
    return (isinstance(value, dict) and 'asset' in value and 'version' in value
            and 'files' in value)


def compose(*args, manifest=None, base=None, delta=None, base_manifest=None):
    """Build the complete final file set from verified base and delta members.

    ``base`` supplies the exact ``manifest['base_version']`` final set,
    ``delta`` supplies the changed files from the delta archive, and
    ``manifest['removals']`` names files that disappear.  The result must
    correspond item by item to ``manifest['files']``; otherwise nothing is
    returned.  Removed files are absent, never zero-length.

    Calls may pass the three values positionally in any order (the manifest
    is recognized by its shape) or by keyword.
    """
    positional = list(args)
    if manifest is None:
        for value in positional:
            if _is_manifest(value):
                manifest = value
                positional.remove(value)
                break
    if base is None and positional:
        base = positional.pop(0)
    if delta is None and positional:
        delta = positional.pop(0)
    if positional:
        raise CompositionError('too many arguments')
    if manifest is None:
        raise CompositionError('manifest is required')
    checked = validate_manifest(manifest)
    _check_removals_are_absent(checked)
    if checked['mode'] != 'delta':
        raise CompositionError('delta composition requires a delta manifest')
    expected = checked['files']
    removals = set(checked['removals'])

    base_entries = _member_entries(base)
    if base_manifest is not None:
        checked_base = validate_manifest(base_manifest)
        if checked_base['asset'] != checked['asset']:
            raise CompositionError('base version belongs to another asset')
        if checked_base['version'] != checked['base_version']:
            raise CompositionError('base is not the declared dependency version')
        if set(base_manifest['files']) != {entry['name'] for entry in base_entries}:
            raise CompositionError('base members are incomplete')
        for entry in base_entries:
            wanted = checked_base['files'][entry['name']]
            if (entry['sha256'] != wanted['sha256']
                    or entry['size'] != wanted['size']):
                raise IntegrityError('base member digest mismatch')

    final = {}
    for entry in base_entries:
        if entry['name'] in removals:
            continue
        final[entry['name']] = entry
    for entry in _member_entries(delta):
        name = entry['name']
        if name in removals:
            raise CompositionError('delta carries a removed path')
        if name not in expected:
            raise CompositionError('delta carries a member outside the final file set')
        wanted = expected[name]
        if entry['sha256'] != wanted['sha256'] or entry['size'] != wanted['size']:
            raise IntegrityError('delta member digest mismatch')
        final[name] = entry

    missing = sorted(set(expected) - set(final))
    extra = sorted(set(final) - set(expected))
    if missing or extra:
        raise CompositionError(
            'final file set does not match the manifest'
            + (f'; missing {missing}' if missing else '')
            + (f'; unexpected {extra}' if extra else ''))
    if set(final) & removals:
        raise CompositionError('removed path is still in the final file set')
    for name, entry in final.items():
        wanted = expected[name]
        if entry['sha256'] != wanted['sha256'] or entry['size'] != wanted['size']:
            raise IntegrityError('final member digest mismatch')
    return [final[name] for name in expected]


# Descriptive aliases for callers that use different vocabulary.
member_entries = _member_entries
compose_bundle = compose
compose_members = compose
compose_final = compose
apply_delta = compose
resolve_delta = compose
