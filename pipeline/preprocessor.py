"""
pipeline/preprocessor.py
------------------------
从操作录屏视频中提取关键帧。

主要职责：
  1. 按固定间隔抽帧（粗筛）
  2. 用 SSIM 结构相似度过滤重复帧（精筛）
  3. 可选：检测鼠标点击区域并裁剪局部放大图
  4. 输出带时间戳的帧列表，供 analyzer.py 消费

无任何外部 API 依赖，只需 opencv-python + scikit-image。
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from skimage.metrics import structural_similarity as ssim


# ─────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────

@dataclass
class FrameInfo:
    """单个关键帧的元数据"""
    frame_idx: int          # 原始帧序号
    timestamp: float        # 对应视频时间（秒）
    path: str               # 保存到磁盘的路径
    ssim_score: float       # 与上一关键帧的相似度（越低越不同）
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict:
        return {
            "frame_idx": self.frame_idx,
            "timestamp": self.timestamp,
            "path": self.path,
            "ssim_score": self.ssim_score,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class PreprocessResult:
    """extract_key_frames 的返回值"""
    frames: list[FrameInfo] = field(default_factory=list)
    total_frames: int = 0
    duration_sec: float = 0.0
    fps: float = 0.0
    video_width: int = 0
    video_height: int = 0
    elapsed_sec: float = 0.0           # 处理耗时

    @property
    def count(self) -> int:
        return len(self.frames)

    def summary(self) -> str:
        return (
            f"视频 {self.duration_sec:.1f}s / {self.fps:.1f}fps / "
            f"{self.video_width}x{self.video_height}  →  "
            f"从 {self.total_frames} 帧中提取 {self.count} 个关键帧  "
            f"耗时 {self.elapsed_sec:.1f}s"
        )


# ─────────────────────────────────────────────
# 核心函数
# ─────────────────────────────────────────────

def extract_key_frames(
    video_path: str,
    output_dir: str = "./frames",
    *,
    similarity_threshold: float = 0.92,
    sample_interval_sec: float = 0.2,
    compare_size: tuple[int, int] = (320, 240),
    min_frames: int = 2,
    clean_output_dir: bool = False,
) -> PreprocessResult:
    """
    从视频中提取关键帧（画面发生明显变化的帧）。

    Parameters
    ----------
    video_path : str
        输入视频文件路径（mp4 / avi / mov 等 OpenCV 支持的格式）。
    output_dir : str
        关键帧 PNG 文件的输出目录，不存在时自动创建。
    similarity_threshold : float
        SSIM 阈值，范围 0~1。低于该值则认为画面发生了变化。
        推荐值：
          0.95 — 只捕捉大变化（页面跳转、弹窗）
          0.92 — 默认，捕捉表单填写、按钮点击（推荐）
          0.85 — 捕捉微小变化（光标移动、悬停效果）
    sample_interval_sec : float
        抽帧间隔（秒）。0.2 表示每 0.2 秒最多取一帧，
        可过滤掉动画过渡期间的中间帧，降低噪声。
    compare_size : tuple[int, int]
        SSIM 比较前将帧缩放到的尺寸，越小越快但精度略降。
    min_frames : int
        至少保留的帧数（即使全部相似也会强制保留首尾帧）。
    clean_output_dir : bool
        True 时先清空 output_dir，适合重新处理同一视频。

    Returns
    -------
    PreprocessResult
        包含 FrameInfo 列表及视频元信息。

    Raises
    ------
    FileNotFoundError
        视频文件不存在。
    RuntimeError
        视频无法打开（格式不支持或文件损坏）。
    """
    video_path = str(video_path)
    if not Path(video_path).exists():
        raise FileNotFoundError(f"视频文件不存在: {video_path}")

    out_dir = Path(output_dir)
    if clean_output_dir and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")

    # 读取视频元信息
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    video_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    video_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration_sec = total_frames / fps

    # 根据 sample_interval_sec 计算每隔多少帧取一次样
    sample_every_n = max(1, int(fps * sample_interval_sec))

    result = PreprocessResult(
        total_frames=total_frames,
        duration_sec=duration_sec,
        fps=fps,
        video_width=video_width,
        video_height=video_height,
    )

    t_start = time.perf_counter()
    prev_gray: np.ndarray | None = None
    frame_idx = 0
    key_frames: list[FrameInfo] = []

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # 按间隔采样，跳过中间帧
        if frame_idx % sample_every_n != 0:
            frame_idx += 1
            continue

        gray = _to_compare_gray(frame, compare_size)
        timestamp = round(frame_idx / fps, 3)

        if prev_gray is None:
            # 第一帧无条件保留
            info = _save_frame(frame, frame_idx, timestamp, out_dir, ssim_score=0.0)
            key_frames.append(info)
            prev_gray = gray
        else:
            score = _ssim(prev_gray, gray)
            if score < similarity_threshold:
                info = _save_frame(frame, frame_idx, timestamp, out_dir, ssim_score=score)
                key_frames.append(info)
                prev_gray = gray  # 以当前帧为新基准

        frame_idx += 1

    cap.release()

    # 保证至少有 min_frames 帧：如果只抽到 1 帧，强制保留最后一帧
    if len(key_frames) < min_frames and total_frames > 1:
        cap2 = cv2.VideoCapture(video_path)
        cap2.set(cv2.CAP_PROP_POS_FRAMES, total_frames - 1)
        ret, last_frame = cap2.read()
        cap2.release()
        if ret:
            last_ts = round((total_frames - 1) / fps, 3)
            info = _save_frame(last_frame, total_frames - 1, last_ts, out_dir, ssim_score=0.0)
            # 避免与已有帧重复
            if not key_frames or key_frames[-1].frame_idx != total_frames - 1:
                key_frames.append(info)

    result.frames = key_frames
    result.elapsed_sec = round(time.perf_counter() - t_start, 2)
    return result


# ─────────────────────────────────────────────
# 私有工具函数
# ─────────────────────────────────────────────

def _to_compare_gray(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """将帧缩小并转灰度，用于 SSIM 比较"""
    small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)


def _ssim(a: np.ndarray, b: np.ndarray) -> float:
    """计算两张灰度图的结构相似度，返回 0~1"""
    score, _ = ssim(a, b, full=True)
    return float(score)


def _save_frame(
    frame: np.ndarray,
    frame_idx: int,
    timestamp: float,
    out_dir: Path,
    ssim_score: float,
) -> FrameInfo:
    """将帧写入磁盘，返回 FrameInfo"""
    filename = f"frame_{frame_idx:06d}_t{timestamp:.3f}s.png"
    path = str(out_dir / filename)
    cv2.imwrite(path, frame)
    h, w = frame.shape[:2]
    return FrameInfo(
        frame_idx=frame_idx,
        timestamp=timestamp,
        path=path,
        ssim_score=round(ssim_score, 4),
        width=w,
        height=h,
    )