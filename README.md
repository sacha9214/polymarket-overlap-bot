# Polymarket Overlap — Discord Bot

The [website](https://sacha9214.github.io/polymarket-overlap/) can't do anything until you open it.
This bot **tells you**: it watches the positions of the best Polymarket traders and posts
in a channel when the smart money **enters** a market or **exits** one.

The model (estimated probability, real track record, disagreements, conviction) is the same as the
website's, ported one-to-one in `overlap.py`.

## Commands

| Command | Effect |
|---|---|
| `/setup` | Creates the full channel structure and wires everything up |
| `/best [count]` | The best entries right now (1 to 5) |
| `/wallet <address>` | Bets, real track record and unclaimed winnings of an address |
| `/watch-buys [threshold]` | Sends BUY alerts to this channel: tracked traders opening a position (default: above $10,000) |
| `/watch-exits [threshold]` | Sends EXIT alerts to this channel: positions they closed |
| `/unwatch` | Unsubscribes this channel |
| `/board` | Installs a live board of the best overlaps, rewritten in place every cycle |
| `/guide` | Posts the how-to-read guide for this channel (pin it) |
| `/track <profile>` · `/untrack` · `/tracked` | Always follow a trader, even outside the weekly top 50 |
| `/trader-board <profile>` · `/untrack-board` | Follows one trader in this channel with a live board |
| `/dataset` · `/dataset-board` | Does the smart money we track actually win? Live track record |
| `/consensus` | Only alert when the tracked wallets agree |
| `/sectors` | Weights wallets by the sector they actually play |
| `/marketmakers` | How to handle market makers in the analysis (off, flag or exclude) |
| `/status` | Monitoring status |

Commands that change the server's configuration are restricted to members who can manage the server.

## Two ways to use it

| | Webhook only | Full bot |
|---|---|---|
| Setup | ~30 s, no token | create a Discord application |
| Entry/exit alerts | ✅ | ✅ |
| Commands `/best`, `/wallet`… | ❌ | ✅ |
| Start | `./start_webhook.sh` | `./start_mac_linux.sh` |

### Webhook version (the simplest)

A webhook can only **send** messages, which is enough for alerts.

1. Discord → **Server Settings** → **Integrations** → **Webhooks** → *New Webhook*
2. Pick the channel, then **Copy Webhook URL**
3. Paste it into a `webhook.txt` file next to the scripts (it is gitignored), or set `DISCORD_WEBHOOK_URL`
4. `./start_webhook.sh`

Anyone with this URL can post in your channel: keep it private.

## Setting up the full bot

1. **Create the bot** at <https://discord.com/developers/applications> → *New Application* → *Bot* tab → *Reset Token* → copy the token.
2. **Paste the token** into a `token.txt` file next to `bot.py` (a single line).
   Never share it: it gives full control over the bot.
3. **Invite the bot**: *OAuth2 → URL Generator* tab, tick `bot` + `applications.commands`,
   permissions `Send Messages` and `Embed Links`, then open the generated URL.
4. **Start it**:

```bash
./start_mac_linux.sh
```

No *privileged intent* is needed: the bot never reads messages.

## Settings (environment variables, all optional)

| Variable | Default | Role |
|---|---|---|
| `DISCORD_BOT_TOKEN` | — | Alternative to `token.txt` |
| `DISCORD_GUILD_ID` | — | Your server: commands show up immediately instead of after ~1 h |
| `POLL_MINUTES` | `2` | How often positions are checked |
| `PRESET_SIZE` | `50` | Number of traders followed (weekly top) |

## Good to know

- **The first cycle never sends alerts**: it is the baseline snapshot, otherwise you would get
  hundreds of messages at once. Alerts start from the next cycle.
- **At most 5 alerts per cycle and per channel**, to keep things readable.
- **A leaderboard reshuffle is not an exit**: only traders present in two consecutive cycles are
  compared, so a trader dropping out of the top 50 doesn't trigger fake exits.
- **It must run continuously** to monitor anything. For 24/7 operation, host it
  (Railway, Fly.io, a small VPS…).
- The Polymarket API caps history at 500 events per wallet, about 2 days for large
  traders: track records are computed over that window, not over their whole career.
- The engine can be tested without Discord: `./venv/bin/python overlap.py` prints the best entries
  in the terminal.

Not financial advice. Only bet what you can afford to lose.

## License

[MIT](LICENSE)
