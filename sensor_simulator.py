# sensor_simulator.py - 传感器数据采集与模拟模块

import logging
import math
import random
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Optional

from config import SENSOR_INTERVAL, THRESHOLDS, SIMULATOR_DAY_CYCLE_SECONDS

logger = logging.getLogger("SmartFarm.Sensor")


class SensorSimulator:
    """
    传感器模拟器：模拟真实IoT传感器的数据输出。
    实际部署时，可替换为读取真实硬件（如DHT22、BH1750等）的模块。
    """

    def __init__(self):
        # 当前传感器状态（模拟值）
        self._values = {
            "temperature": 25.0,
            "humidity": 60.0,
            "light": 15000,
            "soil_moisture": 50.0,
            "soil_ph": 6.5,
            "co2": 600,
        }
        # 设备控制状态
        self._device_status = {
            "fan": False,
            "water_pump": False,
            "light_supplement": False,
            "ventilation": False,
            "shading": False,
        }
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable] = None
        self._lock = threading.Lock()
        self._start_time = time.time()

    def set_callback(self, callback: Callable):
        """设置数据采集回调函数"""
        self._callback = callback

    def update_device_status(self, device: str, status: bool):
        """更新设备控制状态（影响模拟数据）"""
        with self._lock:
            if device in self._device_status:
                self._device_status[device] = status
                logger.info(f"设备状态更新: {device} -> {'开启' if status else '关闭'}")

    def _simulate_environment(self):
        """
        模拟环境变化：
        - 基于时间产生自然波动
        - 设备状态影响环境参数
        """
        elapsed = time.time() - self._start_time

        with self._lock:
            # 基础时间周期（模拟一天24小时压缩为10分钟）
            # 昼夜周期：默认真实一天（86400s），此前压缩为 600s 导致模拟数据
            # 变化过快，与 ESP32 真实数据交替时使设备频繁启停，现已修正。
            day_phase = (elapsed % SIMULATOR_DAY_CYCLE_SECONDS) / float(SIMULATOR_DAY_CYCLE_SECONDS)  # 0~1
            sun_factor = max(0, math.sin(day_phase * math.pi))  # 光照因子

            # 温度：基础值 + 日照影响 + 随机波动 + 设备影响
            base_temp = 20 + 10 * sun_factor
            temp_noise = random.gauss(0, 0.5)
            fan_effect = -2.0 if self._device_status["fan"] else 0
            vent_effect = -1.5 if self._device_status["ventilation"] else 0
            self._values["temperature"] = round(
                base_temp + temp_noise + fan_effect + vent_effect, 1
            )

            # 湿度：与温度负相关 + 水泵影响
            base_humidity = 70 - 15 * sun_factor
            humidity_noise = random.gauss(0, 1.5)
            pump_effect = 5.0 if self._device_status["water_pump"] else 0
            self._values["humidity"] = round(
                max(20, min(95, base_humidity + humidity_noise + pump_effect)), 1
            )

            # 光照：日照周期 + 补光灯影响 + 遮阳影响
            base_light = 40000 * sun_factor
            light_noise = random.gauss(0, 500)
            supplement_light = 10000 if self._device_status["light_supplement"] else 0
            shading_factor = 0.4 if self._device_status["shading"] else 1.0
            self._values["light"] = max(
                0, int((base_light + light_noise + supplement_light) * shading_factor)
            )

            # 土壤湿度：缓慢变化 + 水泵影响
            moisture_drift = random.gauss(-0.1, 0.3)  # 自然蒸发
            pump_moisture = 2.0 if self._device_status["water_pump"] else 0
            self._values["soil_moisture"] = round(
                max(10, min(90, self._values["soil_moisture"] + moisture_drift + pump_moisture)), 1
            )

            # 土壤pH：缓慢漂移
            ph_drift = random.gauss(0, 0.02)
            self._values["soil_ph"] = round(
                max(4.0, min(9.0, self._values["soil_ph"] + ph_drift)), 2
            )

            # CO2浓度：光合作用消耗 + 通风影响
            co2_consumption = 5 * sun_factor  # 光合作用消耗CO2
            co2_generation = random.gauss(2, 1)  # 土壤呼吸产生CO2
            vent_co2 = -20 if self._device_status["ventilation"] else 0
            self._values["co2"] = max(
                300, int(self._values["co2"] - co2_consumption + co2_generation + vent_co2)
            )

    def _collection_loop(self):
        """数据采集循环"""
        logger.info("传感器数据采集已启动")
        while self._running:
            try:
                self._simulate_environment()

                with self._lock:
                    data = {
                        "timestamp": datetime.now().isoformat(),
                        "sensors": self._values.copy(),
                        "devices": self._device_status.copy(),
                    }

                if self._callback:
                    self._callback(data)

            except Exception as e:
                logger.error(f"传感器数据采集异常: {e}")

            time.sleep(SENSOR_INTERVAL)

        logger.info("传感器数据采集已停止")

    def start(self):
        """启动传感器采集"""
        if not self._running:
            self._running = True
            self._start_time = time.time()
            self._thread = threading.Thread(target=self._collection_loop, daemon=True)
            self._thread.start()

    def stop(self):
        """停止传感器采集"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def get_current_values(self) -> Dict:
        """获取当前传感器值"""
        with self._lock:
            return self._values.copy()

    def get_device_status(self) -> Dict:
        """获取设备状态"""
        with self._lock:
            return self._device_status.copy()