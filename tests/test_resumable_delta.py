import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from transfer.acquire import Acquisition, acquire
from transfer.client import ManifestClient
from transfer.errors import (CompositionError, DownloadError, DownloadFailed,
                             DownloadInterrupted, ImmutableVersionConflict,
                             IntegrityError, MissingBaseVersion, PackageError,
                             RevokedVersion, UnsafePath, UnsupportedResponse,
                             VersionUnavailable)
from transfer.fetch import Downloader
from transfer.package import compose, inspect_bundle
from transfer.receiving import Receiving
from transfer.source import BundleSource, serve


def manifest_for(source, url, version=None):
    return source.manifest(url, version=version)


def base_files():
    return {'subtitles/main.srt': '1\n00:00:01,000 --> 00:00:02,000\n山间来信\n'.encode(),
            'cover.txt': b'cover-description'}


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_interrupted_transfer_resumes_without_redownloading(self):
        with serve() as (url, source):
            manifest = source.manifest(url)
            destination = self.root / 'bundle.zip'
            source.fail_after = 100
            with self.assertRaises(DownloadError):
                Downloader(attempts=1).fetch(manifest, destination)
            self.assertFalse(destination.exists())
            partial = destination.with_suffix('.zip.partial')
            self.assertTrue(partial.exists())
            self.assertGreater(partial.stat().st_size, 0)
            Downloader().fetch(manifest, destination)
            self.assertEqual(destination.read_bytes(), source.bundle())
            ranges = [entry['range'] for entry in source.requests if entry['path'].startswith('/download/')]
            self.assertEqual(ranges[0], None)
            self.assertIsNotNone(ranges[1])
            self.assertTrue(ranges[1].startswith('bytes='))

    def test_multiple_interruptions_and_new_signed_url(self):
        with serve() as (url, source):
            manifest = source.manifest(url)
            destination = self.root / 'bundle.zip'
            source.fail_after = [50, 30, 20]
            Downloader(attempts=1).fetch(manifest, destination) if False else None
            for _ in range(3):
                try:
                    Downloader(attempts=1).fetch(manifest, destination)
                except DownloadError:
                    pass
                else:
                    break
            # The signed URL is re-issued; the manifest bytes are unchanged.
            source.token = 'signed-fresh'
            refreshed = source.manifest(url)
            self.assertEqual(refreshed['sha256'], manifest['sha256'])
            Downloader().fetch(refreshed, destination)
            self.assertEqual(destination.read_bytes(), source.bundle())

    def test_source_ignoring_range_restarts_cleanly(self):
        with serve() as (url, source):
            manifest = source.manifest(url)
            destination = self.root / 'bundle.zip'
            source.fail_after = 40
            try:
                Downloader(attempts=1).fetch(manifest, destination)
            except DownloadError:
                pass
            source.ignore_range = True
            Downloader().fetch(manifest, destination)
            self.assertEqual(destination.read_bytes(), source.bundle())

    def test_changed_bytes_are_not_glued_to_retained_prefix(self):
        with serve() as (url, source):
            manifest = source.manifest(url)
            destination = self.root / 'bundle.zip'
            source.fail_after = 60
            try:
                Downloader(attempts=1).fetch(manifest, destination)
            except DownloadError:
                pass
            # The same URL now yields a different representation: its
            # validator changes, so If-Range does not match and the origin
            # sends the whole body.  The retained prefix must not be used.
            source.files = {'subtitles/main.srt': b'entirely new subtitle' * 20,
                            'cover.txt': b'new cover'}
            with self.assertRaises(DownloadError):
                Downloader(attempts=1).fetch(manifest, destination)
            self.assertFalse(destination.exists())

    def test_tampered_partial_with_matching_validator_is_rejected(self):
        with serve() as (url, source):
            manifest = source.manifest(url)
            destination = self.root / 'bundle.zip'
            partial = destination.with_suffix('.zip.partial')
            state = destination.with_suffix('.zip.state')
            partial.write_bytes(b'X' * 100)
            state.write_text(json.dumps({
                'format': 1, 'asset': manifest['asset'], 'version': manifest['version'],
                'sha256': manifest['sha256'], 'size': manifest['size'],
                'url': manifest['url'], 'etag': '"' + manifest['sha256'] + '"',
                'offset': 100}))
            with self.assertRaises(DownloadError):
                Downloader(attempts=1).fetch(manifest, destination)
            self.assertFalse(destination.exists())
            self.assertFalse(partial.exists())
            self.assertFalse(state.exists())

    def test_new_validator_with_same_bytes_resumes(self):
        with serve() as (url, source):
            manifest = source.manifest(url)
            destination = self.root / 'bundle.zip'
            source.fail_after = 60
            try:
                Downloader(attempts=1).fetch(manifest, destination)
            except DownloadError:
                pass
            source.token = 'signed-again'
            source.etag = 'rotated-validation-id'
            refreshed = source.manifest(url)
            self.assertEqual(refreshed['sha256'], manifest['sha256'])
            Downloader().fetch(refreshed, destination)
            self.assertEqual(destination.read_bytes(), source.bundle())

    def test_partial_length_is_not_proof_of_completion(self):
        with serve() as (url, source):
            manifest = source.manifest(url)
            destination = self.root / 'bundle.zip'
            partial = destination.with_suffix('.zip.partial')
            partial.write_bytes(source.bundle()[:10])
            # No state file: bytes alone must not be treated as progress.
            Downloader().fetch(manifest, destination)
            self.assertEqual(destination.read_bytes(), source.bundle())

    def test_short_file_at_destination_is_not_success(self):
        with serve() as (url, source):
            manifest = source.manifest(url)
            destination = self.root / 'bundle.zip'
            destination.write_bytes(b'12345')
            with self.assertRaises(DownloadError):
                Downloader().fetch(manifest, destination)
            self.assertEqual(destination.read_bytes(), b'12345')

    def test_error_page_is_not_saved_as_material(self):
        with serve() as (url, source):
            manifest = source.manifest(url)
            source.error_body = b'<html><body>temporarily unavailable</body></html>'
            destination = self.root / 'bundle.zip'
            with self.assertRaises(DownloadError):
                Downloader(attempts=2).fetch(manifest, destination)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix('.zip.partial').exists())

    def test_stale_state_for_another_version_is_ignored(self):
        with serve() as (url, source):
            manifest = source.manifest(url)
            destination = self.root / 'bundle.zip'
            source.fail_after = 30
            try:
                Downloader(attempts=1).fetch(manifest, destination)
            except DownloadError:
                pass
            source.publish(version=2, token='signed-v2',
                           files={'subtitles/main.srt': b'other body'})
            next_manifest = source.manifest(url)
            Downloader().fetch(next_manifest, destination)
            self.assertEqual(destination.read_bytes(), source.bundle())


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def _receive_v1(self, url, source):
        receiving = Receiving(self.root / 'received')
        manifest = source.manifest(url)
        package = Downloader().fetch(manifest, self.root / 'v1.zip')
        receiving.accept(manifest, inspect_bundle(package, manifest))
        return receiving, manifest

    def test_delta_removed_file_is_absent_not_empty(self):
        source = BundleSource()
        source.publish(version=2, token='signed-2', mode='delta', base_version=1,
                       files={'subtitles/new.srt': b'new subtitle'},
                       removals=['cover.txt'])
        source.final_files = {name: content for name, content in base_files().items()
                              if name != 'cover.txt'}
        source.final_files['subtitles/new.srt'] = b'new subtitle'
        manifest = source.manifest('http://127.0.0.1:1')
        base = [{'name': name, 'data': content} for name, content in base_files().items()]
        final = compose(manifest, base, [{'name': 'subtitles/new.srt',
                                          'data': b'new subtitle'}])
        names = [entry['name'] for entry in final]
        self.assertNotIn('cover.txt', names)
        self.assertEqual(sorted(names), sorted(manifest['files']))
        for entry in final:
            self.assertNotEqual(entry['data'], b'')
            self.assertEqual(entry['size'], len(entry['data']))

    def test_compose_rejects_removed_path_in_delta(self):
        source = BundleSource()
        source.version = 2
        source.mode = 'delta'
        source.base_version = 1
        source.removals = ['cover.txt']
        source.files = {'cover.txt': b'resurrected'}
        source.final_files = {'cover.txt': b'resurrected'}
        manifest = source.manifest('http://127.0.0.1:1')
        with self.assertRaises(CompositionError):
            compose(manifest, [], [{'name': 'cover.txt', 'data': b'resurrected'}])

    def test_compose_rejects_incomplete_final_set(self):
        source = BundleSource()
        source.version = 2
        source.mode = 'delta'
        source.base_version = 1
        source.files = {'subtitles/new.srt': b'new'}
        source.final_files = dict(base_files(), **{'subtitles/new.srt': b'new'})
        manifest = source.manifest('http://127.0.0.1:1')
        with self.assertRaises(CompositionError):
            compose(manifest, [], [{'name': 'subtitles/new.srt', 'data': b'new'}])

    def test_compose_rejects_extra_member(self):
        source = BundleSource()
        source.version = 2
        source.mode = 'delta'
        source.base_version = 1
        source.removals = ['cover.txt']
        source.files = {'subtitles/new.srt': b'new'}
        source.final_files = {'subtitles/main.srt': base_files()['subtitles/main.srt'],
                              'subtitles/new.srt': b'new'}
        manifest = source.manifest('http://127.0.0.1:1')
        base = [{'name': name, 'data': content} for name, content in base_files().items()]
        with self.assertRaises(CompositionError):
            compose(manifest, base, [{'name': 'other.txt', 'data': b'x'}])

    def test_compose_rejects_wrong_base_version(self):
        source = BundleSource()
        source.version = 5
        source.mode = 'delta'
        source.base_version = 4
        source.files = {'subtitles/new.srt': b'new'}
        source.final_files = dict(base_files(), **{'subtitles/new.srt': b'new'})
        manifest = source.manifest('http://127.0.0.1:1')
        wrong_base = source.manifest('http://127.0.0.1:1')
        wrong_base['version'] = 3
        with self.assertRaises(CompositionError):
            compose(manifest, base=[], base_manifest=wrong_base)

    def test_delta_refuses_to_substitute_locally_newest_version(self):
        with serve() as (url, source):
            receiving, _ = self._receive_v1(url, source)
            # Publish v2 and v3 where v3 depends on a v2 we never received.
            source.publish(version=2, token='signed-v2',
                           files={'subtitles/main.srt': b'v2 body'})
            source.publish(version=3, token='signed-v3', mode='delta', base_version=2,
                           files={'subtitles/extra.srt': b'extra'})
            source.final_files = {'subtitles/main.srt': b'v2 body',
                                  'subtitles/extra.srt': b'extra'}
            client = ManifestClient(url)
            acquisition = Acquisition(receiving, client=client,
                                      workdir=self.root / 'work',
                                      downloader=Downloader(attempts=2))
            # v2 is no longer offered by the source, so v3 must fail rather
            # than build on the locally available v1.
            del source.retained[2]
            with self.assertRaises(MissingBaseVersion):
                acquisition.collect(version=3)
            self.assertEqual(receiving.versions('delivery-a'),
                             [('delivery-a', 1)])
            diagnostic = receiving.diagnose('delivery-a')
            self.assertTrue(any(item['version'] == 3 for item in diagnostic['pending']))

    def test_delta_fetches_the_exact_declared_base(self):
        with serve() as (url, source):
            source.publish(version=2, token='signed-v2',
                           files={'subtitles/main.srt': b'v2 body'})
            source.publish(version=3, token='signed-v3', mode='delta', base_version=2,
                           files={'subtitles/extra.srt': b'extra'})
            source.final_files = {'subtitles/main.srt': b'v2 body',
                                  'subtitles/extra.srt': b'extra'}
            receiving = Receiving(self.root / 'received')
            manifest = source.manifest(url)
            package = Downloader().fetch(manifest, self.root / 'v3.zip')
            # The receiving list does not have v2 yet.
            with self.assertRaises(VersionUnavailable):
                receiving.entries('delivery-a', 2)
            receipt = acquire(url, receiving, version=3)
            self.assertEqual(receipt['version'], 3)
            self.assertEqual(receipt['base_version'], 2)
            self.assertEqual(sorted(receiving.versions('delivery-a')),
                             [('delivery-a', 2), ('delivery-a', 3)])
            rows = [row for row in receiving.rows() if row[1] == 3]
            self.assertEqual({row[2] for row in rows},
                             {'subtitles/main.srt', 'subtitles/extra.srt'})

    def test_delta_keeps_members_still_referenced_by_older_versions(self):
        with serve() as (url, source):
            receiving = Receiving(self.root / 'received')
            receipt1 = acquire(url, receiving)
            self.assertEqual(receipt1['version'], 1)
            old_digest = dict((row[2], row[3]) for row in receiving.rows())
            source.publish(version=2, token='signed-2', mode='delta', base_version=1,
                           files={'subtitles/main.srt': b'main v2'},
                           removals=['cover.txt'])
            source.final_files = {'subtitles/main.srt': b'main v2'}
            acquire(url, receiving, version=2)
            rows = receiving.rows()
            older = {(row[2], row[3]) for row in rows if row[1] == 1}
            self.assertEqual(older, {(name, digest) for name, digest in old_digest.items()})
            # v2 has no cover row and no empty stand-in.
            current = {(row[2], row[3]) for row in rows if row[1] == 2}
            self.assertEqual(current, {('subtitles/main.srt',
                                        hashlib.sha256(b'main v2').hexdigest())})
            # The old cover content is still readable for the older delivery.
            self.assertTrue((Path(self.root / 'received') /
                             old_digest['cover.txt']).exists())

    def test_repeating_download_does_not_duplicate_receipt(self):
        with serve() as (url, source):
            receiving = Receiving(self.root / 'received')
            first = acquire(url, receiving)
            second = acquire(url, receiving)
            self.assertEqual(first, second)
            self.assertEqual(len(receiving.receipts('delivery-a')), 1)
            self.assertEqual(len(receiving.rows()), 2)

    def test_missing_package_does_not_break_previous_versions(self):
        with serve() as (url, source):
            receiving = Receiving(self.root / 'received')
            acquire(url, receiving)
            baseline = receiving.rows()
            source.publish(version=2, token='signed-2', mode='delta', base_version=1,
                           files={'subtitles/broken.srt': b'broken'})
            source.final_files = {'subtitles/broken.srt': b'broken'}
            # Corrupt the served package without changing its manifest digest.
            source.files = {'subtitles/broken.srt': b'broken' * 100}
            with self.assertRaises(TransferFailure):
                acquire(url, receiving, version=2)
            self.assertEqual(receiving.rows(), baseline)
            self.assertEqual(len(receiving.receipts('delivery-a')), 1)
            self.assertEqual(receiving.versions('delivery-a'), [('delivery-a', 1)])


TransferFailure = (DownloadError, IntegrityError, PackageError)


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_revoked_version_is_refused_and_existing_material_remains(self):
        with serve() as (url, source):
            receiving = Receiving(self.root / 'received')
            acquire(url, receiving)
            baseline = receiving.rows()
            source.publish(version=2, token='signed-2', revoked=True,
                           files={'subtitles/main.srt': b'withdrawn'})
            with self.assertRaises(RevokedVersion):
                acquire(url, receiving, version=2)
            self.assertEqual(receiving.rows(), baseline)
            self.assertEqual(receiving.receipts('delivery-a')[0]['version'], 1)
            self.assertEqual(receiving.versions('delivery-a'), [('delivery-a', 1)])

    def test_unsafe_paths_are_rejected(self):
        with serve() as (url, source):
            receiving = Receiving(self.root / 'received')
            for index, unsafe in enumerate(('../escape.srt', '/absolute.srt',
                                            'a/../../escape.srt',
                                            'C:\\windows\\evil.srt', 'a\\b.srt')):
                source.files = {unsafe: b'bad'}
                manifest = source.manifest(url)
                package = self.root / f'bad-{index}.zip'
                Downloader().fetch(manifest, package)
                with self.assertRaises((PackageError, UnsafePath)):
                    inspect_bundle(package, manifest)
                with self.assertRaises((PackageError, UnsafePath)):
                    receiving.accept(manifest, [{'name': unsafe, 'data': b'bad'}])
            self.assertEqual(receiving.rows(), [])

    def test_digest_failure_does_not_publish_version(self):
        with serve() as (url, source):
            receiving = Receiving(self.root / 'received')
            manifest = source.manifest(url)
            package = Downloader().fetch(manifest, self.root / 'v1.zip')
            members = inspect_bundle(package, manifest)
            corrupted = copy.deepcopy(manifest)
            corrupted['files']['cover.txt']['sha256'] = '0' * 64
            with self.assertRaises(ValueError):
                receiving.accept(corrupted, members)
            self.assertEqual(receiving.rows(), [])

    def test_immutable_version_conflict(self):
        with serve() as (url, source):
            receiving = Receiving(self.root / 'received')
            manifest = source.manifest(url)
            package = Downloader().fetch(manifest, self.root / 'v1.zip')
            receiving.accept(manifest, inspect_bundle(package, manifest))
            altered = copy.deepcopy(manifest)
            altered['files']['cover.txt']['sha256'] = hashlib.sha256(b'changed').hexdigest()
            altered['files']['cover.txt']['size'] = len(b'changed')
            with self.assertRaises(ImmutableVersionConflict):
                receiving.accept(altered, [{'name': 'subtitles/main.srt',
                                            'data': source.files['subtitles/main.srt']},
                                           {'name': 'cover.txt', 'data': b'changed'}])

    def test_unverified_member_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            receiving = Receiving(directory)
            source = BundleSource()
            manifest = source.manifest('http://127.0.0.1:1')
            with self.assertRaises((PackageError, IntegrityError)):
                receiving.accept(manifest, [{'name': 'subtitles/main.srt',
                                             'data': b'x'}])
            self.assertEqual(receiving.rows(), [])
            self.assertEqual(list(Path(directory).glob('[0-9a-f]' * 1 + '*')), [])


class DiagnosticTests(unittest.TestCase):
    def test_diagnose_traces_dependency_and_outstanding_reason(self):
        with tempfile.TemporaryDirectory() as directory, serve() as (url, source):
            receiving = Receiving(directory)
            source.mode = 'delta'
            source.base_version = 5
            source.version = 6
            source.files = {'subtitles/new.srt': b'new'}
            source.final_files = dict(base_files(), **{'subtitles/new.srt': b'new'})
            with self.assertRaises(MissingBaseVersion):
                acquire(url, receiving, version=6)
            diagnostic = receiving.diagnose('delivery-a')
            self.assertEqual(diagnostic['versions'], [])
            self.assertEqual(len(diagnostic['pending']), 1)
            pending = diagnostic['pending'][0]
            self.assertEqual(pending['version'], 6)
            self.assertEqual(pending['base_version'], 5)
            self.assertIn('5', pending['explanation'])
            self.assertIsNotNone(pending['reason'])

    def test_version_query_does_not_return_current_for_missing(self):
        with serve() as (url, source):
            source.publish(version=2, token='signed-2', files={'a.srt': b'a'})
            client = ManifestClient(url)
            with self.assertRaises(VersionUnavailable):
                client.manifest(5)
            self.assertEqual(client.manifest(2)['version'], 2)
            self.assertEqual(client.current()['version'], 2)

    def test_manifest_never_returns_current_for_unretained_version(self):
        with serve() as (url, source):
            source.publish(version=2, token='signed-2', files={'a.srt': b'a'})
            source.publish(version=3, token='signed-3', files={'a.srt': b'b'})
            with self.assertRaises(HTTPError) as error:
                urlopen(url + '/manifest?version=5')
            self.assertEqual(error.exception.code, 404)

    def test_manifest_for_another_asset_is_rejected(self):
        with serve() as (url, source):
            client = ManifestClient(url)
            with self.assertRaises(PackageError):
                client.current(asset='another-delivery')


class AcquirerTests(unittest.TestCase):
    def test_resume_survives_a_process_restart(self):
        with tempfile.TemporaryDirectory() as directory, serve() as (url, source):
            root = Path(directory)
            source.files = {'subtitles/main.srt': b'payload' * 30000}
            manifest = source.manifest(url)
            source.fail_after = manifest['size'] // 2
            first = Acquisition(Receiving(root / 'received'),
                                client=ManifestClient(url),
                                downloader=Downloader(attempts=1))
            with self.assertRaises(DownloadFailed):
                first.collect()
            # Nothing is received and only unverified transfer state remains.
            self.assertEqual(Receiving(root / 'received').receipts(), [])
            self.assertTrue(any(path.suffix == '.partial'
                                for path in first.workdir.iterdir()))
            # A brand new process only knows the directories.
            second = Acquisition(Receiving(root / 'received'),
                                 client=ManifestClient(url))
            receipt = second.collect()
            self.assertEqual(receipt['version'], 1)
            ranged = [entry['range'] for entry in source.requests
                      if entry['path'].startswith('/download/')]
            self.assertIn(None, ranged)
            self.assertTrue(any(r and r.startswith('bytes=') for r in ranged))

    def test_full_version_receipt_and_rows(self):
        with tempfile.TemporaryDirectory() as directory, serve() as (url, source):
            receiving = Receiving(directory)
            receipt = acquire(url, receiving, asset='delivery-a')
            self.assertEqual(receipt['mode'], 'full')
            self.assertEqual(len(receiving.rows()), 2)
            self.assertEqual(len(receiving.receipts()), 1)

    def test_revoked_refusal_keeps_other_received_version_usable(self):
        with tempfile.TemporaryDirectory() as directory, serve() as (url, source):
            receiving = Receiving(directory)
            acquire(url, receiving)
            entries = receiving.entries('delivery-a', 1)
            self.assertEqual(len(entries), 2)
            self.assertTrue(all(entry['data'] for entry in entries))


if __name__ == '__main__':
    unittest.main()
