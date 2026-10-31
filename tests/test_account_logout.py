# -*- coding: utf-8 -*-
"""账号删除（omp RPC logout）测试。

覆盖：RPC 子进程会话（ready/事件帧/失败/超时/清理）、按 credentialId 删除的
校验（provider、ID 类型、已变化、api_key、身份不一致、重复删除），以及
identityKey 为空账号（devin）也要出现在账号列表里。

运行：python -m pytest tests/test_account_logout.py -v
      （或 cd tests && python -m unittest test_account_logout -v；仓库无 tests/__init__.py，
        所以 `python -m unittest tests.test_account_logout` 不可用，其他测试同样如此。）
"""
import json
import os
import queue
import subprocess
import urllib.error
import urllib.request
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


class FakeOmp:
    """模拟 `omp --mode rpc` 子进程：按写入的 JSONL 请求同步产帧。"""

    def __init__(self, responder, ready=True):
        self.q = queue.Queue()
        self.responder = responder
        self.stdin = self
        self.stdout = self
        self.stderr = _Empty()
        self.returncode = None
        self.killed = False
        self.terminated = False
        self.closed = False
        self.stuck = False
        if ready:
            self._push({"type": "ready", "protocolVersion": 1})
        else:
            self._push(None)  # 没有任何就绪帧就 EOF

    def _push(self, obj):
        self.q.put(None if obj is None else json.dumps(obj) + "\n")

    # --- stdin ---
    def write(self, data):
        for line in data.splitlines():
            if not line.strip():
                continue
            out = self.responder(json.loads(line))
            if out is not None:      # None = 永不应答（模拟卡死）
                self._push(out)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True

    # --- stdout ---
    def readline(self):
        while not self.killed:
            try:
                item = self.q.get(timeout=0.02)
            except queue.Empty:
                continue
            return "" if item is None else item
        return ""

    # --- 进程控制 ---
    def wait(self, timeout=None):
        if self.stuck and not self.killed:
            time.sleep(min(timeout or 1, 1))   # 模拟不响应的子进程
            raise subprocess.TimeoutExpired("omp", timeout)
        self.returncode = 0 if not self.killed else -9
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.stuck = False
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.stuck = False


class _Empty:
    def read(self, *a):
        return ""

    def close(self):
        pass


def ok(req, data):
    return {"id": req.get("id"), "type": "response", "command": req.get("type"),
            "success": True, "data": data}


def fail(req, error):
    return {"id": req.get("id"), "type": "response", "command": req.get("type"),
            "success": False, "error": error}


class TestRpcSession(unittest.TestCase):
    def test_reads_ready_and_returns_each_response_in_order(self):
        def responder(req):
            if req["type"] == "get_btw":  # 无关命令
                return ok(req, {})
            return ok(req, {"echo": req["type"]})
        # 事件帧混在响应之间不应影响解析
        fake = FakeOmp(lambda r: ok(r, {"echo": r["type"]}))
        orig_push = fake._push

        def push(obj):
            if obj is not None:
                orig_push({"type": "message_update", "delta": "x"})
            orig_push(obj)
        fake._push = push
        got = server._rpc_session(
            lambda send: [send("a"), send("b", k=1)], timeout=5, spawn=lambda: fake)
        self.assertEqual(got, [{"echo": "a"}, {"echo": "b"}])
        self.assertTrue(fake.closed, "会话结束必须关闭 stdin")

    def test_failure_response_raises_with_upstream_text(self):
        fake = FakeOmp(lambda r: fail(r, "Credential 7 is not stored for anthropic"))
        with self.assertRaises(server.RpcFailure) as cm:
            server._rpc_session(lambda send: send("logout", providerId="anthropic",
                                                  credentialId=7),
                                timeout=5, spawn=lambda: fake)
        self.assertIn("not stored", str(cm.exception))

    def test_missing_ready_frame_is_error(self):
        fake = FakeOmp(lambda r: ok(r, {}), ready=False)
        with self.assertRaises(server.RpcFailure):
            server._rpc_session(lambda send: send("get_login_providers"),
                                timeout=5, spawn=lambda: fake)

    def test_hang_times_out_and_kills_child(self):
        fake = FakeOmp(lambda r: None)  # 永不回应
        fake.stuck = True
        with self.assertRaises(server.RpcFailure) as cm:
            server._rpc_session(lambda send: send("get_login_providers"), timeout=0.3,
                                spawn=lambda: fake)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertTrue(fake.terminated or fake.killed,
                        "超时必须终止子进程，不能留下挂死的 omp")
        self.assertFalse(fake.stuck, "终止后子进程不应仍处于卡死状态")

    def test_spawn_failure_is_reported(self):
        def boom():
            raise OSError("no omp")
        with self.assertRaises(server.RpcFailure) as cm:
            server._rpc_session(lambda send: send("x"), timeout=5, spawn=boom)
        self.assertEqual(cm.exception.code, "unavailable")


class TestRpcAccountsIndex(unittest.TestCase):
    def test_only_authenticated_providers_are_queried(self):
        provider_frames = [{"id": "openai-codex", "available": True, "authenticated": True},
                           {"id": "zai", "available": True, "authenticated": False}]
        asked = []

        def responder(req):
            if req["type"] == "get_login_providers":
                return ok(req, {"providers": provider_frames})
            asked.append(req["providerId"])
            return ok(req, {"accounts": [
                {"credentialId": 10, "provider": req["providerId"], "label": "a@b",
                 "detail": "a@b · oauth #10", "type": "oauth", "active": True}]})

        got = server.rpc_accounts_index(timeout=5, spawn=lambda: FakeOmp(responder))
        self.assertEqual(asked, ["openai-codex"])
        self.assertEqual([a["credentialId"] for a in got["openai-codex"]], [10])

    def test_unknown_provider_with_empty_list_is_kept_as_empty(self):
        def responder(req):
            if req["type"] == "get_login_providers":
                return ok(req, {"providers": [
                    {"id": "devin", "available": True, "authenticated": True}]})
            return ok(req, {"accounts": []})
        got = server.rpc_accounts_index(timeout=5, spawn=lambda: FakeOmp(responder))
        self.assertEqual(got, {"devin": []})


class Stub:
    """把网络/RPC/DB 外部依赖替换成可控桩。"""

    def __init__(self, rpc=None, ids=None, usage=None, broker=None, agent_dir="__keep__"):
        self.rpc = rpc or {}
        self.ids = ids if ids is not None else {}
        self.usage = usage if usage is not None else (
            {"accounts": [{"provider": "anthropic", "identityKey": "email:a@x.io"}]}, None)
        self.broker = broker or {"enabled": False, "known": True, "blocked": False}
        self.agent_dir = agent_dir
        self.logout_calls = []
        self.orig = {}
        self.fake_logout = self._default_logout

    def _default_logout(self, provider, credential_id, timeout=None):
        self.logout_calls.append((provider, credential_id))
        return {"remainingSource": "env: ANTHROPIC_API_KEY"}

    def _gate(self, timeout=15):
        gate = {"blocked": bool(self.broker.get("blocked")),
                "broker": dict(self.broker), "reason": None}
        if self.agent_dir is None:
            gate.update(blocked=True, reason="无法确定 omp 的 agent 目录")
        elif gate["blocked"]:
            gate["reason"] = "omp 正在使用 auth-broker 托管凭据，本页不执行远程删除"
        return gate

    def __enter__(self):
        self.orig = {
            "rpc_accounts_index": server.rpc_accounts_index,
            "credential_identities": server.credential_identities,
            "list_accounts": server.list_accounts,
            "invalidate_usage": server.invalidate_usage,
            "rpc_logout": server.rpc_logout,
            "deletion_gate": server.deletion_gate,
        }

        def fake_rpc(timeout=None, spawn=None):
            if isinstance(self.rpc, Exception):
                raise self.rpc
            return self.rpc

        server.rpc_accounts_index = fake_rpc
        server.credential_identities = lambda: self.ids
        server.list_accounts = lambda: self.usage
        server.invalidate_usage = lambda provider: {"ok": True}
        server.rpc_logout = lambda p, c, timeout=None: self.fake_logout(p, c, timeout)
        server.deletion_gate = self._gate
        if self.agent_dir != "__keep__":
            server.AGENT_DIR = self.agent_dir
        return self

    def __exit__(self, *a):
        for k, v in self.orig.items():
            setattr(server, k, v)


ANTH = {"credentialId": 5, "provider": "anthropic", "label": "a@x.io (Org)",
        "detail": "a@x.io · account acct · oauth #5", "type": "oauth", "active": True}


class TestLogoutAccount(unittest.TestCase):
    def run_logout(self, payload, stub):
        with stub:
            return stub, server.logout_account(payload)

    def test_identity_upgrade_to_org_scope_is_refused(self):
        """旧页面持有的 email:a 不能删已被 omp 升级成 email:a|org:new 的同一 ID。"""
        stub = Stub(rpc={"anthropic": [ANTH]},
                    ids={5: ("anthropic", "email:a@x.io|org:new")})
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5,
                                "identityKey": "email:a@x.io"}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "stale")
        self.assertEqual(stub.logout_calls, [])

    def test_identity_field_drop_is_refused(self):
        """反向也成立：当前没有 org 而页面带了 org 同样是身份变更。"""
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "email:a@x.io")})
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5,
                                "identityKey": "email:a@x.io|org:old"}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "stale")
        self.assertEqual(stub.logout_calls, [])

    def test_missing_identity_on_either_side_is_refused(self):
        """无法核验身份时不得降级放行。"""
        # 库里没有身份，但页面给了身份
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "")})
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5,
                                "identityKey": "email:a@x.io"}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "stale")
        # 库里有身份，但页面没给
        stub2 = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "email:a@x.io")})
        _, r2 = self.run_logout({"provider": "anthropic", "credentialId": 5}, stub2)
        self.assertFalse(r2["ok"], r2)
        self.assertEqual(r2["code"], "stale")
        self.assertEqual(stub.logout_calls + stub2.logout_calls, [])

    def test_credential_missing_from_local_db_is_refused(self):
        """凭据库读不到该行时不能凭 RPC 单方面说法删。"""
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={})
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5,
                                "identityKey": "email:a@x.io"}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "unavailable")
        self.assertEqual(stub.logout_calls, [])

    def test_rpc_reported_provider_disagreeing_with_request_is_refused(self):
        other = dict(ANTH, provider="openai-codex")
        stub = Stub(rpc={"anthropic": [other]}, ids={5: ("anthropic", "email:a@x.io")})
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5,
                                "identityKey": "email:a@x.io"}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "stale")
        self.assertEqual(stub.logout_calls, [])

    def test_broker_enabled_blocks_deletion(self):
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "email:a@x.io")},
                    broker={"enabled": True, "known": True, "blocked": True})
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5,
                                "identityKey": "email:a@x.io"}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "broker")
        self.assertEqual(stub.logout_calls, [])

    def test_unknown_broker_state_blocks_deletion(self):
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "email:a@x.io")},
                    broker={"enabled": False, "known": False, "blocked": True})
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5,
                                "identityKey": "email:a@x.io"}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "broker")
        self.assertEqual(stub.logout_calls, [])

    def test_unresolved_agent_dir_blocks_deletion(self):
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "email:a@x.io")},
                    agent_dir=None)
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5,
                                "identityKey": "email:a@x.io"}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "unavailable")
        self.assertEqual(stub.logout_calls, [])

    def test_deletes_by_credential_id_and_reports_remaining_source(self):
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "email:a@x.io")})
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5,
                                "identityKey": "email:a@x.io"}, stub)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["remainingSource"], "仍有其他认证源时应为 True")
        self.assertEqual(stub.logout_calls, [("anthropic", 5)])

    def test_no_remaining_source_reports_false(self):
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "email:a@x.io")})
        stub.fake_logout = lambda p, c, timeout=None: {"remainingSource": None}
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5,
                                "identityKey": "email:a@x.io"}, stub)
        self.assertTrue(r["ok"], r)
        self.assertIs(r["remainingSource"], False)

    def test_rejects_bad_provider_and_non_integer_id(self):
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "email:a@x.io")})
        for payload in ({"provider": "anthropic; rm -rf", "credentialId": 5},
                        {"provider": "anthropic", "credentialId": "5"},
                        {"provider": "anthropic", "credentialId": True},
                        {"provider": "anthropic", "credentialId": 0}):
            _, r = self.run_logout(payload, stub)
            self.assertFalse(r["ok"], payload)
            self.assertEqual(r["code"], "invalid", payload)
        self.assertEqual(stub.logout_calls, [])

    def test_rejects_credential_that_is_no_longer_listed(self):
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "email:a@x.io")})
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 17}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "stale")
        self.assertEqual(stub.logout_calls, [], "对象已变化时不得调用 logout")

    def test_rejects_provider_mismatch_for_same_id(self):
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "email:a@x.io")})
        _, r = self.run_logout({"provider": "openai-codex", "credentialId": 5}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "stale")
        self.assertEqual(stub.logout_calls, [])

    def test_rejects_identity_mismatch_for_same_email_other_org(self):
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "email:a@x.io|org:o1")})
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5,
                                "identityKey": "email:a@x.io|org:o2"}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "stale")
        self.assertEqual(stub.logout_calls, [])

    def test_deletes_api_key_credential_too(self):
        """API Key 凭据同样经 omp 的 logout 删除；本功能不排除它们。"""
        api = {"credentialId": 15, "provider": "cline-pass", "label": "API key #15",
               "detail": "", "type": "api_key", "active": True}
        stub = Stub(rpc={"cline-pass": [api]}, ids={15: ("cline-pass", "")})
        _, r = self.run_logout({"provider": "cline-pass", "credentialId": 15,
                                "identityKey": None}, stub)
        self.assertTrue(r["ok"], r)
        self.assertEqual(stub.logout_calls, [("cline-pass", 15)])

    def test_api_key_still_needs_identity_consistency_when_given(self):
        api = {"credentialId": 15, "provider": "cline-pass", "label": "API key #15",
               "detail": "", "type": "api_key", "active": True}
        # DB 说该 id 属于别的 provider：必须拒绝，不能靠 RPC 的单方面说法删
        stub = Stub(rpc={"cline-pass": [api]}, ids={15: ("zai", "")})
        _, r = self.run_logout({"provider": "cline-pass", "credentialId": 15}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "stale")
        self.assertEqual(stub.logout_calls, [])

    def test_rpc_failure_is_reported_not_swallowed(self):
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={5: ("anthropic", "email:a@x.io")})

        def failing(provider, credential_id, timeout=None):
            raise server.RpcFailure("Credential 5 is not stored for anthropic", "gone")
        stub.fake_logout = failing
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5,
                                "identityKey": "email:a@x.io"}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "gone")
        self.assertIn("not stored", r["error"])

    def test_listing_failure_blocks_deletion(self):
        stub = Stub(rpc=server.RpcFailure("omp RPC 超时（25s）", "timeout"), ids={})
        _, r = self.run_logout({"provider": "anthropic", "credentialId": 5}, stub)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "unavailable")
        self.assertEqual(stub.logout_calls, [])

    def test_second_delete_of_same_credential_is_refused(self):
        accounts = {"anthropic": [ANTH]}

        def fake_logout(provider, credential_id, timeout=None):
            accounts["anthropic"] = []
            return {"remainingSource": None}

        stub = Stub(rpc=accounts, ids={5: ("anthropic", "email:a@x.io")})
        stub.fake_logout = fake_logout
        with stub:
            first = server.logout_account({"provider": "anthropic", "credentialId": 5,
                                           "identityKey": "email:a@x.io"})
            second = server.logout_account({"provider": "anthropic", "credentialId": 5,
                                            "identityKey": "email:a@x.io"})
        self.assertTrue(first["ok"], first)
        self.assertFalse(second["ok"])
        self.assertEqual(second["code"], "stale")


class TestNoOrderPairing(unittest.TestCase):
    """无 identityKey 的凭据绝不能按顺序/个数猜着配对：只能精确对应或独立成条。"""

    def state(self, rpc, ids, usage, broker=None):
        stub = Stub(rpc=rpc, ids=ids, usage=(usage, None), broker=broker)
        import tempfile
        orig_conf, orig_usage = server.CONFIG, server.usage_report
        tmp = tempfile.TemporaryDirectory()
        try:
            server.CONFIG = Path(tmp.name) / "config.yml"
            server.CONFIG.write_bytes(b"auth:\n  accountPolicies: []\n")
            server.usage_report = lambda: {"reports": []}
            with stub:
                return server.accounts_state()
        finally:
            server.CONFIG, server.usage_report = orig_conf, orig_usage
            tmp.cleanup()

    def test_broker_mode_reports_note_instead_of_upstream_error(self):
        """broker 下连 usage 都取不到时，要给出 broker 说明而不是误导性的报错。"""
        stub = Stub(rpc={"anthropic": [ANTH]}, ids={},
                    usage=({}, "omp usage accounts 失败"),
                    broker={"enabled": True, "known": True, "blocked": True})
        import tempfile
        orig_conf, orig_usage = server.CONFIG, server.usage_report
        tmp = tempfile.TemporaryDirectory()
        try:
            server.CONFIG = Path(tmp.name) / "config.yml"
            server.CONFIG.write_bytes(b"auth:\n  accountPolicies: []\n")
            server.usage_report = lambda: {"reports": []}
            with stub:
                # list_accounts 返回 (None, err) 的形状
                server.list_accounts = lambda: (None, "omp usage accounts 失败")
                state = server.accounts_state()
        finally:
            server.CONFIG, server.usage_report = orig_conf, orig_usage
            tmp.cleanup()
        self.assertIsNone(state.get("error"))
        self.assertTrue(state.get("brokerNote"))
        self.assertEqual(state["providers"], [])

    def test_two_unidentified_credentials_stay_self_contained(self):
        """两个 devin 登录都没有身份标识：各自独立成条，标签来自自己的 RPC 条目。"""
        rpc = {"devin": [
            {"credentialId": 6, "provider": "devin", "label": "https://devin.invalid/one",
             "detail": "", "type": "oauth", "active": False},
            {"credentialId": 7, "provider": "devin", "label": "https://devin.invalid/two",
             "detail": "", "type": "oauth", "active": True}]}
        state = self.state(rpc, {6: ("devin", ""), 7: ("devin", "")},
                           {"accounts": [{"provider": "devin", "identityKey": None},
                                         {"provider": "devin", "identityKey": None}]})
        devin = [a for p in state["providers"] if p["provider"] == "devin"
                 for a in p["accounts"]]
        self.assertEqual(len(devin), 2)
        by_cid = {a["credentialId"]: a for a in devin}
        self.assertEqual(set(by_cid), {6, 7})
        self.assertEqual(by_cid[6]["label"], "https://devin.invalid/one")
        self.assertEqual(by_cid[7]["label"], "https://devin.invalid/two")
        self.assertTrue(by_cid[7]["active"], "active 必须跟着自己的凭据走")
        self.assertFalse(by_cid[6]["active"])
        # 无身份标识的条目绝不能显示成「可设策略」
        for a in devin:
            self.assertFalse(a["policyEditable"])

    def test_old_omp_without_logout_rpc_keeps_accounts_but_disables_removal(self):
        """旧版 omp 没有 get_logout_accounts：本机实测回 `Unknown command: ...`。

        此时账号仍列出，但 canDelete 全为 False 且给出 rpcError——不静默失败。
        """
        def responder(req):
            if req["type"] == "get_login_providers":
                return ok(req, {"providers": [
                    {"id": "anthropic", "available": True, "authenticated": True}]})
            return fail(req, "Unknown command: get_logout_accounts")

        orig = (server.CONFIG, server.usage_report, server.list_accounts,
                server._rpc_spawn, server.credential_identities)
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        try:
            server.CONFIG = Path(tmp.name) / "config.yml"
            server.CONFIG.write_bytes(b"auth:\n  accountPolicies: []\n")
            server.usage_report = lambda: {"reports": []}
            server.list_accounts = lambda: ({"accounts": [
                {"provider": "anthropic", "identityKey": "email:a@x.io"}]}, None)
            # 走真实的 rpc_accounts_index / _rpc_session，只把子进程换成假 omp
            server._rpc_spawn = lambda: FakeOmp(responder)
            state = server.accounts_state()
        finally:
            (server.CONFIG, server.usage_report, server.list_accounts,
             server._rpc_spawn, server.credential_identities) = orig
            tmp.cleanup()
        self.assertEqual(state.get("rpcError"), "upstream")
        anth = [a for p in state["providers"] for a in p["accounts"]]
        self.assertEqual(len(anth), 1)
        self.assertFalse(anth[0]["canDelete"])

    def test_same_credential_listed_under_two_provider_ids_appears_once(self):
        """omp 的 provider 别名会让同一条凭据在两个 providerId 下都出现（本机实测：
        zai 与 zai-coding-plan 都返回 credentialId 8）。它必须只出现一次，
        且归到凭据库记录的真实 provider。"""
        api = {"credentialId": 8, "provider": "zai", "label": "API key #8",
               "detail": "", "type": "api_key", "active": True}
        alias = dict(api, provider="zai-coding-plan")
        # 库里的真实 provider 排在别名的后面，结果仍必须归到 zai
        state = self.state({"zai-coding-plan": [alias], "zai": [api]},
                           {8: ("zai", "")}, {"accounts": []})
        rows = [(p["provider"], a["credentialId"]) for p in state["providers"]
                for a in p["accounts"]]
        self.assertEqual(rows, [("zai", 8)])

    def test_api_key_without_identity_is_listed_once(self):
        rpc = {"zai": [{"credentialId": 8, "provider": "zai", "label": "API key #8",
                        "detail": "", "type": "api_key", "active": True}]}
        state = self.state(rpc, {8: ("zai", "")}, {"accounts": []})
        zai = [a for p in state["providers"] if p["provider"] == "zai"
               for a in p["accounts"]]
        self.assertEqual(len(zai), 1)
        self.assertEqual(zai[0]["credentialId"], 8)


class TestAccountsStateKeepsUnidentified(unittest.TestCase):
    def test_account_without_identity_key_is_listed_and_deletable(self):
        stub = Stub(
            rpc={"devin": [{"credentialId": 16, "provider": "devin",
                            "label": "https://devin.ai/acme", "detail": "acme #16",
                            "type": "oauth", "active": False}],
                 "anthropic": [ANTH]},
            ids={16: ("devin", ""), 5: ("anthropic", "email:a@x.io")},
            usage=({"accounts": [
                {"provider": "anthropic", "identityKey": "email:a@x.io"},
                {"provider": "devin", "identityKey": ""}]}, None))
        orig_conf, orig_usage = server.CONFIG, server.usage_report
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        try:
            server.CONFIG = Path(tmp.name) / "config.yml"
            server.CONFIG.write_bytes(b"auth:\n  accountPolicies: []\n")
            server.usage_report = lambda: {"reports": []}
            with stub:
                state = server.accounts_state()
        finally:
            server.CONFIG, server.usage_report = orig_conf, orig_usage
            tmp.cleanup()
        devin = [a for p in state["providers"] if p["provider"] == "devin"
                 for a in p["accounts"]]
        self.assertEqual(len(devin), 1, "identityKey 为空的账号不能被漏掉")
        self.assertEqual(devin[0]["credentialId"], 16)
        self.assertTrue(devin[0]["canDelete"])
        self.assertFalse(devin[0]["policyEditable"])
        anth = [a for p in state["providers"] if p["provider"] == "anthropic"
                for a in p["accounts"]][0]
        self.assertEqual(anth["credentialId"], 5)
        self.assertTrue(anth["policyEditable"])

    def test_accounts_stay_listed_when_rpc_unavailable(self):
        stub = Stub(rpc=server.RpcFailure("omp RPC 超时（25s）", "timeout"),
                    ids={5: ("anthropic", "email:a@x.io")})
        orig_conf, orig_usage = server.CONFIG, server.usage_report
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        try:
            server.CONFIG = Path(tmp.name) / "config.yml"
            server.CONFIG.write_bytes(b"auth:\n  accountPolicies: []\n")
            server.usage_report = lambda: {"reports": []}
            with stub:
                state = server.accounts_state()
        finally:
            server.CONFIG, server.usage_report = orig_conf, orig_usage
            tmp.cleanup()
        anth = [a for p in state["providers"] for a in p["accounts"]]
        self.assertEqual(len(anth), 1)
        self.assertFalse(anth[0]["canDelete"], "拿不到 RPC 账号列表时不能提供删除")
        self.assertIn("timeout", state["rpcError"])


class TestHttpRoute(unittest.TestCase):
    """真实 HTTP 层：路由状态码、同源校验、请求体。"""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        # Host 白名单按 server.PORT 构造，测试服务必须绑同一个端口才代表
        # 「浏览器访问的就是本服务」；端口全被占就跳过，绝不伪造白名单。
        cls._orig_port = server.PORT
        cls.srv = None
        for port in range(8860, 8900):
            try:
                cls.srv = ThreadingHTTPServer(("127.0.0.1", port), server.Handler)
            except OSError:
                continue
            server.PORT = port
            break
        if cls.srv is None:
            raise unittest.SkipTest("no free port for the HTTP route test")
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        server.PORT = cls._orig_port

    def setUp(self):
        self.orig_logout = server.logout_account
        self.calls = []

    def tearDown(self):
        server.logout_account = self.orig_logout

    def post(self, body, headers=None):
        url = f"http://127.0.0.1:{self.port}/api/accounts/logout"
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              **(headers or {})}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_success_returns_200_and_body(self):
        server.logout_account = lambda p: (self.calls.append(p),
                                          {"ok": True, "remainingSource": None})[1]
        status, body = self.post({"provider": "anthropic", "credentialId": 5})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.calls, [{"provider": "anthropic", "credentialId": 5}])

    def test_stale_credential_is_409(self):
        server.logout_account = lambda p: {"ok": False, "code": "stale", "error": "已变化"}
        status, body = self.post({"provider": "anthropic", "credentialId": 5})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "stale")

    def test_rpc_unavailable_is_503_and_invalid_is_400(self):
        server.logout_account = lambda p: {"ok": False, "code": "unavailable", "error": "x"}
        self.assertEqual(self.post({})[0], 503)
        server.logout_account = lambda p: {"ok": False, "code": "invalid", "error": "x"}
        self.assertEqual(self.post({})[0], 400)

    def test_dns_rebinding_host_is_rejected(self):
        """Host 不在白名单必须拒绝：rebinding 下 Origin 与 Host 完全一致。"""
        server.logout_account = lambda p: (self.calls.append(p), {"ok": True})[1]
        status, _ = self.post({"provider": "anthropic", "credentialId": 5},
                              {"Host": f"evil.example:{self.port}",
                               "Origin": f"http://evil.example:{self.port}"})
        self.assertEqual(status, 403)
        self.assertEqual(self.calls, [], "rebinding 攻击绝不能触发删除")

    def test_cross_site_origin_is_rejected_before_any_deletion(self):
        server.logout_account = lambda p: (self.calls.append(p), {"ok": True})[1]
        status, body = self.post({"provider": "anthropic", "credentialId": 5},
                                 {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.calls, [], "跨站请求绝不能触发删除")

    def test_same_origin_and_no_origin_are_accepted(self):
        server.logout_account = lambda p: {"ok": True}
        host = f"127.0.0.1:{self.port}"
        self.assertEqual(self.post({}, {"Origin": f"http://{host}"})[0], 200)
        self.assertEqual(self.post({}, {"Referer": f"http://{host}/?x=1"})[0], 200)
        self.assertEqual(self.post({})[0], 200)


if __name__ == "__main__":
    unittest.main()


class TestProfileEnvSemantics(unittest.TestCase):
    """R4：配置档判定必须是「存在性」语义，不能让 truthy 回退改掉 omp 的档位。

    omp 的规则：--profile > OMP_PROFILE > PI_PROFILE；显式空串与 "default"
    都表示默认档。曾经写成 `OMP_PROFILE or PI_PROFILE`，于是显式
    `OMP_PROFILE=''`（用户明确要默认档）会错误地退回 `PI_PROFILE=work`。
    """

    KEYS = ("OMP_PROFILE", "PI_PROFILE")

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def profile(self):
        return server._env_profile()

    def test_neither_set_means_default(self):
        self.assertIsNone(self.profile())

    def test_explicit_empty_omp_wins_over_pi(self):
        """显式 OMP_PROFILE='' 必须压过 PI_PROFILE=work（这就是原缺陷）。"""
        os.environ["OMP_PROFILE"] = ""
        os.environ["PI_PROFILE"] = "work"
        self.assertIsNone(self.profile())

    def test_explicit_empty_omp_alone(self):
        os.environ["OMP_PROFILE"] = ""
        self.assertIsNone(self.profile())

    def test_omp_default_sentinel(self):
        os.environ["OMP_PROFILE"] = "default"
        os.environ["PI_PROFILE"] = "work"
        self.assertIsNone(self.profile())

    def test_omp_named_wins_over_pi(self):
        os.environ["OMP_PROFILE"] = "chosen"
        os.environ["PI_PROFILE"] = "work"
        self.assertEqual(self.profile(), "chosen")

    def test_pi_only_is_used_when_omp_absent(self):
        os.environ["PI_PROFILE"] = "work"
        self.assertEqual(self.profile(), "work")

    def test_pi_default_sentinel(self):
        os.environ["PI_PROFILE"] = "default"
        self.assertIsNone(self.profile())

    def test_no_profile_flag_is_synthesized(self):
        """子进程继承环境，由 omp 自己解析档位；本页不注入 --profile。"""
        os.environ["OMP_PROFILE"] = "chosen"
        self.assertEqual(server.omp_cmd(["omp", "config", "path"]),
                         ["omp", "config", "path"])
        self.assertNotIn("--profile", server._rpc_argv())
