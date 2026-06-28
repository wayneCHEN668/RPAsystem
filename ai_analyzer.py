# ai_analyzer.py
import anthropic
import base64
import json
from pathlib import Path

client = anthropic.Anthropic()

ANALYSIS_PROMPT = """
你是一个专业的UI操作分析专家。请仔细分析这张截图，识别用户正在执行的操作。

请以JSON格式返回，只返回JSON，不要有其他文字：
{
  "action_type": "操作类型，如: navigate/click/fill/select/scroll/wait/hover",
  "element_description": "被操作元素的描述，如: '提交按钮'、'用户名输入框'",
  "element_hints": {
    "text": "元素可见文字",
    "placeholder": "输入框placeholder（如有）",
    "location": "元素大致位置，如: top-right/center/bottom"
  },
  "input_value": "如果是输入操作，填写的内容（如有）",
  "url": "当前页面URL（如可见）",
  "page_title": "页面标题或面包屑（如可见）",
  "notes": "其他值得注意的信息"
}
"""

def analyze_frame(frame_path: str, timestamp: float) -> dict:
    """用 Claude Vision 分析单帧的操作意图"""
    with open(frame_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")
    
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=800,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_data,
                        },
                    },
                    {"type": "text", "text": ANALYSIS_PROMPT}
                ],
            }
        ],
    )
    
    try:
        result = json.loads(message.content[0].text)
        result["timestamp"] = timestamp
        result["frame_path"] = frame_path
        return result
    except json.JSONDecodeError:
        return {"error": "解析失败", "raw": message.content[0].text, "timestamp": timestamp}


def analyze_video_frames(key_frames: list[dict]) -> list[dict]:
    """批量分析所有关键帧"""
    actions = []
    for i, frame in enumerate(key_frames):
        print(f"分析第 {i+1}/{len(key_frames)} 帧 (t={frame['timestamp']}s)...")
        action = analyze_frame(frame["path"], frame["timestamp"])
        actions.append(action)
    return actions