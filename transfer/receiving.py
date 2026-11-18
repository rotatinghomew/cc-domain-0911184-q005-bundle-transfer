"""The receiving list: verified material only.

Three states are deliberately kept apart:

* bytes retained by :mod:`transfer.fetch` are unverified transfer state;
* a package accepted by :func:`transfer.package.inspect_bundle` is verified
  content, but only exists in the working area;
* a version becomes part of the receiving list only after *every* member has
  been verified.  A version's rows, its content-addressed blobs and its
  receipt are written in one transaction, so an interruption or a rejection
  never leaves a half-referenced version behind.

Rows are immutable per ``(asset, version, name)``.  Accepting a delta version
therefore adds rows for that version's complete final file set; it never
rewrites or deletes rows belonging to older versions that other deliveries
may still reference.  A removed file simply has no row - it is not stored as
an empty file.
"""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .errors import (ImmutableVersionConflict, IntegrityError, PackageError,
                     RevokedVersion, VersionUnavailable)
from .package import member_entries, safe_member_path, validate_manifest

_HEX_DIGEST = re.compile(r'^[0-9a-fA-F]{64}$')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Receiving:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'receiving.sqlite3')
        self.db.execute('''CREATE TABLE IF NOT EXISTS members(
            asset TEXT, version INTEGER, name TEXT, digest TEXT,
            PRIMARY KEY(asset,version,name))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS receipts(
            asset TEXT, version INTEGER, mode TEXT, base_version INTEGER,
            package_sha256 TEXT, received_at TEXT,
            PRIMARY KEY(asset,version))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS acquisitions(
            asset TEXT, version INTEGER, mode TEXT, base_version INTEGER,
            downloaded_version INTEGER, package_sha256 TEXT, url TEXT,
            state TEXT, reason TEXT, updated_at TEXT,
            PRIMARY KEY(asset,version))''')
        self.db.execute('CREATE INDEX IF NOT EXISTS members_digest ON members(digest)')
        self.db.commit()

    # ------------------------------------------------------------------
    # Material access
    # ------------------------------------------------------------------
    def _blob(self, digest):
        return self.root / digest

    def _store_blob(self, digest, data):
        path = self._blob(digest)
        # Content-addressed names can be written concurrently; verify an
        # existing blob instead of trusting its name.
        if path.exists():
            try:
                if hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                    return
            except OSError:
                pass
        handle, temporary = tempfile.mkstemp(prefix='.' + digest[:16] + '.',
                                             suffix='.tmp', dir=self.root)
        try:
            with os.fdopen(handle, 'wb') as output:
                output.write(data)
                output.flush()
                try:
                    os.fsync(output.fileno())
                except OSError:
                    pass
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def received_version(self, asset, version):
        return self.db.execute(
            'SELECT 1 FROM receipts WHERE asset=? AND version=?',
            (asset, version)).fetchone() is not None

    def rows(self):
        return self.db.execute(
            'SELECT asset,version,name,digest FROM members ORDER BY asset,version,name').fetchall()

    def entries(self, asset, version):
        """Return verified members of an already received version.

        Used as the exact base for a delta.  A missing version raises
        :class:`VersionUnavailable`; callers must not substitute another
        version that happens to be present locally.  A received version whose
        final set is empty legitimately has no members.
        """
        if not self.received_version(asset, version):
            raise VersionUnavailable(f'{asset} version {version} has not been received')
        rows = self.db.execute(
            'SELECT name,digest FROM members WHERE asset=? AND version=? ORDER BY name',
            (asset, version)).fetchall()
        entries = []
        for name, digest in rows:
            safe_member_path(name)
            path = self._blob(digest)
            try:
                data = path.read_bytes()
            except OSError as error:
                raise IntegrityError('stored member is missing') from error
            actual = hashlib.sha256(data).hexdigest()
            if actual != digest:
                raise IntegrityError('stored member digest mismatch')
            entries.append({'name': name, 'sha256': digest,
                            'size': len(data), 'data': data})
        return entries

    def received_manifest(self, asset, version):
        """Describe the exact received final file set of a version."""
        if not self.received_version(asset, version):
            raise VersionUnavailable(f'{asset} version {version} has not been received')
        entries = self.entries(asset, version)
        row = self.db.execute(
            'SELECT mode,base_version FROM receipts WHERE asset=? AND version=?',
            (asset, version)).fetchone()
        return {'asset': asset, 'version': version, 'mode': 'full',
                'base_version': None, 'removals': [], 'revoked': False,
                'files': {entry['name']: {'sha256': entry['sha256'],
                                          'size': entry['size']}
                          for entry in entries}}

    def versions(self, asset=None):
        query = ('SELECT asset,version FROM receipts')
        params = ()
        if asset is not None:
            query += ' WHERE asset=?'
            params = (asset,)
        query += ' ORDER BY asset,version'
        return self.db.execute(query, params).fetchall()

    # ------------------------------------------------------------------
    # Acceptance
    # ------------------------------------------------------------------
    def accept(self, manifest, members):
        """Atomically add a fully verified final file set to the receiving list.

        ``members`` must cover exactly ``manifest['files']`` with matching
        sizes and digests.  A revoked version is refused without touching
        anything already received.  Re-accepting the same version is
        idempotent and does not create a second receipt.
        """
        if manifest.get('revoked'):
            self.record_acquisition(manifest, state='refused', reason='revoked version')
            raise RevokedVersion('revoked version cannot be received')
        checked = validate_manifest(manifest)

        normalized = member_entries(members)
        by_name = {}
        for member in normalized:
            name = safe_member_path(member['name'])
            if name in by_name:
                raise PackageError('duplicate member')
            by_name[name] = member
        if set(by_name) != set(checked['files']):
            raise PackageError('incomplete bundle')
        for name, member in by_name.items():
            expected = checked['files'][name]
            if (member['sha256'] != expected['sha256']
                    or member['size'] != expected['size']):
                raise IntegrityError('unverified content')
            digest = hashlib.sha256(member['data']).hexdigest()
            if digest != expected['sha256']:
                raise IntegrityError('unverified content')

        asset = checked['asset']
        version = checked['version']
        existing = self.db.execute(
            'SELECT name,digest FROM members WHERE asset=? AND version=? ORDER BY name',
            (asset, version)).fetchall()
        existing_names = {name for name, digest in existing}
        if existing_names and existing_names != set(by_name):
            raise ImmutableVersionConflict('received version has a different file set')
        for name, digest in existing:
            if by_name[name]['sha256'] != digest:
                raise ImmutableVersionConflict('immutable asset version conflict')

        package_sha = manifest.get('sha256')
        previous = self.db.execute(
            'SELECT package_sha256 FROM receipts WHERE asset=? AND version=?',
            (asset, version)).fetchone()
        if previous and package_sha and previous[0] != package_sha:
            raise ImmutableVersionConflict('version was received from different content')

        with self.db:
            for name in sorted(by_name):
                member = by_name[name]
                self._store_blob(member['sha256'], member['data'])
                self.db.execute(
                    'INSERT OR IGNORE INTO members VALUES(?,?,?,?)',
                    (asset, version, name, member['sha256']))
            self.db.execute(
                'INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?,?)',
                (asset, version, checked['mode'], checked['base_version'],
                 package_sha, _now()))
            self.db.execute(
                '''INSERT INTO acquisitions(
                       asset,version,mode,base_version,downloaded_version,
                       package_sha256,url,state,reason,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(asset,version) DO UPDATE SET
                       mode=excluded.mode, base_version=excluded.base_version,
                       downloaded_version=excluded.downloaded_version,
                       package_sha256=COALESCE(excluded.package_sha256,
                                               acquisitions.package_sha256),
                       url=COALESCE(excluded.url, acquisitions.url),
                       state=excluded.state,
                       reason=excluded.reason, updated_at=excluded.updated_at''',
                (asset, version, checked['mode'], checked['base_version'],
                 version, package_sha, manifest.get('url'), 'received', None, _now()))
        return self.receipt(asset, version)

    def receipt(self, asset, version):
        row = self.db.execute(
            'SELECT asset,version,mode,base_version,package_sha256,received_at '
            'FROM receipts WHERE asset=? AND version=?', (asset, version)).fetchone()
        if row is None:
            raise VersionUnavailable(f'{asset} version {version} has not been received')
        return {'asset': row[0], 'version': row[1], 'mode': row[2],
                'base_version': row[3], 'sha256': row[4], 'received_at': row[5]}

    def receipts(self, asset=None):
        query = ('SELECT asset,version,mode,base_version,package_sha256,received_at '
                 'FROM receipts')
        params = ()
        if asset is not None:
            query += ' WHERE asset=?'
            params = (asset,)
        query += ' ORDER BY asset,version'
        return [{'asset': row[0], 'version': row[1], 'mode': row[2],
                 'base_version': row[3], 'sha256': row[4], 'received_at': row[5]}
                for row in self.db.execute(query, params).fetchall()]

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def record_acquisition(self, manifest, *, state, reason=None, url=None,
                           downloaded_version=None, package_sha256=None):
        """Record why a logical asset version was (or was not) received."""
        try:
            checked = validate_manifest({key: manifest.get(key) for key in
                                         ('asset', 'version', 'files', 'mode',
                                          'base_version', 'removals')})
            asset, version = checked['asset'], checked['version']
            mode, base_version = checked['mode'], checked['base_version']
        except (PackageError, AttributeError):
            asset = manifest.get('asset')
            version = manifest.get('version')
            mode = manifest.get('mode')
            base_version = manifest.get('base_version')
            if asset is None or version is None:
                raise
        digest = package_sha256 if package_sha256 is not None else manifest.get('sha256')
        with self.db:
            self.db.execute(
                '''INSERT INTO acquisitions(
                       asset,version,mode,base_version,downloaded_version,
                       package_sha256,url,state,reason,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(asset,version) DO UPDATE SET
                       mode=excluded.mode, base_version=excluded.base_version,
                       downloaded_version=COALESCE(excluded.downloaded_version,
                                                   acquisitions.downloaded_version),
                       package_sha256=COALESCE(excluded.package_sha256,
                                               acquisitions.package_sha256),
                       url=COALESCE(excluded.url, acquisitions.url),
                       state=excluded.state, reason=excluded.reason,
                       updated_at=excluded.updated_at''',
                (asset, version, mode, base_version, downloaded_version,
                 digest, url, state, reason, _now()))

    def acquisitions(self, asset=None):
        query = ('SELECT asset,version,mode,base_version,downloaded_version,'
                 'package_sha256,url,state,reason,updated_at FROM acquisitions')
        params = ()
        if asset is not None:
            query += ' WHERE asset=?'
            params = (asset,)
        query += ' ORDER BY asset,version'
        return [{'asset': row[0], 'version': row[1], 'mode': row[2],
                 'base_version': row[3], 'downloaded_version': row[4],
                 'sha256': row[5], 'url': row[6], 'state': row[7],
                 'reason': row[8], 'updated_at': row[9]}
                for row in self.db.execute(query, params).fetchall()]

    def diagnose(self, asset):
        """Trace one logical asset: dependencies, received and outstanding versions."""
        received = self.receipts(asset)
        received_versions = {receipt['version'] for receipt in received}
        pending = []
        for record in self.acquisitions(asset):
            if record['state'] in ('received', 'complete') and record['version'] in received_versions:
                continue
            dependency = record['base_version']
            explanation = record['reason'] or record['state']
            if dependency is not None and dependency not in received_versions:
                explanation = f'depends on version {dependency}, which is not received: {explanation}'
            pending.append({**record, 'explanation': explanation})
        return {'asset': asset, 'received': received,
                'versions': sorted(received_versions), 'pending': pending}

    def incomplete(self, asset=None):
        query = ("SELECT asset,version,base_version,state,reason FROM acquisitions "
                 "WHERE state!='received'")
        params = ()
        if asset is not None:
            query += ' AND asset=?'
            params = (asset,)
        query += ' ORDER BY asset,version'
        return [{'asset': row[0], 'version': row[1], 'base_version': row[2],
                 'state': row[3], 'reason': row[4]}
                for row in self.db.execute(query, params).fetchall()]

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
