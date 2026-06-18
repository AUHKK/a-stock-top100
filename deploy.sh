#!/bin/bash
# 本地一键部署：推送代码到 GitHub 并手动触发工作流
set -e
echo "📦 提交代码..."
git add -A
git commit -m "update: $(date +%Y-%m-%d)" || echo "No changes"
git push origin main
echo "🚀 触发 GitHub Actions 工作流..."
gh workflow run update.yml
echo "✅ 部署已触发，请到 GitHub Actions 查看进度"
