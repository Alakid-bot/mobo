# 1812

**A Discord chatbot powered by OpenAI. Loud when it needs to be.**

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg?logo=apache)](https://opensource.org/licenses/Apache-2.0)
[![GitHub](https://img.shields.io/badge/GitHub-CryptoJones%2F1812-181717?logo=github&logoColor=white)](https://github.com/CryptoJones/1812)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Discord](https://img.shields.io/badge/discord.py-2.3%2B-5865F2?logo=discord&logoColor=white)](https://discordpy.readthedocs.io/)
[![Version](https://img.shields.io/badge/version-v0.1.0--dev-orange)]()

> *"Cannons firing. Bells ringing. The overture begins."*
> — Tchaikovsky, 1880

---

## Overview

1812 is a self-hosted, open-source Discord chatbot backed by the OpenAI API. It maintains
per-channel conversation history across restarts, responds when mentioned or DMed, and stays
out of the way when it's not needed.

Named after Tchaikovsky's 1812 Overture — a piece that knows exactly when to be quiet
and exactly when to be deafening.

---

## Features

- **Persistent memory** — conversation history survives bot restarts via `history.json`
- **Per-channel context** — each channel maintains its own independent conversation thread
- **Mention or DM** — responds when tagged by user mention, role mention, or direct message
- **Configurable personality** — set any system prompt via `.env`
- **Model-agnostic** — swap between `gpt-4o`, `gpt-4o-mini`, or any OpenAI-compatible model
- **`!clear` command** — wipe conversation history for the current channel
- **Token-safe** — caps history at 20 messages per channel to prevent runaway token usage

---

## Architecture

| Component | Technology |
|---|---|
| Bot framework | [discord.py](https://discordpy.readthedocs.io/) 2.3+ |
| LLM backend | [OpenAI API](https://platform.openai.com/) |
| History storage | Local `history.json` (JSON, per-channel) |
| Config | `.env` via `python-dotenv` |
| Runtime | Python 3.10+ |

---

## Setup

**1. Clone the repo**
```bash
git clone https://github.com/CryptoJones/1812.git
cd 1812
```

**2. Run setup**
```bash
chmod +x setup.sh
./setup.sh
```

**3. Configure your environment**
```bash
# Edit .env with your tokens
nano .env
```

**4. Launch**
```bash
source .venv/bin/activate
python bot.py
```

---

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | — | Your Discord bot token |
| `OPENAI_API_KEY` | Yes | — | Your OpenAI API key |
| `OPENAI_MODEL` | No | `gpt-4o-mini` | OpenAI model to use |
| `SYSTEM_PROMPT` | No | `You are 1812...` | Bot personality / system prompt |

---

## Discord Setup

1. Go to [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. Create a new application named `1812`
3. Navigate to **Bot** → enable **Message Content Intent** and **Server Members Intent**
4. Copy your bot token into `.env`
5. Invite the bot to your server via **OAuth2 → URL Generator** with `bot` scope and `Send Messages`, `Read Message History` permissions

---

## Usage

| Action | How |
|---|---|
| Chat with 1812 | `@1812 your message` in any channel |
| DM 1812 | Send a direct message |
| Clear history | `!clear` in a channel |

---

## Project Structure

```
1812/
├── LICENSE
├── README.md
├── .env.example
├── .gitignore
├── requirements.txt
├── setup.sh
├── publish.sh
└── bot.py
```

---

## Contributing

Pull requests welcome. Keep it simple. Keep it clean.

---

## License

**Apache License 2.0** — Copyright 2026 Aaron K. Clark

See [LICENSE](LICENSE) for full terms.

---

---

## Acknowledgments

To **Dr. John Crichton** of the Farscape Project — astronaut, wormhole theorist, and the most
unlikely hero to ever fall through the wrong end of the universe. Wherever he is.

---

Proudly Made in Nebraska. 🌽
