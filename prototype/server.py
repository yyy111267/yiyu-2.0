#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
以渔 2.0 原型开发服务器：纯静态文件服务（替代 python -m http.server）。
"""
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8123


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)


if __name__ == "__main__":
    print("以渔 2.0 dev server  ->  http://localhost:%d" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
