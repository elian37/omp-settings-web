# -*- coding: utf-8 -*-
"""auth.accountPolicies 解析/校验/写入测试（含 omp ≥18.12.4 的 reclaimAbovePct）。

运行：python -m pytest tests/test_policies.py   或
      python -m unittest tests.test_policies -v
（临时改写 server.CONFIG 指向 tmp 文件，绝不动真实 ~/.omp/agent/config.yml。）
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


class PolicyBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / "config.yml"
        self._orig_config = server.CONFIG
        self._orig_backup = server.write_backup
        self._orig_accounts = server.list_accounts
        server.CONFIG = self.cfg
        server.write_backup = lambda: None
        server.list_accounts = lambda: ({"accounts": [
            {"provider": "anthropic", "identityKey": "email:a@x.io"},
        ]}, None)

    def tearDown(self):
        server.CONFIG = self._orig_config
        server.write_backup = self._orig_backup
        server.list_accounts = self._orig_accounts
        self._tmp.cleanup()

    def write(self, text):
        self.cfg.write_bytes(text.encode('utf-8'))

    def read(self):
        return self.cfg.read_bytes().decode('utf-8')

    def pay(self, **kw):
        base = {"provider": "anthropic", "identityKey": "email:a@x.io"}
        base.update(kw)
        return base


class TestParse(PolicyBase):
    def test_parse_three_scalars(self):
        self.write("auth:\n  accountPolicies:\n"
                   "    - provider: anthropic\n"
                   "      account:\n"
                   "        email: a@x.io\n"
                   "      priority: 5\n"
                   "      reservePct: 20\n"
                   "      reclaimAbovePct: 80\n")
        blk = server._policies_block(self.read().splitlines(keepends=True))
        self.assertIsNone(blk.get("error"))
        self.assertEqual(blk["items"][0]["reclaimAbovePct"], 80)

    def test_unknown_scalar_still_readonly(self):
        self.write("auth:\n  accountPolicies:\n"
                   "    - provider: anthropic\n"
                   "      account:\n"
                   "        email: a@x.io\n"
                   "      magicPct: 42\n")
        blk = server._policies_block(self.read().splitlines(keepends=True))
        self.assertIsNone(blk.get("items"))


class TestSetPolicy(PolicyBase):
    def test_write_reclaim(self):
        self.write("auth:\n  accountPolicies: []\nother: 1\n")
        err = server.set_account_policy(self.pay(priority=5, reservePct=20,
                                               reclaimAbovePct=80))
        self.assertIsNone(err)
        txt = self.read()
        self.assertIn("reclaimAbovePct: 80", txt)
        self.assertIn("other: 1", txt)

    def test_reclaim_must_exceed_reserve(self):
        self.write("auth:\n  accountPolicies: []\n")
        err = server.set_account_policy(self.pay(reservePct=50, reclaimAbovePct=40))
        self.assertIsNotNone(err)
        self.assertNotIn("reclaimAbovePct", self.read())

    def test_reclaim_against_default_reserve_when_reserve_empty(self):
        # reservePct 未设 → 用全局默认 retry.usageReservePct（未配置时 10）做下限
        self.write("auth:\n  accountPolicies: []\n")
        err = server.set_account_policy(self.pay(reclaimAbovePct=5))
        self.assertIsNotNone(err)

    def test_reclaim_equal_reserve_rejected(self):
        self.write("auth:\n  accountPolicies: []\n")
        err = server.set_account_policy(self.pay(reservePct=50, reclaimAbovePct=50))
        self.assertIsNotNone(err)

    def test_reclaim_range_checked(self):
        self.write("auth:\n  accountPolicies: []\n")
        self.assertIsNotNone(server.set_account_policy(self.pay(reclaimAbovePct=150)))
        self.assertIsNotNone(server.set_account_policy(self.pay(reclaimAbovePct=-1)))

    def test_update_existing_adds_reclaim(self):
        self.write("auth:\n  accountPolicies:\n"
                   "    - provider: anthropic\n"
                   "      account:\n"
                   "        email: a@x.io\n"
                   "      priority: 5\n"
                   "      reservePct: 20\n")
        err = server.set_account_policy(self.pay(reservePct=20, reclaimAbovePct=70))
        self.assertIsNone(err)
        txt = self.read()
        self.assertIn("reclaimAbovePct: 70", txt)
        # 未传的 priority 被整体替换语义清掉（与原有行为一致：三个标量全量重写）
        self.assertNotIn("priority", txt)

    def test_clear_all_removes_policy(self):
        self.write("auth:\n  accountPolicies:\n"
                   "    - provider: anthropic\n"
                   "      account:\n"
                   "        email: a@x.io\n"
                   "      reservePct: 20\n"
                   "      reclaimAbovePct: 70\n")
        err = server.set_account_policy(self.pay(priority="", reservePct="",
                                                 reclaimAbovePct=""))
        self.assertIsNone(err)
        self.assertIn("accountPolicies: []", self.read())


class TestEmailCase(PolicyBase):
    """omp 的 identityKey 把 email 规范成小写，而策略匹配是逐字节精确比较。

    因此写 config.yml 时必须用凭据里的原始大小写，否则 omp 会打印
    `Account policy matches no stored OAuth account; ignoring it` 并忽略整条策略。
    """
    MIXED = "email:Babybooyxq@gmx.pt|org:910cbe63"

    def setUp(self):
        super().setUp()
        self._orig_emails = server.credential_original_emails
        server.list_accounts = lambda: ({"accounts": [
            {"provider": "anthropic", "identityKey": self.MIXED},
        ]}, None)
        server.credential_original_emails = lambda: {
            self.MIXED.casefold(): "Babybooyxq@gmx.pt"}

    def tearDown(self):
        server.credential_original_emails = self._orig_emails
        super().tearDown()

    def mixed_pay(self, **kw):
        base = {"provider": "anthropic", "identityKey": self.MIXED}
        base.update(kw)
        return base

    def test_new_policy_uses_original_case(self):
        self.write("auth:\n  accountPolicies: []\n")
        err = server.set_account_policy(self.mixed_pay(reservePct=100))
        self.assertIsNone(err)
        self.assertIn("email: Babybooyxq@gmx.pt", self.read())

    def test_existing_policy_recognized_despite_case(self):
        """配置里已是大写时，小写 identityKey 必须命中同一条，而不是再追加一条。"""
        self.write("auth:\n  accountPolicies:\n"
                   "    - provider: anthropic\n"
                   "      account:\n"
                   "        email: Babybooyxq@gmx.pt\n"
                   "      reservePct: 100\n")
        err = server.set_account_policy(self.mixed_pay(priority=3, reservePct=100))
        self.assertIsNone(err)
        txt = self.read()
        self.assertEqual(txt.count("- provider: anthropic"), 1)
        self.assertIn("email: Babybooyxq@gmx.pt", txt)

    def test_missing_original_email_falls_back_to_identity_key(self):
        server.credential_original_emails = lambda: {}
        self.write("auth:\n  accountPolicies: []\n")
        err = server.set_account_policy(self.mixed_pay(reservePct=50))
        self.assertIsNone(err)
        self.assertIn("email: Babybooyxq@gmx.pt", self.read())


if __name__ == "__main__":
    unittest.main()
