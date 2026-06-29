# 主流程：录屏 → 脚本
python main.py generate recording.mp4 \
  --output scripts/test_order.spec.ts \
  --suite-name "工单创建流程" \
  --base-url https://your-system.com

# 调试：先看抽帧效果，不花 API 费用
python main.py inspect recording.mp4 --threshold 0.90

# 单独验证已有脚本
python main.py validate scripts/test_order.spec.ts
