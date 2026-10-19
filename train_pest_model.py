# train_pest_model.py - 病虫害识别模型训练脚本
#
# 使用 PlantVillage 公开数据集训练 YOLOv8 分类模型
# 数据集：54,305 张叶片图片，38 类病虫害（含健康）
#
# 使用方法：
#   1. 先运行本脚本下载数据集并训练
#   2. 训练完成后模型自动复制到 models/pest_yolov8n.pt
#   3. 重启智慧农业系统，自动切换到 YOLO 引擎
#
# 硬件要求：
#   - 有 NVIDIA GPU：训练约 30-60 分钟
#   - 纯 CPU：训练约 4-8 小时（建议用 epochs=20 快速验证）

import os
import shutil
import sys
import zipfile
import urllib.request
from pathlib import Path

# ==================== 配置 ====================
PROJECT_DIR = Path(__file__).parent
DATA_DIR = PROJECT_DIR / "data" / "plantvillage"
MODELS_DIR = PROJECT_DIR / "models"
RUNS_DIR = PROJECT_DIR / "runs"

# 数据集下载地址（PlantVillage 数据集，按类别分文件夹）
# 使用 GitHub 镜像，国内可访问
DATASET_URL = "https://github.com/spMohanty/PlantVillage-Dataset/archive/refs/heads/master.zip"
DATASET_ZIP = PROJECT_DIR / "data" / "plantvillage.zip"

# 训练参数
EPOCHS = 50          # 训练轮次（CPU 建议降到 20）
IMG_SIZE = 224       # 输入图片尺寸（分类任务常用 224）
BATCH_SIZE = 32      # 批次大小（GPU 显存小则降到 16）
MODEL_NAME = "yolov8n-cls.pt"  # YOLOv8n 分类模型（轻量，适合CPU推理）

# 我们关心的大棚作物类别（PlantVillage 38 类中筛选）
TARGET_CROPS = [
    "Tomato", "Cucumber", "Strawberry", "Pepper", "Lettuce",
    "Apple", "Grape", "Corn", "Potato", "Soybean", "Squash",
]


def download_dataset():
    """下载 PlantVillage 数据集"""
    DATA_DIR.parent.mkdir(parents=True, exist_ok=True)

    if DATASET_ZIP.exists() and DATASET_ZIP.stat().st_size > 1000000:
        print(f"[1/4] 数据集压缩包已存在: {DATASET_ZIP}")
        return

    print(f"[1/4] 正在下载 PlantVillage 数据集...")
    print(f"      地址: {DATASET_URL}")
    print(f"      大小: 约 1.5GB，请耐心等待")

    def report_progress(block_num, block_size, total_size):
        downloaded = block_num * block_size
        percent = min(100, downloaded * 100 / total_size)
        mb_downloaded = downloaded / 1024 / 1024
        mb_total = total_size / 1024 / 1024
        print(f"\r      进度: {percent:.1f}% ({mb_downloaded:.1f}/{mb_total:.1f} MB)", end="")

    try:
        urllib.request.urlretrieve(DATASET_URL, str(DATASET_ZIP), reporthook=report_progress)
        print("\n      下载完成!")
    except Exception as e:
        print(f"\n      下载失败: {e}")
        print("\n      备用方案：手动下载数据集")
        print("      1. 访问 https://www.kaggle.com/datasets/emmarex/plantdisease")
        print("      2. 下载后解压到 data/plantvillage/ 目录")
        print("      3. 确保目录结构为 data/plantvillage/raw/类别名/图片.jpg")
        sys.exit(1)


def extract_and_prepare():
    """解压数据集并整理为 YOLO 分类格式"""
    print(f"\n[2/4] 解压并整理数据集...")

    raw_dir = DATA_DIR / "raw"
    if raw_dir.exists() and any(raw_dir.iterdir()):
        print(f"      数据集已解压: {raw_dir}")
        class_count = len([d for d in raw_dir.iterdir() if d.is_dir()])
        print(f"      类别数: {class_count}")
        return raw_dir

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 解压
    print(f"      正在解压 {DATASET_ZIP} ...")
    with zipfile.ZipFile(str(DATASET_ZIP), 'r') as zf:
        zf.extractall(str(DATA_DIR))

    # 查找解压后的实际数据目录
    # PlantVillage-Dataset-master/raw/color/ 下是按类别分的文件夹
    extracted_dirs = list(DATA_DIR.glob("PlantVillage-Dataset-*"))
    if extracted_dirs:
        source_base = extracted_dirs[0] / "raw" / "color"
    else:
        source_base = DATA_DIR / "raw" / "color"

    if not source_base.exists():
        # 尝试其他可能的结构
        for p in DATA_DIR.rglob("*"):
            if p.is_dir() and any(p.glob("*.JPG")):
                source_base = p.parent
                break

    if not source_base.exists():
        print(f"      错误: 找不到数据目录 {source_base}")
        print(f"      请手动检查 {DATA_DIR} 下的目录结构")
        sys.exit(1)

    # 复制到 raw 目录（统一路径）
    print(f"      整理类别文件夹到 {raw_dir} ...")
    raw_dir.mkdir(parents=True, exist_ok=True)

    class_dirs = [d for d in source_base.iterdir() if d.is_dir()]
    print(f"      找到 {len(class_dirs)} 个类别文件夹")

    for class_dir in class_dirs:
        dest = raw_dir / class_dir.name
        if not dest.exists():
            shutil.copytree(str(class_dir), str(dest))

    # 清理解压的临时文件
    for d in extracted_dirs:
        shutil.rmtree(str(d), ignore_errors=True)

    class_count = len([d for d in raw_dir.iterdir() if d.is_dir()])
    print(f"      整理完成，共 {class_count} 个类别")
    return raw_dir


def split_dataset(raw_dir):
    """按 80/10/10 划分训练/验证/测试集"""
    print(f"\n[3/4] 划分数据集（训练80% / 验证10% / 测试10%）...")

    import random
    random.seed(42)

    dataset_dir = DATA_DIR / "yolo_cls"
    if (dataset_dir / "train").exists():
        print(f"      数据集已划分: {dataset_dir}")
        return dataset_dir

    for split in ["train", "val", "test"]:
        (dataset_dir / split).mkdir(parents=True, exist_ok=True)

    class_dirs = sorted([d for d in raw_dir.iterdir() if d.is_dir()])

    for class_dir in class_dirs:
        images = sorted([f for f in class_dir.iterdir()
                         if f.suffix.lower() in ['.jpg', '.jpeg', '.png']])
        random.shuffle(images)

        n = len(images)
        n_train = int(n * 0.8)
        n_val = int(n * 0.1)

        splits = {
            "train": images[:n_train],
            "val": images[n_train:n_train + n_val],
            "test": images[n_train + n_val:],
        }

        for split_name, split_images in splits.items():
            dest_dir = dataset_dir / split_name / class_dir.name
            dest_dir.mkdir(parents=True, exist_ok=True)
            for img in split_images:
                shutil.copy2(str(img), str(dest_dir / img.name))

        print(f"      {class_dir.name}: {len(images)} 张 "
              f"(train={len(splits['train'])}, val={len(splits['val'])}, test={len(splits['test'])})")

    print(f"      划分完成: {dataset_dir}")
    return dataset_dir


def train_model(dataset_dir):
    """训练 YOLOv8 分类模型"""
    print(f"\n[4/4] 开始训练 YOLOv8 分类模型...")
    print(f"      模型: {MODEL_NAME}")
    print(f"      轮次: {EPOCHS}")
    print(f"      图片尺寸: {IMG_SIZE}")
    print(f"      批次大小: {BATCH_SIZE}")
    print(f"      数据集: {dataset_dir}")
    print()

    try:
        from ultralytics import YOLO
    except ImportError:
        print("错误: 未安装 ultralytics，请先运行: pip install ultralytics")
        sys.exit(1)

    # 加载预训练分类模型
    model = YOLO(MODEL_NAME)

    # 训练
    results = model.train(
        data=str(dataset_dir),
        epochs=EPOCHS,
        imgsz=IMG_SIZE,
        batch=BATCH_SIZE,
        project=str(RUNS_DIR),
        name="pest_cls",
        exist_ok=True,
        patience=10,          # 10 轮无提升则早停
        save=True,
        verbose=True,
    )

    # 找到最佳模型
    best_model = RUNS_DIR / "pest_cls" / "weights" / "best.pt"
    if not best_model.exists():
        # 尝试其他可能的路径
        candidates = list(RUNS_DIR.rglob("best.pt"))
        if candidates:
            best_model = candidates[0]
        else:
            print("警告: 未找到 best.pt，使用 last.pt")
            last_model = RUNS_DIR / "pest_cls" / "weights" / "last.pt"
            if last_model.exists():
                best_model = last_model
            else:
                print("错误: 训练完成但未找到模型文件")
                sys.exit(1)

    # 复制到项目 models 目录
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest_model = MODELS_DIR / "pest_yolov8n.pt"
    shutil.copy2(str(best_model), str(dest_model))

    print(f"\n{'='*50}")
    print(f"训练完成!")
    print(f"最佳模型: {best_model}")
    print(f"已复制到: {dest_model}")
    print(f"{'='*50}")
    print(f"\n下一步: 重启智慧农业系统，病虫害识别将自动切换到 YOLO 引擎")
    print(f"启动日志中会显示: YOLOv8 识别引擎已加载: {dest_model}")

    return dest_model


def main():
    print("=" * 60)
    print("  病虫害识别模型训练脚本 (YOLOv8 + PlantVillage)")
    print("=" * 60)
    print()

    # 检查是否有 GPU
    try:
        import torch
        if torch.cuda.is_available():
            print(f"检测到 GPU: {torch.cuda.get_device_name(0)}")
            print(f"训练将使用 GPU 加速")
        else:
            print("未检测到 NVIDIA GPU，将使用 CPU 训练（较慢）")
            print("建议: 将 EPOCHS 降到 20 以快速验证效果")
    except ImportError:
        print("未安装 torch，将在训练时自动安装")

    print()

    # 1. 下载数据集
    download_dataset()

    # 2. 解压整理
    raw_dir = extract_and_prepare()

    # 3. 划分数据集
    dataset_dir = split_dataset(raw_dir)

    # 4. 训练
    train_model(dataset_dir)


if __name__ == "__main__":
    main()
