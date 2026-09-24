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
    AuthorizationError,
    ConflictError,
    DomainError,
    Lab,
    NotFoundError,
)

SERVICE_ID = "mobile-ad-audit"
SERVICE_NAME = "移动广告合规实验室"

DATA_FILE = os.environ.get("LAB_DATA_FILE")


class PersistenceError(RuntimeError):
    """落盘失败：内存已回滚到上一份持久快照，服务可继续提供服务。"""


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Store:
    """带锁与快照持久化的 Lab 仓储。

    所有写操作都在同一把锁内「先改内存、再原子落盘」。落盘失败时内存
    回滚到上一份已持久化快照，绝不保留未落盘的半成品（如半个案件周期）。
    """

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
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.lab.to_snapshot(), handle, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            # 原子替换未成功：旧快照仍是权威文件，清理半成品临时文件
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _rollback(self):
        """丢弃未落盘的内存改动，恢复上一份持久快照。"""
        try:
            self.lab = self._load()
        except Exception:
            # 快照文件本身不可读时也不能留半成品：退回到空仓储
            self.lab = Lab()

    def call(self, fn, *args, persist=False, **kwargs):
        with self.lock:
            result = fn(*args, **kwargs)
            if persist:
                try:
                    self.save()
                except OSError as exc:
                    self._rollback()
                    raise PersistenceError(
                        f"状态落盘失败，已回滚到上一持久快照：{exc}") from exc
            return result


STORE = Store(DATA_FILE)


def reset_store(path=None):
    """清空并重建全局仓储（供测试隔离使用）。"""
    global STORE
    STORE = Store(path)
    return STORE


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与合规实验室 JSON API。"""

    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
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

    def _send_error(self, exc):
        """把领域异常映射为稳定的 HTTP 错误语义。"""
        if isinstance(exc, NotFoundError):
            self._send_json(404, {"error": "not_found", "message": str(exc)})
        elif isinstance(exc, AuthorizationError):
            self._send_json(403, {"error": "forbidden", "message": str(exc)})
        elif isinstance(exc, ConflictError):
            # 409：终态冲突。响应体带回当前有效结论与审计链，方便调用方核对
            payload = {"error": "conflict", "code": exc.conflict_kind,
                       "message": str(exc)}
            if exc.current is not None:
                payload["current"] = exc.current
            self._send_json(409, payload)
        elif isinstance(exc, PersistenceError):
            self._send_json(500, {"error": "persistence_failed", "message": str(exc)})
        else:
            self._send_json(400, {"error": "domain_error", "message": str(exc)})

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
        except (NotFoundError, AuthorizationError, ConflictError,
                PersistenceError, DomainError) as exc:
            self._send_error(exc)

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

            if path.startswith("/findings/") and (
                    path.endswith("/review") or path.endswith("/corrections")):
                is_correction = path.endswith("/corrections")
                head = path[len("/findings/"):]
                suffix = "/corrections" if is_correction else "/review"
                finding_id = unquote(head[:-len(suffix)].rstrip("/"))

                def review_op():
                    if is_correction:
                        finding = lab.correct_finding(finding_id, payload)
                    else:
                        finding = lab.review_finding(finding_id, payload)
                    # 响应体即当前发现快照（含完整审计链）；幂等命中时用响应头标注
                    return lab._finding_snapshot(finding), getattr(lab, "_last_replay", False)

                snapshot, replayed = STORE.call(review_op, persist=True)
                headers = {"X-Idempotent-Replay": "true" if replayed else "false"}
                self._send_json(200, snapshot, extra_headers=headers)
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
        except (NotFoundError, AuthorizationError, ConflictError,
                PersistenceError, DomainError) as exc:
            self._send_error(exc)

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
