"""领域规则的端到端测试：三轨迹、双设备、幂等、版本固化、复测与回潮。"""

import unittest

from domain import (
    FINDING_CONFIRMED,
    FINDING_DISMISSED,
    FINDING_SUSPECTED,
    RULE_AUTO_JUMP,
    RULE_NO_CLOSE_PATH,
    RULE_SHAKE_THRESHOLD,
    SUBJECT_ADVERTISER,
    SUBJECT_APP,
    SUBJECT_SDK,
    TRACK_ELDERLY,
    TRACK_NORMAL,
    TRACK_SCREEN_READER,
    AuthorizationError,
    ConflictError,
    DomainError,
    Lab,
    NotFoundError,
    REVIEW_CORRECTION,
    REVIEW_INITIAL,
    REVIEWER_ROLE_REVIEWER,
    REVIEWER_ROLE_SUPERVISOR,
)

NOW = 1_700_000_000
APP_ID = "com.example.news"
SDK_ID = "shake-sdk-9"
ADVERTISER_ID = "ad-brand-x"
BUILD_V1 = f"{APP_ID}:1001"
BUILD_V2 = f"{APP_ID}:1002"
BUILD_V3 = f"{APP_ID}:1003"
DEV_A = "device-A"
DEV_B = "device-B"


class FakeClock:
    def __init__(self, start=NOW):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


def base_lab(clock):
    lab = Lab(clock=clock)
    lab.register_regulation({
        "version": "v2025.1", "effective_at": 0,
        "title": "移动互联网广告合规规范 v2025.1",
        "params": {"rectification_days": 10},
    })
    lab.register_script({"script_id": "ad-trip", "version": "1.0", "created_at": clock()})
    lab.register_device({"device_id": DEV_A, "model": "Pixel 6", "os_version": "Android 12"})
    lab.register_device({"device_id": DEV_B, "model": "Pixel 8", "os_version": "Android 14"})
    lab.register_build({
        "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
        "version_code": 1001, "version_name": "8.1.0",
    })
    return lab


def ad(ad_id, placement="splash", advertiser=None):
    payload = {"ad_id": ad_id, "placement": placement}
    if advertiser:
        payload["advertiser"] = {"id": advertiser}
    return {"event_id": f"e-{ad_id}-shown", "seq": 1, "type": "ad_shown",
            "occurred_at": NOW + 100, "payload": payload}


def close(ad_id, seq, after, size, reader=True):
    return {"event_id": f"e-{ad_id}-close-{seq}", "seq": seq, "type": "close_affordance",
            "occurred_at": NOW + 100 + after,
            "payload": {"ad_id": ad_id, "present": True, "visible_after_seconds": after,
                        "touch_target_dp": size, "screen_reader_actionable": reader,
                        "label": "跳过"}}


def jump(ad_id, seq, at, trigger, target="market://details?id=x", sdk=None,
         advertiser=None, **sensor):
    payload = {"ad_id": ad_id, "trigger": trigger, "target_url": target}
    if sdk:
        payload["sdk"] = {"id": sdk, "name": "摇一摇SDK"}
    if advertiser:
        payload["advertiser"] = {"id": advertiser}
    payload.update(sensor)
    return {"event_id": f"e-{ad_id}-jump-{seq}", "seq": seq, "type": "jump",
            "occurred_at": at, "payload": payload}


def sensor(ad_id, seq, at, accel, rotation, seconds):
    return {"event_id": f"e-{ad_id}-sensor-{seq}", "seq": seq, "type": "sensor_reading",
            "occurred_at": at,
            "payload": {"ad_id": ad_id, "peak_acceleration": accel,
                        "peak_rotation_deg": rotation, "reading_seconds": seconds}}


def gesture(seq, at):
    return {"event_id": f"e-gesture-{seq}", "seq": seq, "type": "gesture",
            "occurred_at": at, "payload": {"kind": "tap"}}


def network(ad_id, seq, at, status, url):
    return {"event_id": f"e-{ad_id}-net-{seq}", "seq": seq, "type": "network_response",
            "occurred_at": at,
            "payload": {"ad_id": ad_id, "http_status": status, "url": url}}


class DomainFlowTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.lab = base_lab(self.clock)

    def _task(self, device, track):
        return self.lab.create_task({"build_id": BUILD_V1, "device_id": device, "track": track})

    # ---------------------------------------------------------------- #

    def test_three_tracks_on_same_build_coexist_and_pin_versions(self):
        t_normal = self._task(DEV_A, TRACK_NORMAL)
        t_reader = self._task(DEV_A, TRACK_SCREEN_READER)
        t_elder = self._task(DEV_A, TRACK_ELDERLY)
        for task in (t_normal, t_reader, t_elder):
            self.assertEqual(task["regulation_version"], "v2025.1")
            self.assertEqual(task["script"], {"script_id": "ad-trip", "version": "1.0"})
        self.assertTrue(t_reader["accessibility"]["screen_reader_enabled"])
        self.assertTrue(t_elder["accessibility"]["elderly_mode_enabled"])
        self.assertNotEqual(t_normal["task_id"], t_reader["task_id"])

        # 同一构建在另一台设备上的轨迹并存，不覆盖设备 A 的结论
        t_b = self._task(DEV_B, TRACK_NORMAL)
        report = self.lab.build_report(BUILD_V1)
        keys = {(row["device_id"], row["track"]) for row in report["tracks"]}
        self.assertEqual(keys, {
            (DEV_A, TRACK_NORMAL), (DEV_A, TRACK_SCREEN_READER),
            (DEV_A, TRACK_ELDERLY), (DEV_B, TRACK_NORMAL),
        })
        self.assertEqual(t_b["device_id"], DEV_B)

    def test_normal_track_flags_late_close_and_auto_jump_blames_sdk(self):
        task = self._task(DEV_A, TRACK_NORMAL)
        self.lab.ingest_events(task["task_id"], [
            ad("a1"),
            close("a1", 2, after=5, size=36),          # 出现太晚且点区过小
            jump("a1", 3, NOW + 106, "auto", sdk=SDK_ID,
                 target="https://shop.example/promo"),
            network("a1", 4, NOW + 106, 302, "https://shop.example/promo"),
        ])
        result = self.lab.complete_task(task["task_id"])
        self.assertEqual(set(result["new_suspected_findings"]),
                         {self._finding(task, RULE_NO_CLOSE_PATH)["finding_id"],
                          self._finding(task, RULE_AUTO_JUMP)["finding_id"]})

        close_finding = self._finding(task, RULE_NO_CLOSE_PATH)
        self.assertEqual(close_finding["status"], FINDING_SUSPECTED)  # 规则只标涉嫌
        self.assertEqual(close_finding["responsible_subject"]["type"], SUBJECT_APP)
        self.assertFalse(close_finding["detail"]["close_path"]["operable"])
        self.assertEqual(len(close_finding["detail"]["violations"]), 2)

        jump_finding = self._finding(task, RULE_AUTO_JUMP)
        self.assertEqual(jump_finding["responsible_subject"]["type"], SUBJECT_SDK)
        self.assertEqual(jump_finding["responsible_subject"]["id"], SDK_ID)
        # 责任链同时保留应用与 SDK，广告主缺失时为空
        self.assertEqual(jump_finding["responsibility_chain"]["app"]["id"], APP_ID)
        self.assertEqual(jump_finding["responsibility_chain"]["sdk"]["id"], SDK_ID)
        self.assertIsNone(jump_finding["responsibility_chain"]["advertiser"])
        self.assertEqual(jump_finding["detail"]["jump"]["target_url"],
                         "https://shop.example/promo")

    def test_screen_reader_track_blames_shake_sdk_on_low_threshold(self):
        task = self._task(DEV_A, TRACK_SCREEN_READER)
        self.lab.ingest_events(task["task_id"], [
            ad("a2"),
            close("a2", 2, after=1, size=48, reader=False),  # 读屏不可聚焦
            sensor("a2", 3, NOW + 104, accel=8, rotation=10, seconds=1),
            jump("a2", 4, NOW + 105, "shake", sdk=SDK_ID,
                 advertiser=ADVERTISER_ID, target="https://shop.example/p"),
        ])
        self.lab.complete_task(task["task_id"])
        shake = self._finding(task, RULE_SHAKE_THRESHOLD)
        self.assertEqual(shake["responsible_subject"]["type"], SUBJECT_SDK)
        self.assertEqual(shake["responsibility_chain"]["advertiser"]["id"], ADVERTISER_ID)
        self.assertEqual(len(shake["detail"]["violations"]), 3)
        close_finding = self._finding(task, RULE_NO_CLOSE_PATH)
        self.assertTrue(
            any("读屏" in v for v in close_finding["detail"]["violations"])
        )

    def test_elderly_track_requires_larger_close_target_and_blames_advertiser(self):
        task = self._task(DEV_A, TRACK_ELDERLY)
        self.lab.ingest_events(task["task_id"], [
            ad("a3", advertiser=ADVERTISER_ID),
            close("a3", 2, after=1, size=48),             # 老人模式需 >=56dp
            jump("a3", 3, NOW + 103, "auto",
                 advertiser=ADVERTISER_ID, target="https://brand.example/"),
        ])
        self.lab.complete_task(task["task_id"])
        close_finding = self._finding(task, RULE_NO_CLOSE_PATH)
        self.assertTrue(any("56" in v for v in close_finding["detail"]["violations"]))
        auto = self._finding(task, RULE_AUTO_JUMP)
        # 无 SDK 信息时自动跳转归广告主，应用仍在责任链中
        self.assertEqual(auto["responsible_subject"]["type"], SUBJECT_ADVERTISER)
        self.assertEqual(auto["responsibility_chain"]["app"]["id"], APP_ID)

    def test_compliant_run_on_second_device_has_no_findings(self):
        task = self._task(DEV_B, TRACK_NORMAL)
        self.lab.ingest_events(task["task_id"], [
            ad("b1"),
            close("b1", 2, after=1, size=48),
            gesture(3, NOW + 104),
            sensor("b1", 4, NOW + 105, accel=22, rotation=45, seconds=4),
            jump("b1", 5, NOW + 106, "shake", target="https://ok.example/"),
        ])
        result = self.lab.complete_task(task["task_id"])
        self.assertEqual(result["new_suspected_findings"], [])

    def test_retransmission_is_deduplicated_and_reassessment_does_not_duplicate(self):
        task = self._task(DEV_A, TRACK_NORMAL)
        batch = [ad("a1"), close("a1", 2, after=5, size=36)]
        first = self.lab.ingest_events(task["task_id"], batch)
        self.assertEqual(first["accepted"], ["e-a1-shown", "e-a1-close-2"])
        again = self.lab.ingest_events(task["task_id"], batch)  # 重传
        self.assertEqual(again["duplicates"], ["e-a1-shown", "e-a1-close-2"])
        self.assertEqual(again["accepted"], [])
        self.lab.complete_task(task["task_id"])
        # 补传：重复事件判重 + 一条新证据触发复判，指纹去重保证发现不翻倍
        recheck = self.lab.ingest_events(task["task_id"], batch + [
            network("a1", 9, NOW + 107, 200, "https://ad.example/impression"),
        ])
        self.assertEqual(recheck["duplicates"], ["e-a1-shown", "e-a1-close-2"])
        self.assertEqual(recheck["accepted"], ["e-a1-net-9"])
        self.assertEqual(recheck["assessment"]["new_suspected_findings"], [])

    def test_late_events_after_completion_are_appended_not_backfilled(self):
        task = self._task(DEV_B, TRACK_NORMAL)
        self.lab.ingest_events(task["task_id"], [ad("b1"), close("b1", 2, after=1, size=48)])
        self.lab.complete_task(task["task_id"])
        late = self.lab.ingest_events(task["task_id"], [
            jump("b1", 9, NOW + 300, "auto", sdk=SDK_ID, target="https://x.example/"),
        ])
        self.assertEqual(late["accepted"], ["e-b1-jump-9"])
        self.assertTrue(late["late_arrivals"])
        finding = self.lab.findings[late["assessment"]["new_suspected_findings"][0]]
        self.assertEqual(finding["rule_id"], RULE_AUTO_JUMP)
        evidence = self.lab.events[(task["task_id"], "e-b1-jump-9")]
        self.assertTrue(evidence["late"])

    def test_only_confirmed_findings_can_enter_notice(self):
        task = self._task(DEV_A, TRACK_NORMAL)
        self.lab.ingest_events(task["task_id"], [
            ad("a1"), close("a1", 2, after=5, size=36),
            jump("a1", 3, NOW + 106, "auto", sdk=SDK_ID),
        ])
        self.lab.complete_task(task["task_id"])
        # 没有任何确认发现时不能出告知材料（案件尚不存在）
        with self.assertRaises(NotFoundError):
            self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})

        close_id = self._finding(task, RULE_NO_CLOSE_PATH)["finding_id"]
        jump_id = self._finding(task, RULE_AUTO_JUMP)["finding_id"]
        self.lab.review_finding(close_id, {"decision": FINDING_CONFIRMED,
                                           "reviewer": "复核员乙", "comment": "关闭路径确实不可用"})
        notice = self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        self.assertEqual(len(notice["findings"]), 1)            # 涉嫌的跳转发现不在材料中
        self.assertEqual(notice["findings"][0]["finding_id"], close_id)
        self.assertEqual(notice["rectification_days"], 10)
        self.assertEqual(notice["rectification_deadline"], self.clock() + 10 * 86400)
        self.assertEqual(notice["regulation_versions"], ["v2025.1"])

        # 没有新增已确认发现时不得重复出具
        with self.assertRaises(DomainError):
            self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})

        # 驳回的发现不进案件
        self.lab.review_finding(jump_id, {"decision": FINDING_DISMISSED, "reviewer": "复核员乙"})
        self.assertEqual(self.lab.findings[jump_id]["status"], FINDING_DISMISSED)

    def _confirmed_app_case(self):
        task = self._task(DEV_A, TRACK_NORMAL)
        self.lab.ingest_events(task["task_id"], [
            ad("a1"), close("a1", 2, after=5, size=36),
        ])
        self.lab.complete_task(task["task_id"])
        finding = self._finding(task, RULE_NO_CLOSE_PATH)
        self.lab.review_finding(finding["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})
        self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        return task, finding

    def test_retest_pass_closes_cycle_but_keeps_problem_period(self):
        task, finding = self._confirmed_app_case()
        self.clock.advance(3 * 86400)
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002, "version_name": "8.2.0",
        })
        retest_task = self.lab.create_task(
            {"build_id": BUILD_V2, "device_id": DEV_A, "track": TRACK_NORMAL})
        self.lab.ingest_events(retest_task["task_id"], [
            ad("c1"), close("c1", 2, after=1, size=48),
        ])
        self.lab.complete_task(retest_task["task_id"])
        retest = self.lab.record_retest(SUBJECT_APP, APP_ID,
                                        {"task_id": retest_task["task_id"], "by": "复测员丙"})
        self.assertEqual(retest["result"], "passed")

        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["status"], "rectified")
        self.assertEqual(view["relapse_count"], 0)
        cycle = view["cycles"][0]
        self.assertEqual(cycle["status"], "rectified")
        # 问题时段保留
        self.assertEqual(cycle["problem_period"]["first_observed_at"], finding["observed_at"])
        # 旧构建与旧证据未被新版覆盖
        self.assertIn(BUILD_V1, self.lab.builds)
        old_report = self.lab.task_report(task["task_id"])
        self.assertEqual(old_report["build"]["version_code"], 1001)
        self.assertEqual(old_report["findings"][0]["status"], FINDING_CONFIRMED)

    def test_relapse_opens_new_cycle_and_accumulates_history(self):
        self._confirmed_app_case()
        self.clock.advance(3 * 86400)
        # 第一次整改：新版复测通过
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002, "version_name": "8.2.0"})
        fixed = self.lab.create_task(
            {"build_id": BUILD_V2, "device_id": DEV_A, "track": TRACK_NORMAL})
        self.lab.ingest_events(fixed["task_id"], [ad("c1"), close("c1", 2, after=1, size=48)])
        self.lab.complete_task(fixed["task_id"])
        self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": fixed["task_id"]})

        # 回潮：又一版构建恢复旧行为
        self.clock.advance(20 * 86400)
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1003, "version_name": "8.3.0"})
        relapsed = self.lab.create_task(
            {"build_id": BUILD_V3, "device_id": DEV_A, "track": TRACK_ELDERLY})
        self.lab.ingest_events(relapsed["task_id"], [ad("d1"), close("d1", 2, after=6, size=40)])
        self.lab.complete_task(relapsed["task_id"])
        relapse_finding = self._finding(relapsed, RULE_NO_CLOSE_PATH)
        self.lab.review_finding(relapse_finding["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})

        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["status"], "open")
        self.assertEqual(view["relapse_count"], 1)
        self.assertEqual(len(view["cycles"]), 2)
        self.assertEqual(view["current_cycle_seq"], 2)
        # 第一周期仍保留完整的问题时段与告知记录
        self.assertEqual(view["cycles"][0]["status"], "rectified")
        self.assertEqual(view["cycles"][0]["problem_period"]["last_observed_at"],
                         NOW + 100)
        self.assertEqual(view["cycles"][1]["problem_period"]["first_observed_at"],
                         NOW + 100)

        # 复测失败时周期保持开启
        failed_retest_task = self.lab.create_task(
            {"build_id": BUILD_V3, "device_id": DEV_B, "track": TRACK_NORMAL})
        self.lab.ingest_events(failed_retest_task["task_id"],
                               [ad("d2"), close("d2", 2, after=8, size=30)])
        self.lab.complete_task(failed_retest_task["task_id"])
        bad = self._finding(failed_retest_task, RULE_NO_CLOSE_PATH)
        self.lab.review_finding(bad["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})
        retest = self.lab.record_retest(SUBJECT_APP, APP_ID,
                                        {"task_id": failed_retest_task["task_id"]})
        self.assertEqual(retest["result"], "failed")
        self.assertEqual(self.lab.subject_view(SUBJECT_APP, APP_ID)["status"], "open")

        # 再次整改通过
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1004, "version_name": "8.3.1"})
        ok_task = self.lab.create_task(
            {"build_id": f"{APP_ID}:1004", "device_id": DEV_A, "track": TRACK_ELDERLY})
        self.lab.ingest_events(ok_task["task_id"], [ad("e1"), close("e1", 2, after=1, size=60)])
        self.lab.complete_task(ok_task["task_id"])
        self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": ok_task["task_id"]})
        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["status"], "rectified")
        self.assertEqual(len(view["cycles"][1]["retests"]), 2)  # 失败记录也保留

    def test_script_and_regulation_updates_only_affect_new_tasks(self):
        old_task = self._task(DEV_A, TRACK_NORMAL)
        self.clock.advance(86400)
        self.lab.register_regulation({
            "version": "v2026.1", "effective_at": self.clock(),
            "params": {"close_max_delay_seconds": 1.0, "rectification_days": 5},
        })
        self.lab.register_script({"script_id": "ad-trip", "version": "2.0",
                                  "created_at": self.clock()})
        new_task = self._task(DEV_A, TRACK_NORMAL)
        self.assertEqual(old_task["regulation_version"], "v2025.1")
        self.assertEqual(old_task["script"]["version"], "1.0")
        self.assertEqual(new_task["regulation_version"], "v2026.1")
        self.assertEqual(new_task["script"]["version"], "2.0")
        # 旧任务上的判定仍按 v1（3 秒内出现即合规），新任务按 v2（1 秒）
        self.lab.ingest_events(old_task["task_id"], [ad("o1"), close("o1", 2, after=2, size=48)])
        self.lab.complete_task(old_task["task_id"])
        self.assertIsNone(self._finding(old_task, RULE_NO_CLOSE_PATH))
        self.lab.ingest_events(new_task["task_id"], [ad("n1"), close("n1", 2, after=2, size=48)])
        self.lab.complete_task(new_task["task_id"])
        self.assertIsNotNone(self._finding(new_task, RULE_NO_CLOSE_PATH))

    def test_registrations_are_append_only(self):
        with self.assertRaises(DomainError):
            self.lab.register_regulation({"version": "v2025.1", "effective_at": 0})
        with self.assertRaises(DomainError):
            self.lab.register_build({
                "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
                "version_code": 1001})

    def test_snapshot_roundtrip_preserves_evidence_and_id_sequence(self):
        task, _ = self._confirmed_app_case()
        data = self.lab.to_snapshot()
        restored = Lab.from_snapshot(data, clock=self.clock)
        report = restored.task_report(task["task_id"])
        self.assertEqual(report["findings"][0]["status"], FINDING_CONFIRMED)
        view = restored.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["cycles"][0]["notices"][0]["finding_count"], 1)
        new_device = restored.register_device(
            {"device_id": "device-C", "model": "Pixel 10", "os_version": "Android 15"})
        self.assertTrue(new_device["device_id"])  # ID 序列不与既有编号冲突

    def _finding(self, task, rule_id):
        rows = [f for f in self.lab.findings.values()
                if f["task_id"] == task["task_id"] and f["rule_id"] == rule_id]
        return rows[0] if rows else None


class ReviewTerminalAndCorrectionTest(unittest.TestCase):
    """复核终态、幂等重放与追加更正的回归测试。"""

    def setUp(self):
        self.clock = FakeClock()
        self.lab = base_lab(self.clock)

    def _suspected_finding(self, ad_id="a1", device=DEV_A, track=TRACK_NORMAL):
        """跑一条必出 R-CLOSE-001 涉嫌发现的任务，返回 finding。"""
        task = self.lab.create_task(
            {"build_id": BUILD_V1, "device_id": device, "track": track})
        self.lab.ingest_events(task["task_id"], [
            ad(ad_id), close(ad_id, 2, after=5, size=36),
        ])
        self.lab.complete_task(task["task_id"])
        return next(
            f for f in self.lab.findings.values() if f["task_id"] == task["task_id"])

    def _confirm(self, finding, reviewer="复核员乙", comment="关闭路径确实不可用"):
        return self.lab.review_finding(finding["finding_id"], {
            "decision": FINDING_CONFIRMED, "reviewer": reviewer, "comment": comment})

    def _dismiss(self, finding, reviewer="复核员乙", comment="误报"):
        return self.lab.review_finding(finding["finding_id"], {
            "decision": FINDING_DISMISSED, "reviewer": reviewer, "comment": comment})

    def _correct(self, finding, decision, reason="补充证据表明原结论有误",
                 reviewer="监督负责人丁", role=REVIEWER_ROLE_SUPERVISOR, **extra):
        payload = {"decision": decision, "reviewer": reviewer,
                   "reviewer_role": role, "reason": reason}
        payload.update(extra)
        return self.lab.correct_finding(finding["finding_id"], payload)

    # ---------------------------------------------------------------- #

    def test_initial_review_happens_once_and_exact_replay_is_idempotent(self):
        finding = self._suspected_finding()
        result = self._confirm(finding)
        self.assertFalse(result["idempotent"])
        self.assertEqual(finding["status"], FINDING_CONFIRMED)
        self.assertEqual(len(self.lab.cases[("app", APP_ID)]["cycles"]), 1)

        # 完全相同的请求重放：幂等返回，不产生第二条结论、不重复挂周期
        replay = self._confirm(finding)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["replay_of"], result["review"]["review_id"])
        self.assertEqual(len(finding["reviews"]), 1)
        self.assertEqual(len(self.lab.cases[("app", APP_ID)]["cycles"]), 1)

    def test_different_content_replay_conflicts_instead_of_overwriting(self):
        finding = self._suspected_finding()
        first = self._confirm(finding)
        # 另一名复核员想从原入口反向改判：冲突，不得原地覆盖
        with self.assertRaises(ConflictError):
            self.lab.review_finding(finding["finding_id"],
                                    {"decision": FINDING_DISMISSED, "reviewer": "复核员戊"})
        # 同结论但换人/换意见同样冲突
        with self.assertRaises(ConflictError):
            self.lab.review_finding(finding["finding_id"],
                                    {"decision": FINDING_CONFIRMED, "reviewer": "复核员戊"})
        self.assertEqual(finding["status"], FINDING_CONFIRMED)
        self.assertEqual(finding["reviewed_by"], "复核员乙")
        self.assertEqual(len(finding["reviews"]), 1)
        self.assertIsNone(first["review"]["superseded_by"])

    def test_correction_requires_reason_and_authorized_role(self):
        finding = self._suspected_finding()
        self._confirm(finding)
        # 缺理由
        with self.assertRaises(DomainError):
            self.lab.correct_finding(finding["finding_id"], {
                "decision": FINDING_DISMISSED, "reviewer": "监督负责人丁",
                "reviewer_role": REVIEWER_ROLE_SUPERVISOR})
        # 普通复核员无权更正
        with self.assertRaises(AuthorizationError):
            self._correct(finding, FINDING_DISMISSED, role=REVIEWER_ROLE_REVIEWER)
        # 未初始复核不得更正
        fresh = self._suspected_finding("a9")
        with self.assertRaises(ConflictError):
            self._correct(fresh, FINDING_DISMISSED)
        self.assertEqual(fresh["status"], FINDING_SUSPECTED)

    def test_correction_before_notice_confirm_to_dismiss_removes_case(self):
        finding = self._suspected_finding()
        self._confirm(finding)
        self.assertIn(("app", APP_ID), self.lab.cases)

        result = self._correct(finding, FINDING_DISMISSED, reason="新证据证明入口可达")
        correction = result["review"]
        self.assertEqual(correction["kind"], REVIEW_CORRECTION)
        self.assertEqual(correction["prior_decision"], FINDING_CONFIRMED)
        self.assertEqual(correction["supersedes"], finding["reviews"][0]["review_id"])
        self.assertFalse(correction["notice_issued"])
        # 当前有效结论为驳回，待告知项消失，案件被整体摘除
        self.assertEqual(finding["status"], FINDING_DISMISSED)
        self.assertNotIn(("app", APP_ID), self.lab.cases)
        with self.assertRaises(NotFoundError):
            self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        # 初始结论仍在审计链中且标记被取代
        self.assertEqual(len(finding["reviews"]), 2)
        self.assertEqual(finding["reviews"][0]["superseded_by"], correction["review_id"])

    def test_correction_before_notice_dismiss_to_confirm_creates_case(self):
        finding = self._suspected_finding()
        self._dismiss(finding)
        self.assertNotIn(("app", APP_ID), self.lab.cases)
        self._correct(finding, FINDING_CONFIRMED, reason="补查确认关闭路径确不可用")
        self.assertEqual(finding["status"], FINDING_CONFIRMED)
        notice = self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        self.assertEqual(notice["findings"][0]["finding_id"], finding["finding_id"])

    def test_correction_after_notice_keeps_material_but_recomputes_state(self):
        finding = self._suspected_finding()
        self._confirm(finding)
        notice = self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        self.assertEqual(notice["findings"][0]["status"], FINDING_CONFIRMED)

        self._correct(finding, FINDING_DISMISSED, reason="告知后核验发现判定依据有误")
        # 历史告知快照保持原样：仍写着「确认」
        stored_notice = self.lab.notices[notice["notice_id"]]
        self.assertEqual(stored_notice["findings"][0]["status"], FINDING_CONFIRMED)
        self.assertEqual(len(stored_notice["findings"][0]["reviews"]), 1)

        # 当前任务报告显示驳回；周期不再含该发现，待告知项为空
        report = self.lab.task_report(finding["task_id"])
        row = report["findings"][0]
        self.assertEqual(row["status"], FINDING_DISMISSED)
        self.assertEqual(len(row["reviews"]), 2)
        with self.assertRaises(DomainError):
            self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        # 无有效确认发现时复测失去依据
        with self.assertRaises(DomainError):
            self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": finding["task_id"]})

        # 责任主体视图保留周期、告知与更正审计链
        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(len(view["cycles"]), 1)
        cycle = view["cycles"][0]
        self.assertEqual(cycle["findings"], [])
        self.assertIsNone(cycle["problem_period"])
        self.assertEqual(cycle["notices"][0]["finding_count"], 1)
        self.assertEqual(len(cycle["corrections"]), 1)
        cview = cycle["corrections"][0]
        self.assertEqual(cview["from_decision"], FINDING_CONFIRMED)
        self.assertEqual(cview["to_decision"], FINDING_DISMISSED)
        self.assertEqual(cview["notice_id"], notice["notice_id"])
        self.assertTrue(cview["notice_issued"])

    def test_correction_replay_rules(self):
        finding = self._suspected_finding()
        self._confirm(finding)
        first = self._correct(finding, FINDING_DISMISSED, reason="依据有误")
        # 完全相同的更正重放：幂等
        replay = self._correct(finding, FINDING_DISMISSED, reason="依据有误")
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["replay_of"], first["review"]["review_id"])
        self.assertEqual(len(finding["reviews"]), 2)
        # 同结论再更正属于冲突（结论与当前有效结论一致）
        with self.assertRaises(ConflictError):
            self._correct(finding, FINDING_DISMISSED, reason="另一个理由")

    def test_correction_in_relapse_cycle_recomputes_retest_and_keeps_history(self):
        # 第一周期：确认 + 告知 + 复测通过
        first = self._suspected_finding("a1")
        self._confirm(first)
        self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        self.clock.advance(3 * 86400)
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002, "version_name": "8.2.0"})
        fixed = self.lab.create_task(
            {"build_id": BUILD_V2, "device_id": DEV_A, "track": TRACK_NORMAL})
        self.lab.ingest_events(fixed["task_id"], [ad("c1"), close("c1", 2, 1, 48)])
        self.lab.complete_task(fixed["task_id"])
        self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": fixed["task_id"]})

        # 回潮第二周期：新发现确认，复测失败（复测任务中又有确认发现）
        self.clock.advance(20 * 86400)
        relapsed = self._suspected_finding("d1", track=TRACK_ELDERLY)
        self._confirm(relapsed)
        bad = self._suspected_finding("d2", device=DEV_B, track=TRACK_NORMAL)
        self._confirm(bad)
        retest = self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": bad["task_id"]})
        self.assertEqual(retest["result"], "failed")

        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["relapse_count"], 1)
        self.assertEqual(view["cycles"][1]["status"], "open")

        # 告知后更正：复测依据发现被驳回 → 复测自动改判通过，周期关闭
        self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        self._correct(bad, FINDING_DISMISSED, reason="回潮证据设备串号，不成立")
        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        cycle2 = view["cycles"][1]
        self.assertEqual(cycle2["status"], "rectified")
        self.assertEqual(cycle2["retests"][-1]["result"], "passed")
        self.assertEqual(cycle2["retests"][-1]["confirmed_count"], 0)
        # 回潮次数与第一周期历史不变
        self.assertEqual(view["relapse_count"], 1)
        self.assertEqual(view["cycles"][0]["status"], "rectified")
        # 更正链挂在回潮周期下
        self.assertEqual(len(cycle2["corrections"]), 1)
        self.assertEqual(cycle2["corrections"][0]["finding_id"], bad["finding_id"])

    def test_snapshot_preserves_review_chain_and_corrections(self):
        finding = self._suspected_finding()
        self._confirm(finding)
        notice = self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        self._correct(finding, FINDING_DISMISSED, reason="依据有误")
        data = self.lab.to_snapshot()
        restored = Lab.from_snapshot(data, clock=self.clock)
        row = restored.findings[finding["finding_id"]]
        self.assertEqual(row["status"], FINDING_DISMISSED)
        self.assertEqual(len(row["reviews"]), 2)
        self.assertEqual(len(restored.corrections), 1)
        # 历史告知材料仍为确认
        self.assertEqual(restored.notices[notice["notice_id"]]["findings"][0]["status"],
                         FINDING_CONFIRMED)
        view = restored.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["cycles"][0]["corrections"][0]["to_decision"],
                         FINDING_DISMISSED)


if __name__ == "__main__":
    unittest.main()
