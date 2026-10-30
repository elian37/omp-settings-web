# -*- coding: utf-8 -*-
"""enum 设置项的合法值校验。

背景（2026-10-30 实测，omp 18.14.0）：omp 对 config.yml 里的非法枚举/布尔值
不报错，而是静默回落默认值。写入 `display.showTurnTime: bogusXyz` 后
`omp config get` 返回 false，文件里却留着 bogusXyz——文件内容与生效值不一致
且毫无提示。所以页面侧必须在写入前自己拦。

运行：python -m pytest tests/test_enum_guard.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


class TestCoerceEnum(unittest.TestCase):
    OPTS = ['auto', 'smart', 'always', 'off']

    def test_accepts_listed_value(self):
        self.assertEqual(
            server.coerce_value('enum', 'smart', self.OPTS), 'smart')

    def test_strips_surrounding_space(self):
        self.assertEqual(
            server.coerce_value('enum', '  off  ', self.OPTS), 'off')

    def test_rejects_unlisted_value(self):
        with self.assertRaises(ValueError) as cm:
            server.coerce_value('enum', 'nonsense', self.OPTS)
        msg = str(cm.exception)
        self.assertIn('nonsense', msg)
        self.assertIn('auto', msg)  # 报错里给出全部合法值

    def test_rejects_empty_when_options_known(self):
        # 空串在 omp 里等价于「未设置」，但枚举项允许的写法是显式值；
        # 与其写进一个会被静默丢弃的空值，不如直接拒绝。
        with self.assertRaises(ValueError):
            server.coerce_value('enum', '', self.OPTS)

    def test_without_options_falls_through(self):
        # zh.json 与 omp 都没给出值域时不能误拒：退回原行为。
        self.assertEqual(server.coerce_value('enum', 'whatever'), 'whatever')


class TestEnumOptions(unittest.TestCase):
    """值域来源：zh.json 优先，缺失时兜底问 omp 文本输出。"""

    def setUp(self):
        self._orig = server.enum_options_from_omp

    def tearDown(self):
        server.enum_options_from_omp = self._orig

    def test_zh_json_wins(self):
        zh = {'tui.autoGraph': {'options': ['a', 'b']}}
        self.assertEqual(server.enum_options('tui.autoGraph', zh), ['a', 'b'])

    def test_falls_back_to_omp_text_output(self):
        server.enum_options_from_omp = lambda: {'x.y': ['p', 'q']}
        self.assertEqual(server.enum_options('x.y', {}), ['p', 'q'])

    def test_unknown_key_yields_empty(self):
        server.enum_options_from_omp = lambda: {}
        self.assertEqual(server.enum_options('nope.nope', {}), [])


class TestEnumOptionsParser(unittest.TestCase):
    """`omp config list` 的 `key = value (a|b|c)` 行解析。"""

    def setUp(self):
        self._orig_run = server.run

    def tearDown(self):
        server.run = self._orig_run

    def test_parses_enum_lines_only(self):
        server.run = lambda args: (
            "Settings:\n"
            "\n"
            "[appearance]\n"
            "  theme.dark = titanium (string)\n"
            "  tui.autoGraph = auto (auto|smart|always|off)\n"
            "  tools.approvalMode = yolo "
            "(always-ask|always-ask-cached|write|yolo)\n")
        got = server.enum_options_from_omp()
        self.assertEqual(got['tui.autoGraph'], ['auto', 'smart', 'always', 'off'])
        self.assertEqual(got['tools.approvalMode'],
                         ['always-ask', 'always-ask-cached', 'write', 'yolo'])
        self.assertNotIn('theme.dark', got)


class TestSetKeyRejectsBadEnum(unittest.TestCase):
    """整条链路：/api/set-key 落到 config.yml 之前就必须挡住。"""

    def setUp(self):
        import json
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / "config.yml"
        self.cfg.write_text("tui:\n  autoGraph: auto\n", encoding='utf-8')
        self._orig_config = server.CONFIG
        self._orig_backup = server.write_backup
        self._orig_run = server.run
        self._orig_zh = server.load_zh
        server.CONFIG = self.cfg
        server.write_backup = lambda: None
        server.load_zh = lambda: {'tui.autoGraph': {'options': ['auto', 'off']}}
        server.run = lambda args: json.dumps({
            'tui.autoGraph': {'type': 'enum', 'value': 'auto'}})

    def tearDown(self):
        server.CONFIG = self._orig_config
        server.write_backup = self._orig_backup
        server.run = self._orig_run
        server.load_zh = self._orig_zh
        self._tmp.cleanup()

    def _set(self, value):
        h = server.Handler.__new__(server.Handler)
        return h._set_key({'key': 'tui.autoGraph', 'value': value})

    def test_bad_value_returns_error_and_leaves_file_alone(self):
        before = self.cfg.read_text(encoding='utf-8')
        err = self._set('bogus')
        self.assertTrue(err and 'bogus' in err, err)
        self.assertEqual(self.cfg.read_text(encoding='utf-8'), before)

    def test_good_value_is_written(self):
        self.assertIsNone(self._set('off'))
        self.assertIn('autoGraph: off',
                      self.cfg.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
