"""移动广告合规实验室的 HTTP 入口。

除健康检查外提供 JSON API（详见 README「接口一览」）。仓储默认在内存中，
设置环境变量 LAB_DATA_FILE 后会把只增证据快照落盘，重启自动恢复。
"""

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from domain import (
    DomainError,
    Lab,
    NotFoundError,
)

SERVICE_ID = "mobile-ad-audit"
SERVICE_NAME = "移动广告合规实验室"

DATA_FILE = os.environ.get("LAB_DATA_FILE")


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Store:
    """带锁与快照持久化的 Lab 仓储。"""

    def __init__(self, path=None):
        self.path = path
        self.lock = threading.RLock()
        self.lab = self._load()

    def _load(self):
        if self.path and os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as handle:
                return Lab.from_snapshot(json.load(handle))
        return Lab()

    def save(self):
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.lab.to_snapshot(), handle, ensure_ascii=False)
        os.replace(tmp, self.path)

    def call(self, fn, *args, persist=False, **kwargs):
        with self.lock:
            result = fn(*args, **kwargs)
            if persist:
                self.save()
            return result


STORE = Store(DATA_FILE)


def reset_store(path=None):
    """清空并重建全局仓储（供测试隔离使用）。"""
    global STORE
    STORE = Store(path)
    return STORE


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与合规实验室 JSON API。"""

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}")

    def _json_404(self):
        self._send_json(404, {"error": "not_found", "message": "未知接口"})

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if path == "/health":
                self._send_json(200, health_payload())
                return
            if path.startswith("/tasks/"):
                task_id = unquote(path.split("/", 2)[2])
                self._send_json(200, STORE.call(STORE.lab.task_report, task_id))
                return
            if path.startswith("/builds/") and path.endswith("/report"):
                build_id = unquote(path[len("/builds/"):-len("/report")])
                self._send_json(200, STORE.call(STORE.lab.build_report, build_id))
                return
            if path.startswith("/subjects/"):
                parts = path.split("/")
                if len(parts) != 4:
                    self._json_404()
                    return
                # /subjects/{type}/{id}
                subject_type, subject_id = parts[2], unquote(parts[3])
                self._send_json(
                    200, STORE.call(STORE.lab.subject_view, subject_type, subject_id)
                )
                return
            self._json_404()
        except NotFoundError as exc:
            self._send_json(404, {"error": "not_found", "message": str(exc)})
        except DomainError as exc:
            self._send_json(400, {"error": "domain_error", "message": str(exc)})

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            payload = self._read_json()
            lab = STORE.lab

            if path == "/admin/regulations":
                self._send_json(201, STORE.call(lab.register_regulation, payload, persist=True))
                return
            if path == "/admin/scripts":
                self._send_json(201, STORE.call(lab.register_script, payload, persist=True))
                return
            if path == "/devices":
                self._send_json(201, STORE.call(lab.register_device, payload, persist=True))
                return
            if path == "/builds":
                self._send_json(201, STORE.call(lab.register_build, payload, persist=True))
                return
            if path == "/tasks":
                self._send_json(201, STORE.call(lab.create_task, payload, persist=True))
                return

            if path.startswith("/tasks/"):
                rest = path[len("/tasks/"):]
                if rest.endswith("/events"):
                    task_id = unquote(rest[: -len("/events")])
                    result = STORE.call(
                        lab.ingest_events, task_id, payload.get("events", []), persist=True
                    )
                    self._send_json(202, result)
                    return
                if rest.endswith("/complete"):
                    task_id = unquote(rest[: -len("/complete")].rstrip("/"))
                    self._send_json(200, STORE.call(lab.complete_task, task_id, persist=True))
                    return

            if path.startswith("/findings/") and path.endswith("/review"):
                finding_id = unquote(path[len("/findings/"):-len("/review")].rstrip("/"))
                self._send_json(200, STORE.call(lab.review_finding, finding_id, payload, persist=True))
                return

            if path.startswith("/subjects/"):
                parts = path.split("/")
                # /subjects/{type}/{id}/notices | /retests
                if len(parts) != 5 or parts[4] not in ("notices", "retests"):
                    self._json_404()
                    return
                subject_type, subject_id = parts[2], unquote(parts[3])
                if parts[4] == "notices":
                    self._send_json(
                        201,
                        STORE.call(lab.generate_notice, subject_type, subject_id, payload, persist=True),
                    )
                else:
                    self._send_json(
                        201,
                        STORE.call(lab.record_retest, subject_type, subject_id, payload, persist=True),
                    )
                return

            self._json_404()
        except NotFoundError as exc:
            self._send_json(404, {"error": "not_found", "message": str(exc)})
        except DomainError as exc:
            self._send_json(400, {"error": "domain_error", "message": str(exc)})

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        Lab()  # 领域模块可实例化
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
