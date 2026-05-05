# Veil Security Bot

A Discord bot for STFC (Star Trek: Fleet Command) player verification and management.

## Features

- **Player Verification**: Verify STFC players via stfc.pro or stfc.wtf
- **Auto Role Assignment**: 
  - Server roles (matched by server ID)
  - Verification role (if configured)
  - OPS 71+ role (only if player level ≥ 71)
- **Periodic Updates**: Automatically checks for OPS level changes every 24 hours
- **Screenshot Logging**: Verification attempts are logged with player screenshots

## Setup

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Environment
Copy `.env.example` to `.env` and fill in your values:
```bash
cp .env.example .env
```

Edit `.env` with:
- `DISCORD_TOKEN`: Your Discord bot token
- `GUILD_ID`: Your Discord server ID
- `OPS71_ROLE_ID`: ID of the OPS 71+ role
- `VERIFY_ROLE_ID`: ID of the verification role (optional)
- `VERIFY_CHANNEL_ID`: Channel where verification command can be used
- `LOG_CHANNEL_ID`: Channel for logging verifications (optional)

### 3. Discord Setup

Create these roles in your Discord server:
- **OPS 71+** role
- **Verification** role (optional)
- **Server roles** with names matching server IDs (e.g., "118", "106", etc.)

### 4. Run the Bot
```bash
python bot.py
```

## Commands

### `/verify_stfc`
Verify a player's STFC account.

**Arguments:**
- `player_link`: stfc.pro/stfc.wtf URL or player ID (e.g., `https://stfc.pro/players/2659122580`)
- `screenshot`: Screenshot of the player's STFC profile

**Results:**
- Sets Discord nickname: `[SERVER] ALLIANCE - USERNAME`
- Assigns server role (matched by server ID)
- Assigns verification role (if configured)
- Assigns OPS 71+ role (ONLY if player level ≥ 71)
- Logs verification to LOG_CHANNEL with screenshot

## How It Works

### Verification Command (`/verify_stfc`)
1. User provides a stfc.pro/stfc.wtf link and screenshot
2. Bot fetches player data from stfc.pro
3. Bot assigns roles based on player data:
   - Server role (e.g., "118") → always assigned
   - Verification role → always assigned (if configured)
   - OPS 71+ role → **only if level ≥ 71**
4. Nickname is set to: `[SERVER] ALLIANCE - USERNAME`
5. Verification is logged with screenshot

### Periodic Updates (every 24 hours)
1. Bot fetches all stored player links
2. For each player:
   - Updates player data from stfc.pro
   - Updates nickname if changed
   - **Promotes to OPS 71+ if they reached level 71**
   - Does NOT remove OPS 71+ if they drop below 71

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes | Discord bot token |
| `GUILD_ID` | Yes | Discord server ID |
| `OPS71_ROLE_ID` | Yes | ID of the OPS 71+ role |
| `VERIFY_ROLE_ID` | No | ID of the verification role |
| `ADMIN_ROLE_ID` | No | ID of admin role (for notifications) |
| `VERIFY_CHANNEL_ID` | Yes | Channel where verification command works |
| `LOG_CHANNEL_ID` | No | Channel for logging verifications |
| `MIN_OPS_LEVEL` | No | Minimum OPS level for role assignment (default: 71) |
| `UPDATE_CHECK_HOURS` | No | Hours between periodic updates (default: 24) |
| `DB_PATH` | No | Path to SQLite database (default: stfc_players.db) |
| `DEBUG` | No | Enable debug logging (0 or 1, default: 0) |

## Database

The bot stores player STFC links in SQLite (`stfc_players.db`) for periodic updates. This database is automatically created on first run.

## Logging

Logs are output to stdout. Enable debug logging by setting `DEBUG=1` in `.env`.

Verification events are logged to the `LOG_CHANNEL` with:
- Player name and OPS level
- Server and alliance information
- Verification screenshot

## Bug Fixes

**v2.0 (Current):**
- ✅ Fixed OPS 71+ role assignment bug (explicit `level >= 71` check)
- ✅ Removed voter eligibility system (no longer needed)
- ✅ Simplified codebase (removed 700+ lines of unused code)
