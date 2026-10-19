# download_pest_model.py - 下载预训练病虫害识别模型
#
# 从 Hugging Face 下载已训练好的 YOLOv8 作物病害检测模型
# 国内自动使用 hf-mirror.com 镜像加速
# 下载后自动放到 models/pest_yolov8n.pt，重启系统即可启用 YOLO 识别

import os
import sys
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
MODELS_DIR = PROJECT_DIR / "models"
TARGET_PATH = MODELS_DIR / "pest_yolov8n.pt"

# 候选模型源（按优先级尝试，第一个成功就用）
MODEL_SOURCES = [
    {
        "name": "YOLOv8-TransFPN 作物病害检测 (HuggingFace)",
        "url": "https://hf-mirror.com/iamnotpalak/yolov8-transfpn-crop-disease-detection/resolve/main/weights/best.pt",
        "fallback_url": "https://huggingface.co/iamnotpalak/yolov8-transfpn-crop-disease-detection/resolve/main/weights/best.pt",
        "type": "detect",  # 目标检测模型
    },
    {
        "name": "PlantVillage 多类 YOLO (GitHub)",
        "url": "https://ghfast.top/https://github.com/DataWithAnish/PlantVillage-Multiclass-YOLO/raw/main/best.pt",
        "fallback_url": "https://raw.githubusercontent.com/DataWithAnish/PlantVillage-Multiclass-YOLO/main/best.pt",
        "type": "detect",
    },
]


def download_file(url, dest_path, source_name):
    """下载文件，显示进度"""
    print(f"\n正在从 {source_name} 下载...")
    print(f"地址: {url}")

    def report_progress(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            percent = min(100, downloaded * 100 / total_size)
            mb_downloaded = downloaded / 1024 / 1024
            mb_total = total_size / 1024 / 1024
            print(f"\r  进度: {percent:5.1f}% ({mb_downloaded:5.1f}/{mb_total:5.1f} MB)", end="")

    try:
        urllib.request.urlretrieve(url, str(dest_path), reporthook=report_progress)
        print()  # 换行
        file_size = dest_path.stat().st_size / 1024 / 1024
        print(f"  下载完成! 文件大小: {file_size:.1f} MB")
        return True
    except Exception as e:
        print(f"\n  下载失败: {e}")
        if dest_path.exists():
            dest_path.unlink()
        return False


def verify_model(model_path):
    """验证模型文件是否能被 ultralytics 正常加载，并输出类别信息"""
    print(f"\n正在验证模型: {model_path}")
    try:
        from ultralytics import YOLO
        model = YOLO(str(model_path))
        print(f"  模型加载成功!")
        print(f"  模型类型: {'检测模型' if model.task == 'detect' else '分类模型' if model.task == 'classify' else model.task}")
        print(f"  类别数量: {len(model.names)}")
        print(f"  类别列表:")
        for cls_id, cls_name in model.names.items():
            print(f"    {cls_id:2d}: {cls_name}")
        return True
    except Exception as e:
        print(f"  模型验证失败: {e}")
        return False


def main():
    print("=" * 60)
    print("  病虫害识别预训练模型下载工具")
    print("=" * 60)

    # 检查 ultralytics
    try:
        import ultralytics
        print(f"\nultralytics 已安装: {ultralytics.__version__}")
    except ImportError:
        print("\n错误: 未安装 ultralytics，请先运行:")
        print("  pip install ultralytics -i https://pypi.tuna.tsinghua.edu.cn/simple")
        sys.exit(1)

    # 创建目录
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # 检查是否已存在
    if TARGET_PATH.exists() and TARGET_PATH.stat().st_size > 1000000:
        print(f"\n模型文件已存在: {TARGET_PATH} ({TARGET_PATH.stat().st_size / 1024 / 1024:.1f} MB)")
        verify_model(TARGET_PATH)
        print("\n如需重新下载，请先删除现有模型文件。")
        return

    # 依次尝试下载
    success = False
    for source in MODEL_SOURCES:
        for url in [source["url"], source.get("fallback_url")]:
            if not url:
                continue
            if download_file(url, TARGET_PATH, source["name"]):
                if TARGET_PATH.stat().st_size > 1000000:  # 大于 1MB 才算成功
                    success = True
                    break
                else:
                    print("  文件过小，可能下载不完整，尝试下一个源...")
                    TARGET_PATH.unlink(missing_ok=True)
        if success:
            break

    if not success:
        print("\n" + "=" * 60)
        print("  所有下载源均失败。")
        print("  请手动下载模型文件并放到:")
        print(f"    {TARGET_PATH}")
        print("\n  可用的手动下载地址:")
        for source in MODEL_SOURCES:
            print(f"    - {source['name']}: {source['url']}")
        print("=" * 60)
        sys.exit(1)

    # 验证模型
    verify_model(TARGET_PATH)

    print("\n" + "=" * 60)
    print(f"  模型已就绪: {TARGET_PATH}")
    print("  下一步: 重启智慧农业系统")
    print("  启动日志中会显示: YOLOv8 识别引擎已加载")
    print("  Web 面板引擎标签会从 '颜色分析' 变为 'YOLOv8'")
    print("=" * 60)


if __name__ == "__main__":
    main()
