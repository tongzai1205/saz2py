# -*- coding: utf-8 -*-
"""saz2py 测试套件：解析 / 生成 / 链式变量 / 业务断言 / 损坏恢复 / 端到端。

运行: python -m pytest tests -q
"""
import io
import json
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import saz2py  # noqa: E402

DEMO = ROOT / "examples" / "demo.saz"

REQ_TMPL = (
    "GET /api/order/list HTTP/1.1\r\n"
    "Host: demo.example.com\r\n"
    "Accept: application/json\r\n"
    "\r\n"
)
RESP_TMPL = (
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: application/json; charset=utf-8\r\n"
    "\r\n"
    '{"code": 0, "msg": "ok", "data": [{"id": 2092805340221083649}]}'
)


def _sess(i, method="GET", path="/api/x", query="", status=200,
          resp=None, resp_ctype="application/json", body=b"", req_ctype=""):
    return {
        "id": i, "method": method, "url": "http://demo.example.com" + path,
        "host": "demo.example.com", "path": path, "query": query,
        "status": status, "resp_ctype": resp_ctype,
        "resp_body": json.dumps(resp).encode() if resp is not None else b"",
        "req_ctype": req_ctype, "req_headers": {"Accept": "application/json"},
        "req_body": body,
    }


# ------------------------------------------------------------------ 解析

def test_parse_demo_saz():
    total, noise, sessions = saz2py.parse_saz(str(DEMO))
    assert total == 6          # 6 个有效会话（含 1 个重复接口、1 个 500）
    assert len(sessions) == 4  # 去重后 4 个接口
    assert all(s["path"].startswith("/api") for s in sessions)


def test_parse_filter():
    _t, _n, sessions = saz2py.parse_saz(str(DEMO), url_filter="login")
    assert len(sessions) == 1
    assert "login" in sessions[0]["path"]


def test_noise_detection():
    assert saz2py._is_noise(_sess(1, method="OPTIONS"))
    assert saz2py._is_noise(_sess(2, path="/assets/index-a1b2.js"))
    assert saz2py._is_noise(_sess(3, path="/todo-center", resp_ctype="text/html"))
    assert saz2py._is_noise(_sess(4, path="/@id/virtual:svg-icons-register"))
    assert not saz2py._is_noise(_sess(5, path="/api/order/page"))


def test_recover_truncated_saz(tmp_path):
    """导出中断的 saz（中央目录丢失）应能走恢复模式解析。"""
    p = tmp_path / "broken.saz"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("raw/01_c.txt", REQ_TMPL)
        z.writestr("raw/01_s.txt", RESP_TMPL)
    p.write_bytes(buf.getvalue()[:-22])  # 砍掉 EOCD，模拟截断

    with pytest.raises(zipfile.BadZipFile):  # 标准 zip 读取确认已损坏
        zipfile.ZipFile(str(p))

    total, _noise, sessions = saz2py.parse_saz(str(p))
    assert total == 1
    assert sessions[0]["path"] == "/api/order/list"
    assert sessions[0]["status"] == 200


# ------------------------------------------------------------------ 断言推断

@pytest.mark.parametrize("resp,expected", [
    ({"code": 0, "data": {}}, [("eq", "code", 0)]),
    ({"code": "0", "data": {}}, [("eq", "code", "0")]),
    ({"status": 200}, [("eq", "status", 200)]),
    ({"success": True}, [("eq", "success", True)]),
    ({"data": {"a": 1}}, [("has", "data")]),
    ({"code": 500, "msg": "err"}, []),
    ({"unrelated": 1}, []),
])
def test_infer_asserts(resp, expected):
    assert saz2py.infer_asserts(_sess(1, resp=resp)) == expected


def test_infer_asserts_skips_non_success():
    s = _sess(1, resp={"code": 0}, status=500)
    assert saz2py.infer_asserts(s) == []
    s = _sess(2, resp=None, resp_ctype="text/html")
    assert saz2py.infer_asserts(s) == []


# ------------------------------------------------------------------ 链式变量

def test_build_chains():
    rid = "2092805340221083649"
    producer = _sess(1, path="/api/order/list",
                     resp={"code": 0, "data": [{"id": int(rid)}]})
    consumer = _sess(2, path="/api/order/%s/detail" % rid, query="orderId=" + rid)
    var_by_value, setmap = saz2py.build_chains([producer, consumer])
    assert var_by_value == {rid: "order_id"}
    assert setmap[1] == [(("data", 0, "id"), "order_id")]


def test_chain_rendered_in_outputs():
    rid = "2092805340221083649"
    sessions = [
        _sess(1, path="/api/order/list",
              resp={"code": 0, "data": [{"id": int(rid)}]}),
        _sess(2, path="/api/order/%s/detail" % rid, query="orderId=" + rid),
    ]
    var_by_value, setmap = saz2py.build_chains(sessions)

    py = saz2py.gen_pytest(sessions, "t.saz", var_by_value, setmap)
    assert "ctx['order_id'] = resp.json()['data'][0]['id']" in py
    assert "ctx.get('order_id', '%s')" % rid in py
    compile(py, "gen.py", "exec")  # 语法必须合法

    pm = saz2py.gen_postman(sessions, var_by_value=var_by_value, setmap=setmap)
    assert pm["variable"] == [{"key": "order_id", "value": rid}]
    assert "{{order_id}}" in json.dumps(pm, ensure_ascii=False)
    assert any(it.get("event") for it in pm["item"])  # 生产者挂了提取脚本


def test_build_chains_query_and_body():
    """v1.2: query 参数与 JSON 请求体里的 id 引用也要连成链。"""
    rid = "2092805340221083649"
    producer = _sess(1, method="POST", path="/api/tree/save",
                     resp={"code": 0, "data": {"id": rid}})
    q_consumer = _sess(2, path="/api/tree/detail", query="id=" + rid)
    b_consumer = _sess(3, method="POST", path="/api/other/save",
                       req_ctype="application/json",
                       body=json.dumps({"pid": rid}).encode())
    var_by_value, setmap = saz2py.build_chains([producer, q_consumer, b_consumer])
    assert var_by_value == {rid: "tree_id"}
    assert setmap[1] == [(("data", "id"), "tree_id")]
    # 渲染: query 与 body 里都要变成 ctx 引用
    py = saz2py.gen_pytest([producer, q_consumer, b_consumer],
                           "t.saz", var_by_value, setmap)
    assert "?id=' + ctx.get('tree_id'" in py
    assert "'pid': ctx.get('tree_id'" in py
    compile(py, "gen.py", "exec")


def test_echo_session_not_producer():
    """查询回显型接口(GET ?id=X 且响应含 X)不能当生产者。"""
    rid = "2092805340221083649"
    echo = _sess(1, path="/api/tree/get", query="id=" + rid,
                 resp={"code": 0, "data": {"id": rid}})
    consumer = _sess(2, method="POST", path="/api/other/save",
                     req_ctype="application/json",
                     body=json.dumps({"pid": rid}).encode())
    var_by_value, _ = saz2py.build_chains([echo, consumer])
    assert var_by_value == {}


def test_rescue_producers_keeps_creator():
    """链路感知去重: 同端点多次调用时，创建者会话（响应产出后续所需 id）要保留。"""
    rid = "2092805340221083649"
    creator = _sess(1, method="POST", path="/api/tree/save",
                    resp={"code": 0, "data": {"id": rid}})
    poll = _sess(2, path="/api/tree/get", query="id=" + rid,
                 resp={"code": 0, "data": {"id": rid}})
    other = _sess(3, method="POST", path="/api/tree/save",
                  req_ctype="application/json",
                  body=json.dumps({"name": "b", "pid": rid}).encode(),
                  resp={"code": 0, "data": "ok"})
    kept, rescued = saz2py._rescue_producers([creator, poll, other], [poll, other])
    assert rescued == 1
    assert [s["id"] for s in kept] == [1, 2, 3]  # creator 被恢复


def test_rescue_prefers_write_producer():
    """有写接口生产者时，不恢复 GET 回显会话。"""
    rid = "2092805340221083649"
    creator = _sess(1, method="POST", path="/api/tree/save",
                    resp={"code": 0, "data": {"id": rid}})
    get_echo = _sess(2, path="/api/tree/get", query="name=x",
                     resp={"code": 0, "data": {"list": [rid]}})
    consumer = _sess(3, path="/api/tree/detail", query="id=" + rid)
    kept, rescued = saz2py._rescue_producers(
        [creator, get_echo, consumer], [get_echo, consumer])
    assert rescued == 1
    assert [s["id"] for s in kept] == [1, 2, 3]  # 恢复的是 #1 而非 GET 回显


# ------------------------------------------------------------------ 回放唯一化

def _post_save_sess():
    """模拟一个写入型创建请求: 自身 id(雪花) + 外键短 id + 名称。"""
    return _sess(
        1, method="POST", path="/api/business/x/save", status=200,
        resp={"code": 0, "data": None}, req_ctype="application/json",
        body=json.dumps({
            "id": "2114646914210250753",   # 资源自身主键(长数字)
            "fkId": "222826",              # 外键(短数字，应保留)
            "name": "测试 11",             # 名称类字段
        }).encode())


def test_fuzz_id_only_by_default():
    """默认只对写入型请求体的 id 唯一化，外键与名称保持不变。"""
    s = _post_save_sess()
    py = saz2py.gen_pytest([s], "t.saz")
    assert "'id': gen_id()" in py
    assert "fkId': '222826'" in py          # 外键保留原值
    assert "'name': '测试 11'" in py         # 名称未动（除非 --fuzz-fields）
    assert "def gen_id():" in py
    assert "def gen_unique(" in py            # 辅助函数一并注入（即使未用到）
    compile(py, "gen.py", "exec")


def test_fuzz_id_disabled_with_no_unique():
    """--no-unique 应关闭唯一化，保留原始 id 字面值。"""
    s = _post_save_sess()
    py = saz2py.gen_pytest([s], "t.saz", auto_id=False)
    assert "gen_id()" not in py
    assert "'id': '2114646914210250753'" in py


def test_fuzz_fields_adds_unique_suffix():
    """--fuzz-fields name 应对该字段生成 gen_unique(原值)，追加时间戳。"""
    s = _post_save_sess()
    py = saz2py.gen_pytest([s], "t.saz", fuzz_keys={"name"}, auto_id=True)
    assert "gen_unique('测试 11')" in py
    assert "'id': gen_id()" in py            # id 仍自动唯一化


def test_fuzz_does_not_touch_get():
    """GET 请求体里的 id 不应被唯一化(避免把查询参数变成随机值)。"""
    s = _sess(1, path="/api/x", query="id=2114646914210250753",
              resp={"code": 0})
    py = saz2py.gen_pytest([s], "t.saz")
    assert "gen_id()" not in py


def test_fuzz_does_not_touch_single_id_reference():
    """单键 {id:...} 的「确认/提交/删除」类报文是引用已存在记录，
    不应自动唯一化，否则服务端报「未找到相关节点」。"""
    s = _sess(1, method="POST", path="/api/business/x/confirm", status=200,
              resp={"code": 0}, req_ctype="application/json",
              body=json.dumps({"id": "2114646914210250753"}).encode())
    py = saz2py.gen_pytest([s], "t.saz")
    assert "gen_id()" not in py
    assert "'id': '2114646914210250753'" in py   # 保留原值


def test_fuzz_touches_multi_field_create():
    """POST 多字段「保存/新建」报文里的 id 才是资源自身主键，应唯一化。"""
    s = _post_save_sess()   # POST .../save，含 id+fkId+name
    py = saz2py.gen_pytest([s], "t.saz")
    assert "'id': gen_id()" in py


def test_gen_id_unique_across_calls():
    # gen_id 仅存在于生成文件，这里复刻其算法做唯一性校验（与源码保持一致）
    import time, random
    seen = set()
    for _ in range(1000):
        v = str(time.time_ns()) + str(random.randint(1000, 9999))
        assert v not in seen
        seen.add(v)


# ------------------------------------------------------------------ 生成产物

def test_gen_script_compiles():
    sessions = saz2py.parse_saz(str(DEMO))[2]
    code = saz2py.gen_script(sessions, "demo.saz")
    compile(code, "replay.py", "exec")
    assert "BASE_URL" in code and "def api_001_" in code


def test_gen_postman_structure():
    sessions = saz2py.parse_saz(str(DEMO))[2]
    coll = saz2py.gen_postman(sessions)
    assert coll["info"]["schema"].endswith("collection.json")
    assert len(coll["item"]) == len(sessions)
    assert all("request" in it for it in coll["item"])


def test_generated_pytest_collects(tmp_path):
    sessions = saz2py.parse_saz(str(DEMO))[2]
    f = tmp_path / "test_demo.py"
    f.write_text(saz2py.gen_pytest(sessions, "demo.saz",
                                   out_name="test_demo.py"), encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "pytest", str(f),
                        "--collect-only", "-q"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "%d tests collected" % len(sessions) in r.stdout


# ------------------------------------------------------------------ 端到端

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"code": 0, "msg": "ok",
                           "data": {"id": 2092805340221083649}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_end_to_end_against_mock_server(tmp_path):
    """本地起 mock 服务，生成 pytest 用例并真跑，必须全绿。"""
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        sessions = []
        for i, path in enumerate(["/api/order/list", "/api/order/detail"], 1):
            s = _sess(i, path=path,
                      resp={"code": 0, "data": {"id": 2092805340221083649}})
            s["url"] = "http://127.0.0.1:%d%s" % (port, path)
            sessions.append(s)
        f = tmp_path / "test_live.py"
        f.write_text(saz2py.gen_pytest(sessions, "live.saz",
                                       out_name="test_live.py"), encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "pytest", str(f), "-q"],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "2 passed" in r.stdout
    finally:
        srv.shutdown()
