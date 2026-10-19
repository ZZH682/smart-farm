# notify.py - 手机/群聊推送通知模块
# 将系统告警、设备事件通过 webhook 推送到手机（飞书 / 钉钉 / 企业微信 / Server酱）。
# 未配置 webhook 时自动禁用，不影响系统其他功能。

import json
import logging
import threading
import time
from datetime import datetime
from typing import Dict
from urllib import request, error, parse

from config import NOTIFY_WEBHOOK_URL, NOTIFY_WEBHOOK_TYPE

logger = logging.getLogger("SmartFarm.Notify")

# ==================== 告警去重/节流 ====================
# 防止传感器坏值/阈值抖动引发告警风暴刷爆 webhook。
# 同一 (level, param) 在窗口期内只推送一次。
_ALERT_DEDUP_WINDOW = 600.0  # 秒（10 分钟）
_last_alert_sent: Dict[str, float] = {}
_alert_lock = threading.Lock()


def _should_send_alert(level: str, param: str) -> bool:
    """判断该告警是否应推送（节流窗口内重复告警直接丢弃）"""
    key = f"{level}:{param}"
    now = time.time()
    with _alert_lock:
        last = _last_alert_sent.get(key, 0.0)
        if now - last < _ALERT_DEDUP_WINDOW:
            return False
        _last_alert_sent[key] = now
        return True


def _detect_type(url: str) -> str:
    """根据 webhook 地址自动识别平台类型"""
    forced = NOTIFY_WEBHOOK_TYPE
    if forced and forced != "auto":
        return forced
    if "open.feishu.cn" in url or "feishu" in url.lower():
        return "feishu"
    if "oapi.dingtalk.com" in url:
        return "dingtalk"
    if "qyapi.weixin.qq.com" in url:
        return "wecom"
    if "sctapi.ftqq.com" in url or "serverchan" in url:
        return "serverchan"
    return "generic"


def _build_payload(platform: str, title: str, message: str) -> dict:
    """按平台构建 webhook 请求体"""
    if platform == "feishu":
        return {
            "msg_type": "text",
            "content": {"text": f"{title}\n{message}"},
        }
    if platform == "dingtalk":
        return {
            "msgtype": "text",
            "text": {"content": f"{title}\n{message}"},
        }
    if platform == "wecom":
        return {
            "msgtype": "text",
            "text": {"content": f"{title}\n{message}"},
        }
    if platform == "serverchan":
        # Server酱 使用表单提交 title + desp
        return {"title": title, "desp": message}
    # 通用：尝试常见格式
    return {
        "msgtype": "text",
        "text": {"content": f"{title}\n{message}"},
    }


def _do_send(url: str, platform: str, title: str, message: str) -> bool:
    """执行一次 webhook 发送（同步调用，由线程池包装）"""
    payload = _build_payload(platform, title, message)

    if platform == "serverchan":
        # Server酱 用 application/x-www-form-urlencoded
        data = parse.urlencode(payload).encode("utf-8")
        req = request.Request(url, data=data, method="POST")
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    try:
        # 显式绕过系统代理直连：环境变量里若残留 HTTP(S)_PROXY 指向未运行的
        # 本地代理（如 127.0.0.1:7897），urllib 默认会走代理导致 10061 连接被拒。
        # 企业微信/飞书等 webhook 均为公网直连，无需代理。
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        logger.info(f"推送成功 ({platform}): {title} | 响应: {body[:120]}")
        return True
    except error.HTTPError as e:
        logger.error(f"推送 HTTP 错误 ({platform}): {e.code} {e.read().decode('utf-8', errors='ignore')[:200]}")
    except error.URLError as e:
        logger.error(f"推送网络错误 ({platform}): {e.reason}")
    except Exception as e:
        logger.error(f"推送异常 ({platform}): {e}")
    return False


def _worker(title: str, message: str):
    """异步发送线程"""
    url = NOTIFY_WEBHOOK_URL
    platform = _detect_type(url)
    try:
        _do_send(url, platform, title, message)
    except Exception as e:
        logger.error(f"推送线程异常: {e}")


def send_notification(title: str, message: str) -> bool:
    """发送一条通知（异步，不阻塞主流程）。
    未配置 webhook 时返回 False 并跳过。
    """
    if not NOTIFY_WEBHOOK_URL:
        logger.debug("未配置 NOTIFY_WEBHOOK_URL，已跳过推送")
        return False

    threading.Thread(target=_worker, args=(title, message), daemon=True).start()
    return True


def send_alert(alert: dict) -> bool:
    """告警事件推送（自动带上时间与等级，含去重节流）"""
    level = alert.get("level", "info")
    param = alert.get("param", "general")

    # 去重节流：同一 (level, param) 在窗口期内只推送一次，避免告警风暴
    if not _should_send_alert(level, param):
        logger.debug(f"告警节流跳过推送: {level}:{param}（{_ALERT_DEDUP_WINDOW:.0f}s 窗口内重复）")
        return False

    level_name = {"critical": "严重", "warning": "警告", "info": "提示"}.get(level, level)
    title = f"🌱 智慧大棚 {level_name}告警"
    ts = alert.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = alert.get("message", "")
    if ts:
        message = f"{message}\n⏰ {ts}"
    return send_notification(title, message)


def send_device_event(device_name: str, action: str, reason: str = "") -> bool:
    """设备动作事件推送（如手动开启风扇等）"""
    message = f"设备「{device_name}」已{action}"
    if reason:
        message += f"\n原因：{reason}"
    return send_notification(f"🌱 智慧大棚 设备事件", message)
