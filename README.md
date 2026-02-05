# VPNBot
Advanced Telegram Bot for Managing VPN Services via XUI / 3x-ui Panels

## 🚀 Overview

VPNBot is a fully-featured Telegram bot designed to **automate the sale, management, and renewal of VPN services**
using **XUI / 3x-ui panels**.

Unlike simple scripts, this project provides **real API-level integration** with the panel and is built to run
in **production environments**.

It supports both **V2Ray (VLESS)** and **WireGuard** services with a complete user flow.

---

## ✨ Features

### 🔹 User Features
- Buy VPN subscriptions
- Renew existing subscriptions
- Get free test accounts
- QR code + config delivery
- Crypto payments (TRX / TON)
- Wallet balance & referral rewards
- Step-by-step connection guides

### 🔹 Admin Features
- Manual payment approval
- Bulk account creation
- Maintenance mode
- Test & simulation tools
- Referral reward automation

---

## 🧠 Architecture Overview

```text
Telegram User
      │
      ▼
Aiogram Bot (FSM-based)
      │
      ├── SQLite Database
      ├── Payment & Wallet System
      ├── Referral Engine
      │
      └── TxuiManager
              │
              ▼
        XUI / 3x-ui Panel API
