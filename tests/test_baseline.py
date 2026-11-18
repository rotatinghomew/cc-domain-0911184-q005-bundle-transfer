import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from transfer.fetch import Downloader
from transfer.package import inspect_bundle
from transfer.receiving import Receiving
from transfer.source import serve


class BundleTests(unittest.TestCase):
    def test_delta_manifest_describes_final_set_without_fabricating_members(self):
        with serve() as (url, source):
            source.mode = 'delta'
            source.base_version = 1
            source.version = 3
            inherited = dict(source.files)
            inherited.pop('cover.txt')
            inherited['subtitles/new.srt'] = b'new subtitle'
            source.files = {'subtitles/new.srt': b'new subtitle'}
            source.removals = ['cover.txt']
            source.final_files = inherited
            manifest = source.manifest(url)
            self.assertEqual(manifest['base_version'], 1)
            self.assertIn('subtitles/main.srt', manifest['files'])
            self.assertNotIn('cover.txt', manifest['files'])
            with self.assertRaises(HTTPError) as error:
                urlopen(url + '/manifest?version=1')
            self.assertEqual(error.exception.code, 404)

    def test_full_download_verify_receive(self):
        with tempfile.TemporaryDirectory() as directory, serve() as (url, source):
            manifest = source.manifest(url)
            bundle = Downloader().fetch(manifest, Path(directory) / 'bundle.zip')
            members = inspect_bundle(bundle, manifest)
            receiving = Receiving(Path(directory) / 'received')
            receiving.accept(manifest, members)
            receiving.accept(manifest, members)
            self.assertEqual(len(receiving.rows()), 2)

    def test_bad_bundle_is_not_promoted(self):
        with tempfile.TemporaryDirectory() as directory, serve() as (url, source):
            manifest = source.manifest(url)
            manifest['sha256'] = '0' * 64
            destination = Path(directory) / 'bundle.zip'
            with self.assertRaises(ValueError):
                Downloader().fetch(manifest, destination)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix('.zip.partial').exists())

    def test_range_and_if_range_protocol(self):
        with serve() as (url, source):
            manifest = source.manifest(url)
            etag = '"' + manifest['sha256'] + '"'
            with urlopen(Request(manifest['url'], headers={'Range': 'bytes=4-11', 'If-Range': etag})) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), source.bundle()[4:12])
                self.assertTrue(response.headers['Content-Range'].startswith('bytes 4-11/'))
            with urlopen(Request(manifest['url'], headers={'Range': 'bytes=4-', 'If-Range': '"different"'})) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), source.bundle())

    def test_signed_url_rotation_and_ignored_range(self):
        with serve() as (url, source):
            old = source.manifest(url)
            source.token = 'signed-2'
            with self.assertRaises(HTTPError):
                urlopen(old['url'])
            source.ignore_range = True
            new = source.manifest(url)
            self.assertEqual(new['sha256'], old['sha256'])
            with urlopen(Request(new['url'], headers={'Range': 'bytes=20-'})) as response:
                self.assertEqual(response.status, 200)

    def test_member_integrity_and_paths(self):
        with tempfile.TemporaryDirectory() as directory, serve() as (url, source):
            manifest = source.manifest(url)
            bundle = Path(directory) / 'bundle.zip'
            bundle.write_bytes(source.bundle())
            altered = copy.deepcopy(manifest)
            altered['files']['cover.txt']['sha256'] = '0' * 64
            with self.assertRaises(ValueError):
                inspect_bundle(bundle, altered)
            source.files = {'../escape.srt': b'local synthetic invalid path'}
            bundle.write_bytes(source.bundle())
            with self.assertRaises(ValueError):
                inspect_bundle(bundle, source.manifest(url))

    def test_accept_requires_all_verified_members(self):
        with tempfile.TemporaryDirectory() as directory, serve() as (url, source):
            receiving = Receiving(directory)
            with self.assertRaises(ValueError):
                receiving.accept(source.manifest(url), [])
            self.assertEqual(receiving.rows(), [])
