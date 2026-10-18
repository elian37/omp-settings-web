import unittest
from test_fallback import FallbackBase
import server


class TestFallbackEdges(FallbackBase):
    def test_commented_headers_and_empty_retry(self):
        for text in ('retry: {}\n', 'retry: # retry note\n  fallbackChains: # chains note\n    task: []\n'):
            with self.subTest(text=text):
                self.write(text)
                self.assertIsNone(server.set_fallback_chain('default', ['openai/model']))
                self.assertEqual(server.fallback_state()['chains']['default'], ['openai/model'])

    def test_invalid_flow_array_never_overwrites(self):
        before = 'retry:\n  fallbackChains: {task: [provider/model}\n'
        self.write(before)
        self.assertIsNotNone(server.set_fallback_chain('default', ['provider/model']))
        self.assertEqual(self.read(), before)

    def test_model_key_and_prefixed_selector(self):
        self.write('retry:\n  fallbackChains: {}\n')
        self.assertIsNone(server.set_fallback_chain('anthropic/*', ['openrouter/anthropic/model:high']))
        self.assertEqual(server.fallback_state()['chains']['anthropic/*'], ['openrouter/anthropic/model:high'])

    def test_alias_and_bare_selector_are_rejected(self):
        self.write('retry:\n  fallbackChains: {}\n')
        before = self.read()
        for selector in ('@slow', 'slow'):
            self.assertIsNotNone(server.set_fallback_chain('default', [selector]))
        self.assertEqual(self.read(), before)

    def test_block_comments_survive_update_and_delete(self):
        self.write('retry:\n  fallbackChains:\n    task: # task note\n      - old/model\n    # keep next-chain note\n    web: [web/exa]\n')
        self.assertIsNone(server.set_fallback_chain('task', ['new/model']))
        self.assertIn('# task note', self.read())
        self.assertIn('    # keep next-chain note\n    web: [web/exa]', self.read())
        self.assertIsNone(server.set_fallback_chain('task', None))
        self.assertIn('# keep next-chain note', self.read())


if __name__ == '__main__':
    unittest.main()
