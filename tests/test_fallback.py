# -*- coding: utf-8 -*-
"""retry.fallbackChains 编辑接口测试。

运行：python -m pytest tests/test_fallback.py   或
      python -m unittest tests.test_fallback -v
（测试通过临时改写 server.CONFIG 指向 tmp 文件，绝不动真实 ~/.omp/agent/config.yml。）
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


class FallbackBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / "config.yml"
        self._orig_config = server.CONFIG
        self._orig_backup = server.write_backup
        server.CONFIG = self.cfg
        server.write_backup = lambda: None
        # 备份照常走真实路径则 copy2 会落在 tmp 里，这里直接禁用即可

    def tearDown(self):
        server.CONFIG = self._orig_config
        server.write_backup = self._orig_backup
        self._tmp.cleanup()

    def write(self, text):
        self.cfg.write_bytes(text.encode('utf-8'))

    def read(self):
        return self.cfg.read_bytes().decode('utf-8')


class TestRead(FallbackBase):
    def test_missing_retry(self):
        self.write("modelRoles:\n  default: anthropic/claude\n")
        st = server.fallback_state()
        self.assertNotIn("error", st)
        self.assertEqual(st["chains"], {})

    def test_block_map_with_scalars(self):
        self.write(
            "retry:\n"
            "  modelFallback: true\n"
            "  fallbackOn: usage-limit\n"
            "  fallbackRevertPolicy: never\n"
            "  fallbackChains:\n"
            "    default:\n"          # 块序列
            "      - anthropic/claude-sonnet-4\n"
            "      - openai/gpt-5\n"
            "    fast: [google/gemini-3[low], zhipu/glm-5:high]  # inline\n"
            "other:\n  x: 1\n")
        st = server.fallback_state()
        self.assertEqual(st["chains"]["default"],
                         ["anthropic/claude-sonnet-4", "openai/gpt-5"])
        self.assertEqual(st["chains"]["fast"],
                         ["google/gemini-3[low]", "zhipu/glm-5:high"])
        self.assertTrue(st["modelFallback"])
        self.assertEqual(st["fallbackOn"], "usage-limit")
        self.assertEqual(st["fallbackRevertPolicy"], "never")

    def test_inline_flow_map(self):
        self.write('retry: {fallbackChains: {default: [a/m1, b/m2]}, '
                   'modelFallback: true}\n')
        st = server.fallback_state()
        self.assertEqual(st["chains"], {"default": ["a/m1", "b/m2"]})
        self.assertTrue(st["modelFallback"])

    def test_fallbackchains_null(self):
        self.write("retry:\n  fallbackChains: null\n")
        self.assertEqual(server.fallback_state()["chains"], {})

    def test_complex_yaml_refused(self):
        self.write("retry:\n  fallbackChains:\n"
                   "    default:\n      - {path: x}\n")
        st = server.fallback_state()
        self.assertIn("error", st)

    def test_read_failure(self):
        self.cfg.unlink(missing_ok=True)
        self.assertIn("error", server.fallback_state())


class TestWrite(FallbackBase):
    def test_add_when_parent_missing(self):
        self.write("modelRoles:\n  default: a/m\n")
        self.assertIsNone(server.set_fallback_chain(
            "default", ["a/m1", "b/m2"]))
        self.assertEqual(self.read(),
                         "modelRoles:\n  default: a/m\n"
                         "retry:\n  fallbackChains:\n"
                         "    default: [a/m1, b/m2]\n")

    def test_add_when_fallbackchains_missing(self):
        self.write("retry:\n  modelFallback: true\n")
        self.assertIsNone(server.set_fallback_chain("default", ["a/m"]))
        self.assertEqual(
            self.read(),
            "retry:\n  fallbackChains:\n    default: [a/m]\n"
            "  modelFallback: true\n")

    def test_update_preserves_sibling_and_comment(self):
        self.write("retry:\n  fallbackChains:\n"
                   "    default: [a/m1]  # 旧链\n"
                   "    fast: [x/y]\n")
        self.assertIsNone(server.set_fallback_chain(
            "default", ["a/m2", "b/m3"]))
        text = self.read()
        self.assertIn("default: [a/m2, b/m3]", text)
        self.assertIn("fast: [x/y]", text)

    def test_empty_list_explicit_disable(self):
        self.write("retry:\n  fallbackChains:\n    default: [a/m]\n")
        self.assertIsNone(server.set_fallback_chain("default", []))
        self.assertIn("default: []", self.read())

    def test_ordered_chain(self):
        self.write("retry:\n  fallbackChains:\n    k:\n      - a/m\n")
        self.assertIsNone(server.set_fallback_chain(
            "k", ["z/m9", "a/m1", "b/m2"]))
        self.assertIn("k: [z/m9, a/m1, b/m2]", self.read())

    def test_delete_key(self):
        self.write("retry:\n  fallbackChains:\n"
                   "    default: [a/m]\n    fast: [x/y]\n")
        self.assertIsNone(server.set_fallback_chain("default", None))
        text = self.read()
        self.assertNotIn("default", text)
        self.assertIn("fast: [x/y]", text)
        self.assertIn("fallbackChains:", text)

    def test_delete_last_key_removes_header(self):
        self.write("retry:\n  modelFallback: true\n"
                   "  fallbackChains:\n    default: [a/m]\n")
        self.assertIsNone(server.set_fallback_chain("default", None))
        text = self.read()
        self.assertNotIn("default", text)
        self.assertNotIn("fallbackChains", text)

    def test_inline_flow_map_write(self):
        self.write("retry: {fallbackChains: {default: [a/m]}, "
                   "modelFallback: true}\n")
        self.assertIsNone(server.set_fallback_chain("fast", ["x/y"]))
        text = self.read()
        self.assertIn("modelFallback: true", text)
        self.assertIn("default: [a/m]", text)
        self.assertIn("fast: [x/y]", text)
        self.assertIn("fallbackChains:", text)

    def test_inline_flow_map_delete(self):
        self.write("retry: {fallbackChains: {default: [a/m]}}\n")
        self.assertIsNone(server.set_fallback_chain("default", None))
        text = self.read()
        self.assertNotIn("default", text)
        self.assertNotIn("fallbackChains", text)

    def test_delete_absent_key_noop(self):
        before = "retry:\n  fallbackChains:\n    fast: [x/y]\n"
        self.write(before)
        self.assertIsNone(server.set_fallback_chain("default", None))
        self.assertEqual(self.read(), before)

    def test_delete_no_retry_noop(self):
        before = "modelRoles:\n  default: a/m\n"
        self.write(before)
        self.assertIsNone(server.set_fallback_chain("default", None))
        self.assertEqual(self.read(), before)

    def test_crlf_preserved(self):
        self.write("retry:\r\n  fallbackChains:\r\n    old: [a/m]  # cmt\r\n"
                   "other: 1\r\n")
        self.assertIsNone(server.set_fallback_chain("new", ["x/y"]))
        text = self.read()
        self.assertIn("\r\n", text)
        self.assertIn("new: [x/y]\r\n", text)
        self.assertIn("old: [a/m]  # cmt", text)

    def test_inline_fallbackchains_write(self):
        # retry 块内 fallbackChains 是单行 flow map：保持 inline 形式改写
        self.write("retry:\n"
                   "  fallbackChains: {default: [a/m]}  # keep\n"
                   "  modelFallback: false\n")
        self.assertIsNone(server.set_fallback_chain("fast", ["x/y"]))
        text = self.read()
        self.assertIn("fallbackChains: {default: [a/m], fast: [x/y]}", text)
        self.assertIn("# keep", text)
        self.assertIn("modelFallback: false", text)

    def test_complex_yaml_no_write(self):
        before = "retry:\n  fallbackChains:\n    default:\n      - {path: x}\n"
        self.write(before)
        self.assertIsNotNone(server.set_fallback_chain("k", ["a/m"]))
        self.assertEqual(self.read(), before)

    def test_retry_empty_map(self):
        self.write("retry: {}\n")
        self.assertIsNone(server.set_fallback_chain("k", ["a/m"]))
        self.assertEqual(self.read(),
                         "retry:\n  fallbackChains:\n    k: [a/m]\n")

    def test_fallbackchains_empty_inline(self):
        self.write("retry:\n  fallbackChains: []\n")
        self.assertIsNone(server.set_fallback_chain("k", ["a/m"]))
        self.assertEqual(server.fallback_state()["chains"], {"k": ["a/m"]})

    def test_reject_bad_key_newline(self):
        before = "retry:\n  fallbackChains:\n    a: [x/y]\n"
        self.write(before)
        for bad in ("a\nb: evil", "a: x", "", "a b", "k#x"):
            self.assertIsNotNone(server.set_fallback_chain(bad, ["x/y"]))
        self.assertEqual(self.read(), before)

    def test_reject_bad_selector(self):
        before = "retry:\n  fallbackChains:\n    a: [x/y]\n"
        self.write(before)
        for bad in ("a/m\nb: 1", "- a", "a/m; rm", "not a selector ?"):
            self.assertIsNotNone(server.set_fallback_chain("k", [bad]))
        self.assertIsNotNone(server.set_fallback_chain("k", "notalist"))
        self.assertIsNotNone(server.set_fallback_chain("k", [1, 2]))
        self.assertEqual(self.read(), before)

    def test_selector_allowlist(self):
        self.write("retry:\n  fallbackChains:\n    a: [x/y]\n")
        for ok in ("anthropic/claude-sonnet-4.5",
                   "p/m:high", "openai/*"):
            self.assertIsNone(server.set_fallback_chain("k", [ok]))


if __name__ == "__main__":
    unittest.main()
