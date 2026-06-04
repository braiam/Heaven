# Heaven

Unified offline dashboard for Uma Musume: breeding optimizer + Team Trials analysis.

> **Early version** — Heaven merges two previously separate tools ([Heir](https://github.com/Nighty3333/heir) for breeding and TTAnalyzer for Team Trials) into a single app. The breeding side (Inventory, Affinity, Breed) is still being polished after the merge — expect rough edges. The Team Trials side (Overview, Skill Planner, Skill Lookup, Track & Condition) is the most mature and battle-tested part right now. Report bugs or suggestions in the Discord thread.

### How does it work?

**Nothing is injected into the game.** Heaven is a purely passive tool:

| Feature | Method | Injects into game? |
|---------|--------|:------------------:|
| **Inventory / Affinity / Breed** | Reads your account data via the game's own API (same request your client makes on login) | No |
| **Team Trials / Skill Planner / Skill Lookup / Track & Condition** | Captures network traffic through a local HTTPS proxy (mitmproxy) — it just reads packets, never modifies them | No |

Heaven **never sends packets to the game server**, never modifies game memory, and never injects code. It only reads data that your game client is already sending and receiving.

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

Character portraits are bundled in the repo. Missing ones download from gametora on first access and are cached locally.

---

## Features

### Inventory

Browse your full character factor inventory. Each card shows its factor stars, inherited skills, and scenario bonuses. Use the search bar to filter by name. Click any card to see the full factor breakdown.

### Breed Optimizer

Select a target build (running style + distance), pick your "want" skills, and the optimizer finds the best parent combinations from your inventory. Uses the **expected proc model**: instead of arbitrary weights, it computes the real probability of each spark proccing across all 6 tree entities (parents + 4 grandparents), each with their own individual affinity. Blue stat procs (70/80/90% base) are included in scoring too.

**Pair detail view** shows:
- **Lineage tree** — visual family tree with portraits, individual affinity badges, and top sparks per entity
- **Spark proc odds** — P(>=1 proc) for every spark over a full run (2 inheritance events), with per-source breakdown: click any spark to see which tree entity contributes it, with what stars, affinity, and the formula `base% x (1 + affinity/100)`
- **Inherited factors** — combined factor list from both parents

Use "Prepare For" (style + distance) to auto-select skills. If TTAnalyzer data is available, skills are weighted by real activation rates.

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

**Option A: One-liner (PowerShell as Admin)**

```powershell
Import-Certificate -FilePath "$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.cer" -CertStoreLocation Cert:\LocalMachine\Root
```

**Option B: GUI**

1. Double-click the `.cer` file
2. Click **Install Certificate...**
3. Select **Local Machine** (click Yes on the admin prompt)
4. Select **Place all certificates in the following store**
5. Click **Browse** and pick **Trusted Root Certification Authorities**
6. Click OK > Next > Finish

**Verify** (either way): run this in PowerShell:
```powershell
Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -like "mitmproxy" }
```
If it returns a row with `O=mitmproxy, CN=mitmproxy`, you're good.

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
| `mitmdump exited immediately` | Something is already using port 8080, or the certificate isn't installed. Try: `mitmdump --listen-port 8080` in a terminal to see the real error |
| Capture is running but no data appears | Game traffic isn't going through the proxy. Check Windows Settings > Proxy and make sure it's set to `127.0.0.1:8080` |
| "Stop & Process" says 0 trials added | The data might already be processed. Run `python tt_analyze.py` manually from the Heaven folder to see the full output |
| Game can't connect to servers | Stop capture first, verify your internet works without the proxy, then try again |
| `udid` not auto-detected | Save your udid manually to `data/udid.txt` (one line, 32 hex characters) |
| Internet stuck / no connection after using Heaven | The system proxy got stuck. Fix it immediately: **Windows Settings > Network & Internet > Proxy > Manual proxy setup > turn it OFF**. Or run in PowerShell: `Set-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" ProxyEnable 0`. Heaven restores it automatically on normal exit, but a hard crash or force-closing the window can leave it on |
| DNS errors (`getaddrinfo failed`) | Your DNS might not resolve game servers. Change DNS to Google: Windows Settings > Network > your adapter > DNS > set `8.8.8.8` and `8.8.4.4` |

### Quick test: is the proxy working?

If you're not sure whether the proxy is capturing anything, run mitmdump manually:

```bash
cd Heaven
mitmdump -s discover_addon.py --listen-port 8080 --set block_global=false
```

Then open the game. You should see lines like `team_stadium/start`, `team_stadium/all_race_end`, etc. scrolling in the terminal as you play. If you see those, the proxy works — press Ctrl+C and use the dashboard's Start Capture button instead.

---

## Updating

```bash
cd Heaven
git pull
pip install -r requirements.txt
.\start.bat
```

Your data files (`data/` folder, images, notes) are local-only and won't be affected by updates.
