"""Exercise the Python embedded in the reusable workflow, without publishing."""
import io
import json
import os
import subprocess
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/gh-release.yaml').read_text())
STEPS = WORKFLOW['jobs']['release']['steps']
NOTES_STEP = next(step for step in STEPS if step.get('id') == 'notes')
SCRIPT = NOTES_STEP['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
NOTES = types.ModuleType('release_notes')
exec(compile(SCRIPT, 'gh-release.yaml:release-notes', 'exec'), NOTES.__dict__)


def pull(body='## Release notes\nUpgrade now.\n## Checks\nPassed.', **changes):
    result = {
        'merged_at': '2026-10-28T12:00:00Z',
        'merge_commit_sha': 'release-sha',
        'body': body,
        'base': {'repo': {'full_name': 'example/app'}, 'ref': 'main'},
    }
    result.update(changes)
    return result


class ExtractionTests(unittest.TestCase):
    def test_sections(self):
        cases = [
            ('## Summary\nSummary.\n## Release notes\nUpgrade.\n## Checks\nPass.', 'Upgrade.\n'),
            ('## Release notes\n### Upgrade steps\nRun SQL.\n# Other\nStop.', '### Upgrade steps\nRun SQL.\n'),
            ('## Release notes\r\nUpgrade.\r\n## Checks\r\nPass.', 'Upgrade.\n'),
            ('## Release notes\nFinal section.', 'Final section.\n'),
            ('  ## Release notes ##\nUnicode: café.\n## Checks', 'Unicode: café.\n'),
            ('## Release notes\nKeep\n---\nthis rule.\n## Attribution\nPrepared-by: agent', 'Keep\n---\nthis rule.\n'),
            ('## Release notes\nFirst.\n## Other\n## Release notes\nSecond.', 'First.\n'),
            ('## Release notes\n\n## Checks\nPass.', ''),
            ('## Summary\nNo notes.', ''),
            ('### Release notes\nWrong level.', ''),
            ('## Release notes extra\nWrong title.', ''),
            ('', ''),
            (None, ''),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(NOTES.extract_notes(body), expected)

    def test_fences_do_not_start_or_end_section(self):
        for fence in ('```', '~~~~'):
            body = (f'{fence}markdown\n## Release notes\nIgnore\n{fence}\n'
                    f'## Release notes\n### Upgrade\n{fence}sh\n'
                    f'## Keep this shell comment\n# Keep this too\n'
                    f'{fence}\nAfter code.\n## Checks\nExclude')
            self.assertEqual(NOTES.extract_notes(body),
                             f'### Upgrade\n{fence}sh\n## Keep this shell comment\n'
                             f'# Keep this too\n{fence}\nAfter code.\n')

    def test_shorter_and_different_fences_do_not_close_code(self):
        body = ('## Release notes\n````markdown\n```\n~~~\n'
                '## Still code\n````\nAfter.\n## Checks\nExclude')
        self.assertEqual(NOTES.extract_notes(body),
                         '````markdown\n```\n~~~\n## Still code\n````\nAfter.\n')


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            'GH_TOKEN': 'synthetic-test-token',
            'GITHUB_API_URL': 'https://api.github.test',
            'GITHUB_REPOSITORY': 'example/app',
            'GITHUB_SHA': 'release-sha',
            'GITHUB_REF_NAME': 'main',
            'REPOSITORY_PRIVATE': 'false',
            'RUNNER_TEMP': self.temp.name,
            'GITHUB_OUTPUT': str(Path(self.temp.name) / 'outputs'),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def response(self, value):
        return io.BytesIO(json.dumps(value).encode())

    def test_pagination(self):
        first = [pull(merged_at=None)] * 100
        with patch.object(NOTES, 'urlopen', side_effect=[self.response(first), self.response([pull()])]) as request:
            self.assertEqual(len(NOTES.read_pull_requests()), 101)
        self.assertTrue(request.call_args_list[1].args[0].full_url.endswith('per_page=100&page=2'))
        self.assertEqual(request.call_args_list[0].args[0].get_header('Authorization'), 'Bearer synthetic-test-token')

    def test_public_permission_fallback_omits_token(self):
        denied = HTTPError('https://api.github.test', 403, 'Forbidden', {}, None)
        with patch.object(NOTES, 'urlopen', side_effect=[denied, self.response([pull()])]) as request:
            self.assertEqual(len(NOTES.read_pull_requests()), 1)
        self.assertIsNone(request.call_args_list[1].args[0].get_header('Authorization'))

    def test_private_permission_failure_never_retries_without_token(self):
        denied = HTTPError('https://api.github.test', 403, 'Forbidden', {}, None)
        with patch.dict(os.environ, {'REPOSITORY_PRIVATE': 'true'}):
            with patch.object(NOTES, 'urlopen', side_effect=denied) as request:
                with self.assertRaises(HTTPError):
                    NOTES.read_pull_requests()
        self.assertEqual(request.call_count, 1)

    def test_api_failures_stop_before_release(self):
        for code in (401, 404, 429, 500):
            with self.subTest(code=code):
                failure = HTTPError('https://api.github.test', code, 'Failed', {}, None)
                with patch.object(NOTES, 'urlopen', side_effect=failure):
                    with self.assertRaises(HTTPError):
                        NOTES.main()
                self.assertFalse((Path(self.temp.name) / 'outputs').exists())

    def test_failed_public_fallback_is_not_silenced(self):
        denied = HTTPError('https://api.github.test', 403, 'Forbidden', {}, None)
        with patch.object(NOTES, 'urlopen', side_effect=denied):
            with self.assertRaises(HTTPError):
                NOTES.main()

    def test_selects_exact_merged_commit_and_target_branch(self):
        others = [pull(merged_at=None), pull(merge_commit_sha='older-sha'),
                  pull(base={'repo': {'full_name': 'example/elsewhere'}, 'ref': 'main'}),
                  pull(base={'repo': {'full_name': 'example/app'}, 'ref': 'hotfix'})]
        with patch.object(NOTES, 'read_pull_requests', return_value=others + [pull()]):
            NOTES.main()
        self.assertEqual((Path(self.temp.name) / 'release-notes.md').read_text(), 'Upgrade now.\n')
        self.assertEqual((Path(self.temp.name) / 'outputs').read_text(),
                         f'body_path={self.temp.name}/release-notes.md\n')

    def test_hotfix_branch(self):
        with patch.dict(os.environ, {'GITHUB_REF_NAME': 'hotfix/fix'}):
            pr = pull(base={'repo': {'full_name': 'Example/App'}, 'ref': 'hotfix/fix'})
            with patch.object(NOTES, 'read_pull_requests', return_value=[pr]):
                NOTES.main()
        self.assertEqual((Path(self.temp.name) / 'release-notes.md').read_text(), 'Upgrade now.\n')

    def test_no_pr_or_notes_leaves_body_path_unset(self):
        for pulls in ([], [pull(body=None)], [pull(body='## Checks\nDone')],
                      [pull(body='## Release notes\n \n## Checks')]):
            with self.subTest(pulls=pulls):
                with patch.object(NOTES, 'read_pull_requests', return_value=pulls):
                    NOTES.main()
                self.assertEqual((Path(self.temp.name) / 'outputs').read_text(), '')
                self.assertEqual((Path(self.temp.name) / 'release-notes.md').read_text(), '')

    def test_ambiguous_matches_stop_release(self):
        with patch.object(NOTES, 'read_pull_requests', return_value=[pull(), pull()]):
            with self.assertRaisesRegex(RuntimeError, 'Multiple merged pull requests'):
                NOTES.main()
        self.assertFalse((Path(self.temp.name) / 'outputs').exists())


class WiringTests(unittest.TestCase):
    def test_source_validation(self):
        validate = next(step for step in STEPS if step.get('name') == 'Validate release notes source')
        for source, expected in [('merge-request', 0), ('none', 0), ('', 1), ('typo', 1)]:
            with self.subTest(source=source):
                result = subprocess.run(['bash', '-e', '-c', validate['run']],
                                        env={**os.environ, 'NOTES_FROM': source},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_none_skips_lookup_and_keeps_generated_notes(self):
        self.assertEqual(NOTES_STEP['if'], "inputs.notes-from == 'merge-request'")
        release = next(step for step in STEPS if step.get('name') == 'Release')
        self.assertTrue(release['with']['generate_release_notes'])
        self.assertEqual(release['with']['body_path'], '${{ steps.notes.outputs.body_path }}')
        self.assertLess(STEPS.index(NOTES_STEP), STEPS.index(release))


if __name__ == '__main__':
    unittest.main()
