"""Setup-only checks; all inputs and installed documents are synthetic."""

import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from contextlib import closing

import pymupdf

from docker.bootstrap import install_credentials
from legal_analyzer.extraction import extract_text
from legal_analyzer.privacy import analyze_privacy, valid_turkish_national_id, valid_turkish_tax_number

ROOT = Path(__file__).resolve().parents[1]


def load_sample_builder():
    """samples/build_turkish_samples.py, imported by path (samples/ is not a package)."""
    spec = importlib.util.spec_from_file_location('build_turkish_samples', ROOT / 'samples/build_turkish_samples.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'install.json'
        self.db = Path(self.tmp.name) / 'documents.db'

    def test_fresh_random_credentials_are_private_and_survive_reruns(self):
        first = install_credentials(self.path, self.db)
        original = self.path.read_bytes()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(first['secret_key']), 64)
        self.assertGreaterEqual(len(first['password']), 32)
        self.assertEqual(install_credentials(self.path, self.db), first)
        self.assertEqual(self.path.read_bytes(), original)
        second = install_credentials(self.path.parent / 'second.json', self.db)
        self.assertNotEqual(first['secret_key'], second['secret_key'])
        self.assertNotEqual(first['password'], second['password'])

    def test_missing_credentials_never_replace_an_existing_install(self):
        self.db.write_bytes(b'synthetic existing database sentinel')
        with self.assertRaisesRegex(RuntimeError, 'Database exists'):
            install_credentials(self.path, self.db)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.db.read_bytes(), b'synthetic existing database sentinel')

    def test_invalid_credentials_fail_closed_without_rewriting(self):
        for value in ({}, {'username': 'admin', 'password': '', 'secret_key': 'x'}):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                before = self.path.read_bytes()
                with self.assertRaises(RuntimeError):
                    install_credentials(self.path, self.db)
                self.assertEqual(self.path.read_bytes(), before)

    def test_creation_is_exclusive(self):
        # Model a concurrent setup creating the file between exists() and open().
        self.path.write_text('another setup owns this file')
        with patch.object(Path, 'exists', return_value=False):
            with self.assertRaises(FileExistsError):
                install_credentials(self.path, self.db)
        self.assertEqual(self.path.read_text(), 'another setup owns this file')


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ('db', 'legal_analyzer', 'blueprints', 'docker', 'samples'):
            shutil.copytree(ROOT / directory, self.root / directory,
                            ignore=shutil.ignore_patterns('*.db*', '__pycache__'))
        for name in ('app.py', 'classify.py', 'web_security.py'):
            shutil.copyfile(ROOT / name, self.root / name)
        self.db = self.root / 'db' / 'legal_documents.db'
        self.credentials = self.root / 'install.json'

    def run_bootstrap(self):
        return subprocess.run([sys.executable, '-c',
            "from docker.bootstrap import *; bootstrap(install_credentials(ROOT / 'install.json', ROOT / 'db/legal_documents.db'))"],
            cwd=self.root, capture_output=True, text=True, check=False)

    def rows(self, table):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()

    def test_clean_bootstrap_and_rerun_preserve_documents_reviews_and_credentials(self):
        first = self.run_bootstrap()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(self.rows('documents')), 6)
        self.assertEqual(len(self.rows('users')), 1)
        self.assertEqual(len(self.rows('document_matters')), 6)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE privacy_findings SET review_status = 'approved' WHERE id = 1")
            conn.execute("INSERT INTO audit_log (action, metadata) VALUES ('synthetic.review', '{}')")
            conn.execute("INSERT INTO documents (filename, filepath, file_extension, file_size) VALUES ('synthetic-orphan.docx', 'synthetic-orphan.docx', '.docx', 1)")
            conn.commit()
        conn.close()
        tables = ('documents', 'privacy_findings', 'pseudonym_mappings', 'users',
                  'matters', 'document_matters', 'audit_log', 'schema_migrations')
        before = {table: self.rows(table) for table in tables}
        credentials = self.credentials.read_bytes()
        rerun = self.run_bootstrap()
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual({table: self.rows(table) for table in tables}, before)
        self.assertEqual(self.credentials.read_bytes(), credentials)

    def test_missing_sample_refuses_to_start(self):
        (self.root / 'samples/documents/scanned_receipt.pdf').unlink()
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('practice documents are missing', result.stderr)

    def test_classification_errors_refuse_to_start(self):
        # Inject a failed classifier report without touching the application.
        result = subprocess.run([sys.executable, '-c',
            "from unittest.mock import patch; from docker.bootstrap import *; "
            "p=patch('classify.scan_and_classify', return_value={'errors':['synthetic failure'], 'summary':{}}); "
            "p.start(); bootstrap(install_credentials(ROOT/'install.json', ROOT/'db/legal_documents.db'))"],
            cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Sample classification failed', result.stderr)

    def test_mismatched_admin_credentials_are_not_silently_reset(self):
        self.assertEqual(self.run_bootstrap().returncode, 0)
        users = self.rows('users')
        value = json.loads(self.credentials.read_text())
        value['password'] = 'synthetic-wrong-password'
        self.credentials.write_text(json.dumps(value))
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('do not match the active admin', result.stderr)
        self.assertEqual(self.rows('users'), users)


class PythonRouteTests(unittest.TestCase):
    """The README's "Run without Docker" indexing commands, run as written, in a scratch copy."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ('db', 'legal_analyzer', 'samples'):
            shutil.copytree(ROOT / directory, self.root / directory,
                            ignore=shutil.ignore_patterns('*.db*', '__pycache__'))
        shutil.copyfile(ROOT / 'classify.py', self.root / 'classify.py')
        self.db = self.root / 'db' / 'legal_documents.db'

    def index(self, stock_dir):
        env = {**os.environ, 'LEGAL_ANALYZER_STOCK_DIR': str(stock_dir)}
        for step in (['db/init_db.py'], ['db/migrate.py'],
                     ['classify.py', '--report', str(self.root / 'report.json')]):
            result = subprocess.run([sys.executable, *step], cwd=self.root, env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    def matters(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return dict(conn.execute(
                'SELECT m.name, COUNT(dm.document_id) FROM matters m '
                'LEFT JOIN document_matters dm ON dm.matter_id = m.id GROUP BY m.id'
            ).fetchall())

    def test_the_bundled_samples_land_in_synthetic_practice(self):
        self.index(self.root / 'samples' / 'documents')
        self.assertEqual(self.matters().get('Synthetic practice'), 6)
        # Running it again neither duplicates the matter nor moves anything.
        self.index(self.root / 'samples' / 'documents')
        self.assertEqual(self.matters().get('Synthetic practice'), 6)

    def test_a_sample_moved_to_another_matter_stays_there(self):
        self.index(self.root / 'samples' / 'documents')
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO matters (name, status) VALUES ('Synthetic moved', 'active')")
            conn.execute(
                "UPDATE document_matters SET matter_id = (SELECT id FROM matters WHERE name = 'Synthetic moved') "
                "WHERE document_id = (SELECT id FROM documents WHERE filepath = 'turkish_petition.docx')")
            conn.commit()
        self.index(self.root / 'samples' / 'documents')
        matters = self.matters()
        self.assertEqual((matters['Synthetic practice'], matters['Synthetic moved']), (5, 1))

    def test_a_users_own_document_named_like_a_sample_is_not_filed_as_practice(self):
        own = self.root / 'own-documents'
        own.mkdir()
        shutil.copyfile(self.root / 'samples' / 'documents' / 'english_agreement.docx', own / 'english_agreement.docx')
        self.index(own)
        self.index(self.root / 'samples' / 'documents')
        with closing(sqlite3.connect(self.db)) as conn:
            filed = conn.execute(
                "SELECT d.filepath FROM documents d JOIN document_matters dm ON dm.document_id = d.id "
                "JOIN matters m ON m.id = dm.matter_id WHERE m.name = 'Synthetic practice'"
            ).fetchall()
        self.assertNotIn(('english_agreement.docx',), filed)
        self.assertEqual(len(filed), 5)

    def test_an_extra_file_indexed_with_the_samples_is_not_filed_as_practice(self):
        own = self.root / 'own-documents'
        own.mkdir()
        extra = own / 'synthetic_extra_agreement.docx'
        shutil.copyfile(self.root / 'samples' / 'documents' / 'english_agreement.docx', extra)
        env = {**os.environ, 'LEGAL_ANALYZER_STOCK_DIR': str(self.root / 'samples' / 'documents'),
               'LEGAL_ANALYZER_EXTRA_FILES': str(extra)}
        for step in (['db/init_db.py'], ['db/migrate.py'], ['classify.py']):
            result = subprocess.run([sys.executable, *step], cwd=self.root, env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(self.db)) as conn:
            filed = {row[0] for row in conn.execute(
                "SELECT d.filename FROM documents d JOIN document_matters dm ON dm.document_id = d.id "
                "JOIN matters m ON m.id = dm.matter_id WHERE m.name = 'Synthetic practice'"
            )}
            indexed = {row[0] for row in conn.execute("SELECT filename FROM documents")}
        self.assertIn('synthetic_extra_agreement.docx', indexed)
        self.assertNotIn('synthetic_extra_agreement.docx', filed)
        self.assertEqual(len(filed), 6)

    def start_app(self, password, port='1'):
        for name in ('app.py', 'web_security.py'):
            shutil.copyfile(ROOT / name, self.root / name)
        shutil.copytree(ROOT / 'blueprints', self.root / 'blueprints', ignore=shutil.ignore_patterns('__pycache__'))
        env = {key: value for key, value in os.environ.items() if not key.startswith('LEGAL_ANALYZER_')}
        env.update({'LEGAL_ANALYZER_SECRET_KEY': 'python-route-test-key-0123456789abcdef',
                    'LEGAL_ANALYZER_ADMIN_PASSWORD': password, 'LEGAL_ANALYZER_PORT': port})
        # Port 1 cannot be bound unprivileged, so an app that gets past its
        # start-up checks exits on the bind instead of serving forever.
        return subprocess.run([sys.executable, 'app.py'], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_starting_with_no_admin_and_no_password_is_refused(self):
        self.index(self.root / 'samples' / 'documents')
        result = self.start_app('')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('LEGAL_ANALYZER_ADMIN_PASSWORD is not set', result.stderr)

    def test_an_existing_admin_needs_no_password_in_the_environment(self):
        self.index(self.root / 'samples' / 'documents')
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin', 'synthetic-hash', 'admin')")
            conn.commit()
        result = self.start_app('')
        self.assertNotIn('LEGAL_ANALYZER_ADMIN_PASSWORD', result.stderr)

    def test_the_script_listens_on_legal_analyzer_port(self):
        self.index(self.root / 'samples' / 'documents')
        result = self.start_app('a synthetic chosen passphrase', port='not-a-port')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LEGAL_ANALYZER_PORT must be a port number from 1 to 65535, not 'not-a-port'", result.stderr)

    def test_a_users_own_folder_is_never_filed_as_practice_samples(self):
        # Same file names, different folder: the names alone prove nothing.
        own = self.root / 'own-documents'
        shutil.copytree(self.root / 'samples' / 'documents', own)
        self.index(own)
        self.assertNotIn('Synthetic practice', self.matters())


class LauncherTests(unittest.TestCase):
    def run_launcher(self, args=(), failure=''):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            log = root / 'calls'
            docker = root / 'docker'
            docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n'
                              'if [ "$*" = "$FAIL_COMMAND" ]; then exit 1; fi\n')
            docker.chmod(0o755)
            result = subprocess.run(['sh', str(ROOT / 'start.sh'), *args],
                env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                     'CALL_LOG': str(log), 'FAIL_COMMAND': failure},
                capture_output=True, text=True)
            return result, log.read_text().splitlines() if log.exists() else []

    def test_rerun_is_nondestructive_reset_requires_exact_flag(self):
        result, calls = self.run_launcher()
        self.assertEqual(result.returncode, 0)
        self.assertFalse(any('down' in call for call in calls))
        self.assertIn('compose up --build --wait --wait-timeout 120', calls)
        result, calls = self.run_launcher(['--reset'])
        self.assertEqual(result.returncode, 0)
        self.assertLess(calls.index('compose down --volumes'), calls.index('compose up --build --wait --wait-timeout 120'))
        for args in (['--resett'], ['--reset', 'extra']):
            result, calls = self.run_launcher(args)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(calls, [])

    def test_the_closing_line_counts_the_samples_that_ship(self):
        from classify import BUNDLED_SAMPLES

        result, _ = self.run_launcher()
        self.assertEqual(result.returncode, 0)
        shipped = sorted(path.name for path in (ROOT / 'samples/documents').iterdir()
                         if path.suffix in {'.docx', '.pdf'})
        self.assertEqual(shipped, sorted(BUNDLED_SAMPLES))
        self.assertIn(f'{len(shipped)} fictional practice documents are ready', result.stdout)
        self.assertIn('Synthetic practice', result.stdout)

    def test_unavailable_engine_and_failed_start_never_claim_ready(self):
        for failure in ('info', 'compose up --build --wait --wait-timeout 120'):
            result, calls = self.run_launcher(failure=failure)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Legal Redact is ready', result.stdout)
            self.assertFalse(any('exec' in call for call in calls))


class DockerBuildContextTests(unittest.TestCase):
    """A local database never reaches the image build.

    A working copy used for real review keeps db/legal_documents.db (and
    backups beside it) next to the schema. .dockerignore used to re-include the
    whole db/ directory, so ./start.sh sent that database to the build -- and
    then failed at the image's own `ln -s /state/legal_documents.db`.
    """

    # Files a working copy can hold under db/ that must stay out, and the ones
    # docker/bootstrap.py needs in.
    LOCAL_ONLY = ('db/legal_documents.db', 'db/legal_documents.db-wal', 'db/legal_documents.db-shm',
                  'db/prior-slot-backup.db', 'db/legal_documents.sqlite', 'db/notes.txt', '.env',
                  'data/uploads/client.docx', 'documents/client.docx')
    NEEDED = ('db/schema.sql', 'db/migrations/001_synthetic.sql', 'db/init_db.py', 'db/migrate.py')

    def test_the_build_context_carries_no_local_database(self):
        if shutil.which('docker') is None or subprocess.run(
                ['docker', 'info'], capture_output=True).returncode != 0:
            if os.environ.get('LEGAL_REDACT_REQUIRE_DOCKER') == '1':
                self.fail('LEGAL_REDACT_REQUIRE_DOCKER=1 but no Docker engine answers')
            self.skipTest('no Docker engine; CI runs this with LEGAL_REDACT_REQUIRE_DOCKER=1')
        with TemporaryDirectory() as folder:
            context, out = Path(folder) / 'context', Path(folder) / 'out'
            context.mkdir()
            shutil.copyfile(ROOT / '.dockerignore', context / '.dockerignore')
            for name in self.LOCAL_ONLY + self.NEEDED:
                (context / name).parent.mkdir(parents=True, exist_ok=True)
                (context / name).write_text('synthetic placeholder\n')
            # The real context transfer, with nothing installed: copy whatever
            # Docker was sent into an empty image and export it as files.
            result = subprocess.run(
                ['docker', 'build', '--quiet', '-f', '-', '--output', f'type=local,dest={out}', str(context)],
                input='FROM scratch\nCOPY . /\n', capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr)
            sent = {str(path.relative_to(out)) for path in out.rglob('*') if path.is_file()}
        self.assertEqual(sent & set(self.LOCAL_ONLY), set())
        self.assertEqual(sorted(sent), sorted(self.NEEDED))


class ShippedSamplesTests(unittest.TestCase):
    def test_docx_samples_are_synthetic_and_demonstrate_validated_identifiers(self):
        for name in ('turkish_petition.docx', 'english_agreement.docx'):
            with self.subTest(name=name):
                text, warning = extract_text(ROOT / 'samples/documents' / name)
                self.assertFalse(warning)
                self.assertIn('SYNTHETIC SAMPLE', text)
                profile = analyze_privacy(name, text, warning)
                self.assertTrue(any(f['category'] == 'email_address' for f in profile['risk_map']))
                tckn = re.search(r'TCKN: (\d{11})', text).group(1)
                vkn = re.search(r'VKN: (\d{10})', text).group(1)
                self.assertTrue(valid_turkish_national_id(tckn))
                self.assertTrue(valid_turkish_tax_number(vkn))
                samples = {f['sample'] for f in profile['risk_map']}
                self.assertIn(tckn, samples)
                self.assertTrue(any(vkn in sample for sample in samples))
                self.assertTrue('rastgele' in text or 'randomly generated' in text)
                self.assertFalse(profile['release_controls']['redaction_completed'])
                self.assertFalse(profile['release_controls']['human_review_approved'])

    def test_turkish_practice_samples_are_the_text_their_builder_writes(self):
        """The committed .docx files and samples/build_turkish_samples.py agree."""
        builder = load_sample_builder()
        self.assertEqual(
            set(builder.SAMPLES),
            {'turkish_contract.docx', 'turkish_enforcement_file.docx', 'turkish_notary_notice.docx'},
        )
        for name in builder.SAMPLES:
            with self.subTest(name=name):
                text, warning = extract_text(ROOT / 'samples/documents' / name)
                self.assertIsNone(warning)
                self.assertEqual(text, builder.sample_text(name))

    def test_turkish_practice_samples_are_synthetic_turkish_and_held_for_review(self):
        for name in load_sample_builder().SAMPLES:
            with self.subTest(name=name):
                text, warning = extract_text(ROOT / 'samples/documents' / name)
                self.assertIn('SENTETİK ÖRNEK / SYNTHETIC SAMPLE', text)
                self.assertIn('rastgele üretilmiştir', text)
                for tckn in re.findall(r'TCKN: (\d{11})', text):
                    self.assertTrue(valid_turkish_national_id(tckn), tckn)
                for vkn in re.findall(r'VKN: (\d{10})', text):
                    self.assertTrue(valid_turkish_tax_number(vkn), vkn)
                profile = analyze_privacy(name, text, warning)
                # Read as Turkish, so the rules measure it, and still held back:
                # a synthetic label bypasses nothing.
                self.assertEqual(profile['language_coverage']['language'], 'TR')
                self.assertEqual(profile['extraction_status']['status'], 'Complete')
                self.assertEqual(profile['residual_risk']['level'], 'High')
                self.assertFalse(profile['external_llm_gate']['allowed'])
                self.assertFalse(profile['release_controls']['redaction_completed'])
                self.assertFalse(profile['release_controls']['human_review_approved'])

    def test_scan_has_an_image_and_no_hidden_text_layer(self):
        with pymupdf.open(ROOT / 'samples/documents/scanned_receipt.pdf') as pdf:
            self.assertEqual(len(pdf), 1)
            self.assertEqual(pdf[0].get_text().strip(), '')
            self.assertTrue(pdf[0].get_images())
        text, warning = extract_text(ROOT / 'samples/documents/scanned_receipt.pdf')
        self.assertTrue(warning)
        self.assertNotEqual(analyze_privacy('scanned_receipt.pdf', text, warning)['extraction_status']['status'], 'Complete')


if __name__ == '__main__':
    unittest.main()
