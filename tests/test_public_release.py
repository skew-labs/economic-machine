"""Publication boundaries must not be bypassed by ignored local files."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('public_release', Path(__file__).parents[1] / 'scripts/check_public_release.py')
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value=''):
        file = self.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(value)
        return name

    def reasons(self, paths):
        return {e['reason'] for e in CHECKER.publication_issues(self.root, paths)['failures']}

    def test_public_source_contracts_and_architecture_are_admitted(self):
        paths = [self.write('README.md', '[Contracts](contracts/) [Architecture](docs/ARCHITECTURE.md)'),
                 self.write('contracts/Example.sol', 'contract Example {}'),
                 self.write('docs/ARCHITECTURE.md', '[Source](../contracts/Example.sol)')]
        self.assertEqual(self.reasons(paths), set())

    def test_ignored_file_cannot_satisfy_a_public_link(self):
        self.write('private/notes.md', 'operator notes')
        page = self.write('README.md', '[Notes](private/notes.md)')
        self.assertIn('UNPUBLISHED_DOCUMENT_LINK', self.reasons([page]))

    def test_force_added_internal_documents_and_generated_reports_fail(self):
        for name in ['docs/ARBITRUM_DEMO_SCRIPT.md', 'docs/PR12.md', 'docs/new-notes.md',
                     'INTERNAL_PLAN.md', 'internal/release.md']:
            with self.subTest(name=name):
                self.assertIn('INTERNAL_DOCUMENT_IN_PUBLIC_TREE', self.reasons([self.write(name)]))
        self.assertIn('GENERATED_OUTPUT_IN_PUBLIC_TREE', self.reasons([self.write('artifacts/build.json', '{}')]))

    def test_internal_diary_cannot_hide_in_allowed_guide(self):
        page = self.write('docs/ARCHITECTURE.md', 'Amazon plan PR5 is implemented here.')
        self.assertIn('INTERNAL_WORK_RECORD_IN_GUIDE', self.reasons([page]))

    def test_credentials_are_flagged_without_returning_values(self):
        secret = 'ory_' + 'rt_' + 'a' * 32
        page = self.write('README.md', secret)
        report = CHECKER.publication_issues(self.root, [page])
        self.assertIn('CREDENTIAL_PATTERN', {item['reason'] for item in report['failures']})
        self.assertNotIn(secret, str(report))
