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
