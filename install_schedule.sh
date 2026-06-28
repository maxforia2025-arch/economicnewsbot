#!/bin/bash
# Установка постоянного расписания для economicnewsrussiabot.
# Запусти ОДИН раз: bash install_schedule.sh
# Бот будет запускаться сразу и далее каждые 2 часа, переживая перезагрузку.

set -e
PLIST="$HOME/Library/LaunchAgents/com.maxim.economicnewsbot.plist"
DIR="/Users/maksim/Desktop/YOUTUBE 1/economic_news_bot"

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.maxim.economicnewsbot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>${DIR}/bot.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${DIR}</string>
    <key>StartInterval</key>
    <integer>7200</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${DIR}/run.log</string>
    <key>StandardErrorPath</key>
    <string>${DIR}/run.log</string>
</dict>
</plist>
PLISTEOF

# перезагружаем агент
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "✅ Расписание установлено: бот стартует сейчас и далее каждые 2 часа."
echo "   Лог: ${DIR}/run.log"
echo "   Снять с расписания: launchctl unload \"$PLIST\""
