import ast
import asyncio
import copy
import hashlib
import io
import json
import os
import re
import shutil
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

try:
    from quart import jsonify, request
except Exception:  # pragma: no cover
    jsonify = request = None

try:
    from astrbot.api import AstrBotConfig
except Exception:  # pragma: no cover
    AstrBotConfig = dict

try:
    from astrbot.api import llm_tool
except Exception:  # pragma: no cover
    def llm_tool(*a, **k):
        def deco(fn):
            return fn
        return deco

PLUGIN_NAME = "astrbot_plugin_self_aware"


def _hook(name):
    """取一个 filter 钩子；老版本 AstrBot 没有这个钩子时就退化成什么都不做"""
    try:
        deco = getattr(filter, name)
        return deco()
    except Exception:
        return lambda fn: fn


def _data_dir():
    """跨平台定位数据目录：优先 AstrBot 的数据目录，退回到插件自己目录"""
    # 1) AstrBot 官方工具
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        base = str(get_astrbot_data_path())
        if base and os.path.isdir(base):
            d = os.path.join(base, "plugin_data", PLUGIN_NAME)
            os.makedirs(d, exist_ok=True)
            return d
    except Exception:
        pass
    # 2) 环境变量
    for k in ("ASTRBOT_DATA_PATH", "ASTRBOT_DATA_DIR"):
        v = os.environ.get(k)
        if v and os.path.isdir(v):
            d = os.path.join(v, "plugin_data", PLUGIN_NAME)
            os.makedirs(d, exist_ok=True)
            return d
    # 3) 插件自己的 data 目录
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(d, exist_ok=True)
    return d


CACHE_DIR = _data_dir()
LEVELS = {"关": "none", "低": "low", "中": "medium", "高": "high"}
THOUGHT_DIR = os.path.join(CACHE_DIR, "thoughts")
CAP_CACHE = os.path.join(CACHE_DIR, "self_aware_caps.json")
TOOL_STATE = os.path.join(CACHE_DIR, "tool_state.json")
PROFILE_DIR = os.path.join(CACHE_DIR, "profiles")
MANUAL_DIR = os.path.join(CACHE_DIR, "manuals")

# 语义触发词：不问"你会什么"，也能判断她现在需要某个能力
# 以前这里有一张手写关键词表（图/歌/表情…），已删除：改成只看画像



def _log(msg):
    logger.info("[能力自省] %s" % msg)
    try:
        _LOGBOOK.add("[能力自省] " + str(msg))
    except Exception:
        pass


def _read_json(p, default):
    try:
        with io.open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(p, data):
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log("写文件失败: %r" % e)


# ---------- 参数清洗：把真人说的话洗成能当命令用的参数 ----------
_ARG_STRIP = (
    (r"【[^】]{0,300}】", " "),                       # 能力提示块
    (r"[（(][^（）()]{0,160}[）)]", " "),             # 括号里的解释
    (r"@[^\s@]{1,30}?\(\d{4,}\)", " "),              # @昵称(QQ号)
    (r"@[^\s@]{1,20}", " "),                         # 剩下的 @昵称
    (r"\[引用了[^\]]{0,60}\]", " "),                  # 引用标记
    (r"\[主人转发了一段聊天记录[^\]]*\]", " "),
    (r"\[(?:图片|表情|语音|视频|文件|闪照)\]", " "),
    (r"[┌└├│─]", " "),                               # 合并转发边框
    (r"[\r\n\t]+", " "),
)


def _clean_arg(text, limit=40):
    """去掉 @、引用、转发边框、括号解释，压空白，超长截断"""
    t = str(text or "")
    for pat, rep in _ARG_STRIP:
        t = re.sub(pat, rep, t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.strip("，,。.！!？?：:；;、~～\"'“”‘’《》「」[]()（）")
    t = re.sub(r"^(?:个|张|点|些|一张|两张|几张|一个|两个|几个)\s*", "", t)
    if limit and len(t) > limit:
        t = t[:limit].rstrip()
    return t


def _read_plugin_doc(folder, limit=4000):
    """读插件自带的说明文档，优先挑"命令/用法/常见"相关的段落"""
    import re as _re
    for fn in ("README.md", "readme.md", "README_zh.md", "README.zh-CN.md", "HELP.md", "USAGE.md"):
        p = os.path.join(folder, fn)
        if not os.path.isfile(p):
            continue
        try:
            txt = io.open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        if not txt.strip():
            continue
        # 按二级标题切块，只留和"怎么用"有关的
        parts = _re.split(r"\n(?=#{1,3} )", txt)
        keep, other = [], []
        for seg in parts:
            head = seg.split("\n", 1)[0].lower()
            if any(k in head for k in ("命令", "指令", "用法", "使用", "常见", "faq", "教程", "功能")):
                keep.append(seg)
            else:
                other.append(seg)
        body = "\n".join(keep).strip()
        if len(body) < 200:          # 没找到相关章节，退回开头一节
            body = "\n".join(other).strip()
        return body[:limit]
    return ""


class _LogBook(object):
    """本插件自己的运行日志：只写自己的文件，不碰任何 logger 的 handler"""

    def __init__(self, keep=1200):
        self.lines = []
        self.keep = keep
        self.path = ""

    def _ensure_path(self):
        if self.path:
            return
        import datetime as _dt
        d = os.path.join(CACHE_DIR, "logs")
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, _dt.datetime.now().strftime("%Y-%m-%d.log"))

    def add(self, msg, level="INFO"):
        try:
            line = "[%s] [%s] %s" % (time.strftime("%H:%M:%S"), level, str(msg))
            if self.lines and self.lines[-1].split("] ", 2)[-1] == line.split("] ", 2)[-1]:
                return
            self.lines.append(line)
            if len(self.lines) > self.keep:
                del self.lines[:len(self.lines) - self.keep]
            self._ensure_path()
            with io.open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def tail(self, n=40, keyword=""):
        pool = self.lines
        if keyword:
            pool = [x for x in pool if keyword in x]
        return pool[-int(n):]


_LOGBOOK = _LogBook()


@register(PLUGIN_NAME, "鲸娘", "让 bot 认识、理解并使用自己的能力（命令与工具都算）", "2.5.0")
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._caps = []
        self._fp = ""
        self._register_web_apis()
        self._last_error = ""
        self._diag_ts = 0.0
        self._last_cap = {}
        self._talk_ts = {}
        self._last_output = []

    # ---------- 配置 ----------
    def _cfg(self, key, default=None):
        try:
            v = self.config.get(key, default)
        except Exception:
            v = default
        return default if v is None else v

    def _plugins_dir(self):
        d = str(self._cfg("plugins_dir", "") or "").strip()
        if d and os.path.isdir(d):
            return d
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.dirname(here)

    # ---------- 日志：优先读 AstrBot 自己的日志文件（合规做法） ----------
    def _astrbot_log_file(self):
        """AstrBot 的日志文件，只有在用户开了“记录到文件”时才有"""
        try:
            cfg = self.context.get_config() or {}
            if not bool(cfg.get("log_file_enable", False)):
                return ""
            p = str(cfg.get("log_file_path") or "logs/astrbot.log")
        except Exception:
            return ""
        try:
            if not os.path.isabs(p):
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path
                p = os.path.join(str(get_astrbot_data_path()), p)
        except Exception:
            return ""
        return p if os.path.isfile(p) else ""

    def _tail_file(self, path, n=60, keyword="", max_bytes=256 * 1024):
        """只读文件末尾，别把整份日志读进内存"""
        try:
            size = os.path.getsize(path)
            with io.open(path, "rb") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                data = f.read()
            lines = data.decode("utf-8", "replace").splitlines()
        except Exception:
            return []
        if keyword:
            lines = [x for x in lines if keyword in x]
        return lines[-int(n):]

    def _log_lines(self, n=40, keyword=""):
        """先看 AstrBot 的日志文件；没有就退回本插件自己的日志"""
        p = self._astrbot_log_file()
        if p:
            rows = self._tail_file(p, n, keyword)
            if rows:
                return rows
        return _LOGBOOK.tail(n, keyword)

    # ---------- 扫描 ----------
    def _disabled(self):
        """哪些插件被停用了：问框架（star_map），不直接读数据库"""
        try:
            from astrbot.core.star.star import star_map
        except Exception:
            return set()
        out = set()
        try:
            for module_path, meta in list(star_map.items()):
                if getattr(meta, "activated", True):
                    continue
                parts = str(module_path).split(".")
                out.add(parts[2] if len(parts) >= 3 else parts[-1])
        except Exception:
            return set()
        return out

    def _meta(self, folder):
        for name in ("metadata.yaml", "metadata.yml"):
            p = os.path.join(folder, name)
            if not os.path.isfile(p):
                continue
            out = {}
            try:
                with io.open(p, encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
                key = None
                buf = []
                for line in lines:
                    m = re.match(r"^([a-zA-Z_]+):\s*(.*?)\s*$", line)
                    if m:
                        if key and buf:
                            out[key] = " ".join(buf).strip()
                        key, val = m.group(1), m.group(2).strip('"\'')
                        # YAML 的 | 和 > 是多行块，先收着，后面一行行拼
                        if val in ("|", ">", "|-", ">-", "|+", ">+"):
                            buf = []
                        else:
                            if key not in out:
                                out[key] = val
                            key, buf = None, []
                    elif key is not None and (line.startswith(" ") or line.startswith("\t")):
                        if line.strip():
                            buf.append(line.strip())
                    elif key is not None:
                        if buf:
                            out[key] = " ".join(buf).strip()
                        key, buf = None, []
                if key and buf:
                    out[key] = " ".join(buf).strip()
            except Exception:
                pass
            return out
        return {}

    def _commands(self, folder):
        """用 ast 精确提取 @filter.command 及其 docstring"""
        out = []
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests", "docs", "views")]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(root, fn)
                try:
                    src = io.open(p, encoding="utf-8", errors="replace").read()
                    tree = ast.parse(src)
                except Exception:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                        continue
                    for dec in node.decorator_list:
                        try:
                            if not isinstance(dec, ast.Call):
                                continue
                            f = dec.func
                            kind = None
                            if isinstance(f, ast.Attribute) and f.attr == "command":
                                kind = "cmd"
                            elif isinstance(f, ast.Attribute) and f.attr == "llm_tool":
                                kind = "tool"
                            if not kind:
                                continue
                            first = dec.args[0] if dec.args else None
                            if not isinstance(first, ast.Constant):
                                # llm_tool 用关键字 name=
                                first = None
                                for kw in dec.keywords:
                                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                                        first = kw.value
                                        break
                            if first is None or not isinstance(first, ast.Constant):
                                continue
                            cmd = str(first.value)
                            doc = (ast.get_docstring(node) or "").strip().splitlines()
                            doc = doc[0].strip() if doc else ""
                            if cmd and len(out) < 80:
                                out.append({"cmd": cmd, "full": cmd, "doc": doc[:60], "kind": kind})
                        except Exception:
                            continue
        seen, uniq = set(), []
        for c in out:
            if c["cmd"] in seen:
                continue
            seen.add(c["cmd"])
            uniq.append(c)
        return uniq

    # ---------- 从 AstrBot 注册表发现指令（权威来源） ----------
    DANGER_WORDS = ("删除", "清空", "重置", "重启", "关闭", "停止", "配置", "设置",
                    "权限", "踢", "禁言", "撤回", "管理员", "卸载", "安装", "更新",
                    "disable", "enable", "reset", "delete", "clear", "reload")

    def _is_danger(self, name, doc=""):
        blob = (name or "") + " " + (doc or "")
        return any(w in blob for w in self.DANGER_WORDS)

    def _registry_commands(self, use_cache=True):
        """从 star_handlers_registry 读已注册的指令：{插件目录名: [命令]}"""
        if use_cache:
            c = getattr(self, "_reg_cache", None)
            if c and time.time() - c[0] < 30:
                return c[1]
        out = self._registry_commands_raw()
        self._reg_cache = (time.time(), out)
        return out

    def _registry_commands_raw(self):
        out = {}
        try:
            from astrbot.core.star.star_handler import (
                EventType, star_handlers_registry)
        except Exception as e:
            _log("拿不到注册表: %r" % e)
            return out
        try:
            handlers = star_handlers_registry.get_handlers_by_event_type(
                EventType.AdapterMessageEvent, only_activated=True)
        except Exception as e:
            _log("读注册表失败: %r" % e)
            return out

        for h in handlers:
            names = []          # [(命令短名, 完整写法)]
            regexes = []        # 用 @filter.regex 注册的路由（不是命令，但也是能力）
            for f in (getattr(h, "event_filters", None) or []):
                pat = getattr(f, "regex_str", None)
                if pat:
                    regexes.append(str(pat))
                cn = getattr(f, "command_name", None)
                if not cn:
                    continue
                fulls = []
                try:
                    fulls = [str(x) for x in (f.get_complete_command_names() or [])]
                except Exception:
                    fulls = []
                if not fulls:
                    fulls = [str(cn)]
                for full in fulls:
                    names.append((str(cn), full))
            if not names and not regexes:
                continue
            mod = str(getattr(h, "handler_module_path", "") or "")
            seg = mod.split(".")
            plugin = ""
            for i, x in enumerate(seg):
                if x == "plugins" and i + 1 < len(seg):
                    plugin = seg[i + 1]
                    break
            if not plugin:
                plugin = mod or "unknown"
            doc = str(getattr(h, "desc", "") or "").strip()
            if not doc:
                try:
                    doc = (h.handler.__doc__ or "").strip().splitlines()[0]
                except Exception:
                    doc = ""
            # partial/装饰器包过的函数，__doc__ 是 functools 那句废话，别当说明用
            if doc.startswith("partial(") or "new function with partial" in doc:
                doc = ""
            for short, full in names:
                if self._is_danger(full, doc) or self._is_danger(short, doc):
                    continue
                out.setdefault(plugin, []).append(
                    {"cmd": short, "full": full, "doc": doc[:60], "kind": "cmd"})
            # 正则路由：没有命令名，但用户说出来就该触发，同样算能力
            for pat in regexes[:8]:
                label = str(getattr(h, "handler_name", "") or "").strip() or pat[:20]
                if self._is_danger(label, doc):
                    continue
                out.setdefault(plugin, []).append({
                    "cmd": label, "full": label, "doc": (doc or pat)[:60],
                    "kind": "regex", "pattern": pat,
                })
        # 去重（按完整写法）
        for k, v in out.items():
            seen, uniq = set(), []
            for c in v:
                key = c.get("full") or c["cmd"]
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(c)
            out[k] = uniq
        return out

    def _fingerprint(self, base):
        h = hashlib.md5()
        try:
            for name in sorted(os.listdir(base)):
                folder = os.path.join(base, name)
                if not os.path.isdir(folder):
                    continue
                for fn in ("metadata.yaml", "main.py"):
                    p = os.path.join(folder, fn)
                    if os.path.isfile(p):
                        h.update(("%s:%d;" % (name + fn, os.path.getmtime(p))).encode())
        except Exception:
            pass
        try:
            reg = self._registry_commands()
            h.update(("reg:%d;" % sum(len(v) for v in reg.values())).encode())
        except Exception:
            pass
        try:
            h.update(("tools:%s;" % ",".join(sorted(self._runtime_tools().keys()))).encode())
        except Exception:
            pass
        return h.hexdigest()

    # ---------- 运行时能力：工具（llm_tool）也算能力，不只是指令 ----------
    def _runtime_tools(self):
        """从 AstrBot 的工具注册表里读真实存在的工具：{name: {...}}"""
        try:
            from astrbot.core.provider.register import llm_tools
        except Exception:
            return {}
        try:
            tools = list(getattr(llm_tools, "func_list", []) or [])
        except Exception:
            return {}
        out = {}
        for t in tools:
            try:
                name = str(getattr(t, "name", "") or "").strip()
                if not name:
                    continue
                schema = getattr(t, "parameters", None)
                props = schema.get("properties") if isinstance(schema, dict) else {}
                params = [k for k in (props or {}).keys() if k not in ("event", "context")]
                mod = str(getattr(t, "handler_module_path", "") or "")
                if not mod:
                    mod = str(getattr(getattr(t, "handler", None), "__module__", "") or "")
                plugin = ""
                seg = mod.split(".")
                if "plugins" in seg:
                    i = seg.index("plugins")
                    if i + 1 < len(seg):
                        plugin = seg[i + 1]
                out[name] = {
                    "name": name,
                    "desc": str(getattr(t, "description", "") or ""),
                    "params": params,
                    "required": list(schema.get("required") or []) if isinstance(schema, dict) else [],
                    "handler": getattr(t, "handler", None),
                    "plugin": plugin,
                    "active": bool(getattr(t, "active", True)),
                }
            except Exception:
                continue
        return out

    def _scan(self, force=False):
        base = self._plugins_dir()
        fp = self._fingerprint(base)
        if not force and fp == self._fp and self._caps:
            return self._caps
        cached = _read_json(CAP_CACHE, {})
        if not force and cached.get("fp") == fp and cached.get("caps"):
            self._caps = cached["caps"]
            self._fp = fp
            self._prune_once({c.get("plugin") or "" for c in self._caps})
            return self._caps

        off = self._disabled()
        skip = (PLUGIN_NAME,)
        reg = self._registry_commands()
        tools = self._runtime_tools()
        if reg:
            _log("注册表里拿到 %d 个插件、%d 条指令" % (
                len(reg), sum(len(v) for v in reg.values())))
        if tools:
            _log("工具表里拿到 %d 个可调用工具" % len(tools))
        caps = []
        try:
            names = sorted(os.listdir(base))
        except Exception:
            names = []
        report = []          # 每个目录为什么进/不进能力表，方便诊断
        for name in names:
            folder = os.path.join(base, name)
            if not os.path.isdir(folder):
                continue
            if name in skip:
                report.append((name, "跳过自己"))
                continue
            if name in off:
                report.append((name, "在停用名单"))
                continue
            meta = self._meta(folder)
            if not meta:
                # 没有 metadata.yaml 的插件也认：用目录名当名字，别整条丢掉
                report.append((name, "无 metadata，按目录名收录"))
            display = str(meta.get("display_name") or meta.get("name") or name)
            desc = str(meta.get("desc") or meta.get("short_desc") or "").strip()
            cmds = list(reg.get(name) or [])
            # 这个插件注册的工具也是能力；有工具就不用再整目录 AST 解析了
            for t in tools.values():
                if t.get("plugin") == name and t.get("active", True):
                    cmds.append({"cmd": t["name"], "doc": t["desc"][:60],
                                 "kind": "tool", "params": t.get("params") or []})
            if not cmds:
                cmds = self._commands(folder)   # 注册表/工具都没有，才扫源码
            seen, uniq = set(), []
            for c in cmds:
                key = (c.get("full") or c.get("cmd") or "") + "|" + (c.get("pattern") or "")
                if not c.get("cmd") or key in seen:
                    continue
                seen.add(key)
                uniq.append(c)
            cmds = [c for c in uniq if not self._is_danger(c.get('cmd', ''), c.get('doc', ''))]
            caps.append({
                "plugin": name,
                "name": display,
                "desc": re.sub(r"\s+", " ", desc)[:120],
                "cmds": cmds,
                "tags": [],
            })
            report.append((name, "收录（%d 命令 / %d 工具）" % (
                len([c for c in cmds if c.get("kind") != "tool"]),
                len([c for c in cmds if c.get("kind") == "tool"]))))
        self._scan_report = {"dir": base, "folders": len(names), "report": report}
        self._caps = caps
        self._fp = fp
        _write_json(CAP_CACHE, {"fp": fp, "ts": time.time(), "caps": caps})
        self._prune_once({c.get("plugin") or "" for c in caps})
        _log("扫描到 %d 个插件能力（目录里 %d 个文件夹）" % (len(caps), len(names)))
        return caps

    def _prune_once(self, live):
        """一个进程只扫一遍磁盘，别每条消息都去 listdir"""
        if getattr(self, "_pruned", False):
            return
        self._pruned = True
        self._prune_gone(live)

    def _prune_gone(self, live):
        """插件目录都没了的，把它的旧画像/说明书清掉，别留着当幽灵能力。
        只是停用（目录还在）的不动，免得再启用又要重新画像。"""
        alive = live | {PLUGIN_NAME}
        base = self._plugins_dir()
        gone = []
        for folder in (PROFILE_DIR, MANUAL_DIR):
            try:
                files = os.listdir(folder)
            except Exception:
                continue
            for fn in files:
                if not fn.endswith((".json", ".txt")):
                    continue
                stem = fn.rsplit(".", 1)[0]
                if stem in alive or os.path.isdir(os.path.join(base, stem)):
                    continue
                try:
                    os.remove(os.path.join(folder, fn))
                    gone.append(fn)
                except Exception as e:
                    _log("清不掉旧档案 %s：%r" % (fn, e))
        if gone:
            _log("清掉 %d 个已卸载插件的旧档案：%s" % (len(gone), "、".join(gone[:8])))

    # ---------- 自兼容：让模型读插件，自己写规则 ----------
    RULES_CACHE = os.path.join(CACHE_DIR, "auto_rules.json")

    async def _llm_make_rules(self, cap):
        """让模型给这个插件写"人话 → 命令"的规则（命令多就分批问）"""
        try:
            provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if not provider:
            return []
        import json as _json

        cmds = [c for c in (cap.get("cmds") or []) if c.get("kind") not in ("tool", "regex")]
        if not cmds:
            return []
        rules, seen = [], set()
        chunk = 8
        for i in range(0, len(cmds), chunk):
            part = cmds[i:i + chunk]
            brief = _json.dumps({
                "plugin": cap["name"],
                "desc": cap.get("desc", ""),
                "commands": [{"cmd": c["cmd"], "doc": (c.get("doc") or "")[:40]} for c in part],
            }, ensure_ascii=False)
            prompt = (
                "你是插件指令适配器。下面是一个 AstrBot 插件的部分指令，请为它们生成"
                "「人话到命令」的转换规则。\n"
                "要求：\n"
                "1) 每条规则：pattern（Python 正则，有参数时用捕获组）、template（命令模板，{1} 是第一个捕获组）、label（名字）。\n"
                "2) 每条指令最多 2 条规则；不需要参数的指令可以用固定模板（如 /签到）。\n"
                "3) 正则必须单行可编译；不要过于宽泛。\n"
                "4) 只输出 JSON 数组，不要解释、不要代码块。\n"
                "插件信息：" + brief
            )
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=prompt, contexts=[], system_prompt=None), timeout=60)
                text = (getattr(resp, "completion_text", "") or "").strip()
            except Exception as e:
                _log("生成规则失败(%s 第%d批): %r" % (cap["name"], i // chunk + 1, e))
                continue
            data = self._parse_rules_json(text) or []
            for r in data:
                if not isinstance(r, dict):
                    continue
                pat, tpl = str(r.get("pattern") or ""), str(r.get("template") or "")
                label = str(r.get("label") or "自动")
                if not pat or not tpl.startswith("/"):
                    continue
                try:
                    re.compile(pat)
                except Exception:
                    continue
                key = (pat, tpl)
                if key in seen:
                    continue
                seen.add(key)
                rules.append({"pattern": pat, "template": tpl, "label": label,
                              "plugin": cap["plugin"], "auto": True})
        return rules[:16]

    @staticmethod
    def _parse_rules_json(text):
        """容忍代码块、前后废话、尾逗号"""
        t = (text or "").strip()
        if not t:
            return None
        # 去掉 ```json ``` 包裹
        t = re.sub(r"^\s*```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
        i, j = t.find("["), t.rfind("]")
        if i < 0 or j <= i:
            return None
        body = t[i:j + 1]
        for attempt in (body, re.sub(r",\s*([}\]])", r"\1", body)):
            try:
                data = json.loads(attempt)
                return data if isinstance(data, list) else None
            except Exception:
                continue
        return None

    async def _gen_rules(self, force=False):
        caps = self._scan(force)
        fp = self._fp
        cached = _read_json(self.RULES_CACHE, {})
        if not force and cached.get("fp") == fp and cached.get("rules") is not None:
            return cached.get("rules") or []
        rules = []
        for cap in caps:
            if not (cap.get("cmds") or []):
                _log("%s：没有命令，跳过" % cap["name"])
                continue
            got = await self._llm_make_rules(cap)
            if not got:
                _log("%s：第一次没生成出来，重试" % cap["name"])
                got = await self._llm_make_rules(cap)
            if got:
                rules.extend(got)
                _log("%s：生成 %d 条规则" % (cap["name"], len(got)))
            else:
                _log("%s：仍然没有规则（这个插件可能只有工具，或命令不适合自然语言）" % cap["name"])
        _write_json(self.RULES_CACHE, {"fp": fp, "ts": time.time(), "rules": rules})
        return rules

    def _auto_rules(self):
        cached = _read_json(self.RULES_CACHE, {})
        return cached.get("rules") or []

    IMG_NOUNS = ("图", "图片", "插画", "壁纸", "照片", "头像")
    TALK_CMDS = ("传话", "带话", "去群里说")

    def _rule_guard_ok(self, text, rule, arg):
        """自动规则命中之后的兜底校验：模型写的正则经常太宽"""
        tpl = str(rule.get("template") or "")
        cmd = tpl.lstrip("/").split(" ", 1)[0]
        # 取图类命令：句子里必须真的在要图，不能因为有个"图"字就发
        if cmd in ("p", "搜图", "来图", "图"):
            if not any(k in text for k in self.IMG_NOUNS):
                return False
        # 传话类：必须有明确的"传话/带话"，或者冒号分隔
        if cmd in self.TALK_CMDS:
            if not re.search(r"传话|带话|去群里说", text) and not re.search(r"[：:]", text):
                return False
        if not arg:
            return False
        if arg.endswith(("什么了", "什么", "了没", "吗", "呢")):
            return False
        if arg[0] in "你我他她它这那谁啥什么怎":
            return False
        return True

    # 自动规则是某个时刻的快照，插件卸载/停用后它并不知道；这里只提示一次，别刷屏
    _AR_GONE_LOGGED = set()

    def _dispatch_by_auto(self, text):
        live = set()
        for cap in self._scan():
            live.add(cap.get("plugin") or "")
        for r in self._auto_rules():
            pl = r.get("plugin") or ""
            if pl and pl not in live:
                # 这条规则属于一个已经不在能力表里的插件，照着它执行只会扑空
                if pl not in self._AR_GONE_LOGGED:
                    self._AR_GONE_LOGGED.add(pl)
                    _log("自动规则跳过：%s 已不在能力表，它的旧规则不再参与" % pl)
                continue
            try:
                m = re.match(r["pattern"], text, re.S | re.I)
            except Exception:
                continue
            if not m:
                continue
            tpl = r["template"]
            cmd = tpl
            if "{1}" in tpl:
                arg = _clean_arg(m.group(1) if m.groups() else "", limit=30)
                if tpl.lstrip("/").split(" ", 1)[0] in ("p", "搜图", "来图", "图"):
                    # 取图命令的参数只要题材，"的图/插画"这类尾巴去掉
                    arg = re.sub(r"(?:的)?(?:图|图片|插画|壁纸|照片|表情包)$", "", arg).strip()
                if not self._rule_guard_ok(text, r, arg):
                    continue
                cmd = tpl.replace("{1}", arg)
                # 模板里用不到的占位符（比如 {2}）直接丢掉，别发出去
                cmd = re.sub(r"\{\d+\}", "", cmd)
                cmd = re.sub(r"\s+", " ", cmd).strip()
            return cmd, "自动规则:" + r.get("label", "")
        return None, None

    # ---------- 能力画像：一个插件一份，按指纹增量分析 ----------
    PROFILE_PROMPT = (
        "你在给一个聊天机器人建「能力卡片」。下面是一个 AstrBot 插件的资料。\n"
        "只输出一个 JSON 对象，字段如下（都用中文，短）：\n"
        '  "what": 一句话说明它到底能帮用户做什么（不超过30字）\n'
        '  "when": 3-5 条「用户什么情况下会需要它」的短句\n'
        '  "examples": 3-5 条用户真的会说的、应该触发它的话（口语、短）\n'
        '  "avoid": 2-3 条听起来相关但不该触发它的话\n'
        '  "limits": 1-3 条它做不到的事\n'
        '  "commands": 命令级说明，数组，每条：'
        '{"cmd": 命令完整写法（含分组前缀，例如 表情管理 添加表情）, '
        '"what": 这条命令干什么（20字内）, '
        '"when": 什么时候该用它（15字内）, '
        '"avoid": 什么时候别用它（15字内，没有就留空）, '
        '"args": 参数怎么填（20字内，没有就留空）, '
        '"prep": 用之前先做什么、合法值从哪查（25字内，没有就留空）, '
        '"template": 调用格式，参数处写 {arg}（例如 #ncm点歌 {arg}、/表情管理 添加表情 {arg}；'
        '没参数就写命令本身）}。'
        '最多列 12 条，优先列用户真会用的；同一插件里容易混的命令要写清区别。\n'
        "不要编造资料里没有的能力；资料不足以判断时字段留空数组。不要解释、不要代码块。\n"
        "资料里有「docs」字段（插件自带的说明文档）时，参数、合法值从哪查、前置条件、"
        "典型流程优先从里面找，找不到才按命令名推断。\n"
        "插件资料："
    )

    def _profile_path(self, plugin):
        return os.path.join(PROFILE_DIR, "%s.json" % plugin)

    def _cap_fp(self, cap):
        """单个插件的指纹：描述 + 入口清单 + metadata/main.py 的修改时间"""
        h = hashlib.md5()
        h.update(str(cap.get("name") or "").encode())
        h.update(str(cap.get("desc") or "").encode())
        for c in (cap.get("cmds") or []):
            h.update(("%s|%s|%s;" % (
                c.get("cmd"), c.get("kind"), ",".join(c.get("params") or []))).encode())
        try:
            folder = os.path.join(self._plugins_dir(), cap.get("plugin") or "")
            for fn in ("metadata.yaml", "main.py", "README.md", "readme.md", "README_zh.md"):
                p = os.path.join(folder, fn)
                if os.path.isfile(p):
                    h.update(("%s:%d;" % (fn, os.path.getmtime(p))).encode())
        except Exception:
            pass
        return h.hexdigest()[:12]

    def _profile(self, plugin):
        """读画像（带内存缓存，避免每次注入都读盘）"""
        if not plugin:
            return {}
        cache = getattr(self, "_profile_cache", None)
        if cache is None:
            cache = self._profile_cache = {}
        if plugin in cache:
            return cache[plugin]
        prof = _read_json(self._profile_path(plugin), {}) or {}
        cache[plugin] = prof
        return prof

    def _profile_stale(self, cap):
        prof = self._profile(cap.get("plugin"))
        if prof.get("fallback"):        # 上次是拿插件说明顶的，等模型能用了再试
            return True
        if (not prof.get("what")) or prof.get("fp") != self._cap_fp(cap):
            return True
        # 命令多的插件，光有插件级说明不够，得补命令级（老画像没这个字段）
        real = [c for c in (cap.get("cmds") or []) if c.get("kind") != "tool"]
        if len(real) >= 2 and not prof.get("commands"):
            return True
        return False

    def _profile_brief(self, cap):
        """喂给模型的素材：说明文档 + 命令（带完整写法）+ 工具参数，不喂源码"""
        doc = ""
        try:
            doc = _read_plugin_doc(os.path.join(self._plugins_dir(), cap.get("plugin") or ""))
        except Exception:
            doc = ""
        return {
            "plugin": cap.get("name"),
            "desc": cap.get("desc"),
            "commands": [{"cmd": c.get("full") or c["cmd"], "doc": c.get("doc") or ""}
                         for c in (cap.get("cmds") or [])
                         if c.get("kind") != "tool"][:20],
            "tools": [{"tool": c["cmd"], "doc": c.get("doc") or "",
                       "params": c.get("params") or []}
                      for c in (cap.get("cmds") or [])
                      if c.get("kind") == "tool"][:10],
            "docs": doc,
        }

    def _cmd_detail(self, cap, cmd):
        """某条命令的命令级说明（画像里生成过才有）"""
        prof = self._profile(cap.get("plugin")) or {}
        for d in (prof.get("commands") or []):
            if str(d.get("cmd") or "") == cmd:
                return d
        return {}

    @staticmethod
    def _parse_obj(text):
        """从模型输出里抠一个 JSON 对象，容忍代码块和尾逗号"""
        t = (text or "").strip()
        if not t:
            return None
        t = re.sub(r"^\s*```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            return None
        body = t[i:j + 1]
        for attempt in (body, re.sub(r",\s*([}\]])", r"\1", body)):
            try:
                data = json.loads(attempt)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return None

    async def _llm_profile(self, cap):
        try:
            provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if not provider:
            return None
        brief = json.dumps(self._profile_brief(cap), ensure_ascii=False)
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=self.PROFILE_PROMPT + brief,
                                   contexts=[], system_prompt=None), timeout=60)
            text = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            _log("分析 %s 失败: %r" % (cap.get("plugin"), e))
            return None
        return self._parse_obj(text)

    async def _make_profile(self, cap):
        got = await self._llm_profile(cap)
        if not got:
            got = await self._llm_profile(cap)      # 重试一次，别因为一次抖动就空着
        fallback = False
        if not got:
            got = {"what": (cap.get("desc") or "")[:40]}
            fallback = True
            _log("%s：画像没生成出来，先用插件说明顶着（下次启动再试）" % cap.get("plugin"))
        prof = {
            "plugin": cap.get("plugin"),
            "fp": self._cap_fp(cap),
            "ts": time.time(),
            "fallback": fallback,
            "what": str(got.get("what") or "")[:80],
            "when": [str(x)[:40] for x in (got.get("when") or []) if x][:5],
            "examples": [str(x)[:40] for x in (got.get("examples") or []) if x][:5],
            "avoid": [str(x)[:40] for x in (got.get("avoid") or []) if x][:5],
            "limits": [str(x)[:40] for x in (got.get("limits") or []) if x][:4],
            "entry": [{"kind": c.get("kind"), "name": c.get("cmd"),
                       "params": c.get("params") or []}
                      for c in (cap.get("cmds") or [])][:20],
            "commands": [
                {"cmd": str(d.get("cmd") or "")[:30],
                 "what": str(d.get("what") or "")[:30],
                 "when": str(d.get("when") or "")[:20],
                 "avoid": str(d.get("avoid") or "")[:20],
                 "args": str(d.get("args") or "")[:30],
                 "prep": str(d.get("prep") or "")[:40],
                 "template": str(d.get("template") or "")[:60]}
                for d in (got.get("commands") or [])
                if isinstance(d, dict) and d.get("cmd")
            ][:12],
        }
        old = self._profile(cap.get("plugin")) or {}
        updated = bool(old.get("what")) and old.get("fp") != prof["fp"]
        _write_json(self._profile_path(cap.get("plugin")), prof)
        self._profile_cache = getattr(self, "_profile_cache", {})
        self._profile_cache[cap.get("plugin")] = prof
        self._save_manual(cap)      # 说明书跟着画像一起更新
        if updated:
            _log("插件有变化，画像和说明书已重写：%s" % cap.get("name"))
        _log("画像完成：%s → %s" % (cap.get("name"), prof["what"]))
        return prof

    async def ensure_profiles(self, force=False, limit=3):
        """只分析「没有画像」或「指纹变了」的插件，一次最多 limit 个"""
        done = []
        for cap in self._scan(force):
            if not force and not self._profile_stale(cap):
                if not os.path.isfile(self._manual_path(cap.get("plugin"))):
                    self._save_manual(cap)      # 画像旧但说明书缺了，补一份不花钱
                continue
            await self._make_profile(cap)
            done.append(cap)
            if len(done) >= limit:
                break
        return done

    # ---------- 简易说明书：一个插件一份，存着，问到再发 ----------
    def _manual_path(self, plugin):
        return os.path.join(MANUAL_DIR, "%s.txt" % plugin)

    def _manual_text(self, cap):
        prof = self._profile(cap.get("plugin")) or {}
        lines = ["【%s】%s" % (
            cap.get("name"), prof.get("what") or (cap.get("desc") or "")[:60])]
        when = prof.get("when") or []
        if when:
            lines.append("什么时候用：" + "；".join(when[:4]))
        ex = prof.get("examples") or []
        if ex:
            lines.append("这样说就行：")
            for e in ex[:5]:
                lines.append("  · " + e)
        cmds = [c for c in (cap.get("cmds") or []) if c.get("kind") not in ("tool", "regex")][:8]
        if cmds:
            lines.append("命令：")
            for c in cmds:
                d = self._cmd_detail(cap, c["cmd"])
                what = d.get("what") or (c.get("doc") or "").split("\n")[0][:40]
                line = "  · /%s%s" % ((c.get("full") or c["cmd"]), ("　" + what) if what else "")
                if d.get("args"):
                    line += "［参数：%s］" % d["args"]
                if d.get("prep"):
                    line += "［先：%s］" % d["prep"]
                if d.get("when"):
                    line += "（%s）" % d["when"]
                if d.get("avoid"):
                    line += "［别在：%s］" % d["avoid"]
                lines.append(line)
        tools = [c for c in (cap.get("cmds") or []) if c.get("kind") == "tool"][:4]
        if tools:
            lines.append("工具（她自己能调）：")
            for t in tools:
                p = "、".join(t.get("params") or [])
                lines.append("  · %s%s" % (t["cmd"], ("（参数 %s）" % p) if p else ""))
        lim = prof.get("limits") or []
        if lim:
            lines.append("做不到：" + "；".join(lim[:3]))
        return "\n".join(lines)

    def _save_manual(self, cap):
        try:
            txt = self._manual_text(cap)
            os.makedirs(MANUAL_DIR, exist_ok=True)
            with io.open(self._manual_path(cap.get("plugin")), "w", encoding="utf-8") as f:
                f.write(txt)
            return txt
        except Exception as e:
            _log("写说明书失败: %r" % e)
            return ""

    def _manual(self, cap):
        """优先读存档；没有就现造一份并存下来"""
        p = self._manual_path(cap.get("plugin"))
        if os.path.isfile(p):
            try:
                return io.open(p, encoding="utf-8").read().strip()
            except Exception:
                pass
        return self._save_manual(cap)

    MANUAL_HINTS = ("怎么用", "说明书", "有什么功能", "有什么命令", "怎么玩", "教程", "还能干嘛")

    def _manual_query(self, text):
        return any(h in (text or "") for h in self.MANUAL_HINTS)

    def _pick_cap_for(self, text):
        """按插件名/关键词找最可能是他问的那个能力"""
        t = text or ""
        for c in self._scan():
            name = c.get("name") or ""
            if name and name in t:
                return c
            if c.get("plugin") and c["plugin"] in t:
                return c
        hits = self.match(t)
        return hits[0] if hits else None

    @_hook("on_plugin_loaded")
    async def on_plugin_loaded(self, metadata):
        """有插件加载完成：如果它还没有画像，后台补一份（不阻塞启动）"""
        try:
            name = str(getattr(metadata, "root_dir_name", "") or "")
            if not name or name == PLUGIN_NAME:
                return
            cap = next((c for c in self._scan() if c.get("plugin") == name), None)
            if not cap or not self._profile_stale(cap):
                return
            if getattr(self, "_profiling", False):
                return
            _log("检测到插件加载：%s，排队补画像" % name)

            async def _bg():
                self._profiling = True
                try:
                    await self.ensure_profiles(limit=1)
                except Exception as e2:
                    _log("后台补画像失败: %r" % e2)
                finally:
                    self._profiling = False

            t = asyncio.get_event_loop().create_task(_bg())
            self._tasks = getattr(self, "_tasks", set())
            self._tasks.add(t)
        except Exception as e:
            _log("插件加载钩子处理失败: %r" % e)

    # ---------- WebUI 接口（思考流页面用） ----------
    def _register_web_apis(self):
        routes = [
            ("/astrbot_plugin_self_aware/files", self._api_files, ["GET"], "列出思考流文件"),
            ("/astrbot_plugin_self_aware/content", self._api_content, ["GET"], "读取思考流文件"),
            ("/astrbot_plugin_self_aware/clear", self._api_clear, ["POST"], "清空思考流"),
            ("/astrbot_plugin_self_aware/level", self._api_level, ["POST"], "设置推理等级"),
            ("/astrbot_plugin_self_aware/caps", self._api_caps, ["GET"], "能力面板数据"),
            ("/astrbot_plugin_self_aware/actions", self._api_actions, ["GET"], "执行记录"),
            ("/astrbot_plugin_self_aware/analyze", self._api_analyze, ["POST"], "分析插件能力"),
            ("/astrbot_plugin_self_aware/manual", self._api_manual, ["GET"], "读取说明书"),
            ("/astrbot_plugin_self_aware/reset", self._api_reset, ["POST"], "重置缓存与清理"),
        ]
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(route, handler, methods, desc)
            except Exception as e:
                _log("注册接口 %s 失败: %r" % (route, e))

    async def _api_files(self):
        if jsonify is None:
            return {"status": "error", "msg": "quart 不可用"}
        items = []
        for p in self._thought_files():
            try:
                st = os.stat(p)
            except Exception:
                continue
            items.append({
                "name": os.path.basename(p),
                "size": st.st_size,
                "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            })
        return jsonify({
            "status": "ok",
            "dir": str(self._cfg("thought_dir", "") or THOUGHT_DIR),
            "effort": str(self._cfg("effort", "中")),
            "show": bool(self._cfg("show_in_chat", False)),
            "items": items,
        })

    async def _api_content(self):
        if jsonify is None:
            return {"status": "error"}
        try:
            name = os.path.basename(str(request.args.get("name") or ""))
            if not name.endswith(".log"):
                return jsonify({"status": "error", "msg": "文件名不对"})
            p = os.path.join(str(self._cfg("thought_dir", "") or THOUGHT_DIR), name)
            if not os.path.isfile(p):
                return jsonify({"status": "error", "msg": "文件不存在"})
            data = io.open(p, encoding="utf-8", errors="replace").read()
            return jsonify({"status": "ok", "name": name, "text": data[-200000:]})
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)})

    async def _api_clear(self):
        if jsonify is None:
            return {"status": "error"}
        n = 0
        for p in self._thought_files():
            try:
                os.remove(p)
                n += 1
            except Exception:
                pass
        return jsonify({"status": "ok", "removed": n})

    async def _api_caps(self):
        if jsonify is None:
            return {"status": "error", "msg": "quart 不可用"}
        try:
            info = self._runtime_tools()
            blocked = set((_read_json(TOOL_STATE, {}) or {}).get("blocked") or [])
            items = []
            for c in self._scan():
                prof = self._profile(c.get("plugin"))
                cmds = [x for x in (c.get("cmds") or []) if x.get("kind") != "tool"]
                tools = [x for x in (c.get("cmds") or []) if x.get("kind") == "tool"]
                items.append({
                    "plugin": c.get("plugin"),
                    "name": c.get("name"),
                    "what": prof.get("what") or "",
                    "stale": self._profile_stale(c),
                    "fallback": bool(prof.get("fallback")),
                    "manual": os.path.isfile(self._manual_path(c.get("plugin"))),
                    "commands": len(cmds),
                    "tools": len(tools),
                })
            items.sort(key=lambda x: (not x["stale"], x["name"] or ""))
            return jsonify({
                "status": "ok",
                "dir": str(CACHE_DIR),
                "items": items,
                "tool_count": len(info),
                "blocked": sorted(blocked),
            })
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)})

    async def _api_actions(self):
        """面板用：最近执行记录（她做过什么、成没成）"""
        if jsonify is None:
            return {"status": "error", "msg": "quart 不可用"}
        try:
            limit = 30
            try:
                q = request.args.get("limit")
                if q:
                    limit = max(1, min(int(q), 100))
            except Exception:
                limit = 30
            rows = self._act_all()[-limit:]
            rows.reverse()
            items = []
            for r in rows:
                items.append({
                    "time": time.strftime("%m-%d %H:%M:%S", time.localtime(float(r.get("ts") or 0))),
                    "cmd": r.get("cmd") or "",
                    "args": r.get("args") or "",
                    "target": r.get("target") or "",
                    "status": r.get("status") or "",
                    "note": r.get("note") or "",
                    "took": r.get("took"),
                })
            return jsonify({"status": "ok", "items": items, "total": len(self._act_all())})
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)})

    async def _api_analyze(self):
        if jsonify is None:
            return {"status": "error"}
        try:
            data = (await request.get_json(silent=True)) or {}
            force = bool(data.get("force"))
            done = await self.ensure_profiles(force=force, limit=20)
            return jsonify({"status": "ok", "done": [c.get("name") for c in done]})
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)})

    async def _api_reset(self):
        """一键重置：清缓存/画像/说明书（可选连日志一起清），然后重扫"""
        if jsonify is None:
            return {"status": "error"}
        try:
            data = (await request.get_json(silent=True)) or {}
            scope = str(data.get("scope") or "build").lower()
            removed = 0
            if scope in ("build", "all"):
                for p in (CAP_CACHE, TOOL_STATE):
                    if os.path.isfile(p):
                        try:
                            os.remove(p)
                            removed += 1
                        except Exception:
                            pass
                for d in (PROFILE_DIR, MANUAL_DIR):
                    if os.path.isdir(d):
                        removed += sum(len(fs) for _, _, fs in os.walk(d))
                        shutil.rmtree(d, ignore_errors=True)
                self._caps = []
                self._fp = ""
                self._profile_cache = {}
                self._reg_cache = None
                self._scan(force=True)
            if scope in ("logs", "all"):
                for sub in ("logs", "thoughts"):
                    d = os.path.join(CACHE_DIR, sub)
                    if os.path.isdir(d):
                        removed += sum(len(fs) for _, _, fs in os.walk(d))
                        shutil.rmtree(d, ignore_errors=True)
            _log("手动重置：scope=%s，删了 %d 个文件" % (scope, removed))
            return jsonify({"status": "ok", "scope": scope,
                            "removed": removed, "caps": len(self._caps or [])})
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)})

    async def _api_manual(self):
        if jsonify is None:
            return {"status": "error"}
        try:
            name = str(request.args.get("name") or "").strip()
            cap = next((c for c in self._scan() if c.get("plugin") == name), None)
            if not cap:
                return jsonify({"status": "error", "msg": "没有这个插件"})
            return jsonify({"status": "ok", "text": self._manual(cap)})
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)})

    async def _api_level(self):
        if jsonify is None:
            return {"status": "error"}
        try:
            data = (await request.get_json(silent=True)) or {}
            key = str(data.get("effort") or "").strip()
            if key not in LEVELS:
                return jsonify({"status": "error", "msg": "只认 关/低/中/高"})
            self.config["effort"] = key
            ok = self._apply_effort()
            return jsonify({"status": "ok", "effort": key, "applied": ok})
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)})

    # ---------- 推理等级 ----------
    def _apply_effort(self, provider=None):
        try:
            level = str(self._cfg("effort", "中") or "中")
            effort = LEVELS.get(level, "medium")
            prov = provider or self.context.get_using_provider()
            if not prov:
                return False
            cfg = getattr(prov, "provider_config", None)
            if not isinstance(cfg, dict):
                return False
            body = cfg.get("custom_extra_body")
            if not isinstance(body, dict):
                body = {}
                cfg["custom_extra_body"] = body
            if effort == "none":
                body["reasoning_effort"] = "none"
                body["thinking"] = {"type": "disabled"}
            else:
                body["reasoning_effort"] = effort
                body.pop("thinking", None)
            return True
        except Exception as e:
            _log("设置推理等级失败: %r" % e)
            return False

    @filter.command("推理")
    async def cmd_level(self, event: AstrMessageEvent):
        """查看或切换推理等级：/推理 关|低|中|高"""
        arg = (event.message_str or "").replace("/推理", "", 1).strip()
        if not arg:
            yield event.plain_result("当前推理等级：%s（可选：关 / 低 / 中 / 高）" % self._cfg("effort", "中"))
            return
        key = arg[0]
        if key not in LEVELS:
            yield event.plain_result("只认这四个：关 / 低 / 中 / 高")
            return
        self.config["effort"] = key
        ok = self._apply_effort()
        yield event.plain_result("推理等级已设为「%s」（%s）%s" % (
            key, LEVELS[key], "" if ok else "，但当前 provider 没设上"))

    # ---------- 思考流 ----------
    def _thought_files(self):
        d = str(self._cfg("thought_dir", "") or "").strip() or THOUGHT_DIR
        os.makedirs(d, exist_ok=True)
        out = []
        try:
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if name.endswith(".log") and os.path.isfile(p):
                    out.append((os.path.getmtime(p), p))
        except Exception:
            return []
        out.sort(reverse=True)
        return [p for _, p in out]

    def _dig_reasoning(self, resp):
        cands = []
        try:
            cands.append(getattr(resp, "reasoning_content", None))
        except Exception:
            pass
        try:
            rc = getattr(resp, "raw_completion", None)
            choice = rc.choices[0]
            for obj in (getattr(choice, "message", None), getattr(choice, "delta", None)):
                if obj is None:
                    continue
                for key in ("reasoning_content", "reasoning", "thinking"):
                    v = getattr(obj, key, None)
                    if v is None:
                        extra = getattr(obj, "model_extra", None)
                        if isinstance(extra, dict):
                            v = extra.get(key)
                    if v:
                        cands.append(v)
        except Exception:
            pass
        for v in cands:
            if not v:
                continue
            v = str(v).strip()
            if v:
                return v
        return ""

    @filter.on_llm_response()
    async def capture_reasoning(self, event: AstrMessageEvent, resp):
        """把思考流写进文件；可选发到聊天"""
        try:
            if not self._cfg("capture_reasoning", True):
                return
            text = self._dig_reasoning(resp)
            if not text:
                return
            import datetime as _dt
            d = str(self._cfg("thought_dir", "") or "").strip() or THOUGHT_DIR
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, _dt.datetime.now().strftime("%Y-%m-%d.log"))
            head = "[%s] %s | %s" % (
                _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                self._cfg("effort", "中"), str(event.unified_msg_origin))
            body = (event.message_str or "")[:120]
            with io.open(p, "a", encoding="utf-8") as f:
                f.write("%s\n问：%s\n%s\n%s\n" % (head, body, text, "-" * 40))
            if self._cfg("show_in_chat", False):
                if self._cfg("only_private", True) and ":GroupMessage:" in str(event.unified_msg_origin):
                    return
                limit = int(self._cfg("show_max_chars", 400))
                from astrbot.core.message.message_event_result import MessageChain
                await self.context.send_message(
                    event.unified_msg_origin,
                    MessageChain().message("（思考流 %s 字）\n%s" % (len(text), text[:limit])))
        except Exception as e:
            _log("记录思考流失败: %r" % e)

    @filter.command("思考流")
    async def cmd_thought(self, event: AstrMessageEvent):
        """思考流：/思考流 开|关|最后 N|清空|目录"""
        arg = (event.message_str or "").replace("/思考流", "", 1).strip()
        if not arg:
            yield event.plain_result("思考流：%s｜目录：%s\n用法：/思考流 开 / 关 / 最后 3 / 清空" % (
                "开" if self._cfg("show_in_chat", False) else "关",
                str(self._cfg("thought_dir", "") or THOUGHT_DIR)))
            return
        head = arg.split()[0]
        if head in ("开", "关"):
            self.config["show_in_chat"] = (head == "开")
            yield event.plain_result("思考流显示：%s" % head)
            return
        if head == "清空":
            n = 0
            for p in self._thought_files():
                try:
                    os.remove(p)
                    n += 1
                except Exception:
                    pass
            yield event.plain_result("清掉了 %d 个文件" % n)
            return
        if head == "最后":
            n = 1
            parts = arg.split()
            if len(parts) > 1 and parts[1].isdigit():
                n = max(1, min(20, int(parts[1])))
            files = self._thought_files()
            if not files:
                yield event.plain_result("还没有记录。")
                return
            chunks = []
            try:
                data = io.open(files[0], encoding="utf-8").read()
                chunks = [b for b in data.split("-" * 40) if b.strip()][-n:]
            except Exception:
                pass
            yield event.plain_result("\n---\n".join(c.strip()[:600] for c in chunks) or "读不到内容。")
            return
        yield event.plain_result("用法：/思考流 开 / 关 / 最后 3 / 清空")

    # ---------- 语义匹配 ----------
    @staticmethod
    def _grams(text):
        t = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", text or "")
        out = set()
        for i in range(len(t) - 1):
            out.add(t[i:i + 2])
        if len(t) == 1:
            out.add(t)
        return out

    def match(self, text, topk=3):
        caps = self._scan()
        g = self._grams(text)
        if not g:
            return []
        scored = []
        for c in caps:
            hay = c["name"] + c["desc"] + " ".join(c.get("tags") or [])
            hay += " " + " ".join((x["cmd"] + x["doc"]) for x in (c.get("cmds") or []))
            prof = self._profile(c.get("plugin"))
            if prof:
                # 画像里的"用户会怎么说"比插件作者写的 desc 更贴近真实说法
                hay += " " + " ".join((prof.get("examples") or []) + (prof.get("when") or []))
            cg = self._grams(hay)
            if not cg:
                continue
            hit = len(g & cg)
            if not hit:
                continue
            score = hit / (len(g) ** 0.5)
            # 台词里直接说了插件名（“表情包怎么用”）→ 这个最可信
            name_hit = len(g & self._grams(c.get("name") or ""))
            if name_hit:
                score += 0.8 * name_hit
            scored.append((score, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        need = float(self._cfg("match_threshold", 0.7))
        return [c for s, c in scored[:topk] if s >= need]

    # ---------- 生成提示 ----------
    # ---------- 跨会话能力：她要跟"别的会话"打交道时，单独提醒她 ----------
    CROSS_WORDS = ("群里", "群聊", "别的群", "另一个群", "那个群", "群号", "这个群",
                   "传话", "带话", "转发到", "发到群", "去群里", "让群里", "那边的群")
    CROSS_MARKS = ("传话", "带话", "转发", "群里", "群聊", "记录", "记忆", "指定群", "会话",
                   "to_group", "other_session", "cross")

    def _cross_block(self, text):
        """句子里在说"别的会话"时，把能跨会话用的入口列给她，别让她以为做不到"""
        t = text or ""
        if not any(w in t for w in self.CROSS_WORDS):
            return ""
        by_plugin = {}
        want_media = any(k in t for k in ("图", "图片", "表情", "照片", "插画"))
        for cap in self._scan():
            for c in (cap.get("cmds") or []):
                name = c.get("cmd") or ""
                full = c.get("full") or name
                doc = c.get("doc") or ""
                blob = (name + full + doc).lower()
                hits = sum(1 for k in self.CROSS_MARKS if k in blob)
                if not hits:
                    continue
                # 用户提到"图/表情"→ 优先带图的入口
                if want_media and ("图" in blob or "image" in blob):
                    hits += 2
                by_plugin.setdefault(cap.get("name") or "?", []).append(
                    (hits, full, doc.split("\n")[0][:30]))
        if not by_plugin:
            return ""
        queues = []
        for pname, lst in by_plugin.items():
            lst.sort(key=lambda x: (-x[0], x[1]))
            queues.append([pname, lst])
        queues.sort(key=lambda q: -q[1][0][0])
        lines, seen = [], set()
        while len(lines) < 8 and queues:
            for q in list(queues):
                if not q[1]:
                    queues.remove(q)
                    continue
                _, full, doc = q[1].pop(0)
                if full in seen:
                    continue
                seen.add(full)
                lines.append("- /%s%s（%s）" % (full, ("　" + doc) if doc else "", q[0]))
                if len(lines) >= 8:
                    break
        if not lines:
            return ""
        return ("【跟别的会话打交道时，你确实能用这些（之前做过，别直接说做不到）】\n"
                + "\n".join(lines)
                + "\n（要在哪个群里做，就把群号填进 target："
                  "run_capability(command=命令名, args=参数, target=群号)，命令会在那个群里执行，"
                  "不是做完再转发过去。）")

    # ---------- 正则路由：不是命令，但用户说出来就该触发 ----------
    def _regex_dispatch(self, text):
        t = (text or "").strip()
        if not t:
            return None
        for cap in self._scan():
            for c in (cap.get("cmds") or []):
                if c.get("kind") != "regex":
                    continue
                pat = c.get("pattern") or ""
                try:
                    if re.search(pat, t, re.S):
                        return cap, c
                except Exception:
                    continue
        return None

    async def _invoke_regex(self, event, entry, text):
        """直接调那条正则路由的 handler：把原话放回 message_str，让它自己解析"""
        try:
            from astrbot.core.star.star_handler import (
                EventType, star_handlers_registry)
            handlers = star_handlers_registry.get_handlers_by_event_type(
                EventType.AdapterMessageEvent, only_activated=False)
        except Exception:
            handlers = []
        h = None
        want = entry.get("pattern") or ""
        for cand in handlers:
            for f in (getattr(cand, "event_filters", None) or []):
                pat = getattr(f, "regex_str", None)
                if not pat:
                    continue
                ok = (pat == want) if want else bool(re.search(pat, text, re.S))
                if ok:
                    h = cand
                    break
            if h:
                break
        if h is None:
            self._last_error = "找不到这条正则路由"
            _log("找不到正则路由：%s" % want[:40])
            return False
        old = event.message_str
        event.message_str = text
        try:
            event.message_obj.message_str = text
        except Exception:
            pass
        try:
            results = await self._run_handler(h.handler, event, "")
            self._last_output = []
            for r in results:
                await self._emit_result(event, r)
                txt = self._result_text(r)
                if txt:
                    self._last_output.append(txt)
            ok = True
            _log("已执行正则路由 %s（%s）%s" % (
                entry.get("cmd"), "有返回文字" if self._last_output else "插件直接发了消息",
                ("：" + self._last_output[0][:60]) if self._last_output else ""))
            return ok
        except Exception as e:
            self._last_error = repr(e)
            _log("执行正则路由 %s 失败: %r" % (entry.get("cmd"), e))
            return False
        finally:
            try:
                event.message_str = old
            except Exception:
                pass

    # ---------- 她做过什么：动作台账 ----------
    ACTIONS_FILE = os.path.join(CACHE_DIR, "actions.json")

    def _act_all(self):
        try:
            d = _read_json(self.ACTIONS_FILE, []) or []
        except Exception:
            d = []
        return d if isinstance(d, list) else []

    def _act_save(self, rows):
        try:
            _write_json(self.ACTIONS_FILE, rows[-50:])
        except Exception:
            pass

    def _act_start(self, cmd, args="", target=""):
        rows = self._act_all()
        rid = int(time.time() * 1000)
        rows.append({
            "id": rid, "ts": time.time(), "cmd": str(cmd or "")[:40],
            "args": str(args or "")[:60], "target": str(target or "")[:60],
            "status": "running", "note": "",
        })
        self._act_save(rows)
        return rid

    def _act_end(self, rid, ok, note=""):
        rows = self._act_all()
        for r in rows:
            if r.get("id") == rid:
                r["status"] = "done" if ok else "failed"
                r["note"] = str(note or "")[:80]
                r["took"] = round(time.time() - float(r.get("ts") or time.time()), 1)
                break
        self._act_save(rows)

    def _act_recent(self, n=3):
        return self._act_all()[-n:]

    def _act_text(self, n=3):
        """给她自己看的一小段：刚才做了什么、成没成"""
        rows = self._act_recent(n)
        if not rows:
            return ""
        lines = []
        for r in rows:
            ago = int(time.time() - float(r.get("ts") or time.time()))
            when = "刚刚" if ago < 60 else ("%d 分钟前" % (ago // 60) if ago < 3600 else "%d 小时前" % (ago // 3600))
            st = {"running": "还在跑", "done": "成了", "failed": "没成"}.get(r.get("status"), "?")
            cmd = r.get("cmd") or ""
            args = r.get("args") or ""
            note = r.get("note") or ""
            seg = "- %s 你执行 %s %s → %s" % (when, cmd, args, st)
            if note:
                seg += "：%s" % note
            lines.append(seg)
        return "\n".join(lines)

    TIMING_FILE = os.path.join(CACHE_DIR, "timings.json")

    def _timing(self, cmd):
        """这条命令以前跑了多久：(平均秒数, 次数, 慢了几次)"""
        try:
            d = _read_json(self.TIMING_FILE, {}) or {}
        except Exception:
            d = {}
        v = d.get(cmd) or {}
        return float(v.get("avg") or 0), int(v.get("n") or 0), int(v.get("slow") or 0)

    def _note_timing(self, cmd, seconds=None, slow=False):
        if not cmd:
            return
        try:
            d = _read_json(self.TIMING_FILE, {}) or {}
        except Exception:
            d = {}
        v = d.get(cmd) or {}
        n = int(v.get("n") or 0)
        avg = float(v.get("avg") or 0)
        cnt = int(v.get("slow") or 0)
        if slow:
            cnt += 1
        if seconds is not None:
            n += 1
            avg = (avg * (n - 1) + float(seconds)) / n
        d[cmd] = {"avg": round(avg, 1), "n": n, "slow": cnt}
        _write_json(self.TIMING_FILE, d)

    def _cmd_meaning(self, cmd):
        """这条命令实际在干什么（别让她把 p 理解成 P图）"""
        try:
            cap, short = self._resolve_cap_cmd(cmd)
            if not cap:
                return ""
            prof = self._profile(cap.get("plugin")) or {}
            for d in (prof.get("commands") or []):
                dc = str(d.get("cmd") or "")
                if dc and short and dc.startswith("#") and short in dc and d.get("what"):
                    return str(d.get("what"))[:50]
            for d in (prof.get("commands") or []):
                if short and short in str(d.get("cmd") or "") and d.get("what"):
                    return str(d.get("what"))[:50]
            return (cap.get("desc") or "")[:50]
        except Exception:
            return ""

    async def _self_report(self, event, label, args, ok=True, note="", phase="after"):
        """用她自己的口吻说话：phase=before 说"我要去做"，after 说"做完了/没成" """
        if not self._cfg("self_report", True) or event is None:
            return
        # 同一个会话 10 秒内最多一句，别啰嗦
        try:
            umo = getattr(event, "unified_msg_origin", "")
            now = time.time()
            if now - float(self._talk_ts.get(umo) or 0) < 10:
                return
            self._talk_ts[umo] = now
        except Exception:
            pass
        try:
            provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if not provider:
            return
        system_prompt = None
        try:
            pm = getattr(self.context, "persona_manager", None)
            if pm:
                p = await asyncio.wait_for(
                    pm.get_default_persona_v3(event.unified_msg_origin), timeout=5)
                system_prompt = getattr(p, "system_prompt", None)
        except Exception:
            system_prompt = None
        meaning = self._cmd_meaning(label)
        what = ("（这条命令实际是：%s）" % meaning) if meaning else ""
        if phase == "before":
            fact = "你正要去做这件事：%s %s%s" % (label, args, what)
            ask = ("用你自己的口吻，跟对方说一句你要去做什么。\n"
                   "要求：只说一句；口语；别客套；别说“请稍等/马上就好”这种客服话；"
                   "别解释步骤；别用括号；别把命令名照字面理解；可以带一点你自己的情绪或判断。")
        else:
            done = ("做成了" + ("，结果：" + note[:60] if note else "")) if ok else \
                   ("没做成" + ("，原因：" + note[:60] if note else ""))
            fact = "你刚做完这件事：%s %s%s，%s" % (label, args, what, done)
            ask = ("用你自己的口吻，跟对方说一句结果。\n"
                   "要求：只说一句；口语；别客套；别复述内容；别用括号；"
                   "可以评价一下、可以带点情绪；如果没成，就直说没成。")
        prompt = fact + "\n" + ask
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, contexts=[], system_prompt=system_prompt),
                timeout=15)
            line = (getattr(resp, "completion_text", "") or "").strip()
            if line:
                line = line.strip("（）()")
                await self._emit_result(event, line[:200])
                _log("她说（%s）：%s" % (phase, line[:60]))
        except Exception as e:
            _log("自我反馈失败: %r" % e)

    async def _grace(self, coro, seconds=8, label="", event=None, args="", target="",
                     report=False):
        """跑一会儿；没跑完就丢到后台，别把整条消息卡住"""
        t0 = time.time()
        rid = self._act_start(label, args, target)
        task = asyncio.create_task(coro)

        def _done(t):
            try:
                took = time.time() - t0
                if t.cancelled():
                    ok = False
                elif t.exception() is not None:
                    _log("后台命令 %s 失败: %r" % (label or "?", t.exception()))
                    ok = False
                else:
                    ok = bool(t.result())
                self._note_timing(label, seconds=took)
                note = ""
                try:
                    note = (self._last_output[0] if self._last_output else "") or (self._last_error or "")
                except Exception:
                    note = ""
                self._act_end(rid, ok, note)
                _log("命令 %s 用时 %.1fs（%s）" % (label or "?", took, "成功" if ok else "没成"))
                if report and event is not None:
                    asyncio.create_task(self._self_report(event, label, args, ok, note))
                elif (not ok) and event is not None:
                    asyncio.create_task(self._emit_result(
                        event, "（刚才那个命令没跑成，我看下日志）"))
            except Exception:
                pass

        task.add_done_callback(_done)
        try:
            done, _ = await asyncio.wait({task}, timeout=seconds)
        except Exception:
            done = set()
        if not done:
            _log("命令执行超过 %ds，转后台继续，结果出来会自己发" % seconds)
            self._note_timing(label, slow=True)
            if report and event is not None and not task.done():
                try:
                    await self._self_report(event, label, args, phase="before")
                except Exception:
                    pass
            elif event is not None:
                try:
                    await self._emit_result(event, "（这个要跑一会儿，我先去做，好了直接发出来）")
                except Exception:
                    pass
            return None
        try:
            return bool(task.result())
        except Exception as e:
            _log("执行出错: %r" % e)
            return False

    async def _invoke_text(self, event, entry, text):
        """按画像模板拼好的一句话，直接喂给插件：它自己会解析"""
        if entry.get("kind") == "regex":
            return await self._invoke_regex(event, entry, text)
        name = entry.get("cmd") or ""
        h = self._find_handler(name)
        if h is None:
            self._last_error = "找不到 %s 的处理函数" % name
            _log("找不到 %s 的处理函数" % name)
            return False
        old = event.message_str
        event.message_str = text
        try:
            event.message_obj.message_str = text
        except Exception:
            pass
        try:
            arg = text
            for w in (entry.get("full") or "", entry.get("cmd") or ""):
                if w and arg.startswith(w):
                    arg = arg[len(w):]
                    break
            arg = arg.lstrip("/# ").strip()
            results = await self._run_handler(h.handler, event, arg)
            self._last_output = []
            for r in results:
                await self._emit_result(event, r)
                tx = self._result_text(r)
                if tx:
                    self._last_output.append(tx)
            # 插件常常自己把消息发出去、不 return 文字，所以"没返回值"不等于失败
            ok = True
            if not self._last_output:
                self._last_error = ""
            _log("已执行 %s（%s）%s" % (
                text[:40], "有返回文字" if self._last_output else "插件直接发了消息",
                ("：" + self._last_output[0][:60]) if self._last_output else ""))
            return ok
        except Exception as e:
            self._last_error = repr(e)
            _log("执行 %s 失败: %r" % (text[:30], e))
            return False
        finally:
            try:
                event.message_str = old
            except Exception:
                pass

    # ---------- 按能力表选命令（执行层直接消费画像） ----------
    @staticmethod
    def _pattern_label(pat):
        """从正则里抠出人看的名字：^#?(ncm|NCM)\\s*签到$ → 签到"""
        zh = re.findall(r"[\u4e00-\u9fa5]+", str(pat or ""))
        return zh[0] if zh else ""

    def _cap_relevance(self, cap, text):
        """这条能力和这句话有多相关：画像是主，命令/标签是辅"""
        prof = self._profile(cap.get("plugin")) or {}
        blob = " ".join([
            cap.get("name") or "", cap.get("desc") or "",
            str(prof.get("what") or ""), " ".join(prof.get("when") or []),
            " ".join(str(x) for x in (prof.get("examples") or [])),
            " ".join(cap.get("tags") or []),
        ])
        g = self._grams(self._expand(text or ""))
        return len(g & self._grams(blob))

    def _cap_pool(self, text=""):
        """先按相关度选插件，再展开它的命令；不按顺序截断"""
        caps = list(self._scan())
        g = self._grams(self._expand(text or "")) if text else set()
        if g:
            # 和这句话相关的插件排前面；完全不沾边的也留着往后排，但不会挤掉相关的
            caps.sort(key=lambda c: -self._cap_relevance(c, text))
        pool = []
        for cap in caps:
            prof = self._profile(cap.get("plugin")) or {}
            details = [d for d in (prof.get("commands") or []) if d.get("cmd")]
            for c in (cap.get("cmds") or []):
                if c.get("kind") == "tool":
                    continue
                full = c.get("full") or c.get("cmd") or ""
                label = self._pattern_label(c.get("pattern"))
                d = {}
                for cand in (full, c.get("cmd"), label):
                    if not cand:
                        continue
                    for dd in details:
                        dc = str(dd.get("cmd") or "")
                        if dc and (cand == dc or cand in dc or dc in cand):
                            d = dd
                            break
                    if d:
                        break
                pool.append({
                    "cap": cap, "cmd": c.get("cmd"), "full": full,
                    "doc": c.get("doc") or "", "label": label,
                    "what": d.get("what") or "", "when": d.get("when") or "",
                    "args": d.get("args") or "", "prep": d.get("prep") or "",
                    "template": d.get("template") or "",
                    "examples": prof.get("examples") or [],
                })
        return pool

    def _pool_ranked(self, text, limit=None):
        """给候选排个座次：有模板的、名字/例句对得上的优先"""
        pool = self._cap_pool(text)
        if not text:
            return pool if limit is None else pool[:limit]
        g = self._grams(self._expand(text))
        scored = []
        for e in pool:
            blob = " ".join([e["label"] or "", e["cmd"] or "", e["doc"] or "",
                             e["what"] or "", " ".join(str(x) for x in e["examples"])])
            s = len(g & self._grams(blob))
            if e.get("template"):
                s += 1
            scored.append((s, e))
        scored.sort(key=lambda x: -x[0])
        out = [e for s, e in scored if s > 0]
        return out if limit is None else out[:limit]

    def _extract_arg(self, text, entry):
        t = text or ""
        for w in [entry.get("full"), entry.get("cmd")] + list(self.TRIGGER_WORDS):
            if w:
                t = t.replace(w, " ")
        t = re.sub(r"^(一下|一句|一首|来一首|来首|来|个|请|帮我|给我|麻烦)+", " ", t)
        return _clean_arg(t, limit=30)

    # ---------- 找"对象"：两句话的最长公共片段，不靠任何词表 ----------
    @staticmethod
    def _lcs(a, b, minlen=2):
        a, b = str(a or ""), str(b or "")
        if len(a) < minlen or len(b) < minlen:
            return ""
        best = ""
        for i in range(len(a)):
            for j in range(len(b)):
                k = 0
                while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                    k += 1
                if k >= minlen and k > len(best):
                    best = a[i:i + k]
        return best

    def _cmd_backed_by_text(self, text, cmd):
        """模型/通用层选的命令，得在话里有依据：名字出现，或和卡片例句有明显重合"""
        t = text or ""
        for cap in self._scan():
            for c in (cap.get("cmds") or []):
                if c.get("cmd") == cmd or (c.get("full") or "") == cmd:
                    if cmd and cmd in t:
                        return True
                    prof = self._profile(cap.get("plugin")) or {}
                    for ex in (prof.get("examples") or [])[:8]:
                        if len(self._lcs(t, str(ex))) >= 2:
                            return True
                    return False
        return True          # 查不到就放行

    def _cap_dispatch(self, text):
        """用能力表选命令：靠卡片里的例句找对象，靠模板拼调用格式"""
        t = (text or "").strip()
        if not t or len(t) > 40:
            return None
        g = self._grams(self._expand(t))
        if not g:
            return None
        best = None
        for e in self._pool_ranked(t, limit=80):
            core = " ".join([e["cmd"] or "", e["full"] or "", e["doc"] or "",
                             e["what"] or "", e["when"] or ""])
            score = len(g & self._grams(core))
            if e["cmd"] and e["cmd"] in t:
                score += 4
            if e["full"] and e["full"] in t:
                score += 4
            # 和卡片例句比：最长公共片段就是"对象"（唱首晴天 ↔ #ncm点歌 晴天 → 晴天）
            obj = ""
            for ex in (e["examples"] or [])[:8]:
                common = self._lcs(t, str(ex))
                if len(common) > len(obj):
                    obj = common
            run = ""
            if len(obj) >= 2:
                score += 3 + min(len(obj), 3)
                tpl = e.get("template") or ""
                arg = obj
                lb = e.get("label") or ""
                if lb and arg.startswith(lb) and len(arg) > len(lb):
                    arg = arg[len(lb):].strip()      # "说 你好" 别变成 "说 说 你好"
                if tpl and "{arg}" in tpl:
                    run = tpl.replace("{arg}", arg)
            if score < 3:
                continue
            # 只有话里真的出现了这条命令的名字/人话名，才敢自动执行；
            # 而且必须是"当命令用"（在句首），不能只是句子里带这个字
            named = bool((e.get("label") and t.startswith(e["label"])) or
                         (e["cmd"] and t.startswith(e["cmd"])) or
                         (e["full"] and t.startswith(e["full"])))
            if not named:
                continue
            # 疑问句不自动执行（"你不说有个点歌插件吗"）
            if re.search(r"(吗|呢|么|是不是|有没有)\s*[？?]?$", t):
                continue
            # 认出了对象，却没有调用模板 → 不知道格式，不猜
            if obj and not run:
                continue
            e2 = dict(e)
            if run:
                e2["_run"] = run
            if not best or score > best[0]:
                best = (score, e2)
        if not best:
            return None
        score, e = best
        return e, e.get("_run") or "", score

    def _cmd_in_caps(self, name):
        cap, short = self._resolve_cap_cmd(name)
        return short or ""

    def _cap_line(self, c):
        prof = self._profile(c.get("plugin"))
        what = prof.get("what") or (c.get("desc") or "")[:60]
        cmds = c.get("cmds") or []
        details = [d for d in (prof.get("commands") or []) if d.get("what")]
        lines = ["- %s：%s" % (c["name"], what)]
        if details:
            # 有命令级说明：把容易混的几条分开写清楚
            for d in details[:4]:
                seg = "  · /%s：%s" % (d.get("cmd"), d.get("what"))
                if d.get("args"):
                    seg += "［参数：%s］" % d["args"]
                if d.get("prep"):
                    seg += "［先：%s］" % d["prep"]
                if d.get("when"):
                    seg += "（%s）" % d["when"]
                if d.get("avoid"):
                    seg += "［别在：%s］" % d["avoid"]
                # 这条以前跑得很慢，就标出来，别让她以为几秒就好
                cmd_key = self._norm_cmd(d.get("cmd") or "")
                avg, n, slow = self._timing(cmd_key)
                if slow and n < 2:
                    seg += "［慢：要跑几分钟］"
                elif avg >= 10:
                    seg += "［慢：约 %d 秒］" % int(avg)
                lines.append(seg)
        else:
            # 没有命令级说明：退回原来的写法
            use = ""
            if cmds:
                tools = [x for x in cmds if x.get("kind") == "tool"]
                normal = [x for x in cmds if x.get("kind") != "tool"]
                parts = []
                for x in normal[:3]:
                    parts.append("/" + (x.get("full") or x["cmd"]) + ("（%s）" % x["doc"] if x["doc"] else ""))
                if tools:
                    parts.append("可直接调用工具：" + "、".join(t["cmd"] for t in tools[:3]))
                use = "｜" + "；".join(parts) if parts else ""
            if use:
                lines[0] += use
        when = prof.get("when") or []
        if when:
            lines.append("  什么时候用：%s" % "；".join(when[:3]))
        return "\n".join(lines)

    def _apply_block(self, caps, semantic=False):
        lines = ["【你确实能做到的事（本机插件，不是编的）】"]
        for c in caps:
            lines.append(self._cap_line(c))
        voices = self._voices()
        if voices:
            lines.append("- 你的声线：%s" % "、".join(voices))
        if semantic:
            lines.append("（他这句话已经踩到你能做的事上了：有工具就直接调工具；"
                         "没有工具就用 run_capability 去执行对应命令"
                         "（command=命令名不带斜杠，args=参数，target=群号时在该群里执行）；"
                         "别让他自己打，也别说“你去发 /xxx”。）")
        else:
            lines.append("（被问到能力时照实说，不要否认。）")
        return "\n".join(lines)

    def _voices(self):
        """只看配置里的目录；不猜路径，别人拿去装了也不会读到本机的文件夹"""
        try:
            d = str(self._cfg("voices_dir", "") or "").strip()
            if not d or not os.path.isdir(d):
                return []
            out = []
            for f in os.listdir(d):
                if f.endswith(".pth"):
                    out.append(os.path.splitext(f)[0])
            return sorted(out)
        except Exception:
            return []

    # ---------- 思考流整合 ----------
    def _note_thought(self, text):
        if not self._cfg("merge_thought", True):
            return
        try:
            import datetime as _dt
            tdir = str(self._cfg("thought_dir", "") or "").strip() or os.path.join(CACHE_DIR, "thoughts")
            os.makedirs(tdir, exist_ok=True)
            p = os.path.join(tdir, _dt.datetime.now().strftime("%Y-%m-%d.log"))
            with io.open(p, "a", encoding="utf-8") as f:
                f.write("[%s] 能力 | 语义命中\n%s\n%s\n" % (
                    _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text, "-" * 40))
        except Exception:
            pass

    # ---------- 语义直连：把话改写成命令，让别的插件真正执行 ----------
    DISPATCH_RULES = [
        # 指定用哪个翻唱后端：用rvc唱海底 / svc 海底
        (r"^\s*(?:用)?\s*(rvc|svc|svcvc)\s*(?:唱|翻唱|点歌)?\s*[《「\"]?([^》」\"？?，,。]{1,24})[》」\"]?\s*$",
         "/{1} {2}", "指定翻唱后端"),
        # (正则, 命令模板, 说明)
        (r"^\s*(?:给我)?(?:唱|来一首|放一首|点歌|点一首|来首|放首)\s*(?:一首|首|一首歌|个)?\s*"
         r"[《「\"]?([^》」\"？?，,。]{1,24})[》」\"]?\s*$",
         "/点歌 {1}", "点歌"),
        (r"^\s*(?:把)?\s*([^，,。]{1,24}?)\s*(?:的)?原曲(?:发我|给我|发一下)?\s*$",
         "/原曲 {1}", "原曲"),
        (r"^\s*([^，,。？?]{1,16})\s*(?:是)?什么梗\s*[？?]?\s*$",
         "/梗 {1}", "查梗"),
        (r"^\s*(?:你)?(?:还有)?(?:多少|几)(?:钱|块)(?:钱)?\s*$",
         "/余额", "余额"),
        (r"^\s*(?:看看|翻翻|查)(?:一下)?(?:刚才的)?(?:聊天)?记录\s*$",
         "/记录 20", "看记录"),
        # 要图：发个初音未来的图 / 来两张猫图
        (r"^\s*(?:(?:发|来|给|找|搜|弄|要|整)(?:个|张)?)+\s*(?:我)?\s*"
         r"(?:[0-9一二两三四五六七八九十]+(?=个|张|点|些))?\s*(?:个|张|点|些)?\s*"
         r"([^，,。？?！!]{1,20}?)\s*的?\s*(?:图|图片|照片|壁纸|插画)\s*$",
         "/p {1}", "搜图"),
    ]

    def _dispatch(self, text):
        """把一句自然话变成命令；返回 (命令, 说明) 或 (None, None)"""
        t = (text or "").strip()
        if not t or t.startswith("/"):
            return None, None
        rules = list(self.DISPATCH_RULES)
        # 用户自定义规则："关键词=>命令模板"
        extra = str(self._cfg("extra_rules", "") or "").strip()
        for line in extra.splitlines():
            line = line.strip()
            if "=>" in line:
                kw, tpl = line.split("=>", 1)
                kw, tpl = kw.strip(), tpl.strip()
                if kw and tpl:
                    rules.append((re.escape(kw) + r"\s*(.*)$", tpl + " {1}", "自定义"))
        for pat, tpl, label in rules:
            m = re.match(pat, t, re.S)
            if not m:
                continue
            cmd = tpl
            if "{2}" in tpl:
                g1 = (m.group(1) or "").strip() if m.lastindex and m.lastindex >= 1 else ""
                g2 = (m.group(2) or "").strip() if m.lastindex and m.lastindex >= 2 else ""
                g2 = g2.strip("《》「」\"' ")
                if not g1 or not g2:
                    continue
                cmd = tpl.replace("{1}", g1).replace("{2}", g2).strip()
                return cmd, label
            if "{1}" in tpl:
                arg = (m.group(1) or "").strip() if m.groups() else ""
                arg = arg.strip("《》「」\"' ")
                if not arg or arg in ("这个", "那个", "啥", "什么", "这", "那", "它", "他"):
                    continue
                if label == "搜图":
                    # 去掉"我/找/给/几/个/张"这类残留
                    for _ in range(3):
                        new_arg = re.sub(r"^(?:我|你|他|她|找|给|发|来|搜|弄|要|整)", "", arg)
                        new_arg = re.sub(r"^(?:几|一|两|三|四|五|六|七|八|九|十|\d+)?(?:个|张|点|些)", "", new_arg)
                        if new_arg == arg:
                            break
                        arg = new_arg.strip()
                    if not arg:
                        continue
                cmd = tpl.replace("{1}", arg).strip()
            return cmd, label
        return None, None

    # 通用直连：从任意插件的命令 + 说明里推规则（不写死）
    TRIGGER_WORDS = ("帮我", "给我", "请", "麻烦", "用语音", "语音", "念一下", "念一段", "念",
                     "读一下", "读一段", "读", "合成", "说一句", "说一段", "说", "一下", "一句",
                     "一句吧", "发语音", "配音", "转成语音")

    # 近义词扩展：让"念/读"也能对上"语音"类插件
    SYNONYMS = {
        "念": "语音", "朗读": "语音", "读": "语音", "配音": "语音", "发声": "语音", "声音": "语音",
        "画一张": "画图", "画画": "画图", "配图": "画图",
        "搜一下": "搜索", "查一下": "搜索",
        "唱一首": "点歌", "唱歌": "点歌",
    }

    def _expand(self, text):
        t = text or ""
        extra = []
        for k, v in self.SYNONYMS.items():
            if k in t and v not in t:
                extra.append(v)
        return (t + " " + " ".join(extra)).strip()

    # 一件能力"像不像取物/搜索"，按它自己的描述判断
    SEARCHISH = ("图", "画", "插画", "壁纸", "梗", "歌", "视频", "表情", "随机", "获取")

    def _cap_blob(self, cap):
        blob = (cap.get("desc") or "") + (cap.get("name") or "")
        for c in cap.get("cmds") or []:
            blob += (c.get("cmd") or "") + (c.get("doc") or "")
        return blob

    def _is_searchish(self, cap):
        return any(k in self._cap_blob(cap) for k in self.SEARCHISH)

    def _strip_arg(self, text, cap):
        arg = text or ""
        for c in cap.get("cmds") or []:
            if c.get("cmd"):
                arg = arg.replace(c["cmd"], " ")
        for w in ("帮我", "给我", "麻烦", "请", "一下", "一个", "一张", "几张", "个", "张",
                  "点", "些", "全年龄的", "全年龄", "的", "图片", "照片", "壁纸", "图"):
            arg = arg.replace(w, " ")
        arg = re.sub(r"^\s*(?:发|来|给|找|搜|弄|整|要|做|放|点|查|看)+", " ", arg)
        arg = re.sub(r"[，,。！!？?《》「」\"'~]+", " ", arg).strip()
        return re.sub(r"\s+", " ", arg).strip()

    # ---------- 让模型自己挑指令（兜底，最像"理解"的一层） ----------
    async def _recent_context(self, event, n=6):
        """最近几轮对话；拿不到就返回空串"""
        try:
            cm = getattr(self.context, "conversation_manager", None)
            if not cm or event is None:
                return ""
            umo = getattr(event, "unified_msg_origin", "")
            if not umo:
                return ""
            cid = await asyncio.wait_for(cm.get_curr_conversation_id(umo), timeout=5)
            if not cid:
                return ""
            conv = await asyncio.wait_for(cm.get_conversation(umo, cid), timeout=5)
            hist = getattr(conv, "history", None) if conv else None
            if not hist:
                return ""
            rows = json.loads(hist) if isinstance(hist, str) else list(hist)
            out = []
            for m in rows[-n:]:
                role = "你" if (m.get("role") == "assistant") else "对方"
                c = m.get("content")
                if isinstance(c, list):
                    c = " ".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
                c = re.sub(r"\s+", " ", str(c or "")).strip()
                if c:
                    out.append("%s：%s" % (role, c[:120]))
            return "\n".join(out)
        except Exception as e:
            _log("读上下文失败: %r" % e)
            return ""

    async def _llm_route(self, text, event=None):
        """把这句话和候选指令一起给模型，让它输出 {"cmd":..., "arg":...}"""
        if not self._cfg("llm_route", True):
            return None
        t = (text or "").strip()
        if not t or len(t) > 40:
            return None
        ctx = await self._recent_context(event)
        # 有上下文时，短句/没动词的说法也可能是"接着让机器人做事"，不拦
        if not ctx and not re.match(
                r"^\s*(?:发|来|给|找|搜|唱|点|放|换|查|看|要|弄|帮|用|把|让|试)", t):
            return None
        try:
            provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if not provider:
            return None

        cands = []
        for e in self._pool_ranked(t, limit=40):
            cands.append({
                "key": e["cmd"], "label": e["label"] or e["cmd"],
                "doc": (e["what"] or e["doc"] or "")[:24],
                "template": e["template"], "kind": "regex" if e["label"] and not e["template"] else "cmd",
            })
        if not cands:
            return None
        listing = "\n".join(
            "- %s：%s%s" % (c["label"], c["doc"] or "（无说明）",
                            ("（模板 %s）" % c["template"]) if c["template"] else "")
            for c in cands)
        prompt = (
            "你要判断用户这句话是不是在让机器人做一件本机能做的事，并选能力、填参数。\n"
            + (("最近的对话（供你理解上下文，别照抄）：\n" + ctx + "\n\n") if ctx else "")
            + "可选能力（名字：说明（模板））：\n" + listing + "\n\n"
            "注意：如果这句话是在接着上文让机器人做事（哪怕只有一两个词），也要选出来；"
            "如果只是聊天、回答、感慨，就输出 {\"name\":null}。\n"
            "如果对方让你自己决定（挑一个、随便来个、你看着办），参数由你自己定，别留空、别反问。\n"
            "只输出 JSON：{\"name\":\"能力名字\",\"arg\":\"参数\"}。不要解释。\n"
            "用户刚说：" + t
        )
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, contexts=[], system_prompt=None), timeout=20)
            out = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            _log("模型选指令失败: %r" % e)
            return None
        i, j = out.find("{"), out.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            data = json.loads(out[i:j + 1])
        except Exception:
            return None
        name = str(data.get("name") or "").strip().lstrip("/")
        arg = str(data.get("arg") or "").strip()
        if not name:
            _log("模型层：候选 %d 条，它说都不合适" % len(cands))
            return None
        pick = None
        for c in cands:
            if name in (c["label"], c["key"]):
                pick = c
                break
        if pick is None:
            _log("模型层：它选了「%s」，不在候选里" % name[:20])
            return None
        _log("模型层：选了 %s / %s" % (name[:20], arg[:20]))
        if pick["template"] and "{arg}" in pick["template"]:
            val = _clean_arg(arg, limit=30)
            if not val:
                # 对方让自己挑 → 她得自己定一个，别留空、别反问
                val = await self._pick_arg_self(pick, prompt_t=t, ctx=ctx)
                if not val:
                    _log("模型层：%s 需要参数，自己也没定出来，放弃" % name[:20])
                    return None
                _log("模型层：参数自己定的 → %s" % val[:20])
            return pick["template"].replace("{arg}", val)
        # 没有模板就不知道正确写法，宁可放弃，也不要拼出错误的 /点歌
        if pick.get("kind") == "regex" or not pick["key"]:
            return None
        return ("/%s %s" % (pick["key"], _clean_arg(arg, limit=30))).strip()

    async def _pick_arg_self(self, pick, prompt_t="", ctx=""):
        """参数空着的时候，让她结合上下文自己挑一个"""
        try:
            provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if not provider:
            return ""
        p = (
            "对方让你自己决定，但你还差一个参数没定。\n"
            + (("最近的对话：\n" + ctx + "\n\n") if ctx else "")
            + "你要执行的命令是：%s（%s）\n"
            "对方刚说：%s\n\n"
            "请直接给出一个你认为合适的参数值（一个名字/关键词，别带引号、别解释、别反问）；"
            "真的没法自己定就输出 NONE。"
            % (pick.get("label") or pick.get("key"), pick.get("doc") or "", prompt_t)
        )
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=p, contexts=[], system_prompt=None), timeout=15)
            v = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            _log("自己定参数失败: %r" % e)
            return ""
        v = v.strip("《》「」\"'“” 。，,")
        if not v or v.upper() == "NONE":
            return ""
        return _clean_arg(v, limit=30)

    def _hit_to_cmd(self, text, caps):
        """命中的能力里有"搜索/取物"类指令 → 直接把话变成那个指令"""
        t = (text or "").strip()
        if not t:
            return None
        # 必须是"要图"的句式：动词 + 对象 + 图/插画/壁纸，光有"图"字不算
        if not re.search(r"(?:来|发|给|找|搜|弄|整|要|画|抽|换)(?:个|一张|张|两张|几张|点)?"
                         r"[^，,。？?！!]{0,16}(?:图|图片|插画|壁纸)", t):
            return None
        for cap in caps[:3]:
            if not self._is_searchish(cap):
                continue
            cmds = [c for c in (cap.get("cmds") or []) if c.get("kind") not in ("tool", "regex")]
            if not cmds:
                continue
            # 只认名字/说明明确就是"搜索/取图"的那条，不再拿第一条命令凑数
            target = None
            for c in cmds:
                name, doc = c.get("cmd") or "", c.get("doc") or ""
                if name in ("p", "搜", "查", "找", "search", "来图", "图") or any(
                        k in doc for k in ("搜索", "查找", "搜图", "获取")):
                    target = c
                    break
            if target is None:
                continue
            arg = _clean_arg(self._strip_arg(t, cap), limit=30)
            if not arg:
                continue
            return "/%s %s" % (target["cmd"], arg)
        return None

    # ---------- 工具直连：没有命令、只有 llm_tool 的插件也让她用得起来 ----------
    TOOL_STOPWORDS = ("帮我", "给我", "麻烦", "请", "替我", "用你的", "用", "发一个", "发个",
                      "来一个", "来个", "一个", "一张", "一张图", "表情包", "表情", "图片", "图",
                      "一下", "一段", "一句", "一声", "句", "语音", "声音", "念一下", "念",
                      "读一下", "读", "说一下", "说一句", "说一声", "说", "你", "她")

    def _tool_arg(self, text, params):
        """只敢给单参数工具猜参数；多参数的交给模型，别瞎填"""
        if len(params) != 1:
            return None
        arg = text or ""
        for w in self.TOOL_STOPWORDS:
            arg = arg.replace(w, " ")
        arg = _clean_arg(arg, limit=20)
        arg = re.sub(r"^[的地得]+", "", arg).strip()
        # 整段就是个语气词（"呢""啊吧"）→ 当没给参数
        if arg and all(ch in "呢吧啊呀哦嘛啦吗" for ch in arg):
            return ""
        # 只去一个纯语气词，且参数够长才去；"了"不去，它可能是实义
        if len(arg) >= 3:
            arg = re.sub(r"[呢吧啊呀哦嘛啦吗]$", "", arg).strip()
        arg = re.sub(r"[的地得]+$", "", arg).strip()
        return arg

    def _tool_needs_arg(self, handler):
        """看函数签名：除 event 外还有没有必填参数"""
        import inspect
        try:
            sig = inspect.signature(handler)
        except Exception:
            return False
        for i, p in enumerate(sig.parameters.values()):
            if i == 0:
                continue        # 第一个参数是 event
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY) \
                    and p.default is p.empty:
                return True
        return False

    TOOL_INTENT_PREFIX = ("帮", "给", "来", "发", "用", "念", "读", "说", "麻烦", "请",
                          "替我", "能", "唱", "放", "查", "搜")
    TOOL_INTENT_WORDS = ("帮我", "给我", "麻烦", "请", "替我", "能不能", "可以帮", "帮忙")

    def _tool_intent_ok(self, text):
        """只有像"在吩咐一件事"的句子才允许走工具直连。
        "你又读到啥了"这种叙述句，虽然含"读"，不该被当成点单。"""
        t = (text or "").strip()
        if not t:
            return False
        if any(t.startswith(p) for p in self.TOOL_INTENT_PREFIX):
            return True
        return any(k in t for k in self.TOOL_INTENT_WORDS)

    def _tool_pick(self, text):
        """从命中的能力里挑一个真能调用的工具，返回 (工具名, 参数)"""
        if not self._tool_intent_ok(text):
            return None, None
        info = self._runtime_tools()
        if not info:
            return None, None
        blocked = set((_read_json(TOOL_STATE, {}) or {}).get("blocked") or [])
        g = self._grams(self._expand(text))
        want_search = any(k in text for k in ("搜", "找", "查", "检索"))
        want_send = any(k in text for k in ("发", "来", "给", "要", "整", "抽", "换", "来点", "来个"))
        cands = []
        for cap in self.match(self._expand(text))[:3]:
            for c in (cap.get("cmds") or []):
                if c.get("kind") != "tool":
                    continue
                t = info.get(c["cmd"])
                if not t or not t.get("handler") or not t.get("active", True):
                    continue
                if c["cmd"] in blocked:
                    continue
                arg = self._tool_arg(text, t.get("params") or [])
                if arg is None:
                    continue
                # 这个工具要参数、但话里抽不出来，就别硬调（schema 没写 required 的也查签名）
                if not arg and (t.get("required") or self._tool_needs_arg(t.get("handler"))):
                    continue
                blob = (c["cmd"] or "") + (c.get("doc") or "") + (t.get("desc") or "")
                low = blob.lower()
                name_low = (c["cmd"] or "").lower()
                name_is_search = any(k in name_low for k in ("search", "搜", "查", "find"))
                is_search = name_is_search or any(k in low for k in ("搜", "查", "find", "search"))
                is_send = any(k in low for k in ("发", "send", "给", "输出"))
                # 用户要"发"时，不要拿搜索工具顶（搜索类工具名里一般带 search/搜/查）
                if want_send and not want_search and name_is_search:
                    continue
                score = len(g & self._grams(blob))
                if want_search and is_search:
                    score += 3
                if want_send and is_send:
                    score += 3
                cands.append((score, c["cmd"], arg))
        if not cands:
            return None, None
        cands.sort(key=lambda x: -x[0])
        return cands[0][1], cands[0][2]

    def _tool_block(self, name):
        """记下这个工具不能脱离请求直接调，以后不再抢消息，交给模型自己调"""
        try:
            st = _read_json(TOOL_STATE, {}) or {}
            blocked = set(st.get("blocked") or [])
            if name in blocked:
                return
            blocked.add(name)
            st["blocked"] = sorted(blocked)
            _write_json(TOOL_STATE, st)
        except Exception:
            pass

    def _tool_unblock(self):
        _write_json(TOOL_STATE, {"blocked": []})

    def _auto_dispatch(self, text):
        """完全通用：不看插件名，靠"这句话和它的说明有多重合"来选"""
        t = (text or "").strip()
        if not t or len(t) > 40:
            return None
        g = self._grams(self._expand(t))
        if not g:
            return None
        caps = self.match(self._expand(t))
        best = None
        for cap in caps[:4]:
            if not self._is_searchish(cap):
                continue
            cmds = [c for c in (cap.get("cmds") or []) if c.get("kind") not in ("tool", "regex")]
            if not cmds:
                continue
            # 这句话得是"在点单"（发/来/搜/唱/查…开头），单个词不算
            if not any(t.startswith(w) for w in self.DISPATCH_TRIGGERS):
                continue
            cg = self._grams(self._cap_blob(cap))
            overlap = len(g & cg)
            if overlap < 2:
                continue
            # 命令名直接出现，优先用它；否则用第一条能对上的命令
            target, boost = cmds[0], 0.0
            for c in cmds:
                name, doc = c.get("cmd") or "", c.get("doc") or ""
                if name and name in t:
                    target, boost = c, 3.0
                    break
                if len(g & self._grams(name + doc)) >= 2:
                    target, boost = c, 0.0
                    break
            arg = _clean_arg(self._strip_arg(t, cap), limit=30)
            if not arg:
                continue
            score = overlap + boost
            if not best or score > best[0]:
                best = (score, "/%s %s" % (target["cmd"], arg), cap["name"])
        if best and best[0] >= 2.5:
            return best[1]
        return None

    def _generic_dispatch(self, text):
        """对任意插件：靠"命令名 + docstring"的语义重合来判断"""
        orig = text or ""
        text = self._expand(text)
        caps = self.match(text)
        if not caps:
            return None
        g = self._grams(text)
        best = None
        for cap in caps[:2]:
            for c in (cap.get("cmds") or []):
                # 工具/正则路由不是命令，别拼成 "/名字 参数" 去执行
                if c.get("kind") in ("tool", "regex"):
                    continue
                doc = c.get("doc") or ""
                if not doc:
                    continue
                cg = self._grams(c["cmd"] + doc)
                score = len(g & cg)
                has_trigger = any(w in text for w in self.TRIGGER_WORDS)
                if score < 2 and not (score >= 1 and has_trigger):
                    continue
                need_arg = bool(re.search(r"[<＜（(【].{1,12}[>＞）)】]|\s", doc))
                # 命令名直接出现在话里，也认
                if c["cmd"] in text and len(text) <= 20:
                    arg = orig
                    for w in (c["cmd"],) + self.TRIGGER_WORDS:
                        arg = arg.replace(w, " ")
                    arg = re.sub(r"[，,。！!？?：《》「」\"'~]+", " ", arg).strip()
                    arg = re.sub(r"\s+", " ", arg).strip()
                    cmd = ("/%s %s" % (c["cmd"], arg)).strip() if arg else "/" + c["cmd"]
                    cand = (3.0, cmd, cap["name"])
                    if not best or cand[0] > best[0]:
                        best = cand
                    continue
                if not need_arg:
                    # 不需要参数的命令：重合度必须够，否则一个插件几十条命令总能蒙中一条
                    if score < 3 and c["cmd"] not in text:
                        continue
                    cand = (score + 0.5, "/" + c["cmd"], cap["name"])
                    if not best or cand[0] > best[0]:
                        best = cand
                    continue
                arg = orig
                arg = re.sub(r"(用|拿)?(她|他|你|我)?的?(声音|嗓子|嗓音)", " ", arg)
                arg = re.sub(r"(这段|那段|下面|这句|这些|上面)", " ", arg)
                for w in self.TRIGGER_WORDS:
                    arg = arg.replace(w, " ")
                arg = re.sub(r"[，,。！!？?《》「」\"'~]+", " ", arg).strip()
                arg = re.sub(r"^\s*(一下|一段|一句|话|段|句|：|:)+\s*", "", arg)
                arg = _clean_arg(arg, limit=60)
                if not arg:
                    continue
                # 带参数的命令要求更严：分数够、或命令名就在话里；
                # 否则容易把整句话当参数塞给 /陪伴 这类宽命令
                if score < 3 and c["cmd"] not in text:
                    continue
                cand = (score, "/%s %s" % (c["cmd"], arg), cap["name"])
                if not best or cand[0] > best[0]:
                    best = cand
        if best:
            return best[1]
        return None

    # ---------- 直接调用注册表里的指令处理函数 ----------
    def _norm_cmd(self, name):
        """命令名归一：去掉开头的 # / 空格，只留本体"""
        return re.sub(r"^[#/\s]+", "", str(name or "").strip())

    def _resolve_cap_cmd(self, name):
        """把她说出来的命令名对到能力表里的那条：返回 (cap, 短名)"""
        want = self._norm_cmd(name)
        if not want:
            return None, ""
        alts = []
        for c in self._scan():
            for x in (c.get("cmds") or []):
                if x.get("kind") == "tool":
                    continue
                short = x.get("cmd") or ""
                full = x.get("full") or short
                alts.append(full)
                cands = {self._norm_cmd(short), self._norm_cmd(full),
                         self._norm_cmd(full.replace(short, ""))}
                if want in cands:
                    return c, short
        for c in self._scan():
            for x in (c.get("cmds") or []):
                if x.get("kind") == "tool":
                    continue
                short = self._norm_cmd(x.get("cmd") or "")
                full = self._norm_cmd(x.get("full") or short)
                if want and (full.endswith(want) or want.endswith(short)):
                    return c, x.get("cmd") or ""
        self._cmd_alts = alts[:15]
        return None, ""

    # ---------- 抓"真正发出去的消息"：补上她看不到的结果 ----------
    SENT_FILE = os.path.join(CACHE_DIR, "sent.json")

    def _sent_all(self):
        try:
            d = _read_json(self.SENT_FILE, {}) or {}
        except Exception:
            d = {}
        return d if isinstance(d, dict) else {}

    def _sent_add(self, umo, text):
        if not umo or not text:
            return
        d = self._sent_all()
        rows = d.get(umo) or []
        rows.append({"ts": time.time(), "text": str(text)[:300]})
        d[umo] = rows[-8:]
        try:
            _write_json(self.SENT_FILE, d)
        except Exception:
            pass

    def _sent_recent(self, umo, n=2, age=600):
        rows = self._sent_all().get(umo) or []
        rows = [r for r in rows if time.time() - float(r.get("ts") or 0) <= age]
        return rows[-n:]

    def _sent_text(self, event):
        """把一条已发送的消息压成一句文本（媒体只留占位）"""
        try:
            res = event.get_result()
        except Exception:
            res = None
        if res is None:
            return ""
        parts = []
        for comp in (getattr(res, "chain", None) or []):
            t = getattr(comp, "text", None)
            if t:
                parts.append(str(t))
                continue
            nm = type(comp).__name__.lower()
            for key, mark in (("image", "[图片]"), ("record", "[语音]"), ("video", "[视频]"),
                              ("file", "[文件]"), ("face", "[表情]"), ("at", "[@")):
                if key in nm:
                    parts.append(mark)
                    break
        return " ".join(p for p in parts if p).strip()

    @filter.after_message_sent()
    async def on_after_sent(self, event: AstrMessageEvent):
        """每条消息发出去之后都会走这里：记下来，她下轮就能知道群里出现了什么"""
        try:
            txt = self._sent_text(event)
            if not txt:
                return
            umo = getattr(event, "unified_msg_origin", "") or ""
            # 只在"刚执行过命令"的窗口里记，免得把她自己平时的回复也当结果
            recent = [r for r in self._act_all()
                      if float(r.get("ts") or 0) > time.time() - 180
                      and (not r.get("target") or r.get("target") == umo)]
            if not recent:
                return
            self._sent_add(umo, txt)
            _log("抓到已发送内容（%d 字）：%s" % (len(txt), txt[:60]))
        except Exception as e:
            _log("抓取已发送消息失败: %r" % e)

    def _is_command_word(self, word):
        """这个词本身是不是本机的一条命令名（p / 签到 / ncm点歌 …）"""
        w = self._norm_cmd(word)
        if not w:
            return False
        try:
            for cap in self._scan():
                for c in (cap.get("cmds") or []):
                    if c.get("kind") == "tool":
                        continue
                    names = [c.get("cmd"), c.get("full"), self._pattern_label(c.get("pattern"))]
                    for name in names:
                        if name and self._norm_cmd(name) == w:
                            return True
        except Exception:
            return False
        return False

    def _find_handler(self, cmd):
        try:
            from astrbot.core.star.star_handler import (
                EventType, star_handlers_registry)
            handlers = star_handlers_registry.get_handlers_by_event_type(
                EventType.AdapterMessageEvent, only_activated=False)
        except Exception:
            return None
        for h in handlers:
            for f in (getattr(h, "event_filters", None) or []):
                names = []
                try:
                    names = [str(x) for x in (f.get_complete_command_names() or [])]
                except Exception:
                    names = []
                cn = getattr(f, "command_name", None)
                if cn:
                    names.append(str(cn))
                for a in (getattr(f, "alias", None) or []):
                    if a:
                        names.append(str(a))
                want = self._norm_cmd(cmd)
                if any(self._norm_cmd(n) == want for n in names):
                    return h
        return None

    def _full_name(self, cmd):
        """把命令短名换成带分组前缀的完整写法（表情管理 添加表情）"""
        for c in self._caps or []:
            for x in (c.get("cmds") or []):
                if x.get("cmd") == cmd and x.get("full"):
                    return str(x["full"])
        return cmd

    async def _diagnose(self, what, why):
        """让模型看一眼日志，说一句可能的原因"""
        try:
            provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if not provider:
            return ""
        logs = self._log_lines(60)
        bad = [x for x in logs
               if re.search(r"失败|错误|异常|超时|拒绝|Traceback|Error|WARN|拒绝访问", x)][-8:]
        tail = "\n".join(bad) or "\n".join(logs[-5:])
        prompt = (
            "你刚试着做一件事但失败了。请判断可能的原因，并给一句可执行的建议。\n"
            "做了什么：%s\n失败信息：%s\n最近相关日志：\n%s\n\n"
            "要求：两三句话，说人话；只依据上面的信息，不要编造；"
            "不确定就说不确定，并指出该检查什么（配置、依赖、参数、对方插件是否改过）。"
            % (what, why or "没有更多信息", tail)
        )
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, contexts=[], system_prompt=None), timeout=20)
            text = (getattr(resp, "completion_text", "") or "").strip()
            return text[:400]
        except Exception as e:
            _log("诊断调用失败: %r" % e)
            return ""

    async def _report_failure(self, event, what, why):
        """失败时回一句：能诊断就诊断，诊断不了就说没弄成"""
        if not self._cfg("notify_failure", True):
            return
        now = time.time()
        if now - float(getattr(self, "_diag_ts", 0) or 0) < 15:
            return          # 同一个时间窗里别连环刷
        self._diag_ts = now
        text = ""
        try:
            if self._cfg("diagnose_failure", True):
                text = await self._diagnose(what, why)
        except Exception as e:
            _log("失败诊断出错: %r" % e)
        if not text:
            text = "刚试了一下，没弄成……%s" % (("（%s）" % str(why)[:80]) if why else "")
        try:
            from astrbot.core.message.message_event_result import MessageChain as _MCF
            await self.context.send_message(event.unified_msg_origin, _MCF().message(text))
        except Exception as e:
            _log("发送失败说明出错: %r" % e)

    async def _invoke_tool(self, event, name, arg):
        """直接调用插件注册的工具（工具通常会自己发消息）。返回 (是否成功, 原因)"""
        import inspect
        info = self._runtime_tools().get(name)
        if not info:
            _log("找不到工具：%s" % name)
            return False, "not_found"
        h = info.get("handler")
        if h is None:
            _log("工具 %s 没有可调用的 handler" % name)
            return False, "no_handler"
        kwargs = {}
        params = info.get("params") or []
        if params and arg:
            kwargs[params[0]] = arg
        try:
            ret = h(event, **kwargs)
            if inspect.isasyncgen(ret):
                last = None
                async for item in ret:
                    last = item
                ret = last
            elif inspect.isawaitable(ret):
                ret = await ret
        except Exception as e:
            _log("调用工具 %s 抛异常: %r" % (name, e))
            self._note_thought("调用工具：%s(%s) → 异常 %r" % (name, arg, e))
            return False, "exception: %r" % (e,)
        ok, reason = True, ""
        if isinstance(ret, str):
            body = ret.strip()
            if body.startswith("{"):
                try:
                    data = json.loads(body)
                    ok = bool(data.get("ok", True))
                    # 各家插件用的字段名不一样，能拿到的都记下来
                    reason = str(data.get("reason") or data.get("message")
                                 or data.get("error") or data.get("instruction") or "")
                except Exception:
                    reason = body[:60]
            elif body:
                reason = body[:60]
        _log("调用工具 %s(%s) → %s%s" % (
            name, arg, "成功" if ok else "失败",
            ("（%s）" % reason[:120]) if reason else ""))
        self._note_thought("调用工具：%s(%s) → %s%s" % (
            name, arg, "成功" if ok else "失败", ("：" + reason[:80]) if reason else ""))
        return ok, reason

    def _result_text(self, r):
        """把一次执行结果里的文字抠出来，给她自己看"""
        try:
            if isinstance(r, str):
                return r.strip()
            chain = getattr(r, "chain", None)
            if chain is None:
                return ""
            out = []
            for comp in chain:
                t = getattr(comp, "text", None)
                if t:
                    out.append(str(t))
            return " ".join(out).strip()
        except Exception:
            return ""

    def _arg_plans(self, fn, event, arg):
        """列出几种可能的调用顺序；按参数名猜的最准，放最前面"""
        import inspect
        plans = []
        try:
            sig = inspect.signature(fn)
            params = [p for p in sig.parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)]
            if params and params[0].name in ("self", "cls"):
                params = params[1:]
            seq = []
            for p in params:
                if p.kind == p.VAR_POSITIONAL:
                    if arg:
                        seq.append(arg)
                    continue
                nm = p.name.lower()
                if nm in ("event", "e", "ev", "msg_event", "message_event"):
                    seq.append(event)
                elif p.default is not p.empty:
                    break
                else:
                    seq.append(arg)
            if seq and any(x is event for x in seq):
                plans.append(tuple(seq))
        except Exception:
            pass
        plans.append((event, arg))
        if arg:
            plans.append((arg, event))     # 参数在前的写法
        plans.append((event,))
        out, seen = [], set()
        for pl in plans:
            key = tuple(id(x) for x in pl)
            if key in seen:
                continue
            seen.add(key)
            out.append(pl)
        return out

    @staticmethod
    def _shape_error(e):
        """是不是"调用形状不对"这类错（可以换种调法重试）"""
        msg = str(e)
        if isinstance(e, TypeError):
            return ("positional argument" in msg or "unexpected keyword" in msg
                    or "missing 1 required" in msg)
        if isinstance(e, (ValueError, AttributeError)):
            return ("stop_event" in msg or "'str' object" in msg or "unpack" in msg)
        return False

    async def _run_handler(self, fn, event, arg):
        """真正把 handler 跑完：协程/异步生成器都会 await，参数顺序不对就换一种"""
        import inspect
        plans = self._arg_plans(fn, event, arg)
        last = None
        for i, plan in enumerate(plans):
            try:
                ret = fn(*plan)
                if inspect.isasyncgen(ret):
                    out = []
                    async for r in ret:
                        if r is not None:
                            out.append(r)
                    return out
                if not inspect.isawaitable(ret):
                    return [ret] if ret is not None else []
                r = await ret
                return [r] if r is not None else []
            except Exception as e:
                if self._shape_error(e) and i + 1 < len(plans):
                    _log("调用参数对不上，换个顺序重试（第 %d 种）" % (i + 2))
                    last = e
                    continue
                raise
        if last is not None:
            raise last
        return []

    async def _invoke_command(self, event, cmd, arg):
        """把 /cmd arg 真正执行掉：直接调 handler，收集并发送结果"""
        import inspect
        h = self._find_handler(cmd)
        if h is None:
            self._last_error = "注册表里没有 /%s 这个指令（可能插件没加载或被停用）" % cmd
            _log("找不到指令处理函数：/%s" % cmd)
            return False
        old = event.message_str
        disp = self._full_name(cmd)
        event.message_str = ("/%s %s" % (disp, arg)).strip() if arg else "/" + disp
        try:
            event.message_obj.message_str = event.message_str
        except Exception:
            pass
        try:
            fn = h.handler
            results = await self._run_handler(fn, event, arg)
            self._last_output = []
            for r in results:
                await self._emit_result(event, r)
                txt = self._result_text(r)
                if txt:
                    self._last_output.append(txt)
            # 同上：插件自己发消息是常态，没返回值也算执行成功
            ok = True
            _log("已直接执行 /%s %s（%s）%s" % (
                cmd, arg, "有返回文字" if self._last_output else "插件直接发了消息",
                ("：" + self._last_output[0][:80]) if self._last_output else ""))
            return ok
        except Exception as e:
            self._last_error = repr(e)
            _log("直接执行 /%s 失败: %r" % (cmd, e))
            return False
        finally:
            try:
                event.message_str = old
            except Exception:
                pass

    async def _emit_result(self, event, r):
        try:
            from astrbot.core.message.message_event_result import MessageEventResult
            if isinstance(r, MessageEventResult):
                # 直接挂到 event 上，别 await send（在 process 阶段会卡住）
                if getattr(r, "chain", None):
                    from astrbot.core.message.message_event_result import MessageChain as _MC
                    await self.context.send_message(event.unified_msg_origin, r)
                else:
                    event.set_result(r)
                return
            if isinstance(r, str):
                from astrbot.core.message.message_event_result import MessageChain as _MC
                await self.context.send_message(event.unified_msg_origin, _MC().message(r))
                return
            if hasattr(r, "chain") and r.chain is not None:
                await self.context.send_message(event.unified_msg_origin, r)
        except Exception as e:
            _log("发送指令结果失败: %r" % e)

    # 出现这些标记就说明这句话带着引用/转发/多段内容，不该被猜成命令
    DISPATCH_BLOCK_MARKS = ("[引用", "引用了", "[图片]", "[表情]", "[语音]", "[视频]",
                            "主人转发了一段聊天记录", "嵌套转发", "合并转发", "┌─", "└─")
    # 以这些字开头才算"在点单"，长一点也允许；否则超过 40 字一律不猜
    DISPATCH_TRIGGERS = ("发", "来", "给", "找", "搜", "唱", "点", "放", "换", "查", "看", "要",
                         "弄", "帮", "用", "念", "读", "说", "画", "传话", "带话")

    def _dispatch_safe(self, text):
        """直连前的闸门：长句、多行、引用、转发一律不猜"""
        t = (text or "").strip()
        if not t or "\n" in t or "\r" in t:
            return False
        if any(k in t for k in self.DISPATCH_BLOCK_MARKS):
            return False
        limit = 80 if t.startswith(self.DISPATCH_TRIGGERS) else 40
        return len(t) <= limit

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def semantic_dispatch(self, event: AstrMessageEvent):
        """装好就生效：按语义把话转成别的插件的命令"""
        try:
            if not self._cfg("auto_dispatch", True):
                return
            if event.get_extra("sa_dispatched"):
                return
            text = (event.message_str or "").strip()
            if not text or text.startswith("/") or text.startswith("#"):
                # 用户自己在用命令：记一笔，让她下轮知道这件事发生过
                try:
                    body = text.lstrip("/#").strip()
                    if body:
                        w, _, rest = body.partition(" ")
                        rid = self._act_start(w, rest.strip(), event.unified_msg_origin)
                        self._act_end(rid, True, "主人自己发的命令")
                except Exception:
                    pass
                return
            # 命令前缀在这一步之前可能已经被框架剥掉了（"/p x" 到这儿是 "p x"），
            # 所以还要看首词是不是本机命令名；是的话就不许改写
            try:
                first = re.split(r"\s+", text)[0]
                if self._is_command_word(first):
                    if len(text) <= 20:
                        _log("决策：%s → 首词 %s 是命令，放行" % (text[:20], first))
                    try:
                        rest = text[len(first):].strip()
                        rid = self._act_start(first, rest, event.unified_msg_origin)
                        self._act_end(rid, True, "主人自己发的命令")
                    except Exception:
                        pass
                    return
            except Exception:
                pass
            try:
                if str(event.get_sender_id()) == str(event.get_self_id()):
                    return
            except Exception:
                pass
            # 闸门：长句、多行、引用/转发一律不猜
            if not self._dispatch_safe(text):
                if len(text) <= 14:
                    _log("决策：%s → 闸门拦下（长句/引用/多行）" % text[:20])
                return
            notes = []          # 每层的结论，最后一行日志说清楚卡在哪
            # 问"怎么用/说明书"的，交给说明书 handler，别当命令去执行
            if self._manual_query(text):
                return
            # 在说"别的会话"的事：交给模型自己判断（她已经能用 run_capability 执行命令），
            # 这里硬猜容易猜成 /陪伴 之类，把话整个当参数塞进去
            if any(w in text for w in self.CROSS_WORDS):
                return
            cmd, label = None, None
            # -1) 正则路由：用户原话直接命中某个插件的匹配式，最准，优先走
            regex_call = None
            if self._cfg("auto_dispatch", True):
                regex_call = self._regex_dispatch(text)
            notes.append("正则:%s" % ("命中" if regex_call else "无"))
            # 0) 再按能力表选命令（画像里的命令级理解在这里生效）
            run_text = ""      # 按画像模板拼好的调用文本（填完 {arg}）
            picked = None if regex_call else self._cap_dispatch(text)
            notes.append("能力表:%s" % ("命中" if picked else "无"))
            if picked:
                e, arg0, score = picked
                run_text = e.get("_run") or ""
                if e.get("kind") == "regex":
                    regex_call = (e["cap"], e)
                    label = "能力表:%s(%.1f)" % (e["cap"].get("name"), score)
                else:
                    cmd = ("/%s %s" % (e["cmd"], arg0)).strip() if arg0 else "/" + e["cmd"]
                    label = "能力表:%s(%.1f)" % (e["cap"].get("name"), score)
            if not cmd and self._cfg("auto_compat", True) and self._auto_rules():
                c2, l2 = self._dispatch_by_auto(text)
                if c2:
                    cmd, label = c2, l2
            if not cmd and self._cfg("auto_dispatch", True):
                g = self._auto_dispatch(text)
                if g:
                    cmd, label = g, "通用"
            if not cmd and self._cfg("auto_dispatch", True):
                caps = self.match(self._expand(text))
                if caps:
                    h = self._hit_to_cmd(text, caps)
                    if h:
                        cmd, label = h, "命中直达"
            # 没有命令、但有工具插件的，走工具直连
            tool_call = None
            if not cmd and not regex_call and self._cfg("tool_dispatch", True):
                tn, ta = self._tool_pick(text)
                if tn:
                    tool_call = (tn, ta)
            if not cmd and not regex_call and self._cfg("builtin_rules", False):
                c3, l3 = self._dispatch(text)
                if c3:
                    nm = c3.lstrip("/").split(" ", 1)[0]
                    if self._cmd_in_caps(nm):
                        cmd, label = c3, l3
                    else:
                        _log("内置规则 /%s 本机不存在，已跳过" % nm)
            if not cmd and not tool_call and not regex_call and self._cfg("generic_dispatch", True):
                g2 = self._generic_dispatch(text)
                if g2:
                    nm = g2.lstrip("/").split(" ", 1)[0]
                    if self._cmd_backed_by_text(text, nm):
                        cmd, label = g2, "通用"
                    else:
                        _log("通用兜底选了 /%s，动作对不上，丢弃" % nm)
            notes.append("通用:%s" % ("命中" if cmd else "无"))
            if not cmd and not tool_call and not regex_call:
                r = await self._llm_route(text, event)
                notes.append("模型:%s" % ("选了" if r else "没选"))
                if r:
                    if r.startswith("#"):
                        # 模型按模板拼出的原样调用（正则路由）
                        run_text = r
                        label = "模型模板"
                    else:
                        nm = r.lstrip("/").split(" ", 1)[0]
                        if self._cmd_backed_by_text(text, nm):
                            cmd, label = r, "模型选指令"
                        else:
                            _log("模型选了 /%s，在话里没依据，丢弃" % nm)
            if not cmd and not tool_call and not regex_call and not run_text:
                if len(text) <= 14:
                    _log("决策：%s → 没有可用的命令（%s）" % (text[:20], "｜".join(notes)))
                return
            if run_text and not regex_call:
                # ---- 按画像模板拼好的写法，直接执行 ----
                entry = (picked[0] if picked else
                         {"kind": "regex", "cmd": "", "pattern": ""})
                self._note_thought("语义直连：%s → %s（%s）" % (text[:40], run_text, label))
                _log("语义直连：%s → %s（%s）" % (text[:30], run_text, label))
                ok = await self._grace(self._invoke_text(event, entry, run_text), 8,
                                       label=(entry.get("cmd") or run_text[:20]), event=event,
                                       args=run_text, report=True)
                if ok is not False:
                    event.set_extra("sa_dispatched", True)
                    event.stop_event()
                return
            if regex_call:
                # ---- 正则路由：原话直接喂给它自己的匹配逻辑 ----
                cap_r, entry = regex_call
                self._note_thought("语义直连：%s → 正则路由 %s（%s）" % (
                    text[:40], entry.get("cmd"), cap_r.get("name")))
                _log("语义直连：%s → 正则路由 %s（%s）" % (
                    text[:30], entry.get("cmd"), cap_r.get("name")))
                ok = await self._grace(
                    self._invoke_regex(event, entry, entry.get("_run") or text), 8,
                    label=(entry.get("cmd") or "正则"), event=event, args=text,
                    report=True)
                if ok is not False:
                    event.set_extra("sa_dispatched", True)
                    event.stop_event()
                return
            if not cmd:
                # ---- 工具直连：先试一下，成了才抢这条消息 ----
                name, targ = tool_call
                if targ is None:
                    return
                self._note_thought("语义直连：%s → 工具 %s(%s)" % (text[:40], name, targ))
                _log("语义直连：%s → 工具 %s(%s)" % (text[:30], name, targ))
                ok, why = False, ""
                try:
                    ok, why = await asyncio.wait_for(
                        self._invoke_tool(event, name, targ), timeout=12)
                except asyncio.TimeoutError:
                    why = "timeout"
                except BaseException as e2:
                    why = repr(e2)
                if ok:
                    event.set_extra("sa_dispatched", True)
                    event.stop_event()
                    return
                # 失败就放开：让这条消息照常走模型，由插件自己的规矩调它的工具
                if why in ("tool_unavailable", "tool_disabled", "no_handler", "not_found"):
                    self._tool_block(name)
                    _log("工具 %s 依赖请求期准备（%s），以后不再直连，改由模型自己调" % (name, why))
                _log("工具 %s 直连没成（%s），这条消息放行给正常流程" % (name, why))
                return
            # 参数是"这个/那个/啥"这种指代就放弃
            arg = cmd.split(" ", 1)[1] if " " in cmd else ""
            if arg.strip(" 《》「」\"") in ("这个", "那个", "啥", "什么", "这", "那"):
                return
            # 记一笔，避免死循环
            event.set_extra("sa_dispatched", True)
            parts = cmd.lstrip("/").split(" ", 1)
            _c = parts[0]
            _a = _clean_arg(parts[1], limit=40) if len(parts) > 1 else ""
            if len(parts) > 1 and not _a:
                return
            cmd = ("/%s %s" % (_c, _a)).strip() if _a else "/" + _c
            self._note_thought("语义直连：%s → %s（%s）" % (text[:40], cmd, label))
            _log("语义直连：%s → %s（%s）" % (text[:30], cmd, label or "?"))
            # 后台跑，不阻塞管线：图片/翻唱这类本来就慢
            async def _runner(ev=event, c=_c, a=_a):
                ok = await self._grace(self._invoke_command(ev, c, a), 8,
                                       label=c, event=ev, args=a, report=True)
                if ok is False:
                    await self._report_failure(
                        ev, "执行指令 /%s %s" % (c, a or "（无参数）"), self._last_error)
            t = asyncio.get_event_loop().create_task(_runner())
            self._tasks = getattr(self, "_tasks", set())
            self._tasks.add(t)
            event.stop_event()
        except Exception as e:
            _log("语义直连失败: %r" % e)

    # ---------- 注入 ----------
    @filter.on_llm_request()
    async def inject_caps(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            mode = str(self._cfg("inject_mode", "semantic") or "semantic").lower()
            if mode == "off":
                return
            text = (event.message_str or "").strip()
            caps = self._scan()
            # 她做过什么：最近有动作（15 分钟内，或还在后台跑）就带上，免得她"忘了"
            try:
                rows = self._act_recent(3)
                if any(float(r.get("ts") or 0) > time.time() - 900 or r.get("status") == "running"
                       for r in rows):
                    act = self._act_text(3)
                    if act:
                        self._attach(req, "【你刚才做过的事（真实记录，不是猜的）】\n" + act)
            except Exception:
                pass
            # 群里刚发出去的内容（含你打的命令、别的插件发的结果）：她看不到，只能这样告诉她
            try:
                bufs = self._sent_recent(event.unified_msg_origin, 2, age=240)
                if bufs:
                    lines = "\n".join("- " + (b.get("text") or "")[:150] for b in bufs)
                    self._attach(req, "【这个会话里刚刚发出去的内容（不是你写的，也不是你说的）】\n" + lines)
            except Exception:
                pass

            if mode == "always":
                self._attach(req, self._apply_block(caps))
                return

            hits = self.match(text) if text else []
            # 话题延续：刚聊过哪个插件，紧接着的短句也算它，别让她失去上下文
            try:
                if hits:
                    self._last_cap[event.unified_msg_origin] = (
                        hits[0].get("plugin"), time.time())
                elif text:
                    p, ts = self._last_cap.get(event.unified_msg_origin) or ("", 0)
                    if p and time.time() - ts < 120:
                        cap = next((c for c in caps if c.get("plugin") == p), None)
                        if cap:
                            hits = [cap]
            except Exception:
                pass
            cross = self._cross_block(text) if text else ""
            if hits:
                block = self._apply_block(hits, semantic=True)
                tool_lines = []
                avail = self._req_tool_names(req)
                for c in hits:
                    ts = [x["cmd"] for x in (c.get("cmds") or []) if x.get("kind") == "tool"]
                    if avail is not None:
                        # 只报这次请求里真挂上的工具，免得她调一个不存在的
                        ts = [x for x in ts if x in avail]
                    if ts:
                        tool_lines.append("- %s 可用工具：%s" % (c["name"], "、".join(ts[:4])))
                if tool_lines:
                    block += "\n【优先直接调用这些工具，不要让你家主人自己敲命令】\n" + "\n".join(tool_lines)
                if cross:
                    block += "\n" + cross
                self._attach(req, block)
                self._note_thought("用户说：%s\n命中能力：%s" % (
                    text[:60], "、".join(c["name"] for c in hits)))
                _log("语义命中 %d 个能力：%s" % (len(hits), "、".join(c["name"] for c in hits)))
            elif cross:
                # 没命中具体插件，但这句话在说"别的会话"，把跨会话入口给她
                self._attach(req, cross)
                self._note_thought("用户说：%s\n跨会话提示已注入" % text[:60])
            elif self._cfg("mini_inject", True) and any(
                    k in text for k in ("你会", "能干什么", "有什么功能", "会什么", "能不能", "帮我")):
                self._attach(req, self._apply_block(caps))
        except Exception as e:
            _log("注入失败: %r" % e)

    # ---------- 让她在"别的会话"里执行 ----------
    def _resolve_umo(self, event, target):
        """把群号/会话标识解析成 umo：123456789 → 33:GroupMessage:123456789"""
        t = str(target or "").strip()
        if not t:
            return ""
        if t.count(":") >= 2:
            return t
        if t.isdigit():
            cur = str(getattr(event, "unified_msg_origin", "") or "")
            plat = cur.split(":", 1)[0] if ":" in cur else "aiocqhttp"
            return "%s:GroupMessage:%s" % (plat, t)
        return ""

    def _redirect_event(self, event, umo):
        """复制一份事件，只把目标会话换掉：命令和工具就会在那个会话里发消息"""
        if not umo:
            return event
        try:
            ev = copy.copy(event)
        except Exception:
            return event
        try:
            ev.unified_msg_origin = umo
        except Exception:
            return event
        return ev

    # ---------- 给她一个"自己执行命令"的入口 ----------
    @llm_tool(name="recent_actions")
    async def recent_actions_tool(self, event: AstrMessageEvent, count: int = 5) -> str:
        """查你最近做过什么（执行过哪些命令、成功没有、结果如何）。
        用户问"你刚才干什么了""那个弄好了吗"时用它，别靠猜。

        Args:
            count(int): 要看最近几条，默认 5
        """
        try:
            n = max(1, min(int(count or 5), 20))
        except Exception:
            n = 5
        rows = self._act_recent(n)
        if not rows:
            return json.dumps({"ok": True, "actions": [], "note": "最近没有执行记录"},
                              ensure_ascii=False)
        out = []
        for r in rows:
            out.append({
                "命令": r.get("cmd"), "参数": r.get("args"),
                "状态": {"running": "还在跑", "done": "成功", "failed": "失败"}.get(
                    r.get("status"), r.get("status")),
                "结果": r.get("note") or "",
                "多久以前": int(time.time() - float(r.get("ts") or time.time())),
                "耗时": r.get("took"),
            })
        return json.dumps({"ok": True, "actions": out}, ensure_ascii=False)

    @llm_tool(name="run_capability")
    async def run_capability_tool(self, event: AstrMessageEvent,
                                  command: str, args: str = "", target: str = "") -> str:
        """当用户要你做一件本机插件才能做的事，而这件事没有对应工具时，用这个去执行那条命令。
        要"去某个群做"的时候，把群号填进 target，命令会在那个会话里执行（不是做完转发过去）。

        Args:
            command(string): 命令名，不带斜杠，例如 点歌、群里聊什么
            args(string): 命令参数
            target(string): 目标会话：群号，或完整会话标识（33:GroupMessage:123456）；留空=当前会话
        """
        name = str(command or "").strip()
        if not self._norm_cmd(name):
            return json.dumps({"ok": False, "reason": "没给命令名"}, ensure_ascii=False)
        cap, short = self._resolve_cap_cmd(name)
        if cap is None:
            alts = getattr(self, "_cmd_alts", []) or []
            return json.dumps({
                "ok": False,
                "reason": "没有这个命令：%s" % name,
                "available": alts,
                "hint": "命令名要完全对上；下面是本机可用的命令，挑一个再调",
            }, ensure_ascii=False)
        if self._is_danger(name, ""):
            return json.dumps({"ok": False, "reason": "这个命令不允许自动执行"},
                              ensure_ascii=False)
        arg = _clean_arg(args, limit=200)
        umo = self._resolve_umo(event, target)
        if str(target or "").strip() and not umo:
            return json.dumps({"ok": False, "reason": "目标会话认不出来：%s" % target},
                              ensure_ascii=False)
        ev = self._redirect_event(event, umo)
        where = ("在 %s 里" % umo) if umo and umo != str(getattr(event, "unified_msg_origin", "")) else ""
        self._note_thought("她主动执行：%s/%s %s（%s）" % (where, name, arg, cap.get("name")))
        _log("工具 run_capability：%s/%s %s" % (where, short, arg))
        # 有的命令要跑几分钟（下载/变声/渲染），不能把她的回复卡在那里等
        ok = await self._grace(self._invoke_command(ev, short, arg), 8,
                               label=short, event=ev, args=arg, target=where, report=True)
        if ok is None:
            _log("run_capability：/%s 还在跑，先放她回话，结果出来会自己发到会话里" % short)
            return json.dumps({
                "ok": True, "status": "running", "command": name,
                "plugin": cap.get("name"), "target": umo or "当前会话",
                "hint": "已经交给插件在后台跑了，结果会自己出现在聊天里；别再调第二次",
            }, ensure_ascii=False)
        if ok is False:
            return json.dumps({"ok": False, "reason": self._last_error or "没跑成",
                               "command": name}, ensure_ascii=False)
        reply = " ".join(self._last_output)[:200] if self._last_output else ""
        return json.dumps({"ok": bool(ok), "command": name,
                           "plugin": cap.get("name"), "target": umo or "当前会话",
                           "reply": reply}, ensure_ascii=False)

    def _req_tool_names(self, req):
        """当前请求里真正挂了哪些工具；拿不到就返回 None（不过滤）"""
        try:
            ts = getattr(req, "func_tool", None)
            if ts is None:
                return None
            for attr in ("tools", "func_list"):
                lst = getattr(ts, attr, None)
                if isinstance(lst, (list, tuple, set)):
                    names = set()
                    for t in lst:
                        n = getattr(t, "name", None)
                        if not n and isinstance(t, dict):
                            n = t.get("name")
                        if n:
                            names.add(str(n))
                    return names
            g = getattr(ts, "get_tool", None)
            if callable(g):
                names = set()
                for nm in self._runtime_tools().keys():
                    try:
                        if g(nm):
                            names.add(nm)
                    except Exception:
                        pass
                return names
            return None
        except Exception:
            return None

    def _attach(self, req, block):
        try:
            from astrbot.core.agent.message import TextPart
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                req.extra_user_content_parts = []
                parts = req.extra_user_content_parts
            limit = int(self._cfg("max_chars", 1200))
            parts.append(TextPart(text=block[:limit]))
        except Exception as e:
            _log("附加失败: %r" % e)

    # ---------- 命令 ----------
    # ---------- 余额 / 讨饭 ----------
    def _balance_key(self):
        """优先用手填的，其次从 AstrBot 的 provider 配置里读，最后看环境变量"""
        k = str(self._cfg("balance_api_key", "") or "").strip()
        if k.startswith("sk-"):
            return k
        try:
            import json as _json
            cfg = os.path.join(os.path.dirname(os.path.dirname(self._plugins_dir())), "cmd_config.json")
            if os.path.isfile(cfg):
                t = io.open(cfg, encoding="utf-8-sig").read()
                m = re.findall(r"sk-[A-Za-z0-9_\-]{10,}", t)
                if m:
                    return m[0]
        except Exception:
            pass
        for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
            v = os.environ.get(env)
            if v:
                return v
        return ""

    async def _balance(self):
        """返回 (余额, 说明)：余额 None 表示这个接口查不到"""
        try:
            import aiohttp
            key = self._balance_key()
            if not key:
                return None, "没有 API Key"
            url = str(self._cfg("balance_api_url", "") or "").strip()
            if not url:
                url = "https://api.deepseek.com/user/balance"
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers={
                        "Authorization": "Bearer " + key,
                        "Accept": "application/json"}, timeout=20) as r:
                    if r.status in (401, 403):
                        return None, "Key 无效（HTTP %s）" % r.status
                    if r.status == 404:
                        return None, "这个模型没有余额接口"
                    if r.status != 200:
                        return None, "接口返回 %s" % r.status
                    data = await r.json(content_type=None)
            # DeepSeek: balance_infos[0].total_balance
            infos = data.get("balance_infos") or []
            if infos:
                return float(infos[0].get("total_balance") or 0), ""
            # 通用：total / balance / credit
            for k in ("total_balance", "balance", "credit", "total"):
                if k in data:
                    try:
                        return float(data[k]), ""
                    except Exception:
                        continue
            return None, "接口返回里没有余额字段"
        except Exception as e:
            _log("查余额失败: %r" % e)
            return None, "查询出错"

    @filter.command("余额")
    async def cmd_balance(self, event: AstrMessageEvent):
        """查模型余额（支持自定义接口）"""
        b, why = await self._balance()
        if b is None:
            yield event.plain_result("查不到余额：%s" % (why or "未知原因"))
        else:
            yield event.plain_result("余额还剩 %.2f 元。" % b)

    async def _beg_loop(self):
        await asyncio.sleep(30)
        while True:
            try:
                if self._cfg("enable_balance_alert", True):
                    b, why = await self._balance()
                    thr = float(self._cfg("balance_threshold", 5) or 5)
                    if b is not None and b < thr:
                        cd = float(self._cfg("balance_cooldown_hours", 2) or 0) * 3600
                        last = float(_read_json(CAP_CACHE, {}).get("beg_ts") or 0)
                        if not cd or time.time() - last >= cd:
                            target = str(self._cfg("beg_target_session", "") or "").strip()
                            if target:
                                from astrbot.core.message.message_event_result import MessageChain
                                await self.context.send_message(
                                    target,
                                    MessageChain().message("主人，余额只剩 %.2f 元了……" % b))
                                cache = _read_json(CAP_CACHE, {})
                                cache["beg_ts"] = time.time()
                                _write_json(CAP_CACHE, cache)
                                _log("已讨饭 %.2f" % b)
            except Exception as e:
                _log("讨饭循环异常: %r" % e)
            try:
                mins = max(5, int(self._cfg("balance_check_minutes", 30) or 30))
            except Exception:
                mins = 30
            await asyncio.sleep(mins * 60)

    async def initialize(self):
        try:
            self._apply_effort()
        except Exception:
            pass
        try:
            _log("运行日志已就绪（只记本插件自己的日志）")
        except Exception:
            pass
        try:
            if self._cfg("enable_balance_alert", True):
                t = asyncio.get_event_loop().create_task(self._beg_loop())
                self._tasks = getattr(self, "_tasks", set())
                self._tasks.add(t)
                _log("余额监视已启动")
        except Exception as e:
            _log("启动监视失败: %r" % e)
        try:
            if self._cfg("auto_profile", True):
                async def _warm():
                    await asyncio.sleep(25)     # 等插件都加载完再动，别抢启动
                    try:
                        done = await self.ensure_profiles(limit=3)
                        if done:
                            _log("启动补画像：%s" % "、".join(c["plugin"] for c in done))
                    except Exception as e2:
                        _log("启动补画像失败: %r" % e2)
                t2 = asyncio.get_event_loop().create_task(_warm())
                self._tasks = getattr(self, "_tasks", set())
                self._tasks.add(t2)
        except Exception as e:
            _log("启动画像任务失败: %r" % e)

    @filter.command("自兼容")
    async def cmd_autocompat(self, event: AstrMessageEvent):
        """生成/查看自兼容规则：/自兼容 生成|查看|清空"""
        arg = re.sub(r"^.*?自兼容", "", event.message_str or "").strip()
        if arg in ("查看", "list"):
            rules = self._auto_rules()
            if not rules:
                yield event.plain_result("还没有自动规则，发 /自兼容 生成。")
                return
            lines = ["自动规则 %d 条：" % len(rules)]
            for r in rules[:15]:
                lines.append("- [%s] %s → %s" % (r.get("plugin", ""), r.get("label", ""), r.get("template")))
            yield event.plain_result("\n".join(lines))
            return
        if arg in ("清空", "clear"):
            _write_json(self.RULES_CACHE, {"fp": "", "rules": []})
            yield event.plain_result("已清空自动规则。")
            return
        # 生成
        yield event.plain_result("开始读插件、生成规则，可能要一两分钟…")
        rules = await self._gen_rules(force=True)
        yield event.plain_result("生成完成：%d 条规则。" % len(rules))

    @filter.command("日志")
    async def cmd_log(self, event: AstrMessageEvent):
        """看她最近的运行日志：/日志 [关键词] [行数]"""
        arg = re.sub(r"^.*?日志", "", event.message_str or "").strip()
        n = 30
        kw = ""
        for tok in arg.split():
            if tok.isdigit():
                n = max(5, min(80, int(tok)))
            else:
                kw += tok
        lines = self._log_lines(n, kw)
        if not lines:
            yield event.plain_result("还没有日志（刚重启的话，跑一会儿就有了）。")
            return
        body = "\n".join(lines)
        yield event.plain_result(body[-1800:])

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9)
    async def log_intent(self, event: AstrMessageEvent):
        """她说"看看日志/为什么没发出来"时，自己去查"""
        q = (event.message_str or "").strip()
        if not q or len(q) > 60 or q.startswith("/"):
            return
        if not any(k in q for k in ("看看日志", "查日志", "看日志", "日志里", "为什么失败",
                                    "怎么失败了", "报错了", "刚才怎么了", "出啥问题")):
            return
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return
        except Exception:
            pass
        lines = self._log_lines(30)
        if not lines:
            return
        event.stop_event()
        yield event.plain_result("我翻了翻日志：\n" + "\n".join(lines)[-1500:])

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9)
    async def manual_intent(self, event: AstrMessageEvent):
        """问「某个插件怎么用」时，把存好的简易说明书发出去"""
        q = (event.message_str or "").strip()
        if not q or len(q) > 60 or q.startswith("/"):
            return
        if not self._manual_query(q):
            return
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return
        except Exception:
            pass
        cap = self._pick_cap_for(q)
        if not cap:
            return
        txt = self._manual(cap) or ""
        if not txt:
            return
        event.stop_event()
        yield event.plain_result(txt[:1800])

    PROFILE_HINTS = ("新插件", "新功能", "装了个插件", "装了个新功能", "装了插件",
                     "你去看看我给你装", "看看我装")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9)
    async def profile_intent(self, event: AstrMessageEvent):
        """她说"你看看我装的新东西"时，自己去补画像"""
        q = (event.message_str or "").strip()
        if not q or len(q) > 60 or q.startswith("/"):
            return
        if not any(k in q for k in self.PROFILE_HINTS):
            return
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return
        except Exception:
            pass
        event.stop_event()
        yield event.plain_result("我去看看……")
        try:
            done = await self.ensure_profiles(limit=3)
        except Exception as e:
            yield event.plain_result("看的时候出错了：%r" % (e,))
            return
        if not done:
            caps = [c for c in self._scan()
                    if self._profile(c.get("plugin")).get("what")]
            yield event.plain_result("没发现新的。我现在认得 %d 个：%s" % (
                len(caps), "、".join(c["name"] for c in caps[:8])))
            return
        lines = ["看完了，认出来 %d 个：" % len(done)]
        for cap in done:
            prof = self._profile(cap.get("plugin"))
            lines.append("- %s：%s" % (cap["name"], prof.get("what") or ""))
        yield event.plain_result("\n".join(lines))

    @filter.command("能力")
    async def cmd_caps(self, event: AstrMessageEvent):
        """查看/搜索自己的能力：/能力 [关键词|刷新|说明书|工具|过时]"""
        arg = re.sub(r"^.*?能力", "", event.message_str or "").strip()
        if arg in ("诊断", "diag"):
            rep = getattr(self, "_scan_report", None) or {}
            if not rep:
                self._scan(force=True)
                rep = getattr(self, "_scan_report", None) or {}
            reg = self._registry_commands()
            tools = self._runtime_tools()
            lines = [
                "插件目录：%s" % rep.get("dir", self._plugins_dir()),
                "文件夹 %s 个｜注册表 %d 个插件 / %d 条命令｜工具 %d 个" % (
                    rep.get("folders", "?"), len(reg),
                    sum(len(v) for v in reg.values()), len(tools)),
            ]
            for nm, why in (rep.get("report") or [])[:20]:
                lines.append("- %s：%s" % (nm, why))
            if len(rep.get("report") or []) > 20:
                lines.append("…共 %d 个目录" % len(rep["report"]))
            yield event.plain_result("\n".join(lines)[:1800])
            return
        if arg in ("刷新", "reload", "分析", "画像分析"):
            # 一个入口搞定：重扫 + 重写变过的画像和说明书
            self._scan(force=True)
            done = await self.ensure_profiles(limit=10)
            yield event.plain_result("已重新扫描：%d 个插件能力，更新了 %d 份画像/说明书。" % (
                len(self._caps), len(done)))
            return
        if arg in ("说明书", "说明", "画像", "profiles"):
            caps = self._scan()
            names = [c["name"] for c in caps
                     if os.path.isfile(self._manual_path(c.get("plugin")))]
            yield event.plain_result("已有说明书 %d 份：%s\n发 /能力 说明书 <插件名> 看具体一份" % (
                len(names), "、".join(names[:10])))
            return
        if arg.startswith("说明书 "):
            kw = arg.split(" ", 1)[1].strip()
            cap = self._pick_cap_for(kw)
            if not cap:
                yield event.plain_result("没找到和「%s」相关的插件。" % kw)
                return
            yield event.plain_result((self._manual(cap) or "")[:1800])
            return
        if arg in ("过时", "stale"):
            stale = [c["name"] for c in self._scan() if self._profile_stale(c)]
            if stale:
                yield event.plain_result("有 %d 个插件的画像/说明书过时：%s\n发 /能力 刷新 重写" % (
                    len(stale), "、".join(stale[:10])))
            else:
                yield event.plain_result("画像和说明书都是最新的。")
            return
        if arg in ("工具", "tools"):
            info = self._runtime_tools()
            blocked = set((_read_json(TOOL_STATE, {}) or {}).get("blocked") or [])
            lines = ["可调用工具 %d 个：" % len(info)]
            for n, t in sorted(info.items()):
                lines.append("- %s（%s）%s" % (
                    n, t.get("plugin") or "?", "｜不直连，交给模型" if n in blocked else ""))
            yield event.plain_result("\n".join(lines)[:1800])
            return
        if arg in ("重置工具", "工具重置"):
            self._tool_unblock()
            yield event.plain_result("已清空「不直连」的工具记录，下次会重新试。")
            return
        if arg:
            hits = self.match(arg, topk=5)
            if not hits:
                yield event.plain_result("没找到和「%s」相关的能力。" % arg)
                return
            lines = ["和「%s」相关的能力：" % arg]
            for c in hits:
                lines.append(self._cap_line(c))
            yield event.plain_result("\n".join(lines))
            return
        caps = self._scan()
        lines = ["本机能力（%d 个）：" % len(caps)]
        for c in caps:
            lines.append(self._cap_line(c))
        yield event.plain_result("\n".join(lines))
