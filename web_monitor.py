
# web_monitor.py - Web 实时监控界面（Flask）

import functools
import json
import logging
import os
import secrets
import threading
from datetime import datetime
from typing import Dict, List

from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_socketio import SocketIO

from config import WEB_HOST, WEB_PORT, THRESHOLDS, WEB_TOKEN, DEVICE_NAMES
from data_storage import DataStorage
from pest_detect import CROP_PREFIXES as PEST_CROP_PREFIXES

logger = logging.getLogger("SmartFarm.Web")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("WEB_SECRET_KEY", secrets.token_hex(32))

# Socket.IO 实时推送（实时数据大屏：传感器/告警/控制事件即时到达浏览器）
# CORS 收紧为同源：不再允许任意跨域来源（此前 cors_allowed_origins="*" 风险过高）
socketio = SocketIO(app, async_mode="threading")

# ==================== 控制接口鉴权令牌 ====================
# 未配置 WEB_TOKEN 时自动生成随机令牌；前端页面通过 /api/token 获取后
# 在控制类请求的 X-Auth-Token 头中携带。防止跨站/未授权直接调用控制接口。
CONTROL_TOKEN = WEB_TOKEN if WEB_TOKEN else secrets.token_hex(16)


def get_web_token() -> str:
    """供 main.py 启动时打印令牌（未手动配置时）"""
    return CONTROL_TOKEN


def require_token(f):
    """控制接口鉴权装饰器：必须携带正确的 X-Auth-Token 头"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Auth-Token", "")
        if token != CONTROL_TOKEN:
            return jsonify({"success": False, "message": "未授权：缺少或错误的控制令牌"}), 401
        return f(*args, **kwargs)
    return wrapper

# 全局引用（由 main.py 注入）
storage: DataStorage = None
auto_controller = None
sensor_simulator = None
mqtt_client = None
device_manager = None
ai_assistant = None
pest_detector = None
yield_predictor = None


def init_web(storage_instance, controller, simulator, mqtt, device_mgr, ai=None,
             pest=None, yield_pred=None):
    """初始化 Web 模块的全局引用，由 main.py 在启动时调用"""
    global storage, auto_controller, sensor_simulator, mqtt_client, device_manager, ai_assistant
    global pest_detector, yield_predictor
    storage = storage_instance
    auto_controller = controller
    sensor_simulator = simulator
    mqtt_client = mqtt
    device_manager = device_mgr
    ai_assistant = ai
    pest_detector = pest
    yield_predictor = yield_pred


# ==================== Socket.IO 实时推送 ====================

def _build_socket_payload(data: dict) -> dict:
    """把传感器采集 data 转成前端友好的数据结构（与 /api/data 一致）"""
    sensors = data.get("sensors", {}) or {}
    devices = data.get("devices", {}) or {}
    result = {
        "success": True,
        "temperature": sensors.get("temperature"),
        "humidity": sensors.get("humidity"),
        "light": sensors.get("light"),
        "soil_moisture": sensors.get("soil_moisture"),
        "soil_ph": sensors.get("soil_ph"),
        "co2": sensors.get("co2"),
        "device_fan": devices.get("fan", False),
        "device_water_pump": devices.get("water_pump", False),
        "device_light_supplement": devices.get("light_supplement", False),
        "device_ventilation": devices.get("ventilation", False),
        "device_shading": devices.get("shading", False),
        "timestamp": data.get("timestamp"),
        "auto_control_enabled": auto_controller.enabled if auto_controller else True,
        "mqtt_connected": mqtt_client.is_connected if mqtt_client else False,
    }
    if device_manager:
        result["device_online"] = {
            d["device_id"]: d["online"]
            for d in device_manager.get_all_devices()
        }
    return result


def emit_sensor_data(data: dict):
    """实时推送传感器数据到浏览器（实时大屏）"""
    try:
        socketio.emit("sensor_data", _build_socket_payload(data))
    except Exception as e:
        logger.debug(f"Socket.IO 传感器推送异常: {e}")


def emit_alert(alert: dict):
    """实时推送告警到浏览器（弹窗提示）"""
    try:
        socketio.emit("alert", alert)
    except Exception as e:
        logger.debug(f"Socket.IO 告警推送异常: {e}")


def emit_control_event(device: str, state: bool, reason: str = ""):
    """实时推送控制事件到浏览器（设备状态联动）"""
    try:
        socketio.emit("control_event", {
            "device": device,
            "state": bool(state),
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        })
    except Exception as e:
        logger.debug(f"Socket.IO 控制事件推送异常: {e}")


def emit_pest_detection(result: dict):
    """实时推送病虫害识别结果到浏览器"""
    try:
        socketio.emit("pest_detection", result)
    except Exception as e:
        logger.debug(f"Socket.IO 病虫害识别推送异常: {e}")


def emit_yield_prediction(result: dict):
    """实时推送产量预测结果到浏览器"""
    try:
        socketio.emit("yield_prediction", result)
    except Exception as e:
        logger.debug(f"Socket.IO 产量预测推送异常: {e}")


# ==================== 页面路由 ====================

@app.route("/")
def dashboard():
    """监控面板首页"""
    return render_template("dashboard.html")


# ==================== 传感器数据 API ====================

@app.route("/api/data")
def api_data():
    """获取当前传感器数据（前端每 3 秒轮询一次）"""
    if storage is None:
        return jsonify({"success": False, "message": "存储模块未初始化"})

    latest = storage.get_latest_sensor_data()
    if latest:
        devices = {}
        try:
            devices = json.loads(latest.get("devices", "{}"))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

        result = {
            "success": True,
            "temperature": latest.get("temperature"),
            "humidity": latest.get("humidity"),
            "light": latest.get("light"),
            "soil_moisture": latest.get("soil_moisture"),
            "soil_ph": latest.get("soil_ph"),
            "co2": latest.get("co2"),
            "device_fan": devices.get("fan", False),
            "device_water_pump": devices.get("water_pump", False),
            "device_light_supplement": devices.get("light_supplement", False),
            "device_ventilation": devices.get("ventilation", False),
            "device_shading": devices.get("shading", False),
            "timestamp": latest.get("timestamp"),
            "auto_control_enabled": auto_controller.enabled if auto_controller else True,
            "mqtt_connected": mqtt_client.is_connected if mqtt_client else False,
        }

        # 附加设备在线状态
        if device_manager:
            result["device_online"] = {
                d["device_id"]: d["online"]
                for d in device_manager.get_all_devices()
            }

        return jsonify(result)

    return jsonify({"success": False, "message": "暂无数据"})


@app.route("/api/history")
def api_history():
    """获取历史数据"""
    if storage is None:
        return jsonify({"success": False, "message": "存储模块未初始化"})

    hours = request.args.get("hours", 1, type=int)
    data = storage.get_recent_sensor_data(hours=hours)
    return jsonify({"success": True, "data": data})


@app.route("/api/statistics")
def api_statistics():
    """获取统计数据"""
    if storage is None:
        return jsonify({"success": False, "message": "存储模块未初始化"})

    hours = request.args.get("hours", 24, type=int)
    stats = storage.get_statistics(hours=hours)
    return jsonify({"success": True, "data": stats})


@app.route("/api/alerts")
def api_alerts():
    """获取告警记录"""
    if storage is None:
        return jsonify({"success": False, "message": "存储模块未初始化"})

    alerts = storage.get_recent_alerts(limit=50)
    return jsonify({"success": True, "data": alerts})


@app.route("/api/control_log")
def api_control_log():
    """获取控制日志（包含 source 字段：auto/manual）"""
    if storage is None:
        return jsonify({"success": False, "message": "存储模块未初始化"})

    logs = storage.get_recent_control_log(limit=50)
    return jsonify({"success": True, "data": logs})


# ==================== 设备控制 API ====================

@app.route("/api/token")
def api_token():
    """获取控制接口令牌（前端页面加载时调用，同源页面可见）。
    配合 X-Auth-Token 头使用，防止跨站请求伪造与未授权控制。"""
    return jsonify({"success": True, "token": CONTROL_TOKEN})


@app.route("/api/control_device", methods=["POST"])
@require_token
def api_control_device():
    """手动控制设备（POST + 令牌鉴权，每次调用取反当前状态）"""
    data = request.get_json(silent=True) or request.form or {}
    device = data.get("device") if isinstance(data, dict) else None
    if not device:
        return jsonify({"success": False, "message": "缺少 device 参数"}), 400

    # 获取当前设备状态，然后取反
    current_status = {}
    if sensor_simulator:
        try:
            current_status = sensor_simulator.get_device_status()
        except Exception:
            current_status = {}

    current_state = current_status.get(device, False)
    new_state = not current_state

    # 通过自动控制器执行手动控制（带锁定超时）
    if auto_controller:
        try:
            auto_controller.manual_control(device, new_state)
        except Exception as e:
            logger.warning(f"自动控制模块处理失败: {e}")

    # 更新传感器模拟器的设备状态
    if sensor_simulator:
        try:
            sensor_simulator.update_device_status(device, new_state)
        except Exception as e:
            logger.warning(f"传感器模拟器更新失败: {e}")

    name = DEVICE_NAMES.get(device, device)
    action_text = "开启" if new_state else "关闭"
    logger.info(f"手动控制: {name} -> {action_text}")

    return jsonify({
        "success": True,
        "device": device,
        "state": new_state,
        "message": f"已{action_text}{name}",
    })


@app.route("/api/auto_control", methods=["POST"])
@require_token
def api_auto_control():
    """切换自动控制开关（POST + 令牌鉴权）"""
    data = request.get_json(silent=True) or request.form or {}
    enable = data.get("enable", "true")
    if isinstance(enable, str):
        enable = enable.lower() == "true"
    enable = bool(enable)

    if auto_controller:
        auto_controller.enabled = enable
        if enable:
            # 启用自动控制时清除所有手动锁定
            auto_controller.clear_manual_override()
        logger.info(f"自动控制已{'启用' if enable else '禁用'}")
        return jsonify({
            "success": True,
            "enabled": enable,
            "message": f"自动控制已{'启用' if enable else '禁用'}",
        })
    return jsonify({"success": False, "message": "控制模块未初始化"})


@app.route("/api/thresholds", methods=["GET", "POST"])
def api_thresholds():
    """获取或更新阈值配置（POST 需令牌鉴权，修改自动持久化到数据库）"""
    if request.method == "GET":
        return jsonify({"success": True, "data": THRESHOLDS})

    if request.method == "POST":
        # 令牌鉴权（修改类操作必须校验）
        if request.headers.get("X-Auth-Token", "") != CONTROL_TOKEN:
            return jsonify({"success": False, "message": "未授权：缺少或错误的控制令牌"}), 401
        try:
            new_thresholds = request.get_json()
            if not new_thresholds:
                return jsonify({"success": False, "message": "请求体为空"}), 400

            updated_count = 0
            for key, value in new_thresholds.items():
                if key in THRESHOLDS and isinstance(value, dict):
                    if "min" in value:
                        THRESHOLDS[key]["min"] = float(value["min"])
                    if "max" in value:
                        THRESHOLDS[key]["max"] = float(value["max"])
                    if "optimal" in value:
                        THRESHOLDS[key]["optimal"] = float(value["optimal"])
                    updated_count += 1
                    logger.info(
                        f"阈值更新: {key} -> min={value.get('min')}, max={value.get('max')}"
                    )

            if auto_controller and hasattr(auto_controller, "thresholds"):
                auto_controller.thresholds = THRESHOLDS

            # 持久化到数据库（重启不丢失）
            if storage is not None and updated_count > 0:
                try:
                    import json
                    storage.set_setting("thresholds", json.dumps(THRESHOLDS, ensure_ascii=False))
                except Exception as e:
                    logger.error(f"阈值持久化失败: {e}")

            return jsonify({
                "success": True,
                "message": f"已更新 {updated_count} 项阈值",
                "data": THRESHOLDS,
            })
        except Exception as e:
            logger.error(f"更新阈值失败: {e}")
            return jsonify({"success": False, "message": f"更新失败: {e}"}), 500


# ==================== 设备管理 API（新增） ====================

@app.route("/api/devices")
def api_devices():
    """获取所有设备状态"""
    if device_manager is None:
        return jsonify({"success": False, "message": "设备管理器未初始化"})

    devices = device_manager.get_all_devices()
    # 附加手动锁定信息
    manual_overrides = {}
    if auto_controller:
        manual_overrides = auto_controller.get_manual_overrides()

    for device in devices:
        did = device["device_id"]
        device["manual_locked"] = did in manual_overrides
        device["manual_remaining"] = manual_overrides.get(did, {}).get("remaining_seconds")

    return jsonify({
        "success": True,
        "data": devices,
        "summary": device_manager.get_device_count(),
    })


@app.route("/api/devices/<device_id>")
def api_device_detail(device_id):
    """获取单个设备详情"""
    if device_manager is None:
        return jsonify({"success": False, "message": "设备管理器未初始化"})

    device = device_manager.get_device(device_id)
    if not device:
        return jsonify({"success": False, "message": f"未知设备: {device_id}"}), 404

    return jsonify({"success": True, "data": device})


@app.route("/api/devices/<device_id>/simulate", methods=["POST"])
@require_token
def api_device_simulate(device_id):
    """
    模拟设备上线/离线（仅用于测试，真实硬件通过 MQTT 自动上报）
    POST body: {"online": true/false, "state": true/false}
    """
    if device_manager is None:
        return jsonify({"success": False, "message": "设备管理器未初始化"})

    data = request.get_json() or {}

    if "online" in data:
        device_manager.set_device_online(device_id, data["online"])

    if "state" in data and sensor_simulator:
        sensor_simulator.update_device_status(device_id, data["state"])

    device = device_manager.get_device(device_id)
    return jsonify({
        "success": True,
        "message": f"设备 {device_id} 状态已更新",
        "data": device,
    })


@app.route("/api/mqtt/status")
def api_mqtt_status():
    """获取 MQTT 连接状态"""
    connected = mqtt_client.is_connected if mqtt_client else False
    return jsonify({
        "success": True,
        "connected": connected,
        "broker": mqtt_client.broker if mqtt_client else None,
        "port": mqtt_client.port if mqtt_client else None,
    })


# ==================== AI 智能助手 API ====================

@app.route("/api/ai/chat", methods=["POST"])
def api_ai_chat():
    """AI 智能助手对话接口"""
    if ai_assistant is None:
        return jsonify({
            "success": False,
            "message": "AI 助手未初始化"
        }), 503

    data = request.get_json()
    if not data or "message" not in data:
        return jsonify({
            "success": False,
            "message": "缺少 message 参数"
        }), 400

    user_message = data["message"].strip()
    if not user_message:
        return jsonify({
            "success": False,
            "message": "消息不能为空"
        }), 400

    try:
        reply = ai_assistant.chat(user_message)
        return jsonify({
            "success": True,
            "reply": reply,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"AI 助手处理失败: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "message": f"AI 助手处理失败: {str(e)}"
        }), 500


@app.route("/api/ai/history")
def api_ai_history():
    """获取 AI 对话历史"""
    if ai_assistant is None:
        return jsonify({
            "success": False,
            "message": "AI 助手未初始化"
        }), 503

    limit = request.args.get("limit", 20, type=int)
    history = ai_assistant.get_history(limit)

    return jsonify({
        "success": True,
        "history": history
    })


@app.route("/api/ai/clear", methods=["POST"])
def api_ai_clear():
    """清空 AI 对话历史"""
    if ai_assistant is None:
        return jsonify({
            "success": False,
            "message": "AI 助手未初始化"
        }), 503

    ai_assistant.clear_history()
    return jsonify({
        "success": True,
        "message": "对话历史已清空"
    })


# ==================== 病虫害识别 API ====================

@app.route("/api/pest/detect", methods=["POST"])
def api_pest_detect():
    """上传图片进行病虫害识别"""
    if pest_detector is None:
        return jsonify({"success": False, "message": "病虫害识别模块未初始化"}), 503

    if "image" not in request.files:
        return jsonify({"success": False, "message": "请上传图片文件（字段名: image）"}), 400

    file = request.files["image"]
    if file.filename == "":
        return jsonify({"success": False, "message": "未选择文件"}), 400

    # ---- 上传安全校验：扩展名白名单 + 大小限制 + 真实图片格式验证 ----
    ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    MAX_SIZE = 10 * 1024 * 1024  # 10MB
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    if file_size > MAX_SIZE:
        return jsonify({"success": False, "message": f"图片过大（最大 {MAX_SIZE // 1024 // 1024}MB）"}), 400

    ext = os.path.splitext(file.filename)[1].lower() or ".jpg"
    if ext not in ALLOWED_EXT:
        return jsonify({"success": False, "message": f"不支持的图片格式（允许: {sorted(ALLOWED_EXT)}）"}), 400

    # 保存上传图片
    try:
        from config import PEST_UPLOAD_DIR
        os.makedirs(PEST_UPLOAD_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 保存路径使用白名单内的规范化后缀，不直接采用用户文件名，防止路径/后缀注入
        save_path = os.path.join(PEST_UPLOAD_DIR, f"pest_{timestamp}{ext}")
        file.save(save_path)

        # 用 PIL 验证确实是图片（防伪装文件上传）
        try:
            from PIL import Image
            with Image.open(save_path) as img:
                img.verify()
        except Exception:
            os.remove(save_path)
            return jsonify({"success": False, "message": "文件不是有效的图片"}), 400
    except Exception as e:
        logger.error(f"保存上传图片失败: {e}")
        return jsonify({"success": False, "message": f"图片保存失败: {e}"}), 500

    # 执行识别（支持作物类型过滤，提升准确率）
    crop = request.form.get("crop", "auto") or "auto"
    if crop not in PEST_CROP_PREFIXES:
        crop = "auto"
    result = pest_detector.detect_from_image(save_path, source="upload", crop=crop)

    # 实时推送到前端
    emit_pest_detection(result)

    return jsonify(result)


@app.route("/api/pest/history")
def api_pest_history():
    """获取病虫害识别历史"""
    if pest_detector is None:
        return jsonify({"success": False, "message": "病虫害识别模块未初始化"}), 503

    limit = request.args.get("limit", 20, type=int)
    history = pest_detector.get_history(limit)
    return jsonify({"success": True, "data": history})


@app.route("/api/pest/latest")
def api_pest_latest():
    """获取最新一次病虫害识别结果"""
    if pest_detector is None:
        return jsonify({"success": False, "message": "病虫害识别模块未初始化"}), 503

    latest = pest_detector.get_latest_result()
    if latest:
        return jsonify({"success": True, "data": latest})
    return jsonify({"success": False, "message": "暂无识别记录"})


@app.route("/api/pest/camera", methods=["POST"])
def api_pest_camera():
    """摄像头抓拍识别（预留接口）"""
    if pest_detector is None:
        return jsonify({"success": False, "message": "病虫害识别模块未初始化"}), 503

    from config import PEST_CAMERA_ID
    result = pest_detector.detect_from_camera(PEST_CAMERA_ID)
    return jsonify(result)


# ==================== 产量预测 API ====================

@app.route("/api/yield/current")
def api_yield_current():
    """获取当前产量预测"""
    if yield_predictor is None:
        return jsonify({"success": False, "message": "产量预测模块未初始化"}), 503

    latest = yield_predictor.get_latest_prediction()
    if latest:
        return jsonify({"success": True, "data": latest})
    return jsonify({"success": False, "message": "暂无预测数据"})


@app.route("/api/yield/history")
def api_yield_history():
    """获取产量预测历史"""
    if yield_predictor is None:
        return jsonify({"success": False, "message": "产量预测模块未初始化"}), 503

    limit = request.args.get("limit", 50, type=int)
    # 优先从数据库取
    if storage and hasattr(storage, "get_recent_yield_predictions"):
        try:
            history = storage.get_recent_yield_predictions(limit)
            return jsonify({"success": True, "data": history})
        except Exception as e:
            logger.error(f"查询产量预测历史失败: {e}")
    # 回退到内存
    history = yield_predictor.get_prediction_history(limit)
    return jsonify({"success": True, "data": history})


@app.route("/api/yield/trend")
def api_yield_trend():
    """获取产量趋势预测"""
    if yield_predictor is None:
        return jsonify({"success": False, "message": "产量预测模块未初始化"}), 503

    hours = request.args.get("hours", 24, type=int)
    trend = yield_predictor.predict_trend(hours)
    return jsonify({"success": True, "data": trend})


@app.route("/api/yield/refresh", methods=["POST"])
def api_yield_refresh():
    """手动触发一次产量预测"""
    if yield_predictor is None:
        return jsonify({"success": False, "message": "产量预测模块未初始化"}), 503

    result = yield_predictor.run_prediction()
    if result:
        emit_yield_prediction(result)
        return jsonify({"success": True, "data": result})
    return jsonify({"success": False, "message": "预测失败"})


# ==================== 历史趋势分析 API ====================

@app.route("/api/trend/analysis")
def api_trend_analysis():
    """获取 24 小时历史趋势分析（曲线数据 + 温湿度/光照关联分析）"""
    from trend_analysis import get_analyzer
    analyzer = get_analyzer()
    if analyzer is None:
        return jsonify({"success": False, "message": "趋势分析模块未初始化"}), 503

    hours = request.args.get("hours", 24, type=int)
    result = analyzer.analyze_24h(hours=hours)
    return jsonify(result)


@app.route("/api/trend/export")
def api_trend_export():
    """导出历史环境数据（用于机器学习训练/分析）"""
    from trend_analysis import get_analyzer
    analyzer = get_analyzer()
    if analyzer is None:
        return jsonify({"success": False, "message": "趋势分析模块未初始化"}), 503

    hours = request.args.get("hours", 72, type=int)
    data = analyzer.export_training_data(hours=hours)
    return jsonify({
        "success": True,
        "count": len(data),
        "data": data,
        "hint": "可将此数据导出为 CSV/JSON 用于训练温度预测或病虫害识别模型",
    })


# ==================== 业务报表 API ====================

@app.route("/api/report/daily")
def api_report_daily():
    """生成环境数据日报（最近24小时）"""
    from report_generator import get_generator
    generator = get_generator()
    if generator is None:
        return jsonify({"success": False, "message": "报表模块未初始化"}), 503

    result = generator.generate_daily_report()
    if result.get("success"):
        return jsonify({
            "success": True,
            "data": result,
            "html_url": f"/reports/{result['filename']}",
        })
    return jsonify(result)


@app.route("/api/report/weekly")
def api_report_weekly():
    """生成设备运行状态周报（最近7天）"""
    from report_generator import get_generator
    generator = get_generator()
    if generator is None:
        return jsonify({"success": False, "message": "报表模块未初始化"}), 503

    result = generator.generate_weekly_report()
    if result.get("success"):
        return jsonify({
            "success": True,
            "data": result,
            "html_url": f"/reports/{result['filename']}",
        })
    return jsonify(result)


@app.route("/api/reports/list")
def api_report_list():
    """列出已生成的报表文件"""
    from report_generator import REPORT_DIR
    if not os.path.isdir(REPORT_DIR):
        return jsonify({"success": True, "data": []})
    files = []
    for f in sorted(os.listdir(REPORT_DIR), reverse=True):
        if f.endswith(".html"):
            files.append({
                "filename": f,
                "path": f"/reports/{f}",
                "size": os.path.getsize(os.path.join(REPORT_DIR, f)),
                "mtime": datetime.fromtimestamp(
                    os.path.getmtime(os.path.join(REPORT_DIR, f))
                ).strftime("%Y-%m-%d %H:%M"),
            })
    return jsonify({"success": True, "data": files})


@app.route("/reports/<path:filename>", methods=["GET"])
def api_report_file_final(filename):
    """查看/下载已生成的报表"""
    from report_generator import REPORT_DIR
    safe_path = os.path.normpath(os.path.join(REPORT_DIR, filename))
    if not safe_path.startswith(REPORT_DIR) or not os.path.isfile(safe_path):
        return jsonify({"success": False, "message": "报表不存在"}), 404
    return send_from_directory(REPORT_DIR, filename)

@app.route("/api/forecast/temperature")
def api_forecast_temperature():
    """获取最新温度预测结果"""
    from temp_forecast import get_forecaster
    forecaster = get_forecaster()
    if forecaster is None:
        return jsonify({"success": False, "message": "温度预测模块未初始化"}), 503

    latest = forecaster.get_latest_forecast()
    if latest:
        return jsonify({"success": True, "data": latest})
    return jsonify({"success": False, "message": "暂无预测数据，等待首次预测..."})


@app.route("/api/forecast/temperature/run", methods=["POST"])
def api_forecast_temperature_run():
    """手动触发一次温度预测"""
    from temp_forecast import get_forecaster
    forecaster = get_forecaster()
    if forecaster is None:
        return jsonify({"success": False, "message": "温度预测模块未初始化"}), 503

    result = forecaster.run_forecast()
    if result:
        return jsonify({"success": True, "data": result})
    return jsonify({"success": False, "message": "预测失败（历史数据不足？）"})


# ==================== 启动 ====================

def start_web_server():
    """启动 Web 服务器（在独立守护线程中运行，含 Socket.IO 实时推送）"""
    logger.info(f"启动 Web 监控界面: http://{WEB_HOST}:{WEB_PORT} (含实时推送)")
    socketio.run(
        app, host=WEB_HOST, port=WEB_PORT,
        debug=False, use_reloader=False, allow_unsafe_werkzeug=True,
    )
