#!/bin/zsh

set -u

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR" || exit 1
mkdir -p .sync/logs

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display alert "无法同步" message "没有找到 Python 3。" as critical'
  exit 1
fi

CLIPBOARD_URL="$(pbpaste 2>/dev/null || true)"
if [[ "$CLIPBOARD_URL" != https://mp.weixin.qq.com/* ]]; then
  CLIPBOARD_URL=""
fi

ARTICLE_URL="$(osascript - "$CLIPBOARD_URL" <<'APPLESCRIPT'
on run argv
  set defaultURL to item 1 of argv
  set resultDialog to display dialog "粘贴“百味鸡 OB Pluto”新文章的微信链接：" default answer defaultURL buttons {"取消", "开始同步"} default button "开始同步" with title "公众号一键同步"
  return text returned of resultDialog
end run
APPLESCRIPT
)"

if [[ -z "${ARTICLE_URL//[[:space:]]/}" ]]; then
  osascript -e 'display alert "没有填写文章链接" as warning'
  exit 1
fi

LOG_FILE=".sync/logs/sync-$(date '+%Y%m%d-%H%M%S').log"
echo "正在同步，请不要关闭窗口……"
python3 tools/sync_wechat.py "$ARTICLE_URL" --publish 2>&1 | tee "$LOG_FILE"
STATUS=${pipestatus[1]}

if [[ $STATUS -eq 0 ]]; then
  osascript -e 'display notification "文章归档与 GitHub 更新已完成" with title "百味鸡 OB Pluto"'
  echo ""
  echo "完成。按任意键关闭窗口。"
else
  osascript -e 'display alert "同步未完成" message "请查看窗口中的原因；运行日志已保留在仓库 .sync/logs 中。" as critical'
  echo ""
  echo "同步未完成。按任意键关闭窗口。"
fi

read -k 1
exit $STATUS
