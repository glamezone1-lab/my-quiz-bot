MY QUIZ BOT - FIXED VERSION

Files:
- bot.py
- requirements.txt

Render Environment Variables:
BOT_TOKEN = your Telegram bot token
ADMIN_USER_ID = your Telegram numeric user ID

Optional:
KEEPALIVE = false

Important Render Free limitation:
A Free Web Service sleeps after 15 minutes without inbound traffic. Telegram polling does NOT wake the Render service because Telegram's request is outbound from your bot. The /health endpoint is included so an external uptime monitor can wake the service.

If you use an external uptime monitor, point it to:
https://YOUR-RENDER-SERVICE.onrender.com/health
and check every 10 minutes.

The code keeps:
- Native Telegram quiz polls
- 7-button main keyboard
- Single-user ADMIN_USER_ID restriction
- Add Quiz import
- Random quiz, max 20 questions
- Wrong questions due after 24 hours
- Correct answers remove revision
- Explanation support in imported questions
- Render HTTP health endpoint
