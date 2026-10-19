# temp_forecast.py - 大棚温度智能预测模块
#
# 功能：
#   1. 从历史环境数据训练轻量级温度预测模型（多项式回归 + 时间特征）
#   2. 预测未来 2 小时大棚温度（每 15 分钟一个预测点，共 8 个点）
#   3. 预测结果通过 MQTT 发布到 farm/greenhouse/forecast/temperature
#   4. 预测温度超出适宜范围时，推送告警到 farm/greenhouse/alert
#   5. 定时自动预测 + Web 手动触发
#
# 说明：
#   - 不依赖 sklearn，使用 numpy 最小二乘法实现多项式回归，开箱即用
#   - 特征：小时(0-23)、分钟、温度滞后项（最近 3 个历史温度）
#   - 数据不足时降级为"滑动平均外推"，保证功能可用

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from config import THRESHOLDS

logger = logging.getLogger("SmartFarm.Forecast")


class TemperatureForecaster:
    """大棚温度预测器：多项式回归 + 滑动平均外推"""

    def __init__(
        self,
        storage=None,
        mqtt_client=None,
        forecast_interval: int = 900,   # 默认每 15 分钟预测一次
        alert_callback: Optional[Callable] = None,
    ):
        self.storage = storage
        self.mqtt = mqtt_client
        self.forecast_interval = forecast_interval
        self.alert_callback = alert_callback

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # 最新预测结果
        self._latest: Optional[Dict] = None

        # MQTT 主题
        from config import TOPIC_PREFIX
        self._topic_forecast = f"{TOPIC_PREFIX}/forecast/temperature"
        self._topic_alert = f"{TOPIC_PREFIX}/alert"

        # 温度适宜范围
        t = THRESHOLDS.get("temperature", {})
        self._temp_min = t.get("min", 18.0)
        self._temp_max = t.get("max", 32.0)

    # ==================== 预测主流程 ====================

    def run_forecast(self) -> Optional[Dict]:
        """执行一次温度预测，返回预测结果"""
        if self.storage is None:
            return None

        # 1. 读取历史温度数据（近 24 小时，窗口空时回退到数据库最新数据）
        raw = self.storage.get_recent_sensor_data(hours=24, limit=100000)
        if not raw:
            latest_ts = self.storage.get_latest_sensor_timestamp()
            if latest_ts:
                try:
                    end_dt = datetime.fromisoformat(latest_ts)
                    start_dt = end_dt - timedelta(hours=24)
                    raw = self.storage.get_sensor_data_between(
                        start_dt.isoformat(), end_dt.isoformat()
                    )
                except (ValueError, TypeError):
                    raw = []
        history = self._extract_temperature_series(raw)
        if not history or len(history) < 5:
            logger.warning("历史温度数据不足，无法预测")
            return None

        # 2. 训练模型 + 预测未来 2 小时
        try:
            future = self._predict(history)
        except Exception as e:
            logger.error(f"温度预测失败: {e}")
            return None

        # 3. 生成结果
        now = datetime.now()
        result = {
            "timestamp": now.isoformat(),
            "current_temp": history[-1][1],
            "forecast_horizon_min": 120,
            "points": future,
            "method": self._method,
            "over_limit": self._check_over_limit(future),
            "temp_range": {"min": self._temp_min, "max": self._temp_max},
        }

        # 4. 发布到 MQTT
        self._publish_forecast(result)

        # 5. 超限告警
        if result["over_limit"] and self.alert_callback:
            try:
                self.alert_callback({
                    "level": "warning",
                    "param": "temperature_forecast",
                    "value": result["points"][-1]["temp"],
                    "threshold": self._temp_max,
                    "message": (
                        f"预测 {result['points'][-1]['time']} 棚内温度将达 "
                        f"{result['points'][-1]['temp']}℃（超过适宜上限 "
                        f"{self._temp_max}℃），建议提前开启通风/遮阳降温"
                    ),
                })
            except Exception as e:
                logger.error(f"温度预测告警失败: {e}")

        with self._lock:
            self._latest = result
        logger.info(
            f"温度预测完成: 当前 {history[-1][1]}℃ → 2小时后 "
            f"{future[-1]['temp']}℃（{self._method}）"
        )
        return result

    # ==================== 数据与模型 ====================

    def _extract_temperature_series(self, raw: List[Dict]) -> List[Tuple[datetime, float]]:
        """提取 (时间, 温度) 序列，按时间排序"""
        series = []
        for row in raw:
            ts = row.get("timestamp", "")
            temp = row.get("temperature")
            if temp is None:
                continue
            try:
                dt = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
            series.append((dt, float(temp)))
        series.sort(key=lambda x: x[0])
        # 去重：同一分钟内取均值
        dedup: Dict[str, List[float]] = {}
        for dt, temp in series:
            dedup.setdefault(dt.strftime("%Y-%m-%d %H:%M"), []).append(temp)
        result = []
        for key, vals in sorted(dedup.items()):
            dt = datetime.strptime(key, "%Y-%m-%d %H:%M")
            result.append((dt, round(sum(vals) / len(vals), 2)))
        return result

    def _predict(self, history: List[Tuple[datetime, float]]) -> List[Dict]:
        """
        预测未来 2 小时（每 15 分钟一个点）。

        策略：
          - 数据量 >= 12：多项式回归（阶数 2）
          - 数据量 5~11：滑动平均外推（趋势项 + 均值回归）

        时间基准：以最后一条历史数据的时间为 T0，
        预测点从 T0+15min 到 T0+120min，保证预测曲线紧接历史曲线。
        （系统实时运行时 T0≈当前时间，即真正的"未来2小时"）
        """
        t_last = history[-1][0]
        # 相对时间特征：T0=最后一条，过去为负（单位小时）
        X = np.array([(dt - t_last).total_seconds() / 3600.0 for dt, _ in history])
        Y = np.array([temp for _, temp in history])
        base_temp = float(Y[-1])

        future_times = [t_last + timedelta(minutes=15 * i) for i in range(1, 9)]

        temps: List[float] = []

        if len(history) >= 12:
            self._method = "多项式回归(2阶)"
            try:
                coeffs = np.polyfit(X, Y, 2)
                temps = [
                    float(np.polyval(coeffs, 0.25 * i)) for i in range(1, 9)
                ]
                # 合理性检查：预测值离谱时回退滑动平均
                if not all(abs(t - base_temp) <= 12.0 for t in temps):
                    logger.warning("多项式外推异常，回退滑动平均")
                    temps = []
            except Exception as e:
                logger.warning(f"多项式回归失败，降级滑动平均: {e}")
                temps = []

        if not temps:
            # 滑动平均外推：最近 3 点趋势 + 整体均值回归
            self._method = "滑动平均外推"
            recent = Y[-3:]
            trend = (recent[-1] - recent[0]) / 2.0 if len(recent) >= 2 else 0.0
            hourly_trend = trend * 0.6
            mean_temp = float(np.mean(Y))
            for i in range(8):
                hours_ahead = 0.25 * (i + 1)
                drift = hourly_trend * hours_ahead
                regress = (mean_temp - base_temp) * 0.1 * (1 - 1 / (1 + hours_ahead))
                temps.append(base_temp + drift + regress)

        # 最终钳制：预测值不偏离当前温度超过 ±12℃（防止模型外推失控）
        temps = [
            max(min(t, base_temp + 12.0), base_temp - 12.0) for t in temps
        ]

        return [
            {"time": ft.strftime("%H:%M"), "temp": round(temp, 1)}
            for ft, temp in zip(future_times, temps)
        ]

    def _check_over_limit(self, points: List[Dict]) -> bool:
        """检查预测温度是否超出适宜范围"""
        for p in points:
            if p["temp"] > self._temp_max or p["temp"] < self._temp_min:
                return True
        return False

    # ==================== MQTT ====================

    def _publish_forecast(self, result: Dict):
        """发布预测结果到 MQTT"""
        if self.mqtt is None:
            return
        # is_connected 是 @property（返回 bool）
        try:
            connected = bool(getattr(self.mqtt, "is_connected", False))
        except Exception:
            connected = False
        if not connected:
            return
        try:
            self.mqtt.publish(self._topic_forecast, result)
            logger.debug(f"已发布温度预测到 {self._topic_forecast}")
        except Exception as e:
            logger.error(f"发布温度预测失败: {e}")

    # ==================== 查询 ====================

    def get_latest_forecast(self) -> Optional[Dict]:
        """获取最新一次预测结果"""
        with self._lock:
            return self._latest

    # ==================== 生命周期 ====================

    def start(self):
        """启动定时预测线程"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"温度预测模块已启动（间隔 {self.forecast_interval}s）")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self):
        while self._running:
            try:
                self.run_forecast()
            except Exception as e:
                logger.error(f"定时预测异常: {e}")
            # 等待下一个周期
            for _ in range(int(self.forecast_interval / 5)):
                if not self._running:
                    return
                time.sleep(5)


# 单例引用（由 main.py 注入）
_forecaster: Optional[TemperatureForecaster] = None


def init_forecaster(forecaster: TemperatureForecaster):
    """初始化温度预测器（main.py 启动时调用）"""
    global _forecaster
    _forecaster = forecaster
    return _forecaster


def get_forecaster() -> Optional[TemperatureForecaster]:
    return _forecaster
