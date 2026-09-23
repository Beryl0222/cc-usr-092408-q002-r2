"""HTTP 端到端测试：完整跑通采集 → 判定 → 复核 → 告知 → 复测 → 回潮链路。"""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from service import Handler

NOW = 1_700_000_000
APP_ID = "com.example.news"
SDK_ID = "shake-sdk-9"


def call(method, url, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"} if data else {}
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def ad(ad_id, placement="splash"):
    return {"event_id": f"e-{ad_id}-shown", "seq": 1, "type": "ad_shown",
            "occurred_at": NOW + 100, "payload": {"ad_id": ad_id, "placement": placement}}


def close(ad_id, seq, after, size, reader=True):
    return {"event_id": f"e-{ad_id}-close-{seq}", "seq": seq, "type": "close_affordance",
            "occurred_at": NOW + 100 + after,
            "payload": {"ad_id": ad_id, "present": True, "visible_after_seconds": after,
                        "touch_target_dp": size, "screen_reader_actionable": reader}}


def jump(ad_id, seq, at, trigger, sdk=None, **extra):
    payload = {"ad_id": ad_id, "trigger": trigger, "target_url": "https://shop.example/p"}
    if sdk:
        payload["sdk"] = {"id": sdk, "name": "摇一摇SDK"}
    payload.update(extra)
    return {"event_id": f"e-{ad_id}-jump-{seq}", "seq": seq, "type": "jump",
            "occurred_at": at, "payload": payload}


class ApiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service.reset_store()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        service.reset_store()

    def _seed(self):
        self.post("/admin/regulations", 201, {
            "version": "v2025.1", "effective_at": 0, "params": {"rectification_days": 10}})
        self.post("/admin/scripts", 201,
                  {"script_id": "ad-trip", "version": "1.0", "created_at": NOW})
        self.post("/devices", 201,
                  {"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
        self.post("/devices", 201,
                  {"device_id": "dev-B", "model": "Pixel 8", "os_version": "Android 14"})
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1001, "version_name": "8.1.0"})

    def post(self, path, expected, body):
        status, payload = call("POST", self.base + path, body)
        self.assertEqual(status, expected, payload)
        return payload

    def get(self, path, expected=200):
        status, payload = call("GET", self.base + path)
        self.assertEqual(status, expected, payload)
        return payload

    def _create_task(self, device, track):
        return self.post("/tasks", 201,
                         {"build_id": f"{APP_ID}:1001", "device_id": device, "track": track})

    def test_full_enforcement_flow_over_http(self):
        self._seed()

        # 三条轨迹 + 设备 B 的并存任务
        normal = self._create_task("dev-A", "normal")
        reader = self._create_task("dev-A", "screen_reader")
        elder = self._create_task("dev-A", "elderly")
        normal_b = self._create_task("dev-B", "normal")
        for task in (normal, reader, elder, normal_b):
            self.assertEqual(task["regulation_version"], "v2025.1")
            self.assertEqual(task["script"], {"script_id": "ad-trip", "version": "1.0"})

        # 普通轨迹：关闭入口迟到 + SDK 自动跳转
        batch = [ad("a1"), close("a1", 2, after=5, size=36),
                 jump("a1", 3, NOW + 106, "auto", sdk=SDK_ID)]
        accepted = self.post(f"/tasks/{normal['task_id']}/events", 202, {"events": batch})
        self.assertEqual(len(accepted["accepted"]), 3)
        retried = self.post(f"/tasks/{normal['task_id']}/events", 202, {"events": batch})
        self.assertEqual(retried["accepted"], [])
        self.assertEqual(len(retried["duplicates"]), 3)  # 重传统一去重

        # 读屏轨迹：关闭入口不可聚焦
        self.post(f"/tasks/{reader['task_id']}/events", 202, {"events": [
            ad("a2"), close("a2", 2, after=1, size=48, reader=False)]})
        # 老人轨迹：点区不达标
        self.post(f"/tasks/{elder['task_id']}/events", 202, {"events": [
            ad("a3"), close("a3", 2, after=1, size=48)]})
        # 设备 B：合规
        self.post(f"/tasks/{normal_b['task_id']}/events", 202, {"events": [
            ad("b1"), close("b1", 2, after=1, size=48)]})
        for task in (normal, reader, elder, normal_b):
            self.post(f"/tasks/{task['task_id']}/complete", 200, {})

        report = self.get(f"/builds/{APP_ID}:1001/report")
        by_track = {(r["device_id"], r["track"]): r for r in report["tracks"]}
        self.assertEqual(by_track[("dev-A", "normal")]["suspected"], 2)
        self.assertEqual(by_track[("dev-A", "screen_reader")]["suspected"], 1)
        self.assertEqual(by_track[("dev-A", "elderly")]["suspected"], 1)
        self.assertEqual(by_track[("dev-B", "normal")]["suspected"], 0)

        # 复核：确认应用侧关闭路径问题，驳回 SDK 跳转问题
        normal_report = self.get(f"/tasks/{normal['task_id']}")
        close_finding = next(
            f for f in normal_report["findings"] if f["rule_id"] == "R-CLOSE-001")
        jump_finding = next(
            f for f in normal_report["findings"] if f["rule_id"] == "R-JUMP-001")
        self.assertEqual(close_finding["status"], "suspected")  # 自动规则只标涉嫌
        self.post(f"/findings/{close_finding['finding_id']}/review", 200, {
            "decision": "confirmed", "reviewer": "复核员乙", "comment": "关闭路径不可用"})
        self.post(f"/findings/{jump_finding['finding_id']}/review", 200, {
            "decision": "dismissed", "reviewer": "复核员乙", "comment": "确有后台返回异常"})

        # 未确认不得出告知材料
        status, body = call("POST", f"{self.base}/subjects/sdk/{SDK_ID}/notices",
                            {"issued_by": "承办人甲"})
        self.assertEqual(status, 404)

        notice = self.post(f"/subjects/app/{APP_ID}/notices", 201,
                           {"issued_by": "承办人甲"})
        self.assertEqual(len(notice["findings"]), 1)
        self.assertEqual(notice["rectification_days"], 10)
        evidence_types = {e["type"] for e in notice["findings"][0]["evidence"]}
        self.assertEqual(evidence_types, {"ad_shown", "close_affordance"})
        self.assertEqual(notice["regulation_versions"], ["v2025.1"])

        # 整改：新构建复测通过，问题时段保留
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002, "version_name": "8.2.0"})
        retest_task = self.post("/tasks", 201, {
            "build_id": f"{APP_ID}:1002", "device_id": "dev-A", "track": "normal"})
        self.post(f"/tasks/{retest_task['task_id']}/events", 202,
                  {"events": [ad("c1"), close("c1", 2, after=1, size=48)]})
        self.post(f"/tasks/{retest_task['task_id']}/complete", 200, {})
        retest = self.post(f"/subjects/app/{APP_ID}/retests", 201,
                           {"task_id": retest_task["task_id"], "by": "复测员丙"})
        self.assertEqual(retest["result"], "passed")

        view = self.get(f"/subjects/app/{APP_ID}")
        self.assertEqual(view["status"], "rectified")
        self.assertEqual(view["relapse_count"], 0)
        self.assertEqual(view["cycles"][0]["problem_period"]["first_observed_at"], NOW + 100)
        self.assertEqual(view["cycles"][0]["notices"][0]["finding_count"], 1)

        # 回潮：1003 版恢复旧行为
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1003, "version_name": "8.3.0"})
        relapse_task = self.post("/tasks", 201, {
            "build_id": f"{APP_ID}:1003", "device_id": "dev-A", "track": "elderly"})
        self.post(f"/tasks/{relapse_task['task_id']}/events", 202,
                  {"events": [ad("d1"), close("d1", 2, after=6, size=40)]})
        self.post(f"/tasks/{relapse_task['task_id']}/complete", 200, {})
        relapse_report = self.get(f"/tasks/{relapse_task['task_id']}")
        relapse_finding = relapse_report["findings"][0]
        self.post(f"/findings/{relapse_finding['finding_id']}/review", 200,
                  {"decision": "confirmed", "reviewer": "复核员乙"})

        view = self.get(f"/subjects/app/{APP_ID}")
        self.assertEqual(view["status"], "open")
        self.assertEqual(view["relapse_count"], 1)
        self.assertEqual(len(view["cycles"]), 2)
        self.assertEqual(view["current_cycle_seq"], 2)
        # 旧周期完整保留，旧构建证据仍可查
        self.assertEqual(view["cycles"][0]["status"], "rectified")
        old = self.get(f"/tasks/{normal['task_id']}")
        self.assertEqual(old["build"]["version_code"], 1001)

        # 非法输入与未知路由
        status, body = call("POST", f"{self.base}/tasks",
                            {"build_id": "missing", "device_id": "dev-A", "track": "normal"})
        self.assertEqual(status, 404)
        status, _ = call("GET", f"{self.base}/unknown")
        self.assertEqual(status, 404)

    def test_snapshot_file_persistence(self):
        fd, path = tempfile.mkstemp(prefix="lab-", suffix=".json")
        os.close(fd)
        os.unlink(path)
        try:
            service.reset_store(path)
            self.post("/admin/regulations", 201,
                      {"version": "v2025.1", "effective_at": 0})
            self.post("/devices", 201,
                      {"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
            # 用同一文件重建仓储，证据不丢
            service.reset_store(path)
            status, _ = call("POST", f"{self.base}/devices",
                             {"device_id": "dev-A", "model": "Pixel 6",
                              "os_version": "Android 12"})
            self.assertEqual(status, 400)  # 设备已从快照恢复，重复登记被拒
        finally:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
