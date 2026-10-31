# -*- coding: utf-8 -*-
"""omp 设置中心 / omp Settings Web（原 omp Settings Center，更早是模型角色切换器）。

- 模型角色：两级分组选择器，点击切换 modelRoles。
- 全部设置：读取 `omp config list --json` 全量目录（键/值/类型/描述），
  标量项（布尔/数字/字符串/枚举）定向编辑 config.yml；数组与嵌套块只读展示。
- 用量：透传 `omp usage --json` 的额度/用量报告（各供应商窗口限额、
  已用/剩余、重置时间、重置额度券），供「用量」页签做可视化。

安全设计：不用 `omp config set`（会重写整文件并剥离注释）；按字节读写
~/.omp/agent/config.yml，只替换/插入目标行并沿用原行尾；每次写入前自动备份。
"""
import fnmatch
import hashlib
import json
import os
import queue
import threading
import re
import shutil
import subprocess
import sys
import time
import webbrowser
import sqlite3
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 配置档：omp 的规则是 --profile 优先于 OMP_PROFILE 优先于 PI_PROFILE，且空串与
# "default" 都表示默认档；PI_CONFIG_DIR 会改根目录，PI_CODING_AGENT_DIR 只在没有
# 配置档时改 agent 目录。
#
# 这里**不合成 --profile 参数**：子进程直接继承本进程环境，由 omp 自己按上面的
# 规则判断，避免复刻出错（曾经用 `OMP_PROFILE or PI_PROFILE` 的 truthy 回退，结果
# 显式 `OMP_PROFILE=''`（用户明确要默认档）会错误地退回 PI_PROFILE=work）。
# PROFILE 只用于页面展示，且按「存在性」取值：设了 OMP_PROFILE（哪怕为空）就以它为准。
def _env_profile():
    for name in ("OMP_PROFILE", "PI_PROFILE"):
        if name in os.environ:                 # 存在即优先，空串=默认档
            value = os.environ[name].strip()
            return None if value in ("", "default") else value
    return None


PROFILE = _env_profile()

HERE = Path(__file__).parent
# 目录在首次使用时解析（不阻塞启动、失败也能降级）；解析失败时为 None。
_AGENT_DIR = []
_AGENT_DIR_LOCK = threading.Lock()
# 端口可通过环境变量覆盖（隔离冒烟用独立端口，默认仍是 8788）
try:
    PORT = int(os.environ.get("OMP_SETTINGS_PORT") or 8788)
except ValueError:
    PORT = 8788
# 服务只监听 127.0.0.1；LISTEN_IPV6 保留给显式 opt-in 的 ::1 监听，
# Host 白名单据此决定是否接受 [::1] / ::1 形式（不监听就不接受）。
LISTEN_IPV6 = os.environ.get("OMP_SETTINGS_IPV6", "").strip() == "1"
EDITABLE_TYPES = {"string", "boolean", "number", "integer", "enum"}
# pythonw 下无控制台，omp.exe 子进程会各自弹出新窗口；Windows 上一律隐藏
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ThreadingHTTPServer 下并发 POST 会同时改写 config.yml；统一串行化所有写入
_config_lock = threading.Lock()

ROLE_COMMENT = {
    "default": "主会话模型",
    "smol": "轻量快速任务",
    "slow": "深度推理",
    "plan": "规划模式",
    "vision": "图像理解",
    "task": "子代理",
    "commit": "提交信息生成",
    "tiny": "极小分类/标题",
    "web": "网络搜索",
    "advisor": "顾问评审",
}

KEY_LINE = re.compile(r'^([ \t]*)(.+?):(?:[ \t]([^\r\n]*))?(\r?\n)?$')
# 纯标量安全字符；其余一律双引号（JSON 转义是 YAML 双引号风格的子集）
PLAIN_SCALAR = re.compile(r'[A-Za-z0-9_@.,/\-]+(?::[A-Za-z0-9_@.,/\-]+)*')


def omp_cmd(args):
    """返回要执行的 omp argv（不合成 --profile）。

    配置档完全交给 omp：子进程继承本进程环境，omp 自己按
    --profile > OMP_PROFILE > PI_PROFILE 的规则解析，显式空串与 `default`
    都表示默认档。这样不会因为本页的 truthy/回退写法改变 omp 的档位语义。
    """
    return list(args)


def _probe_agent_dir(timeout=15):
    """问 omp 自己的权威 agent 目录（`omp config path`）。

    返回 (Path|None, error|None)。这条命令把 --profile / OMP_PROFILE / PI_PROFILE /
    PI_CONFIG_DIR / 默认档哨兵等全部规则收敛到 omp 内部，比我复刻更可靠。
    """
    try:
        p = subprocess.run(omp_cmd(["omp", "config", "path"]),
                           capture_output=True, text=True, shell=False,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return None, f"omp config path 超时（{timeout}s）"
    except OSError as e:
        return None, f"无法执行 omp: {e}"
    line = (p.stdout or "").strip().splitlines()
    path = line[0].strip() if line else ""
    if p.returncode != 0 or not path:
        err = (p.stderr or "").strip().splitlines()
        return None, (err[0] if err else f"omp config path 退出码 {p.returncode}")
    return Path(path), None


# 目录在导入时解析一次：失败不阻塞，回落到 omp 的默认约定路径（只读功能照旧可用），
# 但 agentDirError 会让删除类操作一律关闭——绝不在猜到的路径上删东西。
AGENT_DIR, AGENT_DIR_ERROR = _probe_agent_dir()
_FALLBACK_AGENT_DIR = Path.home() / ".omp" / "agent"
CONFIG = (AGENT_DIR or _FALLBACK_AGENT_DIR) / "config.yml"
DB = (AGENT_DIR or _FALLBACK_AGENT_DIR) / "agent.db"


def run(args):
    p = subprocess.run(omp_cmd(args), capture_output=True, text=True, shell=False,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return p.stdout


# ---------- config.yml 行级解析 ----------

def split_comment(v):
    """剥掉引号外的行尾注释，返回 (值, 注释)。"""
    inq = None
    for i, ch in enumerate(v):
        if inq:
            if ch == inq:
                inq = None
        elif ch in '"\'':
            inq = ch
        elif ch == '#' and (i == 0 or v[i - 1] in ' \t'):
            return v[:i].rstrip(), v[i:]
    return v.rstrip(), ''


def index_config(lines):
    """按缩进栈扫描，返回 {点路径: {line, kind: scalar|block}}。"""
    idx, stack = {}, []
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        m = KEY_LINE.match(line)
        if m is None:
            continue
        indent = len(m.group(1))
        key = m.group(2).strip().strip('"\'')
        val = (m.group(3) or '').strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = '.'.join(k for _, k in stack + [(indent, key)])
        idx[path] = {'line': i, 'kind': 'scalar' if val else 'block'}
        stack.append((indent, key))
    return idx


def yaml_scalar(s):
    if PLAIN_SCALAR.fullmatch(s) and \
       not re.fullmatch(r'(?i:true|false|null|yes|no|on|off|~|-?\d+(\.\d+)?)', s):
        return s
    return json.dumps(s, ensure_ascii=False)


def load_zh():
    try:
        return json.loads((HERE / "zh.json").read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


_ENUM_LINE = re.compile(
    r'^\s+([A-Za-z0-9_.\-]+)\s*=\s*\S*\s*\(([^)]*\|[^)]*)\)\s*$')


def enum_options_from_omp():
    """从 `omp config list` 文本输出的 `key = value (a|b|c)` 解析真实值域。

    `--json` 只给 type/value/description，不含枚举合法值；文本输出才有。
    zh.json 的 options 可能落后于 omp（新增枚举值时），这里是权威兜底。
    """
    out = {}
    for line in run(["omp", "config", "list"]).splitlines():
        m = _ENUM_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).split('|')
    return out


def enum_options(key, zh=None):
    """某一 enum 项的合法值：zh.json 优先，缺失时问 omp 本身。"""
    opts = ((zh if zh is not None else load_zh()).get(key) or {}).get('options')
    if opts:
        return list(opts)
    return enum_options_from_omp().get(key) or []


def coerce_value(typ, s, options=None):
    """把页面提交的字符串转成 config.yml 里的字面量。

    options 是 enum 项的合法值列表（来自 zh.json）。omp 对 config.yml 里的
    非法枚举/布尔值不报错，而是静默回落默认值（实测：写入
    `display.showTurnTime: bogusXyz` 后 `omp config get` 返回 false，
    文件里却留着 bogusXyz）。所以这里必须自己拦，否则页面写进非法值后
    文件内容与生效值不一致、且没有任何提示。
    """
    s = s.strip()
    if typ == 'boolean':
        low = s.lower()
        if low in ('true', '是', '1', 'yes'):
            return 'true'
        if low in ('false', '否', '0', 'no'):
            return 'false'
        raise ValueError('布尔项只接受 true/false')
    if typ in ('number', 'integer'):
        if re.fullmatch(r'-?\d+(\.\d+)?([eE][-+]?\d+)?', s) is None:
            raise ValueError('该项要求数字')
        return s
    if typ == 'enum' and options:
        if s not in options:
            raise ValueError(
                f'该项只接受这些值之一：{", ".join(options)}（收到 {s!r}）')
        return s
    if s == '':
        raise ValueError('空值请用 null 表示')
    if s.lower() == 'null':
        return 'null'
    return yaml_scalar(s)


def write_backup():
    backup = CONFIG.with_name(
        f"config.yml.bak-settings-web-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CONFIG, backup)
    return backup


def set_config_key(key, formatted):
    with _config_lock:
        return _set_config_key(key, formatted)


def _set_config_key(key, formatted):
    raw = CONFIG.read_bytes().decode('utf-8')
    eol = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.splitlines(keepends=True)
    idx = index_config(lines)
    if key in idx:
        info = idx[key]
        if info['kind'] != 'scalar':
            return '该项在 config.yml 中是块值（数组/嵌套），请手工编辑'
        m = KEY_LINE.match(lines[info['line']])
        if m is None:
            return '内部错误：行格式变化'
        comment = (m.group(4) or '')
        _, c = split_comment(m.group(3) or '')
        lines[info['line']] = f"{m.group(1)}{m.group(2)}: {formatted}{c}{comment}"
        if not lines[info['line']].endswith('\n'):
            lines[info['line']] += eol
    else:
        parts = key.split('.')
        target = None
        for depth in range(len(parts) - 1, 0, -1):
            p = '.'.join(parts[:depth])
            if p in idx:
                if idx[p]['kind'] != 'block':
                    return '父级是标量，无法插入子键'
                target = (idx[p]['line'], depth)
                break
        new = []
        base = target[1] if target else 0
        remaining = parts[base:] if target else parts
        for j, part in enumerate(remaining):
            ind = '  ' * (base + j)
            last = j == len(remaining) - 1
            new.append(f"{ind}{part}:{f' {formatted}' if last else ''}{eol}")
        if target:
            lines[target[0] + 1:target[0] + 1] = new
        else:
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += eol
            lines.extend(new)
    write_backup()
    CONFIG.write_bytes(''.join(lines).encode('utf-8'))
    return None


# ---------- 读取 ----------

def parse_roles():
    roles, in_block = {}, False
    for line in CONFIG.read_bytes().decode('utf-8').splitlines():
        if re.match(r'^modelRoles:\s*$', line):
            in_block = True
            continue
        if in_block:
            m = re.match(r'^  (\S+):\s*(\S+)\s*$', line)
            if m:
                roles[m.group(1)] = m.group(2)
            elif line and not line.startswith(' '):
                break
    return roles


def list_models(timeout=None):
    if timeout is None:
        out = run(["omp", "models", "--json"])
    else:
        out = subprocess.run(omp_cmd(["omp", "models", "--json"]), capture_output=True,
                             text=True, shell=False, encoding="utf-8",
                             errors="replace", timeout=timeout,
                             creationflags=NO_WINDOW).stdout
    try:
        models = json.loads(out)["models"]
    except (json.JSONDecodeError, KeyError):
        return []
    keep = []
    for m in models:
        if m.get("kind") != "chat":
            continue
        keep.append({
            "selector": m["selector"],
            "name": m.get("name") or m["id"],
            "provider": m.get("provider"),
            "contextWindow": m.get("contextWindow"),
            "reasoning": bool(m.get("reasoning")),
        })
    keep.sort(key=lambda x: (x["provider"] or "", x["name"].lower()))
    return keep


class CatalogUnavailable(Exception):
    """omp 模型目录读不到或为空，导致裸模型 ID 的归属无从判定。"""


def full_catalog():
    """omp 的完整模型目录（`--kind all`，含 image/tts/stt/judge/search 等）。

    裸模型 ID 的归属必须按完整目录判定：`list_models()` 只保留 chat 模型，用它
    会把 image/web 等共享 ID 误认成「只属于本供应商」。目录读不到、JSON 解析
    失败或结果为空时一律抛 CatalogUnavailable —— 猜错会删掉别人的有效引用，
    所以这里必须 fail-closed，而不是返回空表。
    """
    p = subprocess.run(omp_cmd(["omp", "models", "--json", "--kind", "all"]),
                       capture_output=True, text=True, shell=False,
                       encoding="utf-8", errors="replace", timeout=120,
                       creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise CatalogUnavailable(
            f"omp models 退出码 {p.returncode}: {(p.stderr or '').strip()[:200]}")
    try:
        models = json.loads(p.stdout or "")["models"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise CatalogUnavailable(f"omp models 输出无法解析: {e}") from e
    if not isinstance(models, list) or not models:
        raise CatalogUnavailable("omp 模型目录为空")
    out = []
    for m in models:
        if not isinstance(m, dict):
            continue
        sel = m.get("selector") or ""
        prov = m.get("provider") or ""
        if not sel or not prov:
            continue
        out.append({"selector": sel, "provider": prov,
                    "kind": m.get("kind") or ""})
    if not out:
        raise CatalogUnavailable("omp 模型目录没有可用的 provider/selector")
    return out


# ---------- 模型连通性测试（omp bench 单次探测） ----------

PROBE_DEADLINE = 60           # 整个探测的总时限（秒），含目录查询与 bench
PROBE_PROMPT = "Reply with OK only."
PROBE_MAX_ERROR = 300         # 透出错误信息的最大长度

# 同一时刻最多一个连通性测试；锁失败即 busy，不排队
_probe_lock = threading.Lock()

_SECRET_SUBS = (
    # key=value / key: value / "key": "value"；值前可带 Bearer/Basic 方案名
    (re.compile(r'(?i)((?:"[^"]*(?:key|token|secret)[^"]*"|'
                r'api[-_ ]?key|authorization|password|secret|token)[ \t]*[:=][ \t]*'
                r'(?:(?:bearer|basic)[ \t]+)?)["\']?\S+'), r'\1[REDACTED]'),
    # 裸 "Bearer <token>" / "Basic <cred>"（前面没有键名时兜底）
    (re.compile(r'(?i)\b(bearer|basic)[ \t]+[A-Za-z0-9._~+/=-]{8,}'),
     r'\1 [REDACTED]'),
    # JWT
    (re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}'),
     '[REDACTED]'),
    # URL 查询参数里的凭据
    (re.compile(r'(?i)([?&][^ \t=]*(?:key|token|secret|sig|signature|auth|cred)'
                r'[^ \t=]*)=\S+'), r'\1=[REDACTED]'),
)


def _sanitize_error(text):
    """把上游报错剪成有限长度的安全文本：脱敏凭据、折叠空白、截断。"""
    s = str(text or '')
    for pat, repl in _SECRET_SUBS:
        s = pat.sub(repl, s)
    s = ' '.join(s.split())
    return s[:PROBE_MAX_ERROR]


def probe_model(selector):
    """对单个 selector 发起一次 omp bench 连通性探测。

    返回 {ok, selector, code, elapsedMs, model?, durationMs?, ttftMs?, error?}；
    code: success / error / timeout / invalid_model / busy / unavailable /
    invalid_response。成功以 bench 报告里 results[0].ok === True 且
    selector/model 严格回显为准，不看 exit code。
    """
    t0 = time.monotonic()
    result = {"ok": False, "selector": selector, "code": "error"}

    def done(**kw):
        result.update(kw)
        result["elapsedMs"] = round((time.monotonic() - t0) * 1000, 1)
        return result

    def remaining():
        """PROBE_DEADLINE 内的剩余秒数；目录查询与 bench 共用同一预算。"""
        return max(0.1, PROBE_DEADLINE - (time.monotonic() - t0))

    # 选择器必须是当前目录中的精确 provider/model 项：拒绝 role 别名
    # （@smol 等）、模糊 id、以及任何未在目录中出现的串（含注入参数）。
    if not _probe_lock.acquire(blocking=False):
        return done(code="busy", error="已有测试在进行中")
    try:
        try:
            catalog = list_models(timeout=remaining())
        except subprocess.TimeoutExpired:
            return done(code="unavailable", error="获取模型目录超时")
        except FileNotFoundError:
            return done(code="unavailable", error="未找到 omp 命令")
        except OSError as e:
            return done(code="unavailable",
                        error=_sanitize_error(f"获取模型目录失败: {e}"))
        if not catalog:
            return done(code="unavailable", error="模型目录为空或不可用")
        if (not isinstance(selector, str)
                or not any(m.get("selector") == selector for m in catalog)):
            return done(code="invalid_model",
                        error="模型不在当前目录中，请刷新后重试")

        probe_selector = selector + ":off"
        cmd = omp_cmd(["omp", "bench", probe_selector, "--runs", "1", "--par", "1",
                       "--max-tokens", "256", "--prompt", PROBE_PROMPT,
                       "--service-tier", "none", "--json"])
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, shell=False,
                               encoding="utf-8", errors="replace",
                               timeout=remaining(), creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return done(code="timeout",
                        error=f"测试超过 {PROBE_DEADLINE} 秒未完成")
        except FileNotFoundError:
            return done(code="unavailable", error="未找到 omp 命令")
        except OSError as e:
            return done(code="unavailable",
                        error=_sanitize_error(f"无法启动 omp: {e}"))

        try:
            report = json.loads(p.stdout or "")
        except (json.JSONDecodeError, TypeError):
            report = None
        if not isinstance(report, dict):
            detail = _sanitize_error(p.stderr) or "bench 未返回 JSON 报告"
            return done(code="invalid_response", error=detail)
        models = report.get("models")
        if not isinstance(models, list) or len(models) != 1:
            return done(code="invalid_response",
                        error="bench 报告未包含单条模型结果")
        entry = models[0]
        if not isinstance(entry, dict):
            return done(code="invalid_response", error="bench 报告格式异常")
        if entry.get("selector") != probe_selector or entry.get("model") != selector:
            return done(code="invalid_response",
                        error="bench 返回的模型与请求不一致")
        results = entry.get("results")
        if not isinstance(results, list) or len(results) != 1 \
                or not isinstance(results[0], dict):
            return done(code="invalid_response",
                        error="bench 报告未包含单条运行结果")
        r = results[0]
        if r.get("ok") is not True:
            detail = _sanitize_error(r.get("error")) \
                or _sanitize_error(p.stderr) \
                or f"omp bench 失败（退出码 {p.returncode}）"
            return done(code="error", error=detail)
        return done(ok=True, code="success", model=entry["model"],
                    durationMs=r.get("durationMs"), ttftMs=r.get("ttftMs"))
    finally:
        _probe_lock.release()


# ---------- 模型显示（enabledModels） ----------

ENABLED_HEAD = re.compile(r'^enabledModels:[ \t]*(\[\s*\])?[ \t]*(#.*)?(\r?\n)?$')
LIST_ITEM = re.compile(r'^[ \t]+-[ \t]+(.*?)[ \t]*(\r?\n)?$')


def enabled_block(lines):
    """定位顶层 enabledModels 段，返回 (首行, 尾后行, 条目) 或 None；含 path 作用域条目时条目为 None。"""
    for i, line in enumerate(lines):
        m = ENABLED_HEAD.match(line)
        if not m:
            continue
        if m.group(1):  # enabledModels: []
            return i, i + 1, []
        entries, j = [], i + 1
        while j < len(lines):
            ln = lines[j]
            if not ln.strip():
                break
            if not ln[0].isspace():
                break
            if ln.lstrip().startswith('#'):
                j += 1                    # 块内注释行不影响条目解析
                continue
            item = LIST_ITEM.match(ln)
            if item is None:
                return i, None, None  # 嵌套/作用域结构，不托管
            v, _ = split_comment(item.group(1))
            if v.startswith(('{', 'path', 'paths')) or v.endswith(':'):
                return i, None, None
            if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
                v = v[1:-1]
            entries.append(v)
            j += 1
        return i, j, entries
    return None


def enabled_item_rows(lines):
    """enabledModels 块列表里每个条目实际所在的行号（跳过块内注释与空行）。"""
    blk = enabled_block(lines)
    if not blk or blk[2] is None:
        return []
    rows, j = [], blk[0] + 1
    while j < blk[1] and len(rows) < len(blk[2]):
        ln = lines[j]
        if ln.strip() and not ln.lstrip().startswith('#'):
            rows.append(j)
        j += 1
    return rows


def pattern_matches(pat, model):
    p = pat.lower()
    sel = model["selector"].lower()
    if any(c in p for c in "*?["):
        return fnmatch.fnmatchcase(sel, p) or fnmatch.fnmatchcase(sel.split('/', 1)[1], p)
    return p == sel or p == sel.split('/', 1)[1]


def visibility_state():
    lines = CONFIG.read_bytes().decode('utf-8').splitlines(keepends=True)
    blk = enabled_block(lines)
    models = list_models()
    patterns = blk[2] if blk else []
    managed = not (blk and blk[2] is None)
    if not patterns:  # 空名单 = 全部显示
        visible = [m["selector"] for m in models]
    else:
        visible = [m["selector"] for m in models
                   if any(pattern_matches(p, m) for p in patterns)]
    return {"models": models, "visible": visible, "patterns": patterns or [],
            "managed": managed, "default": parse_roles().get("default")}


def set_visible(selected):
    with _config_lock:
        return _set_visible(selected)


def _set_visible(selected):
    if not isinstance(selected, list) or not all(isinstance(s, str) for s in selected):
        return "参数格式错误"
    models = list_models()
    by_sel = {m["selector"]: m for m in models}
    chosen = {s for s in selected if s in by_sel}
    if not chosen:
        return "至少要保留一个可见模型（空名单在 omp 中等于全部显示）"
    raw = CONFIG.read_bytes().decode('utf-8')
    eol = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.splitlines(keepends=True)
    blk = enabled_block(lines)
    if blk and blk[2] is None:
        return "config.yml 的 enabledModels 含 path 作用域等复杂结构，请手工编辑"
    old = blk[2] if blk else []
    # 当前匹配不到任何模型的条目（供应商离线等）原样保留，避免上线后被意外隐藏
    dormant = [p for p in old if not any(pattern_matches(p, m) for m in models)]
    providers = {}
    for m in models:
        providers.setdefault(m["provider"], []).append(m["selector"])
    out = []
    default = parse_roles().get("default")
    if default and default in by_sel:  # 启动模型取名单首项：把 default 角色模型放第一位且保持可见
        chosen.add(default)
        out.append(default)
    for prov in sorted(providers):
        sels = providers[prov]
        if all(s in chosen for s in sels):
            out.append(f"{prov}/*")
        else:
            out.extend(s for s in sorted(sels) if s in chosen and s != default)
    out.extend(p for p in dormant if p not in out)
    block = [f"enabledModels:{eol}"] + [f"  - {json.dumps(p)}{eol}" for p in out]
    if blk:
        lines[blk[0]:blk[1]] = block
    else:
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += eol
        lines.extend(block)
    write_backup()
    CONFIG.write_bytes(''.join(lines).encode('utf-8'))
    return None

# ---------- 自定义供应商定义（models.yml 的 providers.<name>） ----------
# 只托管「本地 models.yml 里真正的供应商定义块」：按顶格 `providers:` 的块结构
# 逐行定位，删除时按字节切掉目标块，绝不让 YAML 库重排整个文件（注释、行尾、
# 引号风格、其他供应商都必须原样保留）。结构超出这个子集（内联 map、重复键、
# flow 结构跨到键缩进、tab 缩进）一律拒绝写回，而不是猜测块边界。
#
# 安全边界：不读、不回传 baseUrl / apiKey / headers 的值；删除前核对文件摘要
# （stale 拒绝）、omp agent 目录与 broker 门禁、以及 config.yml 里的模型引用。

"""临时文件：待插入 server.py 的定稿段落。"""
PROVIDER_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}')
# 含 modelOverrides 的块带有对内置模型的覆盖设置，一律不在这里删。
OVERRIDE_ONLY = {"modelOverrides"}
_MODELS_HEAD = re.compile(r'^providers[ \t]*:(?:[ \t]*(?:#.*)?)?$')
_MODELS_HEAD_INLINE = re.compile(r'^providers[ \t]*:')
_KEY_LINE_STRICT = re.compile(r'^("[^"]*"|\'[^\']*\'|[^\s#:\-][^:]*?)[ \t]*:'
                              r'(?:[ \t]+(.*))?$')
_BLOCK_SCALAR_TAIL = re.compile(r':[ \t]*[|>][+-]?\d*[ \t]*$')
_ID_IN_FLOW = re.compile(r'(?:^|[{,\s])id[ \t]*:[ \t]*("[^"]*"|\'[^\']*\'|[^,}\]\s]+)')
_ID_ITEM = re.compile(r'^-\s*id[ \t]*:[ \t]*(\S+)')
_ID_ITEM_FLOW = re.compile(r'^-\s*\{')
_MODELS_KEY_IN_FLOW = re.compile(r'(?:^|[{,]\s*)models[ \t]*:[ \t]*(.*)$')
# 任何位置出现的 `provider/...` 形式引用；前面不能是标识符字符或另一个斜杠。
_REF_IN_LINE = re.compile(r'(?<![\w.\-/])([A-Za-z0-9][A-Za-z0-9_.\-]{0,63})'
                          r'/(?:[A-Za-z0-9_.\-]*|\*)')
# YAML 的锚点/别名/合并：删除一个供应商可能让别处失去锚点，本页不尝试处理。
# YAML 的锚点/别名 token：& / * 出现在「值的开头」且后面紧跟一个锚点名。
# 锚点名按 YAML 的 ns-anchor-char 取「除流指示符与空白外」的字符，因此非 ASCII
# 名字（如中文）也算；同时要求 & / * 前必须是 YAML 的定界位置（行首、空白、
# 或 [ { , 以及 `key:` 之后），这样 `a&b`、URL 里的 `&`、markdown 的 `**粗体**`
# 与引号字符串里的 `&`/`*` 都不会被当成锚点。
_YAML_ANCHOR = re.compile(r'(?:^|[\s\[{,:])[&*]([^\s\[\]{},]+)')
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\'')
_YAML_MERGE = re.compile(r'^[\[{]?[ \t]*(?:<<|"[^"]*"[ \t]*:[ \t]*[&*])')
_THINKING_SUFFIX = re.compile(r':(?:off|minimal|low|medium|high|xhigh|max|auto|inherit)$')

# 同一时刻只允许一次 models.yml / config.yml 的删除写入
_models_lock = threading.Lock()


class UnsafeStructure(Exception):
    """models.yml 的结构超出本页能安全重写的子集。"""


class ReferenceCheckError(Exception):
    """config.yml 无法读取或无法解析，导致「是否有引用」无从判断。"""


def _provider_gate():
    """删除供应商定义的唯一前置条件：omp 自己确认过 agent 目录。

    这里不查 auth-broker：本操作只改本地 models.yml，与远程凭据库无关，
    拿凭据门禁去拦它会误伤（删账号的门禁另在 deletion_gate）。
    """
    if AGENT_DIR is None:
        return AGENT_DIR_ERROR or "无法确定 omp 的 agent 目录"
    return None


def _models_file():
    """本地供应商定义文件：models.yml 优先，其次 models.yaml。

    两个文件名 omp 都支持；本页不改名、不创建，只操作已经存在的那个。
    """
    base = AGENT_DIR if AGENT_DIR is not None else _FALLBACK_AGENT_DIR
    for name in ("models.yml", "models.yaml"):
        p = base / name
        if p.exists():
            return p
    return base / "models.yml"


def _line_parts(line):
    """返回 (缩进列数, 类型, 内容)；类型为 blank / comment / content。"""
    body = line.rstrip("\r\n")
    stripped = body.lstrip(' \t')
    if not stripped:
        return 0, "blank", ""
    lead = body[:len(body) - len(stripped)]
    if '\t' in lead:
        raise UnsafeStructure("模型定义文件使用了 tab 缩进")
    if stripped.startswith('#'):
        return len(lead), "comment", stripped
    return len(lead), "content", stripped


def _flow_delta(code):
    """统计一行里的 flow 括号净增量（跳过引号内容）。"""
    delta, inq = 0, None
    i = 0
    while i < len(code):
        ch = code[i]
        if inq:
            if inq == '"' and ch == '\\':
                i += 2
                continue
            if ch == inq:
                inq = None
        elif ch in '"\'':
            inq = ch
        elif ch in '[{':
            delta += 1
        elif ch in ']}':
            delta -= 1
        i += 1
    return delta


def _scan_region(lines, start, end):
    """扫描 [start, end)：返回 [(缩进, 类型, 进入本行前的 flow 深度, 是否块标量正文)]。

    块标量（| / >）的正文行整体跳过，避免把正文里的方括号当成 flow 结构。
    """
    records, depth, scalar_indent = [], 0, None
    for i in range(start, end):
        indent, kind, content = _line_parts(lines[i])
        if scalar_indent is not None and (kind == "blank" or indent > scalar_indent):
            records.append((indent, "scalar", depth, True))
            continue
        scalar_indent = None
        records.append((indent, kind, depth, False))
        if kind != "content":
            continue
        code, _ = split_comment(content)
        if _BLOCK_SCALAR_TAIL.search(code):
            scalar_indent = indent
            continue
        depth += _flow_delta(code)
        if depth < 0:
            raise UnsafeStructure("模型定义文件的 flow 括号不配对")
    if depth != 0:
        raise UnsafeStructure("模型定义文件的 flow 结构跨行未闭合")
    return records


def _providers_region(lines):
    """定位顶格 `providers:` 块，返回 (标题行, 块首行, 块尾后行, 子键缩进)。

    没有 providers 时返回 (None, None, None, None)；不是块结构时抛错。
    """
    head = next((i for i, line in enumerate(lines)
                 if _MODELS_HEAD.match(line.rstrip("\r\n"))), None)
    if head is None:
        for line in lines:
            body = line.rstrip("\r\n")
            if not _MODELS_HEAD_INLINE.match(body):
                continue
            value = body.split(':', 1)[1].split('#')[0].strip()
            if value == '{}':            # 显式的空映射：没有供应商，可直接照常处理
                return None, None, None, None
            raise UnsafeStructure("providers 不是块结构（疑似内联 map），请手工编辑")
        return None, None, None, None
    end = len(lines)
    for j in range(head + 1, len(lines)):
        indent, kind, _ = _line_parts(lines[j])
        if kind == "content" and indent == 0:
            end = j
            break
    records = _scan_region(lines, head + 1, end)
    child_indent = None
    for indent, kind, depth, scalar in records:
        if kind == "content" and not scalar and depth == 0:
            child_indent = indent
            break
    return head, head + 1, end, child_indent


def _provider_blocks(lines):
    """返回 [{name, start, last, end, keys, models, flow}]；不安全时抛 UnsafeStructure。"""
    head, start, end, child_indent = _providers_region(lines)
    if head is None or child_indent is None:
        return []
    blocks, seen, cur = [], set(), None
    for offset, (indent, kind, depth, scalar) in enumerate(_scan_region(lines, start, end)):
        line_no = start + offset
        if kind == "blank":
            continue
        if kind == "comment":
            # 缩进在第一层以内的注释属于下一个供应商（用户写在那里的说明），
            # 更深的注释属于当前块，必须跟着一起删掉
            if cur is not None and indent > child_indent and not scalar:
                cur["last"] = line_no
            continue
        if scalar:
            if indent <= child_indent:
                raise UnsafeStructure("块标量正文的缩进异常")
            cur["last"] = line_no
            continue
        if depth != 0:
            if indent <= child_indent:
                raise UnsafeStructure("providers 的 flow 结构跨到了键缩进")
            cur["last"] = line_no
            continue
        if indent < child_indent:
            raise UnsafeStructure("providers 下的缩进不一致")
        if indent > child_indent:
            if cur is None:
                raise UnsafeStructure("providers 下出现了没有归属的行")
            cur["last"] = line_no
            continue
        content = lines[line_no].strip()
        if content.startswith('-'):
            raise UnsafeStructure("providers 不是映射结构")
        m = _KEY_LINE_STRICT.match(content)
        if m is None:
            raise UnsafeStructure("providers 下出现了无法识别的行")
        name = _unquote(m.group(1))
        if name in seen:
            raise UnsafeStructure(f"models 里存在重复的供应商键 {name}")
        seen.add(name)
        cur = {"name": name, "start": line_no, "last": line_no}
        blocks.append(cur)
    for b in blocks:
        # 紧随其后的空行一并带走，避免删除后把空行留给上一个供应商
        last = b["last"]
        while last + 1 < end and not lines[last + 1].strip():
            last += 1
        b["end"] = last + 1
        head_line = lines[b["start"]]
        body = lines[b["start"] + 1:b["last"] + 1]
        b["keys"] = _block_top_keys(body)
        b["models"] = _block_model_ids(lines[b["start"]:b["last"] + 1])
        # 单行 flow 供应商（`acme: {baseUrl: …, models: [...]}`）的值写在同一行上，
        # 顶层键不在 body 里；这里从该行的内联映射取键与模型 id。
        inline = _inline_mapping(head_line)
        if inline is not None:
            b["flow"] = True
            b["keys"] = set(inline.keys())
            ids = _inline_model_ids(inline)
            if ids is not None:
                b["models"] = ids
            else:
                # 内联 models 解析不出模型 id：留着旧的扫描结果，但标注不可管理
                b["flow_unparsed"] = True
        else:
            b["flow"] = False
    return blocks


def _inline_mapping(line):
    """解析 `key: {a: 1, b: [...]}` 形式的单行供应商；不是就返回 None。"""
    m = _KEY_LINE_STRICT.match(line.strip())
    if m is None:
        return None
    value, _ = split_comment(m.group(2) or '')
    value = value.strip()
    if not value.startswith('{') or not value.endswith('}'):
        return None
    parts = _split_flow(value[1:-1])
    if parts is None:                        # 跨行 flow：本页不处理
        raise UnsafeStructure("供应商使用跨行 flow 映射，请手工编辑")
    out = {}
    for part in parts:
        pair = _split_flow_pair(part)
        if pair is None:
            raise UnsafeStructure("供应商的内联映射无法解析，请手工编辑")
        key, val = pair
        out[_unquote(key)] = val.strip()
    return out


def _inline_model_ids(mapping):
    """从内联映射的 models 值取模型 id；无法可靠解析时返回 None。"""
    raw = mapping.get("models")
    if raw is None:
        return None
    if not raw.startswith('[') or not raw.endswith(']'):
        return None
    parts = _split_flow(raw[1:-1])
    if parts is None:
        return None

    def entry_id(part):
        text = part.strip()
        if text.startswith('{'):
            if not text.endswith('}'):
                return None
            fields = _split_flow(text[1:-1])
            if fields is None:
                return None
            for field in fields:
                pair = _split_flow_pair(field)
                if pair and _unquote(pair[0]) == "id":
                    return _unquote(pair[1])
            return None
        return _unquote(text)

    out = []
    for part in parts:
        value = entry_id(part)
        if value is None:
            return None                      # 有解析不出的条目：整块视为不可管理
        if value and value not in out:
            out.append(value)
    return out


def _block_top_keys(region):
    """块内第一层键名（用于区分内置覆盖块与真正的自定义定义）。"""
    keys, indent = set(), None
    for line in region:
        indent_i, kind, content = _line_parts(line)
        if kind != "content":
            continue
        if indent is None:
            indent = indent_i
        if indent_i != indent:
            continue
        m = _KEY_LINE_STRICT.match(content)
        if m:
            keys.add(_unquote(m.group(1)))
    return keys


def _block_model_ids(region):
    """从块内 models 里取模型 id；只取 id，不取任何其他字段。"""
    ids, i = [], 0
    while i < len(region):
        indent, kind, content = _line_parts(region[i])
        i += 1
        if kind != "content":
            continue
        code, _ = split_comment(content)
        m = _MODELS_KEY_IN_FLOW.search(code)
        if m is None:
            continue
        value = m.group(1).strip()
        if value.startswith('['):
            ids.extend(_unquote(x) for x in _ID_IN_FLOW.findall(value))
            continue
        while i < len(region):
            ind2, kind2, content2 = _line_parts(region[i])
            if kind2 == "content" and ind2 <= indent:
                break
            if kind2 == "content":
                code2, _ = split_comment(content2)
                text = code2.strip()
                if _ID_ITEM_FLOW.match(text):
                    ids.extend(_unquote(x) for x in _ID_IN_FLOW.findall(text))
                else:
                    mm = _ID_ITEM.match(text)
                    if mm:
                        ids.append(_unquote(mm.group(1)))
            i += 1
    out = []
    for value in ids:
        if value and value not in out:
            out.append(value)
    return out


def _yaml_anchor_risk(lines, start, end):
    """被删除区间之外是否存在 YAML 锚点/别名/合并键。

    锚点（`&x`）被别的块用 `*x` 引用时，删掉定义块会让整份文件无法加载，连带
    毁掉其他供应商。这里不做依赖分析：只要文件里（含目标块内）出现锚点/别名/
    合并键，就拒绝写回，交给用户手工编辑。
    """
    for i, line in enumerate(lines):
        code, _ = split_comment(line)
        if not code.strip():
            continue
        if _YAML_MERGE.match(code.strip()):
            raise UnsafeStructure("模型定义文件使用了 YAML 合并键（<<），请手工编辑")
        # 引号字符串里的 & / * 是普通文本（URL 查询串、markdown 强调等），先剔除
        bare = _QUOTED.sub('""', code)
        if _YAML_ANCHOR.search(bare):
            raise UnsafeStructure("模型定义文件使用了 YAML 锚点/别名（& / *），"
                                  "删除块可能让其他供应商失去锚点，请手工编辑")
    return None


def _inline_flow_value(text):
    """从一行里取出 flow/标量值部分（去掉 `key:` 前缀与行尾注释）。"""
    m = _KEY_LINE_STRICT.match(text)
    if m is None:
        return None
    value, _ = split_comment(m.group(2) or '')
    return value.strip()


def _bare_id_hits(lines, declared, skip):
    """找出可能解析到本供应商声明模型的裸模型 ID 引用。

    omp 的 matchModel 在 `provider/model-id` 之后会做「精确裸 ID」匹配，
    所以 `modelRoles.default: gpt-6.1-sol` 这种不带供应商前缀的写法同样能
    命中 hangzhale 的模型。这里只看真正存放模型选择器的字段，避免把注释或
    描述文字里的同名词当成引用。
    """
    hits = []
    if not declared:
        return hits
    wanted = {d.lower(): d for d in declared}
    paths = _config_paths(lines)

    def note(line_no, selector, label):
        # omp 的选择器字段都先按逗号拆成候选列表（normalizeModelPatternList），
        # 再逐项去 thinking 后缀匹配；`a/m, gpt-6.1-sol` 里的裸 ID 同样会生效。
        for item in _model_candidates(selector):
            if item.lower() in wanted:
                hits.append(f"{label or paths(line_no)} → {item}（裸模型 ID）")

    for i, line in enumerate(lines):
        if i in skip:
            continue
        code, _ = split_comment(line)
        text = code.strip()
        if not text or text.startswith('#'):
            continue
        key = None
        m = _KEY_LINE_STRICT.match(text)
        if m is not None:
            key = _unquote(m.group(1))
        low = (key or '').lower()
        # 角色表与 agent 覆盖表：{角色或 agent 名: selector}
        if low in ("modelroles", "agentmodeloverrides"):
            value = _inline_flow_value(text)
            if value and value.startswith('{'):
                body = value[1:-1] if value.endswith('}') else ''
                for part in _split_flow(body) or []:
                    pair = _split_flow_pair(part)
                    if pair is not None:
                        note(i, _unquote(pair[1]), paths(i))
            continue
        if re.fullmatch(r'[A-Za-z0-9_.\-]{1,64}', low) and i > 0:
            parent = _parent_key(lines, i)
            if parent and parent.lower() in ("modelroles", "agentmodeloverrides"):
                inline = _inline_flow_value(text)
                if inline is None:
                    continue
                if inline.startswith('['):
                    for part in _split_flow(inline[1:-1]) or []:
                        note(i, _unquote(part), paths(i) + f".{key}")
                else:
                    note(i, _unquote(inline), paths(i) + f".{key}")
                continue
        # 标量字段：modelPresets.*.modelRoles.<role>、sharpshooter.model 等
        value = _inline_flow_value(text)
        if value is None:
            continue
        if value.startswith('['):
            for part in _split_flow(value[1:-1]) or []:
                note(i, _unquote(part), paths(i))
        elif not value.startswith(('{', '[')):
            note(i, _unquote(value), paths(i))
    # 去重，保持出现顺序
    out = []
    for h in hits:
        if h not in out:
            out.append(h)
    return out


def _model_candidates(text):
    """把一处选择器值拆成候选列表：按逗号分隔，逐项去空白与思考档后缀。"""
    out = []
    for part in (text or "").split(","):
        item = _THINKING_SUFFIX.sub("", part.strip().strip('"\''))
        if item:
            out.append(item)
    return out


def _parent_key(lines, line_no):
    """返回第 line_no 行的父键名（最近的缩进更小的内容行）。"""
    indent, kind, _ = _line_parts(lines[line_no])
    if kind != "content":
        return None
    for j in range(line_no - 1, -1, -1):
        ind, kd, content = _line_parts(lines[j])
        if kd != "content" or ind >= indent:
            continue
        m = _KEY_LINE_STRICT.match(content)
        return _unquote(m.group(1)) if m else None
    return None


def _config_paths(lines):
    """返回 (行号 → config.yml 该行的配置路径) 的查询函数。"""
    rev = {}
    for path, info in index_config(lines).items():
        rev.setdefault(info["line"], path)
    ordered = sorted(rev.items())

    def path_at(line):
        best = None
        for ln, path in ordered:
            if ln <= line:
                best = path
            else:
                break
        return best or "(顶层)"

    return path_at


# ---------- config.yml 引用清理（schema 驱动的模型选择字段） ----------
#
# 只改 omp 真正当作「模型选择器」的字段：modelRoles.*、retry.fallbackChains 的键与
# 值、enabledModels、task.agentModelOverrides.*、modelPresets.*.modelRoles.*、
# sharpshooter.model、edit.modelVariants 的模型键。其余字段（路径、URL、bash 策略
# 模式等）即使文本里出现供应商名也原样保留，绝不因其中的嵌套 flow 而禁用删除。

# 模型选择器字段（据本机 `omp config list --json` 的 schema）：
#   modelRoles.*、retry.fallbackChains（键与值）、enabledModels、
#   task.agentModelOverrides.*、modelPresets.*.modelRoles.*、sharpshooter.model、
#   edit.modelVariants.*
# 列表之外的字段（路径、URL、bash.patterns 等）原样保留，其中的嵌套 flow 也不
# 会因此禁用删除。
_SELECTOR_PATHS = (
    # 前缀路径, 形态
    (("modelRoles",), "single"),                  # 任意角色名 → 选择器
    (("task", "agentModelOverrides"), "single"),  # 任意 agent 名 → 选择器
    (("retry", "fallbackChains"), "chains"),      # 角色/选择器键 → [选择器]
    (("enabledModels",), "patterns"),             # 选择器/通配列表
    (("sharpshooter", "model"), "single"),
    (("edit", "modelVariants"), "variants"),
)
# 预设里的角色表只在 modelPresets.<preset>.modelRoles 之下；同一层还有
# defaultThinkingLevel 等非选择器字段，不能把整个 modelPresets 前缀当选择器。


def _selector_kind(path):
    """config 路径是否是模型选择器字段；返回形态或 None。

    形态：single（单个或逗号候选）、chains（键与值都是选择器）、
    patterns（选择器/通配列表）、variants（模型键 → 变体）。
    容器行本身（`modelRoles: {…}`）与子行（`modelRoles.default: …`）都算。
    """
    parts = tuple(path.split(".")) if path else ()
    if len(parts) >= 3 and parts[0] == "modelPresets" and parts[2] == "modelRoles":
        return "single"
    for prefix, kind in _SELECTOR_PATHS:
        if len(parts) >= len(prefix) and parts[:len(prefix)] == prefix:
            return kind
    return None


def _split_selector_parts(text):
    """按逗号拆选择器候选，保留每项原始写法（含引号与 thinking 后缀）。"""
    out, cur, inq = [], "", None
    for ch in text:
        if inq:
            cur += ch
            if ch == inq:
                inq = None
            continue
        if ch in '"\'':
            inq = ch
            cur += ch
        elif ch == ',':
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [p for p in (p.strip() for p in out) if p]


def _candidate_hits(candidate, name, declared, bare_owners=None):
    """单个选择器候选是否属于目标供应商。

    bare_owners 给出「裸模型 ID → 提供它的供应商集合」；裸 ID 只有在目标供应商
    是它唯一提供者时才算命中（omp 用 pickPreferredModel() 解析同名裸 ID）。
    """
    item = _THINKING_SUFFIX.sub("", _unquote(candidate).strip())
    if not item:
        return False
    if any(c in item for c in "*?["):
        return _wildcard_hits(item, name, declared)
    head, sep, _rest = item.partition('/')
    if sep:
        return head.lower() == name.lower()
    if item.lower() not in {d.lower() for d in declared or []}:
        return False
    if bare_owners is None:
        # 没有归属记录就不认为它属于目标（缺失一律保留，交给目录不可用的
        # fail-closed 路径去拒绝，而不是在这里猜）
        return False
    owners = bare_owners.get(item.lower()) or set()
    return owners == {name.lower()}


# YAML 普通标量不能以这些字符开头（`@` 保留、`*`/`&` 是别名锚点、`!` 是标签等）
_UNSAFE_LEAD = set("@`*&!%{}[],#>|'\"-?:")
# 这些值裸写会被当成布尔/空值/数字，必须加引号
_SCALAR_KEYWORDS = re.compile(r'(?i:true|false|null|yes|no|on|off|~|-?\d+(\.\d+)?)')


def _encode_selector(text):
    """按 YAML 需要给选择器标量加引号。

    `yaml_scalar()` 的字符集允许 `@` 打头，但 YAML 明确禁止，所以这里再校验
    首字符与保留字；其余走同一套编码，保证与写 config 的其他路径一致。
    """
    value = (text or "").strip()
    if not value:
        return '""'
    if value[0] in _UNSAFE_LEAD or _SCALAR_KEYWORDS.fullmatch(value):
        return json.dumps(value, ensure_ascii=False)
    if not PLAIN_SCALAR.fullmatch(value):
        return json.dumps(value, ensure_ascii=False)
    return value


def _join_selector(kept, quote):
    """把剩余候选拼回一行值。

    每个候选单独安全编码（`@smol` 这类不能裸写的值会加引号），再用原引号风格
    把整串包起来；只剩一个候选且不需要引号时保持裸写。
    """
    if len(kept) == 1:
        return _encode_selector(kept[0])
    joined = ", ".join(kept)
    if quote:
        return f"{quote}{joined}{quote}"
    # 多候选本身含逗号，必须整体加引号，否则会被拆成两个值
    return json.dumps(joined, ensure_ascii=False)


_KEY_OR_ITEM = re.compile(r'^(?P<lead>[ \t]*(?:-[ \t]+)?)(?P<key>"[^"]*"|\'[^\']*\'|'
                          r'[^\s#:][^:]*?)?[ \t]*:(?P<sep>[ \t]*)(?P<val>.*)$')


def _value_span(body):
    """返回一行里「值区间」的 (start, end) 与引号字符；无值返回 None。

    值区间不含行尾注释，也不含 `key:` 与其后空白，改写时只替换这段。
    """
    m = _KEY_OR_ITEM.match(body)
    if m is None or not m.group("key"):
        return None
    start = m.start("val")
    code, comment = split_comment(body[start:])
    end = start + len(code.rstrip())
    quote = None
    raw = body[start:end].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in '"\'':
        quote = raw[0]
    return start, end, quote, m


def _line_op(line_no, body, path, name, declared, bare_owners=None):
    """单行引用的清理方案：返回 (op|None, unsupported?)。

    只托管块写法：
      * 标量值（含逗号候选）——modelRoles / 预设 modelRoles /
        task.agentModelOverrides / sharpshooter.model / edit.modelVariants
      * retry.fallbackChains 的块键与块列表项
      * enabledModels 块列表项
    任何 flow 容器（`{…}` / `[…]`）一律不改写：含目标引用就报 unsupported，
    交给用户改成块写法；不含则原样保留、不影响删除。
    """
    kind = _selector_kind(path)
    if kind is None:
        return None, False                # 不是模型选择器字段：一字节都不动
    text = body.strip()
    if text.startswith('-'):
        if kind not in ("patterns", "chains"):
            return None, False
        value = text[1:].strip()
        if value.startswith(('{', '[')):
            if _flow_line_hits(value, name, declared):
                return None, f"{path}（flow 写法）"
            return None, False
        cands = _split_selector_parts(_unquote(value))
        remain = [c for c in cands
                  if not _candidate_hits(c, name, declared, bare_owners)]
        if len(remain) == len(cands):
            return None, False
        if not remain:
            return {"line": line_no, "path": path, "item": True,
                    "before": value, "after": None}, False
        quote = value[0] if value[:1] in ('"', "'") and len(value) > 1 else None
        return {"line": line_no, "path": path, "item": True, "before": value,
                "after": _join_selector(remain, quote)}, False

    m = _KEY_OR_ITEM.match(body)
    if m is None or not m.group("key"):
        return None, False
    value = (m.group("val") or "").strip()
    # fallbackChains 的「键」本身也可以是模型选择器：必须在看值之前判断，
    # 因为这种键通常是块标题（值为空），指向已删模型的链键必须整块删掉。
    if kind == "chains":
        key_raw = _unquote(m.group("key"))
        # fallbackChains 的键可以是角色名（smol/default/…）或模型选择器；
        # 角色名即使与某个模型 id 同名也绝不是模型引用，只认 provider/model。
        if "/" in key_raw and _candidate_hits(key_raw, name, declared, bare_owners):
            return {"line": line_no, "path": path, "drop_block": True,
                    "before": key_raw, "after": None}, False
    span = _value_span(body)
    if span is None:
        return None, False
    if not value or value[0] in '|>':
        return None, False                # 块标量/嵌套块的父行，交给子行处理
    if value.startswith(('{', '[')):
        # flow 容器：一律不改写。按保守规则判「文本里提到目标」即 unsupported
        if _flow_line_hits(value, name, declared):
            return None, f"{path}（flow 写法）"
        return None, False
    quote = span[2]
    cands = _split_selector_parts(_unquote(value))
    remain = [c for c in cands
              if not _candidate_hits(c, name, declared, bare_owners)]
    if len(remain) == len(cands):
        return None, False
    return {"line": line_no, "path": path, "before": value,
            "after": (_join_selector(remain, quote) if remain else None)}, False


# 与模型选择相关的顶层块：其下任意深度的 flow 行都按保守规则判定。
_MODEL_BLOCKS = {"modelRoles", "retry", "task", "modelPresets", "enabledModels",
                 "sharpshooter", "edit"}


def _in_model_block(path):
    """该配置路径是否落在与模型选择相关的顶层块里（任意深度）。"""
    parts = tuple(path.split(".")) if path else ()
    return bool(parts) and parts[0] in _MODEL_BLOCKS


def _flow_line_hits(line, name, declared):
    """一行 flow 文本是否按词边界提到目标供应商名或其声明的任一模型 ID。

    刻意保守：不做 flow 语义解析，只要文本里出现就拦（允许误拦，不允许漏拦）。
    """
    code, _ = split_comment(line)
    hay = code.lower()
    needles = [name.lower()] + [d.lower() for d in declared or [] if d]
    for needle in needles:
        if not needle:
            continue
        for m in re.finditer(re.escape(needle), hay):
            before = hay[m.start() - 1] if m.start() > 0 else " "
            after = hay[m.end()] if m.end() < len(hay) else " "
            # 前边界：不能紧贴标识符字符（`my-hangzhale`、`hangzhaleX` 不算）
            if before.isalnum() or before in "_-.":
                continue
            # 后边界：`/` 是 provider/model 分隔符，必须算命中；
            # 其余标识符字符（`hangzhaleX`）不算
            if after.isalnum() or after in "_-":
                continue
            return True
    return False


def _indent_of(line):
    return len(line) - len(line.lstrip())


def _flow_regions(lines, scalar_body=None):
    """找出所有多行 flow 区域：返回 {起始行: (结束行, 起始路径)}。

    只有「值以 `{` 或 `[` 开头」的行才可能启动区域（key: 之后或列表项 `-`
    之后，去注释）。普通字符串里的括号（`match: echo [`）、块标量正文
    （`description: |` 下的文本）都不是 flow，不能启动区域 —— 否则它们会把
    后面的真实配置吞进区域，导致漏检引用。

    区域内部继续跟踪 `{` / `[` 深度（跳过引号与行尾注释）直到闭合。
    """
    path_at = _config_paths(lines)
    skip = scalar_body or set()
    regions, i = {}, 0
    while i < len(lines):
        if i in skip:
            i += 1
            continue
        code, _ = split_comment(lines[i])
        text = code.strip()
        if not text or text.startswith('#'):
            i += 1
            continue
        # 取「值」部分：列表项取 `-` 之后，映射行取 `key:` 之后
        if text.startswith('-'):
            value = text[1:].strip()
        else:
            m = _KEY_OR_ITEM.match(text)
            if m is None:
                i += 1
                continue
            value = (m.group("val") or "").strip()
        if not value.startswith(('{', '[')):
            i += 1
            continue
        depth = _flow_delta(value)
        if depth <= 0:
            i += 1                        # 单行内闭合：不是多行区域
            continue
        start = i
        j = i + 1
        while j < len(lines) and depth > 0:
            if j in skip:
                j += 1                    # 块标量正文不参与括号计数
                continue
            inner, _ = split_comment(lines[j])
            depth += _flow_delta(inner)
            j += 1
        regions[start] = (j, path_at(start))
        i = j
    return regions


def _bare_id_owners(name, declared, catalog=None):
    """裸模型 ID → 提供它的供应商集合（omp 完整目录 + 本机 models.yml）。

    目录读不到/为空时抛 CatalogUnavailable：此时无法判断某个裸 ID 是否还有别的
    提供者，猜「只属于本供应商」会删掉别人的有效引用，所以必须 fail-closed。
    """
    if catalog is None:
        catalog = full_catalog()
    owners = {}
    for m in catalog:
        sel = m.get("selector") or ""
        prov = m.get("provider") or ""
        if not sel or not prov:
            continue
        mid = sel.split("/", 1)[1] if "/" in sel else sel
        if mid:
            owners.setdefault(mid.lower(), set()).add(prov.lower())
    # 只按目录记录归属：本机 models.yml 里声明、但 omp 目录里根本不存在的模型
    # 不属于「有归属记录」，此时一律不命中（缺失记录永远不视为属于目标）。
    return owners


def _needs_bare_owners(lines, name, declared):
    """config 的块写法标量里是否存在「可能是目标模型裸 ID」的候选。

    只有这种情况才需要目录判定归属（flow 行已按保守规则拦掉，不参与）。
    """
    wanted = {d.lower() for d in declared or []}
    if not wanted:
        return False
    path_at = _config_paths(lines)
    for i, line in enumerate(lines):
        if _selector_kind(path_at(i)) is None:
            continue
        code, _ = split_comment(line)
        text = code.strip()
        if not text or text.startswith('#'):
            continue
        if text.startswith('-'):
            value = text[1:].strip()
        else:
            m = _KEY_OR_ITEM.match(text)
            if m is None:
                continue
            value = (m.group("val") or "").strip()
        if not value or value.startswith(('{', '[')):
            continue                      # flow 由保守规则处理
        for part in _split_selector_parts(_unquote(value)):
            item = _THINKING_SUFFIX.sub("", part.strip())
            if not item or "/" in item or any(c in item for c in "*?["):
                continue
            if item.lower() in wanted:
                return True
    return False


def _custom_references(name, declared, lines=None, bare_owners=None):
    """config.yml 里指向该供应商的引用，以及逐处精确清理方案。

    返回 (cleared, unsupported, lines)：
      cleared     —— [{line, path, before, after}]；after 为 None 表示删掉该行，
                     drop_block 为 True 表示删掉整条链块。
      unsupported —— [str]，确定含有目标引用但本页无法安全改写的写法。
      lines       —— 本次判定所依据的 config.yml 快照；清理必须与它同源。

    config.yml 读不到时抛 ReferenceCheckError：读不到不等于没有引用。
    """
    if lines is None:
        try:
            raw = CONFIG.read_bytes().decode("utf-8-sig")
        except FileNotFoundError:
            return [], [], []             # 配置尚未生成：确实没有引用
        except OSError as e:
            raise ReferenceCheckError(f"无法读取 config.yml：{e}") from e
        lines = raw.splitlines(keepends=True)
    if bare_owners is None:
        # 只有真的存在「可能是本供应商模型的裸 ID」时才需要目录；此时目录不可用
        # 必须拒绝（fail-closed），否则会按「只属于本供应商」误删别人的引用。
        if _needs_bare_owners(lines, name, declared):
            bare_owners = _bare_id_owners(name, declared)   # 可能抛 CatalogUnavailable
        else:
            bare_owners = {}
    path_at = _config_paths(lines)
    cleared, unsupported = [], []

    scalar_body = set()               # 块标量（| / >）正文行：无法安全改写
    scalar_indent = None
    for i, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        if scalar_indent is not None:
            if not line.strip() or indent > scalar_indent:
                scalar_body.add(i)
                continue
            scalar_indent = None
        code, _ = split_comment(line)
        if re.search(r':[ \t]*[|>][+-]?\d*[ \t]*$', code):
            scalar_indent = indent

    regions = _flow_regions(lines, scalar_body)   # 多行 flow 区域：整段禁改
    flow_inside = {}                      # 区域内部行 → 起始行
    for start, (stop, _p) in regions.items():
        for k in range(start + 1, stop):
            flow_inside[k] = start
    dropped_at = None             # 已被 drop_block 整块删除的区域缩进
    for i, line in enumerate(lines):
        path = path_at(i)
        if i in flow_inside:
            continue                      # flow 区域内部行：永不生成改写操作
        if dropped_at is not None:
            # YAML 注释不结束一个块：空行与注释行一律跳过，只看内容行缩进
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            if _indent_of(line) > dropped_at:
                continue              # 属于已整块删除的区域
            dropped_at = None
        if i in scalar_body:
            if _in_model_block(path) and _flow_line_hits(line, name, declared):
                unsupported.append(f"{path}（块标量正文）")
            continue
        code, _ = split_comment(line)
        body = code.rstrip()
        if not body.strip() or body.lstrip().startswith('#'):
            continue
        if i in regions:
            # 多行 flow 容器的开始行：整段（到闭合行）作为一个 flow 区域，
            # 区域内任意一行按词边界提到目标就拦，位置记容器起始路径。
            stop, start_path = regions[i]
            if _in_model_block(start_path) and any(
                    _flow_line_hits(lines[k], name, declared)
                    for k in range(i, min(stop, len(lines)))):
                unsupported.append(f"{start_path}（flow 写法）")
            continue
        if _in_model_block(path):
            # 模型相关块内任意深度：值以 { 或 [ 开头的行，文本里按词边界提到
            # 目标供应商名或其声明模型 ID 就拦（允许误拦，不允许漏拦）。
            # 覆盖祖先整行 flow 与中间层（modelPresets.fast 等）。
            span = _value_span(body)
            if span is not None:
                value = body[span[0]:span[1]].strip()
                if value.startswith(('{', '[')) and \
                        _flow_line_hits(body, name, declared):
                    unsupported.append(f"{path}（flow 写法）")
                    continue
        op, bad = _line_op(i, body, path, name, declared, bare_owners)
        if bad:
            unsupported.append(bad if isinstance(bad, str) else path)
        elif op is not None:
            if op.get("drop_block"):
                dropped_at = _indent_of(line)
            cleared.append(op)
    if unsupported:
        # 有无法安全改写的引用位置：整条删除都必须拒绝，不能留下半清状态
        return [], sorted(set(unsupported)), lines
    return cleared, [], lines


def _drop_block_rows(rows, line_no):
    """删掉从 line_no 起的整个子块（含其后缩进更深的行）。"""
    base = len(rows[line_no]) - len(rows[line_no].lstrip())
    rows[line_no] = None
    j = line_no + 1
    while j < len(rows):
        if rows[j] is None:
            j += 1
            continue
        if not rows[j].strip() or rows[j].lstrip().startswith('#'):
            j += 1
            continue
        if len(rows[j]) - len(rows[j].lstrip()) <= base:
            break
        rows[j] = None
        j += 1


def _rewrite_config(lines, cleared, eol):
    """按清理方案改写 config.yml；只替换值区间，保留缩进与行尾注释。"""
    rows = list(lines)
    for op in cleared:
        i = op["line"]
        if op.get("drop_block"):
            _drop_block_rows(rows, i)
            continue
        if op["after"] is None:
            rows[i] = _drop_line(rows[i])
            continue
        body = rows[i].rstrip("\r\n")
        tail = rows[i][len(body):] or eol
        if op.get("item"):
            # `- value`：保留 `- `、缩进与行尾注释，只换值区间
            m = re.match(r'^([ \t]*-[ \t]+)(.*)$', body)
            if m is None:
                raise UnsafeStructure(f"第 {i + 1} 行不是列表项，无法改写")
            rest = m.group(2)
            code, _comment = split_comment(rest)
            value = code.rstrip()
            rows[i] = f"{m.group(1)}{op['after']}{rest[len(value):]}{tail}"
            continue
        span = _value_span(body)
        if span is None:
            raise UnsafeStructure(f"第 {i + 1} 行没有可替换的值区间")
        start, end, _quote, _m = span
        rows[i] = f"{body[:start]}{op['after']}{body[end:]}{tail}"
    return _collapse_empty_blocks(rows, lines)


def _drop_line(row):
    """删掉一行；若该行带注释，保留注释（只去掉值部分）。

    删除引用后仍留下 `default:` 空键会破坏语义，所以整行去掉；注释单独保留为
    同级注释行，避免用户写的说明凭空消失。
    """
    body = row.rstrip("\r\n")
    _code, comment = split_comment(body)
    if not comment.strip():
        return None
    lead = body[:len(body) - len(body.lstrip())]
    return f"{lead}{comment.strip()}\n" if "\r\n" not in row else f"{lead}{comment.strip()}\r\n"


EMPTY_MAP_KEEP = {"modelRoles", "fallbackChains", "agentModelOverrides", "modelPresets"}
EMPTY_LIST_KEEP = {"enabledModels"}       # 清空后保留成 `key: []`


def _collapse_empty_blocks(rows, originals):
    """清理后变空的父块：已知顶层键保留为空容器，其余删掉整块。

    空容器的行尾必须沿用原始行（#1）：`rstrip` 后的 body 没有换行，直接拼会
    把下一行粘上来。
    """
    def indent(s):
        return len(s) - len(s.lstrip())

    changed = True
    while changed:
        changed = False
        for i, row in enumerate(rows):
            if row is None:
                continue
            body = row.rstrip("\r\n")
            tail = row[len(body):] or "\n"
            text = body.strip()
            if not re.fullmatch(r'[^\s#:][^:]*:[ \t]*', text):
                continue                  # 只处理「键后无值」的块标题
            key = text.rstrip().rstrip(':').strip().strip('"\'')
            base = indent(body)
            j, has_child = i + 1, False
            while j < len(rows):
                if rows[j] is None:
                    j += 1
                    continue
                if not rows[j].strip() or rows[j].lstrip().startswith('#'):
                    j += 1
                    continue
                if indent(rows[j]) <= base:
                    break
                has_child = True
                break
            if has_child:
                continue
            head = body.rstrip(': \t')
            if key in EMPTY_MAP_KEEP:
                rows[i] = f"{head}: {{}}{tail}"
            elif key in EMPTY_LIST_KEEP:
                rows[i] = f"{head}: []{tail}"
            else:
                rows[i] = None
            changed = True
    return [r for r in rows if r is not None]


def _wildcard_hits(pat, name, declared):
    """通配模式是否命中本供应商（供应商名或它的任一模型）。"""
    if fnmatch.fnmatchcase(name, pat.partition('/')[0]):
        return True
    for mid in declared or ["*"]:
        if fnmatch.fnmatchcase(f"{name}/{mid}", pat):
            return True
    return False


def _provider_entry(b, blocked_reason, lines):
    """把扫描结果转成页面用的条目（名字、模型 id、可删性与将被清掉的引用）。

    这里必须与删除接口用同一套判定，否则页面会显示可删、点下去却被拒。
    引用不再阻断删除：能精确清除的列进 cleared，清不掉的列进 unsupported
    并把 deletable 置为 False（避免页面承诺一件做不到的事）。
    """
    item = {"name": b["name"], "models": b["models"],
            "deletable": False, "reasonCode": None,
            "cleared": [], "unsupported": [], "enabledEmptied": False}
    if OVERRIDE_ONLY & b["keys"]:
        item["reasonCode"] = "builtin_override"
    elif b.get("flow_unparsed"):
        item["reasonCode"] = "unsupported"
        item["unsupported"] = ["单行 flow 写法里的 models 无法可靠解析，请手工编辑"]
    elif not PROVIDER_NAME.fullmatch(b["name"]):
        item["reasonCode"] = "invalid_name"
    elif blocked_reason:
        item["reasonCode"] = "unavailable"
        item["unsupported"] = [blocked_reason]
    else:
        try:
            _yaml_anchor_risk(lines, b["start"], b["end"])
        except UnsafeStructure as e:
            item["reasonCode"] = "unsupported"
            item["unsupported"] = [str(e)]
            return item
        try:
            cleared, unsupported, _lines = _custom_references(
                b["name"], b["models"])
        except (ReferenceCheckError, CatalogUnavailable) as e:
            item["reasonCode"] = "unavailable"
            item["unsupported"] = [str(e)]
            return item
        item["cleared"] = cleared
        if unsupported:
            item["reasonCode"] = "unsupported"
            item["unsupported"] = unsupported
            return item
        item["deletable"] = True
        item["enabledEmptied"] = _enabled_emptied(_lines, cleared)
    return item


def _enabled_entries(lines):
    """enabledModels 的当前条目与「所在行号」，块列表与 flow 写法都支持。

    返回 (total, {行号})；flow 写法整行一条，行号就是该行。结构不可解析时返回
    (None, set())，调用方据此跳过判空（真出现这种情况删除会先报 unsupported）。
    """
    blk = enabled_block(lines)
    if blk and blk[2] is not None:
        return len(blk[2]), set(enabled_item_rows(lines))
    path_at = _config_paths(lines)
    for i, line in enumerate(lines):
        if path_at(i) != "enabledModels":
            continue
        span = _value_span(line.rstrip("\r\n"))
        if span is None:
            return None, set()
        # 只取值区间（不含行尾注释），否则 `[...] # 说明` 会被当成非 flow
        start, end = span[0], span[1]
        value = line.rstrip("\r\n")[start:end].strip()
        if not value.startswith("[") or not value.endswith("]"):
            return None, set()
        parts = _split_flow(value[1:-1])
        if parts is None:
            return None, set()
        return len(parts), {i}
    return 0, set()


def _enabled_emptied(lines, cleared):
    """这份清理方案是否会把 enabledModels 摘空（空名单 = omp 显示全部模型）。

    块列表与 flow 写法共用同一判定（#8），结果同时驱动确认框警示与删除响应。
    """
    total, rows = _enabled_entries(lines)
    if not total or not rows:
        return False
    # 只有「整行被删掉」的操作才会让条目消失；after 非 None 表示该行仍有剩余条目
    dropped = {op["line"] for op in cleared
               if op["path"] == "enabledModels" and op["after"] is None}
    if not dropped:
        return False
    if total == 1:
        # flow 写法：整行一条，删掉该行即摘空
        return rows <= dropped
    return rows <= dropped


def _config_digest(raw):
    """config.yml 的摘要；文件不存在用固定的「空」摘要，便于强制绑定。"""
    if raw is None:
        return hashlib.sha256(b"<absent>").hexdigest()
    return hashlib.sha256(raw).hexdigest()


def custom_providers_state():
    """自定义供应商列表（只有名字与模型 id，绝不含连接配置或密钥）。"""
    path = _models_file()
    out = {"file": str(path), "providers": [], "error": None, "digest": None,
           "configDigest": None}
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return out
    except OSError as e:
        out["error"] = f"无法读取 {path.name}: {e}"
        return out
    out["digest"] = hashlib.sha256(raw).hexdigest()
    lines = raw.decode("utf-8-sig", errors="replace").splitlines(keepends=True)
    try:
        blocks = _provider_blocks(lines)
    except UnsafeStructure as e:
        out["error"] = f"{path.name} 超出本页能安全重写的结构范围：{e}"
        return out
    blocked_reason = _provider_gate()
    with _config_lock:                    # 引用判定必须与写入用同一把锁
        # 确认清单绑定 config 快照：页面据此确认「我看到的就是磁盘上的」，
        # 外部编辑器或另一个页面新增引用后，旧视图的删除会被判 stale。
        try:
            out["configDigest"] = _config_digest(CONFIG.read_bytes())
        except FileNotFoundError:
            out["configDigest"] = _config_digest(None)
        except OSError:
            out["configDigest"] = None    # 读不到 → 页面不发请求，删除会要求摘要
        for b in blocks:
            out["providers"].append(_provider_entry(b, blocked_reason, lines))
    return out


def _split_text(raw):
    """返回 (bom, eol, lines)；保留原文件的 BOM 与行尾风格。"""
    bom = raw.startswith(b"\xef\xbb\xbf")
    eol = "\r\n" if b"\r\n" in raw else "\n"
    return bom, eol, raw.decode("utf-8-sig").splitlines(keepends=True)


def _atomic_write(path, data):
    """同目录临时文件 + 原子替换；异常时不会留下写了一半的目标文件。"""
    tmp = path.with_name(path.name + ".tmp-settings-web")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _restore(path, data):
    """把已知的旧内容写回（回滚用）。失败直接抛，不吞。"""
    _atomic_write(path, data)


def _backup_for(path, data=None):
    """备份到 <name>.bak-settings-web-<stamp>；data 给出时按该内容写。"""
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = path.with_name(f"{path.name}.bak-settings-web-{stamp}")
    if data is None:
        shutil.copy2(path, backup)
    else:
        with open(backup, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    return backup


def _collapse_providers(lines, head, start, stop, eol):
    """删空 providers 后留一个空映射，避免 `providers:` 落成 null。

    只删除目标块 [start, stop)，标题行换成 `providers: {}`；标题行与目标块之间
    的同级注释（用户写在该供应商上方的说明）、以及块之后的注释都原样保留。
    """
    body = lines[head].rstrip("\r\n")
    m = re.match(r'^providers[ \t]*:[ \t]*(#.*)?$', body)
    comment = f" {m.group(1)}" if m and m.group(1) else ""
    return (lines[:head] + [f"providers: {{}}{comment}{eol}"]
            + lines[head + 1:start] + lines[stop:])


def _strip_enabled(raw, remove):
    """从 enabledModels 里摘掉指定条目，其余行（含格式与注释）原样保留。"""
    bom = raw.startswith(b"\xef\xbb\xbf")
    lines = raw.decode("utf-8-sig").splitlines(keepends=True)
    blk = enabled_block(lines)
    if not blk or blk[2] is None:
        raise UnsafeStructure("enabledModels 不是本页能安全改写的结构")
    rows = enabled_item_rows(lines)
    drop = {rows[i] for i, pat in enumerate(blk[2]) if i < len(rows) and pat in remove}
    kept = [ln for i, ln in enumerate(lines) if i not in drop]
    if len(kept) == len(lines):
        return None
    return (b"\xef\xbb\xbf" if bom else b"") + "".join(kept).encode("utf-8")


def delete_custom_provider(payload):
    """删除本地 models.yml 里的一个自定义供应商定义（含它专属的 enabledModels 条目）。

    返回 {ok, code?, error?, references?, enabledRemoved?}；code 取值
    invalid / stale / gone / unsupported / referenced / unavailable / broker / partial。

    整个过程在读/查/写都在同一把锁里完成：
    1. 校验摘要，切块，检查 YAML 锚点与引用（引用判定与写入基于同一份快照）；
    2. 先把 models.yml 与可能被改的 config.yml 的新内容与备份都准备好；
    3. 再依次替换；任一步失败就按备份恢复，恢复再失败则如实报告 partial 与
       可直接使用的备份路径，绝不谎称已恢复。
    """
    name = payload.get("provider")
    digest = payload.get("expectDigest")
    config_digest = payload.get("expectConfigDigest")
    if not isinstance(name, str) or not PROVIDER_NAME.fullmatch(name):
        return {"ok": False, "code": "invalid", "error": "非法的供应商名"}
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        return {"ok": False, "code": "invalid",
                "error": "缺少当前文件摘要，请刷新后重试"}
    # config 摘要必填：确认清单就是按这份 config 生成的，缺了它就无法判断
    # 「用户看到过的引用」是否仍然是磁盘上的那些。
    if not isinstance(config_digest, str) or \
            not re.fullmatch(r'[0-9a-f]{64}', config_digest):
        return {"ok": False, "code": "invalid",
                "error": "缺少 config.yml 摘要，请刷新后重试"}
    blocked_reason = _provider_gate()
    if blocked_reason:
        return {"ok": False, "code": "unavailable", "error": blocked_reason}
    path = _models_file()
    with _models_lock, _config_lock:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {"ok": False, "code": "gone", "error": "没有找到供应商定义文件"}
        except OSError as e:
            return {"ok": False, "code": "unavailable",
                    "error": f"无法读取 {path.name}: {e}"}
        if hashlib.sha256(raw).hexdigest() != digest:
            return {"ok": False, "code": "stale",
                    "error": "模型定义文件已变化，请刷新后重试"}
        try:
            cfg_bytes = CONFIG.read_bytes()
        except FileNotFoundError:
            cfg_bytes = None              # 配置尚未生成：用固定摘要表示「不存在」
        except OSError as e:
            return {"ok": False, "code": "unavailable",
                    "error": f"无法读取 config.yml：{e}"}
        # 页面列出的「将被清空的引用」必须对应磁盘上的这份 config：外部编辑器或
        # 另一页面在期间新增了引用，旧清单就不作数了。config 不存在也有确定摘要，
        # 否则「确认后新建 config」就能绕过绑定。
        if _config_digest(cfg_bytes) != config_digest:
            return {"ok": False, "code": "stale",
                    "error": "config.yml 已变化，请刷新后重试"}
        # 后续清理方案与写入都基于这份已核验字节，绝不再读一次（否则检查与使用
        # 之间被外部改动，最终比对会自洽通过，从而静默清掉新引用）。
        cfg_snapshot_lines = (cfg_bytes or b"").decode("utf-8-sig").splitlines(keepends=True)
        bom, eol, lines = _split_text(raw)
        try:
            blocks = _provider_blocks(lines)
        except UnsafeStructure as e:
            return {"ok": False, "code": "unsupported", "error": str(e)}
        target = next((b for b in blocks if b["name"] == name), None)
        if target is None:
            return {"ok": False, "code": "gone",
                    "error": "该供应商定义已不在模型定义文件中（可能已被删除），请刷新"}
        if OVERRIDE_ONLY & target["keys"]:
            return {"ok": False, "code": "invalid",
                    "error": "这个块含 modelOverrides（内置模型覆盖设置），"
                             "本页不删除，请手工编辑"}
        if target.get("flow_unparsed"):
            return {"ok": False, "code": "unsupported",
                    "error": "单行 flow 写法里的 models 无法可靠解析，请手工编辑"}
        try:
            _yaml_anchor_risk(lines, target["start"], target["end"])
        except UnsafeStructure as e:
            return {"ok": False, "code": "unsupported", "error": str(e)}
        try:
            cleared, unsupported, cfg_lines = _custom_references(
                name, target["models"], cfg_snapshot_lines)
        except (ReferenceCheckError, CatalogUnavailable) as e:
            return {"ok": False, "code": "unavailable", "error": str(e)}
        if unsupported:
            # 能看出引用但无法逐字节安全摘除：宁可拒绝，也不留悬空引用或半清状态
            return {"ok": False, "code": "unsupported",
                    "unsupported": unsupported,
                    "error": "以下位置的引用无法自动清除，请手工处理后重试："
                             + "；".join(unsupported)}
        if [b for b in blocks if b is not target]:
            left = lines[:target["start"]] + lines[target["end"]:]
        else:
            # 删掉最后一个供应商：换成空映射，不留 null；块后的注释仍保留
            head, _start, _end, _indent = _providers_region(lines)
            left = _collapse_providers(lines, head, target["start"],
                                       target["end"], eol)
        new_models = (b"\xef\xbb\xbf" if bom else b"") + "".join(left).encode("utf-8")

        # ---- 先把要写的两份内容算好，再动手 ----
        writes = [(path, new_models)]
        # 无论是否需要清理 enabledModels，写入前都核对引用判定所用的 config 快照
        # 仍是磁盘上的最新内容（同一把锁内，另一页面不可能插进来；这里防的是
        # 手工改文件、外部编辑器等非本进程的改动）。
        try:
            cfg_now = CONFIG.read_bytes()
        except FileNotFoundError:
            cfg_now = None
        except OSError as e:
            return {"ok": False, "code": "unavailable",
                    "error": f"无法再次读取 config.yml：{e}"}
        if cfg_now != cfg_bytes:
            return {"ok": False, "code": "stale",
                    "error": "config.yml 在检查期间被修改，请刷新后重试"}
        cfg_bom = bool(cfg_bytes) and cfg_bytes.startswith(b"\xef\xbb\xbf")
        cfg_eol = "\r\n" if cfg_bytes and b"\r\n" in cfg_bytes else "\n"
        enabled_emptied = False
        if cleared:
            try:
                cfg_rows = _rewrite_config(cfg_lines, cleared, cfg_eol)
            except UnsafeStructure as e:
                return {"ok": False, "code": "unsupported",
                        "error": f"config.yml 无法安全改写：{e}"}
            cfg_new = (b"\xef\xbb\xbf" if cfg_bom else b"") + \
                "".join(cfg_rows).encode("utf-8")
            # 名单是否被摘空：空名单在 omp 里等于「全部显示」。这里与状态接口
            # 用同一套判定（_enabled_emptied 支持块列表与 flow 两种写法），
            # 保证确认框的警示与删除结果一致。
            enabled_emptied = _enabled_emptied(cfg_lines, cleared)
            writes.append((CONFIG, cfg_new))

        # ---- 备份必须先全部成功，之后才有任何替换 ----
        originals, backups = {}, []
        try:
            for target_path, _data in writes:
                originals[target_path] = target_path.read_bytes()
                backups.append((target_path, _backup_for(target_path)))
        except OSError as e:
            for backup in [b for _, b in backups]:
                try:
                    backup.unlink()
                except OSError:
                    pass
            return {"ok": False, "code": "unavailable",
                    "error": f"写入前备份失败，未做任何修改：{e}"}

        done = []
        try:
            for target_path, data in writes:
                _atomic_write(target_path, data)
                done.append(target_path)
        except Exception as e:
            failed, unrecovered = [], []
            for target_path in reversed(done):
                try:
                    _restore(target_path, originals[target_path])
                except Exception:
                    unrecovered.append(str(target_path))
            if unrecovered:
                return {
                    "ok": False, "code": "partial",
                    "error": (f"写入失败（{e}），且以下文件未能自动恢复："
                              + "、".join(unrecovered)
                              + "。请用同一目录下的备份手工恢复："
                              + "、".join(str(b) for _, b in backups)),
                    "backups": [str(b) for _, b in backups],
                    "unrestored": unrecovered,
                }
            return {"ok": False, "code": "unavailable",
                    "error": f"写入失败，已按备份恢复原内容：{e}"}
        return {"ok": True, "removed": name, "models": target["models"],
                "cleared": cleared, "enabledEmptied": enabled_emptied,
                "backups": [str(b) for _, b in backups]}


# 只托管 retry 顶层块/单行 flow map；fallbackChains 仅接受
# 块 map、inline [] 数组或显式 null，其余结构一律拒绝改写。

_MODEL_PATH = r'[A-Za-z0-9_.\-]+/(?:[A-Za-z0-9_.\-]+/)*[A-Za-z0-9_.*\-]+(?::[A-Za-z0-9_.*\-]+)?'
_THINKING = r'(?::(?:off|minimal|low|medium|high|xhigh|max|auto|inherit))?'
CHAIN_KEY = re.compile(r'(?:[A-Za-z0-9_.\-]+|' + _MODEL_PATH + r')' + _THINKING)
CHAIN_SELECTOR = re.compile(_MODEL_PATH + _THINKING)
FALLBACK_DEFAULTS = {"modelFallback": True, "fallbackOn": "any",
                     "fallbackRevertPolicy": "cooldown-expiry"}
FALLBACK_SCALARS = ("modelFallback", "fallbackOn", "fallbackRevertPolicy")


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in '"\'':
        if s[0] == '"':
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return s[1:-1]
        return s[1:-1]
    return s


def _yaml_text(s):
    s, _ = split_comment(s)
    return _unquote(s)


def _split_flow(s):
    """按顶层逗号切 flow 内容，跳过引号与 []/{} 嵌套；不配对返回 None。"""
    parts, depth, inq, cur = [], 0, None, ''
    for ch in s:
        if inq:
            cur += ch
            if ch == inq:
                inq = None
            continue
        if ch in '"\'':
            inq = ch
        elif ch in '[{':
            depth += 1
        elif ch in ']}':
            depth -= 1
        elif ch == ',' and depth == 0:
            parts.append(cur)
            cur = ''
            continue
        cur += ch
    if inq or depth != 0:
        return None
    parts.append(cur)
    return [p for p in parts if p.strip()]


def _split_flow_pair(p):
    """在顶层找第一个冒号切 key:value；非合法 flow 对返回 None。"""
    depth, inq = 0, None
    for i, ch in enumerate(p):
        if inq:
            if ch == inq:
                inq = None
        elif ch in '"\'':
            inq = ch
        elif ch in '[{':
            depth += 1
        elif ch in ']}':
            depth -= 1
        elif ch == ':' and depth == 0:
            return p[:i], p[i + 1:]
    return None


def _parse_flow_seq(s):
    """解析 '[a, b]'；返回 (items 或 None, ok)。ok=False 表示根本不是 '[' 开头。"""
    s = s.strip()
    if not s.startswith('['):
        return None, False
    if not s.endswith(']'):
        return None, False
    parts = _split_flow(s[1:-1])
    if parts is None:
        return None, True
    return [_yaml_text(p) for p in parts], True


def _parse_flow_map(s):
    """解析 '{k: v, ...}' 单级 flow map；返回 (dict 或 None, ok)。"""
    s = s.strip()
    if not s.startswith('{') or not s.endswith('}'):
        return None, False
    parts = _split_flow(s[1:-1])
    if parts is None:
        return None, True
    out = {}
    for p in parts:
        pair = _split_flow_pair(p)
        if pair is None:
            return None, True
        k, v = pair
        out[_unquote(k)] = v.strip()
    return out, True


def _flow_chains(vclean):
    """把 inline 值（flow map 或 null/[] 空形式）解析成 {key: chain}；
    非 map、成员非序列、或结构非法返回 None。"""
    if not vclean or vclean in ('null', '~', '{}', '[]'):
        return {}
    sub, ok = _parse_flow_map(vclean)
    if not ok or sub is None:
        return None
    out = {}
    for ck, cv in sub.items():
        arr, ok = _parse_flow_seq(cv)
        if not ok or arr is None:
            return None
        out[ck] = arr or []
    return out


def _fc_children(lines, head_line, head_indent):
    """扫描块 map 形态的 fallbackChains 子键。
    返回 (end, {key: {'chain': [...], 'seq': 是否块序列, 'ks', 'ke'}})；
    结构不支持返回 (head_line, None)。ke 为包块序列子项的排他结束行。"""
    entries, j = {}, head_line + 1
    while j < len(lines):
        ln = lines[j]
        if not ln.strip() or ln.lstrip().startswith('#'):
            j += 1
            continue
        lead = len(ln) - len(ln.lstrip(' '))
        if lead <= head_indent:
            break
        km = KEY_LINE.match(ln)
        if km is None:
            return head_line, None
        kname = _unquote(km.group(2).strip())
        kval, _ = split_comment(km.group(3) or '')
        kval = kval.strip()
        ks, ke = j, j + 1
        if not kval:
            # 空值 → 后跟更深缩进的 - 块序列；否则视同空链 []
            chain, seq, j2 = [], False, j + 1
            while j2 < len(lines):
                ln2 = lines[j2]
                if not ln2.strip():
                    j2 += 1
                    continue
                if ln2.lstrip().startswith('#'):
                    j2 += 1
                    continue
                if (len(ln2) - len(ln2.lstrip(' '))) <= lead:
                    break
                im = LIST_ITEM.match(ln2)
                if im is None:
                    return head_line, None   # 键下嵌套 map/标量，不托管
                seq = True
                v = _yaml_text(im.group(1))
                if v.startswith(('{', '[')):
                    return head_line, None
                chain.append(v)
                j2 += 1
            # Leave comments between this chain and the next key outside its range.
            ke = j2
            while ke > ks + 1 and (not lines[ke - 1].strip() or
                                   lines[ke - 1].lstrip().startswith('#')):
                ke -= 1
            entries[kname] = {'chain': chain, 'seq': seq, 'ks': ks, 'ke': ke}
            j = ke
            continue
        arr, ok = _parse_flow_seq(kval)
        if not ok or arr is None:
            return head_line, None  # 多行标量/嵌套/引用等，不托管
        entries[kname] = {'chain': arr or [], 'seq': False, 'ks': ks, 'ke': ke}
        j += 1
    return j, entries


def _retry_region(idx, lines):
    """返回 (retry 行, retry 缩进, retry 块结束行)；retry 非块返回 None。"""
    info = idx.get('retry')
    if info is None:
        return None
    m = KEY_LINE.match(lines[info['line']])
    value, _ = split_comment(m.group(3) or '')
    if value.strip():
        return None
    indent = len(lines[info['line']]) - len(lines[info['line']].lstrip(' '))
    j = info['line'] + 1
    while j < len(lines):
        ln = lines[j]
        if ln.strip() and not ln.lstrip().startswith('#') and \
           (len(ln) - len(ln.lstrip(' '))) <= indent:
            break
        j += 1
    return info['line'], indent, j


def _retry_flow_parse(m):
    """解析 retry 行上的 flow map 值，返回 (fmap, chains, scalars) 或 None。
    fmap 为 {key: 原值文本}；chains 是 fallbackChains 的 {key: chain}。"""
    raw, _ = split_comment(m.group(3) or '')
    fmap, ok = _parse_flow_map(raw)
    if not ok or fmap is None:
        return None
    chains, scalars = {}, {}
    for k, v in fmap.items():
        if k == 'fallbackChains':
            c = _flow_chains(v)
            if c is None:
                return None
            chains = c
        elif k in FALLBACK_SCALARS:
            scalars[k] = _yaml_text(v)
    return fmap, chains, scalars


def _scalar_bool(v):
    return str(v).strip().lower() in ('true', 'yes', 'on', '1')


def _scalar_choice(v, options, default):
    v = str(v).strip()
    return v if v in options else default


def fallback_state():
    """读 retry.fallbackChains 与相关标量；不支持的 YAML 返回 {"error": ...}。"""
    try:
        raw = CONFIG.read_bytes().decode('utf-8')
    except OSError as e:
        return {"error": f"无法读取 {CONFIG}: {e}"}
    lines = raw.splitlines(keepends=True)
    idx = index_config(lines)
    chains, scalars = {}, {}
    info = idx.get('retry')
    if info:
        m = KEY_LINE.match(lines[info['line']])
        vclean, _ = split_comment(m.group(3) or '') if m else ('', '')
        vclean = vclean.strip()
        if vclean:
            # retry: {…} 单行 flow map / JSON inline map
            r = _retry_flow_parse(m)
            if r is None:
                return {"error": "config.yml 的 retry 含不支持的 YAML 结构，"
                                "请手工编辑"}
            _, chains, scalars = r
        else:
            reg = _retry_region(idx, lines)
            if reg is None:
                return {"error": "无法定位 retry 块"}
            _, _, rend = reg
            fc = idx.get('retry.fallbackChains')
            if fc and fc['line'] < rend:
                fm = KEY_LINE.match(lines[fc['line']])
                fval, _ = split_comment(fm.group(3) or '') if fm else ('', '')
                fval = fval.strip()
                if not fval:
                    fci = len(lines[fc['line']]) - \
                        len(lines[fc['line']].lstrip(' '))
                    _, entries = _fc_children(lines, fc['line'], fci)
                    if entries is None:
                        return {"error": "config.yml 的 fallbackChains 含不支持的"
                                        " YAML 结构，请手工编辑"}
                    chains = {k: e['chain'] for k, e in entries.items()}
                else:
                    c = _flow_chains(fval)
                    if c is None:
                        return {"error": "config.yml 的 fallbackChains 含不支持"
                                        "的 YAML 结构，请手工编辑"}
                    chains = c
            for s in FALLBACK_SCALARS:
                si = idx.get(f'retry.{s}')
                if si and si['kind'] == 'scalar' and si['line'] < rend:
                    sm = KEY_LINE.match(lines[si['line']])
                    if sm:
                        scalars[s] = _yaml_text(sm.group(3) or '')
    return {"chains": chains,
            "modelFallback": _scalar_bool(
                scalars.get("modelFallback", FALLBACK_DEFAULTS["modelFallback"])),
            "fallbackOn": _scalar_choice(
                scalars.get("fallbackOn"),
                ("any", "usage-limit", "except-usage-limit"),
                FALLBACK_DEFAULTS["fallbackOn"]),
            "fallbackRevertPolicy": _scalar_choice(
                scalars.get("fallbackRevertPolicy"),
                ("cooldown-expiry", "never"),
                FALLBACK_DEFAULTS["fallbackRevertPolicy"])}


def set_fallback_chain(key, chain):
    with _config_lock:
        return _set_fallback_chain(key, chain)


def _set_fallback_chain(key, chain):
    """写入/删除 retry.fallbackChains 下的一个键。
    chain=None 删除该键（表示未配置），[] 显式禁用。只改这一个键。"""
    key = (key or '').strip() if isinstance(key, str) else ''
    if not CHAIN_KEY.fullmatch(key):
        return "非法链键名（允许 provider/model-id、provider/* 或角色名）"
    if chain is not None:
        if not isinstance(chain, list) or \
           not all(isinstance(s, str) for s in chain):
            return "chain 必须是 selector 数组、null 或 []"
        for s in chain:
            if not CHAIN_SELECTOR.fullmatch(s.strip()):
                return (f"非法 selector: {s!r}（须为 provider/model-id，"
                        f"可带 thinking 后缀）")
    try:
        raw = CONFIG.read_bytes().decode('utf-8')
    except OSError as e:
        return f"无法读取 {CONFIG}: {e}"
    eol = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.splitlines(keepends=True)
    body = None
    if chain is not None:
        items = ', '.join(yaml_scalar(s.strip()) for s in chain)
        body = f"{key}: [{items}]{eol}"  # 不带缩进的键行，插入时加 findent

    idx = index_config(lines)
    info = idx.get('retry')

    if info is None:
        if chain is None:
            return None  # 未配置：无需写入
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += eol
        lines.extend([f"retry:{eol}", f"  fallbackChains:{eol}",
                      f"    {body}"])
        write_backup()
        CONFIG.write_bytes(''.join(lines).encode('utf-8'))
        return None

    m = KEY_LINE.match(lines[info['line']])
    rval, _ = split_comment(m.group(3) or '') if m else ('', '')
    rval = rval.strip()

    if rval:
        # retry 是单行值：flow map 可整体改写为块；非 {} 的裸标量拒绝
        if rval and not rval.startswith('{'):
            return "config.yml 的 retry 含不支持的 YAML 结构，请手工编辑"
        r = _retry_flow_parse(m)
        if r is None:
            return "config.yml 的 retry flow map 含不支持的结构，请手工编辑"
        fmap, chains, _scalars = r
        if chain is None:
            if key not in chains:
                return None
            del chains[key]
        else:
            chains[key] = [s.strip() for s in chain]
        indent0 = m.group(1)
        sub = ' ' * (len(indent0) + 2)
        _, comment = split_comment(m.group(3) or '')
        note = ' ' + comment if comment else ''
        new = [f"{indent0}retry:{note}{eol}"]
        if 'fallbackChains' not in fmap and chains:
            fmap['fallbackChains'] = '{}'
        for k, v in fmap.items():
            if k == 'fallbackChains':
                if chains:
                    new.append(f"{sub}fallbackChains:{eol}")
                    new.extend(
                        f"{sub}  {ck}: [{', '.join(yaml_scalar(s) for s in cv)}]"
                        f"{eol}" for ck, cv in chains.items())
            else:
                new.append(f"{sub}{k}: {v}{eol}")
        lines[info['line']:info['line'] + 1] = new
        write_backup()
        CONFIG.write_bytes(''.join(lines).encode('utf-8'))
        return None

    reg = _retry_region(idx, lines)
    if reg is None:
        return "无法定位 retry 块"
    rline, rindent, rend = reg
    cindent = ' ' * (rindent + 2)   # retry 直接子键缩进
    findent = ' ' * (rindent + 4)   # fallbackChains 子键缩进
    fc = idx.get('retry.fallbackChains')
    if fc and fc['line'] >= rend:
        fc = None

    fc_kind = None   # None | 'block' | 'inline' | 'refuse'
    fc_entries = None
    fc_inline = None
    if fc:
        fm = KEY_LINE.match(lines[fc['line']])
        fval, fcmt = split_comment(fm.group(3) or '') if fm else ('', '')
        fval = fval.strip()
        fcmt = (' ' + fcmt) if fcmt else ''
        if not fval:
            fci = len(lines[fc['line']]) - len(lines[fc['line']].lstrip(' '))
            _, fc_entries = _fc_children(lines, fc['line'], fci)
            fc_kind = 'refuse' if fc_entries is None else 'block'
        else:
            c = _flow_chains(fval)
            fc_kind = 'refuse' if c is None else 'inline'
            fc_inline = c
        if fc_kind == 'refuse':
            return ("config.yml 的 fallbackChains 含不支持的 YAML 结构"
                    "（嵌套/混排/非标量值），请手工编辑")

    ent = (fc_entries or {}).get(key) if fc_kind == 'block' else None

    if chain is None:
        if fc_kind == 'block':
            if ent is None:
                return None
            preserved = [line for line in lines[ent['ks']:ent['ke']]
                         if line.lstrip().startswith('#')]
            lines[ent['ks']:ent['ke']] = preserved
            # 删空了整个 map → 连 fallbackChains 头一起去掉
            if not any(k != key for k in fc_entries):
                del lines[fc['line']]
        elif fc_kind == 'inline':
            if key not in fc_inline:
                return None
            del fc_inline[key]
            fm = KEY_LINE.match(lines[fc['line']])
            if fc_inline:
                inner = ', '.join(
                    f"{yaml_scalar(ck)}: "
                    f"[{', '.join(yaml_scalar(s) for s in cv)}]"
                    for ck, cv in fc_inline.items())
                lines[fc['line']] = \
                    f"{fm.group(1)}fallbackChains: {{{inner}}}{fcmt}{eol}"
            else:
                del lines[fc['line']]
        else:
            return None  # 没有该键，也没有 fallbackChains
        write_backup()
        CONFIG.write_bytes(''.join(lines).encode('utf-8'))
        return None

    if ent is not None:
        old = KEY_LINE.match(lines[ent['ks']])
        _, comment = split_comment(old.group(3) or '')
        note = ' ' + comment if comment else ''
        replacement = f"{old.group(1)}{key}: [{items}]{note}{eol}"
        preserved = [line for line in lines[ent['ks'] + 1:ent['ke']]
                     if line.lstrip().startswith('#')]
        lines[ent['ks']:ent['ke']] = [replacement, *preserved]
    elif fc_kind == 'block' and fc is not None:
        pos = fc['line'] + 1
        for e in fc_entries.values():
            pos = max(pos, e['ke'])
        lines[pos:pos] = [f"{findent}{body}"]
    elif fc_kind == 'inline' and fc is not None:
        fc_inline[key] = [s.strip() for s in chain]
        fm = KEY_LINE.match(lines[fc['line']])
        inner = ', '.join(
            f"{yaml_scalar(ck)}: [{', '.join(yaml_scalar(s) for s in cv)}]"
            for ck, cv in fc_inline.items())
        lines[fc['line']] = \
            f"{fm.group(1)}fallbackChains: {{{inner}}}{fcmt}{eol}"
    elif fc is not None:
        # 不应到达（refuse 已提前返回）
        return "config.yml 的 fallbackChains 结构不可写"
    else:
        lines[rline + 1:rline + 1] = [f"{cindent}fallbackChains:{eol}",
                                      f"{findent}{body}"]
    write_backup()
    CONFIG.write_bytes(''.join(lines).encode('utf-8'))
    return None

# ---------- Ctrl+P 轮换顺序（cycleOrder） ----------

CYCLE_DEFAULT = ["smol", "default", "slow"]
CYCLE_HEAD = re.compile(r'^cycleOrder:(?:[ \t]+([^\r\n]*?))?[ \t]*(\r?\n)?$')
CYCLE_ROLE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.\-]*')


def _cycle_block(lines):
    """定位顶层 cycleOrder：返回 (首行, 尾后行, 条目, 首行注释, 列表内注释行)；
    不存在返回 None；结构不支持时条目为 None。"""
    for i, line in enumerate(lines):
        m = CYCLE_HEAD.match(line)
        if not m:
            continue
        value, comment = split_comment(m.group(1) or '')
        value = value.strip()
        if value:
            arr, ok = _parse_flow_seq(value)
            if not ok or arr is None or any(':' in v for v in arr):
                return i, i + 1, None, comment, []
            return i, i + 1, arr, comment, []
        entries, notes, end, j = [], [], i + 1, i + 1
        while j < len(lines):
            ln = lines[j]
            if not ln.strip() or ln.lstrip().startswith('#'):
                j += 1
                continue
            if not ln[0].isspace():
                break
            item = LIST_ITEM.match(ln)
            raw, _ = split_comment(item.group(1)) if item else ('', '')
            if item is None or raw.startswith(('{', '[')) or \
               (':' in raw and raw[:1] not in '"\''):
                return i, i + 1, None, comment, []
            notes.extend(x for x in lines[end:j] if x.lstrip().startswith('#'))
            entries.append(_unquote(raw))
            j += 1
            end = j
        return i, end, entries, comment, notes
    return None


def cycle_order_state():
    """读 cycleOrder；未配置时返回 omp 默认顺序。"""
    try:
        lines = CONFIG.read_bytes().decode('utf-8').splitlines(keepends=True)
    except OSError as e:
        return {"error": f"无法读取 {CONFIG}: {e}"}
    blk = _cycle_block(lines)
    if blk and blk[2] is None:
        return {"error": "config.yml 的 cycleOrder 含不支持的 YAML 结构，请手工编辑"}
    if not blk:
        return {"order": list(CYCLE_DEFAULT), "custom": False,
                "default": list(CYCLE_DEFAULT)}
    head = CYCLE_HEAD.match(lines[blk[0]])
    inline = bool(head and split_comment(head.group(1) or '')[0].strip())
    custom = inline or bool(blk[2])  # 仅写 `cycleOrder:` 且无条目 = YAML null，按默认
    order = list(blk[2] or []) if custom else list(CYCLE_DEFAULT)
    return {"order": order, "custom": custom, "default": list(CYCLE_DEFAULT)}


def set_cycle_order(order):
    with _config_lock:
        return _set_cycle_order(order)


def _set_cycle_order(order):
    """order=None 删除 cycleOrder（恢复 omp 默认）；否则写入有序角色名列表。"""
    if order is not None:
        if not isinstance(order, list) or not order:
            return "轮换列表至少需要一个角色；恢复默认请使用恢复默认"
        if not all(isinstance(r, str) and CYCLE_ROLE.fullmatch(r) for r in order):
            return "角色名只能包含字母、数字、下划线、点和连字符，不能带 @"
        if len(set(order)) != len(order):
            return "轮换列表中的角色不能重复"
    try:
        raw = CONFIG.read_bytes().decode('utf-8')
    except OSError as e:
        return f"无法读取 {CONFIG}: {e}"
    eol = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.splitlines(keepends=True)
    blk = _cycle_block(lines)
    if blk and blk[2] is None:
        return "config.yml 的 cycleOrder 含不支持的 YAML 结构，请手工编辑"
    if order is None:
        if not blk:
            return None
        del lines[blk[0]:blk[1]]
    else:
        note = ' ' + blk[3] if blk and blk[3] else ''
        block = [f"cycleOrder:{note}{eol}"] + \
            [f"  - {yaml_scalar(r)}{eol}" for r in order]
        if blk:
            lines[blk[0]:blk[1]] = block + blk[4]
        else:
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += eol
            lines.extend(block)
    write_backup()
    CONFIG.write_bytes(''.join(lines).encode('utf-8'))
    return None



def list_settings():
    out = run(["omp", "config", "list", "--json"])
    try:
        catalog = json.loads(out)
    except json.JSONDecodeError:
        return {"error": "omp config list --json 解析失败", "items": []}
    raw = CONFIG.read_bytes().decode('utf-8')
    idx = index_config(raw.splitlines(keepends=True))
    zh = load_zh()
    groups = zh.get("__groups", {})
    # zh.json 没写 options 的 enum 项，用 omp 文本输出的真实值域兜底，
    # 免得页面把枚举渲染成自由文本框、写进非法值后被 omp 静默丢弃。
    fallback = None
    if any(m.get('type') == 'enum' and not (zh.get(k) or {}).get('options')
           for k, m in catalog.items()):
        fallback = enum_options_from_omp()
    items = []
    for key, meta in catalog.items():
        typ = meta.get('type', 'string')
        info = zh.get(key, {})
        opts = info.get('options')
        if typ == 'enum' and not opts and fallback:
            opts = fallback.get(key)
        items.append({
            "key": key,
            "value": meta.get('value'),
            "type": typ,
            "desc": meta.get('description', ''),
            "name": info.get('name'),
            "zh": info.get('desc'),
            "desc_en": info.get('desc_en'),
            "options": opts,
            "group": groups.get(key.split('.')[0], key.split('.')[0]),
            "custom": key in idx,
            "editable": typ in EDITABLE_TYPES,
        })
    items.sort(key=lambda x: (x["key"].split('.')[0], x["key"]))
    return {"items": items, "groups": groups, "groupmap": zh.get("__groupmap", {}),
            "groups_en": zh.get("__groups_en", {})}


# ---------- 用量（omp usage --json） ----------

def usage_report():
    """跑 `omp usage --json` 并透传报告；失败返回 {"error": ...}。
    报告中 metadata.email 属本机账号信息，本地面板原样展示；
    但凭据/token 从不进入该 JSON（omp 自己也不输出）。"""
    try:
        p = subprocess.run(
            omp_cmd(["omp", "usage", "--json", "--no-extensions"]),
            capture_output=True, text=True, shell=False,
            encoding="utf-8", errors="replace", timeout=120, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"error": "omp usage 超时（120s）"}
    except OSError as e:
        return {"error": f"无法执行 omp: {e}"}
    if p.returncode != 0 and not p.stdout.strip():
        err = (p.stderr or "").strip().splitlines()
        return {"error": err[0] if err else f"omp usage 退出码 {p.returncode}"}
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"error": "omp usage 输出不是合法 JSON"}

def invalidate_usage(provider):
    """跑 `omp usage invalidate`（可选 --provider）。provider 只允许
    'all' 或已认证账号列表里的 provider id，白名单校验防注入。"""
    args = omp_cmd(["omp", "usage", "invalidate", "--no-extensions"])
    if provider not in (None, "", "all"):
        provider = provider.strip().lower()
        known = {str(r.get("provider", "")).lower()
                 for r in (usage_report().get("reports") or [])}
        if provider not in known:
            return {"error": f"未知供应商: {provider}"}
        args += ["--provider", provider]
    try:
        p = subprocess.run(args, capture_output=True, text=True, shell=False,
                           encoding="utf-8", errors="replace", timeout=60,
                           creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"error": "omp usage invalidate 超时（60s）"}
    except OSError as e:
        return {"error": f"无法执行 omp: {e}"}
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return {"error": err[0] if err else f"omp usage invalidate 退出码 {p.returncode}"}
    return {"ok": True, "message": (p.stdout or "").strip()}

def usage_with_resettable():
    """usage_report() 外加 resettable 标记：仅 anthropic/openai-codex
    且 resetCredits.availableCount>0 时可重置（不透 token）。"""
    r = usage_report()
    if "error" in r:
        return r
    for rep in r.get("reports") or []:
        rc = rep.get("resetCredits")
        rep["resettable"] = rep.get("provider") in REDEEM_PROVIDERS \
            and isinstance(rc, dict)
    return r


# ---------- 多账号：账号列表 + auth.accountPolicies ----------
# 账号身份来自 `omp usage accounts --json`（无 token），额度来自 `omp usage --json`；
# 从不读取 agent.db 的凭据字段。策略只托管 auth.accountPolicies 的已知块结构：
#   accountPolicies:
#     - provider: <id>
#       account:
#         email|accountId|projectId|orgId: <值>
#       priority: <整数>
#       reservePct: <0-100>
#       reclaimAbovePct: <大于 reservePct 的百分比，omp ≥18.12.4>
# 出现注释、flow 写法或未知键时只读，不改写。

POLICY_PROVIDER = re.compile(r'[a-z0-9][a-z0-9_\-]{0,63}')
SELECTOR_VALUE = re.compile(r'[A-Za-z0-9_@.+:\-]{1,200}')
SELECTOR_KEYS = ("email", "accountId", "projectId", "orgId")
POLICY_SCALARS = ("priority", "reservePct", "reclaimAbovePct")
_ITEM_LINE = re.compile(r'^([ \t]*)-[ \t]+([A-Za-z]+):[ \t]*([^\r\n]*?)[ \t]*(\r?\n)?$')
_SUB_LINE = re.compile(r'^([ \t]*)([A-Za-z]+):[ \t]*([^\r\n]*?)[ \t]*(\r?\n)?$')


def _num(s):
    if re.fullmatch(r'-?\d+', s):
        return int(s)
    if re.fullmatch(r'-?\d+\.\d+', s):
        return float(s)
    raise ValueError(s)


def _policies_block(lines):
    """定位 auth.accountPolicies。返回 dict：
    auth=auth 行或 None；head=accountPolicies 行或 None；end=块尾后行；
    indent=accountPolicies 缩进；items=解析结果（不支持时 None）；error=只读原因。"""
    idx = index_config(lines)
    auth = idx.get('auth')
    if auth and auth['kind'] != 'block':
        return {"error": "config.yml 的 auth 不是块结构，本页只读"}
    head = idx.get('auth.accountPolicies')
    out = {"auth": auth['line'] if auth else None, "head": None, "end": None,
           "indent": None, "items": [], "error": None}
    if head is None:
        return out
    h = head['line']
    m = KEY_LINE.match(lines[h])
    indent = len(m.group(1))
    val, comment = split_comment(m.group(3) or '')
    out.update(head=h, indent=indent)
    if comment:
        out.update(items=None, error="accountPolicies 行含注释，本页只读，请手工编辑")
        return out
    if val:
        out["end"] = h + 1
        if val.strip() == '[]':
            return out
        out.update(items=None, error="accountPolicies 使用 flow 写法，本页只读，请手工编辑")
        return out
    j, items, cur, sub = h + 1, [], None, None
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            j += 1
            continue
        ind = len(line) - len(line.lstrip(' \t'))
        if ind <= indent and not line.lstrip().startswith('- '):
            break
        if ind < indent:
            break
        if '#' in split_comment(line.rstrip('\r\n'))[1] or line.lstrip().startswith('#'):
            out.update(items=None, error="accountPolicies 块内含注释，本页只读，请手工编辑")
            return out
        mi = _ITEM_LINE.match(line)
        ms = _SUB_LINE.match(line)
        try:
            if mi:
                cur = {"_ind": len(mi.group(1)) + 2, "account": {}}
                items.append(cur)
                sub = None
                k, v = mi.group(2), _unquote(mi.group(3))
            elif ms and cur is not None:
                k, v = ms.group(2), _unquote(ms.group(3))
                if len(ms.group(1)) == cur["_ind"]:
                    sub = None
                elif sub == "account" and len(ms.group(1)) > cur["_ind"]:
                    if k not in SELECTOR_KEYS or not v:
                        raise ValueError(k)
                    cur["account"][k] = v
                    j += 1
                    continue
                else:
                    raise ValueError(k)
            else:
                raise ValueError(line)
            if k == "account" and not v:
                sub = "account"
            elif k == "provider" and v:
                cur["provider"] = v
            elif k in POLICY_SCALARS and v:
                cur[k] = _num(v)
            else:
                raise ValueError(k)
        except ValueError:
            out.update(items=None, error="accountPolicies 含本页不支持的结构，只读，请手工编辑")
            return out
        j += 1
    for it in items:
        it.pop("_ind", None)
        if "provider" not in it or not it["account"]:
            out.update(items=None, error="accountPolicies 有条目缺少 provider/account，只读")
            return out
    # 块尾：回退掉末尾空行，保持块后空行不动
    while j > h + 1 and not lines[j - 1].strip():
        j -= 1
    out.update(end=j, items=items)
    return out


def _render_policies(items, indent, eol):
    pad = ' ' * indent
    if not items:
        return [f"{pad}accountPolicies: []{eol}"]
    out = [f"{pad}accountPolicies:{eol}"]
    for it in items:
        out.append(f"{pad}  - provider: {yaml_scalar(it['provider'])}{eol}")
        out.append(f"{pad}    account:{eol}")
        for k in SELECTOR_KEYS:
            if k in it["account"]:
                out.append(f"{pad}      {k}: {yaml_scalar(str(it['account'][k]))}{eol}")
        for k in POLICY_SCALARS:
            if it.get(k) is not None:
                out.append(f"{pad}    {k}: {it[k]}{eol}")
    return out


def _write_policies(items):
    """整段重写 auth.accountPolicies；调用方持有 _config_lock。块外内容逐字节不变。"""
    raw = CONFIG.read_bytes().decode('utf-8')
    eol = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.splitlines(keepends=True)
    blk = _policies_block(lines)
    if blk.get("error"):
        return blk["error"]
    if blk["head"] is not None:
        new = _render_policies(items, blk["indent"], eol)
        lines[blk["head"]:blk["end"]] = new
    elif blk["auth"] is not None:
        ind = None
        for k in lines[blk["auth"] + 1:]:
            if k.strip() and not k.lstrip().startswith('#'):
                n = len(k) - len(k.lstrip(' '))
                ind = n if n > 0 else None
                break
        lines[blk["auth"] + 1:blk["auth"] + 1] = _render_policies(items, ind or 2, eol)
    else:
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += eol
        lines.append(f"auth:{eol}")
        lines.extend(_render_policies(items, 2, eol))
    write_backup()
    CONFIG.write_bytes(''.join(lines).encode('utf-8'))
    return None


def _identity_parts(key):
    """'email:a@b|org:x' / 'account:y' → {'email':..,'orgId':..,'accountId':..,'projectId':..}"""
    names = {"email": "email", "org": "orgId", "account": "accountId", "project": "projectId"}
    out = {}
    for part in (key or "").split('|'):
        k, _, v = part.partition(':')
        if k in names and v:
            out[names[k]] = v
    return out


def _policy_matches(pol_account, ident):
    """选择器每个给出的字段都要对上；orgId 可选。

    email 按大小写不敏感比较：omp 的 `identityKey` 把 email 规范化成小写，
    而 config.yml 里写的是凭据原始大小写，两者是同一个账号。omp 自己的
    `FI()` 是逐字节精确比较，所以**写入**时必须用原始大小写（见
    `_original_identity`）；这里放宽只为识别「同一条策略」，不用于写回。
    """
    keys = [k for k in ("email", "accountId", "projectId") if k in pol_account]
    if not keys:
        return False
    for k in keys + (["orgId"] if "orgId" in pol_account else []):
        want, got = pol_account[k], ident.get(k)
        if k == "email":
            if str(want).casefold() != str(got or "").casefold():
                return False
        elif want != got:
            return False
    return True


def _run_json(args, timeout):
    try:
        p = subprocess.run(args, capture_output=True, text=True, shell=False,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=NO_WINDOW)
        return json.loads(p.stdout), None
    except subprocess.TimeoutExpired:
        return None, f"{' '.join(args[:3])} 超时"
    except (OSError, json.JSONDecodeError) as e:
        return None, f"{' '.join(args[:3])} 失败: {e}"


def list_accounts():
    return _run_json(omp_cmd(["omp", "usage", "accounts", "--json", "--no-extensions"]), 60)


# ---------- 账号凭据列表 / 注销（omp RPC） ----------
# 删除只走 omp 自己的 `logout` RPC（等价 TUI 的 /logout），绝不直接改 agent.db：
# omp 会在删除后刷新模型注册表、维护会话固定（pin）与限流状态。
# 本模块只读 agent.db 的 provider/identity_key 两列用于「同一账号不同组织」的
# 交叉核对，凭据值（data）从不读取、不返回、不落日志。

RPC_TIMEOUT = 25          # 一次 RPC 会话的总时限（秒），含启动与所有命令
RPC_MAX_LINE = 1 << 20    # 单帧上限，与 omp 的 maxFrameBytes 对齐（1 MiB）
RPC_COMMAND = ["--mode", "rpc", "--no-session", "--no-extensions", "--no-skills",
               "--no-rules", "--no-context-files", "--no-lsp", "--no-ui"]


class RpcFailure(Exception):
    """omp RPC 调用失败：code 供前端区分处理，message 为可展示文本。"""

    def __init__(self, message, code="rpc_error"):
        super().__init__(message)
        self.code = code


def _rpc_argv():
    # 与 omp_cmd 一致：不合成 --profile，配置档由继承的环境交给 omp 解析。
    return ["omp"] + RPC_COMMAND


def _rpc_spawn():
    """启动 omp RPC 子进程；stdin/stdout 用二进制以免 Windows 换行被翻译。"""
    return subprocess.Popen(_rpc_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, shell=False,
                            creationflags=NO_WINDOW)


def _rpc_session(script, timeout=RPC_TIMEOUT, spawn=None):
    """在一个 omp RPC 子进程里按顺序执行 script(send)。

    send(type, **fields) 发一条命令并阻塞等待同 id 的响应，返回响应里的 data；
    上游 success=false 时抛 RpcFailure。script 的返回值原样返回。

    stdout 由独立读线程搬到队列里，因此即使 omp 完全不回话，总的时限也一定
    生效（阻塞 readline 会让时限失效，这是曾经的真实缺陷）。子进程一定被清理：
    正常结束关 stdin 等其退出，超时先 terminate 再 kill。
    """
    proc = None
    frames = queue.Queue()
    eof = object()

    def reader(pipe):
        try:
            while True:
                line = pipe.readline()
                if not line:
                    break
                frames.put(line)
        except (OSError, ValueError):
            pass
        finally:
            frames.put(eof)

    def next_frame(stop):
        """取下一帧；超过 stop 就抛超时（读线程不会阻塞主线程）。"""
        left = stop - time.monotonic()
        if left <= 0:
            raise RpcFailure(f"omp RPC 超时（{timeout}s）", "timeout")
        try:
            item = frames.get(timeout=left)
        except queue.Empty:
            raise RpcFailure(f"omp RPC 超时（{timeout}s）", "timeout")
        if item is eof:
            raise RpcFailure("omp RPC 进程提前退出", "unavailable")
        if isinstance(item, bytes):
            if len(item) > RPC_MAX_LINE:
                raise RpcFailure("omp RPC 帧超过 1 MiB", "invalid_response")
            item = item.decode("utf-8", "replace")
        if not item.strip():
            return next_frame(stop)
        try:
            obj = json.loads(item)
        except json.JSONDecodeError:
            raise RpcFailure("omp RPC 输出不是合法 JSON", "invalid_response")
        if not isinstance(obj, dict):
            raise RpcFailure("omp RPC 帧格式异常", "invalid_response")
        return obj

    deadline = time.monotonic() + timeout

    try:
        try:
            proc = (spawn or _rpc_spawn)()
        except OSError as e:
            raise RpcFailure(f"无法启动 omp: {e}", "unavailable")

        threading.Thread(target=reader, args=(proc.stdout,), daemon=True).start()

        while True:                                  # 等 ready 帧
            frame = next_frame(deadline)
            if frame.get("type") == "ready":
                break
            if frame.get("type") == "response":
                raise RpcFailure("omp RPC 未发出就绪帧", "unavailable")

        counter = [0]

        def send(ctype, **fields):
            counter[0] += 1
            rid = f"ompweb-{counter[0]}"
            payload = dict(fields, id=rid, type=ctype)
            try:
                proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))  # type: ignore[union-attr]
                proc.stdin.flush()                                          # type: ignore[union-attr]
            except (OSError, ValueError) as e:
                raise RpcFailure(f"omp RPC 写入失败: {e}", "unavailable")
            while True:
                frame = next_frame(deadline)
                if frame.get("type") != "response" or frame.get("id") != rid:
                    continue     # 事件帧、其他响应，忽略
                if frame.get("success") is not True:
                    raise RpcFailure(_sanitize_error(frame.get("error")) or "omp RPC 失败",
                                     "upstream")
                data = frame.get("data")
                return data if isinstance(data, dict) else {}

        return script(send)
    finally:
        if proc is not None:
            try:
                proc.stdin.close()                     # type: ignore[union-attr]
            except (OSError, ValueError, AttributeError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass


def _rpc_accounts_for_providers(providers, timeout=RPC_TIMEOUT, spawn=None):
    """在同一个 RPC 会话里取每个 provider 可注销的凭据列表。"""
    def script(send):
        out = {}
        for prov in providers:
            data = send("get_logout_accounts", providerId=prov)
            accounts = data.get("accounts")
            out[prov] = [a for a in accounts if isinstance(a, dict)] \
                if isinstance(accounts, list) else []
        return out
    return _rpc_session(script, timeout=timeout, spawn=spawn)


def rpc_accounts_index(timeout=RPC_TIMEOUT, spawn=None):
    """{provider: [LogoutAccount]}，只查 omp 认为已登录的 provider。

    LogoutAccount = {credentialId:int, provider, label, detail,
                     type:'api_key'|'oauth', active:bool}；不含任何凭据值。
    """
    def script(send):
        data = send("get_login_providers")
        providers = data.get("providers")
        if not isinstance(providers, list):
            raise RpcFailure("omp RPC 未返回供应商列表", "invalid_response")
        authenticated = [str(p.get("id")) for p in providers
                         if isinstance(p, dict) and p.get("authenticated") is True
                         and isinstance(p.get("id"), str)]
        out = {}
        for prov in authenticated:
            data = send("get_logout_accounts", providerId=prov)
            accounts = data.get("accounts")
            out[prov] = [a for a in accounts if isinstance(a, dict)] \
                if isinstance(accounts, list) else []
        return out
    return _rpc_session(script, timeout=timeout, spawn=spawn)


def rpc_logout(provider, credential_id, timeout=RPC_TIMEOUT):
    """调用 omp 的 logout RPC 删除一条已存凭据（软删，不改订阅）。"""
    def script(send):
        return send("logout", providerId=provider, credentialId=credential_id)
    return _rpc_session(script, timeout=timeout)


def credential_identities():
    """{credential_id: (provider, identity_key)}：只读两列，用作删除前的交叉核对。

    读不到库时返回空 dict（调用方据此降级为只用 RPC 的 provider+ID 核对）。
    """
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT id, provider, identity_key FROM auth_credentials").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {int(r[0]): (str(r[1]), str(r[2] or "")) for r in rows
            if isinstance(r[0], int)}


def credential_original_emails():
    """{identity_key 小写: 凭据里的原始大小写 email}。

    omp 的 `identity_key` 列把 email 规范化成小写，而 `auth_credentials.data`
    里的 `email` 保留登录时的原始大小写。omp 的账号策略匹配函数 `FI()` 是逐字节
    精确比较，所以写 config.yml 时必须用原始大小写，否则策略永远不生效
    （日志会刷 `Account policy matches no stored OAuth account; ignoring it`）。
    读不到时返回空 dict，调用方回退到 identity_key 的值。
    """
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT identity_key, data FROM auth_credentials").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    out = {}
    for key, data in rows:
        try:
            email = (json.loads(data) or {}).get("email")
        except (TypeError, ValueError):
            continue
        if isinstance(email, str) and email and key:
            out[str(key).casefold()] = email
    return out


def read_config_text():
    """按字节读 config.yml；profile 尚未生成配置时视为空（只读展示，不写）。"""
    try:
        return CONFIG.read_bytes().decode('utf-8')
    except FileNotFoundError:
        return ""


def default_reserve_pct():
    lines = read_config_text().splitlines(keepends=True)
    info = index_config(lines).get('retry.usageReservePct')
    if info and info['kind'] == 'scalar':
        m = KEY_LINE.match(lines[info['line']])
        try:
            return _num(split_comment(m.group(3) or '')[0].strip()), True
        except ValueError:
            pass
    return 10, False


def deletion_gate(timeout=15):
    """统一判定「本页此刻是否允许删除凭据」。

    两项都必须成立：agent 目录由 omp 自己确认（R4），且 omp 没有把凭据托管在
    auth-broker 上（R5；查不到也算不允许）。列表与每次删除共用这一个判据，
    避免两处逻辑漂移。
    """
    broker = broker_state(timeout=timeout)
    blocked = bool(broker["blocked"]) or AGENT_DIR is None
    reason = None
    if AGENT_DIR is None:
        reason = AGENT_DIR_ERROR or "无法确定 omp 的 agent 目录"
    elif broker["blocked"]:
        reason = "omp 正在使用 auth-broker 托管凭据，本页不执行远程删除"
    return {"blocked": blocked, "broker": broker, "reason": reason}


def broker_state(timeout=15):
    """omp 是否把凭据托管在 auth-broker（远程凭据库）。

    返回 {"enabled": bool, "known": bool, "blocked": bool}：
    - enabled: broker 已配置（本地凭据不会被使用，logout 会删远程凭据）
    - known:   是否成功问到了权威答案；查不到时 blocked=True（宁可不删）
    - blocked: 本页是否必须禁用删除

    只查非敏感的 `auth.broker.url`。**绝不**查询或打印 `auth.broker.token`
    （那是凭据值）。环境变量只用于判断「是否启用」，其值同样不读取。
    """
    env_on = any(os.environ.get(k, "").strip()
                 for k in ("OMP_AUTH_BROKER_URL", "OMP_AUTH_BROKER_TOKEN"))
    url = None
    try:
        p = subprocess.run(omp_cmd(["omp", "config", "get", "auth.broker.url"]),
                           capture_output=True, text=True, shell=False,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        # 问不到就按最保守处理：只在环境变量明确显示 broker 时才算启用，
        # 但 known=False 仍然禁用删除，避免在远程凭据上执行本地软删。
        return {"enabled": env_on, "known": False, "blocked": True}
    if p.returncode != 0:
        return {"enabled": env_on, "known": False, "blocked": True}
    value = (p.stdout or "").strip()
    # omp 对未设置的键输出 "(not set)"；空输出同样视为未设置。
    url = None if value in ("", "(not set)") else value
    enabled = bool(url) or env_on
    return {"enabled": enabled, "known": True, "blocked": enabled}


def _rpc_index_safe():
    """RPC 账号索引的容错包装：返回 (index, code, message)。"""
    try:
        return rpc_accounts_index(), None, None
    except RpcFailure as e:
        return {}, e.code, str(e)
    except Exception as e:                      # 任何意外都不应拖垮账号页
        return {}, "unavailable", _sanitize_error(str(e))


def accounts_state():
    """账号页数据：按供应商分组的账号（身份/额度/策略/可注销凭据）+ 未匹配的策略。

    credentialId 只来自 omp RPC 的 get_logout_accounts（真正能删的对象）；
    identityKey 为空的登录（如 devin）无法设策略，但仍列出并可注销。
    """
    res = {}
    t1 = threading.Thread(target=lambda: res.__setitem__("acc", list_accounts()))
    t2 = threading.Thread(target=lambda: res.__setitem__("use", usage_report()))
    t3 = threading.Thread(target=lambda: res.__setitem__("rpc", _rpc_index_safe()))
    t1.start(); t2.start(); t3.start(); t1.join(); t2.join(); t3.join()
    acc, err = res["acc"]
    if err:
        # broker 模式下连 usage 都取不到（omp 要求 bearer token）：给出 broker 说明，
        # 而不是把上游的 token 报错当成主因展示。
        gate = deletion_gate()
        if gate["blocked"]:
            return {"error": None, "broker": gate["broker"], "brokerNote": True,
                    "providers": [], "stale": [],
                    "defaultReservePct": 10, "defaultReserveSet": False,
                    "editable": False, "policyError": None, "usageError": None,
                    "rpcError": None, "rpcErrorText": None,
                    "agentDir": str(AGENT_DIR) if AGENT_DIR else None,
                    "agentDirError": AGENT_DIR_ERROR, "profile": PROFILE}
        return {"error": err}
    usage = res["use"] if "error" not in res["use"] else {}
    rpc_index, rpc_code, rpc_msg = res["rpc"]
    idents = credential_identities()
    orig_emails = credential_original_emails()
    lines = read_config_text().splitlines(keepends=True)
    blk = _policies_block(lines)
    policies = blk.get("items") or []
    reserve, reserve_set = default_reserve_pct()
    reports = usage.get("reports") or []
    used_policies, used_reports = set(), set()
    providers, order = {}, []

    def slot(prov):
        if prov not in providers:
            providers[prov] = {"provider": prov, "accounts": []}
            order.append(prov)
        return providers[prov]

    def rpc_entries(prov):
        """该 provider 下 omp 能注销的每一条凭据（OAuth 与 API Key 都算）。"""
        out = []
        for e in rpc_index.get(prov) or []:
            cid = e.get("credentialId")
            if isinstance(cid, int) and not isinstance(cid, bool):
                out.append(e)
        return out

    # 第一遍：omp 的 usage 账号列表（含身份与额度），先全部列出；
    # 每条可注销凭据随后按「provider + identityKey」精确对上，对不上就单独成条。
    rows = []
    for a in acc.get("accounts") or []:
        prov, key = a.get("provider"), (a.get("identityKey") or "")
        ident = _identity_parts(key)
        rep = None
        for ri, r in enumerate(reports):
            md = r.get("metadata") or {}
            if r.get("provider") != prov or ri in used_reports:
                continue
            same_email = ident.get("email") and \
                str(md.get("email") or "").casefold() == ident["email"].casefold()
            if (same_email and (not ident.get("orgId") or not md.get("orgId")
                                or md.get("orgId") == ident["orgId"])) or \
               (ident.get("accountId") and md.get("accountId") == ident["accountId"]):
                rep = r
                used_reports.add(ri)
                break
        if rep:
            md = rep.get("metadata") or {}
            for k in ("accountId", "projectId", "orgId"):
                if md.get(k) and k not in ident:
                    ident[k] = md[k]
        pol = None
        if key:
            for i, p in enumerate(policies):
                if p["provider"] == prov and _policy_matches(p["account"], ident):
                    used_policies.add(i)
                    if pol is None:
                        pol = {"index": i, "account": p["account"],
                               "priority": p.get("priority"),
                               "reservePct": p.get("reservePct"),
                               "reclaimAbovePct": p.get("reclaimAbovePct")}
        rows.append({
            "provider": prov, "identityKey": key or None, "ident": ident,
            "orgName": a.get("orgName"), "plan": ((rep or {}).get("metadata") or {}).get("planType"),
            "report": rep, "policy": pol, "policyEditable": bool(key),
            "credentialId": None, "canDelete": False, "active": None,
            "label": None, "detail": None})

    # 第二遍：把每条凭据挂到能与之精确对应的展示行。
    #
    # R1 规则：只有「凭据计数」与「usage 展示行计数」双向都唯一时才合并，否则凭据
    # 自成一独立行。identity_key / usage 的 identityKey 都不是唯一键（实测 omp 的
    # 索引 idx_auth_provider_identity 不是 UNIQUE，同一 provider 下可有多行相同
    # identity_key；无身份标识时两边更是一模一样的 None），任何 next()/顺序认领都会
    # 把别的凭据的额度或标签挂错，所以一律以计数为判据。
    # 凭据自身的 label/detail/credentialId 永远是删除对象的权威来源。
    # 同一凭据经 provider 别名（如 zai / zai-coding-plan）在多个 providerId 下出现：
    # 每个 credentialId 只记一次，归到凭据库里的真实 provider（不依赖遍历顺序）。
    by_cid = {}
    for prov in rpc_index:
        for e in rpc_entries(prov):
            by_cid.setdefault(e["credentialId"], []).append((prov, e))
    ordered = []
    for cid, cands in by_cid.items():
        db_prov = (idents.get(cid) or (None, ""))[0]
        ordered.append(next((c for c in cands if c[0] == db_prov), cands[0]))

    def rows_for(prov, key):
        return [r for r in rows if r["provider"] == prov
                and r["credentialId"] is None
                and (r["identityKey"] or None) == (key or None)]

    # 每个 (provider, identityKey) 上的凭据数，供「双向唯一」判定
    cred_count = {}
    for prov, e in ordered:
        db_provider, db_key = idents.get(e["credentialId"], (None, ""))
        if db_provider is not None and db_provider != prov:
            db_key = ""          # 库与 RPC 说法不一致：不做身份配对
        cred_count[(prov, db_key or None)] = cred_count.get((prov, db_key or None), 0) + 1

    for prov, e in ordered:
        cid = e["credentialId"]
        db_provider, db_key = idents.get(cid, (None, ""))
        if db_provider is not None and db_provider != prov:
            db_key = ""
        key = db_key or None
        row = None
        if cred_count.get((prov, key), 0) == 1:
            cands = rows_for(prov, key)
            if len(cands) == 1:
                row = cands[0]       # 两个方向都恰好一条：对应关系唯一
        if row is None:
            ident = _identity_parts(db_key)
            pol = None
            if db_key:
                for i, p in enumerate(policies):
                    if p["provider"] == prov and _policy_matches(p["account"], ident):
                        used_policies.add(i)
                        if pol is None:
                            pol = {"index": i, "account": p["account"],
                                   "priority": p.get("priority"),
                                   "reservePct": p.get("reservePct"),
                                   "reclaimAbovePct": p.get("reclaimAbovePct")}
            row = {
                "provider": prov, "identityKey": key, "ident": ident,
                "orgName": None, "plan": None, "report": None, "policy": pol,
                "policyEditable": bool(db_key), "credentialId": None,
                "canDelete": False, "active": None, "label": None, "detail": None}
            rows.append(row)
        row["credentialId"] = cid
        row["canDelete"] = True
        row["active"] = bool(e.get("active"))
        row["label"] = e.get("label")
        row["detail"] = e.get("detail")

    # 没能被任何凭据认领的无身份 usage 行只是占位（omp 只给 provider/identityKey），
    # 该 provider 已有真实凭据时以凭据条目为准，避免同一登录出现两次。
    rows = [r for r in rows
            if r["identityKey"] or r["credentialId"] is not None
            or not rpc_entries(r["provider"])]

    # 有凭据可注销时才允许删除；broker / agent 目录解析失败会整体关闭（R4/R5）。
    delete_block = deletion_gate()["blocked"]
    for row in rows:
        ident = row.pop("ident")
        can_delete = row["canDelete"] and not delete_block
        # email 展示凭据里的原始大小写（identityKey 里的是小写规范化形式）；
        # 前端把它回传作为策略选择器时，才不会写出 omp 匹配不上的策略。
        email = orig_emails.get((row["identityKey"] or "").casefold()) or ident.get("email")
        slot(row["provider"])["accounts"].append({
            "identityKey": row["identityKey"], "email": email,
            "orgId": ident.get("orgId"), "accountId": ident.get("accountId"),
            "orgName": row["orgName"], "label": row["label"], "detail": row["detail"],
            "credentialId": row["credentialId"], "canDelete": can_delete,
            "policyEditable": row["policyEditable"], "active": row["active"],
            "plan": row["plan"], "report": row["report"], "policy": row["policy"]})

    stale = [{"index": i, **p} for i, p in enumerate(policies) if i not in used_policies]
    return {"providers": [providers[p] for p in order], "stale": stale,
            "defaultReservePct": reserve, "defaultReserveSet": reserve_set,
            "editable": blk.get("error") is None, "policyError": blk.get("error"),
            "usageError": res["use"].get("error"),
            "rpcError": rpc_code, "rpcErrorText": rpc_msg,
            "profile": PROFILE,
            "agentDir": str(AGENT_DIR) if AGENT_DIR else None,
            "agentDirError": AGENT_DIR_ERROR,
            "broker": broker_state()}


def _check_scalars(payload):
    out = {}
    pr = payload.get("priority")
    if pr not in (None, ""):
        if isinstance(pr, bool) or not re.fullmatch(r'-?\d{1,6}', str(pr).strip()):
            return None, "priority 必须是整数"
        out["priority"] = int(str(pr).strip())
    for k in ("reservePct", "reclaimAbovePct"):
        v = payload.get(k)
        if v in (None, ""):
            continue
        try:
            n = _num(str(v).strip())
        except ValueError:
            return None, f"{k} 必须是 0–100 的数字"
        if isinstance(v, bool) or not 0 <= n <= 100:
            return None, f"{k} 必须是 0–100 的数字"
        out[k] = n
    return out, None


def set_account_policy(payload):
    """为一个已登录 OAuth 账号新增/更新/清除策略；三个标量全空即删除。
    reclaimAbovePct（omp ≥18.12.4）：会话被降级到低优先级账号后，
    该账号额度恢复到这个百分比以上时自动切回；必须大于 reservePct。"""
    provider, key = str(payload.get("provider", "")), str(payload.get("identityKey", ""))
    if not POLICY_PROVIDER.fullmatch(provider):
        return "非法 provider"
    scalars, err = _check_scalars(payload)
    if err:
        return err
    acc, err = list_accounts()
    if err:
        return err
    if not any(a.get("provider") == provider and a.get("identityKey") == key
               for a in acc.get("accounts") or []):
        return "该账号不在 omp usage accounts 列表中（可能已登出）"
    ident = _identity_parts(key)
    for k, v in list(payload.get("extra", {}).items()) if isinstance(payload.get("extra"), dict) else []:
        if k in ("accountId", "projectId") and SELECTOR_VALUE.fullmatch(str(v)) and k not in ident:
            ident[k] = str(v)
    # omp 的 identityKey 把 email 规范化成小写，但策略匹配是逐字节精确比较，
    # 必须写回凭据里的原始大小写，否则这条策略会被 omp 忽略。
    if ident.get("email"):
        ident["email"] = credential_original_emails().get(key.casefold(), ident["email"])
    with _config_lock:
        lines = CONFIG.read_bytes().decode('utf-8').splitlines(keepends=True)
        blk = _policies_block(lines)
        if blk.get("error"):
            return blk["error"]
        items = blk["items"]
        hits = [i for i, p in enumerate(items)
                if p["provider"] == provider and _policy_matches(p["account"], ident)]
        if not scalars:
            if not hits:
                return None
            items = [p for i, p in enumerate(items) if i not in hits]
        elif hits:
            p = items[hits[0]]
            for k in POLICY_SCALARS:
                p.pop(k, None)
            p.update(scalars)
        else:
            if ident.get("email"):
                sel = {"email": ident["email"]}
                if ident.get("orgId"):
                    sel["orgId"] = ident["orgId"]
            elif ident.get("accountId"):
                sel = {"accountId": ident["accountId"]}
            elif ident.get("projectId"):
                sel = {"projectId": ident["projectId"]}
            else:
                return "该账号没有可用作选择器的 email/accountId/projectId"
            if not all(SELECTOR_VALUE.fullmatch(v) for v in sel.values()):
                return "账号标识含非法字符"
            items.append({"provider": provider, "account": sel, **scalars})
        if scalars.get("reclaimAbovePct") is not None:
            res = scalars.get("reservePct")
            if res is None:
                res = default_reserve_pct()[0]
            if scalars["reclaimAbovePct"] <= res:
                return (f"reclaimAbovePct（{scalars['reclaimAbovePct']}）必须大于 "
                        f"该账号的保留额度（{res}），否则请调低保留额度")
        return _write_policies(items)


def delete_policy(payload):
    """按序号删除一条策略；须同时给出 provider 与 account 以防序号漂移误删。"""
    i = payload.get("index")
    if not isinstance(i, int) or isinstance(i, bool):
        return "缺少 index"
    with _config_lock:
        lines = CONFIG.read_bytes().decode('utf-8').splitlines(keepends=True)
        blk = _policies_block(lines)
        if blk.get("error"):
            return blk["error"]
        items = blk["items"]
        if not 0 <= i < len(items) or items[i]["provider"] != payload.get("provider") \
           or items[i]["account"] != payload.get("account"):
            return "策略已变化，请刷新后重试"
        del items[i]
        return _write_policies(items)


def _canonical_identity(key):
    """identityKey → 规范化后的字段字典（email 小写、值去空白、丢弃空值）。

    omp 的 identityKey 形如 `email:a@b|org:x|account:y`。这里只保留出现的字段，
    因此「字段出现/消失」在比较时天然就是不同——这正是重新登录后同一 ID 从
    `email:a@b` 升级成 `email:a@b|org:new` 必须被识别为身份变更的原因。
    """
    out = {}
    for part in (key or "").split('|'):
        k, _, v = part.partition(':')
        v = (v or "").strip()
        if not k or not v:
            continue
        out[k] = v.casefold() if k in ("email", "account", "org", "project") else v
    return out


def _identity_matches(expected, actual):
    """删除前的身份核对：规范化后必须**完整相等**。

    旧实现只比较交集字段，导致旧页面拿着的 `email:a@b` 与当前已升级为
    `email:a@b|org:new` 的同一 credentialId 被判为相同。omp 重新登录时会保留
    ID 而升级组织作用域，那时旧页面就能删掉一条已经变了身份的登录——所以这里
    逐字段全等，字段出现或消失都算变更。
    """
    return _canonical_identity(expected) == _canonical_identity(actual)


def logout_account(payload):
    """按 omp 的 credentialId 注销一条 OAuth 登录（软删，不影响付费订阅）。

    返回 {ok, remainingSource?, code?, error?}。删除前必须同时通过：
    provider 合法、credentialId 是正整数、该凭据此刻仍出现在 omp RPC 的
    get_logout_accounts、类型是 oauth、且与本页展示的身份Key（若给出）一致。
    任一不满足即拒绝，绝不盲删。
    """
    provider = str(payload.get("provider") or "")
    cid = payload.get("credentialId")
    key = payload.get("identityKey")
    if not POLICY_PROVIDER.fullmatch(provider):
        return {"ok": False, "code": "invalid", "error": "非法 provider"}
    if not isinstance(cid, int) or isinstance(cid, bool) or cid <= 0:
        return {"ok": False, "code": "invalid", "error": "缺少有效的 credentialId"}
    if key is not None and not isinstance(key, str):
        return {"ok": False, "code": "invalid", "error": "identityKey 不是字符串"}

    # R4/R5：每次删除前重新核对 agent 目录与 broker；两者任一不成立就拒绝。
    gate = deletion_gate()
    if gate["blocked"]:
        code = "unavailable" if AGENT_DIR is None else "broker"
        return {"ok": False, "code": code, "error": gate["reason"]}

    try:
        index = rpc_accounts_index()
    except RpcFailure as e:
        return {"ok": False, "code": "unavailable" if e.code != "upstream" else "unavailable",
                "error": str(e) or "无法读取 omp 凭据列表"}

    target = None
    for e in index.get(provider) or []:
        if e.get("credentialId") == cid:
            target = e
            break
    if target is None:
        return {"ok": False, "code": "stale",
                "error": "该凭据已不在 omp 的凭据列表中（可能已被删除或已登出）"}
    if target.get("type") not in ("oauth", "api_key"):
        return {"ok": False, "code": "invalid",
                "error": "omp 未说明这条凭据的类型，请刷新后重试"}
    # RPC 也可能经 provider 别名报出同一条凭据；真实归属只认凭据库。
    if target.get("provider") not in (None, provider):
        return {"ok": False, "code": "stale",
                "error": "该凭据的供应商归属与请求不一致，请刷新后重试"}

    idents = credential_identities()
    if cid not in idents:
        # R4/P1：拿不到凭据库里的这一行就无法核验身份与归属，不能凭 RPC 单方面说法删。
        return {"ok": False, "code": "unavailable",
                "error": "无法从 omp 的凭据库确认这条凭据，已拒绝删除"}
    db_provider, db_key = idents[cid]
    if db_provider != provider:
        return {"ok": False, "code": "stale",
                "error": "凭据所属供应商已变化，请刷新后重试"}
    # P1：页面给出的身份若无法核验（库中没有该行身份）或规范化后不完全相等，一律拒绝。
    # 旧页面持有的 `email:a@b` 与升级后的 `email:a@b|org:new` 必须被判为不同。
    if key is not None or db_key:
        if not key or not db_key:
            return {"ok": False, "code": "stale",
                    "error": "该凭据的身份信息已变化，请刷新后重试"}
        if not _identity_matches(key, db_key):
            return {"ok": False, "code": "stale",
                    "error": "该凭据的身份已变化（可能是同邮箱的另一个组织），请刷新后重试"}

    try:
        data = rpc_logout(provider, cid)
    except RpcFailure as e:
        code = "gone" if e.code == "upstream" and "not stored" in str(e) else e.code
        return {"ok": False, "code": code, "error": str(e)}
    # R6：只暴露「是否还有其他认证源」这个布尔，不回传路径或邮箱。
    invalidate_usage(provider)
    return {"ok": True, "remainingSource": bool(data.get("remainingSource"))}


# ---------- 额度券重置（复刻 omp auth/resets.ts + usage/openai-codex-reset.ts） ----------
# 仅支持 anthropic / openai-codex（omp 的 resets.list 也是这个白名单）。
# token 只在本进程内存与出站 Authorization 头里出现，绝不写入响应/日志/文件。

REDEEM_PROVIDERS = {"anthropic", "openai-codex"}
ANTHROPIC_API = "https://api.anthropic.com"
ANTHROPIC_BETA = "oauth-2025-04-20"
ANTHROPIC_CLI_VER = "2.1.280"  # 对齐 omp 内嵌的 claude-cli 版本
CODEX_API = "https://chatgpt.com/backend-api"
RE_CREDIT_ID = re.compile(r"^[a-z0-9_-]{1,40}$")   # omp 侧 _ks
RE_ORG_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")  # omp 侧 Tvt

# 与 omp 一致的「同 request_id 待确认」状态：首扣拿到响应后缓存，
# 重复点击用同一 request_id 重放（服务端幂等），直到确认成功/终态。
_redeem_pending = {}  # provider -> {"credit_id","request_id","program","remaining"}


def _cred(provider):
    """从 ~/.omp/agent/agent.db 读该 provider 的 oauth 凭据（只读）。"""
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        row = con.execute(
            "SELECT data FROM auth_credentials "
            "WHERE provider=? AND credential_type='oauth' "
            "AND disabled_cause IS NULL ORDER BY id", (provider,)).fetchone()
        con.close()
    except sqlite3.Error as e:
        return None, f"读取凭据库失败: {e}"
    if not row:
        return None, f"{provider} 无 oauth 凭据"
    try:
        d = json.loads(row[0])
    except json.JSONDecodeError:
        return None, "凭据数据不是合法 JSON"
    access = d.get("access")
    if not isinstance(access, str) or not access:
        return None, "凭据缺少 access token"
    if isinstance(d.get("expires"), (int, float)) and d["expires"] <= time.time() * 1000:
        return None, "access token 已过期，请在 omp 中重新登录后再试"
    return {
        "access": access,
        "accountId": d.get("accountId") if isinstance(d.get("accountId"), str) else None,
        "orgId": d.get("orgId") if isinstance(d.get("orgId"), str) else None,
    }, None


def _http(method, url, access, extra_headers=None, body=None, timeout=25):
    """最小 HTTPS 调用，返回 (status, json_or_None, error_str_or_None)。
    任何异常/响应体都不会把 access token 带出。"""
    headers = {"Authorization": f"Bearer {access}",
               "User-Agent": "omp-settings-web/1.0"}
    for k, v in (extra_headers or {}).items():  # 大小写不敏感覆盖
        headers = {h: x for h, x in headers.items() if h.lower() != k.lower()}
        headers[k] = v
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _json_or_none(raw), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:2000]
        return e.code, _json_or_none(raw), None
    except Exception as e:  # URLError/timeout/ssl 等
        return 0, None, str(e)


def _json_or_none(raw):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def redeem_reset(provider):
    """消耗一张额度券重置该 provider 用量。返回结构化结果 dict。"""
    provider = (provider or "").strip().lower()
    if provider not in REDEEM_PROVIDERS:
        return {"ok": False, "code": "unsupported_provider",
                "error": "仅 anthropic / openai-codex 支持额度券重置"}
    cred, err = _cred(provider)
    if err:
        return {"ok": False, "code": "no_account", "error": err}
    if provider == "anthropic":
        return _redeem_anthropic(cred)
    return _redeem_codex(cred)


# ----- anthropic：两步确认（同 request_id 重放），对齐 omp #r 逻辑 -----

def _anthropic_headers():
    return {"accept": "application/json, text/plain, */*",
            "anthropic-beta": ANTHROPIC_BETA,
            # 服务端按 client surface 判定券资格（ineligible_reason:"surface"），
            # 必须与 omp 一致伪装成 claude-cli
            "user-agent": f"claude-cli/{ANTHROPIC_CLI_VER} (external, cli)"}


def _anthropic_list(cred):
    """GET /api/oauth/usage?cedar_ember=1&skip_spend=1 → 规范化券列表。"""
    url = f"{ANTHROPIC_API}/api/oauth/usage?cedar_ember=1&skip_spend=1"
    status, body, err = _http("GET", url, cred["access"],
                              _anthropic_headers())
    if err:
        return None, {"ok": False, "code": "network_error", "error": err}
    if status in (401, 403):
        return None, {"ok": False, "code": "auth_error",
                      "error": f"鉴权失败（HTTP {status}），请在 omp 中重新登录"}
    if status != 200 or not isinstance(body, dict):
        return None, {"ok": False, "code": f"http_{status}",
                      "error": f"列券接口返回 HTTP {status}"}
    blk = body.get("cedar_ember")
    if blk is None:
        return {"eligible": False, "grants": [], "nextGrantId": None,
                "cooldownUntil": None}, None
    if not isinstance(blk, dict) or not isinstance(blk.get("eligible"), bool):
        return None, {"ok": False, "code": "malformed_response",
                      "error": "列券响应格式不符"}
    grants = []
    for g in blk.get("grants") or []:
        if not isinstance(g, dict) or not RE_CREDIT_ID.match(str(g.get("id", ""))):
            continue
        grants.append({
            "id": g["id"],
            "label": g.get("label"),
            "remaining": g.get("resets_left", 0),
            "expiresAt": g.get("ends_at"),
            "usableNow": bool(g.get("usable_now")),
            "paused": bool(g.get("paused")),
            "clears": g.get("clears") or [],
        })
    nxt = blk.get("next_grant_id")
    nxt = nxt if isinstance(nxt, str) and any(g["id"] == nxt for g in grants) else None
    # cedar_ember 无可用券时回退 juniper_tide（对齐 omp e4 的第二条探测）
    if nxt is None or not blk["eligible"]:
        jt = _anthropic_list_juniper(cred)
        if jt and (jt["nextGrantId"] or jt["grants"]):
            return jt, None
    return {"eligible": blk["eligible"], "grants": grants, "nextGrantId": nxt,
            "cooldownUntil": blk.get("cooldown_until"),
            "reason": blk.get("ineligible_reason")}, None

def _anthropic_list_juniper(cred):
    """omp e4 的第二条探测：GET .../usage?at_wall=1&skip_spend=1 → juniper_tide 键。
    返回规范化结构（program='juniper_tide' 的虚拟券）或 None。"""
    url = f"{ANTHROPIC_API}/api/oauth/usage?at_wall=1&skip_spend=1"
    status, body, err = _http("GET", url, cred["access"],
                              _anthropic_headers(), timeout=15)
    if err or status != 200 or not isinstance(body, dict):
        return None
    blk = body.get("juniper_tide")
    if not isinstance(blk, dict) or not isinstance(blk.get("eligible"), bool):
        return None
    usable = blk["eligible"] and blk.get("arm") == "reset" and bool(blk.get("available"))
    grants = [{
        "id": "juniper_tide", "label": "Claude session limit reset",
        "program": "juniper_tide",
        "remaining": 1 if usable else 0,
        "expiresAt": blk.get("weekly_resets_at"),
        "usableNow": usable, "paused": False,
        "clears": ["anthropic:5h"],
    }] if blk.get("arm") == "reset" else []
    return {"eligible": blk["eligible"], "grants": grants,
            "nextGrantId": "juniper_tide" if usable else None,
            "cooldownUntil": blk.get("next_available_at"),
            "reason": blk.get("ineligible_reason"),
            "program": "juniper_tide"}


def _redeem_anthropic(cred):
    listed, err = _anthropic_list(cred)
    if err or listed is None:
        return err or {"ok": False, "code": "credit_list_failed",
                       "error": "列券失败"}
    program = listed.get("program") or "cedar_ember"
    credit = next((g for g in listed["grants"] if g["id"] == listed["nextGrantId"]), None)
    if not listed["eligible"] or not credit or not credit["usableNow"] \
            or credit["remaining"] < 1:
        code = "ineligible" if listed["grants"] else "no_credit"
        return {"ok": False, "code": code,
                "error": listed.get("reason") or "当前没有可用额度券"}
    # orgId：凭据里有就直接用，否则 GET /profile
    org = cred.get("orgId")
    if not (isinstance(org, str) and RE_ORG_ID.match(org)):
        status, body, err = _http("GET", f"{ANTHROPIC_API}/api/oauth/profile",
                                  cred["access"], _anthropic_headers(), timeout=5)
        org = None
        if isinstance(body, dict):
            o = body.get("organization")
            org = (o.get("uuid") if isinstance(o, dict) else None) \
                  or body.get("organization_uuid")
        if not (isinstance(org, str) and RE_ORG_ID.match(org)):
            return {"ok": False, "code": "organization_unavailable",
                    "error": "无法解析组织 ID"}
    # 两步确认：pending 里已有同券同 request_id 才视为确认；否则首扣并缓存
    pend = _redeem_pending.get("anthropic")
    if pend and pend["credit_id"] != credit["id"]:
        pend = None  # 券变了 → 重新开始
    if pend is None:
        pend = {"credit_id": credit["id"], "request_id": str(uuid.uuid4()),
                "remaining": credit["remaining"]}
        _redeem_pending["anthropic"] = pend
    if program == "juniper_tide":
        body = {"program": "juniper_tide"}
    else:
        body = {"program": "cedar_ember", "grant_id": credit["id"],
                "request_id": pend["request_id"]}
    status, resp, err = _http(
        "POST", f"{ANTHROPIC_API}/api/organizations/{org}/reset_rate_limits",
        cred["access"], _anthropic_headers(), body=body)
    if err:
        return {"ok": False, "code": "network_error", "error": err}
    if status != 200 or not isinstance(resp, dict) or not isinstance(resp.get("result"), str):
        code = {401: "auth_error", 403: "auth_error", 429: "rate_limited"}.get(
            status, f"http_{status}")
        _redeem_pending.pop("anthropic", None)
        return {"ok": False, "code": code, "error": f"HTTP {status}"}
    result = resp["result"].strip()
    code = {"already_used": "already_redeemed",
            "not_limited": "nothing_to_reset"}.get(result, result)
    if result == "reset":
        _redeem_pending.pop("anthropic", None)
        invalidate_usage("anthropic")
        return {"ok": True, "code": "reset", "cleared": resp.get("cleared") or []}
    if code in ("already_redeemed", "nothing_to_reset", "ineligible"):
        _redeem_pending.pop("anthropic", None)
    return {"ok": False, "code": code,
            "error": resp.get("reason") or f"result={result}"}


# ----- openai-codex：单步（request_id 幂等），对齐 omp JHe/a4 -----

def _codex_headers(cred):
    h = {}
    if cred.get("accountId"):
        h["ChatGPT-Account-Id"] = cred["accountId"]
    return h


def _redeem_codex(cred):
    url = f"{CODEX_API}/wham/rate-limit-reset-credits"
    status, body, err = _http("GET", url, cred["access"], _codex_headers(cred))
    if err:
        return {"ok": False, "code": "network_error", "error": err}
    if status in (401, 403):
        return {"ok": False, "code": "auth_error",
                "error": f"鉴权失败（HTTP {status}），请在 omp 中重新登录"}
    if status != 200 or not isinstance(body, dict):
        return {"ok": False, "code": f"http_{status}", "error": f"HTTP {status}"}
    credits = [c for c in (body.get("credits") or [])
               if isinstance(c, dict) and (c.get("status") or "available") == "available"
               and isinstance(c.get("id"), str)]
    if not credits:
        return {"ok": False, "code": "no_credit", "error": "当前没有可用额度券"}
    # 选最早过期的（omp ZHe 逻辑）
    def expkey(c):
        try:
            return float("inf") if not c.get("expires_at") else \
                __import__("datetime").datetime.fromisoformat(
                    c["expires_at"].replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return float("inf")
    credit = min(credits, key=expkey)
    pend = _redeem_pending.get("openai-codex")
    if not pend or pend["credit_id"] != credit["id"]:
        pend = {"credit_id": credit["id"], "request_id": str(uuid.uuid4())}
        _redeem_pending["openai-codex"] = pend
    status, resp, err = _http(
        "POST", f"{CODEX_API}/wham/rate-limit-reset-credits/consume",
        cred["access"], _codex_headers(cred),
        body={"credit_id": credit["id"],
              "redeem_request_id": pend["request_id"],
              "account_id": cred.get("accountId")})
    if err:
        return {"ok": False, "code": "network_error", "error": err}
    code = (resp or {}).get("code") if isinstance(resp, dict) else None
    code = code if isinstance(code, str) else ("reset" if 200 <= status < 300 else f"http_{status}")
    if code == "reset":
        _redeem_pending.pop("openai-codex", None)
        invalidate_usage("openai-codex")
        return {"ok": True, "code": "reset"}
    _redeem_pending.pop("openai-codex", None)
    return {"ok": False, "code": code, "error": f"consume 返回 {code}"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8",
              close=False):
        data = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if close:                    # 请求体没能读完时，明确告知不要再复用连接
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # rebinding 下攻击页可先 GET /api/accounts 拿到 credentialId 再 POST；
        # 所以读接口同样必须过 Host 白名单（只校验写接口不够）。
        if not self._same_origin():
            self._send(403, json.dumps({"error": "跨站请求被拒绝"}, ensure_ascii=False))
            return
        path = self.path.split('?', 1)[0]  # 允许 /?v=... 查询串（缓存穿透），路由只看路径
        if path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif path == "/api/state":
            vis = visibility_state()
            self._send(200, json.dumps(
                {"roles": parse_roles(), "models": vis["models"],
                 "visible": vis["visible"], "config": str(CONFIG)}, ensure_ascii=False))
        elif path == "/api/settings":
            self._send(200, json.dumps(list_settings(), ensure_ascii=False))
        elif path == "/api/visibility":
            self._send(200, json.dumps(visibility_state(), ensure_ascii=False))
        elif path == "/api/usage":
            self._send(200, json.dumps(usage_with_resettable(), ensure_ascii=False))
        elif path == "/api/fallback":
            self._send(200, json.dumps(fallback_state(), ensure_ascii=False))
        elif path == "/api/cycle-order":
            self._send(200, json.dumps(cycle_order_state(), ensure_ascii=False))
        elif path == "/api/accounts":
            self._send(200, json.dumps(accounts_state(), ensure_ascii=False))
        elif path == "/api/custom-providers":
            self._send(200, json.dumps(custom_providers_state(), ensure_ascii=False))
        else:
            self._send(404, "{}")

    def _allowed_hosts(self):
        """本服务实际可达的 Host 白名单；IPv6 只在真的监听 ::1 时加入。"""
        hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if LISTEN_IPV6:
            hosts |= {f"[::1]:{PORT}", f"::1:{PORT}"}
        return hosts

    def _same_origin(self):
        """Host 必须在白名单内，且 Origin/Referer（若带）必须与 Host 同源。

        DNS rebinding 下攻击者域名的 Origin 与 Host 完全一致，只比这两个值会被
        绕过（实测 Host=evil.example 时旧实现返回 200 并真的调用了 logout），
        所以先用 Host 白名单把「浏览器把我们的端口当成了外部站点」挡掉。
        Origin 只有 authority；Referer 带完整路径，按 scheme://authority 截取。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self._allowed_hosts():
            return False

        def authority(value):
            m = re.match(r'https?://([^/?#]+)', value.strip(), re.I)
            return m.group(1).lower() if m else None

        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if value and value.strip():
                return authority(value) == host
        return True          # 无 Origin/Referer：非浏览器客户端（curl、测试）

    def do_POST(self):
        # 破坏性接口（删账号）只接受同源请求：本机其他网站/脚本不能借浏览器
        # 已有的 localhost 访问权直接删凭据。Origin 缺失（非浏览器）时放行，
        # 因为本服务只监听 127.0.0.1 且不带 cookie 鉴权。
        path = self.path.split('?', 1)[0]
        destructive = path in ("/api/accounts/logout",
                               "/api/custom-providers/delete")
        origin_ok = self._same_origin()
        # 无论是否拒绝，都先把请求体读掉：客户端还在写 body 时直接关连接，
        # 对端会收到 RST（Windows 上实测 WinError 10053），403 就传不出去。
        # 读取必须有上限与超时：谎报 Content-Length 或慢慢发的对端不能把
        # 请求线程一直挂着（超时即放弃读取，并让响应显式关闭连接）。
        length = int(self.headers.get("Content-Length", 0) or 0)
        body, drained = self._read_body(length)
        if not drained:
            self._send(413, json.dumps(
                {"error": "请求体过大或读取超时"}, ensure_ascii=False), close=True)
            return
        if destructive and not origin_ok:
            self._send(403, json.dumps({"error": "跨站请求被拒绝"}, ensure_ascii=False))
            return
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._send(400, '{"error": "bad json"}')
            return
        if path == "/api/set":
            err = set_role(payload.get("role", ""), payload.get("value", ""))
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            else:
                self._send(200, json.dumps(
                    {"ok": True, "roles": parse_roles()}, ensure_ascii=False))
        elif self.path == "/api/set-key":
            err = self._set_key(payload)
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            else:
                self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
        elif self.path == "/api/visibility":
            err = set_visible(payload.get("visible"))
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            else:
                self._send(200, json.dumps({"ok": True, **visibility_state()},
                                           ensure_ascii=False))
        elif self.path == "/api/fallback":
            err = set_fallback_chain(payload.get("key", ""),
                                     payload.get("chain", None))
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            else:
                self._send(200, json.dumps(
                    {"ok": True, **fallback_state()}, ensure_ascii=False))
        elif self.path == "/api/cycle-order":
            err = set_cycle_order(payload.get("order", None))
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            else:
                self._send(200, json.dumps(
                    {"ok": True, **cycle_order_state()}, ensure_ascii=False))
        elif self.path == "/api/usage/invalidate":
            self._send(200, json.dumps(
                invalidate_usage(payload.get("provider")), ensure_ascii=False))
        elif self.path in ("/api/accounts/policy", "/api/accounts/policy-delete"):
            err = (set_account_policy if self.path.endswith("policy") else delete_policy)(payload)
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            else:
                self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
        elif self.path == "/api/accounts/logout":
            r = logout_account(payload)
            status = 200 if r.get("ok") else \
                409 if r.get("code") in ("stale", "gone", "broker") else \
                503 if r.get("code") == "unavailable" else 400
            self._send(status, json.dumps(r, ensure_ascii=False))
        elif self.path == "/api/usage/redeem":
            self._send(200, json.dumps(
                redeem_reset(payload.get("provider")), ensure_ascii=False))
        elif path == "/api/custom-providers/delete":
            r = delete_custom_provider(payload)
            status = 200 if r.get("ok") else \
                409 if r.get("code") in ("stale", "referenced") else \
                410 if r.get("code") == "gone" else \
                500 if r.get("code") == "partial" else \
                503 if r.get("code") in ("unavailable", "broker") else \
                400
            if status != 200:
                r["code"] = r.get("code") or "invalid"
                # 刷新页面上的定义列表（可能已部分变化）；拿不到就沿用旧列表
                try:
                    r["providers"] = custom_providers_state()["providers"]
                except Exception:
                    pass
            self._send(status, json.dumps(r, ensure_ascii=False))
        elif self.path == "/api/models/test":
            r = probe_model(payload.get("selector"))
            status = 400 if r["code"] == "invalid_model" else \
                     409 if r["code"] == "busy" else 200
            self._send(status, json.dumps(r, ensure_ascii=False))
        else:
            self._send(404, "{}")

    MAX_BODY = 256 * 1024          # 本服务所有 POST 都是小 JSON
    MAX_DRAIN = 8 * 1024 * 1024    # 超过 MAX_BODY 时最多丢弃这么多，只为把响应送回
    BODY_TIMEOUT = 10.0

    def _read_body(self, length):
        """读固定长度请求体；返回 (body, ok)。

        超时或对端提前断开返回 (b"", False)。请求体超过 MAX_BODY 时不再保留内容，
        但会按上限丢弃剩余字节，让客户端把 body 写完再读到 413；否则提前关连接
        会让对端收到 RST（Windows 上实测 WinError 10053），错误反而传不出去。
        """
        if length <= 0:
            return b"", True
        keep = length <= self.MAX_BODY
        if not keep and length > self.MAX_DRAIN:
            return b"", False           # 太大：直接拒绝，不尝试丢弃
        try:
            self.connection.settimeout(self.BODY_TIMEOUT)
        except OSError:
            pass
        chunks, got = [], 0
        try:
            while got < length:
                chunk = self.rfile.read(min(65536, length - got))
                if not chunk:
                    return b"", False
                got += len(chunk)
                if keep:
                    chunks.append(chunk)
        except OSError:                 # 超时（socket.timeout）或连接中断
            return b"", False
        finally:
            try:
                self.connection.settimeout(None)
            except OSError:
                pass
        return (b"".join(chunks) if keep else b""), keep

    def _set_key(self, payload):
        key = payload.get("key", "").strip()
        catalog = json.loads(run(["omp", "config", "list", "--json"]))
        meta = catalog.get(key)
        if meta is None:
            return f'未知设置项: {key}'
        if meta.get('type') not in EDITABLE_TYPES:
            return f'类型 {meta.get("type")} 不支持在线编辑'
        try:
            formatted = coerce_value(meta['type'], str(payload.get("value", "")),
                                     enum_options(key))
        except ValueError as e:
            return str(e)
        return set_config_key(key, formatted)

    def log_message(self, format, *args):
        pass


def set_role(role, value):
    with _config_lock:
        return _set_role(role, value)


def _set_role(role, value):
    role, value = role.strip(), value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_@:,\.\-\/\*]+", role) or \
       not re.fullmatch(r"[A-Za-z0-9_@:,\.\-\/\*\?\[\]]+", value):
        return "illegal role or value"
    raw = CONFIG.read_bytes().decode('utf-8')
    eol = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.splitlines(keepends=True)
    pat = re.compile(rf"^(  {re.escape(role)}:)[^\r\n]*(\r?\n)?$")
    replaced = False
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = f"  {role}: {value}{eol}"
            replaced = True
            break
    if not replaced:
        for i, line in enumerate(lines):
            if re.match(r"^modelRoles:\s*(\r?\n)?$", line):
                lines.insert(i + 1, f"  {role}: {value}{eol}")
                replaced = True
                break
        if not replaced:
            return "modelRoles block not found"
    write_backup()
    CONFIG.write_bytes("".join(lines).encode('utf-8'))
    return None


def main():
    srv = None
    for addr in ([("::1", PORT)] if LISTEN_IPV6 else []) + [("127.0.0.1", PORT)]:
        try:
            srv = ThreadingHTTPServer(addr, Handler)
            break
        except OSError as e:
            print(f"port {PORT} unavailable on {addr[0]}, another instance likely running: {e}")
    if srv is None:
        return
    url = f"http://{srv.server_address[0]}:{PORT}"
    print(f"omp settings web ui -> {url}")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
