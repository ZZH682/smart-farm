
# ai_assistant.py - AI 智能农业助手（规则引擎，可结合实时数据回答农事问题）

import json
import logging
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

from config import (
    THRESHOLDS,
    LLM_API_BASE, LLM_API_KEY, LLM_MODEL, LLM_TIMEOUT,
    DEVICE_NAMES,
)
try:
    from config import LLM_MAX_TOKENS, LLM_TEMPERATURE
except ImportError:
    LLM_MAX_TOKENS = 800   # 未配置时使用默认值
    LLM_TEMPERATURE = 0.7

logger = logging.getLogger("SmartFarm.AI")

# 传感器参数中文名
PARAM_NAMES = {
    "temperature": "温度",
    "humidity": "湿度",
    "light": "光照强度",
    "soil_moisture": "土壤湿度",
    "soil_ph": "土壤pH值",
    "co2": "CO2浓度",
}

# 传感器参数单位
PARAM_UNITS = {
    "temperature": "°C",
    "humidity": "%",
    "light": "lux",
    "soil_moisture": "%",
    "soil_ph": "",
    "co2": "ppm",
}

# 设备中文名（统一从 config 导入，避免多份定义）
# DEVICE_NAMES 见 config.py

# 常见作物适宜环境参考（用于种植建议）
CROP_ADVICE = {
    "番茄": {
        "temperature": "18~28°C，最适 25°C 左右",
        "humidity": "40%~70%",
        "light": "喜光，需较强光照（≥20000lux）",
        "note": "注意防治灰霉病与早疫病，通风降温可降低发病率",
    },
    "黄瓜": {
        "temperature": "18~30°C，最适 25~28°C",
        "humidity": "60%~85%",
        "light": "喜光，但强光需适当遮阳",
        "note": "保持土壤湿润但不积水，定期补充钾肥",
    },
    "草莓": {
        "temperature": "15~25°C，最适 20°C 左右",
        "humidity": "50%~70%",
        "light": "中等光照，日照过长需遮阳",
        "note": "注意通风防灰霉病，果实期控制浇水频率",
    },
    "生菜": {
        "temperature": "15~25°C",
        "humidity": "60%~80%",
        "light": "中等光照即可，忌强光直射",
        "note": "生长周期短，注意补水保持叶片鲜嫩",
    },
    "辣椒": {
        "temperature": "20~30°C，最适 25°C 左右",
        "humidity": "50%~70%",
        "light": "喜光喜温",
        "note": "注意防治蚜虫，花期控制氮肥、增施磷钾肥",
    },
}


class AgriculturalAI:
    """
    AI 农业助手（规则引擎版）：
    - 结合实时传感器数据、设备状态回答农事问题
    - 支持：环境查询 / 设备查询 / 控制建议 / 阈值建议 / 作物种植建议 / 告警解释
    - 保留对话历史（内存），支持查询与清空
    """

    def __init__(self, storage, sensor, device_mgr):
        self.storage = storage
        self.sensor = sensor
        self.device_mgr = device_mgr
        self.pest_detector = None
        self.yield_predictor = None
        self._history: List[Dict] = []
        self._max_history = 100

    def set_pest_detector(self, pest_detector):
        """注入病虫害识别模块"""
        self.pest_detector = pest_detector

    def set_yield_predictor(self, yield_predictor):
        """注入产量预测模块"""
        self.yield_predictor = yield_predictor

    # ==================== 对话接口 ====================

    def chat(self, message: str) -> str:
        """处理用户消息并返回回复。
        若已配置大模型，优先使用大模型对话（含实时数据上下文）；
        否则回退到内置规则引擎。
        """
        message = (message or "").strip()
        if not message:
            return "请问您想了解什么？例如：当前环境数据、设备状态、作物种植建议。"

        # 优先使用大模型（配置了 API 时）
        if self._llm_configured():
            try:
                reply = self._llm_chat(message)
            except Exception as e:
                logger.warning(f"大模型调用失败，已回退规则引擎: {e}")
                reply = self._respond(message)
        else:
            reply = self._respond(message)

        # 记录对话历史
        self._history.append({
            "role": "user",
            "content": message,
            "timestamp": datetime.now().isoformat(),
        })
        self._history.append({
            "role": "assistant",
            "content": reply,
            "timestamp": datetime.now().isoformat(),
        })
        # 裁剪过长的历史
        if len(self._history) > self._max_history * 2:
            self._history = self._history[-self._max_history * 2:]

        return reply

    def get_history(self, limit: int = 20) -> List[Dict]:
        """获取对话历史"""
        return self._history[-limit * 2:]

    # ==================== 大模型（LLM）对话 ====================

    @staticmethod
    def _llm_configured() -> bool:
        """是否已配置大模型 API"""
        return bool(LLM_API_BASE and LLM_API_KEY and LLM_MODEL)

    def _build_llm_context(self) -> str:
        """构建实时数据上下文，供大模型参考（确保回答基于真实大棚数据）"""
        lines = []

        # 1. 当前环境数据
        try:
            values = self.sensor.get_current_values() if self.sensor else {}
        except Exception as e:
            logger.error(f"读取传感器数据失败: {e}")
            values = {}
        if values:
            lines.append("【当前实时环境数据】")
            for param in ["temperature", "humidity", "light", "soil_moisture", "soil_ph", "co2"]:
                v = values.get(param)
                if v is not None:
                    lines.append(
                        f"- {PARAM_NAMES.get(param, param)}：{v}{PARAM_UNITS.get(param, '')}"
                    )

        # 2. 设备状态
        try:
            devices = self.device_mgr.get_all_devices() if self.device_mgr else []
        except Exception as e:
            logger.error(f"读取设备状态失败: {e}")
            devices = []
        if devices:
            lines.append("【当前设备状态】")
            for d in devices:
                online = "在线" if d.get("online") else "离线"
                state = "开启" if d.get("state") else "关闭"
                lines.append(f"- {d.get('name', d.get('device_id'))}：{online}，{state}")

        # 3. 环境适宜阈值
        lines.append("【环境适宜范围】")
        for param in ["temperature", "humidity", "light", "soil_moisture", "soil_ph", "co2"]:
            cfg = THRESHOLDS.get(param)
            if cfg:
                lines.append(
                    f"- {PARAM_NAMES.get(param, param)}：{cfg['min']}~{cfg['max']}"
                    f"{PARAM_UNITS.get(param, '')}"
                )

        # 4. 最近告警
        try:
            recent = self.storage.get_recent_alerts(limit=3) if self.storage else []
        except Exception as e:
            logger.error(f"读取告警记录失败: {e}")
            recent = []
        if recent:
            lines.append("【最近告警】")
            for alert in recent:
                lines.append(f"- {alert.get('message', '')}")

        # 5. 最新病虫害识别结果
        if self.pest_detector:
            try:
                pest = self.pest_detector.get_latest_result()
                if pest:
                    lines.append("【最新病虫害识别】")
                    summary = pest.get("summary", {})
                    status = "健康" if summary.get("healthy") else f"疑似{summary.get('primary_pest', '病害')}"
                    lines.append(f"- 状态：{status}（置信度 {summary.get('max_confidence', 0):.0%}）")
            except Exception as e:
                logger.error(f"读取病虫害识别结果失败: {e}")

        # 6. 最新产量预测
        if self.yield_predictor:
            try:
                yp = self.yield_predictor.get_latest_prediction()
                if yp:
                    lines.append("【最新产量预测】")
                    lines.append(f"- 相对产量指数：{yp.get('yield_index', 0)}/100（{yp.get('level', '未知')}）")
                    factors = yp.get("limiting_factors", [])
                    if factors:
                        factor_names = [PARAM_NAMES.get(f, f) for f in factors]
                        lines.append(f"- 主要限制因素：{'、'.join(factor_names)}")
            except Exception as e:
                logger.error(f"读取产量预测失败: {e}")

        return "\n".join(lines)

    def _llm_chat(self, message: str) -> str:
        """调用大模型（OpenAI 兼容 /chat/completions 接口）"""
        system_prompt = (
            "你是智慧农业大棚的AI助手。请结合下方提供的实时大棚数据，"
            "用简洁、专业的中文回答用户关于种植、环境调控、设备状态的问题。"
            "可以给出分析和建议，但不要执行任何实际控制操作。"
            "如果数据不足，请明确说明。"
        )
        context = self._build_llm_context()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{context}\n\n【用户问题】{message}"},
        ]

        body = json.dumps({
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": LLM_MAX_TOKENS,
        }).encode("utf-8")

        url = f"{LLM_API_BASE.rstrip('/')}/chat/completions"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_API_KEY}",
            },
        )

        # 显式绕过系统代理直连：环境变量里若残留 HTTP(S)_PROXY 指向未运行的
        # 本地代理（如 127.0.0.1:7897），urllib 默认会走代理导致连接被拒，
        # 大模型 API 为公网直连，无需代理。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        reply = data["choices"][0]["message"]["content"].strip()
        if not reply:
            raise ValueError("大模型返回为空")
        return reply

    def clear_history(self):
        """清空对话历史"""
        self._history.clear()
        logger.info("AI 对话历史已清空")

    # ==================== 意图识别与回复 ====================

    def _respond(self, message: str) -> str:
        msg = message.lower()

        # 1. 打招呼
        if any(k in msg for k in ["你好", "您好", "hi", "hello", "在吗"]):
            return self._greeting()

        # 2. 帮助
        if any(k in msg for k in ["帮助", "help", "能做什么", "功能"]):
            return self._help_text()

        # 3. 环境/传感器数据查询
        if any(k in msg for k in ["环境", "数据", "当前", "现在", "温度", "湿度",
                                  "光照", "土壤", "co2", "ph", "传感器", "监测"]):
            return self._env_reply(message)

        # 4. 设备状态 / 控制查询
        if any(k in msg for k in ["设备", "风扇", "水泵", "补光", "通风", "遮阳",
                                  "开启", "关闭", "运行"]):
            return self._device_reply(message)

        # 5. 作物种植建议
        for crop in CROP_ADVICE:
            if crop in message:
                return self._crop_reply(crop)

        # 6. 最佳范围 / 阈值查询
        if any(k in msg for k in ["最佳", "最适", "范围", "阈值", "合适", "标准",
                                  "正常值", "多少合适"]):
            return self._threshold_reply(message)

        # 7. 告警 / 异常查询
        if any(k in msg for k in ["告警", "报警", "异常", "有问题", "健康"]):
            return self._alert_reply()

        # 8. 病虫害识别查询
        if any(k in msg for k in ["病虫害", "病害", "虫害", "病", "虫", "识别", "检测", "叶子", "叶片"]):
            return self._pest_reply()

        # 9. 产量预测查询
        if any(k in msg for k in ["产量", "收成", "预测", "趋势", "长势", "生长情况"]):
            return self._yield_reply()

        # 10. 默认回复
        return self._default_reply()

    def _greeting(self) -> str:
        return (
            "您好！我是智慧大棚 AI 助手 🌱\n"
            "我可以帮您：\n"
            "· 查看当前环境数据（如：\"当前温度多少\"）\n"
            "· 查看设备运行状态（如：\"水泵开了吗\"）\n"
            "· 提供环境适宜范围（如：\"温度多少合适\"）\n"
            "· 提供作物种植建议（如：\"番茄怎么种\"）\n"
            "· 查询病虫害识别结果（如：\"有病虫害吗\"）\n"
            "· 查询产量预测（如：\"产量怎么样\"）\n"
            "输入\"帮助\"可查看全部功能。"
        )

    def _help_text(self) -> str:
        return (
            "【AI 助手功能清单】\n"
            "1. 环境查询：问\"当前温度/湿度/光照/土壤/CO2\"\n"
            "2. 设备状态：问\"风扇/水泵/补光灯/通风/遮阳的状态\"\n"
            "3. 环境标准：问\"温度多少合适/最佳湿度范围\"\n"
            "4. 作物建议：问\"番茄/黄瓜/草莓/生菜/辣椒怎么种\"\n"
            "5. 告警解释：问\"最近有什么告警\"\n"
            "6. 病虫害识别：问\"有病虫害吗/叶片检测结果\"\n"
            "   （在监控面板上传叶片照片后可查询结果）\n"
            "7. 产量预测：问\"产量怎么样/收成预测/生长趋势\"\n"
            "8. 自动控制：问\"什么时候会开启水泵\"\n"
            "输入\"你好\"开始对话。"
        )

    def _env_reply(self, message: str) -> str:
        """回答环境数据查询"""
        values = {}
        try:
            values = self.sensor.get_current_values() if self.sensor else {}
        except Exception as e:
            logger.error(f"读取传感器数据失败: {e}")

        if not values:
            return "暂时无法获取传感器数据，请确认传感器模块已启动。"

        # 定位用户关心的具体参数
        target = None
        if any(k in message for k in ["温度"]):
            target = "temperature"
        elif any(k in message for k in ["湿度"]):
            target = "humidity"
        elif any(k in message for k in ["光照", "光"]):
            target = "light"
        elif any(k in message for k in ["土壤", "土"]):
            if "ph" in message.lower() or "酸碱" in message:
                target = "soil_ph"
            else:
                target = "soil_moisture"
        elif "co2" in message.lower() or "二氧化碳" in message:
            target = "co2"

        def _format_line(param: str) -> str:
            value = values.get(param)
            unit = PARAM_UNITS.get(param, "")
            name = PARAM_NAMES.get(param, param)
            if value is None:
                return f"· {name}：暂无数据"
            return f"· {name}：{value}{unit}"

        if target:
            value = values.get(target)
            unit = PARAM_UNITS.get(target, "")
            name = PARAM_NAMES.get(target, target)
            if value is None:
                return f"{name}暂无数据，请稍后再试。"
            cfg = THRESHOLDS.get(target)
            status = ""
            if cfg:
                if value > cfg["max"]:
                    status = f"（高于适宜上限 {cfg['max']}{unit}，建议采取降温/通风等措施）"
                elif value < cfg["min"]:
                    status = f"（低于适宜下限 {cfg['min']}{unit}，建议采取加温/加湿等措施）"
                else:
                    status = f"（处于适宜范围 {cfg['min']}~{cfg['max']}{unit} 内，状态良好）"
            return f"当前{name}为 {value}{unit} {status}"

        # 未指定参数，返回全部环境数据
        lines = ["【当前环境数据】"]
        for param in ["temperature", "humidity", "light", "soil_moisture", "soil_ph", "co2"]:
            lines.append(_format_line(param))
        lines.append("\n如需了解某一项是否适宜，可问我\"温度多少合适\"。")
        return "\n".join(lines)

    def _device_reply(self, message: str) -> str:
        """回答设备状态与控制查询"""
        devices = []
        try:
            devices = self.device_mgr.get_all_devices() if self.device_mgr else []
        except Exception as e:
            logger.error(f"读取设备状态失败: {e}")

        if not devices:
            return "暂时无法获取设备状态，请确认设备管理器已启动。"

        # 定位用户关心的具体设备
        target = None
        if any(k in message for k in ["风扇"]):
            target = "fan"
        elif any(k in message for k in ["水泵", "灌溉"]):
            target = "water_pump"
        elif any(k in message for k in ["补光"]):
            target = "light_supplement"
        elif any(k in message for k in ["通风"]):
            target = "ventilation"
        elif any(k in message for k in ["遮阳"]):
            target = "shading"

        def _format_device(device: Dict) -> str:
            name = device.get("name", device.get("device_id"))
            online = "在线" if device.get("online") else "离线"
            state = "开启" if device.get("state") else "关闭"
            return f"· {name}：{online}，{state}"

        if target:
            device = next((d for d in devices if d["device_id"] == target), None)
            if not device:
                return f"未找到设备：{target}"
            return _format_device(device)

        lines = ["【设备运行状态】"]
        for device in devices:
            lines.append(_format_device(device))
        lines.append("\n提示：手动控制请到监控面板操作，或问我\"什么时候会开风扇\"了解自动控制逻辑。")
        return "\n".join(lines)

    def _threshold_reply(self, message: str) -> str:
        """回答环境适宜范围"""
        target = None
        if any(k in message for k in ["温度"]):
            target = "temperature"
        elif any(k in message for k in ["湿度"]):
            target = "humidity"
        elif any(k in message for k in ["光照", "光"]):
            target = "light"
        elif any(k in message for k in ["土壤", "土"]):
            if "ph" in message.lower() or "酸碱" in message:
                target = "soil_ph"
            else:
                target = "soil_moisture"
        elif "co2" in message.lower() or "二氧化碳" in message:
            target = "co2"

        if target:
            cfg = THRESHOLDS.get(target)
            if not cfg:
                return "抱歉，暂不支持该参数的阈值查询。"
            name = PARAM_NAMES.get(target, target)
            unit = PARAM_UNITS.get(target, "")
            return (
                f"{name}适宜范围为 {cfg['min']}~{cfg['max']}{unit}，"
                f"最适值约 {cfg['optimal']}{unit}。\n"
                f"当前系统会自动在超出范围时采取调控措施。"
            )

        lines = ["【环境适宜范围参考】"]
        for param in ["temperature", "humidity", "light", "soil_moisture", "soil_ph", "co2"]:
            cfg = THRESHOLDS.get(param)
            if cfg:
                unit = PARAM_UNITS.get(param, "")
                lines.append(
                    f"· {PARAM_NAMES.get(param, param)}："
                    f"{cfg['min']}~{cfg['max']}{unit}（最适 {cfg['optimal']}{unit}）"
                )
        lines.append("\n自动控制系统将依据上述范围自动开启/关闭对应设备。")
        return "\n".join(lines)

    def _crop_reply(self, crop: str) -> str:
        """回答作物种植建议"""
        advice = CROP_ADVICE.get(crop)
        if not advice:
            return f"暂时没有 {crop} 的种植资料，可咨询其他常见作物（番茄/黄瓜/草莓/生菜/辣椒）。"

        return (
            f"【{crop} 种植建议】\n"
            f"· 温度：{advice['temperature']}\n"
            f"· 湿度：{advice['humidity']}\n"
            f"· 光照：{advice['light']}\n"
            f"· 提示：{advice['note']}\n"
            "如需查看当前环境是否适合，可问我\"当前环境数据\"。"
        )

    def _alert_reply(self) -> str:
        """回答告警查询"""
        recent = []
        try:
            recent = self.storage.get_recent_alerts(limit=5) if self.storage else []
        except Exception as e:
            logger.error(f"读取告警记录失败: {e}")

        if not recent:
            return "最近没有告警记录，各项环境参数运行正常。👍"

        lines = ["【最近告警记录】"]
        for alert in recent:
            lines.append(
                f"· [{alert.get('level')}] {alert.get('message', '')} "
                f"（{alert.get('timestamp', '')[:19]}）"
            )
        lines.append("\n告警通常由环境参数超出阈值触发，系统会自动调控，也可在面板手动干预。")
        return "\n".join(lines)

    def _pest_reply(self) -> str:
        """回答病虫害识别查询"""
        if self.pest_detector is None:
            return "病虫害识别模块未初始化。"

        latest = self.pest_detector.get_latest_result()
        if not latest:
            return (
                "暂无病虫害识别记录。\n"
                "您可以在监控面板的「病虫害识别」区域上传作物叶片照片，"
                "系统会自动识别是否存在病虫害并给出防治建议。"
            )

        summary = latest.get("summary", {})
        detections = latest.get("detections", [])
        engine = latest.get("engine", "unknown")
        engine_name = {"yolo": "YOLOv8 深度学习", "color": "颜色分析（降级）", "mock": "模拟"}.get(engine, engine)

        if summary.get("healthy"):
            lines = [
                "【病虫害识别结果】",
                f"✅ 最近一次识别：植株健康",
                f"识别引擎：{engine_name}",
                f"识别时间：{latest.get('timestamp', '')[:19]}",
            ]
            if detections:
                lines.append(f"置信度：{detections[0].get('confidence', 0):.0%}")
            lines.append("\n提示：可定期上传叶片照片持续监测，早发现早防治。")
            return "\n".join(lines)

        lines = [
            "【病虫害识别结果】",
            f"⚠️ 检测到疑似病虫害！",
            f"识别引擎：{engine_name}",
            f"识别时间：{latest.get('timestamp', '')[:19]}",
        ]
        for i, d in enumerate(detections[:3], 1):
            lines.append(
                f"{i}. {d.get('name', '未知')}（置信度 {d.get('confidence', 0):.0%}）"
            )
            if d.get("advice"):
                lines.append(f"   建议：{d['advice']}")
        lines.append("\n请及时采取防治措施，严重时建议咨询专业农技人员。")
        return "\n".join(lines)

    def _yield_reply(self) -> str:
        """回答产量预测查询"""
        if self.yield_predictor is None:
            return "产量预测模块未初始化。"

        latest = self.yield_predictor.get_latest_prediction()
        if not latest:
            return "暂无产量预测数据，系统正在收集中，请稍后再试。"

        idx = latest.get("yield_index", 0)
        level = latest.get("level", "未知")
        desc = latest.get("level_desc", "")
        factors = latest.get("limiting_factors", [])

        lines = [
            "【相对产量指数预测】",
            f"当前指数：{idx}/100（{level}）",
            f"评估：{desc}",
        ]

        # 各参数适宜度
        param_scores = latest.get("param_scores", {})
        if param_scores:
            lines.append("\n【各环境参数适宜度】")
            for param, score in param_scores.items():
                name = PARAM_NAMES.get(param, param)
                suit = score.get("suitability", 0)
                value = score.get("value")
                unit = PARAM_UNITS.get(param, "")
                bar = "█" * int(suit * 10) + "░" * (10 - int(suit * 10))
                lines.append(f"· {name}：{value}{unit}  适宜度 {bar} {suit:.0%}")

        if factors:
            factor_names = [PARAM_NAMES.get(f, f) for f in factors]
            lines.append(f"\n⚠️ 主要限制因素：{'、'.join(factor_names)}")
            lines.append("建议优先调控上述参数以提升产量潜力。")

        lines.append(
            "\n注：本指数为基于环境参数的相对产量评估（0-100），"
            "不代表绝对产量。积累真实产量数据后可进一步校准。"
        )
        return "\n".join(lines)

    def _default_reply(self) -> str:
        return (
            "这个问题我暂时没法直接回答哦。\n"
            "我可以帮您：查询环境数据、查看设备状态、了解环境适宜范围、"
            "提供作物种植建议、查看最近告警。\n"
            "输入\"帮助\"查看完整功能列表。"
        )
