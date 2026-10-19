
# device_manager.py - 设备管理器（维护设备在线状态 / 运行状态 / 心跳与命令超时检测）

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from config import DEVICE_STATUS_TOPICS, DEVICE_NAMES

logger = logging.getLogger("SmartFarm.Device")

# 心跳超时（秒）：超过该时间未收到心跳，判定设备离线
DEVICE_HEARTBEAT_TIMEOUT = 60
# 心跳巡检间隔（秒）
HEARTBEAT_CHECK_INTERVAL = 5
# 命令响应超时（秒）：下发控制指令后，超过该时间未收到状态反馈，判定命令超时
DEVICE_COMMAND_TIMEOUT = 30


class DeviceManager:
    """
    设备管理器：
    - 维护所有受控设备（排风扇/水泵/补光灯/通风窗/遮阳帘）的在线与运行状态
    - 后台线程定期巡检心跳，超时自动判定设备离线并触发回调
    - 记录已下发的控制指令，超时未响应触发命令超时回调
    - 真实硬件通过 MQTT 上报状态（on_device_status_received 接收）
    """

    def __init__(self):
        # device_id -> 设备信息
        self._devices: Dict[str, Dict] = {}
        # device_id -> {"state": bool, "reason": str, "sent_at": float} 待响应命令
        self._pending_commands: Dict[str, Dict] = {}

        self._on_device_change_callback: Optional[Callable] = None
        self._on_command_timeout_callback: Optional[Callable] = None

        self.command_timeout = DEVICE_COMMAND_TIMEOUT

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self._init_devices()

    def _init_devices(self):
        """根据配置创建设备初始状态（默认离线，等待硬件/MQTT 上报上线）。
        除 5 个受控设备外，注册 ESP32 节点本身（其心跳是整机在线的依据）。"""
        now = datetime.now().isoformat()
        device_ids = list(DEVICE_STATUS_TOPICS) + ["esp32"]
        for device_id in device_ids:
            self._devices[device_id] = {
                "device_id": device_id,
                "name": DEVICE_NAMES.get(device_id, device_id),
                "online": False,
                "state": False,
                "last_seen": None,
                "online_since": None,
                "heartbeat_interval": DEVICE_HEARTBEAT_TIMEOUT,
            }

    # ==================== 回调设置 ====================

    def set_on_device_change_callback(self, callback: Callable):
        """设置设备状态变更回调，回调签名: (device_id, change_info)"""
        self._on_device_change_callback = callback

    def set_on_command_timeout_callback(self, callback: Callable):
        """设置控制指令超时回调，回调签名: (device_id, cmd_dict)"""
        self._on_command_timeout_callback = callback

    # ==================== 设备状态管理 ====================

    def on_device_status_received(self, device_id: str, payload):
        """
        接收设备状态反馈（来自 MQTT 或心跳）。
        payload 可为 dict（含 state / online 字段）或原始心跳消息。
        """
        if device_id not in self._devices:
            logger.warning(f"收到未知设备状态: {device_id}")
            return

        now = datetime.now()
        now_ts = time.time()

        with self._lock:
            device = self._devices[device_id]
            was_online = device["online"]
            old_state = device["state"]

            # 解析 payload
            state = None
            if isinstance(payload, dict):
                state = payload.get("state")
                if "online" in payload:
                    self._set_device_online_locked(device_id, payload["online"], now)
            else:
                logger.debug(f"心跳消息(非JSON): {payload}")

            # 更新最后心跳时间（ISO 字符串 + 时间戳，供巡检判断超时）
            device["last_seen"] = now.isoformat()
            device["last_seen_ts"] = now_ts

            # 心跳本身证明设备在线
            if not device["online"]:
                self._set_device_online_locked(device_id, True, now)
                was_online = False

            # 更新运行状态（若 payload 携带）
            change_info = None
            if state is not None and state != old_state:
                device["state"] = bool(state)
                change_info = {
                    "type": "state_change",
                    "device": dict(device),
                }
            elif not was_online and device["online"]:
                change_info = {
                    "type": "online",
                    "device": dict(device),
                }

            # 若存在待响应命令且收到状态反馈，视为命令已响应，清除待响应记录
            if state is not None and device_id in self._pending_commands:
                logger.info(f"设备 {device['name']} 已响应控制指令")
                del self._pending_commands[device_id]

            online = device["online"]
            device_snapshot = dict(device)

        if online and state is not None:
            logger.info(
                f"设备状态上报: {device_snapshot['name']} -> "
                f"{'开启' if state else '关闭'} (在线)"
            )
        elif online:
            logger.debug(f"设备心跳: {device_snapshot['name']}")

        if change_info:
            self._fire_device_change(device_id, change_info)

    def on_node_heartbeat(self, device_id: str):
        """
        处理节点心跳（ESP32 节点统一心跳）。
        - 节点心跳本身证明该节点在线；
        - ESP32 节点在线时，其下挂的继电器视为在线（物理上随整机供电），
          同步心跳时间，避免继电器被心跳巡检误判离线后反复"离线→恢复"。
        """
        now = datetime.now()
        now_ts = time.time()

        with self._lock:
            node = self._devices.get(device_id)
            if node is None:
                # 未注册的新节点：自动登记为通用节点
                logger.info(f"自动注册新节点: {device_id}")
                node = {
                    "device_id": device_id,
                    "name": DEVICE_NAMES.get(device_id, f"节点{device_id}"),
                    "online": False,
                    "state": False,
                    "last_seen": None,
                    "online_since": None,
                    "heartbeat_interval": DEVICE_HEARTBEAT_TIMEOUT,
                }
                self._devices[device_id] = node

            node["last_seen"] = now.isoformat()
            node["last_seen_ts"] = now_ts
            if not node["online"]:
                node["online"] = True
                node["online_since"] = now.isoformat()
                logger.info(f"设备 {node['name']} 心跳上线")

            # ESP32 节点心跳 = 整机在线，同步其下挂继电器的心跳时间
            if device_id == "esp32":
                for d in self._devices.values():
                    if d["device_id"] in ("esp32",):
                        continue
                    d["last_seen"] = now.isoformat()
                    d["last_seen_ts"] = now_ts
                    if not d["online"]:
                        d["online"] = True
                        d["online_since"] = now.isoformat()

    def set_device_online(self, device_id: str, online: bool):
        """手动设置设备在线/离线（用于测试模拟）"""
        if device_id not in self._devices:
            logger.warning(f"未知设备: {device_id}")
            return

        now = datetime.now()
        with self._lock:
            self._set_device_online_locked(device_id, online, now)
            device = self._devices[device_id]
            was_online = not online
            device_snapshot = dict(device)

        change_info = {
            "type": "online" if online else "offline",
            "device": device_snapshot,
        }
        logger.info(
            f"设备 {device_snapshot['name']} "
            f"{'上线' if online else '离线'}"
        )
        self._fire_device_change(device_id, change_info)

    def _set_device_online_locked(self, device_id: str, online: bool, now: datetime):
        """在持有锁的前提下设置设备在线状态"""
        device = self._devices[device_id]
        if online and not device["online"]:
            device["online"] = True
            device["online_since"] = now.isoformat()
        elif not online:
            device["online"] = False
            device["online_since"] = None
            # 设备离线时清除待响应命令
            self._pending_commands.pop(device_id, None)

    # ==================== 控制指令跟踪 ====================

    def on_command_sent(self, device: str, state: bool, reason: str = ""):
        """记录已下发的控制指令，用于超时检测"""
        if device not in self._devices:
            logger.warning(f"向未知设备下发指令: {device}")
            return

        with self._lock:
            self._pending_commands[device] = {
                "state": state,
                "reason": reason,
                "sent_at": time.time(),
                "sent_at_iso": datetime.now().isoformat(),
            }
            logger.debug(f"已记录待响应指令: {device} -> {state}")

    def _check_command_timeout(self):
        """检查是否有控制指令超时未响应"""
        now_ts = time.time()
        with self._lock:
            expired = []
            for device_id, cmd in list(self._pending_commands.items()):
                if now_ts - cmd["sent_at"] > self.command_timeout:
                    expired.append((device_id, cmd))
            for device_id, cmd in expired:
                del self._pending_commands[device_id]

        for device_id, cmd in expired:
            logger.warning(
                f"设备 {DEVICE_NAMES.get(device_id, device_id)} "
                f"控制指令超时（{self.command_timeout}s 未响应）"
            )
            if self._on_command_timeout_callback:
                try:
                    self._on_command_timeout_callback(device_id, cmd)
                except Exception as e:
                    logger.error(f"命令超时回调异常: {e}")

    # ==================== 心跳巡检 ====================

    def _heartbeat_check_loop(self):
        """后台巡检线程：检测设备心跳是否超时"""
        logger.info("设备心跳巡检已启动")
        while self._running:
            try:
                now_ts = time.time()
                offline_devices = []
                with self._lock:
                    for device_id, device in self._devices.items():
                        if device["online"] and device["last_seen"] is None:
                            continue
                        if not device["online"]:
                            continue
                        last_seen = device.get("last_seen_ts")
                        if last_seen is None:
                            continue
                        if now_ts - last_seen > device["heartbeat_interval"]:
                            offline_devices.append(dict(device))
                            device["online"] = False
                            device["online_since"] = None
                            self._pending_commands.pop(device_id, None)

                for device in offline_devices:
                    logger.warning(
                        f"设备 {device['name']} 心跳超时，判定离线"
                    )
                    self._fire_device_change(device["device_id"], {
                        "type": "offline",
                        "device": device,
                    })
            except Exception as e:
                logger.error(f"心跳巡检异常: {e}")

            time.sleep(HEARTBEAT_CHECK_INTERVAL)
        logger.info("设备心跳巡检已停止")

    # ==================== 查询接口 ====================

    def get_all_devices(self) -> List[Dict]:
        """获取所有设备状态"""
        with self._lock:
            return [dict(d) for d in self._devices.values()]

    def get_device(self, device_id: str) -> Optional[Dict]:
        """获取单个设备详情，不存在返回 None"""
        with self._lock:
            device = self._devices.get(device_id)
            return dict(device) if device else None

    def get_device_count(self) -> Dict:
        """获取设备统计信息"""
        with self._lock:
            total = len(self._devices)
            online = sum(1 for d in self._devices.values() if d["online"])
            offline = total - online
        return {"total": total, "online": online, "offline": offline}

    # ==================== 生命周期 ====================

    def start(self):
        """启动设备管理器（心跳巡检线程）"""
        if self._running:
            return
        self._running = True
        # 记录当前时间戳，供心跳超时判断
        now_ts = time.time()
        with self._lock:
            for device in self._devices.values():
                device["last_seen_ts"] = now_ts
        self._thread = threading.Thread(
            target=self._heartbeat_check_loop, daemon=True
        )
        self._thread.start()

    def stop(self):
        """停止设备管理器"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("设备管理器已停止")

    # ==================== 内部工具 ====================

    def _fire_device_change(self, device_id: str, change_info: dict):
        """触发设备变更回调"""
        if not self._on_device_change_callback:
            return
        try:
            self._on_device_change_callback(device_id, change_info)
        except Exception as e:
            logger.error(f"设备变更回调异常: {e}")
