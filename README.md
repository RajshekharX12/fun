# Telegram VPN Helper Bot

A Telegram bot that generates **WireGuard** and **OpenVPN** client config templates
for multiple countries (Netherlands, Germany, US, Singapore).

The bot **does not run a VPN server**. You must already have your own servers.

## Features

- WireGuard + OpenVPN support
- Country selector:
  - 🇳🇱 Netherlands
  - 🇩🇪 Germany
  - 🇺🇸 United States
  - 🇸🇬 Singapore
- Platform selector:
  - Android
  - iOS (iPhone)
  - Desktop (for OpenVPN)
- Inline-only UI:
  - Get VPN Config
  - Change protocol
  - Change country
  - Android / iOS setup help
  - FAQ (legal, privacy, speed, troubleshooting)
  - Account info + "Delete my data"
  - IP test & DNS leak test links
- Simple JSON storage (`vpn_users.json`)

## Install

```bash
git clone https://github.com/YOUR_NAME/vpn-bot.git
cd vpn-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
