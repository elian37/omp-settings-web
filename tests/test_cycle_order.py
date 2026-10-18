# -*- coding: utf-8 -*-
"""cycleOrder（Ctrl+P 轮换顺序）读写测试；只操作临时配置文件。"""
import unittest

from test_fallback import FallbackBase
import server


class TestCycleOrder(FallbackBase):
    def test_missing_key_uses_omp_default(self):
        self.write("modelRoles:\n  default: a/m\n")
        st = server.cycle_order_state()
        self.assertEqual(st["order"], ["smol", "default", "slow"])
        self.assertFalse(st["custom"])

    def test_reads_block_and_inline_lists(self):
        for text in ('cycleOrder:\n  - default\n  - "e"  # custom\nx: 1\n',
                     'cycleOrder: [default, "e"]  # inline\nx: 1\n'):
            with self.subTest(text=text):
                self.write(text)
                st = server.cycle_order_state()
                self.assertEqual(st["order"], ["default", "e"])
                self.assertTrue(st["custom"])

    def test_write_preserves_other_lines_and_order(self):
        self.write("# head\nmodelRoles:\n  default: a/m\nautoResume: false\n")
        self.assertIsNone(server.set_cycle_order(["e", "default", "slow"]))
        self.assertEqual(server.cycle_order_state()["order"], ["e", "default", "slow"])
        text = self.read()
        self.assertTrue(text.startswith("# head\nmodelRoles:\n  default: a/m\nautoResume: false\n"))

    def test_replace_existing_keeps_head_comment_and_crlf(self):
        self.write("cycleOrder: # mine\r\n  - smol\r\n  - default\r\nnext: 1\r\n")
        self.assertIsNone(server.set_cycle_order(["default", "e"]))
        text = self.read()
        self.assertIn("cycleOrder: # mine\r\n", text)
        self.assertIn("next: 1\r\n", text)
        self.assertNotIn("smol", text)
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertEqual(server.cycle_order_state()["order"], ["default", "e"])

    def test_inline_list_is_replaced_in_place(self):
        self.write("a: 1\ncycleOrder: [smol, slow]\nb: 2\n")
        self.assertIsNone(server.set_cycle_order(["default"]))
        text = self.read()
        self.assertTrue(text.startswith("a: 1\ncycleOrder:"))
        self.assertTrue(text.endswith("b: 2\n"))
        self.assertEqual(server.cycle_order_state()["order"], ["default"])

    def test_restore_default_removes_key_only(self):
        self.write("a: 1\ncycleOrder:\n  - e\n  - default\nb: 2\n")
        self.assertIsNone(server.set_cycle_order(None))
        self.assertEqual(self.read(), "a: 1\nb: 2\n")
        self.assertFalse(server.cycle_order_state()["custom"])

    def test_restore_default_when_absent_is_noop(self):
        before = "a: 1\n"
        self.write(before)
        self.assertIsNone(server.set_cycle_order(None))
        self.assertEqual(self.read(), before)

    def test_invalid_orders_never_write(self):
        before = "cycleOrder:\n  - smol\n"
        self.write(before)
        for bad in ([], ["a\nb"], ["@slow"], ["x y"], ["default", "default"],
                    "default", [1], ["a: b"]):
            with self.subTest(bad=bad):
                self.assertIsNotNone(server.set_cycle_order(bad))
        self.assertEqual(self.read(), before)

    def test_unsupported_nested_list_is_refused(self):
        before = "cycleOrder:\n  - path: x\n    value: [smol]\n"
        self.write(before)
        self.assertIn("error", server.cycle_order_state())
        self.assertIsNotNone(server.set_cycle_order(["default"]))
        self.assertEqual(self.read(), before)


if __name__ == "__main__":
    unittest.main()
