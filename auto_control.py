
# auto_control.py - 自动控制引擎

import logging
from datetime import datetime
from typing import Callable, Dict, List, Optional

from config import THRESHOLDS, CONTROL_TOPICS, CONTROL_MIN_ACTION_INTERVAL

logger = logging.getLogger("SmartFarm.Control")


class AutoController:
    """
    自动控制引擎：
    根据传感器数据与预设阈值，自动生成控制指令。
    支持手动模式（手动指令优先，自动模式暂停对应设备）和自动模式。
    """

    def __init__(self):
        self.enabled = True
        self._publish_callback: Optional[Callable] = None
        self._alert_callback: Optional[Callable] = None
        self._control_log: List[Dict] = []
        self._last_actions: Dict[str, Dict] = {}

        # 手动锁定的设备（手动控制期间自动控制不覆盖）
        # device_id -> {"manual": True/False, "expire_time": datetime or None}
        self._manual_overrides: Dict[str, Dict] = {}

        # 手动覆盖超时（秒），超时后自动控制重新接管
        self.manual_timeout = 300  # 5 分钟

        # 设备最近一次动作时间（用于最小动作间隔，抑制设备频繁启停）
        self._last_action_time: Dict[str, datetime] = {}

    def set_publish_callback(self, callback: Callable):
        """设置控制指令发布回调"""
        self._publish_callback = callback

    def set_alert_callback(self, callback: Callable):
        """设置告警回调"""
        self._alert_callback = callback

    def process_sensor_data(self, sensor_data: Dict):
        """
        处理传感器数据，执行自动控制逻辑。

        设计要点（v2.1 修复）：
        1. 规则仲裁：所有规则先生成 (设备, 目标状态, 原因, 优先级) 候选，
           每个设备每轮只执行优先级最高的一条，避免"温度低关通风"与
           "CO2 高开通风"等相反指令在同一轮内先后下发造成设备乱动。
        2. 缺省参数不参与控制：传感器未上报的参数（None）一律跳过，
           不再用默认值臆测（此前 ESP32 只上报部分参数时可能误触发）。
        3. 最小动作间隔：同一设备在 CONTROL_MIN_ACTION_INTERVAL 秒内
           最多切换一次，抑制阈值边界/双数据源交替导致的频繁启停。
        """
        if not self.enabled:
            return

        sensors = sensor_data.get("sensors", {})
        actions = []  # (device, state, reason, priority)

        # ====== 温度控制 ======
        temp = sensors.get("temperature")
        if temp is not None:
            temp_cfg = THRESHOLDS["temperature"]
            if temp > temp_cfg["max"]:
                actions.append(("fan", True, f"温度过高({temp}°C > {temp_cfg['max']}°C)，开启风扇降温", 100))
                actions.append(("ventilation", True, "温度过高，开启通风", 80))
            elif temp < temp_cfg["min"]:
                actions.append(("fan", False, f"温度过低({temp}°C < {temp_cfg['min']}°C)，关闭风扇", 100))
                actions.append(("ventilation", False, "温度过低，关闭通风", 70))

        # ====== 湿度控制 ======
        humidity = sensors.get("humidity")
        if humidity is not None:
            hum_cfg = THRESHOLDS["humidity"]
            if humidity < hum_cfg["min"]:
                actions.append(("water_pump", True, f"湿度过低({humidity}% < {hum_cfg['min']}%)，开启水泵灌溉", 70))
            elif humidity > hum_cfg["max"]:
                actions.append(("water_pump", False, f"湿度过高({humidity}% > {hum_cfg['max']}%)，关闭水泵", 60))

        # ====== 光照控制 ======
        light = sensors.get("light")
        if light is not None:
            light_cfg = THRESHOLDS["light"]
            if light < light_cfg["min"]:
                actions.append(("light_supplement", True, f"光照不足({light}lux < {light_cfg['min']}lux)，开启补光灯", 100))
            elif light > light_cfg["max"]:
                actions.append(("shading", True, f"光照过强({light}lux > {light_cfg['max']}lux)，开启遮阳", 100))
            elif light_cfg["min"] <= light <= light_cfg["max"]:
                actions.append(("light_supplement", False, "光照正常，关闭补光灯", 80))
                actions.append(("shading", False, "光照正常，关闭遮阳", 80))

        # ====== 土壤湿度控制 ======
        soil = sensors.get("soil_moisture")
        if soil is not None:
            soil_cfg = THRESHOLDS["soil_moisture"]
            if soil < soil_cfg["min"]:
                actions.append(("water_pump", True, f"土壤湿度过低({soil}% < {soil_cfg['min']}%)，开启灌溉", 100))
            elif soil > soil_cfg["max"]:
                actions.append(("water_pump", False, f"土壤湿度过高({soil}% > {soil_cfg['max']}%)，关闭灌溉", 90))

        # ====== CO2 控制 ======
        co2 = sensors.get("co2")
        if co2 is not None:
            co2_cfg = THRESHOLDS["co2"]
            if co2 > co2_cfg["max"]:
                actions.append(("ventilation", True, f"CO2浓度过高({co2}ppm > {co2_cfg['max']}ppm)，加强通风", 90))
            elif co2 < co2_cfg["min"]:
                actions.append(("ventilation", False, f"CO2浓度过低({co2}ppm < {co2_cfg['min']}ppm)，减少通风", 60))

        # ====== 规则仲裁：每个设备只执行优先级最高的一条 ======
        # 先按 (设备) 分组，再取组内优先级最高的候选
        best_by_device: Dict[str, tuple] = {}
        for device, state, reason, priority in actions:
            prev = best_by_device.get(device)
            if prev is None or priority > prev[3]:
                best_by_device[device] = (device, state, reason, priority)

        # 执行仲裁结果（跳过手动锁定的设备）
        now = datetime.now()
        for device, state, reason, _priority in best_by_device.values():
            if not self._is_device_manual_locked(device, now):
                self._execute_action(device, state, reason, source="auto")

        # 检查告警
        self._check_alerts(sensors)

    def _is_device_manual_locked(self, device_id: str, now: datetime) -> bool:
        """检查设备是否处于手动锁定状态"""
        override = self._manual_overrides.get(device_id)
        if not override or not override.get("manual"):
            return False
        expire = override.get("expire_time")
        if expire and now > expire:
            # 手动锁定已过期，自动控制重新接管
            override["manual"] = False
            logger.info(f"设备 {device_id} 手动锁定过期，自动控制重新接管")
            return False
        return True

    def _execute_action(self, device: str, state: bool, reason: str, source: str = "auto"):
        """执行控制动作"""
        now = datetime.now()

        # 避免重复发送相同指令（状态未变则跳过）
        if device in self._last_actions:
            last = self._last_actions[device]
            if last["state"] == state:
                return

        # 最小动作间隔：自动控制在间隔内不重复切换同一设备（手动控制不受限）
        if source == "auto":
            last_time = self._last_action_time.get(device)
            if last_time is not None and (now - last_time).total_seconds() < CONTROL_MIN_ACTION_INTERVAL:
                logger.debug(
                    f"设备 {device} 在最小动作间隔内（{CONTROL_MIN_ACTION_INTERVAL}s），"
                    f"跳过自动切换 -> {'开启' if state else '关闭'}"
                )
                return

        log_entry = {
            "timestamp": now.isoformat(),
            "device": device,
            "action": "开启" if state else "关闭",
            "state": state,
            "reason": reason,
            "source": source,
        }
        self._control_log.append(log_entry)
        self._last_actions[device] = {"state": state, "timestamp": now.isoformat()}
        self._last_action_time[device] = now

        mode_tag = "[手动]" if source == "manual" else "[自动]"
        logger.info(f"{mode_tag} {reason}")

        # 发布控制指令
        if self._publish_callback:
            topic = CONTROL_TOPICS.get(device)
            if topic:
                self._publish_callback(topic, {
                    "device": device,
                    "state": state,
                    "action": "on" if state else "off",
                    "reason": reason,
                    "source": source,
                    "timestamp": now.isoformat(),
                })

    def _check_alerts(self, sensors: Dict):
        """检查是否需要发送告警（含昼夜特判：夜间光照低属正常）"""
        hour = datetime.now().hour
        for param, cfg in THRESHOLDS.items():
            value = sensors.get(param)
            if value is None:
                continue

            # 夜间（19:00~07:00）光照低是自然现象，不触发"严重不足"告警
            if param == "light" and (hour >= 19 or hour < 7) and value < cfg["min"] * 0.8:
                logger.debug("夜间光照低，跳过 light 严重不足告警")
                continue

            if value > cfg["max"] * 1.2:
                self._fire_alert({
                    "level": "critical",
                    "param": param,
                    "value": value,
                    "threshold": cfg["max"],
                    "message": f"⚠️ {param}严重超标: {value} (上限: {cfg['max']})",
                })
            elif value < cfg["min"] * 0.8:
                self._fire_alert({
                    "level": "critical",
                    "param": param,
                    "value": value,
                    "threshold": cfg["min"],
                    "message": f"⚠️ {param}严重不足: {value} (下限: {cfg['min']})",
                })

    def _fire_alert(self, alert: dict):
        """触发告警回调"""
        alert["timestamp"] = datetime.now().isoformat()
        logger.warning(f"告警: {alert['message']}")
        if self._alert_callback:
            self._alert_callback(alert)

    def manual_control(self, device: str, state: bool, timeout: int = None):
        """
        手动控制设备。
        手动控制期间，自动控制不会覆盖该设备状态。
        timeout: 手动锁定超时（秒），默认使用 self.manual_timeout
        """
        if timeout is None:
            timeout = self.manual_timeout

        from datetime import timedelta
        # 设置手动锁定
        self._manual_overrides[device] = {
            "manual": True,
            "expire_time": datetime.now() + timedelta(seconds=timeout),
        }
        logger.info(f"手动控制 {device}，自动控制暂停 {timeout}s")

        # 清除之前的自动状态缓存，确保手动指令一定下发
        self._last_actions.pop(device, None)

        reason = f"手动{'开启' if state else '关闭'}{device}"
        self._execute_action(device, state, reason, source="manual")

    def clear_manual_override(self, device: str = None):
        """清除手动锁定，恢复自动控制"""
        if device:
            self._manual_overrides.pop(device, None)
            logger.info(f"设备 {device} 已恢复自动控制")
        else:
            self._manual_overrides.clear()
            logger.info("所有设备已恢复自动控制")

    def get_control_log(self, limit: int = 50) -> List[Dict]:
        """获取控制日志"""
        return self._control_log[-limit:]

    def toggle_enabled(self) -> bool:
        """切换自动控制开关"""
        self.enabled = not self.enabled
        logger.info(f"自动控制已{'启用' if self.enabled else '禁用'}")
        return self.enabled

    def get_manual_overrides(self) -> Dict:
        """获取当前手动锁定的设备列表"""
        now = datetime.now()
        result = {}
        for device_id, override in self._manual_overrides.items():
            if override.get("manual"):
                expire = override.get("expire_time")
                if expire and now > expire:
                    override["manual"] = False
                    continue
                result[device_id] = {
                    "manual": True,
                    "remaining_seconds": int((expire - now).total_seconds()) if expire else None,
                }
        return result
