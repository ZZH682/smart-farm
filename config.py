# config.py - 智慧农场系统配置

import os

# ==================== 环境变量加载 ====================
# 轻量 .env 加载（不引入 python-dotenv 依赖）。
# 优先级：系统环境变量 > .env 文件 > 代码默认值。
# 敏感信息（LLM API Key、webhook、MQTT 密码等）一律放入 .env，
# 严禁提交到代码仓库（参见 .gitignore 与 .env.example）。
def _load_dotenv(path: str = None) -> None:
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # 去掉行尾注释（注意：若密码本身含 " #" 请用引号包裹值）
                value = value.split(" #", 1)[0].strip()
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()

# ==================== MQTT配置 ====================
# 自动检测本机局域网 IP（避免更换网络后 broker 地址失效）
def _detect_local_ip() -> str:
    """获取本机局域网 IP（用于 MQTT broker 地址）"""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 连接一个外部地址以触发路由选择（不实际发包）
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


MQTT_BROKER = os.environ.get("MQTT_BROKER", _detect_local_ip())
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_KEEPALIVE = int(os.environ.get("MQTT_KEEPALIVE", 60))
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "smart_farm_server")

# MQTT 认证（可选，Broker 未开启认证时留空）
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")

# MQTT TLS（可选，需要加密连接时启用）
MQTT_USE_TLS = os.environ.get("MQTT_USE_TLS", "false").lower() == "true"
MQTT_CA_CERTS = os.environ.get("MQTT_CA_CERTS", "")

# MQTT QoS 等级（0=最多一次, 1=至少一次, 2=恰好一次）
MQTT_QOS_PUBLISH = int(os.environ.get("MQTT_QOS_PUBLISH", 1))
MQTT_QOS_SUBSCRIBE = int(os.environ.get("MQTT_QOS_SUBSCRIBE", 1))

# MQTT 重连配置
MQTT_RECONNECT_DELAY_MIN = 1    # 最小重连间隔（秒）
MQTT_RECONNECT_DELAY_MAX = 120  # 最大重连间隔（秒）
MQTT_CONNECT_TIMEOUT = 10       # 连接超时（秒）

# ==================== MQTT 主题定义 ====================
# 主题前缀
TOPIC_PREFIX = "farm/greenhouse"

# 传感器数据上报主题
TOPICS = {
    "temperature": f"{TOPIC_PREFIX}/sensor/temperature",
    "humidity": f"{TOPIC_PREFIX}/sensor/humidity",
    "light": f"{TOPIC_PREFIX}/sensor/light",
    "soil_moisture": f"{TOPIC_PREFIX}/sensor/soil_moisture",
    "soil_ph": f"{TOPIC_PREFIX}/sensor/soil_ph",
    "co2": f"{TOPIC_PREFIX}/sensor/co2",
    "control": f"{TOPIC_PREFIX}/control/#",
    "status": f"{TOPIC_PREFIX}/status",
    "alert": f"{TOPIC_PREFIX}/alert",
}

# 控制指令下发主题（服务端 -> 硬件）
CONTROL_TOPICS = {
    "fan": f"{TOPIC_PREFIX}/control/fan",
    "water_pump": f"{TOPIC_PREFIX}/control/water_pump",
    "light_supplement": f"{TOPIC_PREFIX}/control/light_supplement",
    "ventilation": f"{TOPIC_PREFIX}/control/ventilation",
    "shading": f"{TOPIC_PREFIX}/control/shading",
}

# 硬件状态反馈主题（硬件 -> 服务端）
DEVICE_STATUS_TOPICS = {
    "fan": f"{TOPIC_PREFIX}/device/fan/status",
    "water_pump": f"{TOPIC_PREFIX}/device/water_pump/status",
    "light_supplement": f"{TOPIC_PREFIX}/device/light_supplement/status",
    "ventilation": f"{TOPIC_PREFIX}/device/ventilation/status",
    "shading": f"{TOPIC_PREFIX}/device/shading/status",
}

# 硬件心跳主题
DEVICE_HEARTBEAT_TOPIC = f"{TOPIC_PREFIX}/device/+/heartbeat"

# ==================== MQTT 设备发现（扫描）主题 ====================
# 主动发现协议（识别附近/在线的 MQTT 设备）：
#   1. 扫描端向 DISCOVER_TOPIC 发布探测消息 {"action":"discover","nonce":"..."}
#   2. 支持发现的设备向 DISCOVER_RESPONSE_TOPIC 回报自身信息
#      （device_id/type/name/firmware/ip/sensors/relays...）
#   3. 设备也可向 DISCOVER_INFO_TOPIC/{device_id} 发布 retained 自描述信息，
#      扫描端订阅后立即可读（设备休眠时也能被发现）
# 被动发现：扫描端订阅 TOPIC_PREFIX/# 嗅探心跳/状态主题，自动识别在线设备。
DISCOVER_TOPIC = f"{TOPIC_PREFIX}/discover"
DISCOVER_RESPONSE_TOPIC = f"{TOPIC_PREFIX}/discover/response"
DISCOVER_INFO_TOPIC = f"{TOPIC_PREFIX}/discover/info"
# 主动扫描默认等待时间（秒）：发布探测后等待设备响应的最长时间
DISCOVER_TIMEOUT = float(os.environ.get("DISCOVER_TIMEOUT", 8))
# 发现设备离线判定时间（秒）：超过该时间未再出现，标记为离线
DISCOVER_OFFLINE_AFTER = float(os.environ.get("DISCOVER_OFFLINE_AFTER", 120))

# LWT 遗嘱消息（服务端异常断开时通知硬件）
LWT_TOPIC = f"{TOPIC_PREFIX}/server/status"
LWT_PAYLOAD = '{"status": "offline", "source": "server"}'
LWT_QOS = 1
LWT_RETAIN = True

# ==================== 传感器与控制配置 ====================

# 传感器采集间隔（秒）
SENSOR_INTERVAL = int(os.environ.get("SENSOR_INTERVAL", 5))

# 模拟器昼夜周期（秒）：默认 86400 = 真实一天。
# 此前压缩为 600 秒（10分钟一天）导致模拟数据快速抖动，现已修正。
SIMULATOR_DAY_CYCLE_SECONDS = int(os.environ.get("SIMULATOR_DAY_CYCLE_SECONDS", 86400))

# 传感器参数合理范围（服务端兜底校验）：
# 超出范围的数值视为坏值/传感器故障，丢弃并记日志，不进入控制与存储。
SENSOR_RANGES = {
    "temperature": (-40.0, 60.0),
    "soil_temperature": (-40.0, 80.0),
    "humidity": (0.0, 100.0),
    "light": (0, 200000),
    "soil_moisture": (0.0, 100.0),
    "soil_ph": (0.0, 14.0),
    "co2": (0, 10000),
}

# 自动控制最小动作间隔（秒）：同一设备在间隔内最多切换一次，
# 防止模拟/真实数据交替或阈值抖动导致设备频繁启停。
CONTROL_MIN_ACTION_INTERVAL = int(os.environ.get("CONTROL_MIN_ACTION_INTERVAL", 60))

# 环境阈值配置（用于自动控制）
THRESHOLDS = {
    "temperature": {"min": 18.0, "max": 32.0, "optimal": 25.0},
    "humidity": {"min": 40.0, "max": 80.0, "optimal": 65.0},
    "light": {"min": 3000, "max": 50000, "optimal": 20000},
    "soil_moisture": {"min": 30.0, "max": 70.0, "optimal": 50.0},
    "soil_ph": {"min": 5.5, "max": 7.5, "optimal": 6.5},
    "co2": {"min": 400, "max": 1500, "optimal": 800},
}

# ==================== 数据库配置 ====================
DATABASE_PATH = os.environ.get("DATABASE_PATH", "smart_farm.db")
# 历史数据保留天数：超过该天数的传感器/控制/告警数据会被后台清理线程删除
DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", 90))

# ==================== Web服务配置 ====================
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", 5000))
# 控制接口鉴权令牌：未配置时启动会自动生成随机令牌并打印。
# 局域网演示场景防止跨站/未授权直接调用控制接口；
# 真实生产部署请替换为正式认证方案。
WEB_TOKEN = os.environ.get("WEB_TOKEN", "")

# ==================== 手机推送通知配置（可选） ====================
# 告警推送 webhook 地址。支持飞书 / 钉钉 / 企业微信 / Server酱 机器人 webhook。
# 留空则禁用推送（不影响系统其他功能）。请通过环境变量或 .env 配置，
# 不要在代码中硬编码真实 webhook（避免随源码泄露）。
NOTIFY_WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "")
# 强制指定平台类型：auto 自动识别 / feishu / dingtalk / wecom / serverchan
NOTIFY_WEBHOOK_TYPE = os.environ.get("NOTIFY_WEBHOOK_TYPE", "auto")

# ==================== AI 助手配置 ====================
# 配置后 AI 助手将使用大模型对话（OpenAI 兼容 /chat/completions 接口）；
# 留空则使用内置规则引擎（现有功能不受影响）。
# 密钥必须通过环境变量或 .env 提供（.env.example 中有模板）。
LLM_API_BASE = os.environ.get("LLM_API_BASE", "https://open.bigmodel.cn/api/paas/v4")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-4-flash")

# ===== 以下为必须补充的配置项 =====
LLM_TIMEOUT = 20          # AI 请求超时时间（秒）——正常 LLM 2~4s，20s 足够；超时自动回退规则引擎
LLM_MAX_TOKENS = 2048     # AI 回复最大 Token 数
LLM_TEMPERATURE = 0.7     # 生成随机性（0~1，越大越有创造性）

# ==================== YOLO 病虫害识别配置 ====================
# 模型文件路径（训练后放入 models/ 目录）；不存在时自动降级为颜色分析
PEST_MODEL_PATH = os.environ.get(
    "PEST_MODEL_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "pest_yolov8n.pt"),
)
# 识别置信度阈值（低于此值的检测结果将被过滤）
PEST_CONF_THRESHOLD = float(os.environ.get("PEST_CONF_THRESHOLD", 0.5))
# 上传图片保存目录
PEST_UPLOAD_DIR = os.environ.get(
    "PEST_UPLOAD_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "pest"),
)
# 摄像头设备 ID（预留，后期接入真实摄像头时使用）
PEST_CAMERA_ID = os.environ.get("PEST_CAMERA_ID", "default")

# ==================== 产量预测配置 ====================
# 预测间隔（秒），默认每小时一次
YIELD_PREDICT_INTERVAL = int(os.environ.get("YIELD_PREDICT_INTERVAL", 3600))
# 低产量指数告警阈值（低于此值触发告警）
YIELD_LOW_THRESHOLD = float(os.environ.get("YIELD_LOW_THRESHOLD", 50.0))

# ==================== 设备中文名（唯一收口） ====================
# 全系统统一从此处引用，禁止在模块内重复定义
DEVICE_NAMES = {
    "esp32": "ESP32节点",
    "fan": "排风扇",
    "water_pump": "灌溉水泵",
    "light_supplement": "补光灯",
    "ventilation": "通风窗",
    "shading": "遮阳帘",
}