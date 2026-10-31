# -*- coding: utf-8 -*-
"""自定义供应商定义删除（models.yml 的 providers.<name>）测试。

覆盖：定义扫描（含空供应商、仅 modelOverrides 的内置覆盖块、flow 单行块、
重复键/内联 providers 拒绝）、引用阻断（modelRoles / fallbackChains /
共享通配 enabledModels）、enabledModels 显式条目的同步清理、陈旧摘要、
重复删除、按字节保留（BOM/CRLF/注释/其他供应商）、备份、写入失败无部分修改、
真实 HTTP 路由的跨站拒绝与状态码。

绝不读写真实 ~/.omp/agent：全部指向临时目录里的合成文件。

运行：python -m pytest tests/test_custom_providers.py -v
      （或 cd tests && python -m unittest test_custom_providers -v）
"""
import hashlib
import json
import sys
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


BASE_MODELS = (
    "providers:\n"
    "  builtin-ov:\n"
    "    modelOverrides:\n"
    "      - id: gemini-x\n"
    "        name: Gemini X\n"
    "  # hangzhale 这一段是自定义供应商\n"
    "  hangzhale:\n"
    "    baseUrl: http://127.0.0.1:9999/v1\n"
    "    api: openai-completions\n"
    "    apiKey: sk-SECRET-should-never-leak\n"
    "    authHeader: true\n"
    "    models:\n"
    "      - id: gpt-6.1-sol\n"
    "        name: GPT-6.1 Sol (hangzhale)\n"
    "  other:\n"
    "    baseUrl: http://127.0.0.1:9998/v1\n"
    "    api: openai-completions\n"
    "    models:\n"
    "      - id: m-other\n"
    "\n"
    "# 文件尾注释必须原样保留\n"
)


class CustomProviderBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.models = self.dir / "models.yml"
        self.config = self.dir / "config.yml"
        self._orig = (server.AGENT_DIR, server.CONFIG, server.list_models)
        server.AGENT_DIR = self.dir
        server.CONFIG = self.config
        # 目录桩默认列出 BASE_MODELS 的模型：真实 omp 目录一定包含已声明的模型，
        # 裸 ID 的归属只看目录（缺失记录永不命中）。
        self.catalog = [
            {"selector": "hangzhale/gpt-6.1-sol", "provider": "hangzhale", "kind": "chat"},
            {"selector": "other/m-other", "provider": "other", "kind": "chat"},
        ]
        server.list_models = lambda timeout=None: list(self.catalog)
        self._orig_full = server.full_catalog
        server.full_catalog = lambda: list(self.catalog)
        self.write_config("modelRoles:\n  default: other/m-other\n")

    def tearDown(self):
        server.AGENT_DIR, server.CONFIG, server.list_models = self._orig
        server.full_catalog = self._orig_full
        self._tmp.cleanup()

    def write_models(self, text, encoding="utf-8"):
        self.models.write_bytes(text.encode(encoding))

    def write_config(self, text):
        self.config.write_bytes(text.encode("utf-8"))

    def read(self, path):
        return path.read_bytes()

    def state(self):
        return server.custom_providers_state()

    def provider(self, name, data=None):
        for p in (data or self.state())["providers"]:
            if p["name"] == name:
                return p
        return None

    def delete(self, name, **extra):
        dg = extra.pop("expectDigest", None)
        if dg is None:
            dg = digest(self.models.read_bytes())
        if "expectConfigDigest" not in extra:
            raw = self.config.read_bytes() if self.config.exists() else None
            extra["expectConfigDigest"] = server._config_digest(raw)
        return server.delete_custom_provider(
            {"provider": name, "expectDigest": dg, **extra})


class TestScan(CustomProviderBase):
    def test_lists_custom_providers_with_models(self):
        self.write_models(BASE_MODELS)
        data = self.state()
        self.assertIsNone(data["error"])
        self.assertEqual(data["file"], str(self.models))
        p = self.provider("hangzhale", data)
        self.assertTrue(p["deletable"])
        self.assertEqual(p["models"], ["gpt-6.1-sol"])

    def test_model_overrides_only_block_is_not_deletable(self):
        self.write_models(BASE_MODELS)
        p = self.provider("builtin-ov")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "builtin_override")

    def test_empty_custom_provider_is_listed_and_deletable(self):
        self.write_models("providers:\n"
                          "  ghost:\n"
                          "    baseUrl: http://127.0.0.1:9997/v1\n"
                          "    api: openai-completions\n")
        p = self.provider("ghost")
        self.assertIsNotNone(p, "没有模型列出的自定义供应商也必须出现在列表里")
        self.assertEqual(p["models"], [])
        self.assertTrue(p["deletable"])

    def test_state_never_exposes_secret_values(self):
        self.write_models(BASE_MODELS)
        blob = json.dumps(self.state(), ensure_ascii=False)
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("127.0.0.1:9999", blob)

    def test_duplicate_provider_key_is_refused(self):
        self.write_models("providers:\n"
                          "  a:\n    baseUrl: http://x/v1\n"
                          "  a:\n    baseUrl: http://y/v1\n")
        self.assertIsNotNone(self.state()["error"])
        r = self.delete("a")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "unsupported")

    def test_inline_providers_map_is_refused(self):
        self.write_models("providers: {a: {baseUrl: http://x/v1}}\n")
        self.assertIsNotNone(self.state()["error"])
        self.assertEqual(self.delete("a")["code"], "unsupported")

    def test_flow_style_single_line_provider_is_handled(self):
        self.write_models("providers:\n"
                          "  flow: {baseUrl: http://x/v1, models: [{id: fm-1}]}\n"
                          "  keep:\n    baseUrl: http://y/v1\n")
        p = self.provider("flow")
        self.assertTrue(p["deletable"])
        self.assertEqual(p["models"], ["fm-1"])

    def test_missing_models_file_is_not_an_error(self):
        data = self.state()
        self.assertIsNone(data["error"])
        self.assertEqual(data["providers"], [])
        r = server.delete_custom_provider(
            {"provider": "anything", "expectDigest": "0" * 64,
             "expectConfigDigest": server._config_digest(self.config.read_bytes())})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "gone")


class TestFileNames(CustomProviderBase):
    def test_models_yaml_is_used_when_models_yml_absent(self):
        yml = self.dir / "models.yaml"
        yml.write_bytes(BASE_MODELS.encode("utf-8"))
        self.assertEqual(self.state()["file"], str(yml))
        r = server.delete_custom_provider(
            {"provider": "hangzhale",
             "expectDigest": digest(yml.read_bytes()),
             "expectConfigDigest": server._config_digest(self.config.read_bytes())})
        self.assertTrue(r["ok"], r)
        self.assertIn(b"  other:\n", yml.read_bytes())

    def test_models_yml_wins_when_both_exist(self):
        self.write_models(BASE_MODELS)
        other = self.dir / "models.yaml"
        other.write_bytes(b"providers:\n  from-yaml:\n    baseUrl: http://q/v1\n")
        self.assertEqual(self.state()["file"], str(self.models))


class TestEmptyForms(CustomProviderBase):
    def test_explicit_empty_providers_map_is_a_normal_empty_list(self):
        self.write_models("providers: {}\notherTop: 1\n")
        data = self.state()
        self.assertIsNone(data["error"])
        self.assertEqual(data["providers"], [])

    def test_providers_with_only_comments_has_no_entries(self):
        self.write_models("providers:\n  # 暂无自定义供应商\n\notherTop: 1\n")
        self.assertIsNone(self.state()["error"])
        self.assertEqual(self.state()["providers"], [])

    def test_missing_providers_key_is_not_an_error(self):
        self.write_models("modelRoles:\n  default: z/z\n")
        self.assertIsNone(self.state()["error"])
        self.assertEqual(self.state()["providers"], [])


class TestGate(CustomProviderBase):
    def test_unknown_agent_dir_disables_deletion(self):
        self.write_models(BASE_MODELS)
        server.AGENT_DIR = None
        data = self.state()
        p = self.provider("hangzhale", data)
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "unavailable")
        r = server.delete_custom_provider(
            {"provider": "hangzhale",
             "expectDigest": digest(self.models.read_bytes()),
             "expectConfigDigest": server._config_digest(self.config.read_bytes())})
        self.assertEqual(r["code"], "unavailable")
        self.assertEqual(self.models.read_bytes(), BASE_MODELS.encode("utf-8"))

    def test_remote_broker_does_not_block_local_definition_edits(self):
        """本操作只改本地 models.yml，与 auth-broker 无关，不应被它拦住。"""
        self.write_models(BASE_MODELS)
        with patch.object(server, "broker_state",
                          return_value={"enabled": True, "known": True, "blocked": True}):
            self.assertTrue(self.provider("hangzhale")["deletable"])
            self.assertTrue(self.delete("hangzhale")["ok"])


class TestDelete(CustomProviderBase):
    def test_removes_only_target_block_and_preserves_every_other_byte(self):
        self.write_models(BASE_MODELS)
        raw = self.models.read_bytes()
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.models.read_bytes()
        self.assertNotIn(b"  hangzhale:\n", out)
        self.assertNotIn(b"SECRET", out)
        self.assertIn(b"builtin-ov", out)
        self.assertIn(b"  other:", out)
        self.assertIn("# 文件尾注释必须原样保留", out.decode("utf-8"))
        self.assertIn("# hangzhale 这一段是自定义供应商", out.decode("utf-8"))
        # 其他供应商的字节片段与原文完全一致
        self.assertIn(b"    models:\n      - id: m-other\n", out)
        self.assertLess(len(out), len(raw))

    def test_crlf_and_bom_are_preserved(self):
        text = "\ufeff" + BASE_MODELS.replace("\n", "\r\n")
        self.write_models(text)
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.models.read_bytes()
        self.assertTrue(out.startswith(b"\xef\xbb\xbf"), "BOM 必须保留")
        self.assertNotIn(b"\n", out.replace(b"\r\n", b""), "不允许出现裸 LF")
        self.assertIn(b"  other:\r\n", out)

    def test_backup_is_created_before_write(self):
        self.write_models(BASE_MODELS)
        before = self.models.read_bytes()
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        backups = list(self.dir.glob("models.yml.bak-settings-web-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)

    def test_stale_digest_is_rejected_without_writing(self):
        self.write_models(BASE_MODELS)
        before = self.models.read_bytes()
        r = self.delete("hangzhale", expectDigest="0" * 64)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "stale")
        self.assertEqual(self.models.read_bytes(), before)

    def test_repeat_delete_reports_gone(self):
        self.write_models(BASE_MODELS)
        self.assertTrue(self.delete("hangzhale")["ok"])
        r = self.delete("hangzhale")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "gone")

    def test_model_overrides_only_block_cannot_be_deleted(self):
        self.write_models(BASE_MODELS)
        before = self.models.read_bytes()
        r = self.delete("builtin-ov")
        self.assertEqual(r["code"], "invalid")
        self.assertEqual(self.models.read_bytes(), before)

    def test_illegal_provider_name_is_rejected(self):
        self.write_models(BASE_MODELS)
        for name in ("../x", "hang zhale", "", "hangzhale/../y", "A/B"):
            with self.subTest(name=name):
                self.assertFalse(self.delete(name)["ok"])

    def test_second_write_failure_restores_both_files(self):
        """第一次写真的落盘、第二次失败时必须把第一个文件也恢复回去。"""
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n"
                          "enabledModels:\n"
                          "  - \"other/*\"\n"
                          "  - \"hangzhale/gpt-6.1-sol\"\n")
        m_before, c_before = self.models.read_bytes(), self.config.read_bytes()
        real = server._atomic_write
        attempts = []

        def flaky(path, data):
            attempts.append(path)
            if path == self.config:
                raise OSError("disk full")
            return real(path, data)

        with patch.object(server, "_atomic_write", side_effect=flaky):
            r = self.delete("hangzhale")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "unavailable")
        self.assertIn("已按备份恢复", r["error"])
        # 两次写入都被真的尝试过：说明第一份文件确实已替换，再被恢复
        self.assertEqual(attempts.count(self.models), 2)
        self.assertEqual(attempts.count(self.config), 1)
        self.assertEqual(self.models.read_bytes(), m_before)
        self.assertEqual(self.config.read_bytes(), c_before)

    def test_unrecoverable_rollback_reports_partial_state_and_backup_paths(self):
        """恢复也失败时不能谎称已回滚：要报 partial、列出未恢复文件与备份。"""
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n"
                          "enabledModels:\n"
                          "  - \"other/*\"\n"
                          "  - \"hangzhale/gpt-6.1-sol\"\n")
        real = server._atomic_write

        def flaky(path, data):
            if path == self.config:
                raise OSError("disk full")
            if path == self.models and data == BASE_MODELS.encode("utf-8"):
                raise OSError("read-only")        # 只有「恢复旧内容」这次失败
            return real(path, data)

        with patch.object(server, "_atomic_write", side_effect=flaky):
            r = self.delete("hangzhale")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "partial")
        self.assertEqual(r["unrestored"], [str(self.models)])
        self.assertIn("未能自动恢复", r["error"])
        self.assertNotIn("已按备份恢复", r["error"])
        self.assertEqual(len(r["backups"]), 2)
        for path in r["backups"]:
            self.assertTrue(Path(path).exists(), "备份路径必须真实存在")
        # 备份里是删除前的原始内容，用户可据此手工恢复
        self.assertIn(b"  hangzhale:\n", Path(r["backups"][0]).read_bytes())

    def test_backup_failure_happens_before_any_replacement(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n"
                          "enabledModels:\n"
                          "  - \"other/*\"\n"
                          "  - \"hangzhale/gpt-6.1-sol\"\n")
        m_before, c_before = self.models.read_bytes(), self.config.read_bytes()
        real = server._atomic_write
        writes = []

        def track(path, data):
            writes.append(path)
            return real(path, data)

        with patch.object(server, "_backup_for",
                          side_effect=OSError("no space for backup")), \
             patch.object(server, "_atomic_write", side_effect=track):
            r = self.delete("hangzhale")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "unavailable")
        self.assertIn("备份失败", r["error"])
        self.assertEqual(writes, [], "备份失败时绝不能已经写过任何文件")
        self.assertEqual(self.models.read_bytes(), m_before)
        self.assertEqual(self.config.read_bytes(), c_before)
        self.assertEqual(list(self.dir.glob("*.bak-settings-web-*")), [])


class TestCommentsAndCollapse(CustomProviderBase):
    def test_indented_comment_inside_block_is_removed_with_it(self):
        self.write_models("providers:\n"
                          "  a:\n"
                          "    baseUrl: http://a/v1\n"
                          "    models:\n"
                          "      - id: a-1\n"
                          "    # a 的块内说明\n"
                          "  b:\n"
                          "    baseUrl: http://b/v1\n")
        self.assertTrue(self.delete("a")["ok"])
        out = self.models.read_text(encoding="utf-8")
        self.assertNotIn("a 的块内说明", out)
        self.assertIn("  b:\n", out)

    def test_comment_at_key_indent_belongs_to_next_provider(self):
        """写在 providers 第一层缩进上的注释是用户对该供应商的说明，不随删除消失。"""
        self.write_models("providers:\n"
                          "  # 说明：b 是备用网关\n"
                          "  b:\n"
                          "    baseUrl: http://b/v1\n"
                          "  a:\n"
                          "    baseUrl: http://a/v1\n")
        self.assertTrue(self.delete("a")["ok"])
        self.assertIn("说明：b 是备用网关", self.models.read_text(encoding="utf-8"))

    def test_deleting_last_provider_keeps_empty_mapping(self):
        self.write_models("providers:\n"
                          "  only:\n"
                          "    baseUrl: http://o/v1\n"
                          "    models:\n"
                          "      - id: o-1\n"
                          "\n"
                          "otherTop: 1\n")
        self.write_config("modelRoles:\n  default: x/y\n")
        self.assertTrue(self.delete("only")["ok"])
        import yaml
        out = self.models.read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(out), {"providers": {}, "otherTop": 1})
        self.assertIn("providers: {}\n", out)

    def test_collapse_keeps_comments_after_last_provider(self):
        self.write_models("providers:\n"
                          "  only:\n"
                          "    baseUrl: http://o/v1\n"
                          "# 关于后面的顶层键\n"
                          "otherTop: 1\n")
        self.write_config("modelRoles:\n  default: x/y\n")
        self.assertTrue(self.delete("only")["ok"])
        out = self.models.read_text(encoding="utf-8")
        self.assertEqual(out, "providers: {}\n# 关于后面的顶层键\notherTop: 1\n")

    def test_block_scalar_body_is_not_treated_as_flow(self):
        self.write_models("providers:\n"
                          "  a:\n"
                          "    baseUrl: http://a/v1\n"
                          "    headers:\n"
                          "      X: |\n"
                          "        [ not flow\n"
                          "        ] nor this\n"
                          "  b:\n"
                          "    baseUrl: http://b/v1\n")
        data = self.state()
        self.assertIsNone(data["error"])
        self.assertEqual([p["name"] for p in data["providers"]], ["a", "b"])
        self.assertTrue(self.delete("a")["ok"])
        import yaml
        out = self.models.read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(out)["providers"]["b"]["baseUrl"],
                         "http://b/v1")

    def test_tagged_command_scalar_does_not_break_scan(self):
        """apiKey 用 !powershell 命令取值是常见写法（方括号、冒号都在引号里）。"""
        self.write_models("providers:\n"
                          "  a:\n"
                          "    baseUrl: http://a/v1\n"
                          "    apiKey: !powershell -NoProfile -Command \"(Get-Item [x])\"\n"
                          "    models:\n"
                          "      - id: a-1\n"
                          "  b:\n"
                          "    baseUrl: http://b/v1\n")
        self.assertEqual([p["name"] for p in self.state()["providers"]], ["a", "b"])
        self.assertTrue(self.delete("a")["ok"])
        self.assertIn("  b:\n", self.models.read_text(encoding="utf-8"))


class TestReferencesCleared(CustomProviderBase):
    """引用不再阻断：删除定义的同时在同一事务里清掉 config.yml 的引用。"""

    def test_role_reference_is_cleared_not_blocking(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  slow: hangzhale/gpt-6.1-sol\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertNotIn("hangzhale", cfg)
        self.assertNotIn("slow:", cfg)
        self.assertIn("modelRoles:", cfg)
        self.assertIn("modelRoles.slow", " ".join(x["path"] for x in r["cleared"]))
        self.assertNotIn("  hangzhale:\n", self.models.read_text(encoding="utf-8"))

    def test_state_lists_references_but_stays_deletable(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  slow: hangzhale/gpt-6.1-sol\n")
        p = self.provider("hangzhale")
        self.assertTrue(p["deletable"], "被引用不再阻断")
        self.assertTrue(any("modelRoles.slow" in x["path"] for x in p["cleared"]))

    def test_thinking_suffix_reference_is_cleared(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  slow: hangzhale/gpt-6.1-sol:high\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertNotIn("slow:", self.config.read_text(encoding="utf-8"))

    def test_fallback_chain_removes_only_this_provider(self):
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n    default:\n"
                          "      - anthropic/claude\n"
                          "      - hangzhale/gpt-6.1-sol\n"
                          "      - other/m-other\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertNotIn("hangzhale", cfg)
        self.assertIn("- anthropic/claude", cfg)
        self.assertIn("- other/m-other", cfg)

    def test_empty_fallback_chain_removes_the_chain_key(self):
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n    advisor:\n"
                          "      - hangzhale/gpt-6.1-sol\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertNotIn("advisor", cfg)
        self.assertIn("fallbackChains:", cfg)

    def test_flow_fallback_chain_is_unsupported(self):
        """flow 写法的链不再自动改写：含目标引用即 unsupported。"""
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n"
                          "    default: [other/m-other, hangzhale/gpt-6.1-sol]\n")
        before = self.config.read_bytes()
        p = self.provider("hangzhale")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "unsupported")
        r = self.delete("hangzhale")
        self.assertEqual(r["code"], "unsupported")
        self.assertEqual(self.config.read_bytes(), before)

    def test_explicit_enabled_entry_is_removed_with_provider(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n"
                          "enabledModels:\n"
                          "  - \"other/m-other\"\n"
                          "  - \"hangzhale/gpt-6.1-sol\"\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertNotIn("hangzhale", cfg)
        self.assertIn("other/m-other", cfg)

    def test_provider_scoped_wildcard_entry_is_cleaned(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n"
                          "enabledModels:\n"
                          "  - \"other/*\"\n"
                          "  - \"hangzhale/*\"\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertNotIn("hangzhale", cfg)
        self.assertIn('"other/*"', cfg)

    def test_shared_wildcard_is_also_cleaned_after_warning(self):
        """`*` 命中本供应商：删除后它仍在名单中会让其他供应商继续显示，
        所以也要从名单里清掉（确认框已列出，用户同意才执行）。"""
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n"
                          "enabledModels:\n  - \"*\"\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertNotIn('"*"', cfg)

    def test_emptying_enabled_models_is_warned_not_blocked(self):
        """摘完条目名单变空：仍然允许删除（用户已在确认框看到警示）。"""
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n"
                          "enabledModels:\n  - \"hangzhale/*\"\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["enabledEmptied"],
                        "名单被摘空时必须如实上报，页面据此给出警示")
        cfg = self.config.read_text(encoding="utf-8")
        self.assertIn("enabledModels", cfg)
        self.assertNotIn("hangzhale", cfg)

    def test_role_with_comma_list_removes_only_this_candidate(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n"
                          "  default: 'gpt-6.1-sol,other/m-other'\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertNotIn("gpt-6.1-sol", cfg)
        self.assertIn("default: other/m-other", cfg)

    def test_role_with_only_target_candidate_removes_key(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: gpt-6.1-sol\n"
                          "  smol: other/m-other\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertNotIn("default:", cfg)
        self.assertIn("smol: other/m-other", cfg)

    def test_agent_model_overrides_entry_is_cleared(self):
        self.write_models(BASE_MODELS)
        self.write_config("task:\n  agentModelOverrides:\n"
                          "    scout: 'other/m-other, gpt-6.1-sol:high'\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertNotIn("gpt-6.1-sol", cfg)
        self.assertIn("scout: other/m-other", cfg)

    def test_bare_enabled_entry_is_removed(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n"
                          "enabledModels:\n  - \"gpt-6.1-sol\"\n"
                          "  - \"other/*\"\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertNotIn("gpt-6.1-sol", cfg)
        self.assertIn("other/*", cfg)

    def test_non_selector_blocks_are_untouched(self):
        """schema 里的非模型选择字段即使出现供应商名也原样保留（#2/#9）。"""
        for cfg in ("dev:\n  foo:\n    bar: hangzhale/gpt-6.1-sol\n",
                    "steering:\n  models:\n    - hangzhale/gpt-6.1-sol\n"
                    "    - other/m-other\n"):
            with self.subTest(cfg=cfg):
                self.write_models(BASE_MODELS)
                self.write_config(cfg)
                r = self.delete("hangzhale")
                self.assertTrue(r["ok"], r)
                self.assertEqual(self.config.read_text(encoding="utf-8"), cfg,
                                 "非选择器字段必须逐字节不变")

    def test_every_selector_field_is_cleared(self):
        """反过来：所有模型选择字段都要清，不依赖角色名白名单。"""
        cases = (
            "modelRoles:\n  anyName: hangzhale/gpt-6.1-sol\n",
            "task:\n  agentModelOverrides:\n    scout: hangzhale/gpt-6.1-sol\n",
            "modelPresets:\n  fast:\n    modelRoles:\n"
            "      smol: hangzhale/gpt-6.1-sol\n",
            "sharpshooter:\n  model: hangzhale/gpt-6.1-sol\n",
        )
        for cfg in cases:
            with self.subTest(cfg=cfg):
                self.write_models(BASE_MODELS)
                self.write_config(cfg)
                r = self.delete("hangzhale")
                self.assertTrue(r["ok"], r)
                self.assertNotIn("hangzhale",
                                 self.config.read_text(encoding="utf-8"))

    def test_prefix_similar_provider_is_not_a_reference(self):
        for value in ("hangzhale-tools/x", "my-hangzhale/x", "hangzhaleX/y"):
            with self.subTest(value=value):
                self.write_models(BASE_MODELS)
                self.write_config(f"modelRoles:\n  default: {value}\n")
                r = self.delete("hangzhale")
                self.assertTrue(r["ok"], r)
                cfg = self.config.read_text(encoding="utf-8")
                self.assertIn(value, cfg)

    def test_unsupported_reference_shape_is_reported_not_silently_kept(self):
        """无法精确安全改写的引用位置：返回 unsupported，不允许留下悬空引用。"""
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n"
                          "  default: {nested: hangzhale/gpt-6.1-sol}\n")
        r = self.delete("hangzhale")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "unsupported")
        self.assertEqual(self.models.read_text(encoding="utf-8"), BASE_MODELS)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertIn("hangzhale", cfg, "unsupported 不允许留下已清一半的状态")

class TestTransactionConsistency(CustomProviderBase):
    """引用检查与写入必须在同一把 config 锁内完成。"""

    def test_external_config_change_between_check_and_write_is_rejected(self):
        """非本进程改了 config（编辑器手改等）：写前快照比对必须拦下。"""
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n")
        before = self.models.read_bytes()
        real_refs = server._custom_references
        switched = []

        def racing(name, declared, lines=None):
            result = real_refs(name, declared, lines)
            if not switched:
                switched.append(True)
                self.write_config("modelRoles:\n  default: hangzhale/gpt-6.1-sol\n")
            return result

        with patch.object(server, "_custom_references", side_effect=racing):
            r = self.delete("hangzhale")
        self.assertFalse(r["ok"], "检查后磁盘已变，不能继续删除")
        self.assertEqual(r["code"], "stale")
        self.assertEqual(self.models.read_bytes(), before)

    def test_concurrent_role_save_cannot_interleave(self):
        """检查与写入都在 _config_lock 内：另一个 set_role 必须等它结束。"""
        import threading
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n")
        entered, finished = threading.Event(), []

        def slow_refs(name, declared, lines=None):
            entered.set()
            time.sleep(0.4)
            return [], [], self.config.read_bytes().decode("utf-8").splitlines(keepends=True)

        with patch.object(server, "_custom_references", side_effect=slow_refs):
            worker = threading.Thread(target=lambda: finished.append(
                server.delete_custom_provider(
                    {"provider": "hangzhale",
                     "expectDigest": digest(self.models.read_bytes()),
                     "expectConfigDigest": server._config_digest(
                         self.config.read_bytes())})))
            worker.start()
            self.assertTrue(entered.wait(5))
            # 主线程此时调 set_role：它要拿到 _config_lock 才能改文件
            t0 = time.monotonic()
            server.set_role("default", "hangzhale/gpt-6.1-sol")
            waited = time.monotonic() - t0
            worker.join(10)
        self.assertGreaterEqual(waited, 0.2, "set_role 必须等删除事务释放 config 锁")
        self.assertTrue(finished and finished[0]["ok"], finished)

    def test_no_cleanup_still_holds_the_config_lock(self):
        """没有 enabledModels 要清理时也必须持 config 锁做引用检查。"""
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n")
        held = []

        def refs(name, declared, lines=None):
            held.append(server._config_lock.locked())
            return [], [], self.config.read_bytes().decode("utf-8").splitlines(keepends=True)

        with patch.object(server, "_custom_references", side_effect=refs):
            r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertEqual(held, [True])


class TestBareModelIdsCleared(CustomProviderBase):
    """裸 ID 也是引用：清理时同样摘除，其他候选保留。"""

    def test_bare_id_in_model_roles_is_cleared(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: gpt-6.1-sol\n"
                          "  smol: other/m-other\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertNotIn("default:", cfg)
        self.assertIn("smol: other/m-other", cfg)

    def test_bare_id_in_agent_overrides_keeps_other_candidates(self):
        self.write_models(BASE_MODELS)
        self.write_config("task:\n  agentModelOverrides:\n"
                          "    scout: 'gpt-6.1-sol, other/m-other'\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        cfg = self.config.read_text(encoding="utf-8")
        self.assertNotIn("gpt-6.1-sol", cfg)
        self.assertIn("scout: other/m-other", cfg)

    def test_flow_model_roles_is_unsupported(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles: {default: hangzhale/gpt-6.1-sol, "
                          "smol: other/m-other}\n")
        before = self.config.read_bytes()
        p = self.provider("hangzhale")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "unsupported")
        r = self.delete("hangzhale")
        self.assertEqual(r["code"], "unsupported")
        self.assertEqual(self.config.read_bytes(), before)

    def test_bare_id_belonging_to_another_provider_is_untouched(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: m-other\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertIn("default: m-other", self.config.read_text(encoding="utf-8"))

class TestFlowProviders(CustomProviderBase):
    def test_flow_provider_with_model_overrides_cannot_be_deleted(self):
        self.write_models("providers:\n"
                          "  openai:\n"
                          "    baseUrl: http://o/v1\n"
                          "  anthropic: {modelOverrides: {gpt-6.1-sol: {contextWindow: 131072}}}\n")
        p = self.provider("anthropic")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "builtin_override")
        before = self.models.read_bytes()
        r = self.delete("anthropic")
        self.assertEqual(r["code"], "invalid")
        self.assertEqual(self.models.read_bytes(), before)

    def test_flow_provider_models_are_reported(self):
        self.write_models("providers:\n"
                          "  flow: {baseUrl: http://x/v1, models: [{id: fm-1}, {id: fm-2}]}\n")
        p = self.provider("flow")
        self.assertEqual(p["models"], ["fm-1", "fm-2"])
        self.assertTrue(p["deletable"])

    def test_unparseable_flow_models_is_unsupported_not_deletable(self):
        self.write_models("providers:\n"
                          "  odd: {baseUrl: http://x/v1, models: [{name: no-id}]}\n")
        p = self.provider("odd")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "unsupported")
        before = self.models.read_bytes()
        self.assertEqual(self.delete("odd")["code"], "unsupported")
        self.assertEqual(self.models.read_bytes(), before)


class TestYamlAnchors(CustomProviderBase):
    def test_alias_referencing_the_target_is_refused(self):
        self.write_models("providers:\n"
                          "  hangzhale: &gateway\n"
                          "    baseUrl: http://h/v1\n"
                          "  other: *gateway\n")
        before = self.models.read_bytes()
        r = self.delete("hangzhale")
        self.assertEqual(r["code"], "unsupported")
        self.assertIn("YAML", r["error"])
        self.assertEqual(self.models.read_bytes(), before)

    def test_merge_key_is_refused(self):
        self.write_models("providers:\n"
                          "  base: &b\n"
                          "    api: openai-completions\n"
                          "  hangzhale:\n"
                          "    baseUrl: http://h/v1\n"
                          "  other:\n"
                          "    <<: *b\n")
        self.assertEqual(self.delete("hangzhale")["code"], "unsupported")

    def test_anchor_inside_target_only_still_refused(self):
        """目标块内的锚点也可能被别处引用，本页不做依赖分析，一律拒绝。"""
        self.write_models("providers:\n"
                          "  hangzhale:\n"
                          "    baseUrl: &u http://h/v1\n"
                          "  other:\n"
                          "    baseUrl: http://o/v1\n")
        self.assertEqual(self.delete("hangzhale")["code"], "unsupported")


class TestCommaSeparatedSelectorsCleared(CustomProviderBase):
    """逗号候选列表：摘除属于本供应商的候选，其余不动。"""

    def test_comma_list_in_roles_keeps_other_candidates(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n"
                          "  default: 'gpt-6.1-sol,other/m-other'\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertIn("default: other/m-other",
                      self.config.read_text(encoding="utf-8"))

    def test_comma_list_in_agent_overrides_keeps_other_candidates(self):
        self.write_models(BASE_MODELS)
        self.write_config("task:\n  agentModelOverrides:\n"
                          "    scout: 'other/m-other, gpt-6.1-sol:high'\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertIn("scout: other/m-other",
                      self.config.read_text(encoding="utf-8"))

    def test_comma_list_without_target_is_untouched(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: 'other/m-other, zeta/m'\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertIn("default: 'other/m-other, zeta/m'",
                      self.config.read_text(encoding="utf-8"))

class TestNonAsciiAnchors(CustomProviderBase):
    """YAML 锚点名可以是任意非 ASCII 字符，不能只用 ASCII 正则。"""

    def test_cjk_anchor_and_alias_are_refused(self):
        self.write_models("providers:\n"
                          "  hangzhale: &网关\n"
                          "    baseUrl: http://h/v1\n"
                          "  other: *网关\n")
        before = self.models.read_bytes()
        p = self.provider("hangzhale")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "unsupported")
        r = self.delete("hangzhale")
        self.assertEqual(r["code"], "unsupported")
        self.assertEqual(self.models.read_bytes(), before)

    def test_cjk_anchor_shared_across_three_providers_is_refused(self):
        self.write_models("providers:\n"
                          "  base: &共用\n"
                          "    api: openai-completions\n"
                          "  hangzhale:\n"
                          "    baseUrl: http://h/v1\n"
                          "  other: *共用\n")
        self.assertEqual(self.delete("hangzhale")["code"], "unsupported")

    def test_quoted_ampersand_and_star_are_not_anchors(self):
        """URL 查询串与 markdown 强调里的 & / * 是普通文本，不能拦删除。"""
        self.write_models("providers:\n"
                          "  hangzhale:\n"
                          "    baseUrl: \"http://h/v1/?a=1&b=2\"\n"
                          "    name: \"**bold** and a * b\"\n"
                          "  other:\n"
                          "    baseUrl: http://o/v1\n")
        data = self.state()
        self.assertIsNone(data["error"], data["error"])
        self.assertTrue(self.provider("hangzhale", data)["deletable"])
        self.assertTrue(self.delete("hangzhale")["ok"])

    def test_bare_url_with_ampersand_is_not_an_anchor(self):
        self.write_models("providers:\n"
                          "  hangzhale:\n"
                          "    baseUrl: http://h/v1/?a=1&b=2\n"
                          "  other:\n"
                          "    baseUrl: http://o/v1\n")
        self.assertTrue(self.provider("hangzhale")["deletable"])
        self.assertTrue(self.delete("hangzhale")["ok"])


class TestStateMatchesDelete(CustomProviderBase):
    """状态接口与删除接口必须给出同一结论（页面不能显示可删却删不掉）。"""

    def test_ascii_anchor_blocks_in_state_and_delete(self):
        self.write_models("providers:\n"
                          "  hangzhale: &gw\n"
                          "    baseUrl: http://h/v1\n"
                          "  other: *gw\n")
        p = self.provider("hangzhale")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "unsupported")
        self.assertTrue(p["unsupported"], "原因必须回传给页面，按钮据此禁用并给出说明")
        self.assertEqual(self.delete("hangzhale")["code"], "unsupported")

    def test_every_non_deletable_entry_explains_the_same_reason_on_delete(self):
        """对每种不可删原因，删除接口的 code 必须与状态接口的 reasonCode 对应。"""
        cases = [
            ("builtin_override", "invalid", "ov",
             "providers:\n  ov:\n    modelOverrides:\n      - id: x\n"
             "  ok:\n    baseUrl: http://o/v1\n", None),
            ("unsupported", "unsupported", "anch",
             "providers:\n  anch: &a\n    baseUrl: http://a/v1\n  ok: *a\n", None),
            # 无法精确改写的引用形态：嵌套 map 里的 selector，无法逐字节摘
            ("unsupported", "unsupported", "hangzhale", BASE_MODELS,
             "modelRoles:\n  default: {nested: hangzhale/gpt-6.1-sol}\n"),
        ]
        for reason, code, name, text, cfg in cases:
            with self.subTest(reason=reason):
                self.write_models(text)
                if cfg:
                    self.write_config(cfg)
                else:
                    self.write_config("modelRoles:\n  default: other/m-other\n")
                p = self.provider(name)
                self.assertEqual(p["reasonCode"], reason, p)
                self.assertFalse(p["deletable"])
                self.assertEqual(self.delete(name)["code"], code)


class TestConfigUnreadable(CustomProviderBase):
    def test_unreadable_config_blocks_deletion_and_marks_unavailable(self):
        self.write_models(BASE_MODELS)
        real_read = Path.read_bytes

        def deny_config(self_path, *a, **kw):
            if Path(self_path) == self.config:
                raise PermissionError("denied")
            return real_read(self_path, *a, **kw)

        calls = []

        def refs(*a, **kw):
            calls.append(True)
            return server._custom_references(*a, **kw)

        # 摘要在 patch 生效前算好（patch 期间连算摘要的读都会失败）
        cfg_digest = server._config_digest(self.config.read_bytes())
        with patch.object(Path, "read_bytes", autospec=True, side_effect=deny_config):
            data = self.state()
            p = self.provider("hangzhale", data)
            self.assertFalse(p["deletable"], "读不到 config 不能当成「没有引用」")
            self.assertEqual(p["reasonCode"], "unavailable")
            self.assertEqual(calls, [])
            before = self.models.read_bytes()
            r = self.delete("hangzhale", expectConfigDigest=cfg_digest)
            self.assertEqual(r["code"], "unavailable")
            self.assertIn("config.yml", r["error"])
            self.assertEqual(self.models.read_bytes(), before)

    def test_missing_config_file_means_no_references(self):
        self.write_models(BASE_MODELS)
        self.config.unlink()
        self.assertTrue(self.provider("hangzhale")["deletable"])
        self.assertTrue(self.delete("hangzhale")["ok"])


class TestHttpRoute(CustomProviderBase):
    @classmethod
    def setUpClass(cls):
        cls._orig_port = server.PORT
        cls.srv = None
        for port in range(8900, 8940):
            try:
                from http.server import ThreadingHTTPServer
                cls.srv = ThreadingHTTPServer(("127.0.0.1", port), server.Handler)
            except OSError:
                continue
            server.PORT = port
            break
        if cls.srv is None:
            raise unittest.SkipTest("no free port")
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        server.PORT = cls._orig_port

    def setUp(self):
        super().setUp()
        self.write_models(BASE_MODELS)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path, headers=None):
        req = urllib.request.Request(self.url(path), headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def cfg_digest(self):
        raw = self.config.read_bytes() if self.config.exists() else None
        return server._config_digest(raw)

    def post(self, path, body, headers=None):
        if path == "/api/custom-providers/delete" and \
                "expectConfigDigest" not in body:
            body = {**body, "expectConfigDigest": self.cfg_digest()}
        req = urllib.request.Request(
            self.url(path), data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_get_lists_provider_without_secrets(self):
        status, body = self.get("/api/custom-providers")
        self.assertEqual(status, 200)
        names = [p["name"] for p in body["providers"]]
        self.assertIn("hangzhale", names)
        self.assertNotIn("SECRET", json.dumps(body))

    def test_cross_site_post_is_rejected_before_write(self):
        before = self.models.read_bytes()
        status, _ = self.post("/api/custom-providers/delete",
                              {"provider": "hangzhale",
                               "expectDigest": digest(before)},
                              {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.models.read_bytes(), before)

    def test_missing_digest_is_400_and_stale_is_409(self):
        status, body = self.post("/api/custom-providers/delete",
                                 {"provider": "hangzhale"})
        self.assertEqual(status, 400)
        self.assertIn("摘要", body.get("error", ""))
        # 只给 models 摘要、缺 config 摘要同样必须 400
        req = urllib.request.Request(
            self.url("/api/custom-providers/delete"),
            data=json.dumps({"provider": "hangzhale",
                             "expectDigest": digest(self.models.read_bytes())}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("缺少 config 摘要必须被拒绝")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            self.assertIn("config.yml", json.loads(e.read().decode())["error"])
        status, body = self.post("/api/custom-providers/delete",
                                 {"provider": "hangzhale",
                                  "expectDigest": "0" * 64})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "stale")

    def test_oversized_body_is_413_without_reading_it(self):
        before = self.models.read_bytes()
        req = urllib.request.Request(
            self.url("/api/custom-providers/delete"),
            data=b"{" + b" " * (server.Handler.MAX_BODY + 10) + b"}",
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=15)
            self.fail("oversized body must be rejected")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 413)
            self.assertEqual(e.headers.get("Connection"), "close")
        self.assertEqual(self.models.read_bytes(), before)

    def test_short_body_with_lying_content_length_does_not_hang(self):
        """谎报 Content-Length 却不发完：必须在超时后返回而不是挂住线程。"""
        import socket
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=15)
        try:
            sock.sendall(
                b"POST /api/custom-providers/delete HTTP/1.1\r\n"
                + f"Host: 127.0.0.1:{self.port}\r\n".encode()
                + b"Content-Type: application/json\r\n"
                + b"Origin: http://evil.example\r\n"
                + b"Content-Length: 100\r\n\r\n"
                + b"{partial")
            t0 = time.monotonic()
            sock.settimeout(30)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" in data and b"}" in data.split(b"\r\n\r\n", 1)[1]:
                    break
            elapsed = time.monotonic() - t0
        finally:
            sock.close()
        self.assertLess(elapsed, 25, "拒绝路径不能无限期挂在读取请求体上")
        self.assertIn(b"413", data.split(b"\r\n", 1)[0])

    def test_cross_site_reject_still_returns_403_with_body_sent(self):
        before = self.models.read_bytes()
        status, _ = self.post("/api/custom-providers/delete",
                              {"provider": "hangzhale",
                               "expectDigest": digest(before)},
                              {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.models.read_bytes(), before)

    def test_partial_rollback_is_500_and_names_unrestored_files(self):
        real = server._atomic_write

        def flaky(path, data):
            if path == self.config:
                raise OSError("disk full")
            if path == self.models and data == BASE_MODELS.encode("utf-8"):
                raise OSError("read-only")
            return real(path, data)

        self.write_config("modelRoles:\n  default: other/m-other\n"
                          "enabledModels:\n  - \"other/*\"\n"
                          "  - \"hangzhale/gpt-6.1-sol\"\n")
        with patch.object(server, "_atomic_write", side_effect=flaky):
            status, body = self.post(
                "/api/custom-providers/delete",
                {"provider": "hangzhale",
                 "expectDigest": digest(self.models.read_bytes())})
        self.assertEqual(status, 500)
        self.assertEqual(body["code"], "partial")
        self.assertTrue(body["unrestored"])
        self.assertTrue(body["backups"])

    def test_successful_delete_is_200(self):
        status, body = self.post(
            "/api/custom-providers/delete",
            {"provider": "hangzhale",
             "expectDigest": digest(self.models.read_bytes())})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        # 只删定义块；用户自己写的说明注释不在删除范围内
        text = self.models.read_text(encoding="utf-8")
        self.assertNotIn("  hangzhale:\n", text)
        self.assertIn("  other:", text)


class TestReviewFixes(CustomProviderBase):
    """独立审查 11 项：逐项回归。"""

    # ---- #1 空容器必须带回原行尾 ----
    def test_emptied_role_block_keeps_newline(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: hangzhale/gpt-6.1-sol\ntheme: dark\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.config.read_text(encoding="utf-8")
        import yaml
        self.assertEqual(yaml.safe_load(out), {"modelRoles": {}, "theme": "dark"},
                         "空容器后必须还有换行，否则会拼成 modelRoles: {}theme:")
        self.assertIn("modelRoles: {}\n", out)

    def test_emptied_chain_block_keeps_newline(self):
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n    smol:\n"
                          "      - hangzhale/gpt-6.1-sol\ntheme: dark\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        import yaml
        self.assertEqual(yaml.safe_load(self.config.read_text(encoding="utf-8")),
                         {"retry": {"fallbackChains": {}}, "theme": "dark"})

    def test_crlf_empty_container_keeps_crlf(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\r\n  default: hangzhale/gpt-6.1-sol\r\ntheme: dark\r\n")
        self.assertTrue(self.delete("hangzhale")["ok"])
        out = self.config.read_bytes()
        self.assertNotIn(b"\n", out.replace(b"\r\n", b""), "不允许出现裸 LF")

    # ---- #2 只改模型选择字段：路径/策略原样保留 ----
    def test_non_selector_fields_are_never_touched(self):
        self.write_models(BASE_MODELS)
        cfg = ("modelRoles:\n  default: hangzhale/gpt-6.1-sol\n"
               "shellMinimizer:\n  settingsPath: hangzhale/rules.yml\n"
               "bash:\n  patterns:\n    - match: \"*\"\n      approval: allow\n")
        self.write_config(cfg)
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.config.read_text(encoding="utf-8")
        self.assertIn("settingsPath: hangzhale/rules.yml", out,
                      "路径字段不是模型引用，必须原样保留")
        self.assertIn('match: "*"', out, "bash 策略模式不是模型引用")
        self.assertNotIn("default: hangzhale", out)

    def test_url_field_with_provider_like_text_is_untouched(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n"
                          "webhook:\n  url: https://example.test/hangzhale/x\n")
        self.assertTrue(self.delete("hangzhale")["ok"])
        self.assertIn("https://example.test/hangzhale/x",
                      self.config.read_text(encoding="utf-8"))

    # ---- #3 确认清单绑定 config 快照 ----
    def test_state_exposes_config_digest(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: hangzhale/gpt-6.1-sol\n")
        st = self.state()
        self.assertEqual(st["configDigest"], digest(self.config.read_bytes()))

    def test_delete_rejects_stale_config_digest(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: hangzhale/gpt-6.1-sol\n")
        st = self.state()
        self.write_config("modelRoles:\n  default: hangzhale/gpt-6.1-sol\n"
                          "smol: hangzhale/gpt-6.1-sol\n")
        before = {self.models: self.models.read_bytes(),
                  self.config: self.config.read_bytes()}
        r = self.delete("hangzhale", expectConfigDigest=st["configDigest"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "stale")
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data, "stale 时绝不能写")

    def test_delete_without_config_digest_still_works(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n")
        self.assertTrue(self.delete("hangzhale")["ok"])

    # ---- #4 剩余值的引号必须安全 ----
    def test_at_alias_after_removal_keeps_quotes(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: 'hangzhale/gpt-6.1-sol, @smol'\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.config.read_text(encoding="utf-8")
        import yaml
        self.assertEqual(yaml.safe_load(out)["modelRoles"]["default"], "@smol")
        self.assertRegex(out, r'default: ["\']@smol["\']',
                         "@ 开头的标量必须加引号（单双引号均可）")

    def test_plain_remaining_value_stays_unquoted(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: 'hangzhale/gpt-6.1-sol, other/m-other'\n")
        self.assertTrue(self.delete("hangzhale")["ok"])
        self.assertIn("default: other/m-other",
                      self.config.read_text(encoding="utf-8"))

    # ---- #5 flow 容器逐候选摘除 ----
    def test_flow_map_role_is_unsupported(self):
        self.write_models(BASE_MODELS)
        self.write_config('modelRoles: {default: "hangzhale/gpt-6.1-sol, other/m-other", '
                          'smol: other/m-other}\n')
        before = self.config.read_bytes()
        r = self.delete("hangzhale")
        self.assertEqual(r["code"], "unsupported")
        self.assertEqual(self.config.read_bytes(), before)

    def test_flow_list_chain_is_unsupported(self):
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n"
                          "    smol: ['hangzhale/gpt-6.1-sol, other/m-other', zeta/m]\n")
        before = self.config.read_bytes()
        r = self.delete("hangzhale")
        self.assertEqual(r["code"], "unsupported")
        self.assertEqual(self.config.read_bytes(), before)

    # ---- #6 块列表项改写 ----
    def test_block_list_item_with_remaining_candidate_is_rewritten(self):
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n    smol:\n"
                          "      - 'hangzhale/gpt-6.1-sol, other/m-other'\n"
                          "      - zeta/m\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        import yaml
        chain = yaml.safe_load(self.config.read_text(encoding="utf-8"))["retry"]["fallbackChains"]["smol"]
        self.assertEqual(chain, ["other/m-other", "zeta/m"])

    # ---- #7 裸 ID 只在目标唯一提供时清理 ----
    def test_bare_id_shared_with_other_provider_is_kept(self):
        self.write_models("providers:\n"
                          "  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: gpt-4o-mini\n"
                          "  rival:\n    baseUrl: http://r/v1\n"
                          "    models:\n      - id: gpt-4o-mini\n")
        self.catalog = [
            {"selector": "hangzhale/gpt-4o-mini", "provider": "hangzhale", "kind": "chat"},
            {"selector": "rival/gpt-4o-mini", "provider": "rival", "kind": "chat"},
        ]
        self.write_config("modelRoles:\n  default: gpt-4o-mini\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertIn("default: gpt-4o-mini",
                      self.config.read_text(encoding="utf-8"),
                      "同名模型另有提供者时，裸 ID 引用必须保留")

    def test_bare_id_only_provided_by_target_is_cleared(self):
        self.write_models("providers:\n"
                          "  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: solo-model\n"
                          "  rival:\n    baseUrl: http://r/v1\n"
                          "    models:\n      - id: r-model\n")
        self.catalog = [
            {"selector": "hangzhale/solo-model", "provider": "hangzhale", "kind": "chat"},
            {"selector": "rival/r-model", "provider": "rival", "kind": "chat"},
        ]
        self.write_config("modelRoles:\n  default: solo-model\n  smol: other/m-other\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.config.read_text(encoding="utf-8")
        self.assertNotIn("default:", out, "唯一提供者的裸 ID 应随定义清除")
        self.assertIn("smol: other/m-other", out)

    # ---- #8 flow 写法的 enabledModels 摘空要警示 ----
    def test_flow_enabled_models_is_unsupported(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n"
                          'enabledModels: ["hangzhale/*"]\n')
        p = self.provider("hangzhale")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "unsupported")
        r = self.delete("hangzhale")
        self.assertEqual(r["code"], "unsupported")

    def test_flow_enabled_models_is_unsupported_regardless_of_mix(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n"
                          'enabledModels: ["hangzhale/*", "other/*"]\n')
        p = self.provider("hangzhale")
        self.assertEqual(p["reasonCode"], "unsupported")

    # ---- #9 无关的嵌套 flow 不应禁用删除 ----
    def test_unrelated_nested_flow_does_not_block(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: hangzhale/gpt-6.1-sol\n"
                          "bash:\n  patterns:\n"
                          "    - match: \"git status\"\n      approval: allow\n"
                          "steering:\n  models: [{match: x, y: z}]\n")
        p = self.provider("hangzhale")
        self.assertTrue(p["deletable"], p)
        self.assertEqual(p["unsupported"], [])
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.config.read_text(encoding="utf-8")
        self.assertIn('match: "git status"', out)
        self.assertIn("steering:", out)

    def test_nested_flow_that_does_reference_target_is_unsupported(self):
        """选择器字段里的嵌套容器无法逐字节摘除 → unsupported（不是静默保留）。"""
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: {nested: hangzhale/gpt-6.1-sol}\n")
        p = self.provider("hangzhale")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "unsupported")
        self.assertEqual(self.delete("hangzhale")["code"], "unsupported")

    # ---- #10 fallbackChains 的选择器键 ----
    def test_fallback_chain_selector_key_is_removed(self):
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n"
                          "    hangzhale/gpt-6.1-sol:\n"
                          "      - other/m-other\n"
                          "    smol:\n"
                          "      - zeta/m\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        import yaml
        chains = yaml.safe_load(self.config.read_text(encoding="utf-8"))["retry"]["fallbackChains"]
        self.assertNotIn("hangzhale/gpt-6.1-sol", chains, "指向已删模型的链键必须删除")
        self.assertEqual(chains["smol"], ["zeta/m"], "角色名键不受影响")

    def test_role_named_chain_key_is_kept(self):
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n    smol:\n"
                          "      - hangzhale/gpt-6.1-sol\n")
        self.assertTrue(self.delete("hangzhale")["ok"])
        import yaml
        chains = yaml.safe_load(self.config.read_text(encoding="utf-8"))["retry"]["fallbackChains"]
        self.assertEqual(chains, {}, "链变空后留空映射")

    # ---- #11 改写保留行尾注释与原格式 ----
    def test_trailing_comment_survives_partial_clear(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n"
                          "  default: 'hangzhale/gpt-6.1-sol, other/m-other'  # 保留的说明\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.config.read_text(encoding="utf-8")
        self.assertIn("# 保留的说明", out, "改写值区间时不能丢注释")
        self.assertIn("other/m-other", out)

    def test_flow_with_comment_is_unsupported_and_untouched(self):
        self.write_models(BASE_MODELS)
        cfg = ('modelRoles: {default: "hangzhale/gpt-6.1-sol, other/m-other"}'
               '  # flow 说明\n')
        self.write_config(cfg)
        r = self.delete("hangzhale")
        self.assertEqual(r["code"], "unsupported")
        self.assertEqual(self.config.read_text(encoding="utf-8"), cfg)

    def test_comment_kept_when_reference_line_is_removed(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: hangzhale/gpt-6.1-sol  # 这是主模型\n"
                          "  smol: other/m-other\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.config.read_text(encoding="utf-8")
        self.assertIn("# 这是主模型", out, "删除引用行时不能连注释一起丢")
        self.assertNotIn("hangzhale", out)




class TestCatalogUnavailableFailsClosed(CustomProviderBase):
    """目录不可用时不能靠猜裸 ID 归属：必须 fail-closed。"""

    def setUp(self):
        super().setUp()
        self.write_models("providers:\n"
                          "  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: solo-model\n"
                          "  other:\n    baseUrl: http://o/v1\n")
        self.write_config("modelRoles:\n  default: solo-model\n")

    def _break_catalog(self):
        def boom():
            raise server.CatalogUnavailable("无法读取 omp 模型目录：omp 不可用")
        server.full_catalog = boom

    def test_bare_id_with_unavailable_catalog_is_not_cleared(self):
        self._break_catalog()
        before = {self.models: self.models.read_bytes(),
                  self.config: self.config.read_bytes()}
        r = self.delete("hangzhale")
        self.assertFalse(r["ok"], "目录不可用时不能凭猜清除裸 ID")
        self.assertEqual(r["code"], "unavailable")
        self.assertIn("模型目录", r["error"])
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data, "拒绝路径必须零写入")

    def test_state_marks_unavailable_when_catalog_fails(self):
        self._break_catalog()
        p = self.provider("hangzhale")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "unavailable")
        self.assertTrue(any("模型目录" in x for x in p["unsupported"]), p)

    def test_empty_catalog_also_fails_closed(self):
        """目录为空同样无法判定归属（omp 掉线/未登录），必须拒绝。"""
        def empty():
            raise server.CatalogUnavailable("无法读取 omp 模型目录：目录为空")
        server.full_catalog = empty
        r = self.delete("hangzhale")
        self.assertEqual(r["code"], "unavailable")
        p = self.provider("hangzhale")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "unavailable")

    def test_no_bare_id_reference_still_works_without_catalog(self):
        """没有裸 ID 引用时不需要目录，删除照常（不因目录失败而全面禁用）。"""
        self.write_config("modelRoles:\n  default: other/m-other\n")
        self._break_catalog()
        self.assertTrue(self.provider("hangzhale")["deletable"])
        self.assertTrue(self.delete("hangzhale")["ok"])

    def test_provider_prefixed_reference_works_without_catalog(self):
        self.write_config("modelRoles:\n  default: hangzhale/solo-model\n")
        self._break_catalog()
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertNotIn("default:", self.config.read_text(encoding="utf-8"))




class TestReviewRound2(CustomProviderBase):
    """复审第二轮：5 处残留缺陷的回归。"""

    # ---- A: drop_block 后不再为子行生成 op（否则消费端 None.rstrip）----
    def test_chain_key_drop_does_not_double_visit_children(self):
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n"
                          "    hangzhale/gpt-6.1-sol:\n"
                          "      - hangzhale/gpt-6.1-sol\n"
                          "    smol:\n      - zeta/m\n")
        r = self.delete("hangzhale")          # 不允许 AttributeError
        self.assertTrue(r["ok"], r)
        import yaml
        chains = yaml.safe_load(self.config.read_text(encoding="utf-8"))["retry"]["fallbackChains"]
        self.assertEqual(chains, {"smol": ["zeta/m"]})

    def test_chain_key_drop_cleared_has_no_child_ops(self):
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n"
                          "    hangzhale/gpt-6.1-sol:\n"
                          "      - hangzhale/gpt-6.1-sol\n")
        cleared, _u, _l = server._custom_references(
            "hangzhale", ["gpt-6.1-sol"],
            "retry:\n  fallbackChains:\n    hangzhale/gpt-6.1-sol:\n"
            "      - hangzhale/gpt-6.1-sol\n".splitlines(keepends=True))
        self.assertEqual(len(cleared), 1, cleared)
        self.assertTrue(cleared[0].get("drop_block"))

    # ---- B: 块列表项部分改写保留行尾注释 ----
    def test_block_item_partial_clear_keeps_comment(self):
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n    smol:\n"
                          "      - 'hangzhale/gpt-6.1-sol, other/m-other'  # 链说明\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.config.read_text(encoding="utf-8")
        self.assertIn("# 链说明", out, "块列表项改写也必须保留注释")
        self.assertIn("- other/m-other  # 链说明", out)

    # ---- C: modelPresets 只认 modelRoles，不碰 defaultThinkingLevel ----
    def test_preset_non_selector_field_is_untouched(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelPresets:\n  fast:\n"
                          "    defaultThinkingLevel: gpt-6.1-sol\n"
                          "    modelRoles:\n      smol: gpt-6.1-sol\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.config.read_text(encoding="utf-8")
        self.assertIn("defaultThinkingLevel: gpt-6.1-sol", out,
                      "预设里的非选择器字段必须原样保留")
        self.assertIn("modelRoles: {}", out)

    # ---- D: config 摘要必填，且 config 不存在也有确定摘要 ----
    def test_delete_requires_config_digest(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n")
        r = server.delete_custom_provider(
            {"provider": "hangzhale",
             "expectDigest": digest(self.models.read_bytes())})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "invalid")
        self.assertIn("config.yml", r["error"])

    def test_absent_config_has_stable_digest_and_binds(self):
        self.write_models(BASE_MODELS)
        self.config.unlink()
        st = self.state()
        self.assertEqual(st["configDigest"], server._config_digest(None))
        # 页面确认后、提交前外部新建了 config → 摘要不再匹配
        self.write_config("modelRoles:\n  default: hangzhale/gpt-6.1-sol\n")
        r = self.delete("hangzhale", expectConfigDigest=st["configDigest"])
        self.assertEqual(r["code"], "stale")
        self.assertIn("hangzhale", self.config.read_text(encoding="utf-8"))

    def test_verified_snapshot_is_reused_not_reread(self):
        """摘要核验后 config 又被改：不能拿新内容去算清理方案。"""
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n")
        st = self.state()
        real_read = Path.read_bytes
        calls = []

        def counting(self_path, *a, **kw):
            if Path(self_path) == self.config:
                calls.append(1)
            return real_read(self_path, *a, **kw)

        # 第一次读用于摘要核验；若实现又读一次，说明快照没有被复用
        with patch.object(Path, "read_bytes", autospec=True, side_effect=counting):
            r = self.delete("hangzhale", expectConfigDigest=st["configDigest"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(calls), 2,
                         "应只有「核验」与「写前复核」两次读取，清理方案用同一份快照")

    # ---- E: 含注释的 enabledModels 也要能判空 ----
    def test_commented_enabled_block_reports_emptied(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles:\n  default: other/m-other\n"
                          "enabledModels:\n  # 说明\n  - \"hangzhale/gpt-6.1-sol\"\n")
        p = self.provider("hangzhale")
        self.assertTrue(p["deletable"], p)
        self.assertTrue(p["enabledEmptied"],
                        "块内注释不能导致判空失效")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["enabledEmptied"])

    # ---- F: 目录失败必须 fail-closed（含空结果） ----
    def test_catalog_unavailable_blocks_bare_id_cleanup(self):
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: solo-model\n")
        self.write_config("modelRoles:\n  default: solo-model\n")
        for exc in (server.CatalogUnavailable("无法读取 omp 模型目录：boom"),
                    server.CatalogUnavailable("无法读取 omp 模型目录：目录为空")):
            with self.subTest(exc=str(exc)):
                def boom(e=exc):
                    raise e
                server.full_catalog = boom
                before = (self.models.read_bytes(), self.config.read_bytes())
                p = self.provider("hangzhale")
                self.assertFalse(p["deletable"])
                self.assertEqual(p["reasonCode"], "unavailable")
                self.assertTrue(any("模型目录" in x for x in p["unsupported"]), p)
                r = self.delete("hangzhale")
                self.assertEqual(r["code"], "unavailable")
                self.assertIn("模型目录", r["error"])
                self.assertEqual((self.models.read_bytes(),
                                  self.config.read_bytes()), before)

    def test_non_chat_owner_prevents_bare_id_cleanup(self):
        """image/tts 等非 chat 模型也算提供者（list_models 只留 chat）。"""
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: shared-img\n")
        self.catalog = [
            {"selector": "hangzhale/shared-img", "provider": "hangzhale", "kind": "image"},
            {"selector": "rival/shared-img", "provider": "rival", "kind": "image"},
        ]
        self.write_config("modelRoles:\n  default: shared-img\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertIn("default: shared-img",
                      self.config.read_text(encoding="utf-8"),
                      "非 chat 的共享裸 ID 也必须保留")




class TestReviewRound3(CustomProviderBase):
    """复审第三轮：4 处残留缺陷。"""

    def _shared_catalog(self):
        self.catalog = [
            {"selector": "hangzhale/solo-model", "provider": "hangzhale", "kind": "chat"},
            {"selector": "rival/solo-model", "provider": "rival", "kind": "chat"},
        ]

    # ---- 1: _needs_bare_owners 必须解析 flow，否则 fail-closed 被绕过 ----
    def test_flow_role_bare_id_is_refused_without_catalog(self):
        for cfg in ('modelRoles: {default: solo-model}\n',
                    'modelRoles:\n  smol: [solo-model]\n'):
            with self.subTest(cfg=cfg):
                self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                                  "    models:\n      - id: solo-model\n")
                self.write_config(cfg)
                p = self.provider("hangzhale")
                self.assertFalse(p["deletable"], cfg)
                self.assertEqual(p["reasonCode"], "unsupported")

    def test_flow_bare_id_is_refused_regardless_of_catalog(self):
        """flow 里的裸 ID 由保守文本规则拦下，不依赖目录。"""
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: solo-model\n")
        self.write_config("modelRoles: {default: solo-model}\n")
        before = (self.models.read_bytes(), self.config.read_bytes())
        p = self.provider("hangzhale")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "unsupported")
        r = self.delete("hangzhale")
        self.assertEqual(r["code"], "unsupported")
        self.assertEqual((self.models.read_bytes(), self.config.read_bytes()), before)

    def test_flow_bare_id_is_unsupported_even_when_shared(self):
        self._shared_catalog()
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: solo-model\n")
        self.write_config("modelRoles: {default: solo-model}\n")
        p = self.provider("hangzhale")
        self.assertEqual(p["reasonCode"], "unsupported")
        self.assertIn("solo-model", self.config.read_text(encoding="utf-8"))

    # ---- 2: 祖先 flow 里的选择器 ----
    def test_ancestor_flow_with_target_is_unsupported(self):
        cases = (
            'task: {agentModelOverrides: {scout: hangzhale/solo-model}}\n',
            'retry: {fallbackChains: {default: [hangzhale/solo-model]}}\n',
            'modelPresets: {fast: {modelRoles: {smol: hangzhale/solo-model}}}\n',
            'modelRoles: {default: hangzhale/solo-model}\n',
        )
        for cfg in cases:
            with self.subTest(cfg=cfg):
                self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                                  "    models:\n      - id: solo-model\n")
                self.write_config(cfg)
                before = self.models.read_bytes()
                p = self.provider("hangzhale")
                self.assertFalse(p["deletable"],
                                 "祖先 flow 里确有目标引用时不能报可删")
                self.assertEqual(p["reasonCode"], "unsupported")
                r = self.delete("hangzhale")
                self.assertEqual(r["code"], "unsupported")
                self.assertEqual(self.models.read_bytes(), before)
                self.assertIn("hangzhale", self.config.read_text(encoding="utf-8"))

    def test_ancestor_flow_without_target_stays_deletable(self):
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: solo-model\n")
        self.write_config('task: {agentModelOverrides: {scout: other/m-other}}\n'
                          "modelRoles:\n  default: other/m-other\n")
        self.assertTrue(self.provider("hangzhale")["deletable"])
        self.assertTrue(self.delete("hangzhale")["ok"])

    # ---- 3: flow enabledModels 带行尾注释也要判空 ----
    def test_flow_enabled_models_with_target_is_unsupported(self):
        """flow 写法的 enabledModels 不再自动改写：含目标引用即 unsupported。"""
        self.write_models(BASE_MODELS)
        self.write_config('modelRoles:\n  default: other/m-other\n'
                          'enabledModels: ["hangzhale/*"]  # 说明\n')
        p = self.provider("hangzhale")
        self.assertFalse(p["deletable"])
        self.assertEqual(p["reasonCode"], "unsupported")
        before = self.config.read_bytes()
        r = self.delete("hangzhale")
        self.assertEqual(r["code"], "unsupported")
        self.assertEqual(self.config.read_bytes(), before)

    def test_flow_enabled_models_without_target_is_ignored(self):
        self.write_models(BASE_MODELS)
        self.write_config('modelRoles:\n  default: other/m-other\n'
                          'enabledModels: ["other/*"]  # 说明\n')
        self.assertTrue(self.provider("hangzhale")["deletable"])
        self.assertTrue(self.delete("hangzhale")["ok"])

    # ---- 4: dropped_at 不能被注释行复位 ----
    def test_comment_inside_dropped_block_does_not_reopen_it(self):
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n"
                          "    hangzhale/gpt-6.1-sol:\n"
                          "      - other/m-other\n"
                          "# 顶格说明\n"
                          "      - hangzhale/gpt-6.1-sol\n")
        r = self.delete("hangzhale")          # 不允许 AttributeError
        self.assertTrue(r["ok"], r)
        import yaml
        data = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(data["retry"]["fallbackChains"], {})
        self.assertIn("# 顶格说明", self.config.read_text(encoding="utf-8"))




class TestReviewRound4(CustomProviderBase):
    """复审第四轮：flow leaf 判定、递归祖先、链键裸 ID 默认值。"""

    # ---- 1: _mentions 必须用 flow leaf 判定 ----
    def test_flow_leaf_bare_id_unique_to_target_is_unsupported(self):
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: solo-model\n")
        self.catalog = [{"selector": "hangzhale/solo-model",
                         "provider": "hangzhale", "kind": "chat"}]
        for cfg in ("modelRoles: {default: solo-model}\n",
                    "enabledModels: ['solo-model']\n"):
            with self.subTest(cfg=cfg):
                self.write_config(cfg)
                before = (self.models.read_bytes(), self.config.read_bytes())
                p = self.provider("hangzhale")
                self.assertFalse(p["deletable"],
                                 "flow 里的裸 ID 同样构成引用")
                self.assertEqual(p["reasonCode"], "unsupported")
                r = self.delete("hangzhale")
                self.assertEqual(r["code"], "unsupported")
                self.assertEqual((self.models.read_bytes(),
                                  self.config.read_bytes()), before)

    def test_flow_leaf_provider_prefix_is_unsupported(self):
        self.write_models(BASE_MODELS)
        self.write_config("modelRoles: {default: hangzhale/gpt-6.1-sol}\n")
        self.assertEqual(self.provider("hangzhale")["reasonCode"], "unsupported")

    # ---- 2: 递归识别祖先 flow（含中间层）----
    def test_middle_ancestor_flow_is_unsupported(self):
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: m\n")
        cases = (
            "modelPresets:\n  fast: {modelRoles: {smol: hangzhale/m}}\n",
            "task:\n  agentModelOverrides: {scout: hangzhale/m}\n",
            "retry:\n  fallbackChains: {default: [hangzhale/m]}\n",
        )
        for cfg in cases:
            with self.subTest(cfg=cfg):
                self.write_config(cfg)
                before = (self.models.read_bytes(), self.config.read_bytes())
                p = self.provider("hangzhale")
                self.assertFalse(p["deletable"], cfg)
                self.assertEqual(p["reasonCode"], "unsupported")
                r = self.delete("hangzhale")
                self.assertEqual(r["code"], "unsupported")
                self.assertEqual((self.models.read_bytes(),
                                  self.config.read_bytes()), before)

    def test_middle_ancestor_flow_without_target_is_deletable(self):
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: m\n")
        self.write_config("modelPresets:\n  fast: {modelRoles: {smol: other/m-other}}\n"
                          "modelRoles:\n  default: other/m-other\n")
        self.assertTrue(self.provider("hangzhale")["deletable"])
        self.assertTrue(self.delete("hangzhale")["ok"])

    # ---- 3: 缺失归属不得默认视为目标；链键只认 provider/model ----
    def test_missing_owner_record_is_never_treated_as_target(self):
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: smol\n")
        # 目录里没有 smol 这个 ID（只有别的供应商）
        self.catalog = [{"selector": "other/x", "provider": "other", "kind": "chat"}]
        self.write_config("modelRoles:\n  default: smol\n")
        p = self.provider("hangzhale")
        self.assertTrue(p["deletable"], "无归属记录不命中，引用保留")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertIn("default: smol", self.config.read_text(encoding="utf-8"))

    def test_shared_id_resolved_by_other_provider_is_kept(self):
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: smol\n")
        self.catalog = [{"selector": "other/smol", "provider": "other", "kind": "chat"}]
        self.write_config("modelRoles:\n  default: smol\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        self.assertIn("default: smol", self.config.read_text(encoding="utf-8"))

    def test_role_named_chain_key_is_never_treated_as_model(self):
        """`smol` 是角色链键，即使与某个模型 id 同名也不能当模型引用删掉。"""
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: smol\n")
        self.catalog = [{"selector": "other/smol", "provider": "other", "kind": "chat"}]
        self.write_config("retry:\n  fallbackChains:\n    smol:\n      - other/x\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        import yaml
        chains = yaml.safe_load(self.config.read_text(encoding="utf-8"))["retry"]["fallbackChains"]
        self.assertEqual(chains, {"smol": ["other/x"]},
                         "角色名键必须保留")

    def test_provider_prefixed_chain_key_is_removed(self):
        self.write_models(BASE_MODELS)
        self.write_config("retry:\n  fallbackChains:\n"
                          "    hangzhale/gpt-6.1-sol:\n      - other/x\n"
                          "    smol:\n      - other/x\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        import yaml
        chains = yaml.safe_load(self.config.read_text(encoding="utf-8"))["retry"]["fallbackChains"]
        self.assertEqual(chains, {"smol": ["other/x"]})




class TestMultilineFlow(CustomProviderBase):
    """多行 flow 容器：整段禁改，区域内命中目标即 unsupported。"""

    def setUp(self):
        super().setUp()
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: m\n")

    def _assert_refused(self, cfg):
        self.write_config(cfg)
        before = (self.models.read_bytes(), self.config.read_bytes())
        p = self.provider("hangzhale")
        self.assertFalse(p["deletable"], cfg)
        self.assertEqual(p["reasonCode"], "unsupported", p)
        self.assertTrue(any("flow 写法" in x for x in p["unsupported"]), p)
        r = self.delete("hangzhale")
        self.assertEqual(r["code"], "unsupported")
        self.assertEqual((self.models.read_bytes(), self.config.read_bytes()), before,
                         "拒绝路径必须零写入")

    def test_multiline_list_flow_is_refused(self):
        self._assert_refused("retry:\n  fallbackChains:\n    smol: [\n"
                             "      hangzhale/m,\n      other/n\n    ]\n")

    def test_multiline_map_flow_is_refused(self):
        self._assert_refused("modelRoles: {\n  default: hangzhale/m,\n"
                             "  smol: other/n\n}\n")

    def test_multiline_flow_reason_names_container_start(self):
        self.write_config("modelRoles: {\n  default: hangzhale/m,\n}\n")
        p = self.provider("hangzhale")
        self.assertTrue(any(x.startswith("modelRoles")
                            for x in p["unsupported"]), p)

    def test_multiline_flow_without_target_is_deletable(self):
        cfg = ("retry:\n  fallbackChains:\n    smol: [\n"
               "      other/n,\n      zeta/k\n    ]\n"
               "modelRoles:\n  default: other/n\n")
        self.write_config(cfg)
        self.assertTrue(self.provider("hangzhale")["deletable"])
        self.assertTrue(self.delete("hangzhale")["ok"])
        self.assertEqual(self.config.read_text(encoding="utf-8"), cfg,
                         "无关的多行 flow 必须逐字节不变")

    def test_flow_interior_line_never_produces_an_op(self):
        """多行 flow 内部即便长得像 `key: value` 也不能被当块写法改写。"""
        cfg = "modelRoles: {\n  default: other/n,\n  smol: hangzhale/m\n}\n"
        self.write_config(cfg)
        cleared, unsupported, _l = server._custom_references(
            "hangzhale", ["m"], cfg.splitlines(keepends=True))
        self.assertEqual(cleared, [], "flow 区域内部不得生成清理操作")
        self.assertTrue(unsupported)




class TestFlowRegionScope(CustomProviderBase):
    """flow 区域只能由真实 flow 值启动，不能被普通字符串/块标量正文吞掉。"""

    def setUp(self):
        super().setUp()
        self.write_models("providers:\n  hangzhale:\n    baseUrl: http://h/v1\n"
                          "    models:\n      - id: m\n")

    def test_plain_string_bracket_does_not_start_region(self):
        """`match: echo [` 是普通字符串，不是 flow。"""
        self.write_config("bash:\n  patterns:\n    - match: echo [\n"
                          "      approval: allow\n"
                          "modelRoles:\n  default: hangzhale/m\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.config.read_text(encoding="utf-8")
        self.assertIn("match: echo [", out, "非 flow 的字符串必须原样保留")
        self.assertNotIn("default:", out, "真实的 modelRoles 引用必须被清掉")

    def test_block_scalar_body_bracket_does_not_start_region(self):
        self.write_config("description: |\n  [文档正文\n"
                          "modelRoles:\n  default: hangzhale/m\n")
        r = self.delete("hangzhale")
        self.assertTrue(r["ok"], r)
        out = self.config.read_text(encoding="utf-8")
        self.assertIn("[文档正文", out)
        self.assertNotIn("default:", out)

    def test_region_only_starts_on_value_leading_bracket(self):
        lines = ("bash:\n  patterns:\n    - match: echo [\n"
                 "modelRoles:\n  default: hangzhale/m\n").splitlines(keepends=True)
        self.assertEqual(server._flow_regions(lines), {},
                         "值不是以 {/[ 开头时不得启动 flow 区域")

    def test_real_multiline_flow_still_detected(self):
        lines = ("retry:\n  fallbackChains:\n    smol: [\n"
                 "      hangzhale/m,\n    ]\n").splitlines(keepends=True)
        self.assertEqual(list(server._flow_regions(lines)), [2])




if __name__ == "__main__":
    unittest.main()
