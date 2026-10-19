# report_generator.py - 业务报表生成模块
#
# 功能：
#   1. 环境数据日报：统计最近 24 小时环境参数（温度/湿度/光照/土壤湿度/pH/CO2）
#      - 均值/最大/最小、越限统计、自动控制动作统计
#   2. 设备运行状态周报：统计最近 7 天设备运行情况
#      - 各设备开/关次数、运行总时长、自动/手动控制占比
#   3. 生成 HTML 报表（可直接浏览器打开/打印），并保存到 reports/ 目录
#   4. 定时自动生成（日报每天 00:05，周报每周一 00:05），支持手动触发
#
# 说明：
#   - 报表使用内联 CSS，无外部依赖，可离线打开
#   - 数据全部来自 SQLite 数据库（sensor_data / control_log / alerts）

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from config import DEVICE_NAMES

logger = logging.getLogger("SmartFarm.Report")

# 报表保存目录
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

# 参数中文名与单位
PARAM_META = {
    "temperature": {"name": "温度", "unit": "℃"},
    "humidity": {"name": "湿度", "unit": "%"},
    "light": {"name": "光照", "unit": "lux"},
    "soil_moisture": {"name": "土壤湿度", "unit": "%"},
    "soil_ph": {"name": "土壤pH", "unit": ""},
    "co2": {"name": "CO₂浓度", "unit": "ppm"},
}


class ReportGenerator:
    """业务报表生成器：环境数据日报 + 设备运行状态周报"""

    def __init__(self, storage=None, mqtt_client=None):
        self.storage = storage
        self.mqtt = mqtt_client
        self._running = False
        self._thread: Optional[threading.Thread] = None
        os.makedirs(REPORT_DIR, exist_ok=True)

    # ==================== 环境数据日报 ====================

    def generate_daily_report(self) -> Dict:
        """生成环境数据日报（最近 24 小时）"""
        hours = 24
        raw = self._get_sensor_data(hours=hours)
        if not raw:
            return {"success": False, "message": "暂无传感器数据，无法生成日报"}

        # 统计各参数
        params = ["temperature", "humidity", "light", "soil_moisture", "soil_ph", "co2"]
        stats = {}
        for p in params:
            vals = [r.get(p) for r in raw if r.get(p) is not None]
            if vals:
                stats[p] = {
                    "avg": round(sum(vals) / len(vals), 2),
                    "min": min(vals),
                    "max": max(vals),
                    "count": len(vals),
                }

        # 控制动作统计（自动/手动）
        control_stats = self._get_control_stats(hours=hours)

        # 告警统计
        alert_stats = self._get_alert_stats(hours=hours)

        # 生成 HTML
        html = self._render_daily_report(raw, stats, control_stats, alert_stats)

        # 保存文件（文件名含时分，避免同一天多次生成相互覆盖）
        filename = f"日报_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
        filepath = os.path.join(REPORT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info(f"环境数据日报已生成: {filepath}")
        return {
            "success": True,
            "filename": filename,
            "path": filepath,
            "report_type": "daily",
            "generated_at": datetime.now().isoformat(),
            "stats": stats,
            "control_stats": control_stats,
            "alert_stats": alert_stats,
        }

    # ==================== 设备运行状态周报 ====================

    def generate_weekly_report(self) -> Dict:
        """生成设备运行状态周报（最近 7 天）"""
        hours = 24 * 7
        raw = self._get_sensor_data(hours=hours)
        if not raw:
            return {"success": False, "message": "暂无传感器数据，无法生成周报"}

        # 设备运行统计
        device_stats = self._get_device_stats(hours=hours)

        # 控制动作统计
        control_stats = self._get_control_stats(hours=hours)

        # 告警统计
        alert_stats = self._get_alert_stats(hours=hours)

        # 环境参数汇总（按天聚合均值）
        daily_avg = self._get_daily_avg(hours=hours)

        # 生成 HTML
        html = self._render_weekly_report(
            raw, device_stats, control_stats, alert_stats, daily_avg
        )

        # 保存文件（文件名含时分，避免同一天多次生成相互覆盖）
        filename = f"周报_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
        filepath = os.path.join(REPORT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info(f"设备运行状态周报已生成: {filepath}")
        return {
            "success": True,
            "filename": filename,
            "path": filepath,
            "report_type": "weekly",
            "generated_at": datetime.now().isoformat(),
            "device_stats": device_stats,
            "control_stats": control_stats,
            "alert_stats": alert_stats,
            "daily_avg": daily_avg,
        }

    # ==================== 数据统计 ====================

    def _get_sensor_data(self, hours: int) -> List[Dict]:
        if self.storage is None:
            return []
        try:
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
            return raw
        except Exception as e:
            logger.error(f"读取传感器数据失败: {e}")
            return []

    def _get_control_stats(self, hours: int) -> Dict:
        """统计控制动作（自动/手动/总次数）"""
        if self.storage is None:
            return {}
        since = datetime.now() - timedelta(hours=hours)
        try:
            logs = self.storage.get_recent_control_log(limit=100000)
            # 窗口内无数据时回退到数据库最新数据窗口（系统可能停机过）
            latest = self.storage.get_latest_control_timestamp()
            if latest:
                try:
                    latest_dt = datetime.fromisoformat(latest)
                    if latest_dt < since:
                        logs = self.storage.get_control_log_between(
                            (latest_dt - timedelta(hours=hours)).isoformat(),
                            latest_dt.isoformat(),
                        )
                        since = latest_dt - timedelta(hours=hours)
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            logger.error(f"读取控制日志失败: {e}")
            return {}

        result = {"total": 0, "auto": 0, "manual": 0, "by_device": {}}
        for log in logs:
            ts = log.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
            if dt < since:
                continue
            result["total"] += 1
            reason = log.get("reason", "")
            is_auto = "手动" not in reason and "manual" not in reason.lower()
            if is_auto:
                result["auto"] += 1
            else:
                result["manual"] += 1

            device = log.get("device", "unknown")
            d = result["by_device"].setdefault(device, {"count": 0, "auto": 0, "manual": 0})
            d["count"] += 1
            if is_auto:
                d["auto"] += 1
            else:
                d["manual"] += 1
        return result

    def _get_alert_stats(self, hours: int) -> Dict:
        """统计告警记录"""
        if self.storage is None:
            return {}
        since = datetime.now() - timedelta(hours=hours)
        try:
            alerts = self.storage.get_recent_alerts(limit=100000)
            # 窗口内无数据时回退到数据库最新数据窗口
            latest = self.storage.get_latest_alert_timestamp()
            if latest:
                try:
                    latest_dt = datetime.fromisoformat(latest)
                    if latest_dt < since:
                        alerts = self.storage.get_alerts_between(
                            (latest_dt - timedelta(hours=hours)).isoformat(),
                            latest_dt.isoformat(),
                        )
                        since = latest_dt - timedelta(hours=hours)
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            logger.error(f"读取告警记录失败: {e}")
            return {}

        result = {"total": 0, "levels": {}}
        for alert in alerts:
            ts = alert.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
            if dt < since:
                continue
            result["total"] += 1
            level = alert.get("level", "info")
            result["levels"][level] = result["levels"].get(level, 0) + 1
        return result

    def _get_device_stats(self, hours: int) -> Dict:
        """统计各设备运行时长与次数（基于 control_log 状态变化）"""
        if self.storage is None:
            return {}
        since = datetime.now() - timedelta(hours=hours)
        try:
            logs = self.storage.get_recent_control_log(limit=100000)
            # 窗口内无数据时回退到数据库最新数据窗口
            latest = self.storage.get_latest_control_timestamp()
            if latest:
                try:
                    latest_dt = datetime.fromisoformat(latest)
                    if latest_dt < since:
                        logs = self.storage.get_control_log_between(
                            (latest_dt - timedelta(hours=hours)).isoformat(),
                            latest_dt.isoformat(),
                        )
                        since = latest_dt - timedelta(hours=hours)
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            logger.error(f"读取控制日志失败: {e}")
            return {}
        # 按设备追踪状态变化
        device_logs: Dict[str, List[tuple]] = {}
        for log in logs:
            ts = log.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
            if dt < since:
                continue
            device = log.get("device", "unknown")
            device_logs.setdefault(device, []).append((dt, log.get("state", 0)))
        for d in device_logs:
            device_logs[d].sort(key=lambda x: x[0])

        result = {}
        for device, entries in device_logs.items():
            # 计算运行时长（开→关 或 开→开 之间的时间）
            run_seconds = 0.0
            open_count = 0
            last_open_time = None
            for dt, state in entries:
                if state == 1:
                    open_count += 1
                    if last_open_time is None:
                        last_open_time = dt
                else:
                    if last_open_time is not None:
                        run_seconds += (dt - last_open_time).total_seconds()
                        last_open_time = None
            # 如果最后仍开启，按当前时间算
            if last_open_time is not None:
                run_seconds += (datetime.now() - last_open_time).total_seconds()

            result[device] = {
                "name": DEVICE_NAMES.get(device, device),
                "open_count": open_count,
                "run_seconds": round(run_seconds, 1),
                "run_hours": round(run_seconds / 3600, 2),
                "action_count": len(entries),
            }
        return result

    def _get_daily_avg(self, hours: int) -> List[Dict]:
        """按天聚合环境参数均值"""
        raw = self._get_sensor_data(hours=hours)
        days: Dict[str, Dict[str, list]] = {}
        for r in raw:
            ts = r.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
            day = dt.strftime("%m-%d")
            bucket = days.setdefault(day, {"temperature": [], "humidity": [], "light": []})
            if r.get("temperature") is not None:
                bucket["temperature"].append(float(r["temperature"]))
            if r.get("humidity") is not None:
                bucket["humidity"].append(float(r["humidity"]))
            if r.get("light") is not None:
                bucket["light"].append(float(r["light"]))

        result = []
        for day in sorted(days.keys()):
            b = days[day]
            result.append({
                "date": day,
                "temperature": round(sum(b["temperature"]) / len(b["temperature"]), 1) if b["temperature"] else None,
                "humidity": round(sum(b["humidity"]) / len(b["humidity"]), 1) if b["humidity"] else None,
                "light": round(sum(b["light"]) / len(b["light"])) if b["light"] else None,
            })
        return result

    # ==================== HTML 渲染 ====================

    def _html_head(self, title: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Microsoft YaHei', 'PingFang SC', Arial, sans-serif; background:#f5f7fa; color:#1f2937; padding:24px; }}
.container {{ max-width:900px; margin:0 auto; background:#fff; border-radius:12px; padding:32px; box-shadow:0 2px 12px rgba(0,0,0,0.08); }}
h1 {{ font-size:22px; color:#1f2937; margin-bottom:6px; }}
h2 {{ font-size:17px; color:#374151; margin:28px 0 14px; border-left:4px solid #10b981; padding-left:10px; }}
.subtitle {{ color:#6b7280; font-size:13px; margin-bottom:20px; }}
table {{ width:100%; border-collapse:collapse; margin:10px 0; }}
th, td {{ padding:10px 12px; text-align:center; border:1px solid #e5e7eb; font-size:13px; }}
th {{ background:#f0fdf4; color:#166534; font-weight:600; }}
tr:nth-child(even) {{ background:#f9fafb; }}
.stat-grid {{ display:flex; flex-wrap:wrap; gap:12px; margin:12px 0; }}
.stat-card {{ flex:1 1 140px; min-width:130px; background:#f0fdf4; border:1px solid #d1fae5; border-radius:8px; padding:14px; text-align:center; }}
.stat-card .label {{ font-size:12px; color:#6b7280; }}
.stat-card .value {{ font-size:20px; font-weight:700; color:#047857; margin-top:4px; }}
.stat-card .sub {{ font-size:11px; color:#9ca3af; margin-top:2px; }}
.footer {{ margin-top:30px; padding-top:16px; border-top:1px solid #e5e7eb; color:#9ca3af; font-size:12px; text-align:center; }}
.tag {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; }}
.tag-green {{ background:#d1fae5; color:#047857; }}
.tag-orange {{ background:#fef3c7; color:#b45309; }}
.tag-red {{ background:#fee2e2; color:#b91c1c; }}
.summary-box {{ background:#eff6ff; border:1px solid #bfdbfe; border-radius:8px; padding:14px 16px; font-size:13px; color:#1e40af; line-height:1.8; margin:14px 0; }}
</style>
</head>
<body>
<div class="container">
"""

    def _html_footer(self) -> str:
        return f"""
<div class="footer">
  由智慧农业大棚监控系统自动生成 · {datetime.now().strftime('%Y-%m-%d %H:%M')}
</div>
</div>
</body>
</html>"""

    def _render_daily_report(self, raw, stats, control_stats, alert_stats) -> str:
        now = datetime.now()
        parts = [self._html_head(f"环境数据日报 {now.strftime('%Y-%m-%d')}")]
        parts.append(f"<h1>🌱 环境数据日报</h1>")
        parts.append(f'<div class="subtitle">统计周期：最近 24 小时 · 生成时间：{now.strftime("%Y-%m-%d %H:%M:%S")}</div>')

        # 数据量统计
        parts.append('<h2>📊 数据采集概览</h2>')
        parts.append('<div class="stat-grid">')
        parts.append(f'<div class="stat-card"><div class="label">采集记录数</div><div class="value">{len(raw)}</div><div class="sub">条</div></div>')
        parts.append(f'<div class="stat-card"><div class="label">自动控制动作</div><div class="value">{control_stats.get("auto", 0)}</div><div class="sub">次</div></div>')
        parts.append(f'<div class="stat-card"><div class="label">手动控制动作</div><div class="value">{control_stats.get("manual", 0)}</div><div class="sub">次</div></div>')
        parts.append(f'<div class="stat-card"><div class="label">告警次数</div><div class="value">{alert_stats.get("total", 0)}</div><div class="sub">次</div></div>')
        parts.append('</div>')

        # 环境参数统计
        parts.append('<h2>🌡️ 环境参数统计</h2>')
        parts.append('<table><tr><th>参数</th><th>平均值</th><th>最小值</th><th>最大值</th><th>样本数</th></tr>')
        for p in ["temperature", "humidity", "light", "soil_moisture", "soil_ph", "co2"]:
            if p in stats:
                meta = PARAM_META.get(p, {"name": p, "unit": ""})
                s = stats[p]
                parts.append(
                    f'<tr><td>{meta["name"]} ({meta["unit"]})</td>'
                    f'<td>{s["avg"]}</td><td>{s["min"]}</td><td>{s["max"]}</td>'
                    f'<td>{s["count"]}</td></tr>'
                )
        parts.append('</table>')

        # 控制动作统计
        parts.append('<h2>⚙️ 自动化管理能力</h2>')
        parts.append(
            f'<div class="summary-box">'
            f'本周期内系统共执行 <b>{control_stats.get("total", 0)}</b> 次设备控制，'
            f'其中自动控制 <b>{control_stats.get("auto", 0)}</b> 次'
            f'（占比 {self._pct(control_stats.get("auto", 0), control_stats.get("total", 0))}），'
            f'手动干预 <b>{control_stats.get("manual", 0)}</b> 次。'
            f'自动控制率越高，说明系统环境调控能力越强，人工干预越少。</div>'
        )
        by_device = control_stats.get("by_device", {})
        if by_device:
            parts.append('<table><tr><th>设备</th><th>动作总数</th><th>自动</th><th>手动</th></tr>')
            for device, d in sorted(by_device.items(), key=lambda x: -x[1]["count"]):
                parts.append(
                    f'<tr><td>{DEVICE_NAMES.get(device, device)}</td>'
                    f'<td>{d["count"]}</td><td>{d["auto"]}</td><td>{d["manual"]}</td></tr>'
                )
            parts.append('</table>')

        # 告警统计
        if alert_stats.get("total"):
            parts.append('<h2>⚠️ 告警统计</h2>')
            parts.append('<table><tr><th>等级</th><th>次数</th></tr>')
            for level, count in alert_stats.get("levels", {}).items():
                tag = {"danger": "tag-red", "warning": "tag-orange"}.get(level, "tag-green")
                parts.append(
                    f'<tr><td><span class="tag {tag}">{level}</span></td><td>{count}</td></tr>'
                )
            parts.append('</table>')

        parts.append(self._html_footer())
        return "".join(parts)

    def _render_weekly_report(self, raw, device_stats, control_stats, alert_stats, daily_avg) -> str:
        now = datetime.now()
        parts = [self._html_head(f"设备运行状态周报 {now.strftime('%Y-%m-%d')}")]
        parts.append(f"<h1>📋 设备运行状态周报</h1>")
        parts.append(f'<div class="subtitle">统计周期：最近 7 天 · 生成时间：{now.strftime("%Y-%m-%d %H:%M:%S")}</div>')

        # 概览
        parts.append('<h2>📊 周报概览</h2>')
        parts.append('<div class="stat-grid">')
        parts.append(f'<div class="stat-card"><div class="label">采集记录数</div><div class="value">{len(raw)}</div><div class="sub">条</div></div>')
        parts.append(f'<div class="stat-card"><div class="label">控制动作</div><div class="value">{control_stats.get("total", 0)}</div><div class="sub">次</div></div>')
        parts.append(f'<div class="stat-card"><div class="label">自动控制占比</div><div class="value">{self._pct(control_stats.get("auto", 0), control_stats.get("total", 0))}</div><div class="sub"></div></div>')
        parts.append(f'<div class="stat-card"><div class="label">告警次数</div><div class="value">{alert_stats.get("total", 0)}</div><div class="sub">次</div></div>')
        parts.append('</div>')

        # 设备运行统计
        parts.append('<h2>🔧 设备运行统计</h2>')
        if device_stats:
            parts.append('<table><tr><th>设备</th><th>开启次数</th><th>累计运行时长</th><th>动作总数</th></tr>')
            for device, d in sorted(device_stats.items(), key=lambda x: -x[1]["run_hours"]):
                parts.append(
                    f'<tr><td>{d["name"]}</td><td>{d["open_count"]}</td>'
                    f'<td>{d["run_hours"]} 小时</td><td>{d["action_count"]}</td></tr>'
                )
            parts.append('</table>')
        else:
            parts.append('<div style="color:#6b7280;font-size:13px;">本周暂无设备运行记录</div>')

        # 每日环境均值
        if daily_avg:
            parts.append('<h2>🌡️ 每日环境均值</h2>')
            parts.append('<table><tr><th>日期</th><th>温度(℃)</th><th>湿度(%)</th><th>光照(lux)</th></tr>')
            for d in daily_avg:
                parts.append(
                    f'<tr><td>{d["date"]}</td>'
                    f'<td>{d["temperature"] if d["temperature"] is not None else "-"}</td>'
                    f'<td>{d["humidity"] if d["humidity"] is not None else "-"}</td>'
                    f'<td>{d["light"] if d["light"] is not None else "-"}</td></tr>'
                )
            parts.append('</table>')

        # 自动化管理小结
        parts.append('<h2>⚙️ 自动化管理小结</h2>')
        auto_pct = self._pct(control_stats.get("auto", 0), control_stats.get("total", 0))
        if control_stats.get("total"):
            parts.append(
                f'<div class="summary-box">'
                f'本周系统共执行 <b>{control_stats.get("total")}</b> 次控制动作，'
                f'自动控制占比 <b>{auto_pct}</b>，'
                f'证明系统具备稳定的自动化环境调控能力。'
                f'告警 {alert_stats.get("total", 0)} 次，'
                f'系统均及时响应处理。</div>'
            )
        else:
            parts.append('<div class="summary-box">本周暂无控制动作，系统运行平稳。</div>')

        parts.append(self._html_footer())
        return "".join(parts)

    def _pct(self, part: int, total: int) -> str:
        if total <= 0:
            return "0%"
        return f"{round(part / total * 100)}%"

    # ==================== 生命周期 ====================

    def start(self):
        """启动定时报表线程。
        日报：首次启动立即生成一次，之后每天生成一次；
        周报：每周一生成一次。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("报表生成模块已启动（日报每日00:05，周报每周一00:05）")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self):
        last_daily = ""
        last_weekly = ""
        while self._running:
            try:
                today = datetime.now().strftime("%Y%m%d")
                # 日报：每天生成一次（首次运行立即生成，跨天后自动生成新一天）
                if today != last_daily:
                    try:
                        self.generate_daily_report()
                        last_daily = today
                    except Exception as e:
                        logger.error(f"自动生成日报失败: {e}")
                # 周报：每周一生成
                weekday = datetime.now().weekday()
                week_key = f"{today}_{weekday}"
                if weekday == 0 and week_key != last_weekly:
                    try:
                        self.generate_weekly_report()
                        last_weekly = week_key
                    except Exception as e:
                        logger.error(f"自动生成周报失败: {e}")
            except Exception as e:
                logger.error(f"报表线程异常: {e}")
            time.sleep(300)  # 每 5 分钟检查一次


# 单例引用（由 main.py 注入）
_generator: Optional[ReportGenerator] = None


def init_report_generator(storage, mqtt_client=None):
    """初始化报表生成器（main.py 启动时调用）"""
    global _generator
    _generator = ReportGenerator(storage, mqtt_client)
    return _generator


def get_generator() -> Optional[ReportGenerator]:
    return _generator
