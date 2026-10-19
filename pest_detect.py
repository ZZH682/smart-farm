# pest_detect.py - 农业病虫害识别模块
#
# 功能：
#   1. 图片上传识别（当前主要方式，无摄像头时使用）
#   2. 预留摄像头抓拍接口（后期接入真实摄像头）
#   3. 识别引擎优先级：
#      - EfficientNet-B0 分类模型（PlantVillage 38类，准确率 97%+）—— 首选，最准
#      - YOLOv8 检测/分类模型 —— 第二选择
#      - 颜色分析规则引擎（PIL）—— 降级方案
#      - 模拟结果 —— 兜底
#   4. 识别结果通过 MQTT 发布，检测到病虫害时触发告警
#   5. 识别记录持久化到 SQLite

import base64
import logging
import os
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("SmartFarm.PestDetect")

# ==================== 病虫害类别定义 ====================
# 常见大棚作物病虫害（基于 PlantVillage 数据集类别，覆盖番茄/黄瓜/草莓/辣椒/生菜）
PEST_CLASSES = {
    # 健康
    "healthy": {"name": "健康", "level": "info", "advice": "植株生长良好，继续保持当前环境管理。"},
    # 番茄病害
    "tomato_early_blight": {"name": "番茄早疫病", "level": "warning", "advice": "及时摘除病叶，喷施代森锰锌或百菌清，加强通风降低湿度。"},
    "tomato_late_blight": {"name": "番茄晚疫病", "level": "danger", "advice": "立即喷施霜霉威或烯酰吗啉，严重时拔除病株，避免大水漫灌。"},
    "tomato_leaf_mold": {"name": "番茄叶霉病", "level": "warning", "advice": "加强通风排湿，喷施多抗霉素或武夷菌素，控制氮肥用量。"},
    "tomato_septoria_leaf_spot": {"name": "番茄斑枯病", "level": "warning", "advice": "清除病残体，喷施苯醚甲环唑，保持植株间距利于通风。"},
    "tomato_yellow_leaf_curl_virus": {"name": "番茄黄化曲叶病毒", "level": "danger", "advice": "该病由烟粉虱传播，立即防治粉虱（吡虫啉/噻虫嗪），拔除重病株。"},
    "tomato_mosaic_virus": {"name": "番茄花叶病毒", "level": "danger", "advice": "拔除病株，防治蚜虫，操作前后消毒手部和工具防止接触传播。"},
    "tomato_bacterial_spot": {"name": "番茄细菌性斑点病", "level": "warning", "advice": "喷施噻菌铜或春雷霉素，避免叶面结露，采用滴灌。"},
    # 黄瓜病害
    "cucumber_powdery_mildew": {"name": "黄瓜白粉病", "level": "warning", "advice": "喷施三唑酮或腈菌唑，控制种植密度，保持通风透光。"},
    "cucumber_downy_mildew": {"name": "黄瓜霜霉病", "level": "danger", "advice": "喷施霜脲氰或烯酰吗啉，降低棚内湿度，避免夜间结露。"},
    "cucumber_anthracnose": {"name": "黄瓜炭疽病", "level": "warning", "advice": "喷施咪鲜胺或溴菌腈，及时清除病叶病果。"},
    # 草莓病害
    "strawberry_leaf_scorch": {"name": "草莓叶枯病", "level": "warning", "advice": "摘除病叶，喷施代森锰锌，避免植株过密。"},
    "strawberry_powdery_mildew": {"name": "草莓白粉病", "level": "warning", "advice": "喷施氟硅唑或醚菌酯，控制棚内湿度在 70% 以下。"},
    "strawberry_gray_mold": {"name": "草莓灰霉病", "level": "danger", "advice": "喷施腐霉利或异菌脲，及时摘除病花病果，加强通风。"},
    # 辣椒病害
    "pepper_bacterial_spot": {"name": "辣椒细菌性斑点病", "level": "warning", "advice": "喷施噻菌铜，避免高温高湿，采用高畦栽培。"},
    "pepper_leaf_spot": {"name": "辣椒叶斑病", "level": "warning", "advice": "喷施百菌清或甲基托布津，轮作倒茬。"},
    # 生菜病害
    "lettuce_downy_mildew": {"name": "生菜霜霉病", "level": "warning", "advice": "喷施霜霉威，控制浇水，保持叶面干燥。"},
    "lettuce_bacterial_leaf_spot": {"name": "生菜细菌性叶斑病", "level": "warning", "advice": "喷施农用链霉素，避免大水漫灌。"},
    # 虫害
    "aphid": {"name": "蚜虫", "level": "warning", "advice": "喷施吡虫啉或啶虫脒，可悬挂黄色粘虫板诱杀。"},
    "whitefly": {"name": "白粉虱", "level": "warning", "advice": "喷施噻虫嗪或螺虫乙酯，悬挂黄色粘虫板，注意叶背喷药。"},
    "spider_mite": {"name": "红蜘蛛", "level": "warning", "advice": "喷施阿维菌素或哒螨灵，重点喷叶背，增加棚内湿度。"},
}

# 默认模型路径
DEFAULT_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models", "pest_yolov8n.pt"
)
DEFAULT_EFFICIENTNET_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models", "efficientnet_plant_best.pth"
)
DEFAULT_CLASS_NAMES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models", "class_names.json"
)

# ==================== 作物类型过滤 ====================
# 模型（PlantVillage 38类 / YOLO 30类）只覆盖以下作物。
# 上传时让用户选择作物类型，按作物过滤类别，可显著提升准确率、
# 避免跨作物误报（例如把黄瓜叶误判成玉米叶斑病）。
CROP_PREFIXES = {
    "auto": None,  # 自动：不过滤（全部 38 类）
    "tomato": "Tomato___",
    "strawberry": "Strawberry___",
    "pepper": "Pepper,_bell___",
    "potato": "Potato___",
    "corn": "Corn_(maize)___",
    "grape": "Grape___",
    "apple": "Apple___",
    "peach": "Peach___",
    "squash": "Squash___",
    "orange": "Orange___",
    "cherry": "Cherry_(including_sour)___",
    "blueberry": "Blueberry___",
    "raspberry": "Raspberry___",
    "soybean": "Soybean___",
}
CROP_NAMES = {
    "auto": "自动识别（全部作物）",
    "tomato": "番茄",
    "strawberry": "草莓",
    "pepper": "辣椒/甜椒",
    "potato": "土豆",
    "corn": "玉米",
    "grape": "葡萄",
    "apple": "苹果",
    "peach": "桃",
    "squash": "南瓜",
    "orange": "柑橘",
    "cherry": "樱桃",
    "blueberry": "蓝莓",
    "raspberry": "树莓",
    "soybean": "大豆",
}

# 拒识阈值：Top1 置信度低于此值不硬判（可能是超出模型覆盖的作物或拍摄质量差）
REJECT_CONF = 0.30


class PestDetector:
    """
    病虫害识别器。

    识别优先级：
      1. EfficientNet-B0 分类模型（PlantVillage 38类，准确率 97%+）—— 首选，最准
      2. YOLOv8（ultralytics + 模型文件）—— 第二选择
      3. 颜色分析规则引擎（PIL）—— 降级方案
      4. 模拟结果 —— 兜底
    """

    def __init__(
        self,
        model_path: str = DEFAULT_MODEL_PATH,
        efficientnet_path: str = DEFAULT_EFFICIENTNET_PATH,
        class_names_path: str = DEFAULT_CLASS_NAMES_PATH,
        conf_threshold: float = 0.5,
        mqtt_client=None,
        storage=None,
        alert_callback: Optional[Callable] = None,
    ):
        self.model_path = model_path
        self.efficientnet_path = efficientnet_path
        self.class_names_path = class_names_path
        self.conf_threshold = conf_threshold
        self.mqtt = mqtt_client
        self.storage = storage
        self.alert_callback = alert_callback

        # 识别引擎状态
        self._engine = "none"  # efficientnet / yolo / color / mock
        self._model = None  # YOLO 模型
        self._eff_model = None  # EfficientNet 模型
        self._class_names = []  # PlantVillage 38类类别名
        self._eff_transform = None
        self._init_engine()

        # 最近识别结果缓存（供 Web/AI 查询）
        self._latest_result: Optional[Dict] = None
        self._lock = threading.Lock()

        # MQTT 主题
        from config import TOPIC_PREFIX
        self._topic_pest = f"{TOPIC_PREFIX}/pest/detect"

    # ==================== 引擎初始化 ====================

    def _init_engine(self):
        """初始化识别引擎，按优先级尝试 EfficientNet → YOLO → 颜色分析 → 模拟"""
        # 1. 尝试 EfficientNet-B0 分类模型（首选，最准）
        try:
            import torch
            import timm
            from torchvision import transforms
            import json

            if os.path.exists(self.efficientnet_path) and os.path.exists(self.class_names_path):
                # 加载类别名
                with open(self.class_names_path, 'r') as f:
                    self._class_names = json.load(f)

                # 创建模型并加载权重
                self._eff_model = timm.create_model(
                    'efficientnet_b0', pretrained=False,
                    num_classes=len(self._class_names)
                )
                state_dict = torch.load(
                    self.efficientnet_path, map_location='cpu', weights_only=True
                )
                self._eff_model.load_state_dict(state_dict)
                self._eff_model.eval()

                # 预处理
                self._eff_transform = transforms.Compose([
                    transforms.Resize((224, 224)),
                    transforms.ToTensor(),
                    transforms.Normalize(
                        mean=[0.485, 0.456, 0.406],
                        std=[0.229, 0.224, 0.225]
                    ),
                ])

                self._engine = "efficientnet"
                logger.info(
                    f"EfficientNet-B0 分类引擎已加载: {self.efficientnet_path} "
                    f"({len(self._class_names)} 类)"
                )
                return
            else:
                missing = []
                if not os.path.exists(self.efficientnet_path):
                    missing.append(f"模型文件 {self.efficientnet_path}")
                if not os.path.exists(self.class_names_path):
                    missing.append(f"类别名文件 {self.class_names_path}")
                logger.warning(
                    f"EfficientNet 模型未就绪（缺少: {', '.join(missing)}），"
                    f"尝试 YOLO 引擎。"
                )
        except ImportError as e:
            logger.warning(f"未安装 timm/torchvision，跳过 EfficientNet: {e}")
        except Exception as e:
            logger.warning(f"EfficientNet 加载失败，尝试 YOLO: {e}")

        # 2. 尝试 YOLOv8
        try:
            from ultralytics import YOLO
            if os.path.exists(self.model_path):
                self._model = YOLO(self.model_path)
                self._engine = "yolo"
                logger.info(f"YOLOv8 识别引擎已加载: {self.model_path}")
                return
            else:
                logger.warning(
                    f"YOLO 模型文件不存在: {self.model_path}，"
                    f"将使用颜色分析降级方案。"
                )
        except ImportError:
            logger.warning("未安装 ultralytics，将使用颜色分析降级方案。")
        except Exception as e:
            logger.warning(f"YOLO 加载失败，降级到颜色分析: {e}")

        # 3. 尝试颜色分析（需要 PIL）
        try:
            from PIL import Image  # noqa: F401
            self._engine = "color"
            logger.info("颜色分析识别引擎已启用（降级方案）")
            return
        except ImportError:
            pass

        # 4. 兜底模拟
        self._engine = "mock"
        logger.warning("PIL 也未安装，使用模拟识别结果（仅用于演示）。")

    @property
    def engine(self) -> str:
        return self._engine

    # ==================== 图像预检（识别前） ====================

    @staticmethod
    def _precheck_image(image_path: str) -> Tuple[bool, float, str]:
        """
        识别前图像预检：确认图片主体是否为叶片特写。

        模型基于 PlantVillage 训练（单片叶子特写、背景干净）。拍摄果实/花/
        整株/干枯叶片等非叶片主体时，模型会系统性误判（往往错误偏向"健康"，
        例如带病斑的番茄果实被判 99.7% 健康）。预检通过绿色占比粗判主体：

        - 绿色占比 < 15%：主体基本不是叶片（果实/花/干枯/背景）→ 拦截，提示重拍
        - 15%~35%：叶片占比偏低 → 允许识别，但附加拍摄建议
        - >= 35%：主体为叶片 → 放行

        Returns: (是否放行, 绿色占比, 提示信息)
        """
        from PIL import Image
        try:
            img = Image.open(image_path).convert("RGB")
        except Exception:
            return True, 0.0, ""  # 打不开的图交给主流程报错
        img.thumbnail((400, 400))
        px = list(img.getdata())
        total = len(px) or 1
        green = sum(1 for r, g, b in px if g > r + 20 and g > b + 20 and g > 60)
        green_ratio = green / total

        if green_ratio < 0.15:
            return False, green_ratio, (
                f"图片主体不是叶片（绿色占比仅 {green_ratio:.0%}），疑似拍摄了果实、"
                "花朵、茎秆或干枯部分。当前模型只支持『叶片特写』识别，"
                "请改拍单片叶子：叶子完整、叶片占画面 70% 以上、光线均匀。"
            )
        if green_ratio < 0.35:
            return True, green_ratio, (
                f"提示：叶片在画面中占比较小（绿色占比 {green_ratio:.0%}），"
                "识别结果仅供参考，建议贴近拍摄叶片特写以提高准确率。"
            )
        return True, green_ratio, ""

    # ==================== 核心识别接口 ====================

    def detect_from_image(self, image_path: str, source: str = "upload", crop: str = "auto") -> Dict:
        """
        识别图片中的病虫害。

        Args:
            image_path: 图片文件路径
            source: 来源标识（upload / camera）
            crop: 作物类型（CROP_PREFIXES 的 key，如 "tomato" / "strawberry"）。
                  选择作物后按作物过滤模型类别，显著提升准确率。

        Returns:
            识别结果 dict:
            {
                "success": bool,
                "engine": "efficientnet"/"yolo"/"color"/"mock",
                "image_path": str,
                "detections": [{"class": str, "name": str, "confidence": float,
                                "level": str, "advice": str, "bbox": [x1,y1,x2,y2]}],
                "summary": {"healthy": bool, "primary_pest": str, "max_confidence": float,
                            "recognized": bool, ...},
                "timestamp": str,
                "source": str,
                "crop": str,
            }
        """
        if crop not in CROP_PREFIXES:
            crop = "auto"
        result = {
            "success": False,
            "engine": self._engine,
            "image_path": image_path,
            "detections": [],
            "summary": {"healthy": True, "primary_pest": "", "max_confidence": 0.0},
            "timestamp": datetime.now().isoformat(),
            "source": source,
            "crop": crop,
        }

        if not os.path.exists(image_path):
            result["error"] = f"图片文件不存在: {image_path}"
            logger.error(result["error"])
            return result

        # ---- 图像预检：非叶片主体直接拦截，避免"拍果实报健康"式误判 ----
        pass_check, green_ratio, precheck_note = self._precheck_image(image_path)
        result["green_ratio"] = round(green_ratio, 4)
        if not pass_check:
            result["success"] = True
            result["precheck_failed"] = True
            result["summary"]["recognized"] = False
            result["summary"]["healthy"] = True
            result["summary"]["primary_pest"] = "拍摄对象不符合识别要求"
            result["summary"]["confidence_level"] = "无法识别"
            result["summary"]["note"] = precheck_note
            with self._lock:
                self._latest_result = result
            # 预检拦截不写识别记录库，避免被误读为"识别结果为健康"
            logger.warning(f"病虫害识别预检拦截（非叶片主体，绿色占比 {green_ratio:.0%}）: {image_path}")
            return result

        try:
            if self._engine == "efficientnet":
                detections = self._detect_efficientnet(image_path, crop)
            elif self._engine == "yolo":
                detections = self._detect_yolo(image_path, crop)
            elif self._engine == "color":
                detections = self._detect_color(image_path)
            else:
                detections = self._detect_mock()

            result["detections"] = detections
            result["success"] = True

            # 汇总（区分主要结果和候选参考）
            if detections:
                # Top1 为主要结果（确诊），其余为候选参考
                primary = detections[0]
                candidates = detections[1:]
                result["candidates"] = candidates

                conf = primary["confidence"]

                # ---- 拒识：置信度过低时不硬判 ----
                # 典型场景：拍摄的作物不在模型支持范围内（如黄瓜/生菜叶片），
                # 模型只能从 38 类里硬选，Top1 置信度会崩到 30% 以下。
                # 此时继续判"健康"会误导用户，应明确告知"无法可靠识别"。
                if conf < REJECT_CONF:
                    result["summary"]["recognized"] = False
                    result["summary"]["healthy"] = True  # 不误报病害
                    result["summary"]["max_confidence"] = round(conf, 4)
                    result["summary"]["confidence_level"] = "无法识别"
                    result["summary"]["primary_pest"] = "无法可靠识别"
                    result["summary"]["note"] = (
                        f"识别置信度过低（{conf:.0%}），无法可靠判断。可能原因：\n"
                        "① 拍摄的作物不在模型支持范围内——当前支持：番茄、草莓、辣椒/甜椒、"
                        "土豆、玉米、葡萄、苹果、桃、南瓜、柑橘、樱桃、蓝莓、树莓、大豆；"
                        "（黄瓜、生菜等暂不支持，需另行训练模型）\n"
                        "② 拍摄质量不佳。请在光线充足处拍摄单片叶子的特写，"
                        "让叶片占画面 70% 以上，避免背景杂物。"
                    )
                else:
                    result["summary"]["recognized"] = True
                    result["summary"]["max_confidence"] = round(conf, 4)
                    result["summary"]["primary_pest"] = primary["name"]
                    result["summary"]["primary_class"] = primary["class"]

                    # 置信度等级
                    if conf >= 0.85:
                        result["summary"]["confidence_level"] = "高"
                    elif conf >= 0.6:
                        result["summary"]["confidence_level"] = "中"
                    else:
                        result["summary"]["confidence_level"] = "低"

                    # 健康判断：只有 Top1 是健康类才判健康
                    result["summary"]["healthy"] = (primary["class"] == "healthy")

                    # 低置信度提示
                    if conf < 0.6:
                        result["summary"]["note"] = (
                            "置信度较低，建议在光线充足处重新拍摄清晰的叶片特写照片。"
                        )
            else:
                result["summary"]["healthy"] = True
                result["summary"]["confidence_level"] = "无"
                result["candidates"] = []

            # 附加预检拍摄建议（叶片占比偏低时）
            if precheck_note:
                old_note = result["summary"].get("note", "")
                result["summary"]["note"] = (
                    (old_note + "\n" + precheck_note).strip() if old_note else precheck_note
                )

            # 缓存
            with self._lock:
                self._latest_result = result

            # 持久化
            self._save_detection(result)

            # MQTT 发布
            self._publish_mqtt(result)

            # 告警联动（仅 Top1 确诊为病害且置信度 >= 0.6 才触发，避免误报）
            if not result["summary"]["healthy"] and detections and result["summary"].get("recognized", False):
                if detections[0]["confidence"] >= 0.6:
                    self._trigger_alert(detections[0])

            logger.info(
                f"病虫害识别完成 [{self._engine}] crop={crop}: "
                f"识别={result['summary'].get('recognized')}, "
                f"健康={result['summary']['healthy']}, "
                f"主要={result['summary']['primary_pest']}, "
                f"置信度={result['summary']['max_confidence']}"
            )

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"病虫害识别异常: {e}", exc_info=True)

        return result

    def detect_from_camera(self, camera_id: str = "default") -> Dict:
        """
        从摄像头抓拍并识别（预留接口，后期接入真实摄像头）。
        当前实现：检查是否有抓拍图片，没有则返回提示。
        """
        # 预留：后期可接入 OpenCV VideoCapture 或 ESP32-CAM 抓拍
        # 目前返回提示，不影响系统运行
        logger.info(f"摄像头抓拍接口被调用 (camera_id={camera_id})，当前为预留接口")
        return {
            "success": False,
            "engine": self._engine,
            "error": "摄像头接口为预留功能，请先通过图片上传方式识别",
            "timestamp": datetime.now().isoformat(),
            "source": "camera",
        }

    # ==================== EfficientNet 分类识别（首选） ====================

    def _detect_efficientnet(self, image_path: str, crop: str = "auto") -> List[Dict]:
        """使用 EfficientNet-B0 分类模型识别（PlantVillage 38类，准确率最高）。
        crop 指定作物后，softmax 结果按作物类别过滤并重新归一化，
        避免跨作物误报（如把黄瓜叶硬判成玉米叶斑病）。"""
        import torch
        from PIL import Image

        img = Image.open(image_path).convert("RGB")
        input_tensor = self._eff_transform(img).unsqueeze(0)

        prefix = CROP_PREFIXES.get(crop)

        with torch.no_grad():
            outputs = self._eff_model(input_tensor)
            probs = torch.nn.functional.softmax(outputs, dim=1)

            if prefix is not None:
                # 只保留该作物的类别索引
                keep = [
                    i for i, n in enumerate(self._class_names)
                    if n.startswith(prefix) or n == "healthy"
                ]
                if keep:
                    keep_tensor = torch.tensor(keep, dtype=torch.long)
                    filtered = probs[0][keep_tensor]
                    # 过滤后重新归一化（作物内相对概率）
                    total = filtered.sum()
                    if total > 0:
                        probs = filtered.unsqueeze(0) / total
                        indices = keep_tensor
                        topk_k = min(5, len(indices))
                        top_probs, top_pos = torch.topk(probs, topk_k)
                        top5_probs = top_probs[0]
                        top5_indices = indices[top_pos[0]]
                    else:
                        top5_probs, top5_indices = torch.topk(probs, 5)
                else:
                    top5_probs, top5_indices = torch.topk(probs, 5)
                    top5_probs = top5_probs[0]
                    top5_indices = top5_indices[0]
            else:
                top5_probs, top5_indices = torch.topk(probs, 5)
                top5_probs = top5_probs[0]
                top5_indices = top5_indices[0]

        detections = []
        for i in range(len(top5_indices)):
            cls_idx = int(top5_indices[i].item())
            conf = float(top5_probs[i].item())
            if conf < 0.005:  # 低于 0.5% 的忽略
                continue
            cls_name = self._class_names[cls_idx]
            pest_info = self._get_pest_info(cls_name)
            detections.append({
                "class": self._normalize_class_name(cls_name),
                "name": pest_info["name"],
                "confidence": round(conf, 4),
                "level": pest_info["level"],
                "advice": pest_info["advice"],
                "bbox": [],
            })

        detections.sort(key=lambda x: x["confidence"], reverse=True)
        # 候选去重：同名类别只保留置信度最高的一条（多个作物的 healthy 类
        # 归一化后都叫 healthy，此前会重复出现，现已合并）
        seen: Dict[str, Dict] = {}
        for d in detections:
            key = d["class"]
            if key not in seen or d["confidence"] > seen[key]["confidence"]:
                seen[key] = d
        return sorted(seen.values(), key=lambda x: x["confidence"], reverse=True)

    # ==================== YOLO 识别 ====================

    @staticmethod
    def _normalize_class_name(cls_name: str) -> str:
        """
        将 PlantVillage 数据集的类别名转为内部标准格式。
        PlantVillage 格式: "Tomato___Early_blight" / "Tomato___healthy"
        内部格式: "tomato_early_blight" / "healthy"
        """
        name = cls_name.strip().lower()
        # 替换三个下划线为一个
        name = name.replace("___", "_").replace("__", "_")
        # 替换空格
        name = name.replace(" ", "_")
        # 去掉作物前缀中重复的部分，统一处理 healthy
        if name.endswith("_healthy") or name == "healthy":
            return "healthy"
        return name

    def _get_pest_info(self, cls_name: str) -> Dict:
        """根据类别名获取病虫害信息（先查标准库，查不到则用通用信息）"""
        # 先尝试直接匹配
        if cls_name in PEST_CLASSES:
            return PEST_CLASSES[cls_name]
        # 尝试归一化后匹配
        normalized = self._normalize_class_name(cls_name)
        if normalized in PEST_CLASSES:
            return PEST_CLASSES[normalized]
        # 尝试模糊匹配（包含关键词）
        for key, info in PEST_CLASSES.items():
            if key in normalized or normalized in key:
                return info
        # 通用兜底
        display_name = cls_name.replace("_", " ").replace("  ", " ").title()
        return {
            "name": display_name,
            "level": "warning",
            "advice": "检测到异常，建议咨询专业农技人员确认病害类型并采取相应防治措施。",
        }

    def _detect_yolo(self, image_path: str, crop: str = "auto") -> List[Dict]:
        """使用 YOLOv8 模型识别（自动支持分类模型和检测模型）"""
        results = self._model.predict(
            source=image_path,
            conf=self.conf_threshold,
            verbose=False,
        )
        detections = []

        if not results:
            return detections

        r = results[0]
        prefix = CROP_PREFIXES.get(crop)

        # 判断模型类型：分类模型有 probs，检测模型有 boxes
        if r.probs is not None and hasattr(r.probs, 'top1'):
            # ===== 分类模型（yolov8n-cls）=====
            probs = r.probs
            # 取 top5 类别（作物过滤在循环内判断）
            top_indices = probs.top5[:5] if hasattr(probs, 'top5') and probs.top5 else [probs.top1]
            top_confs = probs.top5conf[:5] if hasattr(probs, 'top5conf') else [probs.top1conf]

            for cls_id, conf in zip(top_indices, top_confs):
                cls_id = int(cls_id)
                conf = float(conf)
                if conf < self.conf_threshold * 0.5:  # 分类模型阈值放宽一半
                    continue
                cls_name = self._model.names.get(cls_id, f"class_{cls_id}")
                # 作物过滤：只保留所选作物的类别
                if prefix is not None:
                    norm = cls_name.strip().lower()
                    if not (norm.startswith(prefix.lower()) or norm == "healthy"):
                        continue
                pest_info = self._get_pest_info(cls_name)
                detections.append({
                    "class": self._normalize_class_name(cls_name),
                    "name": pest_info["name"],
                    "confidence": round(conf, 4),
                    "level": pest_info["level"],
                    "advice": pest_info["advice"],
                    "bbox": [],
                })

        elif r.boxes is not None:
            # ===== 目标检测模型（yolov8n）=====
            for box in r.boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                cls_name = self._model.names.get(cls_id, f"class_{cls_id}")
                bbox = [float(x) for x in box.xyxy[0].tolist()]
                pest_info = self._get_pest_info(cls_name)
                detections.append({
                    "class": self._normalize_class_name(cls_name),
                    "name": pest_info["name"],
                    "confidence": round(conf, 4),
                    "level": pest_info["level"],
                    "advice": pest_info["advice"],
                    "bbox": bbox,
                })

        # 按置信度降序
        detections.sort(key=lambda x: x["confidence"], reverse=True)
        return detections

    # ==================== 颜色分析降级识别 ====================

    def _detect_color(self, image_path: str) -> List[Dict]:
        """
        基于 PIL 的颜色分析降级方案：
        - 统计叶片绿色占比（健康指标）
        - 检测黄色/褐色/斑点区域（病害指标）
        - 综合评分给出健康/疑似病害判断
        """
        from PIL import Image

        img = Image.open(image_path).convert("RGB")
        # 缩放到固定尺寸加速分析
        img.thumbnail((400, 400))
        pixels = list(img.getdata())
        total = len(pixels)

        green_count = 0
        yellow_count = 0
        brown_count = 0
        spot_count = 0

        for r, g, b in pixels:
            # 绿色：G 明显大于 R 和 B
            if g > r + 20 and g > b + 20 and g > 60:
                green_count += 1
            # 黄色：R 和 G 高，B 低
            elif r > 150 and g > 130 and b < 100:
                yellow_count += 1
            # 褐色/病斑：R 中等，G 低，B 低
            elif 80 < r < 180 and g < 100 and b < 80:
                brown_count += 1
            # 深色斑点
            elif r < 80 and g < 80 and b < 80 and total > 0:
                spot_count += 1

        green_ratio = green_count / total if total else 0
        yellow_ratio = yellow_count / total if total else 0
        brown_ratio = brown_count / total if total else 0
        spot_ratio = spot_count / total if total else 0

        detections = []

        # 健康判断：绿色占比高且异常色少
        if green_ratio > 0.4 and yellow_ratio < 0.05 and brown_ratio < 0.03:
            conf = min(0.95, 0.6 + green_ratio * 0.5)
            detections.append({
                "class": "healthy",
                "name": "健康",
                "confidence": round(conf, 4),
                "level": "info",
                "advice": PEST_CLASSES["healthy"]["advice"],
                "bbox": [],
            })
        else:
            # 疑似病害：根据主导异常色判断类型
            if yellow_ratio > brown_ratio and yellow_ratio > 0.03:
                conf = min(0.85, 0.4 + yellow_ratio * 3)
                detections.append({
                    "class": "suspected_yellowing",
                    "name": "疑似黄化类病害",
                    "confidence": round(conf, 4),
                    "level": "warning",
                    "advice": "叶片出现黄化，可能是缺氮、病毒病或根腐病。建议检查根系、补充氮肥，严重时送检确认。",
                    "bbox": [],
                })
            if brown_ratio > 0.02 or spot_ratio > 0.02:
                conf = min(0.85, 0.4 + (brown_ratio + spot_ratio) * 3)
                detections.append({
                    "class": "suspected_leaf_spot",
                    "name": "疑似叶斑/炭疽类病害",
                    "confidence": round(conf, 4),
                    "level": "warning",
                    "advice": "叶片出现褐色斑点，可能是炭疽病、叶斑病或早疫病。建议摘除病叶，喷施代森锰锌或苯醚甲环唑。",
                    "bbox": [],
                })
            if not detections:
                # 绿色占比低但无明显异常色，可能是光照/角度问题
                detections.append({
                    "class": "healthy",
                    "name": "健康（低置信度）",
                    "confidence": 0.5,
                    "level": "info",
                    "advice": "图片分析置信度较低，建议在光线充足处重新拍摄清晰叶片照片。",
                    "bbox": [],
                })

        detections.sort(key=lambda x: x["confidence"], reverse=True)
        return detections

    # ==================== 模拟识别 ====================

    def _detect_mock(self) -> List[Dict]:
        """模拟识别结果（兜底）"""
        import random
        if random.random() > 0.5:
            return [{
                "class": "healthy",
                "name": "健康",
                "confidence": 0.88,
                "level": "info",
                "advice": PEST_CLASSES["healthy"]["advice"],
                "bbox": [],
            }]
        pests = ["tomato_early_blight", "cucumber_powdery_mildew", "aphid"]
        cls = random.choice(pests)
        info = PEST_CLASSES[cls]
        return [{
            "class": cls,
            "name": info["name"],
            "confidence": round(random.uniform(0.6, 0.9), 4),
            "level": info["level"],
            "advice": info["advice"],
            "bbox": [],
        }]

    # ==================== 持久化 / MQTT / 告警 ====================

    def _save_detection(self, result: Dict):
        """保存识别记录到数据库"""
        if self.storage is None:
            return
        try:
            if hasattr(self.storage, "save_pest_detection"):
                self.storage.save_pest_detection(result)
        except Exception as e:
            logger.error(f"保存病虫害识别记录失败: {e}")

    def _publish_mqtt(self, result: Dict):
        """发布识别结果到 MQTT"""
        if self.mqtt is None or not self.mqtt.is_connected:
            return
        try:
            payload = {
                "engine": result["engine"],
                "healthy": result["summary"]["healthy"],
                "primary_pest": result["summary"]["primary_pest"],
                "max_confidence": result["summary"]["max_confidence"],
                "detection_count": len(result["detections"]),
                "source": result["source"],
                "timestamp": result["timestamp"],
                "source": "server",
            }
            self.mqtt.publish(self._topic_pest, payload)
        except Exception as e:
            logger.error(f"MQTT 发布病虫害识别结果失败: {e}")

    def _trigger_alert(self, detection: Dict):
        """检测到病虫害时触发告警"""
        if self.alert_callback is None:
            return
        try:
            alert = {
                "level": detection.get("level", "warning"),
                "param": "pest_detection",
                "value": detection.get("confidence", 0),
                "threshold": self.conf_threshold,
                "message": (
                    f"检测到疑似病虫害：{detection['name']} "
                    f"（置信度 {detection['confidence']:.0%}）。建议：{detection['advice']}"
                ),
                "timestamp": datetime.now().isoformat(),
            }
            self.alert_callback(alert)
        except Exception as e:
            logger.error(f"病虫害告警触发失败: {e}")

    # ==================== 查询接口 ====================

    def get_latest_result(self) -> Optional[Dict]:
        """获取最近一次识别结果"""
        with self._lock:
            return self._latest_result

    def get_history(self, limit: int = 20) -> List[Dict]:
        """获取识别历史（从数据库）"""
        if self.storage and hasattr(self.storage, "get_recent_pest_detections"):
            try:
                return self.storage.get_recent_pest_detections(limit)
            except Exception as e:
                logger.error(f"查询病虫害识别历史失败: {e}")
        return []
