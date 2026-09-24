"""移动广告合规实验室的领域模型与规则引擎。

设计要点（与专项整治业务约定一一对应）：

* 证据只增不改：设备、构建、事件一经登记只能追加，新版构建与旧版并存，
  复核员不能修改或删除原始事件，只能对规则发现作确认/驳回。
* 采集幂等：上报事件带客户端事件号，重传去重；迟到事件允许补录，重新判定
  只会追加新发现，不会抹掉已有结论。
* 版本固化：任务创建时固化当时生效的规范版本与测试脚本版本，脚本升级只影响
  之后创建的任务。
* 规则与复核分离：自动规则只能产生「涉嫌」发现，复核员确认后才能生成告知材料。
* 复测与回潮：复测通过只关闭当前整改周期，此前问题时段完整保留；再次出现问题
  开启新的周期并累计回潮次数。
"""

from dataclasses import dataclass, field
from itertools import count
from time import time

# 三条标准操作轨迹
TRACK_NORMAL = "normal"            # 正常用户
TRACK_SCREEN_READER = "screen_reader"  # 读屏用户（TalkBack 等）
TRACK_ELDERLY = "elderly"          # 老人模式
TRACKS = (TRACK_NORMAL, TRACK_SCREEN_READER, TRACK_ELDERLY)
TRACK_LABELS = {
    TRACK_NORMAL: "正常用户",
    TRACK_SCREEN_READER: "读屏用户",
    TRACK_ELDERLY: "老人模式",
}

EVENT_AD_SHOWN = "ad_shown"                # 广告展示（锁屏画报/开屏弹窗/插屏等）
EVENT_CLOSE_AFFORDANCE = "close_affordance"  # 关闭入口状态
EVENT_SENSOR_READING = "sensor_reading"    # 摇一摇传感器读数
EVENT_JUMP = "jump"                        # 跳转行为
EVENT_NETWORK_RESPONSE = "network_response"  # 网络响应
EVENT_GESTURE = "gesture"                  # 用户真实操作（用于证明有无交互）
EVENT_TYPES = (
    EVENT_AD_SHOWN,
    EVENT_CLOSE_AFFORDANCE,
    EVENT_SENSOR_READING,
    EVENT_JUMP,
    EVENT_NETWORK_RESPONSE,
    EVENT_GESTURE,
)

SUBJECT_APP = "app"
SUBJECT_ADVERTISER = "advertiser"
SUBJECT_SDK = "sdk"
SUBJECT_TYPES = (SUBJECT_APP, SUBJECT_ADVERTISER, SUBJECT_SDK)
SUBJECT_LABELS = {
    SUBJECT_APP: "应用运营者",
    SUBJECT_ADVERTISER: "广告主",
    SUBJECT_SDK: "嵌入SDK",
}

FINDING_SUSPECTED = "suspected"
FINDING_CONFIRMED = "confirmed"
FINDING_DISMISSED = "dismissed"

# 复核动作类型
REVIEW_INITIAL = "initial"      # 初始复核（涉嫌发现有且仅有一次）
REVIEW_CORRECTION = "correction"  # 对既有结论的更正（只追加，不覆盖）

CASE_OPEN = "open"
CASE_RECTIFIED = "rectified"

# 更正的授权角色：只有合规复核员（及其主管）可以推翻自己或他人的结论
REVIEW_ROLE_REVIEWER = "reviewer"        # 合规复核员
REVIEW_ROLE_SUPERVISOR = "supervisor"    # 复核主管（授权更正人）
AUTHORIZED_REVIEW_ROLES = (REVIEW_ROLE_REVIEWER, REVIEW_ROLE_SUPERVISOR)

# 规则编号（对承办人员与告知材料保持稳定）
RULE_NO_CLOSE_PATH = "R-CLOSE-001"       # 不存在可操作的关闭路径
RULE_SHAKE_THRESHOLD = "R-SHAKE-001"     # 摇一摇阈值/读数时间低于规范
RULE_AUTO_JUMP = "R-JUMP-001"            # 无用户操作自动跳转

RULE_LABELS = {
    RULE_NO_CLOSE_PATH: "广告缺少可操作关闭路径",
    RULE_SHAKE_THRESHOLD: "摇一摇广告传感器阈值低于规范",
    RULE_AUTO_JUMP: "无用户操作自动跳转或误导跳转",
}

# 缺省规范参数，可由 register_regulation 按版本覆盖
DEFAULT_REGULATION_PARAMS = {
    "close_max_delay_seconds": 3.0,       # 开屏后关闭入口最迟出现时间
    "close_min_touch_target_dp": 44.0,    # 普通模式关闭入口最小可点尺寸
    "close_elderly_min_touch_target_dp": 56.0,  # 老人模式放大后的要求
    "shake_min_acceleration": 15.0,       # 触发摇一摇的最小加速度 m/s^2
    "shake_min_rotation_deg": 35.0,       # 最小旋转角度
    "shake_min_reading_seconds": 3.0,     # 最短持续读数时间
    "rectification_days": 15,             # 整改期限（自然日）
}


class DomainError(ValueError):
    """请求不符合领域约束（字段缺失、状态不允许等）。"""


class NotFoundError(LookupError):
    """引用的实体不存在。"""


class ConflictError(RuntimeError):
    """请求与资源当前终态冲突（重复复核、结论不一致等），对应 HTTP 409。

    涉嫌发现的初始复核只允许发生一次：完全相同的请求幂等返回，
    结论或复核人不同的重复请求一律冲突，不得原地覆盖。
    """

    def __init__(self, message, *, current=None, conflict_kind="review_conflict"):
        super().__init__(message)
        self.current = current
        self.conflict_kind = conflict_kind


class AuthorizationError(PermissionError):
    """角色无权执行该操作（如非授权角色要求更正结论），对应 HTTP 403。"""


def _require(payload, key, entity):
    if key not in payload or payload[key] in (None, ""):
        raise DomainError(f"{entity}缺少必填字段：{key}")
    return payload[key]


def _subject_key(subject):
    return (subject["type"], subject["id"])


def _display_subject(subject):
    return {"type": subject["type"], "id": subject["id"], "name": subject.get("name", "")}


@dataclass
class Lab:
    """合规实验室的内存仓储与全部业务操作（可整体序列化为 JSON 快照）。"""

    clock: callable = time
    _seq: count = field(default_factory=lambda: count(1), repr=False)
    regulations: dict = field(default_factory=dict)       # version -> regulation
    regulation_order: list = field(default_factory=list)
    scripts: dict = field(default_factory=dict)           # (script_id, version) -> script
    devices: dict = field(default_factory=dict)
    builds: dict = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)            # event_id -> event（全局幂等）
    findings: dict = field(default_factory=dict)
    cases: dict = field(default_factory=dict)             # case 键为责任主体
    notices: dict = field(default_factory=dict)
    # 瞬时标记：本次复核/更正是否命中完全相同请求的幂等返回（不入快照）
    _last_replay: bool = field(default=False, repr=False)

    def _new_id(self, prefix):
        return f"{prefix}-{next(self._seq):04d}"

    # ------------------------------------------------------------------ #
    # 基础档案：规范版本、脚本版本、设备、应用构建
    # ------------------------------------------------------------------ #

    def register_regulation(self, payload):
        """登记一版规范；参数缺省继承 DEFAULT_REGULATION_PARAMS。"""
        version = _require(payload, "version", "规范")
        if version in self.regulations:
            raise DomainError(f"规范版本已存在且不可覆盖：{version}")
        params = dict(DEFAULT_REGULATION_PARAMS)
        params.update(payload.get("params") or {})
        regulation = {
            "version": version,
            "title": payload.get("title", f"移动广告合规规范 {version}"),
            "effective_at": _require(payload, "effective_at", "规范"),
            "params": params,
            "registered_at": self.clock(),
        }
        self.regulations[version] = regulation
        self.regulation_order.append(version)
        return regulation

    def register_script(self, payload):
        """登记一版测试脚本；已创建的任务不会因新版本而改变。"""
        script_id = _require(payload, "script_id", "测试脚本")
        version = _require(payload, "version", "测试脚本")
        key = (script_id, version)
        if key in self.scripts:
            raise DomainError(f"测试脚本版本已存在：{script_id}@{version}")
        script = {
            "script_id": script_id,
            "version": version,
            "created_at": payload.get("created_at", self.clock()),
            "notes": payload.get("notes", ""),
        }
        self.scripts[key] = script
        return script

    def register_device(self, payload):
        device_id = _require(payload, "device_id", "设备")
        if device_id in self.devices:
            raise DomainError(f"设备已登记：{device_id}")
        device = {
            "device_id": device_id,
            "model": _require(payload, "model", "设备"),
            "os_version": _require(payload, "os_version", "设备"),
            "registered_at": self.clock(),
        }
        self.devices[device_id] = device
        return device

    def register_build(self, payload):
        """登记应用构建。开发者提交新版得到新 build_id，旧构建证据原样保留。"""
        app_id = _require(payload, "app_id", "应用")
        version_code = _require(payload, "version_code", "应用构建")
        build_id = payload.get("build_id") or f"{app_id}:{version_code}"
        if build_id in self.builds:
            raise DomainError(f"应用构建已存在，禁止覆盖旧证据：{build_id}")
        build = {
            "build_id": build_id,
            "app_id": app_id,
            "app_name": _require(payload, "app_name", "应用"),
            "developer": _require(payload, "developer", "应用"),
            "version_code": version_code,
            "version_name": payload.get("version_name", str(version_code)),
            "submitted_at": payload.get("submitted_at", self.clock()),
        }
        self.builds[build_id] = build
        return build

    def _regulation_at(self, at, explicit_version=None):
        if explicit_version is not None:
            if explicit_version not in self.regulations:
                raise NotFoundError(f"规范版本不存在：{explicit_version}")
            return self.regulations[explicit_version]
        effective = [
            r for r in self.regulations.values() if r["effective_at"] <= at
        ]
        if not effective:
            raise DomainError("尚无在该时点生效的规范版本，请显式指定 regulation_version")
        return max(effective, key=lambda r: r["effective_at"])

    def _latest_script(self, at):
        available = [s for s in self.scripts.values() if s["created_at"] <= at]
        if not available:
            return None
        return max(available, key=lambda s: (s["created_at"], s["version"]))

    # ------------------------------------------------------------------ #
    # 测试任务：同一构建在不同设备/轨迹上并存
    # ------------------------------------------------------------------ #

    def create_task(self, payload):
        build_id = _require(payload, "build_id", "任务")
        device_id = _require(payload, "device_id", "任务")
        track = payload.get("track", TRACK_NORMAL)
        if build_id not in self.builds:
            raise NotFoundError(f"应用构建不存在：{build_id}")
        if device_id not in self.devices:
            raise NotFoundError(f"设备不存在：{device_id}")
        if track not in TRACKS:
            raise DomainError(f"未知操作轨迹：{track}，允许值：{', '.join(TRACKS)}")
        now = self.clock()
        regulation = self._regulation_at(now, payload.get("regulation_version"))
        script = self._latest_script(now)
        task = {
            "task_id": self._new_id("task"),
            "build_id": build_id,
            "device_id": device_id,
            "track": track,
            "accessibility": {
                "screen_reader_enabled": track == TRACK_SCREEN_READER
                or bool(payload.get("screen_reader_enabled", False)),
                "elderly_mode_enabled": track == TRACK_ELDERLY
                or bool(payload.get("elderly_mode_enabled", False)),
            },
            "regulation_version": regulation["version"],  # 固化
            "script": None if script is None else {"script_id": script["script_id"], "version": script["version"]},
            "status": "collecting",
            "created_at": now,
            "completed_at": None,
            "assessment_count": 0,
            "event_ids": [],
        }
        self.tasks[task["task_id"]] = task
        return task

    def ingest_events(self, task_id, events):
        """幂等接收一批事件。

        同一 (task_id, event_id) 重传直接判重，不产生重复证据；
        任务完成后到达的迟到事件照常追加，并触发追加判定。
        """
        task = self._get_task(task_id)
        accepted, duplicates = [], []
        late = False
        for raw in events:
            event_id = _require(raw, "event_id", "事件")
            dedup_key = (task_id, event_id)
            if dedup_key in self.events:
                duplicates.append(event_id)
                continue
            event_type = _require(raw, "type", "事件")
            if event_type not in EVENT_TYPES:
                raise DomainError(f"未知事件类型：{event_type}")
            event = {
                "event_id": event_id,
                "task_id": task_id,
                "seq": _require(raw, "seq", "事件"),
                "type": event_type,
                "occurred_at": _require(raw, "occurred_at", "事件"),
                "received_at": self.clock(),
                "late": task["status"] == "completed",
                "payload": raw.get("payload", {}),
            }
            self.events[dedup_key] = event
            task["event_ids"].append(event_id)
            accepted.append(event_id)
            if event["late"]:
                late = True
        result = {"accepted": accepted, "duplicates": duplicates}
        if accepted and task["status"] == "completed":
            result["assessment"] = self._evaluate(task)
            result["late_arrivals"] = late
        return result

    def complete_task(self, task_id):
        task = self._get_task(task_id)
        if not task["event_ids"]:
            raise DomainError("任务尚无任何事件，不能完成")
        task["status"] = "completed"
        task["completed_at"] = self.clock()
        return self._evaluate(task)

    def _get_task(self, task_id):
        try:
            return self.tasks[task_id]
        except KeyError:
            raise NotFoundError(f"任务不存在：{task_id}")

    def _task_events(self, task):
        rows = [self.events[(task["task_id"], eid)] for eid in task["event_ids"]]
        return sorted(rows, key=lambda e: (e["occurred_at"], e["seq"]))

    # ------------------------------------------------------------------ #
    # 规则引擎：只产出「涉嫌」发现
    # ------------------------------------------------------------------ #

    def _evaluate(self, task):
        """对任务的全部事件运行规则，按指纹去重追加发现。"""
        regulation = self.regulations[task["regulation_version"]]
        params = regulation["params"]
        events = self._task_events(task)
        build = self.builds[task["build_id"]]
        app_subject = {"type": SUBJECT_APP, "id": build["app_id"], "name": build["developer"]}

        ads = [e for e in events if e["type"] == EVENT_AD_SHOWN]
        new_findings = []

        def first_existing(fingerprint):
            for f in self.findings.values():
                if f["task_id"] == task["task_id"] and f["fingerprint"] == fingerprint:
                    return f
            return None

        def add_finding(rule_id, ad_info, subject, chain, evidence, detail, occurred_at):
            fingerprint = "|".join(
                [rule_id, str(ad_info.get("ad_id", "")), str(ad_info.get("placement", "")),
                 _subject_key(subject)[0], _subject_key(subject)[1]] + sorted(evidence)
            )
            existing = first_existing(fingerprint)
            if existing:
                return existing
            finding = {
                "finding_id": self._new_id("finding"),
                "task_id": task["task_id"],
                "rule_id": rule_id,
                "rule_title": RULE_LABELS[rule_id],
                "status": FINDING_SUSPECTED,
                "responsible_subject": _display_subject(subject),
                "responsibility_chain": chain,
                "ad": ad_info,
                "evidence_event_ids": sorted(evidence),
                "detail": detail,
                "observed_at": occurred_at,
                "regulation_version": task["regulation_version"],
                "created_at": self.clock(),
                "reviewed_by": None,
                "reviewed_at": None,
                "review_comment": None,
                # 复核审计链：初始复核与历次更正都只追加，当前有效结论恒为末条
                "reviews": [],
                "fingerprint": fingerprint,
            }
            self.findings[finding["finding_id"]] = finding
            new_findings.append(finding)
            return finding

        for ad_event in ads:
            ad_payload = ad_event["payload"]
            ad_info = {
                "ad_id": ad_payload.get("ad_id", ad_event["event_id"]),
                "placement": ad_payload.get("placement", "unknown"),
                "creative_id": ad_payload.get("creative_id"),
            }
            refs = ad_info["ad_id"]
            related = [
                e for e in events
                if e["occurred_at"] >= ad_event["occurred_at"]
                and e["payload"].get("ad_id") in (refs, None)
            ]
            affs = [e for e in related if e["type"] == EVENT_CLOSE_AFFORDANCE and e["payload"].get("ad_id") == refs]
            jumps = [e for e in related if e["type"] == EVENT_JUMP and e["payload"].get("ad_id") == refs]

            chain = {"app": _display_subject(app_subject), "advertiser": None, "sdk": None}

            # 规则一：关闭路径是否存在且对当前轨迹可操作
            reasons, close_detail = self._assess_close_path(task, affs, params)
            if reasons:
                detail = {"close_path": close_detail, "violations": reasons}
                add_finding(
                    RULE_NO_CLOSE_PATH, ad_info, app_subject, chain,
                    [ad_event["event_id"]] + [e["event_id"] for e in affs],
                    detail, ad_event["occurred_at"],
                )

            # 跳转类规则：自动跳转 / 摇一摇阈值
            gestures = [e for e in related if e["type"] == EVENT_GESTURE]
            for jump in jumps:
                jp = jump["payload"]
                sdk = self._subject_from_payload(jp.get("sdk"), SUBJECT_SDK)
                advertiser = self._subject_from_payload(
                    jp.get("advertiser") or ad_payload.get("advertiser"), SUBJECT_ADVERTISER
                )
                jump_chain = {
                    "app": _display_subject(app_subject),
                    "advertiser": None if advertiser is None else _display_subject(advertiser),
                    "sdk": None if sdk is None else _display_subject(sdk),
                }
                trigger = jp.get("trigger", "auto")
                evidence = [ad_event["event_id"], jump["event_id"]]

                if trigger == "shake":
                    sensors = [
                        e for e in related
                        if e["type"] == EVENT_SENSOR_READING
                        and e["payload"].get("ad_id") == refs
                        and e["occurred_at"] <= jump["occurred_at"]
                    ]
                    violations, sensor_detail = self._assess_shake(sensors, jp, params)
                    if violations:
                        subject = sdk or advertiser or app_subject
                        detail = {
                            "jump": self._jump_summary(jump, gestures),
                            "sensor": sensor_detail,
                            "violations": violations,
                        }
                        evidence += [e["event_id"] for e in sensors]
                        add_finding(RULE_SHAKE_THRESHOLD, ad_info, subject, jump_chain,
                                    evidence, detail, jump["occurred_at"])
                elif trigger == "auto":
                    subject = sdk or advertiser or app_subject
                    detail = {"jump": self._jump_summary(jump, gestures),
                              "violations": ["跳转发生前无任何用户操作记录"]}
                    add_finding(RULE_AUTO_JUMP, ad_info, subject, jump_chain,
                                evidence, detail, jump["occurred_at"])

        task["assessment_count"] += 1
        return {
            "task_id": task["task_id"],
            "regulation_version": task["regulation_version"],
            "new_suspected_findings": [f["finding_id"] for f in new_findings],
            "suspected_total": len([
                f for f in self.findings.values()
                if f["task_id"] == task["task_id"] and f["status"] == FINDING_SUSPECTED
            ]),
        }

    @staticmethod
    def _subject_from_payload(raw, subject_type):
        if not raw or not raw.get("id"):
            return None
        return {"type": subject_type, "id": raw["id"], "name": raw.get("name", "")}

    def _assess_close_path(self, task, aff_events, params):
        """返回（违规原因列表，关闭路径快照）。"""
        detail = {
            "exists": bool(aff_events),
            "operable": False,
            "visible_after_seconds": None,
            "touch_target_dp": None,
            "screen_reader_actionable": None,
            "track": task["track"],
        }
        if not aff_events:
            return ["广告展示期间未上报任何关闭入口"], detail
        aff = min(aff_events, key=lambda e: e["occurred_at"])
        p = aff["payload"]
        detail["visible_after_seconds"] = p.get("visible_after_seconds")
        detail["touch_target_dp"] = p.get("touch_target_dp")
        detail["screen_reader_actionable"] = p.get("screen_reader_actionable")
        detail["dismiss_label"] = p.get("label")

        reasons = []
        if p.get("present") is False:
            reasons.append("关闭入口不存在")
        if p.get("visible_after_seconds") is not None \
                and p["visible_after_seconds"] > params["close_max_delay_seconds"]:
            reasons.append(
                f"关闭入口迟至 {p['visible_after_seconds']}s 出现，"
                f"超过 {params['close_max_delay_seconds']}s"
            )
        size = p.get("touch_target_dp")
        if size is not None:
            floor = (params["close_elderly_min_touch_target_dp"] if task["track"] == TRACK_ELDERLY
                     else params["close_min_touch_target_dp"])
            if size < floor:
                reasons.append(f"可点区域 {size}dp 小于 {TRACK_LABELS[task['track']]}要求的 {floor}dp")
        if task["track"] == TRACK_SCREEN_READER and not p.get("screen_reader_actionable", False):
            reasons.append("读屏模式下关闭入口不可聚焦或无操作标签")
        detail["operable"] = not reasons
        return reasons, detail

    @staticmethod
    def _assess_shake(sensor_events, jump_payload, params):
        peak_accel = jump_payload.get("peak_acceleration")
        peak_rotation = jump_payload.get("peak_rotation_deg")
        reading_seconds = jump_payload.get("reading_seconds")
        for e in sensor_events:
            p = e["payload"]
            peak_accel = p.get("peak_acceleration", peak_accel)
            peak_rotation = p.get("peak_rotation_deg", peak_rotation)
            reading_seconds = p.get("reading_seconds", reading_seconds)
        detail = {
            "peak_acceleration": peak_accel,
            "peak_rotation_deg": peak_rotation,
            "reading_seconds": reading_seconds,
            "thresholds": {
                "min_acceleration": params["shake_min_acceleration"],
                "min_rotation_deg": params["shake_min_rotation_deg"],
                "min_reading_seconds": params["shake_min_reading_seconds"],
            },
        }
        violations = []
        if peak_accel is not None and peak_accel < params["shake_min_acceleration"]:
            violations.append(f"峰值加速度 {peak_accel} m/s² 低于下限 {params['shake_min_acceleration']}")
        if peak_rotation is not None and peak_rotation < params["shake_min_rotation_deg"]:
            violations.append(f"旋转角度 {peak_rotation}° 低于下限 {params['shake_min_rotation_deg']}")
        if reading_seconds is not None and reading_seconds < params["shake_min_reading_seconds"]:
            violations.append(f"读数持续 {reading_seconds}s 短于下限 {params['shake_min_reading_seconds']}s")
        return violations, detail

    @staticmethod
    def _jump_summary(jump_event, gesture_events):
        p = jump_event["payload"]
        prior_gesture = any(g["occurred_at"] <= jump_event["occurred_at"] for g in gesture_events)
        return {
            "event_id": jump_event["event_id"],
            "at": jump_event["occurred_at"],
            "trigger": p.get("trigger"),
            "target_url": p.get("target_url"),
            "user_gesture_before_jump": prior_gesture,
            "sdk": p.get("sdk"),
            "advertiser": p.get("advertiser"),
        }

    # ------------------------------------------------------------------ #
    # 复核：初始复核唯一、更正只追加、结论变化驱动案件回算
    # ------------------------------------------------------------------ #

    def review_finding(self, finding_id, payload):
        """对涉嫌发现作唯一一次初始复核。

        * 仅 ``suspected`` 发现可以进入初始复核；初始结论一旦写定即终态，
          任何结论或复核人变化都不得原地覆盖（返回 409 冲突）。
        * 与既有结论 *完全相同* 的重复请求按幂等处理，直接返回当前结论。
        * 确需纠正既有结论时，必须走 :meth:`correct_finding` 追加更正记录。
        """
        finding = self._get_finding(finding_id)
        decision, reviewer, comment = self._validate_review_payload(payload)
        role = payload.get("role", REVIEW_ROLE_REVIEWER)
        self._last_replay = False
        if role not in AUTHORIZED_REVIEW_ROLES:
            raise AuthorizationError(
                "只有授权角色（reviewer 合规复核员 / supervisor 复核主管）"
                "可以出具复核结论")
        if not finding["reviews"]:
            record = self._build_review_record(
                finding, REVIEW_INITIAL, decision, reviewer, role, comment, reason=None)
            self._commit_review(finding, record)
            return finding
        latest = finding["reviews"][-1]
        if self._request_signature(REVIEW_INITIAL, decision, reviewer, role,
                                   comment, None, None) == self._record_signature(latest):
            self._last_replay = True
            return finding
        raise ConflictError(
            "发现已完成初始复核且结论为终态，结论或复核人变化不得原地覆盖；"
            "确需纠正请出具带理由与授权角色的更正记录",
            current=self._finding_snapshot(finding),
            conflict_kind="initial_review_finalized",
        )

    def correct_finding(self, finding_id, payload):
        """追加一条更正记录以推翻当前有效结论。

        更正必须携带：新结论、更正人、授权角色（``reviewer``/``supervisor``）
        与更正理由；记录引用被推翻的前序结论序号，形成完整审计链。
        更正提交后按「该发现是否已出具告知决定」重新计算案件状态、
        待告知项与复测资格；已出具的告知材料原样保留。
        """
        finding = self._get_finding(finding_id)
        if not finding["reviews"]:
            raise DomainError("发现尚无初始复核结论，不能更正；请先完成初始复核")
        decision, reviewer, _ = self._validate_review_payload(payload)
        role = payload.get("role")
        if role not in AUTHORIZED_REVIEW_ROLES:
            raise AuthorizationError(
                "只有授权角色（reviewer 合规复核员 / supervisor 复核主管）"
                "可以出具更正记录")
        reason = payload.get("reason")
        if not reason or not str(reason).strip():
            raise DomainError("更正记录缺少必填字段：reason（更正理由）")
        latest = finding["reviews"][-1]
        self._last_replay = False
        incoming = self._request_signature(
            REVIEW_CORRECTION, decision, reviewer, role,
            payload.get("comment", ""), reason, payload.get("authorized_by"))
        if latest["kind"] == REVIEW_CORRECTION and incoming == self._record_signature(latest):
            self._last_replay = True
            return finding
        if decision == latest["decision"]:
            raise ConflictError(
                "更正结论与当前有效结论相同；更正只能改变结论，理由差异不得另立记录",
                current=self._finding_snapshot(finding),
                conflict_kind="correction_without_change",
            )
        record = self._build_review_record(
            finding, REVIEW_CORRECTION, decision, reviewer, role,
            payload.get("comment", ""), reason=reason,
            authorized_by=payload.get("authorized_by"))
        self._commit_review(finding, record)
        return finding

    def _get_finding(self, finding_id):
        try:
            return self.findings[finding_id]
        except KeyError:
            raise NotFoundError(f"发现不存在：{finding_id}")

    @staticmethod
    def _validate_review_payload(payload):
        decision = _require(payload, "decision", "复核结论")
        if decision not in (FINDING_CONFIRMED, FINDING_DISMISSED):
            raise DomainError("复核结论只能是 confirmed 或 dismissed")
        reviewer = _require(payload, "reviewer", "复核结论")
        return decision, reviewer, payload.get("comment", "")

    @staticmethod
    def _request_signature(kind, decision, reviewer, role, comment, reason, authorized_by):
        """完全相同请求的判重签名：结论/复核人/角色/意见/理由/授权人全一致。"""
        return (
            kind, decision, reviewer, role or REVIEW_ROLE_REVIEWER,
            comment or "", reason or "", authorized_by or "",
        )

    @staticmethod
    def _record_signature(record):
        return (
            record["kind"], record["decision"], record["reviewer"],
            record.get("role", REVIEW_ROLE_REVIEWER),
            record.get("comment", ""), record.get("reason") or "",
            record.get("authorized_by") or "",
        )

    def _build_review_record(self, finding, kind, decision, reviewer, role,
                             comment, reason, authorized_by=None):
        seq = len(finding["reviews"]) + 1
        prior = finding["reviews"][-1] if finding["reviews"] else None
        return {
            "seq": seq,
            "kind": kind,
            "decision": decision,
            "reviewer": reviewer,
            "role": role or REVIEW_ROLE_REVIEWER,
            "comment": comment or "",
            "reason": reason,
            "authorized_by": authorized_by,
            "at": self.clock(),
            "prior_decision": None if prior is None else prior["decision"],
            "prior_review_seq": None if prior is None else prior["seq"],
        }

    def _commit_review(self, finding, record):
        """写入审计链并同步当前有效结论，再回算案件周期。"""
        finding["reviews"].append(record)
        finding["status"] = record["decision"]
        finding["reviewed_by"] = record["reviewer"]
        finding["reviewed_at"] = record["at"]
        finding["review_comment"] = record.get("comment") or record.get("reason") or ""
        self._sync_finding_case(finding)

    # ------------------------------------------------------------------ #
    # 整改案件：随当前有效结论回算（历史告知/复测记录原样保留）
    # ------------------------------------------------------------------ #

    def _sync_finding_case(self, finding):
        """按发现的当前有效结论挂载或移出案件周期。

        * 有效结论为 confirmed：挂入本责任主体当前开启的周期；若末周期
          已整改（回潮），开启新周期；必要时把旧周期中的同一条发现迁移
          到新周期，保证一条发现同一时刻只属于一个周期。
        * 有效结论为 dismissed 且从未出具告知：移出周期；空的、从未告知
          也无复测记录的末尾周期连同案件一并回收（回到「无案件」状态）。
        * 已出具告知后改判：发现保留在原周期（告知材料不动），仅影响
          待告知项与复测资格的计算。
        """
        key = _subject_key(finding["responsible_subject"])
        if finding["status"] == FINDING_CONFIRMED:
            case = self.cases.get(key)
            cycle = self._locate_open_cycle(case, finding) if case else None
            if case is None:
                case = self._create_case(key, finding["responsible_subject"])
                self.cases[key] = case
            if cycle is None:
                cycle = self._open_or_get_cycle(case)
            self._move_membership(finding, case, cycle)
        else:
            self._detach_from_case(key, finding)
        self._refresh_case_status(key)

    def _locate_open_cycle(self, case, finding):
        """返回发现当前应归属的开启周期；无合适周期返回 None。"""
        home_seq = finding.get("case_cycle_seq")
        if home_seq is not None:
            home = next((c for c in case["cycles"] if c["seq"] == home_seq), None)
            if home is not None and home["status"] == CASE_OPEN:
                return home
        last = case["cycles"][-1] if case["cycles"] else None
        if last is not None and last["status"] == CASE_OPEN:
            return last
        return None

    def _create_case(self, key, subject):
        return {
            "case_id": self._new_id("case"),
            "responsible_subject": _display_subject(subject),
            "status": CASE_OPEN,
            "relapse_count": 0,
            "cycles": [],
            "created_at": self.clock(),
        }

    def _open_or_get_cycle(self, case):
        """取得当前开启周期；末周期已整改则开启回潮新周期。"""
        if case["cycles"] and case["cycles"][-1]["status"] == CASE_OPEN:
            return case["cycles"][-1]
        case["cycles"].append({
            "seq": len(case["cycles"]) + 1,
            "status": CASE_OPEN,
            "opened_at": self.clock(),
            "rectified_at": None,
            "finding_ids": [],
            "notice_ids": [],
            "retests": [],
        })
        case["relapse_count"] = len(case["cycles"]) - 1
        case["status"] = CASE_OPEN
        return case["cycles"][-1]

    def _move_membership(self, finding, case, cycle):
        """把发现迁移到目标周期，并清理旧周期中的成员引用。"""
        old_seq = finding.get("case_cycle_seq")
        if old_seq is not None and old_seq != cycle["seq"]:
            old = next((c for c in case["cycles"] if c["seq"] == old_seq), None)
            if old is not None:
                old["finding_ids"] = [
                    fid for fid in old["finding_ids"] if fid != finding["finding_id"]
                ]
        if finding["finding_id"] not in cycle["finding_ids"]:
            cycle["finding_ids"].append(finding["finding_id"])
        finding["case_id"] = case["case_id"]
        finding["case_cycle_seq"] = cycle["seq"]

    def _detach_from_case(self, key, finding):
        """有效结论改判驳回时处理周期成员关系。

        已出具告知的发现：成员关系、问题时段与周期整体作为历史保留，
        只有 *当前有效结论* 改变（待告知项、复测资格按有效结论重算）。
        告知前被驳回的发现：移出周期；空白末尾周期连同案件回收。
        """
        if finding.get("notice_id"):
            return
        case = self.cases.get(key)
        if case is None:
            finding["case_id"] = None
            finding["case_cycle_seq"] = None
            return
        seq = finding.get("case_cycle_seq")
        cycle = next((c for c in case["cycles"] if c["seq"] == seq), None)
        if cycle is not None:
            cycle["finding_ids"] = [
                fid for fid in cycle["finding_ids"] if fid != finding["finding_id"]
            ]
        # 只回收从未告知、也无复测记录的空白末尾周期，避免留下半个案件周期。
        while case["cycles"]:
            tail = case["cycles"][-1]
            if tail["finding_ids"] or tail["notice_ids"] or tail["retests"]:
                break
            case["cycles"].pop()
        if not case["cycles"]:
            del self.cases[key]
        finding["case_id"] = None
        finding["case_cycle_seq"] = None

    def _refresh_case_status(self, key):
        case = self.cases.get(key)
        if case is None or not case["cycles"]:
            return
        case["relapse_count"] = len(case["cycles"]) - 1
        case["status"] = case["cycles"][-1]["status"]

    def _cycle_effective_findings(self, cycle):
        """周期内以当前有效结论计的发现（驳回改判不再计入）。"""
        return [
            self.findings[fid] for fid in cycle["finding_ids"]
            if self.findings[fid]["status"] == FINDING_CONFIRMED
        ]

    def generate_notice(self, subject_type, subject_id, payload):
        """对已确认且尚未告知的发现生成告知材料。

        待告知项按 *当前有效结论* 计算：出具前被更正驳回的发现不在周期内、
        不得进入材料；驳回后经更正确认的发现则计入。告知一旦出具，其证据
        快照即固化，事后更正不改变材料内容，只在审计链与承办视图中标注。
        """
        key = (subject_type, subject_id)
        case = self.cases.get(key)
        if case is None or not case["cycles"]:
            raise NotFoundError("该责任主体尚无整改案件")
        cycle = case["cycles"][-1]
        if cycle["status"] != CASE_OPEN:
            raise DomainError("当前整改周期已关闭，不能重复出具告知材料")
        reviewer = _require(payload, "issued_by", "告知材料")
        pending = self._pending_notice_findings(cycle)
        if not pending:
            raise DomainError("没有尚未告知的已确认发现")
        # 整改期限取各发现所依据规范中最严格（最短）的一个
        deadline_days = min(
            self.regulations[f["regulation_version"]]["params"]["rectification_days"]
            for f in pending
        )
        issued_at = self.clock()
        notice = {
            "notice_id": self._new_id("notice"),
            "case_id": case["case_id"],
            "cycle_seq": cycle["seq"],
            "responsible_subject": _display_subject(case["responsible_subject"]),
            "issued_by": reviewer,
            "issued_at": issued_at,
            "rectification_deadline": issued_at + deadline_days * 86400,
            "rectification_days": deadline_days,
            "regulation_versions": sorted({f["regulation_version"] for f in pending}),
            # 快照：出具后任何操作（含更正）都不得改动材料内容
            "findings": [self._finding_snapshot(f) for f in pending],
        }
        self.notices[notice["notice_id"]] = notice
        cycle["notice_ids"].append(notice["notice_id"])
        for f in pending:
            f["notice_id"] = notice["notice_id"]
        return notice

    def _pending_notice_findings(self, cycle):
        """周期内当前有效、且尚未出具过告知的已确认发现。"""
        return [
            f for fid in cycle["finding_ids"]
            for f in (self.findings.get(fid),)
            if f is not None
            and f["status"] == FINDING_CONFIRMED
            and not f.get("notice_id")
        ]

    def record_retest(self, subject_type, subject_id, payload):
        """登记一次复测。复测通过关闭当前周期，但此前问题时段原样保留。

        复测判断只认真实有效的当前结论：复测任务中已经更正驳回的发现不计
        失败；尚未复核的涉嫌发现同样不阻断复测通过（单独计数提示）。
        """
        key = (subject_type, subject_id)
        case = self.cases.get(key)
        if case is None or not case["cycles"]:
            raise NotFoundError("该责任主体尚无整改案件")
        cycle = case["cycles"][-1]
        if cycle["status"] != CASE_OPEN:
            raise DomainError("当前周期不在整改中")
        if not self._retest_eligible(cycle):
            raise DomainError("当前周期已无有效已确认发现且未出具告知，不具备复测事由")
        task_id = _require(payload, "task_id", "复测")
        task = self._get_task(task_id)
        if task["status"] != "completed":
            raise DomainError("复测任务必须先完成采集与判定")
        new_findings = [
            f for f in self.findings.values()
            if f["task_id"] == task_id
            and _subject_key(f["responsible_subject"]) == key
        ]
        confirmed_new = [f for f in new_findings if f["status"] == FINDING_CONFIRMED]
        retest = {
            "task_id": task_id,
            "at": self.clock(),
            "result": "passed" if not confirmed_new else "failed",
            "suspected_count": len([f for f in new_findings if f["status"] == FINDING_SUSPECTED]),
            "confirmed_count": len(confirmed_new),
            "by": payload.get("by", ""),
            "note": "复测通过仅关闭当前整改周期，历史问题时段保留在既有周期中"
                    if not confirmed_new else "复测仍发现已确认问题，整改周期保持开启",
        }
        cycle["retests"].append(retest)
        if retest["result"] == "passed":
            cycle["status"] = CASE_RECTIFIED
            cycle["rectified_at"] = retest["at"]
            case["status"] = CASE_RECTIFIED
        return retest

    def _retest_eligible(self, cycle):
        """复测资格：周期开启，且存在历史告知义务或当前有效已确认发现。"""
        if cycle["status"] != CASE_OPEN:
            return False
        if cycle["notice_ids"]:
            return True
        return bool(self._cycle_effective_findings(cycle))

    # ------------------------------------------------------------------ #
    # 查询与报告
    # ------------------------------------------------------------------ #

    def task_report(self, task_id):
        task = self._get_task(task_id)
        build = self.builds[task["build_id"]]
        device = self.devices[task["device_id"]]
        findings = [f for f in self.findings.values() if f["task_id"] == task_id]
        return {
            "task": {
                "task_id": task_id,
                "track": task["track"],
                "track_label": TRACK_LABELS[task["track"]],
                "accessibility": task["accessibility"],
                "status": task["status"],
                "created_at": task["created_at"],
                "regulation_version": task["regulation_version"],
                "script": task["script"],
            },
            "build": {k: build[k] for k in ("build_id", "app_id", "app_name", "developer",
                                            "version_code", "version_name")},
            "device": device,
            "event_count": len(task["event_ids"]),
            "findings": [self._finding_snapshot(f) for f in findings],
        }

    def build_report(self, build_id):
        """同一构建跨设备、跨轨迹的汇总，三轨迹结果并存不合并。"""
        if build_id not in self.builds:
            raise NotFoundError(f"应用构建不存在：{build_id}")
        tasks = [t for t in self.tasks.values() if t["build_id"] == build_id]
        tracks = []
        for t in sorted(tasks, key=lambda x: (x["device_id"], x["track"])):
            report = self.task_report(t["task_id"])
            tracks.append({
                "device_id": t["device_id"],
                "track": t["track"],
                "track_label": TRACK_LABELS[t["track"]],
                "status": t["status"],
                "confirmed": len([f for f in report["findings"] if f["status"] == FINDING_CONFIRMED]),
                "suspected": len([f for f in report["findings"] if f["status"] == FINDING_SUSPECTED]),
                "dismissed": len([f for f in report["findings"] if f["status"] == FINDING_DISMISSED]),
                "findings": report["findings"],
            })
        return {"build": self.builds[build_id], "tracks": tracks}

    def subject_view(self, subject_type, subject_id):
        """承办人员视图：整改期限、各周期与回潮记录。"""
        if subject_type not in SUBJECT_TYPES:
            raise DomainError(f"未知责任主体类型：{subject_type}")
        case = self.cases.get((subject_type, subject_id))
        if case is None:
            raise NotFoundError("该责任主体尚无案件")
        cycles = []
        for cycle in case["cycles"]:
            notices = [self.notices[nid] for nid in cycle["notice_ids"]]
            effective = self._cycle_effective_findings(cycle)
            cycle_findings = [self.findings[fid] for fid in cycle["finding_ids"]
                              if fid in self.findings]
            cycles.append({
                "seq": cycle["seq"],
                "status": cycle["status"],
                "opened_at": cycle["opened_at"],
                "rectified_at": cycle["rectified_at"],
                "rectification_deadline": min((n["rectification_deadline"] for n in notices), default=None),
                # 以当前有效结论计的待告知项：告知前被更正驳回的发现不再计入
                "effective_confirmed_count": len(effective),
                "pending_notice_count": len(self._pending_notice_findings(cycle)),
                "notices": [{
                    "notice_id": n["notice_id"],
                    "issued_at": n["issued_at"],
                    "rectification_deadline": n["rectification_deadline"],
                    "regulation_versions": n["regulation_versions"],
                    "finding_count": len(n["findings"]),
                    # 告知材料快照原样保留；这里仅标注各发现事后是否被更正，
                    # 不修改材料本身
                    "findings_current_status": [
                        {"finding_id": s["finding_id"],
                         "snapshot_status": s["status"],
                         "current_status": (
                             self.findings[s["finding_id"]]["status"]
                             if s["finding_id"] in self.findings else "missing")}
                        for s in n["findings"]
                    ],
                } for n in notices],
                "retests": cycle["retests"],
                "problem_period": self._problem_period(cycle),
                "findings": [self._finding_snapshot(f) for f in cycle_findings],
            })
        return {
            "case_id": case["case_id"],
            "responsible_subject": _display_subject(case["responsible_subject"]),
            "status": case["status"],
            "relapse_count": case["relapse_count"],
            "current_cycle_seq": case["cycles"][-1]["seq"],
            "cycles": cycles,
        }

    def _problem_period(self, cycle):
        """问题时段：周期内发现的实际观测时间范围（复测通过后仍保留）。"""
        findings = [self.findings[fid] for fid in cycle["finding_ids"]]
        if not findings:
            return None
        return {
            "first_observed_at": min(f["observed_at"] for f in findings),
            "last_observed_at": max(f["observed_at"] for f in findings),
        }

    def _finding_snapshot(self, finding):
        events = []
        for eid in finding["evidence_event_ids"]:
            e = self.events.get((finding["task_id"], eid))
            if e is None:
                continue
            events.append({"event_id": e["event_id"], "type": e["type"],
                           "occurred_at": e["occurred_at"], "late": e["late"],
                           "payload": e["payload"]})
        return {
            "finding_id": finding["finding_id"],
            "task_id": finding["task_id"],
            "rule_id": finding["rule_id"],
            "rule_title": finding["rule_title"],
            # 当前有效结论（审计链末条决定）；历史告知材料中的同名快照不受影响
            "status": finding["status"],
            "responsible_subject": finding["responsible_subject"],
            "responsibility_chain": finding["responsibility_chain"],
            "ad": finding["ad"],
            "detail": finding["detail"],
            "observed_at": finding["observed_at"],
            "regulation_version": finding["regulation_version"],
            "reviewed_by": finding["reviewed_by"],
            "reviewed_at": finding["reviewed_at"],
            "review_comment": finding["review_comment"],
            "notice_id": finding.get("notice_id"),
            "case_id": finding.get("case_id"),
            "case_cycle_seq": finding.get("case_cycle_seq"),
            # 完整复核审计链：初始结论 + 历次更正（含理由、授权角色、前序引用）
            "reviews": list(finding.get("reviews", [])),
            "evidence": events,
        }

    # ------------------------------------------------------------------ #
    # 持久化快照：键含元组的仓储与自增序列
    # ------------------------------------------------------------------ #

    _TUPLE_DICTS = ("scripts", "events", "cases")
    SNAPSHOT_VERSION = 2

    def to_snapshot(self):
        data = {"snapshot_version": self.SNAPSHOT_VERSION, "next_seq": next(self._seq)}
        for name in ("regulations", "regulation_order", "devices", "builds",
                     "tasks", "findings", "notices"):
            data[name] = getattr(self, name)
        for name in self._TUPLE_DICTS:
            mapping = getattr(self, name)
            data[name] = [{"key": list(key), "value": value} for key, value in mapping.items()]
        return data

    @classmethod
    def from_snapshot(cls, data, clock=time):
        lab = cls(clock=clock)
        for name in ("regulations", "regulation_order", "devices", "builds",
                     "tasks", "findings", "notices"):
            setattr(lab, name, data.get(name, {} if name != "regulation_order" else []))
        for name in cls._TUPLE_DICTS:
            setattr(lab, name, {tuple(item["key"]): item["value"] for item in data.get(name, [])})
        lab._seq = count(data.get("next_seq", 1))
        lab._migrate_findings()
        lab._repair_cases()
        return lab

    def _migrate_findings(self):
        """v1 快照（无复核审计链）迁移：把原结论补登为初始复核记录。"""
        for finding in self.findings.values():
            finding.setdefault("reviews", [])
            finding.setdefault("case_id", None)
            finding.setdefault("case_cycle_seq", None)
            finding.setdefault("notice_id", None)
            if not finding["reviews"] and finding["status"] in (
                    FINDING_CONFIRMED, FINDING_DISMISSED):
                finding["reviews"].append({
                    "seq": 1,
                    "kind": REVIEW_INITIAL,
                    "decision": finding["status"],
                    "reviewer": finding.get("reviewed_by") or "迁移补登",
                    "role": REVIEW_ROLE_REVIEWER,
                    "comment": finding.get("review_comment") or "",
                    "reason": None,
                    "authorized_by": None,
                    "at": finding.get("reviewed_at") or finding.get("created_at") or self.clock(),
                    "prior_decision": None,
                    "prior_review_seq": None,
                })

    def _repair_cases(self):
        """恢复时修复案件一致性，杜绝「半个案件周期」。

        * 周期中当前无效（已更正驳回且从未告知）的发现引用剔除；
        * 空白（无成员、无告知、无复测）的末尾周期回收；
        * 案件状态与回潮次数按现存周期重算；案件清空则整体移除。
        """
        for key in list(self.cases.keys()):
            case = self.cases[key]
            for cycle in case["cycles"]:
                kept = []
                for fid in cycle["finding_ids"]:
                    finding = self.findings.get(fid)
                    if finding is None:
                        continue
                    # 已告知后的驳回改判是合法历史（告知快照已固化），保留成员关系；
                    # 仅告知前就失去效力的发现才剔除。
                    if finding["status"] != FINDING_CONFIRMED and not finding.get("notice_id"):
                        continue
                    kept.append(fid)
                cycle["finding_ids"] = kept
            while case["cycles"]:
                tail = case["cycles"][-1]
                if tail["finding_ids"] or tail["notice_ids"] or tail["retests"]:
                    break
                case["cycles"].pop()
            if not case["cycles"]:
                del self.cases[key]
                continue
            case["relapse_count"] = len(case["cycles"]) - 1
            case["status"] = case["cycles"][-1]["status"]
