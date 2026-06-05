# Heaven

Offline dashboard for Uma Musume: breeding optimizer + Team Trials analysis.

> **Nothing is injected into the game.** Heaven is a purely passive tool that reads data your game client already sends and receives. It never sends packets to the game server, never modifies game memory, and never injects code.

| Feature | Method |
|---------|--------|
| **Inventory / Breed** | Reads your account data via the game's own API (same request your client makes on login) |
| **Team Trials** | Captures network traffic through a local HTTPS proxy (mitmproxy) — reads packets, never modifies them |

---

## Requirements

- **Python 3.10+** (tested on 3.12 and 3.14)
- **Windows** (proxy capture uses the Windows registry)
- **DMM version** of Uma Musume

## Quick Start

```bash
git clone https://github.com/Nighty3333/Heaven.git
cd Heaven
python -m pip install -r requirements.txt
.\start.bat
```

Opens at **http://127.0.0.1:1620**

> Use `python -m pip` (not bare `pip`) so every dependency installs into the **same** Python that runs the app. On a PC with more than one Python, a bare `pip` can install into a different environment, which later breaks Team Trials capture.

---

## Features

### Inventory

Browse your full character factor inventory. Cards show the complete factor breakdown (blue stats, pink aptitudes, green uniques, white skills/races) with star counts, proc percentages, and your own contribution highlighted.

- **Filter** by name, note, tag, or owner
- **Source toggle** (All / Yours / Friends) to focus on your umas or borrowed ones — friend cards are visually marked with a green border
- **Sort** by Affinity, G1 Wins, White Count, Score, or Newest
- **Target picker** — select any uma as breeding target to see exact individual affinity scores on every card
- **Inheritance Factor filters** (uma.moe style) — filter by blue/pink/green/white factors with star ranges, collapsible panel
- **White skill truncation** — cards show the top 8 white skills by default with a "+N more" toggle to expand, keeping the list scannable
- **Progressive loading** — loads 25 cards at a time with a "Show more" button for smooth scrolling
- **Quick actions** — assign any uma as Parent 1 or Parent 2 directly from the card, view its race history, or filter to all copies

### Breed Optimizer

Select a target build (running style + distance), pick your "want" skills, and the optimizer finds the best parent combinations from your inventory.

Uses the **expected proc model**: instead of arbitrary weights, it computes the real probability of each spark proccing across all 6 tree entities (parents + 4 grandparents), each with their own individual affinity calculated from the exact in-game formula (character relation + winning saddle bonus + relation level).

**Pair detail view** shows:
- **Lineage tree** — visual family tree with portraits, individual affinity badges, and top sparks per entity
- **Spark proc odds** — P(>=1 proc) for every spark over a full run (2 inheritance events), with expandable per-source breakdown: click any spark to see which tree entity contributes it, at what stars, with what affinity, and the formula `base% x (1 + affinity/100)`
- **Inherited factors** — combined factor list from both parents

**Breeding tray** — a persistent bottom bar that lets you assign parents while browsing the inventory. Pick Parent 1 and Parent 2 from any card, select a target from the visual character picker, and hit "Show pair" without switching tabs.

Use "Prepare For" (style + distance) to auto-select skills. If TTAnalyzer data is available, skills are weighted by real activation rates.

Character portraits are bundled in the repo. Missing ones download from gametora on first access and are cached locally.

### Team Trials Overview

After capturing match data, this tab shows every Team Trials match you've played: your team vs the opponent, per-race results, skill activations, and win/loss verdicts.

**Race Analysis** uses compact summary rows instead of a wide table:
- Each uma shows AVG score, CV% (consistency), BEST, WORST, ACT% (skill activation), a sparkline trend chart, and a verdict pill (GOAT / STRONG / SOLID / WEAK / BENCH)
- Click any row to expand: score history heatmap (colored cells), trimmed average, gap to top, standard deviation, skill activation %, and a full skill breakdown button
- Sortable by any column

### Skill Planner

Plan your skill build for a specific distance + running style combo. Shows activation rates from your captured TT data — how often each skill actually fires in real matches. Helps you pick skills that consistently activate instead of ones that look good on paper but never trigger.

### Skill Lookup

Search any skill by name or effect. Shows the skill's description, activation conditions, and which characters learn it.

### Track & Condition

Track stadium conditions, course modifiers, and weather across your captured matches. Shows top tracks, starting gate distribution, ground/surface/weather/season breakdowns, and a full rounds table.

Also houses the **capture controls** — start/stop the proxy from here without touching a terminal.

---

## Installation

### Step 1: Clone and install

```bash
git clone https://github.com/Nighty3333/Heaven.git
cd Heaven
python -m pip install -r requirements.txt
```

This installs **everything** in one go — the web server, the data decoder (`msgpack` + `pycryptodome`), and `mitmproxy` — all into the same Python environment. Using `python -m pip` (not bare `pip`) guarantees they land in the Python that `start.bat` actually runs.

> ⛔ **Do NOT install mitmproxy from mitmproxy.org (the Windows installer / standalone `.exe`).** That build ships its **own frozen, embedded Python** that you cannot `pip install` into — so the capture addon will fail with `ModuleNotFoundError: No module named 'msgpack'` and `mitmdump exited immediately`. mitmproxy **must** come from pip (it's already in `requirements.txt`), so it shares the same environment as `msgpack`/`pycryptodome`. If you previously installed the standalone, uninstall it (Windows Settings → Apps → mitmproxy) before continuing.

### Step 2: Import your umas

Run Heaven:

```bash
.\start.bat
```

Open **http://127.0.0.1:1620** in your browser. A setup dialog appears with two options:

#### Option A: Memory dump (recommended)

The simplest method. No Steam credentials needed.

1. Open Umamusume and go to **Enhance > List** (the Veteran List screen)
2. Run [UmaExtractor](https://github.com/xancia/UmaExtractor) — it produces a `data.json` file
3. In Heaven's setup dialog, click **Import data.json** and pick the file

> **Note:** UmaExtractor only reads your own umas. Friends' borrowable parents won't be available with this method.

#### Option B: Frida + Steam (advanced)

Captures credentials via Frida and uses the game's API directly. Includes friends' borrowable parents.

1. Click **Open game & capture** — Heaven launches the game and captures the auth key via Frida
2. Enter your Steam username and password when prompted
3. If you have Steam Guard enabled, enter the 2FA code
4. Heaven fetches your full inventory + friends' borrowable parents

Steam credentials are stored locally, encrypted via Windows DPAPI (bound to your Windows user — same scheme Chrome uses for saved passwords). They never leave your machine.

### Step 3: Team Trials setup (optional)

The Team Trials features need live match data captured through mitmproxy. This is a **one-time setup**.

#### 3a. Generate the mitmproxy certificate

`mitmproxy` was already installed in Step 1 via pip — **do not install it separately**, and especially **do not download the standalone installer from mitmproxy.org** (its embedded Python can't see `msgpack`/`pycryptodome`, which breaks capture — see the warning in Step 1). Just run `mitmdump` once to generate the CA certificate, then close it immediately with `Ctrl+C`:

```bash
mitmdump
```

You only need to do this once.

#### 3b. Install the certificate

The certificate file is at:

```
C:\Users\<YOUR_USERNAME>\.mitmproxy\mitmproxy-ca-cert.cer
```

**PowerShell (as Admin) — one-liner:**

```powershell
Import-Certificate -FilePath "$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.cer" -CertStoreLocation Cert:\LocalMachine\Root
```

> ⚠️ **Mind the backslash** between `USERPROFILE` and `.mitmproxy`. It's easy to drop when copying. The path must be `$env:USERPROFILE\.mitmproxy\…` — **not** `$env:USERPROFILE.mitmproxy\…`. If you get *"The certificate file could not be found"*, that missing `\` is almost always the cause.

**Or via GUI:**

1. Double-click the `.cer` file
2. Click **Install Certificate...**
3. Select **Local Machine** (click Yes on the admin prompt)
4. Select **Place all certificates in the following store**
5. Click **Browse** and pick **Trusted Root Certification Authorities**
6. Click OK > Next > Finish

**Verify** the installation:

```powershell
Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -like "*mitmproxy*" }
```

If it returns a row with `O=mitmproxy, CN=mitmproxy`, you're good.

#### 3c. Capture your matches

1. Open Heaven at **http://127.0.0.1:1620**
2. Go to the **Track & Condition** tab
3. Click **Start Capture** — this starts the proxy and sets your Windows proxy automatically
4. Play Team Trials matches normally in DMM
5. Click **Stop Capture** when done — the proxy stops and your Windows proxy is restored
6. Switch to the **Team Trials** tab to see your results

The proxy auto-detects your `udid` from game request headers on first capture. If auto-detection fails, save your udid manually to `data/udid.txt` (one line, 32 hex characters).

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Import-Certificate : The certificate file could not be found` | A backslash got dropped from the path. Use exactly `"$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.cer"` — note the `\` before `.mitmproxy`. Or just double-click the `.cer` file and use the GUI steps instead |
| SSL errors or `SEC_ERROR` in the game | Certificate not installed correctly. Redo step 3b — make sure you select **Local Machine** and the **Trusted Root** store, not Current User |
| `mitmdump exited immediately` / `Error logged during startup, exiting…` / `ModuleNotFoundError: No module named 'msgpack'` | The capture addon can't import `msgpack`/`pycryptodome` because `mitmdump` is running under a different Python than the one those packages were installed into. **#1 cause: you installed the standalone mitmproxy** (Windows installer from mitmproxy.org), which has its own frozen embedded Python. Fix: uninstall it (Windows Settings → Apps → mitmproxy), then `python -m pip install -r requirements.txt` so the pip build is used. Verify with `(Get-Command mitmdump).Source` — it should point at your Python's `Scripts\mitmdump.exe`, **not** `Program Files\mitmproxy`. To see the exact error, run `mitmdump -s discover_addon.py --listen-port 8080 --set block_global=false` (the `-s discover_addon.py` is essential — without it mitmdump starts fine and hides the real problem) |
| Capture running but no data appears | Game traffic isn't going through the proxy. Check Windows Settings > Proxy — it should be set to `127.0.0.1:8080` |
| 0 trials added after processing | Data might already be processed. Run `python tt_analyze.py` manually to see full output |
| Game can't connect to servers | Stop capture first, verify your internet works without the proxy, then try again |
| `udid` not auto-detected | Save your udid manually to `data/udid.txt` (one line, 32 hex characters) |
| Internet stuck after using Heaven | The system proxy got stuck. Fix: **Windows Settings > Network & Internet > Proxy > Manual proxy setup > turn it OFF**. Or run: `Set-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" ProxyEnable 0`. Heaven restores it automatically on normal exit, but a crash can leave it on |
| DNS errors (`getaddrinfo failed`) | Change DNS to Google: Windows Settings > Network > your adapter > DNS > set `8.8.8.8` and `8.8.4.4` |

**Quick proxy test:** not sure if capture is working? Run mitmdump manually:

```bash
mitmdump -s discover_addon.py --listen-port 8080 --set block_global=false
```

Then open the game. You should see lines like `team_stadium/start`, `team_stadium/all_race_end` scrolling in the terminal. If you see those, the proxy works — close it and use the dashboard's Start Capture button instead.

---

## Updating

```bash
cd Heaven
git pull
python -m pip install -r requirements.txt
.\start.bat
```

Your data files (`data/` folder, images, notes) are local-only and won't be affected by updates.
