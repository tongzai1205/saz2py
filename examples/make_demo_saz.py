# -*- coding: utf-8 -*-
"""造一个模拟 Fiddler 导出的 .saz（zip 结构: raw/_c.txtNNN + raw/_s.txtNNN），
用于自测 saz2py。"""
import zipfile

def req(method, path, body=b"", ctype="application/json"):
    head = ("%s %s HTTP/1.1\r\nHost: 192.168.18.43\r\n"
            "User-Agent: Mozilla/5.0 Fiddler\r\n"
            "Content-Type: %s\r\nContent-Length: %d\r\n\r\n"
            % (method, path, ctype, len(body)))
    return b"\xef\xbb\xbf" + head.encode("utf-8") + body  # 带BOM, 模拟Fiddler坑

def resp(code, ctype="application/json", body=b'{"code":0,"msg":"ok"}'):
    return ("HTTP/1.1 %d OK\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n"
            % (code, ctype, len(body))).encode("latin-1") + body

s = [
    (req("POST", "/api/signage/version/save", b'{"name":"v1","price":12.5}'), resp(200)),
    (req("GET", "/api/sceneInfo/list?type=1"), resp(200)),
    (req("GET", "/api/sceneInfo/list?type=2"), resp(200)),          # 重复接口,应去重
    (req("POST", "/api/signage/version/save", b'{"name":"v2","price":13.0}'), resp(200)),  # 应保留这个(最新)
    (req("POST", "/api/upload/img", b"\x00\x01\x02\xff", "application/octet-stream"), resp(500)),
    (req("POST", "/api/login", b"user=te1&pwd=abc", "application/x-www-form-urlencoded"), resp(200)),
    (b"BADDATA", resp(200)),  # 坏数据,应跳过
]

with zipfile.ZipFile("D:/test/pachpng/saz2py/fake_capture.saz", "w") as z:
    for i, (r, p) in enumerate(s, 1):
        z.writestr("raw/_c.txt%03d" % i, r)
        z.writestr("raw/_s.txt%03d" % i, p)
print("fake_capture.saz 生成完毕, 共 %d 个会话" % len(s))
