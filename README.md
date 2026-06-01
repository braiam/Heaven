# Heaven

Unified offline dashboard for Uma Musume: breeding optimizer + Team Trials analysis.

> **Early version** — Heaven merges two previously separate tools ([Heir](https://github.com/Nighty3333/heir) for breeding and TTAnalyzer for Team Trials) into a single app. Some rough edges are expected. Report bugs or suggestions in the Discord thread.

---

## Requirements

- **Python 3.10+** (tested on 3.12 and 3.14)
- **Windows** (proxy capture uses the Windows registry)
- **DMM version** of Uma Musume

## Installation

```bash
git clone https://github.com/Nighty3333/Heaven.git
cd Heaven
pip install -r requirements.txt
```

## Running

```bash
.\start.bat
```

Opens automatically at **http://127.0.0.1:1620**

Character portraits download from gametora on first load and are cached locally.

---

## Features

### Inventory

Browse your full character factor inventory. Each card shows its factor stars, inherited skills, and scenario bonuses. Use the search bar to filter by name. Click any card to see the full factor breakdown.

### Affinity Calculator

Pick two parents and instantly see their affinity score with a detailed breakdown of every bonus: shared races, fans, scenario links, and relation bonuses. The formula matches the game's exact calculation.

### Breed Optimizer

Select a target build (running style + distance), pick your "want" skills, and the optimizer finds the best parent combinations from your inventory. It scores every possible pair by affinity + skill overlap and ranks them. Use "Prepare For" to auto-select skills that match a style (e.g. Front Runner skills for Front Runner builds).

### Team Trials Overview

After capturing match data (see setup below), this tab shows every Team Trials match you've played: your team vs the opponent, per-race results, skill activations, and win/loss verdicts. Click any match to expand the full race-by-race breakdown with position graphs and skill timelines.

### Skill Planner

Plan your skill build for a specific distance + running style combo. Shows activation rates from your captured TT data — how often each skill actually fires in real matches. Helps you pick skills that consistently activate instead of ones that look good on paper but never trigger.

### Skill Lookup

Search any skill by name or effect. Shows the skill's description, activation conditions, and which characters learn it. Useful for quickly checking what a skill does during breeding or team building.

### Track & Condition (Stadium)

Track stadium conditions, course modifiers, and weather across your captured matches. Also houses the **capture controls** — start/stop the proxy from here without touching a terminal.

---

## Team Trials Capture Setup

The Team Trials features need live data captured from the game. This is a **one-time setup** — once configured, you just click Start/Stop in the dashboard.

### Step 1: Generate the mitmproxy certificate

```bash
pip install mitmproxy
mitmdump
```

Run `mitmdump` once and **close it immediately** with Ctrl+C. This generates the certificate files. You only need to do this once.

### Step 2: Install the certificate

The certificate file is at:
```
C:\Users\<YOUR_USERNAME>\.mitmproxy\mitmproxy-ca-cert.cer
```

Install it:

1. Double-click the `.cer` file
2. Click **Install Certificate...**
3. Select **Local Machine** (click Yes on the admin prompt)
4. Select **Place all certificates in the following store**
5. Click **Browse** and pick **Trusted Root Certification Authorities**
6. Click OK > Next > Finish

To verify: open `certmgr.msc` (Win+R > type it), go to Trusted Root Certification Authorities > Certificates, and look for **mitmproxy** in the list.

### Step 3: Capture your matches

1. Open Heaven at http://127.0.0.1:1620
2. Go to the **Track & Condition** tab
3. Click **Start Capture** — this starts the proxy and sets your Windows proxy automatically
4. Play Team Trials matches normally in DMM
5. Click **Stop Capture** when done — the proxy stops and your Windows proxy is restored
6. Switch to the **Team Trials** tab to see your results

The proxy auto-detects your `udid` from game request headers on first capture. If auto-detection fails, find your udid manually (it's a 32-character hex string in the request headers) and save it to `data/udid.txt`.

### Step 4: If something goes wrong

| Problem | Solution |
|---------|----------|
| SSL errors or `SEC_ERROR` in the game | Certificate not installed correctly. Redo Step 2 — make sure you select **Local Machine** and the **Trusted Root** store, not Current User |
| Capture is running but no data appears | Game traffic isn't going through the proxy. Check Windows Settings > Proxy and make sure it's set to `127.0.0.1:8080` |
| Game can't connect to servers | Stop capture first, verify your internet works without the proxy, then try again |
| `udid` not auto-detected | Save your udid manually to `data/udid.txt` (one line, 32 hex characters) |
| Internet proxy stuck after a crash | Go to Windows Settings > Network & Internet > Proxy > Manual proxy setup > turn it **OFF**. Heaven restores it automatically on normal exit, but a hard crash can leave it on |

---

## Updating

```bash
cd Heaven
git pull
pip install -r requirements.txt
.\start.bat
```

Your data files (`data/` folder, images, notes) are local-only and won't be affected by updates.
