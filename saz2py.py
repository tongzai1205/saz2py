#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
saz2py — 把 Fiddler 的 .saz 抓包文件变成可回放的 Python 脚本 / pytest 用例 / Postman 集合。

用法:
    python saz2py.py capture.saz                     # 列出抓包内的接口清单
    python saz2py.py capture.saz -o replay.py        # 生成可回放的 requests 脚本
    python saz2py.py capture.saz --pytest -o test_api.py  # 生成 pytest 测试用例
    python saz2py.py capture.saz --postman api.json  # 导出 Postman Collection v2.1
    python saz2py.py capture.saz -o replay.py -f order   # 只处理 URL 含 "order" 的会话
    python saz2py.py capture.saz --json sessions.json    # 导出结构化 JSON

仅用标准库；生成的脚本依赖 requests (pip install requests)。
"""
import argparse
import io
import json
import re
import struct
import sys
import zipfile
import zlib

__version__ = "1.0.0"
from collections import Counter
from http.client import parse_headers
from pprint import pformat
from urllib.parse import unquote, urlsplit

# 兼容两种内部命名: 经典 Fiddler "raw/_c.txt001" / Fiddler Everywhere "raw/001_c.txt"
REQ_RE = re.compile(r"^raw/(?:_c\.txt(\d+)|(\d+)_c\.txt)$")
DROP_HEADERS = ("host", "content-length", "connection", "accept-encoding")
# 前端框架(Vite/Webpack dev server 等)的静态资源噪音
STATIC_EXT = (".js", ".mjs", ".css", ".scss", ".less", ".vue", ".ts", ".jsx", ".tsx",
              ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".woff",
              ".woff2", ".ttf", ".eot", ".map", ".html", ".htm", ".mp4", ".mp3")


def _is_noise(s):
    """OPTIONS 预检 / 静态资源 / SPA页面路由 / Vite内部路径 -> 无接口价值。"""
    if s["method"] in ("OPTIONS",):
        return True
    p = s["path"].lower()
    if p.endswith(STATIC_EXT):
        return True
    if p.startswith("/@") or p in ("/",) or p.startswith(("/src/", "/node_modules/")):
        return True  # Vite 虚拟模块 /@id/... /@vite/...、根路径、dev server 源码请求
    if "undefined" in p.split("/"):
        return True  # 前端拼接错误产生的 /xxx/undefined 路径
    if "text/html" in s.get("resp_ctype", ""):
        return True  # 响应是 HTML 的 SPA 页面路由（/todo-center /login 等），非接口
    return False


def split_http(raw):
    """原始 HTTP 报文 -> (首行文本, headers(Message对象), body字节)。
    兼容 Fiddler 存储时前导的 UTF-8 BOM。"""
    raw = raw.lstrip(b"\xef\xbb\xbf\r\n \t")
    fp = io.BytesIO(raw)
    first = fp.readline().rstrip(b"\r\n").decode("latin-1", "replace")
    try:
        headers = parse_headers(fp)
    except Exception:
        headers = {}
    return first, headers, fp.read()


def _try_local_entry(data, j):
    """尝试从位置 j 的本地文件头解析一个 zip 条目。
    返回 (ok, 结束位置, 名称, 内容)。"""
    if j + 30 > len(data):
        return False, j, None, None
    try:
        (_v, flags, method, _t, _d, _crc,
         csize, _usize, nlen, xlen) = struct.unpack_from("<HHHHHIIIHH", data, j + 4)
    except struct.error:
        return False, j, None, None
    name_end = j + 30 + nlen
    data_start = name_end + xlen
    if data_start > len(data):
        return False, j, None, None
    name = data[j + 30:name_end].decode("utf-8", "replace")
    if method == 0:  # stored
        content = data[data_start:data_start + csize]
        if len(content) != csize:
            return False, j, None, None
        return True, data_start + csize, name, content
    if method != 8:  # 只认 deflate
        return False, j, None, None
    if csize and not (flags & 0x08):  # 大小已知
        comp = data[data_start:data_start + csize]
        if len(comp) != csize:
            return False, j, None, None
        try:
            return True, data_start + csize, name, zlib.decompress(comp, -15)
        except zlib.error:
            return False, j, None, None
    # 流式写入(大小在后置 data descriptor): 喂块直到 deflate 流结束
    dec = zlib.decompressobj(-15)
    out = bytearray()
    pos = data_start
    while pos < len(data) and not dec.eof:
        chunk = data[pos:pos + (1 << 20)]
        try:
            out += dec.decompress(chunk)
        except zlib.error:
            return False, j, None, None
        pos += len(chunk)
    if not dec.eof:  # 文件在此被截断
        return False, j, None, None
    return True, pos - len(dec.unused_data), name, bytes(out)


def _recover_local_entries(path):
    """zip 中央目录损坏/截断时的恢复模式: 顺序扫本地文件头逐条恢复。
    返回 {name: bytes}；一个条目也救不回来时抛 BadZipFile。"""
    with open(path, "rb") as f:
        data = f.read()
    entries, i, skips = {}, 0, 0
    while True:
        j = data.find(b"PK\x03\x04", i)
        if j < 0:
            break
        ok, end, name, content = _try_local_entry(data, j)
        if ok:
            entries[name.replace("\\", "/")] = content
            i = end
        else:  # 误命中(压缩数据里的伪签名)或截断条目 -> 跳过继续找
            skips += 1
            i = j + 4
    if not entries:
        raise zipfile.BadZipFile("no local entries recovered")
    return entries


def _load_entries(path):
    """优先按标准 zip 读取; BadZipFile 时自动降级恢复模式。
    返回 ({name: bytes}, 是否使用了恢复模式)。"""
    try:
        with zipfile.ZipFile(path) as zf:
            return {n.replace("\\", "/"): zf.read(n)
                    for n in zf.namelist() if not n.endswith("/")}, False
    except zipfile.BadZipFile:
        return _recover_local_entries(path), True


def parse_saz(path, url_filter=None, keep_noise=False):
    """解析 .saz。返回 (原始会话数, 噪音数, 去重后的会话列表)。
    去重规则: 同一 method+path 只保留最后出现的会话（最新参数）。
    zip 结构损坏(如导出中断被截断)时自动启用恢复模式。"""
    entries, recovered = _load_entries(path)
    if recovered:
        print("⚠ zip 结构损坏(可能导出中断/复制不全)，已启用恢复模式，"
              "成功恢复 %d 个条目" % len(entries))
    sessions, total = [], 0
    names = set(entries)
    cids = sorted((m.group(1) or m.group(2) for n in names
                   for m in [REQ_RE.match(n)] if m), key=int)
    for cid in cids:
        cname = ("raw/_c.txt" + cid) if ("raw/_c.txt" + cid) in names \
            else "raw/%s_c.txt" % cid
        first, headers, body = split_http(entries[cname])
        parts = first.split()
        if len(parts) < 2:
            continue
        method, target = parts[0].upper(), parts[1]
        if method == "CONNECT" or not (
                target.startswith("/") or target.startswith("http")):
            continue
        total += 1
        host = headers.get("Host", "").strip()
        if target.startswith("http"):
            url = target
        elif host:
            url = ("https" if host.endswith(":443") else "http") + "://" + host + target
        else:
            url = target
        u = urlsplit(url)
        sess = {
            "id": int(cid), "method": method, "url": url, "host": host,
            "path": u.path or "/", "query": u.query,
            "status": 0, "resp_ctype": "", "resp_body": b"",
            "req_ctype": headers.get("Content-Type", "").split(";")[0].strip(),
            "req_headers": dict(headers.items()), "req_body": body,
        }
        sname = ("raw/_s.txt" + cid) if ("raw/_s.txt" + cid) in names \
            else "raw/%s_s.txt" % cid
        if sname in names:
            rfirst, rheaders, rbody = split_http(entries[sname])
            m = re.match(r"HTTP/[\d.]+\s+(\d+)", rfirst)
            sess["status"] = int(m.group(1)) if m else 0
            sess["resp_ctype"] = rheaders.get("Content-Type", "").split(";")[0].strip()
            sess["resp_body"] = rbody
        sessions.append(sess)

    if url_filter:
        sessions = [s for s in sessions if url_filter.lower() in s["url"].lower()]

    noise = 0
    if not keep_noise:  # 默认过滤静态资源与 OPTIONS 预检（前端框架噪音）
        kept = []
        for s in sessions:
            if _is_noise(s):
                noise += 1
            else:
                kept.append(s)
        sessions = kept

    dedup = {}
    for s in sessions:
        dedup[(s["method"], s["path"])] = s
    return total, noise, sorted(dedup.values(), key=lambda s: s["id"])


def body_kind(sess):
    """请求体分类 -> (kind, value)，kind: json / form / text / binary / None。"""
    body = sess["req_body"]
    if not body:
        return None, None
    ct = sess["req_ctype"]
    if "json" in ct:
        try:
            return "json", json.loads(body.decode("utf-8"))
        except Exception:
            pass
    if "x-www-form-urlencoded" in ct:
        return "form", body.decode("utf-8", "replace")
    try:
        return "text", body.decode("utf-8")
    except UnicodeDecodeError:
        return "binary", None


_SUCCESS_VALUES = {"0", "200", "00000", "success", "SUCCESS"}


def infer_asserts(s):
    """从抓包响应推断业务级断言。
    返回列表，元素为 ("eq", 键, 期望值) 或 ("has", 键)；
    识别不了（非 JSON / 非常见包装结构）时返回空列表。"""
    if s["status"] not in (200, 201) or "json" not in s["resp_ctype"]:
        return []
    try:
        j = json.loads(s["resp_body"].decode("utf-8"))
    except Exception:
        return []
    if not isinstance(j, dict):
        return []
    out = []
    for k in ("code", "errcode", "errCode", "status"):
        v = j.get(k)
        if v is not None and not isinstance(v, bool) and str(v) in _SUCCESS_VALUES:
            out.append(("eq", k, v))
            break
    if not out and j.get("success") is True:
        out.append(("eq", "success", True))
    if not out and "data" in j:
        out.append(("has", "data"))
    return out


def func_name(s, index, seen):
    """接口 -> 合法且可读的 Python 函数名，重名自动加序号。"""
    base = re.sub(r"[^a-z0-9]+", "_",
                  (s["method"] + "_" + s["path"]).lower()).strip("_")[:50] or "api"
    n = seen.get(base, 0)
    seen[base] = n + 1
    return base if n == 0 else "%s_%d" % (base, n)


# ---------------------------------------------------------------- v0.2 链式变量

ID_RE = re.compile(r"\b\d{12,20}\b")  # 雪花id等长数字（12~20位）


class _CtxRef(object):
    """JSON payload 里的占位符: pformat 时渲染成 ctx.get('变量名', '抓包原值')。
    提取优先、抓包值兜底 —— 生产者提取失败时链条不断。"""

    def __init__(self, name, value):
        self.name = name
        self.value = value

    def __repr__(self):
        return "ctx.get(%r, %r)" % (self.name, self.value)


def _find_json_path(obj, value, path=()):
    """在解析后的 JSON 里找 value 出现的位置，返回键/下标路径。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (str, int, float)) and str(v) == value:
                return path + (k,)
            r = _find_json_path(v, value, path + (k,))
            if r is not None:
                return r
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, (str, int, float)) and str(v) == value:
                return path + (i,)
            r = _find_json_path(v, value, path + (i,))
            if r is not None:
                return r
    return None


def _used_ids(s):
    """该请求用到(路径/query/请求体里)的长数字 id 集合。"""
    used = set(ID_RE.findall(s["path"]))
    if s["query"]:
        used |= set(ID_RE.findall(unquote(s["query"])))
    try:
        used |= set(ID_RE.findall(s["req_body"].decode("utf-8")))
    except UnicodeDecodeError:
        pass
    return used


def build_chains(sessions):
    """把「前序接口响应中出现的长数字 id」与「后续请求用到的同一 id」连成变量。
    返回 (var_by_value, setmap):
      var_by_value: id字面值 -> 变量名 (如 '2092516021364604930' -> 'mixingstationdict_id')
      setmap: 生产者会话id -> [(json路径, 变量名)] —— 需要在生成脚本里加提取代码
    只处理路径型 REST id（/resource/<id>），且要求生产者响应是可定位路径的 JSON。"""
    var_by_value, setmap = {}, {}
    resp_text = {s["id"]: s["resp_body"].decode("utf-8", "replace")
                 for s in sessions}
    resp_json = {}
    for s in sessions:  # 预解析响应 JSON
        if "json" in s["resp_ctype"]:
            try:
                resp_json[s["id"]] = json.loads(resp_text[s["id"]])
            except Exception:
                pass
    for s in sessions:
        for v in sorted(_used_ids(s)):
            if v in var_by_value:
                continue
            segs = [x for x in s["path"].split("/") if x]
            if v not in segs:  # 只参数化路径型 id
                continue
            i = segs.index(v)
            if i == 0 or not re.search(r"[A-Za-z]", segs[i - 1]):
                continue
            name = re.sub(r"[^a-z0-9]+", "_", segs[i - 1].lower()).strip("_") + "_id"
            while name in var_by_value.values():  # 变量名去重
                name += "_2"
            # 向前找: 更早会话的 JSON 响应里包含该 id 且能定位路径
            for t in sessions:
                if t["id"] >= s["id"]:
                    break
                j = resp_json.get(t["id"])
                if j is None or v not in resp_text[t["id"]]:
                    continue
                p = _find_json_path(j, v)
                if p is None:
                    continue
                var_by_value[v] = name
                setmap.setdefault(t["id"], []).append((p, name))
                break
    return var_by_value, setmap


def _url_expr(path_q, var_by_value):
    """把含链式 id 的 'path?query' 变成 '/x/' + ctx.get('name','原值') 表达式。"""
    if not var_by_value or not any(v in path_q for v in var_by_value):
        return None
    parts, last = [], 0
    for m in ID_RE.finditer(path_q):
        v = m.group(0)
        if v in var_by_value:
            if m.start() > last:
                parts.append(repr(path_q[last:m.start()]))
            parts.append("ctx.get(%r, %r)" % (var_by_value[v], v))
            last = m.end()
    parts.append(repr(path_q[last:]))
    expr = " + ".join(p for p in parts if p != "''")
    return expr if "ctx.get(" in expr else None


def _ctxify(obj, var_by_value):
    """递归把 JSON payload 中等于链式 id 的字面值换成 _CtxRef 占位符。"""
    if isinstance(obj, dict):
        return {k: _ctxify(v, var_by_value) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_ctxify(v, var_by_value) for v in obj]
    if isinstance(obj, (str, int)) and str(obj) in var_by_value:
        return _CtxRef(var_by_value[str(obj)], str(obj))
    return obj


def _py_path_expr(path):
    """JSON 路径 -> Python 表达式: ('data','list',0,'id') -> resp.json()['data']['list'][0]['id']"""
    expr = "resp.json()"
    for k in path:
        expr += "[%r]" % k
    return expr


def _js_path_expr(path):
    """JSON 路径 -> Postman 测试脚本表达式: -> j.data.list[0].id"""
    expr = "j"
    for k in path:
        if isinstance(k, int):
            expr += "[%d]" % k
        elif re.match(r"^[A-Za-z_]\w*$", k):
            expr += "." + k
        else:
            expr += "[%r]" % k
    return expr


def gen_script(sessions, saz_name="", var_by_value=None, setmap=None):
    """生成自包含的 requests 回放脚本：改 BASE_URL 即可跑。
    v0.2: 链式变量 —— 前序接口响应自动提取 id 存入 ctx，后续接口引用 ctx[...]。"""
    var_by_value = var_by_value or {}
    setmap = setmap or {}
    u0 = urlsplit(sessions[0]["url"]) if sessions else None
    base = ("%s://%s" % (u0.scheme, u0.netloc)) if (u0 and u0.netloc) else "http://CHANGE-ME"
    common = {}
    if sessions:
        for k, v in sessions[0]["req_headers"].items():
            if k.lower() not in DROP_HEADERS:
                common[k] = v

    lines = [
        "# -*- coding: utf-8 -*-",
        '"""由 saz2py 自动生成 (来源: %s) —— 改 BASE_URL 后即可逐个回放。"""' % saz_name,
        "import requests",
        "",
        'BASE_URL = "%s"  # ← 改成你的目标环境' % base,
        "TIMEOUT = 30",
        "",
        "# 登录态在下面的 Cookie / Authorization 里，过期就替换成新的",
        "HEADERS = %s" % json.dumps(common, ensure_ascii=False, indent=4),
        "",
        "# 链式变量: 由前序接口的响应自动提取（回放时随新建数据动态更新）",
        "ctx = {}",
        "",
        "",
    ]
    seen, call_names = {}, []
    for i, s in enumerate(sessions, 1):
        fname = func_name(s, i, seen)
        call_names.append("api_%03d_%s" % (i, fname))
        path_q = s["path"] + ("?" + s["query"] if s["query"] else "")
        extra = {k: v for k, v in s["req_headers"].items()
                 if k.lower() not in DROP_HEADERS and common.get(k) != v}
        h = "HEADERS" if not extra else "{**HEADERS, **%s}" % json.dumps(extra, ensure_ascii=False)
        lines.append("def api_%03d_%s():" % (i, fname))
        lines.append('    """%s %s   (抓包状态: %s)"""'
                     % (s["method"], path_q, s["status"] or "-"))
        kind, val = body_kind(s)
        url_e = _url_expr(path_q, var_by_value)
        url_arg = url_e if url_e else repr(path_q)
        if kind == "json":
            lines.append("    payload = %s" % pformat(_ctxify(val, var_by_value), width=100))
            lines.append('    resp = requests.request("%s", BASE_URL + %s, headers=%s, json=payload, timeout=TIMEOUT)'
                         % (s["method"], url_arg, h))
        elif kind in ("form", "text"):
            lines.append("    payload = %r" % val)
            lines.append('    resp = requests.request("%s", BASE_URL + %s, headers=%s, data=payload, timeout=TIMEOUT)'
                         % (s["method"], url_arg, h))
        elif kind == "binary":
            lines.append("    # 请求体为二进制 (%d 字节, %s)，请手工补充"
                         % (len(s["req_body"]), s["req_ctype"]))
            lines.append('    resp = requests.request("%s", BASE_URL + %s, headers=%s, timeout=TIMEOUT)'
                         % (s["method"], url_arg, h))
        else:
            lines.append('    resp = requests.request("%s", BASE_URL + %s, headers=%s, timeout=TIMEOUT)'
                         % (s["method"], url_arg, h))
        lines.append("    print(resp.status_code, resp.text[:200])")
        for p, name in setmap.get(s["id"], []):  # 生产者: 提取链式变量
            lines.append("    try:")
            lines.append("        ctx[%r] = %s" % (name, _py_path_expr(p)))
            lines.append("        print('  ⤴ 提取 %s =', ctx[%r])" % (name, name))
            lines.append("    except Exception:")
            lines.append("        print('  ⚠ 提取变量 %s 失败（响应结构与抓包时不同）')" % name)
        lines.append("    return resp")
        lines.extend(["", ""])

    lines.append('if __name__ == "__main__":')
    for name in call_names:
        lines.append("    %s()" % name)
    lines.append("")
    return "\n".join(lines)


def gen_pytest(sessions, saz_name="", var_by_value=None, setmap=None,
               out_name="test_replay.py"):
    """生成 pytest 风格测试文件：test_NNN_ 按抓包顺序执行，
    requests.Session fixture 复用连接并自动携带 Cookie，
    链式变量放模块级 ctx，自动生成状态码断言。"""
    var_by_value = var_by_value or {}
    setmap = setmap or {}
    u0 = urlsplit(sessions[0]["url"]) if sessions else None
    base = ("%s://%s" % (u0.scheme, u0.netloc)) if (u0 and u0.netloc) else "http://CHANGE-ME"
    common = {}
    if sessions:
        for k, v in sessions[0]["req_headers"].items():
            if k.lower() not in DROP_HEADERS:
                common[k] = v

    lines = [
        "# -*- coding: utf-8 -*-",
        '"""由 saz2py 自动生成 (来源: %s) —— pytest 回放用例。' % saz_name,
        "",
        "运行: pytest %s -v  (需 pip install requests pytest)" % out_name,
        '"""',
        "import pytest",
        "import requests",
        "",
        'BASE_URL = "%s"  # ← 改成你的目标环境' % base,
        "TIMEOUT = 30",
        "",
        "# 登录态在下面的 Cookie / Authorization 里，过期就替换成新的",
        "HEADERS = %s" % json.dumps(common, ensure_ascii=False, indent=4),
        "",
        "# 链式变量: 由前序接口的响应自动提取（回放时会随新建数据动态更新）",
        "ctx = {}",
        "",
        "",
        "@pytest.fixture(scope=\"session\")",
        "def api():",
        "    \"\"\"共享的 HTTP 会话: 连接复用 + Cookie 自动保持。\"\"\"",
        "    s = requests.Session()",
        "    s.headers.update(HEADERS)",
        "    s.verify = False",
        "    return s",
        "",
        "",
    ]
    seen = {}
    for i, s in enumerate(sessions, 1):
        fname = func_name(s, i, seen)
        tname = "test_%03d_%s" % (i, fname)
        path_q = s["path"] + ("?" + s["query"] if s["query"] else "")
        extra = {k: v for k, v in s["req_headers"].items()
                 if k.lower() not in DROP_HEADERS and common.get(k) != v}
        h = "api.headers" if not extra else "{**api.headers, **%s}" % json.dumps(extra, ensure_ascii=False)
        lines.append("def %s(api):" % tname)
        lines.append('    """%s %s   (抓包状态: %s)"""'
                     % (s["method"], path_q, s["status"] or "-"))
        kind, val = body_kind(s)
        url_e = _url_expr(path_q, var_by_value)
        url_arg = url_e if url_e else repr(path_q)
        if kind == "json":
            lines.append("    payload = %s" % pformat(_ctxify(val, var_by_value), width=100))
            lines.append('    resp = api.request("%s", BASE_URL + %s, headers=%s, json=payload, timeout=TIMEOUT)'
                         % (s["method"], url_arg, h))
        elif kind in ("form", "text"):
            lines.append("    payload = %r" % val)
            lines.append('    resp = api.request("%s", BASE_URL + %s, headers=%s, data=payload, timeout=TIMEOUT)'
                         % (s["method"], url_arg, h))
        elif kind == "binary":
            lines.append("    # 请求体为二进制 (%d 字节, %s)，请手工补充"
                         % (len(s["req_body"]), s["req_ctype"]))
            lines.append('    resp = api.request("%s", BASE_URL + %s, headers=%s, timeout=TIMEOUT)'
                         % (s["method"], url_arg, h))
        else:
            lines.append('    resp = api.request("%s", BASE_URL + %s, headers=%s, timeout=TIMEOUT)'
                         % (s["method"], url_arg, h))
        if s["status"] in (200, 201, 204):  # 只有抓包时明确成功才断言状态码
            lines.append("    assert resp.status_code == %d, resp.text[:300]" % s["status"])
        else:
            lines.append("    print(resp.status_code, resp.text[:200])  # 抓包状态 %s, 未自动断言"
                         % (s["status"] or "-"))
        ba = infer_asserts(s)  # 业务级断言: 按抓包响应的成功包装结构推断
        if ba:
            lines.append("    rj = resp.json()")
            for a in ba:
                if a[0] == "eq":
                    lines.append("    assert rj.get(%r) == %r, "
                                 "'业务码异常(若登录态/token过期请更新 HEADERS 后重跑): ' + str(rj)[:200]"
                                 % (a[1], a[2]))
                else:
                    lines.append("    assert %r in rj, str(rj)[:200]" % a[1])
        for p, name in setmap.get(s["id"], []):  # 生产者: 提取链式变量
            lines.append("    try:")
            lines.append("        ctx[%r] = %s" % (name, _py_path_expr(p)))
            lines.append("    except Exception:")
            lines.append("        print('⚠ 提取变量 %s 失败（响应结构与抓包时不同）')" % name)
        lines.extend(["", ""])
    return "\n".join(lines) + "\n"


def gen_postman(sessions, name="saz2py", var_by_value=None, setmap=None):
    """导出 Postman Collection v2.1。
    v0.2: 链式 id 变量化 ({{name}})，生产者请求挂测试脚本自动 set 集合变量。"""
    var_by_value = var_by_value or {}
    setmap = setmap or {}
    items = []

    def varify(text):
        """把文本里的链式 id 字面值替换成 {{变量名}}。"""
        for v, n in var_by_value.items():
            text = text.replace(v, "{{%s}}" % n)
        return text

    for s in sessions:
        headers = [{"key": k, "value": v} for k, v in s["req_headers"].items()
                   if k.lower() not in DROP_HEADERS]
        raw_url = varify(s["url"])
        item = {
            "name": varify("%s %s" % (s["method"], s["path"])),
            "request": {
                "method": s["method"],
                "header": headers,
                "url": {
                    "raw": raw_url,
                    "host": s["host"].split(":")[0].split("."),
                    "path": [varify(p) for p in s["path"].split("/") if p],
                },
            },
        }
        kind, val = body_kind(s)
        if kind == "json":
            item["request"]["body"] = {
                "mode": "raw",
                "raw": varify(json.dumps(val, ensure_ascii=False)),
                "options": {"raw": {"language": "json"}},
            }
        elif kind in ("form", "text"):
            item["request"]["body"] = {"mode": "raw", "raw": varify(val)}
        exec_lines = None
        for p, name2 in setmap.get(s["id"], []):  # 生产者: 测试脚本提取变量
            if exec_lines is None:
                exec_lines = ["var j = pm.response.json();"]
            exec_lines.append("try { pm.collectionVariables.set(%r, %s); } catch (e) {}"
                              % (name2, _js_path_expr(p)))
        ba = infer_asserts(s)  # 业务级断言 -> Postman Tests
        if ba:
            if exec_lines is None:
                exec_lines = []
            for a in ba:
                if a[0] == "eq":
                    v = a[2]
                    jv = "true" if v is True else ("false" if v is False
                                                   else json.dumps(v))
                    exec_lines.append(
                        "pm.test('%s == %s', function () { "
                        "pm.expect(pm.response.json()[%r]).to.eql(%s); });"
                        % (a[1], v, a[1], jv))
                else:
                    exec_lines.append(
                        "pm.test('has key %s', function () { "
                        "pm.expect(pm.response.json()).to.have.property(%r); });"
                        % (a[1], a[1]))
        if exec_lines:
            item["event"] = [{"listen": "test",
                              "script": {"type": "text/javascript",
                                         "exec": exec_lines}}]
        items.append(item)
    coll = {
        "info": {
            "name": name,
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
    }
    if var_by_value:
        coll["variable"] = [{"key": n, "value": v}
                            for v, n in var_by_value.items()]
    return coll


def sessions_to_json(sessions):
    out = []
    for s in sessions:
        d = dict(s)
        d.pop("resp_body", None)  # 响应体可能很大且含二进制，不导出
        d["req_body"] = s["req_body"].decode("utf-8", "replace")
        out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser(
        description="saz2py — Fiddler .saz 抓包 -> Python 回放脚本 / pytest 用例 / Postman 集合",
        epilog="示例: python saz2py.py capture.saz --pytest -o test_api.py")
    ap.add_argument("saz", help="Fiddler 导出的 .saz 文件路径")
    ap.add_argument("-V", "--version", action="version",
                    version="saz2py %s" % __version__)
    ap.add_argument("-o", "--out", help="生成 Python 回放脚本路径 (.py)")
    ap.add_argument("--postman", help="导出 Postman Collection v2.1 JSON 路径")
    ap.add_argument("--json", dest="json_out", help="导出结构化 JSON 路径")
    ap.add_argument("-f", "--filter", dest="url_filter",
                    help="只保留 URL 包含该关键字的会话")
    ap.add_argument("--all", dest="keep_noise", action="store_true",
                    help="保留 OPTIONS 预检与静态资源请求(默认过滤)")
    ap.add_argument("--pytest", dest="pytest_style", action="store_true",
                    help="配合 -o 使用: 生成 pytest 测试文件而非普通回放脚本")
    args = ap.parse_args()

    try:
        total, noise, sessions = parse_saz(args.saz, args.url_filter,
                                           keep_noise=args.keep_noise)
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        sys.exit("错误: 无法读取 .saz 文件 (标准 zip 与恢复模式均失败: %s)\n"
                 "提示: 若导出时中断, 请在 Fiddler 里重新导出一次" % e)
    except RuntimeError as e:  # saz 设了密码
        sys.exit("错误: 该 .saz 似乎设置了密码 (%s)" % e)
    if not sessions:
        msg = "没有解析到会话"
        if args.url_filter:
            msg += "（--filter=%r 过滤后为空）" % args.url_filter
        else:
            try:  # 文件能打开但格式没识别 -> 把条目名打出来便于排查
                sample = zipfile.ZipFile(args.saz).namelist()[:8]
                msg += "。文件可读但条目格式未识别，内部条目示例:\n  " + "\n  ".join(sample)
            except Exception:
                pass
        sys.exit(msg)

    cnt = Counter((s["method"], s["path"]) for s in sessions)
    print("共 %d 个去重接口 (原始 %d 个会话%s):\n" % (
        len(sessions), total,
        ", 过滤 %d 个静态资源/OPTIONS 噪音" % noise if noise else ""))
    for i, s in enumerate(sessions, 1):
        dup = cnt[(s["method"], s["path"])]
        print("  #%-3d %-6s %-60s %s%s" % (
            i, s["method"], (s["path"] + ("?" + s["query"] if s["query"] else ""))[:60],
            s["status"] or "-", "  x%d" % dup if dup > 1 else ""))

    var_by_value, setmap = build_chains(sessions)
    if var_by_value:
        print("\n✓ 识别出 %d 个链式变量（前序接口响应 -> 后续请求自动传递）:"
              % len(var_by_value))
        for v, n in sorted(var_by_value.items(), key=lambda kv: kv[1]):
            print("    {{%s}}  <-  %s" % (n, v))
    n_assert = sum(1 for s in sessions if infer_asserts(s))
    if n_assert:
        print("✓ 将为 %d 个接口自动生成业务断言（code/success/data 结构）" % n_assert)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            if args.pytest_style:
                f.write(gen_pytest(sessions, args.saz, var_by_value, setmap,
                                   out_name=args.out.split("/")[-1].split("\\")[-1]))
                print("\n✓ pytest 测试文件已生成: %s  (运行: pytest %s -v)"
                      % (args.out, args.out.split("/")[-1].split("\\")[-1]))
            else:
                f.write(gen_script(sessions, args.saz, var_by_value, setmap))
                print("\n✓ 回放脚本已生成: %s" % args.out)
    if args.postman:
        with open(args.postman, "w", encoding="utf-8") as f:
            json.dump(gen_postman(sessions, name="saz2py",
                                  var_by_value=var_by_value, setmap=setmap),
                      f, ensure_ascii=False, indent=2)
        print("✓ Postman 集合已导出: %s" % args.postman)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(sessions_to_json(sessions), f, ensure_ascii=False, indent=2)
        print("✓ JSON 已导出: %s" % args.json_out)


if __name__ == "__main__":
    main()
