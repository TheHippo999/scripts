# Scripts
## net_monitor.py
Logs internet uptime and speed tests to build evidence for ISP complaints.

Run: `python3 net_monitor.py`
Needs: `pip install speedtest-cli`
Output: writes results to a CSV in the same folder.

## DUPR Server

A single-file Python web app for looking up DUPR pickleball ratings,
match history and tournament rosters. It runs a small local web server,
and you use it through your browser.

### Features

- **Player lookup** by DUPR ID: profile, doubles match history with
  win/loss, rating change and post-match rating for each match.
- **Rating chart** showing a player's rating over time.
- **Tournament rosters** from pickleballtournaments.com: paste a
  tournament link, browse its events, and see each team's players with
  their real DUPR ratings looked up automatically.
- **Algorithm change analysis** (`/algo` page): paste a set of tournament
  links and it charts how ratings drifted after DUPR's 8 July 2025
  algorithm change, overall and by age group.

### Requirements

- Python 3 (standard library only, no `pip install` needed)
- A DUPR account

### Setup

Create a file called `DuprSettings.py` in the same folder as the script:

```python
EMAIL = "you@example.com"
PASSWORD = "your-dupr-password"

# --- EDIT THIS: the people you want quick links for ---
PLAYERS = {
    "Name": "Numeric DUPR ID",
    "Name 2": "Numeric DUPR ID",
}
```

This file holds your login, so keep it out of the repo.

**Never commit this file.** Add `DuprSettings.py` to `.gitignore`.

### Running

```
python3 dupr_server.py
```

On first login, DUPR emails a 6-digit code. Type it into the Terminal
when prompted. The login then lasts about 30 days.

Open the address shown in the Terminal in your browser.

### Notes

- DUPR moved its API from `api.dupr.gg` to `api.dupr.com` in
  September 2026. Older builds of this script won't work.
- Only doubles matches are shown. Singles data is available but filtered out.
- The version number prints to the console on startup.
