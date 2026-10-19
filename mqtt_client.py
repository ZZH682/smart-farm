
# mqtt_client.py - 生产级 MQTT 通信客户端

import json
import logging
import ssl
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Optional

import paho.mqtt.client as mqtt

from config import (
    MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE, MQTT_CLIENT_ID,
    MQTT_USERNAME, MQTT_PASSWORD, MQTT_USE_TLS, MQTT_CA_CERTS,
    MQTT_QOS_PUBLISH, MQTT_QOS_SUBSCRIBE,
    MQTT_RECONNECT_DELAY_MIN, MQTT_RECONNECT_DELAY_MAX, MQTT_CONNECT_TIMEOUT,
    LWT_TOPIC, LWT_PAYLOAD, LWT_QOS, LWT_RETAIN,
    DEVICE_STATUS_TOPICS,
    DISCOVER_TOPIC, DISCOVER_RESPONSE_TOPIC, DISCOVER_INFO_TOPIC,
    DISCOVER_TIMEOUT, DISCOVER_OFFLINE_AFTER, TOPIC_PREFIX,
)

logger = logging.getLogger("SmartFarm.MQTT")


class MQTTClient:
    """
    生产级 MQTT 客户端，支持：
    - 指数退避自动重连
    - LWT 遗嘱消息（断线通知）
    - 重连后自动重订阅
    - TLS 加密连接（可选）
    - 用户名密码认证（可选）
    - 连接状态变化回调
    - 设备状态反馈路由
    """

    def __init__(self, broker: str = MQTT_BROKER, port: int = MQTT_PORT,
                 client_id: str = MQTT_CLIENT_ID):
        self.broker = broker
        self.port = port
        self.client_id = client_id

        # 创建 MQTT 客户端
        self.client = mqtt.Client(client_id=self.client_id, protocol=mqtt.MQTTv311)

        # 设置认证
        if MQTT_USERNAME:
            self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        # 设置 TLS（如果启用）
        if MQTT_USE_TLS:
            self.client.tls_set(
                ca_certs=MQTT_CA_CERTS if MQTT_CA_CERTS else None,
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS,
            )

        # 设置 LWT 遗嘱消息
        self.client.will_set(LWT_TOPIC, LWT_PAYLOAD, LWT_QOS, LWT_RETAIN)

        # 设置回调
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        # 内部状态
        self._connected = False
        self._connect_failed = False
        self._reconnect_delay = MQTT_RECONNECT_DELAY_MIN
        self._lock = threading.Lock()

        # 订阅管理（用于重连后自动重订阅）
        self._subscriptions: Dict[str, Callable] = {}

        # 外部回调
        self._on_connect_status_callback: Optional[Callable[[bool], None]] = None
        self._on_device_status_callback: Optional[Callable[[str, dict], None]] = None
        self._on_device_discovered_callback: Optional[Callable[[dict], None]] = None

        # ===== 设备发现（扫描）状态 =====
        # device_id -> 设备信息 dict（含 last_seen_ts / discovered_via）
        self._discovered_devices: Dict[str, Dict] = {}
        self._discovery_lock = threading.Lock()
        # 本次主动扫描的非随机数（用于区分不同轮次的响应）
        self._discovery_nonce = ""
        # 本轮扫描已收到的响应 device_id 集合
        self._discovery_scan_ids: set = set()
        self._discovery_scanning = False
        self._discovery_scan_started_at = 0.0

        # ESP32 真实传感器数据最近到达时间（参数名 -> 时间戳）
        # 用于"模拟器让位"：ESP32 在发真实数据时，模拟器不再发布同名参数
        self._esp32_sensor_last: Dict[str, float] = {}

    def esp32_sensor_recent(self, param: str, window: float = 15.0) -> bool:
        """
        判断指定参数最近 window 秒内是否有 ESP32 真实数据到达。
        返回 True 表示 ESP32 正在发布该参数（模拟器应让位）。
        """
        with self._lock:
            last = self._esp32_sensor_last.get(param)
        return last is not None and (time.time() - last) <= window

    def _mark_esp32_sensor(self, topic: str, payload: dict):
        """记录 ESP32 真实传感器数据到达时间（用于模拟器让位）"""
        from config import TOPICS
        param = None
        for name, t in TOPICS.items():
            if t == topic:
                param = name
                break
        if param is None:
            return
        with self._lock:
            self._esp32_sensor_last[param] = time.time()
        logger.info(f"收到 ESP32 真实数据: {param} = {payload.get('value')}")

    def set_on_connect_callback(self, callback: Callable[[bool], None]):
        """设置连接状态变化回调，参数 connected: bool"""
        self._on_connect_status_callback = callback

    def set_on_device_status_callback(self, callback: Callable[[str, dict], None]):
        """设置设备状态反馈回调，参数 (device_name, status_payload)"""
        self._on_device_status_callback = callback

    def _on_connect(self, client, userdata, flags, rc):
        """连接成功/失败回调"""
        if rc == 0:
            self._connected = True
            self._connect_failed = False
            self._reconnect_delay = MQTT_RECONNECT_DELAY_MIN
            logger.info(f"已连接到 MQTT Broker: {self.broker}:{self.port}")

            # 发布上线消息（覆盖 LWT 遗嘱）
            self._publish_server_online()

            # 重连后自动重订阅
            self._resubscribe_all()

            if self._on_connect_status_callback:
                try:
                    self._on_connect_status_callback(True)
                except Exception as e:
                    logger.error(f"连接回调异常: {e}")
        else:
            self._connect_failed = True
            reasons = {
                1: "协议版本错误",
                2: "Client ID 无效",
                3: "Broker 不可用",
                4: "用户名/密码错误",
                5: "未授权",
            }
            reason = reasons.get(rc, f"未知错误 (rc={rc})")
            logger.error(f"MQTT 连接失败: {reason}")

            if self._on_connect_status_callback:
                try:
                    self._on_connect_status_callback(False)
                except Exception as e:
                    logger.error(f"连接回调异常: {e}")

    def _publish_server_online(self):
        """发布服务端上线消息"""
        try:
            payload = json.dumps({
                "status": "online",
                "source": "server",
                "timestamp": datetime.now().isoformat(),
            })
            self.client.publish(LWT_TOPIC, payload, qos=1, retain=True)
        except Exception as e:
            logger.error(f"发布上线消息失败: {e}")

    def _on_disconnect(self, client, userdata, rc):
        """断开连接回调"""
        was_connected = self._connected
        self._connected = False

        if rc == 0:
            logger.info("MQTT 正常断开")
        else:
            logger.warning(
                f"MQTT 意外断开 (rc={rc}), {self._reconnect_delay}秒后重连..."
            )
            self._start_reconnect()

        if was_connected and self._on_connect_status_callback:
            try:
                self._on_connect_status_callback(False)
            except Exception as e:
                logger.error(f"断连回调异常: {e}")

    def _on_message(self, client, userdata, msg):
        """收到消息回调，自动路由到对应处理器"""
        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = msg.payload.decode()

        topic = msg.topic
        logger.debug(f"收到消息 [{topic}]: {payload}")

        # 识别 ESP32 真实传感器数据（payload 含 "sensor" 字段 = 固件发布格式）
        if isinstance(payload, dict) and payload.get("sensor"):
            self._mark_esp32_sensor(topic, payload)

        # 优先匹配设备状态反馈主题
        for device, status_topic in DEVICE_STATUS_TOPICS.items():
            if topic == status_topic and self._on_device_status_callback:
                try:
                    self._on_device_status_callback(device, payload)
                except Exception as e:
                    logger.error(f"设备状态回调异常 [{device}]: {e}")
                return

        # 匹配通配符订阅
        with self._lock:
            for pattern, callback in self._subscriptions.items():
                if mqtt.topic_matches_sub(pattern, topic):
                    try:
                        callback(topic, payload)
                    except Exception as e:
                        logger.error(f"消息处理异常 [{topic}]: {e}")

    def _resubscribe_all(self):
        """重连后重新订阅所有已注册主题"""
        with self._lock:
            for topic in self._subscriptions:
                self.client.subscribe(topic, MQTT_QOS_SUBSCRIBE)
                logger.debug(f"重新订阅: {topic}")

    def _start_reconnect(self):
        """启动异步重连线程（指数退避策略）"""
        def _reconnect_worker():
            while not self._connected:
                delay = self._reconnect_delay
                logger.info(f"等待 {delay} 秒后重连...")
                time.sleep(delay)
                try:
                    logger.info("尝试重连 MQTT Broker...")
                    self.client.reconnect()
                    return  # 成功会触发 _on_connect
                except Exception as e:
                    logger.warning(f"重连失败: {e}")
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2,
                        MQTT_RECONNECT_DELAY_MAX
                    )

        t = threading.Thread(target=_reconnect_worker, daemon=True)
        t.start()

    def connect(self):
        """连接到 MQTT Broker（带超时等待）"""
        try:
            self.client.connect(self.broker, self.port, MQTT_KEEPALIVE)
            self.client.loop_start()
            logger.info(f"正在连接 MQTT Broker: {self.broker}:{self.port}")

            # 等待连接结果
            deadline = time.time() + MQTT_CONNECT_TIMEOUT
            while not self._connected and not self._connect_failed:
                if time.time() > deadline:
                    break
                time.sleep(0.1)

            if not self._connected:
                self._connect_failed = True
                raise ConnectionError(
                    f"连接 MQTT Broker 超时 ({MQTT_CONNECT_TIMEOUT}s)"
                )

        except Exception as e:
            self._connect_failed = True
            logger.error(f"连接 MQTT Broker 异常: {e}")
            raise

    def disconnect(self):
        """断开连接"""
        if self._connected:
            # 发布离线消息（覆盖 LWT）
            try:
                self.client.publish(LWT_TOPIC, LWT_PAYLOAD, qos=1, retain=True)
            except Exception:
                pass

        self.client.loop_stop()
        self.client.disconnect()
        self._connected = False
        logger.info("已断开 MQTT 连接")

    def subscribe(self, topic: str, callback: Callable):
        """订阅主题（支持通配符），重连后自动重订阅"""
        with self._lock:
            self._subscriptions[topic] = callback

        if self._connected:
            self.client.subscribe(topic, MQTT_QOS_SUBSCRIBE)
            logger.info(f"已订阅主题: {topic}")
        else:
            logger.debug(f"MQTT 未连接，主题 {topic} 已注册，待连接后自动订阅")

    def publish(self, topic: str, payload: dict, qos: int = None, retain: bool = False):
        """
        发布消息。未连接时静默跳过（不报错）。
        """
        if not self._connected:
            logger.debug(f"MQTT 未连接，跳过发布: {topic}")
            return

        if qos is None:
            qos = MQTT_QOS_PUBLISH

        message = json.dumps(payload, ensure_ascii=False)
        result = self.client.publish(topic, message, qos=qos, retain=retain)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.debug(f"已发布消息 [{topic}]: {message}")
        else:
            logger.error(f"发布消息失败 [{topic}], rc={result.rc}")

    def publish_control(self, device: str, state: bool, reason: str = ""):
        """
        发布设备控制指令（便捷方法）。
        自动选择对应的控制主题，QoS=1 确保送达。
        """
        from config import CONTROL_TOPICS
        topic = CONTROL_TOPICS.get(device)
        if not topic:
            logger.error(f"未知设备: {device}")
            return

        payload = {
            "device": device,
            "state": state,
            "action": "on" if state else "off",
            "reason": reason,
            "source": "server",
            "timestamp": datetime.now().isoformat(),
        }
        self.publish(topic, payload, qos=1)

    @property
    def is_connected(self) -> bool:
        return self._connected
