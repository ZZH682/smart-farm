# data_storage.py - 数据存储模块（SQLite）

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from config import DATABASE_PATH

logger = logging.getLogger("SmartFarm.Storage")


class DataStorage:
    """使用SQLite存储传感器数据、控制日志、告警记录、病虫害识别和产量预测"""

    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        # 线程本地连接缓存：同一线程内复用连接，避免每次操作新建连接
        self._local = threading.local()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """获取线程本地连接（复用，不随每次调用新建/关闭）"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _release_connection(self, conn: sqlite3.Connection) -> None:
        """归还连接（线程本地缓存复用，无需真正关闭）。
        所有方法 commit 后事务已干净，可安全复用。"""
        pass

    def close_all(self):
        """关闭所有线程本地连接（系统停止时调用）"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                self._release_connection(conn)

            except Exception:
                pass
            self._local.conn = None

    def _init_db(self):
        """初始化数据库表"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sensor_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    temperature REAL,
                    humidity REAL,
                    light INTEGER,
                    soil_moisture REAL,
                    soil_ph REAL,
                    co2 INTEGER,
                    devices TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS control_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    device TEXT NOT NULL,
                    action TEXT NOT NULL,
                    state INTEGER,
                    reason TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    param TEXT NOT NULL,
                    value REAL,
                    threshold REAL,
                    message TEXT
                )
            """)

            # 病虫害识别记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pest_detections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    engine TEXT,
                    source TEXT,
                    healthy INTEGER,
                    primary_pest TEXT,
                    max_confidence REAL,
                    detection_count INTEGER,
                    image_path TEXT,
                    details TEXT
                )
            """)

            # 产量预测记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS yield_predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    yield_index REAL,
                    level TEXT,
                    limiting_factors TEXT,
                    param_scores TEXT
                )
            """)

            # 系统设置表（阈值等可持久化配置，key -> JSON value）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT
                )
            """)

            # 创建索引以加速查询
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_sensor_timestamp ON sensor_data(timestamp)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_control_timestamp ON control_log(timestamp)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_alert_timestamp ON alerts(timestamp)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_pest_timestamp ON pest_detections(timestamp)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_yield_timestamp ON yield_predictions(timestamp)"
            )

            conn.commit()
            self._release_connection(conn)

            logger.info("数据库初始化完成")

    def save_sensor_data(self, data: Dict):
        """保存传感器数据"""
        sensors = data.get("sensors", {})
        devices = data.get("devices", {})

        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO sensor_data
                (timestamp, temperature, humidity, light, soil_moisture, soil_ph, co2, devices)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get("timestamp", datetime.now().isoformat()),
                sensors.get("temperature"),
                sensors.get("humidity"),
                sensors.get("light"),
                sensors.get("soil_moisture"),
                sensors.get("soil_ph"),
                sensors.get("co2"),
                json.dumps(devices),
            ))
            conn.commit()
            self._release_connection(conn)


    def save_control_log(self, log_entry: Dict):
        """保存控制日志"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO control_log (timestamp, device, action, state, reason)
                VALUES (?, ?, ?, ?, ?)
            """, (
                log_entry.get("timestamp"),
                log_entry.get("device"),
                log_entry.get("action"),
                1 if log_entry.get("state") else 0,
                log_entry.get("reason"),
            ))
            conn.commit()
            self._release_connection(conn)


    def save_alert(self, alert: Dict):
        """保存告警记录"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO alerts (timestamp, level, param, value, threshold, message)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                alert.get("timestamp"),
                alert.get("level"),
                alert.get("param"),
                alert.get("value"),
                alert.get("threshold"),
                alert.get("message"),
            ))
            conn.commit()
            self._release_connection(conn)


    # ==================== 病虫害识别 ====================

    def save_pest_detection(self, result: Dict):
        """保存病虫害识别记录"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            summary = result.get("summary", {})
            cursor.execute("""
                INSERT INTO pest_detections
                (timestamp, engine, source, healthy, primary_pest, max_confidence,
                 detection_count, image_path, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                result.get("timestamp", datetime.now().isoformat()),
                result.get("engine"),
                result.get("source"),
                1 if summary.get("healthy") else 0,
                summary.get("primary_pest"),
                summary.get("max_confidence"),
                len(result.get("detections", [])),
                result.get("image_path"),
                json.dumps(result.get("detections", []), ensure_ascii=False),
            ))
            conn.commit()
            self._release_connection(conn)


    def get_recent_pest_detections(self, limit: int = 20) -> List[Dict]:
        """获取最近的病虫害识别记录"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM pest_detections ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            rows = cursor.fetchall()
            self._release_connection(conn)

            results = []
            for row in rows:
                d = dict(row)
                d["healthy"] = bool(d.get("healthy"))
                try:
                    d["details"] = json.loads(d.get("details", "[]"))
                except (json.JSONDecodeError, TypeError):
                    d["details"] = []
                results.append(d)
            return results

    # ==================== 产量预测 ====================

    def save_yield_prediction(self, result: Dict):
        """保存产量预测记录"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO yield_predictions
                (timestamp, yield_index, level, limiting_factors, param_scores)
                VALUES (?, ?, ?, ?, ?)
            """, (
                result.get("timestamp", datetime.now().isoformat()),
                result.get("yield_index"),
                result.get("level"),
                json.dumps(result.get("limiting_factors", []), ensure_ascii=False),
                json.dumps(result.get("param_scores", {}), ensure_ascii=False),
            ))
            conn.commit()
            self._release_connection(conn)


    def get_recent_yield_predictions(self, limit: int = 50) -> List[Dict]:
        """获取最近的产量预测记录"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM yield_predictions ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            rows = cursor.fetchall()
            self._release_connection(conn)

            results = []
            for row in rows:
                d = dict(row)
                try:
                    d["limiting_factors"] = json.loads(d.get("limiting_factors", "[]"))
                except (json.JSONDecodeError, TypeError):
                    d["limiting_factors"] = []
                try:
                    d["param_scores"] = json.loads(d.get("param_scores", "{}"))
                except (json.JSONDecodeError, TypeError):
                    d["param_scores"] = {}
                results.append(d)
            return results

    def get_latest_yield_prediction(self) -> Optional[Dict]:
        """获取最新一条产量预测"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM yield_predictions ORDER BY timestamp DESC LIMIT 1"
            )
            row = cursor.fetchone()
            self._release_connection(conn)

            if not row:
                return None
            d = dict(row)
            try:
                d["limiting_factors"] = json.loads(d.get("limiting_factors", "[]"))
            except (json.JSONDecodeError, TypeError):
                d["limiting_factors"] = []
            try:
                d["param_scores"] = json.loads(d.get("param_scores", "{}"))
            except (json.JSONDecodeError, TypeError):
                d["param_scores"] = {}
            return d

    # ==================== 查询方法 ====================

    def get_recent_sensor_data(self, hours: int = 1, limit: int = 500) -> List[Dict]:
        """获取最近的传感器数据"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            since = (datetime.now() - timedelta(hours=hours)).isoformat()
            cursor.execute("""
                SELECT * FROM sensor_data
                WHERE timestamp > ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (since, limit))
            rows = cursor.fetchall()
            self._release_connection(conn)

            return [dict(row) for row in reversed(rows)]

    def get_sensor_data_between(self, start: str, end: str, limit: int = 100000) -> List[Dict]:
        """按时间区间查询传感器数据（start <= timestamp <= end）"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM sensor_data
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC
                LIMIT ?
            """, (start, end, limit))
            rows = cursor.fetchall()
            self._release_connection(conn)

            return [dict(row) for row in rows]

    def get_latest_sensor_data(self) -> Optional[Dict]:
        """获取最新一条传感器数据"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM sensor_data ORDER BY timestamp DESC LIMIT 1"
            )
            row = cursor.fetchone()
            self._release_connection(conn)

            return dict(row) if row else None

    def get_latest_sensor_timestamp(self) -> Optional[str]:
        """获取最新一条传感器数据的 timestamp（用于数据窗口回退）"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT timestamp FROM sensor_data ORDER BY timestamp DESC LIMIT 1"
            )
            row = cursor.fetchone()
            self._release_connection(conn)

            return row["timestamp"] if row else None

    def get_recent_alerts(self, limit: int = 20) -> List[Dict]:
        """获取最近的告警"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            rows = cursor.fetchall()
            self._release_connection(conn)

            return [dict(row) for row in rows]

    def get_alerts_between(self, start: str, end: str, limit: int = 100000) -> List[Dict]:
        """按时间区间查询告警（start <= timestamp <= end）"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM alerts
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC
                LIMIT ?
            """, (start, end, limit))
            rows = cursor.fetchall()
            self._release_connection(conn)

            return [dict(row) for row in rows]

    def get_recent_control_log(self, limit: int = 50) -> List[Dict]:
        """获取最近的控制日志"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM control_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            rows = cursor.fetchall()
            self._release_connection(conn)

            return [dict(row) for row in rows]

    def get_control_log_between(self, start: str, end: str, limit: int = 100000) -> List[Dict]:
        """按时间区间查询控制日志（start <= timestamp <= end）"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM control_log
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC
                LIMIT ?
            """, (start, end, limit))
            rows = cursor.fetchall()
            self._release_connection(conn)

            return [dict(row) for row in rows]

    def get_latest_control_timestamp(self) -> Optional[str]:
        """获取最新一条控制日志的 timestamp"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT timestamp FROM control_log ORDER BY timestamp DESC LIMIT 1"
            )
            row = cursor.fetchone()
            self._release_connection(conn)

            return row["timestamp"] if row else None

    def get_latest_alert_timestamp(self) -> Optional[str]:
        """获取最新一条告警的 timestamp"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT timestamp FROM alerts ORDER BY timestamp DESC LIMIT 1"
            )
            row = cursor.fetchone()
            self._release_connection(conn)

            return row["timestamp"] if row else None

    def get_statistics(self, hours: int = 24) -> Dict:
        """获取统计数据"""
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()

            stats = {}
            for param in ["temperature", "humidity", "light", "soil_moisture", "co2"]:
                cursor.execute(f"""
                    SELECT
                        MIN({param}) as min_val,
                        MAX({param}) as max_val,
                        AVG({param}) as avg_val
                    FROM sensor_data
                    WHERE timestamp > ?
                """, (since,))
                row = cursor.fetchone()
                if row and row["min_val"] is not None:
                    stats[param] = {
                        "min": round(row["min_val"], 1),
                        "max": round(row["max_val"], 1),
                        "avg": round(row["avg_val"], 1),
                    }

            self._release_connection(conn)

            return stats

    # ==================== 系统设置（阈值等持久化） ====================

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """读取系统设置（JSON 字符串）"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            self._release_connection(conn)
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """写入系统设置（JSON 字符串，upsert）"""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                updated_at = excluded.updated_at
            """, (key, value, datetime.now().isoformat()))
            conn.commit()
            self._release_connection(conn)

    # ==================== 历史数据清理 ====================

    def cleanup_old_data(self, retention_days: int = 90) -> Dict:
        """删除超过保留天数的历史数据（传感器/控制日志/告警）"""
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        result = {}
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            for table in ("sensor_data", "control_log", "alerts"):
                cursor.execute(
                    f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,)
                )
                result[table] = cursor.rowcount
            conn.commit()
            self._release_connection(conn)
        logger.info(f"历史数据清理完成（保留 {retention_days} 天）: {result}")
        return result

    def start_cleanup(self, retention_days: int = 90, interval_hours: int = 24) -> None:
        """启动后台数据清理线程（每日执行一次）"""
        def _cleanup_loop():
            logger.info(f"数据清理线程已启动（保留 {retention_days} 天，每 {interval_hours} 小时检查）")
            while True:
                try:
                    self.cleanup_old_data(retention_days)
                except Exception as e:
                    logger.error(f"历史数据清理失败: {e}")
                time.sleep(interval_hours * 3600)

        t = threading.Thread(target=_cleanup_loop, daemon=True)
        t.start()
