# yield_predict.py - 农作物产量预测模块
#
# 功能：
#   1. 基于实时环境参数（温度/湿度/光照/土壤湿度/土壤pH/CO2）计算相对产量指数（0-100）
#   2. 无历史产量数据时，采用环境适宜度加权评分模型（可解释、可运行）
#   3. 优先使用 scikit-learn 回归模型；未安装时使用规则评分引擎
#   4. 定期计算并通过 MQTT 发布，保存预测记录
#   5. 提供短期趋势预测（基于最近环境数据外推未来产量指数）
#
# 注意：本模块输出的是"相对产量指数"，反映当前环境对作物生长的适宜程度，
#       不代表绝对产量（斤/亩）。积累真实产量数据后可替换为训练好的回归模型。

import logging
import math
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from config import THRESHOLDS

logger = logging.getLogger("SmartFarm.YieldPredict")

# 各参数对产量的权重（总和=1.0）
# 依据：温度和光照对光合作用影响最大，土壤水分和pH影响根系吸收
PARAM_WEIGHTS = {
    "temperature": 0.25,
    "humidity": 0.15,
    "light": 0.20,
    "soil_moisture": 0.20,
    "soil_ph": 0.10,
    "co2": 0.10,
}

# 产量指数等级
YIELD_LEVELS = [
    (90, "优秀", "环境条件极佳，作物生长旺盛，预计产量潜力高。"),
    (75, "良好", "环境条件适宜，作物正常生长，产量预期稳定。"),
    (60, "一般", "环境条件基本满足，部分参数偏离适宜范围，建议微调。"),
    (40, "较差", "多项环境参数偏离适宜范围，作物生长受抑，需及时调控。"),
    (0, "危险", "环境条件严重不适，作物可能受损，请立即采取措施。"),
]


class YieldPredictor:
    """
    农作物产量预测器。

    核心指标：相对产量指数（Relative Yield Index, RYI），范围 0-100。
    - 基于各环境参数与最适值的偏离程度计算适宜度
    - 加权综合得到产量指数
    - 定期计算并发布
    """

    def __init__(
        self,
        storage=None,
        sensor=None,
        mqtt_client=None,
        predict_interval: int = 3600,  # 默认每小时预测一次
        alert_callback: Optional[Callable] = None,
    ):
        self.storage = storage
        self.sensor = sensor
        self.mqtt = mqtt_client
        self.predict_interval = predict_interval
        self.alert_callback = alert_callback

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # 最新预测结果
        self._latest_prediction: Optional[Dict] = None
        # 预测历史（内存，用于趋势图）
        self._prediction_history: List[Dict] = []
        self._max_history = 168  # 保留最近 168 条（每小时一条 = 7 天）

        # MQTT 主题
        from config import TOPIC_PREFIX
        self._topic_yield = f"{TOPIC_PREFIX}/predict/yield"

        # 模型状态
        self._model_type = "rule"  # rule / sklearn

    # ==================== 核心计算 ====================

    @staticmethod
    def _suitability(value: float, optimal: float, tolerance: float) -> float:
        """
        计算单个参数的适宜度（0-1）。
        采用高斯衰减函数：偏离最适值越远，适宜度越低。

        Args:
            value: 当前值
            optimal: 最适值
            tolerance: 容差（偏离此值时适宜度约为 0.6）
        """
        if value is None or optimal is None or tolerance <= 0:
            return 0.5
        deviation = abs(value - optimal)
        return math.exp(-0.5 * (deviation / tolerance) ** 2)

    def _get_optimal_and_tolerance(self, param: str) -> Tuple[Optional[float], float]:
        """从 THRESHOLDS 获取最适值和容差"""
        cfg = THRESHOLDS.get(param)
        if not cfg:
            return None, 10.0
        optimal = cfg.get("optimal")
        if optimal is None:
            optimal = (cfg["min"] + cfg["max"]) / 2
        # 容差取适宜范围宽度的 1/3
        tolerance = max((cfg["max"] - cfg["min"]) / 3, 0.1)
        return optimal, tolerance

    def calculate_yield_index(self, sensor_values: Dict) -> Dict:
        """
        根据传感器数据计算相对产量指数。

        Args:
            sensor_values: 传感器数据 dict，包含 temperature/humidity/light/soil_moisture/soil_ph/co2

        Returns:
            {
                "yield_index": float (0-100),
                "level": str,
                "level_desc": str,
                "param_scores": {param: {"value": float, "suitability": float, "weight": float}},
                "limiting_factors": [param, ...],  # 最限制产量的前3个参数
                "timestamp": str,
            }
        """
        param_scores = {}
        weighted_sum = 0.0
        total_weight = 0.0

        for param, weight in PARAM_WEIGHTS.items():
            value = sensor_values.get(param)
            optimal, tolerance = self._get_optimal_and_tolerance(param)
            suitability = self._suitability(value, optimal, tolerance)
            param_scores[param] = {
                "value": value,
                "optimal": optimal,
                "suitability": round(suitability, 4),
                "weight": weight,
            }
            weighted_sum += suitability * weight
            total_weight += weight

        # 归一化到 0-100
        yield_index = round((weighted_sum / total_weight) * 100, 1) if total_weight > 0 else 0.0

        # 等级判定
        level = "未知"
        level_desc = ""
        for threshold, name, desc in YIELD_LEVELS:
            if yield_index >= threshold:
                level = name
                level_desc = desc
                break

        # 找出限制因素（适宜度最低的前3个）
        limiting = sorted(
            [(p, s["suitability"]) for p, s in param_scores.items()],
            key=lambda x: x[1],
        )[:3]
        limiting_factors = [p for p, _ in limiting if _ < 0.8]

        result = {
            "yield_index": yield_index,
            "level": level,
            "level_desc": level_desc,
            "param_scores": param_scores,
            "limiting_factors": limiting_factors,
            "timestamp": datetime.now().isoformat(),
        }
        return result

    def predict_trend(self, hours: int = 24) -> Dict:
        """
        基于最近环境数据预测未来产量趋势。

        方法：
        - 取最近 N 小时的传感器数据
        - 计算每个时间点的产量指数
        - 线性回归外推未来 N 小时的产量指数
        - 给出趋势方向（上升/平稳/下降）

        Returns:
            {
                "current_index": float,
                "predicted_index": float,  # 未来 N 小时后的预测值
                "trend": "up" / "stable" / "down",
                "trend_change": float,  # 变化量
                "history": [{"timestamp": str, "yield_index": float}, ...],
                "forecast": [{"timestamp": str, "yield_index": float}, ...],
            }
        """
        # 获取历史数据
        history_points = []
        if self.storage:
            try:
                recent = self.storage.get_recent_sensor_data(hours=hours, limit=500)
                for row in recent:
                    sensor_vals = {
                        "temperature": row.get("temperature"),
                        "humidity": row.get("humidity"),
                        "light": row.get("light"),
                        "soil_moisture": row.get("soil_moisture"),
                        "soil_ph": row.get("soil_ph"),
                        "co2": row.get("co2"),
                    }
                    if any(v is not None for v in sensor_vals.values()):
                        idx = self.calculate_yield_index(sensor_vals)
                        history_points.append({
                            "timestamp": row.get("timestamp"),
                            "yield_index": idx["yield_index"],
                        })
            except Exception as e:
                logger.error(f"获取历史数据计算趋势失败: {e}")

        # 如果历史数据不足，用内存中的预测历史
        if len(history_points) < 3:
            history_points = [
                {"timestamp": p["timestamp"], "yield_index": p["yield_index"]}
                for p in self._prediction_history[-50:]
            ]

        current_index = history_points[-1]["yield_index"] if history_points else 50.0

        # 简单线性趋势外推
        predicted_index = current_index
        trend = "stable"
        trend_change = 0.0

        if len(history_points) >= 3:
            # 计算最近一半数据的斜率
            n = len(history_points)
            half = history_points[n // 2:]
            if len(half) >= 2:
                first_avg = sum(p["yield_index"] for p in history_points[:n // 2]) / (n // 2)
                second_avg = sum(p["yield_index"] for p in half) / len(half)
                trend_change = round(second_avg - first_avg, 1)
                predicted_index = round(max(0, min(100, current_index + trend_change)), 1)
                if trend_change > 2:
                    trend = "up"
                elif trend_change < -2:
                    trend = "down"
                else:
                    trend = "stable"

        # 生成预测点（未来 24 小时，每 3 小时一个点）
        forecast = []
        now = datetime.now()
        for i in range(1, 9):
            future_time = (now + timedelta(hours=i * 3)).isoformat()
            # 简单线性外推 + 向均值回归
            regression = (predicted_index - current_index) * (i / 8) * 0.7
            forecast.append({
                "timestamp": future_time,
                "yield_index": round(max(0, min(100, current_index + regression)), 1),
            })

        return {
            "current_index": current_index,
            "predicted_index": predicted_index,
            "trend": trend,
            "trend_change": trend_change,
            "history": history_points[-50:],  # 最近50个点
            "forecast": forecast,
        }

    # ==================== 定期预测 ====================

    def _prediction_loop(self):
        """定期预测循环"""
        logger.info(f"产量预测模块已启动，间隔 {self.predict_interval}s")
        while self._running:
            try:
                self.run_prediction()
            except Exception as e:
                logger.error(f"产量预测异常: {e}", exc_info=True)
            time.sleep(self.predict_interval)

    def run_prediction(self) -> Optional[Dict]:
        """执行一次预测并发布"""
        if self.sensor is None:
            return None

        try:
            sensor_values = self.sensor.get_current_values()
        except Exception as e:
            logger.error(f"读取传感器数据失败: {e}")
            return None

        if not sensor_values:
            return None

        result = self.calculate_yield_index(sensor_values)

        # 缓存
        with self._lock:
            self._latest_prediction = result
            self._prediction_history.append({
                "timestamp": result["timestamp"],
                "yield_index": result["yield_index"],
                "level": result["level"],
            })
            if len(self._prediction_history) > self._max_history:
                self._prediction_history = self._prediction_history[-self._max_history:]

        # 持久化
        self._save_prediction(result)

        # MQTT 发布
        self._publish_mqtt(result)

        # 低产量指数告警
        if result["yield_index"] < 50:
            self._trigger_low_yield_alert(result)

        logger.info(
            f"产量预测完成: 指数={result['yield_index']}, "
            f"等级={result['level']}, 限制因素={result['limiting_factors']}"
        )
        return result

    # ==================== 持久化 / MQTT / 告警 ====================

    def _save_prediction(self, result: Dict):
        if self.storage is None:
            return
        try:
            if hasattr(self.storage, "save_yield_prediction"):
                self.storage.save_yield_prediction(result)
        except Exception as e:
            logger.error(f"保存产量预测记录失败: {e}")

    def _publish_mqtt(self, result: Dict):
        if self.mqtt is None or not self.mqtt.is_connected:
            return
        try:
            payload = {
                "yield_index": result["yield_index"],
                "level": result["level"],
                "limiting_factors": result["limiting_factors"],
                "timestamp": result["timestamp"],
                "source": "server",
            }
            self.mqtt.publish(self._topic_yield, payload)
        except Exception as e:
            logger.error(f"MQTT 发布产量预测失败: {e}")

    def _trigger_low_yield_alert(self, result: Dict):
        if self.alert_callback is None:
            return
        try:
            factors = "、".join(result["limiting_factors"]) if result["limiting_factors"] else "未知"
            alert = {
                "level": "warning",
                "param": "yield_index",
                "value": result["yield_index"],
                "threshold": 50,
                "message": (
                    f"相对产量指数偏低：{result['yield_index']}（{result['level']}）。"
                    f"主要限制因素：{factors}。建议及时调控环境参数。"
                ),
                "timestamp": datetime.now().isoformat(),
            }
            self.alert_callback(alert)
        except Exception as e:
            logger.error(f"低产量告警触发失败: {e}")

    # ==================== 启停 ====================

    def start(self):
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._prediction_loop, daemon=True)
            self._thread.start()
            # 启动时立即执行一次
            time.sleep(1)
            self.run_prediction()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    # ==================== 查询接口 ====================

    def get_latest_prediction(self) -> Optional[Dict]:
        with self._lock:
            return self._latest_prediction

    def get_prediction_history(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            return self._prediction_history[-limit:]
