# video_preprocessor.py
import cv2
import numpy as np
from pathlib import Path
from skimage.metrics import structural_similarity as ssim

def extract_key_frames(video_path: str, output_dir: str, 
                        similarity_threshold: float = 0.92) -> list[dict]:
    """
    从操作录屏中提取关键帧（变化帧），过滤掉相似的重复画面
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    
    key_frames = []
    prev_gray = None
    frame_idx = 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (320, 240))  # 统一尺寸加速比较

        if prev_gray is not None:
            score = ssim(prev_gray, gray)
            if score < similarity_threshold:  # 画面有明显变化
                timestamp = frame_idx / fps
                frame_path = f"{output_dir}/frame_{frame_idx:06d}.png"
                cv2.imwrite(frame_path, frame)
                key_frames.append({
                    "frame_idx": frame_idx,
                    "timestamp": round(timestamp, 2),
                    "path": frame_path,
                    "ssim_score": round(score, 3)
                })
        else:
            # 保存第一帧
            frame_path = f"{output_dir}/frame_{frame_idx:06d}.png"
            cv2.imwrite(frame_path, frame)
            key_frames.append({"frame_idx": 0, "timestamp": 0.0, "path": frame_path})

        prev_gray = gray
        frame_idx += 1

    cap.release()
    print(f"从 {frame_idx} 帧中提取了 {len(key_frames)} 个关键帧")
    return key_frames