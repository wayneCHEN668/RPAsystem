# main.py —— 一键从视频生成脚本
from video_preprocessor import extract_key_frames
from ai_analyzer import analyze_video_frames
from script_generator import generate_playwright_script
import json

def video_to_playwright(video_path: str, output_script: str = "test_flow.spec.ts"):
    print("=== Step 1: 提取关键帧 ===")
    key_frames = extract_key_frames(
        video_path, 
        output_dir="./frames",
        similarity_threshold=0.90  # 越低 = 提取越多帧
    )
    
    print("\n=== Step 2: AI 分析操作意图 ===")
    actions = analyze_video_frames(key_frames)
    
    # 保存中间结果，便于调试和人工校验
    with open("actions.json", "w", encoding="utf-8") as f:
        json.dump(actions, f, ensure_ascii=False, indent=2)
    print(f"操作序列已保存到 actions.json，共 {len(actions)} 个操作")
    
    print("\n=== Step 3: 生成 Playwright 脚本 ===")
    script = generate_playwright_script(actions, output_script)
    
    print(f"\n完成！脚本路径: {output_script}")
    return script

if __name__ == "__main__":
    video_to_playwright("recording.mp4")