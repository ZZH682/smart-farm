# trend_analysis.py - 历史趋势分析模块
#
# 功能：
#   1. 从 SQLite 时序数据读取大棚环境历史数据
#   2. 生成 24 小时变化曲线数据（温度/湿度/光照，按小时聚合）
#   3. 计算温湿度与光照的关联性（皮尔逊相关系数）
#   4. 输出趋势分析结论（文本描述），供 Web 面板与报表使用
#
# 说明：
#   - 本模块只读数据库，不修改任何数据
#   - 关联分析使用皮尔逊相关系数（numpy 实现），范围为 -1 ~ 1
#   - 数据不足时自动降级，返回友好提示而不是报错

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("SmartFarm.Trend")


def pearson_correlation(a: List[float], b: List[float]) -> float:
    """
    计算两个序列的皮尔逊相关系数。

    返回范围 [-1, 1]：
      > 0.7  强正相关
      0.3~0.7 中等正相关
      -0.3~0.3 弱相关/无相关
      -0.7~-0.3 中等负相关
      < -0.7 强负相关
    """
    if len(a) != len(b) or len(a) < 3:
        return 0.0
    n = len(a)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    std_a = math.sqrt(sum((x - mean_a) ** 2 for x in a))
    std_b = math.sqrt(sum((y - mean_b) ** 2 for y in b))
    if std_a == 0 or std_b == 0:
        return 0.0
    return round(cov / (std_a * std_b), 4)


def _describe_correlation(param_a: str, param_b: str, r: float) -> str:
    """根据相关系数生成中文描述"""
    if r >= 0.7:
        strength = "强正相关"
    elif r >= 0.3:
        strength = "中等正相关"
    elif r > -0.3:
        strength = "弱相关"
    elif r > -0.7:
        strength = "中等负相关"
    else:
        strength = "强负相关"
    return f"{param_a}与{param_b}呈{strength}（r={r}）"


class TrendAnalyzer:
    """历史趋势分析器：24小时曲线 + 温湿度/光照关联分析"""

    def __init__(self, storage=None):
        self.storage = storage

    # ==================== 核心分析 ====================

    def analyze_24h(self, hours: int = 24) -> Dict:
        """
        分析最近 N 小时的趋势数据。

        返回结构：
        {
            "success": bool,
            "message": str,
            "time_labels": [str],       # 小时标签，如 "00:00"
            "temperature": [float],      # 每小时平均温度
            "humidity": [float],
            "light": [float],
            "correlations": {
                "temp_hum": r,
                "temp_light": r,
                "hum_light": r,
            },
            "correlation_texts": [str],
            "summary": str,
            "stats": {...},              # 各指标 min/max/avg
            "data_points": int,
        }
        """
        if self.storage is None:
            return {"success": False, "message": "存储模块未初始化"}

        # 1. 读取历史数据（多取一些，便于聚合）
        #    优先最近 hours 小时；若系统停机过导致窗口为空，
        #    则回退到数据库里最新数据的往前 hours 小时窗口
        raw = self.storage.get_recent_sensor_data(hours=hours, limit=100000)
        if not raw:
            latest_ts = self.storage.get_latest_sensor_timestamp()
            if latest_ts:
                try:
                    end_dt = datetime.fromisoformat(latest_ts)
                    start_dt = end_dt - timedelta(hours=hours)
                    raw = self.storage.get_sensor_data_between(
                        start_dt.isoformat(), end_dt.isoformat()
                    )
                except (ValueError, TypeError):
                    raw = []
        if not raw:
            return {
                "success": False,
                "message": "暂无历史数据，请等待系统运行一段时间后重试",
                "time_labels": [], "temperature": [], "humidity": [], "light": [],
                "correlations": {}, "correlation_texts": [], "summary": "",
                "stats": {}, "data_points": 0,
            }

        # 2. 按小时聚合（均值）
        hourly: Dict[str, Dict[str, list]] = {}
        for row in raw:
            ts = row.get("timestamp", "")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
            hour_key = dt.strftime("%m-%d %H:00")
            bucket = hourly.setdefault(hour_key, {"temperature": [], "humidity": [], "light": []})
            if row.get("temperature") is not None:
                bucket["temperature"].append(float(row["temperature"]))
            if row.get("humidity") is not None:
                bucket["humidity"].append(float(row["humidity"]))
            if row.get("light") is not None:
                bucket["light"].append(float(row["light"]))

        if not hourly:
            return {
                "success": False,
                "message": "历史数据格式异常，无法聚合",
                "time_labels": [], "temperature": [], "humidity": [], "light": [],
                "correlations": {}, "correlation_texts": [], "summary": "",
                "stats": {}, "data_points": 0,
            }

        # 3. 排序并生成曲线数据
        sorted_keys = sorted(hourly.keys())
        time_labels, temps, hums, lights = [], [], [], []
        for key in sorted_keys:
            bucket = hourly[key]
            if bucket["temperature"]:
                temps.append(round(sum(bucket["temperature"]) / len(bucket["temperature"]), 1))
            else:
                temps.append(None)
            if bucket["humidity"]:
                hums.append(round(sum(bucket["humidity"]) / len(bucket["humidity"]), 1))
            else:
                hums.append(None)
            if bucket["light"]:
                lights.append(round(sum(bucket["light"]) / len(bucket["light"])))
            else:
                lights.append(None)
            time_labels.append(key)

        # 4. 关联分析（剔除 None 数据后计算）
        pairs = [
            (t, h) for t, h in zip(temps, hums) if t is not None and h is not None
        ]
        temp_light_pairs = [
            (t, l) for t, l in zip(temps, lights) if t is not None and l is not None
        ]
        hum_light_pairs = [
            (h, l) for h, l in zip(hums, lights) if h is not None and l is not None
        ]

        corr_temp_hum = pearson_correlation(
            [p[0] for p in pairs], [p[1] for p in pairs]
        ) if len(pairs) >= 3 else 0.0
        corr_temp_light = pearson_correlation(
            [p[0] for p in temp_light_pairs], [p[1] for p in temp_light_pairs]
        ) if len(temp_light_pairs) >= 3 else 0.0
        corr_hum_light = pearson_correlation(
            [p[0] for p in hum_light_pairs], [p[1] for p in hum_light_pairs]
        ) if len(hum_light_pairs) >= 3 else 0.0

        correlations = {
            "temp_hum": corr_temp_hum,
            "temp_light": corr_temp_light,
            "hum_light": corr_hum_light,
        }

        correlation_texts = [
            _describe_correlation("温度", "湿度", corr_temp_hum),
            _describe_correlation("温度", "光照", corr_temp_light),
            _describe_correlation("湿度", "光照", corr_hum_light),
        ]

        # 5. 统计汇总（有效值）
        def _stats(vals):
            valid = [v for v in vals if v is not None]
            if not valid:
                return None
            return {
                "min": min(valid),
                "max": max(valid),
                "avg": round(sum(valid) / len(valid), 1),
            }

        stats = {
            "temperature": _stats(temps),
            "humidity": _stats(hums),
            "light": _stats(lights),
        }

        # 6. 生成结论摘要
        summary = self._build_summary(correlations, stats, len(time_labels))

        return {
            "success": True,
            "message": "分析成功",
            "time_labels": time_labels,
            "temperature": temps,
            "humidity": hums,
            "light": lights,
            "correlations": correlations,
            "correlation_texts": correlation_texts,
            "summary": summary,
            "stats": stats,
            "data_points": len(time_labels),
        }

    # ==================== 辅助 ====================

    def _build_summary(self, correlations: Dict, stats: Dict, hours: int) -> str:
        """生成趋势分析结论"""
        lines = []
        t_stats = stats.get("temperature")
        h_stats = stats.get("humidity")
        l_stats = stats.get("light")

        if t_stats:
            lines.append(
                f"近{hours}小时温度在 {t_stats['min']}~{t_stats['max']}℃ 之间，"
                f"平均 {t_stats['avg']}℃。"
            )
        if h_stats:
            lines.append(
                f"湿度在 {h_stats['min']}%~{h_stats['max']}% 之间，"
                f"平均 {h_stats['avg']}%。"
            )
        if l_stats:
            lines.append(
                f"光照强度在 {l_stats['min']}~{l_stats['max']} lux 之间，"
                f"平均 {l_stats['avg']} lux。"
            )

        # 关联结论
        r_tl = correlations.get("temp_light", 0)
        r_th = correlations.get("temp_hum", 0)
        if abs(r_tl) >= 0.5:
            direction = "正相关" if r_tl > 0 else "负相关"
            lines.append(
                f"温度与光照呈明显{direction}（r={r_tl}），"
                "说明光照是影响棚内温度的主要因素，"
                "可通过遮阳帘调控光照来调节温度。"
            )
        if abs(r_th) >= 0.5:
            direction = "正相关" if r_th > 0 else "负相关"
            lines.append(
                f"温度与湿度呈明显{direction}（r={r_th}），"
                "可通过通风、加湿等设备联动调控。"
            )

        if not lines:
            lines.append("历史数据不足，暂无法生成分析结论。")
        return " ".join(lines)

    # ==================== 数据导出（AI 训练用） ====================

    def export_training_data(self, hours: int = 72, limit: int = 100000) -> List[Dict]:
        """
        导出历史环境数据用于机器学习训练。

        每条记录包含：
        - timestamp：ISO 时间戳
        - hour：小时（0-23，时间特征）
        - temperature / humidity / light：环境参数
        - soil_moisture / soil_ph / co2：可选扩展参数
        """
        if self.storage is None:
            return []
        raw = self.storage.get_recent_sensor_data(hours=hours, limit=limit)
        if not raw:
            latest_ts = self.storage.get_latest_sensor_timestamp()
            if latest_ts:
                try:
                    end_dt = datetime.fromisoformat(latest_ts)
                    start_dt = end_dt - timedelta(hours=hours)
                    raw = self.storage.get_sensor_data_between(
                        start_dt.isoformat(), end_dt.isoformat(), limit=limit
                    )
                except (ValueError, TypeError):
                    raw = []
        result = []
        for row in raw:
            ts = row.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
            entry = {
                "timestamp": ts,
                "hour": dt.hour,
                "minute": dt.minute,
                "weekday": dt.weekday(),
                "temperature": row.get("temperature"),
                "humidity": row.get("humidity"),
                "light": row.get("light"),
                "soil_moisture": row.get("soil_moisture"),
                "soil_ph": row.get("soil_ph"),
                "co2": row.get("co2"),
            }
            # 跳过关键字段缺失的记录
            if entry["temperature"] is None:
                continue
            result.append(entry)
        return result


# 单例引用（由 main.py 注入）
_analyzer: Optional[TrendAnalyzer] = None


def init_trend_analyzer(storage):
    """初始化趋势分析器（main.py 启动时调用）"""
    global _analyzer
    _analyzer = TrendAnalyzer(storage)
    return _analyzer


def get_analyzer() -> Optional[TrendAnalyzer]:
    return _analyzer
