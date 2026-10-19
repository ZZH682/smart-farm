
# main.py - 智慧农场系统主程序入口

import logging
import signal
import sys
import threading
import time

from config import TOPICS, CONTROL_TOPICS, DEVICE_STATUS_TOPICS, DEVICE_HEARTBEAT_TOPIC
from config import PEST_MODEL_PATH, PEST_CONF_THRESHOLD, YIELD_PREDICT_INTERVAL
from config import SENSOR_RANGES, WEB_PORT, WEB_TOKEN, DATA_RETENTION_DAYS
from mqtt_client import MQTTClient
from device_manager import DeviceManager
from sensor_simulator import SensorSimulator
from auto_control import AutoController
from data_storage import DataStorage
from ai_assistant import AgriculturalAI
from pest_detect import PestDetector
from yield_predict import YieldPredictor
from web_monitor import (
    init_web, start_web_server,
    emit_sensor_data, emit_alert, emit_control_event,
    emit_pest_detection, emit_yield_prediction,
    get_web_token,
)
from notify import send_alert
from trend_analysis import init_trend_analyzer
from temp_forecast import TemperatureForecaster, init_forecaster
from report_generator import init_report_generator

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SmartFarm.Main")


def is_valid_sensor_value(param: str, value) -> bool:
    """传感器合理性校验：超出配置范围的数值视为坏值（传感器故障/接线异常）。
    坏值不进入自动控制、存储与告警，避免误动作与告警风暴。"""
    if value is None:
        return False
    rng = SENSOR_RANGES.get(param)
    if rng is None:
        return True  # 未配置范围的参数放行
    lo, hi = rng
    try:
        return lo <= float(value) <= hi
    except (TypeError, ValueError):
        return False


class SmartFarmSystem:
    """智慧农场系统主类"""

    def __init__(self):
        self.mqtt = MQTTClient()
        self.device_mgr = DeviceManager()
        self.sensor = SensorSimulator()
        self.controller = AutoController()
        self.storage = DataStorage()
        self.ai = AgriculturalAI(self.storage, self.sensor, self.device_mgr)
        # 病虫害识别模块（YOLO，无摄像头时用图片上传，预留摄像头接口）
        self.pest_detector = PestDetector(
            model_path=PEST_MODEL_PATH,
            conf_threshold=PEST_CONF_THRESHOLD,
            mqtt_client=self.mqtt,
            storage=self.storage,
            alert_callback=self._on_alert,
        )
        # 产量预测模块（基于环境参数的相对产量指数）
        self.yield_predictor = YieldPredictor(
            storage=self.storage,
            sensor=self.sensor,
            mqtt_client=self.mqtt,
            predict_interval=YIELD_PREDICT_INTERVAL,
            alert_callback=self._on_alert,
        )
        # 将新模块注入 AI 助手（支持病虫害/产量相关问答）
        self.ai.set_pest_detector(self.pest_detector)
        self.ai.set_yield_predictor(self.yield_predictor)

        # ===== 数据价值扩展模块 =====
        # 历史趋势分析（24h曲线 + 温湿度/光照关联分析）
        self.trend_analyzer = init_trend_analyzer(self.storage)
        # 温度预测（多项式回归，预测未来2小时，MQTT推送）
        self.forecaster = TemperatureForecaster(
            storage=self.storage,
            mqtt_client=self.mqtt,
            forecast_interval=900,  # 15分钟预测一次
            alert_callback=self._on_alert,
        )
        init_forecaster(self.forecaster)
        # 业务报表（环境日报 + 设备周报）
        self.report_generator = init_report_generator(self.storage, self.mqtt)

        self._running = False
        self._mqtt_available = False

    # ==================== MQTT 回调 ====================

    def _on_mqtt_connect_changed(self, connected: bool):
        """MQTT 连接状态变化回调"""
        self._mqtt_available = connected
        if connected:
            logger.info("MQTT 已连接，硬件通信就绪")
            # 重连后重新订阅设备主题
            self._subscribe_device_topics()
        else:
            logger.warning("MQTT 已断开，系统将在本地模式下运行")

    def _on_device_status_from_mqtt(self, device_id: str, payload: dict):
        """收到硬件设备状态上报（MQTT -> DeviceManager）"""
        self.device_mgr.on_device_status_received(device_id, payload)

    def _on_device_change(self, device_id: str, change_info: dict):
        """设备状态变更回调（DeviceManager -> 系统响应）"""
        change_type = change_info["type"]
        device = change_info["device"]

        if change_type == "offline":
            # 设备离线告警
            alert = {
                "level": "warning",
                "param": "device_offline",
                "value": 0,
                "threshold": 0,
                "message": f"设备 {device['name']} ({device_id}) 离线（心跳超时）",
                "timestamp": None,
            }
            self._on_alert(alert)

        # 同步更新传感器模拟器的设备状态（如果有真实硬件状态反馈）
        state = device.get("state")
        if state is not None:
            self.sensor.update_device_status(device_id, state)

    def _on_command_timeout(self, device_id: str, cmd: dict):
        """控制指令超时回调"""
        alert = {
            "level": "warning",
            "param": "command_timeout",
            "value": 0,
            "threshold": 0,
            "message": (
                f"设备 {device_id} 控制指令超时 "
                f"({self.device_mgr.command_timeout}s 未响应)"
            ),
            "timestamp": None,
        }
        self._on_alert(alert)

    # ==================== 数据流回调 ====================

    def _on_sensor_data(self, data: dict):
        """传感器数据回调（模拟器）"""
        # ---- ESP32 让位：ESP32 正在发布的真实参数，模拟器不再重复发布/存储/推送 ----
        sensors = data.get("sensors", {})
        if self._mqtt_available:
            covered = {p for p in sensors if self.mqtt.esp32_sensor_recent(p)}
            if covered:
                logger.info(f"模拟器让位给 ESP32 真实数据: {sorted(covered)}")
            if covered:
                sensors = {p: v for p, v in sensors.items() if p not in covered}
                data = dict(data)
                data["sensors"] = sensors

        # ---- 坏值过滤：超出合理范围的参数丢弃（模拟器理论上正常，兜底校验） ----
        bad = [p for p, v in sensors.items() if not is_valid_sensor_value(p, v)]
        if bad:
            logger.warning(f"丢弃超出合理范围的模拟器数据: {[(p, sensors[p]) for p in bad]}")
            sensors = {p: v for p, v in sensors.items() if p not in bad}
            data = dict(data)
            data["sensors"] = sensors
        if not sensors:
            return

        # 1. 发布到 MQTT（仅在可用时）
        if self._mqtt_available:
            for param, value in data.get("sensors", {}).items():
                topic = TOPICS.get(param)
                if topic:
                    self.mqtt.publish(topic, {
                        "value": value,
                        "timestamp": data["timestamp"],
                    })
            self.mqtt.publish(TOPICS["status"], {
                "devices": data.get("devices", {}),
                "device_status": {
                    d["device_id"]: {
                        "online": d["online"],
                        "state": d["state"],
                    }
                    for d in self.device_mgr.get_all_devices()
                },
                "timestamp": data["timestamp"],
            })

        # 2. 自动控制处理（传感器全被让位时跳过，ESP32 数据已单独触发）
        if data.get("sensors"):
            self.controller.process_sensor_data(data)

        # 3. 数据存储
        if data.get("sensors"):
            self.storage.save_sensor_data(data)

        # 4. 实时推送到 Web 面板（Socket.IO 实时大屏）
        if data.get("sensors"):
            emit_sensor_data(data)

    def _on_control_publish(self, topic: str, payload: dict):
        """控制指令发布回调（AutoController -> MQTT + DeviceManager）"""
        device = payload.get("device")
        state = payload.get("state", False)

        # 更新传感器模拟器的设备状态
        self.sensor.update_device_status(device, state)

        # 保存控制日志
        self.storage.save_control_log(payload)

        # 实时推送控制事件到 Web 面板
        emit_control_event(device, state, payload.get("reason", ""))

        # 通过 MQTT 下发到硬件
        if self._mqtt_available:
            self.mqtt.publish_control(device, state, payload.get("reason", ""))
            # 记录待响应的命令（用于超时检测）
            self.device_mgr.on_command_sent(device, state, payload.get("reason", ""))

    def _on_alert(self, alert: dict):
        """告警回调（入库 + MQTT + Web 推送，带 60s 去重节流）"""
        # 确保有时间戳
        if not alert.get("timestamp"):
            from datetime import datetime
            alert["timestamp"] = datetime.now().isoformat()

        # 告警去重：同一 (level, param) 在 60 秒窗口内只处理一次。
        # 持续越限时传感器每 5 秒到达一条，若全部入库/推送，
        # alerts 表会以每天上万条的速度膨胀（此前 17 天达 8787 条），
        # Web 告警 toast 也会刷屏。窗口内重复告警直接丢弃。
        alert_key = f"{alert.get('level')}:{alert.get('param')}"
        now = time.time()
        last_db = getattr(self, "_last_alert_db_time", {})
        if now - last_db.get(alert_key, 0.0) < 60:
            return
        last_db[alert_key] = now
        self._last_alert_db_time = last_db

        # 保存告警记录
        self.storage.save_alert(alert)

        # 发布到 MQTT
        if self._mqtt_available:
            self.mqtt.publish(TOPICS["alert"], alert)

        # 实时推送到 Web 面板
        emit_alert(alert)

        # 推送到手机（webhook，需在 config 中配置 NOTIFY_WEBHOOK_URL）
        send_alert(alert)

    # ==================== MQTT 主题订阅 ====================

    def _subscribe_device_topics(self):
        """订阅设备相关主题"""
        if not self._mqtt_available:
            return

        # 订阅设备状态反馈
        for device_id, topic in DEVICE_STATUS_TOPICS.items():
            self.mqtt.subscribe(topic, self._on_device_status_from_mqtt)

        # 订阅心跳主题（通配符）
        self.mqtt.subscribe(DEVICE_HEARTBEAT_TOPIC, self._on_heartbeat_message)

        # 订阅 ESP32 真实传感器数据主题（farm/greenhouse/sensor/#）
        # ESP32 与模拟器发同一主题：通过 payload 中 "sensor" 字段区分来源
        self.mqtt.subscribe(f"{TOPICS['temperature'].rsplit('/', 1)[0]}/#",
                            self._on_esp32_sensor_message)

        # 订阅控制回传（外部直接控制指令）
        self.mqtt.subscribe(TOPICS["control"], self._on_external_control_message)

    def _on_heartbeat_message(self, topic: str, payload):
        """处理心跳消息（ESP32 节点统一心跳，含继电器在线同步）"""
        # 心跳主题格式: farm/greenhouse/device/{device_id}/heartbeat
        parts = topic.split("/")
        if len(parts) >= 4:
            device_id = parts[3]
            self.device_mgr.on_node_heartbeat(device_id)

    def _on_esp32_sensor_message(self, topic: str, payload):
        """处理 ESP32 真实传感器数据（sensor/ 主题，payload 含 "sensor" 字段）"""
        from datetime import datetime
        # 只处理 ESP32 固件格式（含 "sensor" 字段）；模拟器消息在此忽略，避免重复入库
        if not isinstance(payload, dict) or not payload.get("sensor"):
            return
        value = payload.get("value")
        if value is None:
            return
        param = topic.rsplit("/", 1)[-1]
        if param not in ("temperature", "humidity", "soil_moisture", "soil_temperature"):
            return

        # ---- 坏值过滤：ESP32 传感器读取异常（如 DHT11 返回 -2℃/4%）直接丢弃，
        #      不进入自动控制/存储/告警，防止误动作与告警风暴 ----
        if not is_valid_sensor_value(param, value):
            logger.warning(f"丢弃 ESP32 异常数据: {param} = {value}（超出合理范围 {SENSOR_RANGES.get(param)}）")
            return

        data = {
            "timestamp": datetime.now().isoformat(),
            "sensors": {param: value},
            "devices": {},
        }
        # 真实数据同样参与自动控制 + 入库 + Web 实时大屏
        self.controller.process_sensor_data(data)
        self.storage.save_sensor_data(data)
        emit_sensor_data(data)

    def _on_external_control_message(self, topic: str, payload):
        """处理来自 MQTT 的外部控制指令"""
        if isinstance(payload, dict):
            # 忽略服务端自身下发的控制指令（source=server），
            # 否则会被 control/# 通配符订阅捕获形成"自回声"死循环
            if payload.get("source") == "server":
                logger.debug(f"忽略服务端自身控制指令: {topic}")
                return
            device = payload.get("device")
            state = payload.get("state", payload.get("action") == "on")
            if device:
                self.controller.manual_control(device, state)
                self.sensor.update_device_status(device, state)
                logger.info(
                    f"收到外部 MQTT 控制指令: {device} -> {'开启' if state else '关闭'}"
                )

    # ==================== 生命周期 ====================

    def _load_persisted_thresholds(self):
        """启动时从数据库加载持久化阈值（Web 面板修改过的阈值重启不丢失）"""
        try:
            import json
            raw = self.storage.get_setting("thresholds")
            if not raw:
                return
            saved = json.loads(raw)
            from config import THRESHOLDS
            updated = 0
            for key, cfg in saved.items():
                if key in THRESHOLDS and isinstance(cfg, dict):
                    if "min" in cfg:
                        THRESHOLDS[key]["min"] = float(cfg["min"])
                    if "max" in cfg:
                        THRESHOLDS[key]["max"] = float(cfg["max"])
                    if "optimal" in cfg:
                        THRESHOLDS[key]["optimal"] = float(cfg["optimal"])
                    updated += 1
            self.controller.thresholds = THRESHOLDS
            logger.info(f"已从数据库加载持久化阈值配置（{updated} 项）")
        except Exception as e:
            logger.warning(f"加载持久化阈值失败（使用默认值）: {e}")

    def start(self):
        """启动系统"""
        logger.info("=" * 50)
        logger.info("智慧农业大棚环境监控系统 启动中...")
        logger.info("=" * 50)

        self._running = True

        # 0. 加载持久化阈值 + 启动数据清理线程
        self._load_persisted_thresholds()
        self.storage.start_cleanup(retention_days=DATA_RETENTION_DAYS)

        # 1. 连接 MQTT Broker
        try:
            self.mqtt.set_on_connect_callback(self._on_mqtt_connect_changed)
            self.mqtt.set_on_device_status_callback(self._on_device_status_from_mqtt)
            self.mqtt.connect()
        except Exception as e:
            self._mqtt_available = False
            logger.warning(f"MQTT Broker 连接失败: {e}")
            logger.warning("系统将在本地模式下运行（Web 面板和本地功能正常）")
            logger.info("提示: 如需完整 MQTT 功能，请先启动 Mosquitto Broker")

        # 2. 启动设备管理器
        self.device_mgr.set_on_device_change_callback(self._on_device_change)
        self.device_mgr.set_on_command_timeout_callback(self._on_command_timeout)
        self.device_mgr.start()

        # 3. 设置数据流回调
        self.sensor.set_callback(self._on_sensor_data)
        self.controller.set_publish_callback(self._on_control_publish)
        self.controller.set_alert_callback(self._on_alert)

        # 4. 初始化 Web 模块（注入所有模块引用，包括 AI 助手、病虫害识别、产量预测）
        init_web(
            self.storage, self.controller, self.sensor, self.mqtt,
            self.device_mgr, self.ai,
            pest=self.pest_detector, yield_pred=self.yield_predictor,
        )

        # 5. 启动传感器采集
        self.sensor.start()
        logger.info("传感器数据采集已启动")

        # 5.1 启动温度预测模块
        self.forecaster.start()
        logger.info("温度预测模块已启动（每15分钟预测未来2小时温度）")

        # 5.2 启动报表生成模块（日报每日00:05，周报每周一）
        self.report_generator.start()
        logger.info("报表生成模块已启动（环境日报/设备周报）")

        # 6. 启动产量预测模块（定期计算相对产量指数）
        self.yield_predictor.start()
        logger.info(f"产量预测模块已启动（间隔 {YIELD_PREDICT_INTERVAL}s）")

        # 7. 启动 Web 监控界面
        web_thread = threading.Thread(target=start_web_server, daemon=True)
        web_thread.start()
        logger.info("Web 监控界面已启动")

        # 8. 打印状态摘要
        logger.info("=" * 50)
        logger.info("系统启动完成!")
        logger.info(f"监控面板: http://localhost:{WEB_PORT}")
        _web_token = get_web_token()
        if WEB_TOKEN:
            logger.info(f"控制接口令牌（已配置）: {_web_token}（请勿外泄，前端页面自动携带）")
        else:
            logger.info(f"控制接口令牌（自动生成）: {_web_token}（请勿外泄，前端页面自动携带）")
        logger.info(f"MQTT 状态: {'已连接' if self._mqtt_available else '未连接（本地模式）'}")
        logger.info(f"病虫害识别引擎: {self.pest_detector.engine}")
        device_count = self.device_mgr.get_device_count()
        logger.info(f"设备数量: {device_count['total']} 个 "
                     f"(在线 {device_count['online']}, 离线 {device_count['offline']})")
        logger.info("数据价值模块: 趋势分析[24h曲线/关联分析] + 报表[日报/周报] + 温度预测[2h]")
        logger.info("=" * 50)

    def stop(self):
        """停止系统"""
        logger.info("正在关闭系统...")
        self._running = False
        self.sensor.stop()
        self.yield_predictor.stop()
        self.forecaster.stop()
        self.report_generator.stop()
        self.device_mgr.stop()
        if self._mqtt_available:
            self.mqtt.disconnect()
        try:
            self.storage.close_all()
        except Exception:
            pass
        logger.info("系统已关闭")


def main():
    system = SmartFarmSystem()

    def signal_handler(sig, frame):
        logger.info("收到退出信号")
        system.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    system.start()

    try:
        while system._running:
            time.sleep(1)
    except KeyboardInterrupt:
        system.stop()


if __name__ == "__main__":
    main()
