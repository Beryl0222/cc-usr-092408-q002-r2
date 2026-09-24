"""复核终态与更正规则的回归测试。

覆盖：告知前纠正、告知后纠正、完全重放（幂等）、异内容重放（409）、
并发初始复核唯一、写盘失败后的进程恢复，以及回潮周期中的更正。
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from domain import (
    AUTHORIZED_REVIEW_ROLES,
    FINDING_CONFIRMED,
    FINDING_DISMISSED,
    REVIEW_CORRECTION,
    REVIEW_INITIAL,
    REVIEW_ROLE_REVIEWER,
    REVIEW_ROLE_SUPERVISOR,
    RULE_NO_CLOSE_PATH,
    SUBJECT_APP,
    TRACK_NORMAL,
    AuthorizationError,
    ConflictError,
    DomainError,
    Lab,
    NotFoundError,
)
import service
from service import Handler

APP_ID = "com.example.news"
NOW = 1_700_000_000


class FakeClock:
    def __init__(self, start=NOW):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


def _ad(ad_id):
    return {"event_id": f"e-{ad_id}-shown", "seq": 1, "type": "ad_shown",
            "occurred_at": NOW + 100, "payload": {"ad_id": ad_id, "placement": "splash"}}


def _close(ad_id, after=5, size=36):
    return {"event_id": f"e-{ad_id}-close", "seq": 2, "type": "close_affordance",
            "occurred_at": NOW + 100 + after,
            "payload": {"ad_id": ad_id, "present": True,
                        "visible_after_seconds": after, "touch_target_dp": size,
                        "screen_reader_actionable": True, "label": "跳过"}}


def seed_lab(clock=None):
    clock = clock or FakeClock()
    lab = Lab(clock=clock)
    lab.register_regulation({"version": "v2025.1", "effective_at": 0,
                             "params": {"rectification_days": 10}})
    lab.register_device({"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
    lab.register_build({"app_id": APP_ID, "app_name": "某新闻",
                        "developer": "某新闻运营有限公司", "version_code": 1001})
    return lab, clock


def suspected_close_finding(lab):
    task = lab.create_task({"build_id": f"{APP_ID}:1001", "device_id": "dev-A",
                            "track": TRACK_NORMAL})
    lab.ingest_events(task["task_id"], [_ad("a1"), _close("a1")])
    lab.complete_task(task["task_id"])
    finding = next(f for f in lab.findings.values()
                   if f["task_id"] == task["task_id"] and f["rule_id"] == RULE_NO_CLOSE_PATH)
    return task, finding


def confirm(f, lab, reviewer="复核员乙", comment="关闭路径确实不可用"):
    return lab.review_finding(f["finding_id"],
                              {"decision": FINDING_CONFIRMED, "reviewer": reviewer,
                               "role": REVIEW_ROLE_REVIEWER, "comment": comment})


# ---------------------------------------------------------------------- #
# 领域层
# ---------------------------------------------------------------------- #

class ReviewTerminalStateTest(unittest.TestCase):
    def setUp(self):
        self.lab, self.clock = seed_lab()
        _, self.finding = suspected_close_finding(self.lab)
        self.fid = self.finding["finding_id"]

    def test_initial_review_happens_exactly_once(self):
        confirm(self.finding, self.lab)
        self.assertEqual(len(self.finding["reviews"]), 1)
        self.assertEqual(self.finding["reviews"][0]["kind"], REVIEW_INITIAL)

    def test_exact_replay_is_idempotent_and_does_not_append(self):
        payload = {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙",
                   "role": REVIEW_ROLE_REVIEWER, "comment": "关闭路径确实不可用"}
        self.lab.review_finding(self.fid, payload)
        self.clock.advance(999)
        replayed = self.lab.review_finding(self.fid, payload)
        self.assertEqual(replayed["status"], FINDING_CONFIRMED)
        self.assertEqual(replayed["reviewed_by"], "复核员乙")
        self.assertEqual(len(replayed["reviews"]), 1)          # 不新增记录
        self.assertEqual(replayed["reviews"][0]["at"], NOW)    # 原结论时间不动

    def test_different_decision_replay_conflicts_without_overwrite(self):
        confirm(self.finding, self.lab)
        with self.assertRaises(ConflictError) as ctx:
            self.lab.review_finding(self.fid,
                                    {"decision": FINDING_DISMISSED, "reviewer": "复核员乙"})
        self.assertEqual(ctx.exception.conflict_kind, "initial_review_finalized")
        self.assertEqual(ctx.exception.current["status"], FINDING_CONFIRMED)
        self.assertEqual(self.finding["status"], FINDING_CONFIRMED)  # 未原地覆盖
        self.assertEqual(self.finding["reviewed_by"], "复核员乙")
        self.assertEqual(len(self.finding["reviews"]), 1)

    def test_different_reviewer_replay_conflicts(self):
        confirm(self.finding, self.lab)
        with self.assertRaises(ConflictError):
            self.lab.review_finding(self.fid,
                                    {"decision": FINDING_CONFIRMED, "reviewer": "复核员戊"})
        self.assertEqual(self.finding["reviewed_by"], "复核员乙")
        self.assertEqual(len(self.finding["reviews"]), 1)

    def test_unauthorized_role_cannot_review_or_correct(self):
        confirm(self.finding, self.lab)
        with self.assertRaises(AuthorizationError):
            self.lab.review_finding(self.fid,
                                    {"decision": FINDING_DISMISSED, "reviewer": "承办人甲",
                                     "role": "undertaker"})
        with self.assertRaises(AuthorizationError):
            self.lab.correct_finding(self.fid,
                                     {"decision": FINDING_DISMISSED, "reviewer": "承办人甲",
                                      "role": "undertaker", "reason": "误判"})

    def test_correction_requires_prior_review_reason_and_change(self):
        # 尚无初始结论时不能更正
        with self.assertRaises(DomainError):
            self.lab.correct_finding(self.fid,
                                     {"decision": FINDING_CONFIRMED, "reviewer": "复核主管丁",
                                      "role": REVIEW_ROLE_SUPERVISOR, "reason": "x"})
        confirm(self.finding, self.lab)
        # 缺理由
        with self.assertRaises(DomainError):
            self.lab.correct_finding(self.fid,
                                     {"decision": FINDING_DISMISSED, "reviewer": "复核主管丁",
                                      "role": REVIEW_ROLE_SUPERVISOR})
        # 结论未变不得另立更正记录
        with self.assertRaises(ConflictError) as ctx:
            self.lab.correct_finding(self.fid,
                                     {"decision": FINDING_CONFIRMED, "reviewer": "复核主管丁",
                                      "role": REVIEW_ROLE_SUPERVISOR, "reason": "补充意见"})
        self.assertEqual(ctx.exception.conflict_kind, "correction_without_change")

    def test_correction_chain_carries_reason_role_and_prior_reference(self):
        confirm(self.finding, self.lab)
        corrected = self.lab.correct_finding(self.fid, {
            "decision": FINDING_DISMISSED, "reviewer": "复核主管丁",
            "role": REVIEW_ROLE_SUPERVISOR, "reason": "新证据显示关闭入口实际可点",
            "authorized_by": "复核员乙", "comment": "经现场复验改判"})
        chain = corrected["reviews"]
        self.assertEqual([r["seq"] for r in chain], [1, 2])
        self.assertEqual([r["kind"] for r in chain], [REVIEW_INITIAL, REVIEW_CORRECTION])
        self.assertEqual(chain[1]["prior_review_seq"], 1)
        self.assertEqual(chain[1]["prior_decision"], FINDING_CONFIRMED)
        self.assertEqual(chain[1]["reason"], "新证据显示关闭入口实际可点")
        self.assertEqual(chain[1]["authorized_by"], "复核员乙")
        self.assertEqual(corrected["status"], FINDING_DISMISSED)       # 当前有效结论
        self.assertEqual(corrected["reviewed_by"], "复核主管丁")
        self.assertEqual(chain[0]["decision"], FINDING_CONFIRMED)     # 历史结论原样保留

    def test_identical_correction_replay_is_idempotent(self):
        confirm(self.finding, self.lab)
        payload = {"decision": FINDING_DISMISSED, "reviewer": "复核主管丁",
                   "role": REVIEW_ROLE_SUPERVISOR, "reason": "新证据显示关闭入口实际可点"}
        self.lab.correct_finding(self.fid, payload)
        self.clock.advance(10)
        self.lab.correct_finding(self.fid, payload)
        self.assertEqual(len(self.finding["reviews"]), 2)
        self.assertEqual(self.finding["reviews"][-1]["at"], NOW)


class CorrectionBeforeNoticeTest(unittest.TestCase):
    def setUp(self):
        self.lab, self.clock = seed_lab()
        _, self.finding = suspected_close_finding(self.lab)
        self.fid = self.finding["finding_id"]
        confirm(self.finding, self.lab)

    def test_confirmed_to_dismissed_before_notice_reclaims_case(self):
        self.lab.subject_view(SUBJECT_APP, APP_ID)  # 案件存在
        self.lab.correct_finding(self.fid, {
            "decision": FINDING_DISMISSED, "reviewer": "复核主管丁",
            "role": REVIEW_ROLE_SUPERVISOR, "reason": "关闭按钮尺寸实测达标"})
        with self.assertRaises(NotFoundError):
            self.lab.subject_view(SUBJECT_APP, APP_ID)
        with self.assertRaises(NotFoundError):
            self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        # 任务报告反映当前有效结论与完整审计链
        report = self.lab.task_report(self.finding["task_id"])
        snap = report["findings"][0]
        self.assertEqual(snap["status"], FINDING_DISMISSED)
        self.assertEqual(len(snap["reviews"]), 2)

    def test_correct_back_to_confirmed_recreates_pending_notice(self):
        self.lab.correct_finding(self.fid, {
            "decision": FINDING_DISMISSED, "reviewer": "复核主管丁",
            "role": REVIEW_ROLE_SUPERVISOR, "reason": "误判，关闭按钮确实达标"})
        self.lab.correct_finding(self.fid, {
            "decision": FINDING_CONFIRMED, "reviewer": "复核员乙",
            "role": REVIEW_ROLE_REVIEWER, "reason": "复验视频确认入口不可点"})
        self.assertEqual(self.finding["status"], FINDING_CONFIRMED)
        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["status"], "open")
        self.assertEqual(view["relapse_count"], 0)
        self.assertEqual(view["cycles"][0]["effective_confirmed_count"], 1)
        self.assertEqual(view["cycles"][0]["pending_notice_count"], 1)
        notice = self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        self.assertEqual(notice["findings"][0]["finding_id"], self.fid)
        # 快照中的审计链完整（初始 + 两次更正）
        self.assertEqual(len(notice["findings"][0]["reviews"]), 3)
        self.assertEqual(notice["findings"][0]["status"], FINDING_CONFIRMED)


class CorrectionAfterNoticeTest(unittest.TestCase):
    def setUp(self):
        self.lab, self.clock = seed_lab()
        _, self.finding = suspected_close_finding(self.lab)
        self.fid = self.finding["finding_id"]
        confirm(self.finding, self.lab)
        self.notice = self.lab.generate_notice(
            SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})

    def test_post_notice_dismissal_keeps_material_but_refreshes_effective_view(self):
        self.lab.correct_finding(self.fid, {
            "decision": FINDING_DISMISSED, "reviewer": "复核主管丁",
            "role": REVIEW_ROLE_SUPERVISOR, "reason": "告知后企业提交新证据，复核改判"})
        # 历史告知快照原样保留确认结论
        stored = self.lab.notices[self.notice["notice_id"]]
        self.assertEqual(stored["findings"][0]["status"], FINDING_CONFIRMED)
        self.assertEqual(stored["rectification_deadline"], self.notice["rectification_deadline"])
        # 周期不消失，但当前有效结论与待告知项已重算
        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        cycle = view["cycles"][0]
        self.assertEqual(len(view["cycles"]), 1)
        self.assertEqual(cycle["effective_confirmed_count"], 0)
        self.assertEqual(cycle["pending_notice_count"], 0)
        annotation = cycle["notices"][0]["findings_current_status"][0]
        self.assertEqual(annotation["snapshot_status"], FINDING_CONFIRMED)
        self.assertEqual(annotation["current_status"], FINDING_DISMISSED)
        # 任务报告显示当前有效结论为驳回
        report = self.lab.task_report(self.finding["task_id"])
        self.assertEqual(report["findings"][0]["status"], FINDING_DISMISSED)
        self.assertEqual(len(report["findings"][0]["reviews"]), 2)

    def test_post_notice_dismissal_then_confirmation_restores_effective_count(self):
        self.lab.correct_finding(self.fid, {
            "decision": FINDING_DISMISSED, "reviewer": "复核主管丁",
            "role": REVIEW_ROLE_SUPERVISOR, "reason": "新证据"})
        self.lab.correct_finding(self.fid, {
            "decision": FINDING_CONFIRMED, "reviewer": "复核主管丁",
            "role": REVIEW_ROLE_SUPERVISOR, "reason": "证据不成立，恢复确认"})
        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        cycle = view["cycles"][0]
        self.assertEqual(cycle["effective_confirmed_count"], 1)
        # 已告知过的发现不再进入新告知待办，历史告知保持一份
        self.assertEqual(cycle["pending_notice_count"], 0)
        self.assertEqual(len(cycle["notices"]), 1)
        with self.assertRaises(DomainError):
            self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})


class CorrectionInRelapseCycleTest(unittest.TestCase):
    def _build_task(self, version_code, ad_id, after=5, size=36):
        self.lab.register_build({"app_id": APP_ID, "app_name": "某新闻",
                                 "developer": "某新闻运营有限公司",
                                 "version_code": version_code})
        task = self.lab.create_task(
            {"build_id": f"{APP_ID}:{version_code}", "device_id": "dev-A",
             "track": TRACK_NORMAL})
        self.lab.ingest_events(task["task_id"], [_ad(ad_id), _close(ad_id, after, size)])
        self.lab.complete_task(task["task_id"])
        rows = [f for f in self.lab.findings.values() if f["task_id"] == task["task_id"]]
        return task, (rows[0] if rows else None)

    def test_dismissing_relapsed_finding_returns_case_to_rectified(self):
        lab, clock = seed_lab()
        self.lab = lab
        self.clock = clock
        first = suspected_close_finding(lab)[1]
        confirm(first, lab)
        lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        clock.advance(86400)
        # 整改版本复测通过
        fixed, _ = self._build_task(1002, "c1", after=1, size=48)
        lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": fixed["task_id"]})
        # 回潮
        clock.advance(20 * 86400)
        _, relapsed = self._build_task(1003, "d1")
        confirm(relapsed, lab)
        view = lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["relapse_count"], 1)
        self.assertEqual(view["current_cycle_seq"], 2)

        # 回潮发现告知前被更正驳回：第二周期整体回收，案件回到已整改
        lab.correct_finding(relapsed["finding_id"], {
            "decision": FINDING_DISMISSED, "reviewer": "复核主管丁",
            "role": REVIEW_ROLE_SUPERVISOR, "reason": "回潮证据系旧包残留"})
        view = lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["status"], "rectified")
        self.assertEqual(view["relapse_count"], 0)
        self.assertEqual(len(view["cycles"]), 1)
        self.assertEqual(view["current_cycle_seq"], 1)

        # 再次更正确认：重新开启回潮新周期
        lab.correct_finding(relapsed["finding_id"], {
            "decision": FINDING_CONFIRMED, "reviewer": "复核员乙",
            "role": REVIEW_ROLE_REVIEWER, "reason": "确认 1003 版确实复现"})
        view = lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["status"], "open")
        self.assertEqual(view["relapse_count"], 1)
        self.assertEqual(view["current_cycle_seq"], 2)
        self.assertEqual(view["cycles"][1]["seq"], 2)


class SnapshotRecoveryTest(unittest.TestCase):
    def test_review_chain_persists_through_snapshot(self):
        lab, _ = seed_lab()
        _, finding = suspected_close_finding(lab)
        confirm(finding, lab)
        lab.correct_finding(finding["finding_id"], {
            "decision": FINDING_DISMISSED, "reviewer": "复核主管丁",
            "role": REVIEW_ROLE_SUPERVISOR, "reason": "改判"})
        restored = Lab.from_snapshot(lab.to_snapshot())
        row = restored.findings[finding["finding_id"]]
        self.assertEqual(row["status"], FINDING_DISMISSED)
        self.assertEqual(len(row["reviews"]), 2)
        self.assertEqual(row["reviews"][1]["prior_decision"], FINDING_CONFIRMED)
        with self.assertRaises(NotFoundError):
            restored.subject_view(SUBJECT_APP, APP_ID)  # 已回收的案件不复活

    def test_v1_snapshot_migrates_and_repairs_half_cycle(self):
        # v1 形态：发现无 reviews 链；案件里挂着一个已驳回、从未告知的发现（半个周期）
        lab, _ = seed_lab()
        _, finding = suspected_close_finding(lab)
        lab.review_finding(finding["finding_id"],
                           {"decision": FINDING_DISMISSED, "reviewer": "复核员乙"})
        raw = lab.to_snapshot()
        for row in raw["findings"].values():
            row.pop("reviews", None)
        raw.pop("snapshot_version", None)
        raw["cases"] = [{
            "key": [SUBJECT_APP, APP_ID],
            "value": {
                "case_id": "case-broken",
                "responsible_subject": finding["responsible_subject"],
                "status": "open", "relapse_count": 0,
                "cycles": [{
                    "seq": 1, "status": "open", "opened_at": NOW, "rectified_at": None,
                    "finding_ids": [finding["finding_id"]],
                    "notice_ids": [], "retests": [],
                }],
                "created_at": NOW,
            },
        }]
        restored = Lab.from_snapshot(raw)
        # 旧结论被补登为初始复核
        self.assertEqual(restored.findings[finding["finding_id"]]["reviews"][0]["kind"],
                         REVIEW_INITIAL)
        # 半个周期在恢复时被修复：案件整体回收
        with self.assertRaises(NotFoundError):
            restored.subject_view(SUBJECT_APP, APP_ID)

    def test_noticed_dismissed_finding_survives_repair(self):
        # 告知后改判驳回的发现属于合法历史，恢复后周期必须保留
        lab, _ = seed_lab()
        _, finding = suspected_close_finding(lab)
        confirm(finding, lab)
        lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        lab.correct_finding(finding["finding_id"], {
            "decision": FINDING_DISMISSED, "reviewer": "复核主管丁",
            "role": REVIEW_ROLE_SUPERVISOR, "reason": "改判"})
        restored = Lab.from_snapshot(lab.to_snapshot())
        view = restored.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(len(view["cycles"]), 1)
        self.assertEqual(view["cycles"][0]["notices"][0]["finding_count"], 1)


# ---------------------------------------------------------------------- #
# HTTP 语义与并发
# ---------------------------------------------------------------------- #

def http_call(method, url, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"} if data else {}
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), json.load(response)
    except HTTPError as exc:
        return exc.code, dict(exc.headers), json.load(exc)


class HttpReviewSemanticsTest(unittest.TestCase):
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
        self.post("/admin/regulations", 201,
                  {"version": "v2025.1", "effective_at": 0, "params": {"rectification_days": 10}})
        self.post("/devices", 201,
                  {"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
        self.post("/builds", 201, {"app_id": APP_ID, "app_name": "某新闻",
                                   "developer": "某新闻运营有限公司", "version_code": 1001})
        task = self.post("/tasks", 201,
                         {"build_id": f"{APP_ID}:1001", "device_id": "dev-A",
                          "track": TRACK_NORMAL})
        self.post(f"/tasks/{task['task_id']}/events", 202,
                  {"events": [_ad("a1"), _close("a1")]})
        self.post(f"/tasks/{task['task_id']}/complete", 200, {})
        report = self.get(f"/tasks/{task['task_id']}")
        self.fid = report["findings"][0]["finding_id"]
        self.task_id = task["task_id"]

    def post(self, path, expected, body):
        status, _, payload = http_call("POST", self.base + path, body)
        self.assertEqual(status, expected, payload)
        return payload

    def get(self, path, expected=200):
        status, _, payload = http_call("GET", self.base + path)
        self.assertEqual(status, expected, payload)
        return payload

    def test_exact_replay_returns_200_with_replay_header(self):
        body = {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙",
                "comment": "关闭路径不可用"}
        status, headers, payload = http_call(
            "POST", f"{self.base}/findings/{self.fid}/review", body)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Idempotent-Replay"), "false")
        self.assertEqual(len(payload["reviews"]), 1)
        status, headers, payload = http_call(
            "POST", f"{self.base}/findings/{self.fid}/review", body)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Idempotent-Replay"), "true")
        self.assertEqual(len(payload["reviews"]), 1)

    def test_different_replay_returns_409_with_current_chain(self):
        self.post(f"/findings/{self.fid}/review", 200,
                  {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})
        status, _, body = http_call("POST", f"{self.base}/findings/{self.fid}/review",
                                    {"decision": FINDING_DISMISSED, "reviewer": "复核员乙"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "conflict")
        self.assertEqual(body["code"], "initial_review_finalized")
        self.assertEqual(body["current"]["status"], FINDING_CONFIRMED)
        self.assertEqual(len(body["current"]["reviews"]), 1)
        # 报告仍是原结论
        report = self.get(f"/tasks/{self.task_id}")
        self.assertEqual(report["findings"][0]["status"], FINDING_CONFIRMED)

    def test_correction_endpoint_and_forbidden_role(self):
        self.post(f"/findings/{self.fid}/review", 200,
                  {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})
        status, _, body = http_call("POST", f"{self.base}/findings/{self.fid}/corrections",
                                    {"decision": FINDING_DISMISSED, "reviewer": "承办人甲",
                                     "role": "undertaker", "reason": "误判"})
        self.assertEqual((status, body["error"]), (403, "forbidden"))
        status, _, body = http_call("POST", f"{self.base}/findings/{self.fid}/corrections",
                                    {"decision": FINDING_DISMISSED, "reviewer": "复核主管丁",
                                     "role": REVIEW_ROLE_SUPERVISOR})
        self.assertEqual((status, body["error"]), (400, "domain_error"))
        corrected = self.post(f"/findings/{self.fid}/corrections", 200,
                              {"decision": FINDING_DISMISSED, "reviewer": "复核主管丁",
                               "role": REVIEW_ROLE_SUPERVISOR, "reason": "新证据达标"})
        self.assertEqual(corrected["status"], FINDING_DISMISSED)
        self.assertEqual(len(corrected["reviews"]), 2)

    def test_concurrent_initial_reviews_only_one_wins(self):
        winner = {}
        barrier = threading.Barrier(8)
        results = []
        lock = threading.Lock()

        def worker(i):
            barrier.wait()
            status, _, payload = http_call(
                "POST", f"{self.base}/findings/{self.fid}/review",
                {"decision": FINDING_CONFIRMED if i % 2 == 0 else FINDING_DISMISSED,
                 "reviewer": f"复核员{i:02d}", "comment": f"意见{i}"})
            with lock:
                results.append((status, payload))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok = [r for r in results if r[0] == 200]
        conflicts = [r for r in results if r[0] == 409]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(conflicts), 7)
        report = self.get(f"/tasks/{self.task_id}")
        finding = report["findings"][0]
        self.assertEqual(len(finding["reviews"]), 1)  # 只有一个初始结论
        self.assertEqual(finding["reviewed_by"], ok[0][1]["reviewed_by"])

    def test_failed_write_rolls_back_without_half_cycle(self):
        tmpdir = tempfile.mkdtemp(prefix="lab-recovery-")
        path = os.path.join(tmpdir, "lab.json")
        try:
            service.reset_store(path)
            self.post("/admin/regulations", 201,
                      {"version": "v2025.1", "effective_at": 0})
            self.post("/devices", 201,
                      {"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
            self.post("/builds", 201, {"app_id": APP_ID, "app_name": "某新闻",
                                       "developer": "某新闻运营有限公司", "version_code": 1001})
            task = self.post("/tasks", 201,
                             {"build_id": f"{APP_ID}:1001", "device_id": "dev-A",
                              "track": TRACK_NORMAL})
            self.post(f"/tasks/{task['task_id']}/events", 202,
                      {"events": [_ad("a1"), _close("a1")]})
            self.post(f"/tasks/{task['task_id']}/complete", 200, {})
            report = self.get(f"/tasks/{task['task_id']}")
            fid = report["findings"][0]["finding_id"]

            # 落盘失败：内存改动必须随回滚一并消失
            with mock.patch("service.os.replace", side_effect=OSError("disk on fire")):
                status, _, body = http_call(
                    "POST", f"{self.base}/findings/{fid}/review",
                    {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})
            self.assertEqual((status, body["error"]), (500, "persistence_failed"))
            report = self.get(f"/tasks/{task['task_id']}")
            self.assertEqual(report["findings"][0]["status"], "suspected")
            # 没有留下半个案件周期
            status, _, _ = http_call("GET", f"{self.base}/subjects/{SUBJECT_APP}/{APP_ID}")
            self.assertEqual(status, 404)

            # 恢复后同一结论可以正常复核并落盘
            self.post(f"/findings/{fid}/review", 200,
                      {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})
            service.reset_store(path)  # 新进程从磁盘恢复
            report = self.get(f"/tasks/{task['task_id']}")
            self.assertEqual(report["findings"][0]["status"], FINDING_CONFIRMED)
            self.assertEqual(len(report["findings"][0]["reviews"]), 1)
        finally:
            for name in os.listdir(tmpdir):
                os.unlink(os.path.join(tmpdir, name))
            os.rmdir(tmpdir)


if __name__ == "__main__":
    unittest.main()
