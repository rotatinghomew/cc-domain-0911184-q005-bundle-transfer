import hashlib
import sqlite3
from pathlib import Path


class Receiving:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'receiving.sqlite3')
        self.db.execute('''CREATE TABLE IF NOT EXISTS members(
            asset TEXT, version INTEGER, name TEXT, digest TEXT,
            PRIMARY KEY(asset,version,name))''')

    def accept(self, manifest, members):
        validated = []
        for member in members:
            digest = hashlib.sha256(member['data']).hexdigest()
            expected = manifest['files'].get(member['name'])
            if not expected or digest != expected['sha256'] or len(member['data']) != expected['size']:
                raise ValueError('unverified content')
            validated.append((member, digest))
        if {member['name'] for member, digest in validated} != set(manifest['files']):
            raise ValueError('incomplete bundle')
        with self.db:
            for member, digest in validated:
                path = self.root / digest
                if not path.exists():
                    path.write_bytes(member['data'])
                old = self.db.execute('SELECT digest FROM members WHERE asset=? AND version=? AND name=?',
                                      (manifest['asset'], manifest['version'], member['name'])).fetchone()
                if old and old[0] != digest:
                    raise ValueError('immutable asset version conflict')
                self.db.execute('INSERT OR IGNORE INTO members VALUES(?,?,?,?)',
                                (manifest['asset'], manifest['version'], member['name'], digest))

    def rows(self):
        return self.db.execute('SELECT asset,version,name,digest FROM members ORDER BY asset,version,name').fetchall()
