# 1812

A Discord chatbot powered by OpenAI.

## Setup

1. **Clone the repo**
   ```bash
   git clone https://github.com/CryptoJones/1812.git
   cd 1812
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env and fill in your DISCORD_TOKEN and OPENAI_API_KEY
   ```

4. **Run**
   ```bash
   python bot.py
   ```

## Usage

- **Mention the bot** in any channel to chat: `@1812 your message`
- **DM the bot** directly
- `!clear` — clears conversation history for the current channel

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | — | Your Discord bot token |
| `OPENAI_API_KEY` | Yes | — | Your OpenAI API key |
| `OPENAI_MODEL` | No | `gpt-4o-mini` | OpenAI model to use |
| `SYSTEM_PROMPT` | No | See `.env.example` | Bot personality/system prompt |

## License

Apache 2.0
