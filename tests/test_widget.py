# -*- coding: utf-8 -*-
"""额度小组件的数据规则（独立小组件与 desktop-widgets 组件包共用）。"""
import unittest

from widget import is_disabled, provider_rows, visible_quota


class TestQuotaRows(unittest.TestCase):
    def test_explicit_remaining_wins_over_used(self):
        rows = provider_rows({'limits': [{'label': 'weekly', 'amount': {
            'remainingFraction': 0.0, 'usedFraction': 0.2}}]})
        self.assertEqual(rows[0]['frac'], 0.0)

    def test_unknown_limit_is_not_shown_as_available(self):
        self.assertIsNone(provider_rows({'limits': [{'amount': {}}]})[0]['frac'])

    def test_shared_limits_deduplicate_but_distinct_windows_remain(self):
        report = {'limits': [
            {'id': 'a', 'scope': {'sharedGroup': 'pool'}, 'window': {'id': 'daily'},
             'amount': {'used': 3, 'limit': 4}},
            {'id': 'b', 'scope': {'sharedGroup': 'pool'}, 'window': {'id': 'daily'},
             'amount': {'used': 3, 'limit': 4}},
            {'id': 'b', 'scope': {'sharedGroup': 'pool'}, 'window': {'id': 'weekly'},
             'amount': {'remainingFraction': 0.7}},
        ]}
        rows = provider_rows(report)
        self.assertEqual([(r['name'], r['frac']) for r in rows], [('1d', .25), ('7d', .7)])

    def test_unavailable_providers_and_rows_are_hidden(self):
        reports = [
            {'provider': 'ollama-cloud', 'limits': []},
            {'provider': 'openai-codex', 'limits': [
                {'window': {'id': 'weekly'}, 'amount': {'remainingFraction': 0.0}},
                {'window': {'id': 'daily'}, 'amount': {}},
            ]},
            {'provider': 'ollama', 'limits': [{'amount': {}}]},
        ]
        shown = visible_quota(reports)
        self.assertEqual([r['provider'] for r, _ in shown], ['openai-codex'])
        self.assertEqual([row['name'] for row in shown[0][1]], ['7d'])


class TestFallbackOnly(unittest.TestCase):
    def test_needs_reserve_100_and_every_selector_field(self):
        rep = {'provider': 'anthropic', 'metadata': {'email': 'A@x.io', 'orgId': 'o1'}}
        off = {'provider': 'anthropic', 'account': {'email': 'a@x.io', 'orgId': 'o1'}, 'reservePct': 100}
        self.assertTrue(is_disabled(rep, [off]))
        self.assertFalse(is_disabled(rep, [{**off, 'reservePct': 99}]))
        self.assertFalse(is_disabled(rep, [{**off, 'account': {'email': 'a@x.io', 'orgId': 'o2'}}]))
        self.assertFalse(is_disabled(rep, [{**off, 'provider': 'openai-codex'}]))


if __name__ == '__main__':
    unittest.main()
