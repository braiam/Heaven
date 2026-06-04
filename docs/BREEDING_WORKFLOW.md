# Breeding Workflow — Heaven

## Overview

Heaven's breeding optimizer finds the best parent pairs using **expected proc scoring** — the probability each desired spark will actually proc during inheritance. This makes affinity, spark stars, grandparent sparks, and individual entity affinities all feed into ONE number: the real chance your offspring gets what you want.

**Scoring model**: `obj = sum(P(>=1 proc) for each wanted spark) + sum(P(>=1 proc) for blue stats) * 0.5`

No artificial weights needed — affinity is already inside each proc chance via the Cygames patent formula.

---

## Prerequisites

1. **Capture your data**: Run the game once with capture active. Heaven reads:
   - `load/index` — your trained umas (mine)
   - `pre_single_mode/index` — friend/rental parents
   - This populates `data/heir_capture_*.jsonl`

2. **master.mdb** must be present (auto-detected from Umamusume install path)

3. **TTAnalyzer** (optional) — if running at `:7434`, enables act% weighting for skills

---

## Step-by-Step Workflow

### Phase 1: Define Your Goal

Before touching the optimizer, answer these questions:

| Question | Why it matters |
|----------|---------------|
| **What character are you training?** | Determines base affinity with every potential parent |
| **What running style + distance?** | Filters relevant skills (Front Runner Long vs End Closer Sprint) |
| **What sparks do you NEED?** | Blue stats? Specific aptitudes? Skill sparks? |
| **Do you care more about affinity or specific sparks?** | Sets the w_affinity / w_spark ratio |

**Common goals:**
- **Max rank score** → Prioritize blue stats (Speed/Power 3★) + high affinity
- **Specific aptitude** → Set wanted pink sparks (e.g., "Long Distance 3★")
- **Skill inheritance** → Use style filter + act% to find parents with high-activation skills
- **Balanced** → Default weights (2.0 affinity / 1.0 spark)

### Phase 2: Configure the Optimizer

1. **Select Target** — Click the target button, pick your trainee character
2. **Set Wanted Sparks** — Type spark names in the input (autocomplete from master.mdb)
   - Blue: "Speed", "Stamina", "Power", "Guts", "Wit"
   - Pink: "Long Distance", "Front Runner", "Turf", etc.
   - White: Skill names like "Tail Held High", etc.
3. **Optional: Style/Distance Filter**
   - Select running style + distance → Apply
   - If TTAnalyzer is running, auto-adds high-activation skills to wanted list
   - Skills weighted by real activation % from race data
4. **Adjust Weights**
   - `w_affinity` (default 2.0) — how much affinity matters
   - `w_spark` (default 1.0) — how much spark matching matters
   - For affinity-focused: 3.0 / 1.0
   - For spark-focused: 1.0 / 2.0

### Phase 3: Run the Optimizer

Click **Compute breeding**. The optimizer:

1. Builds candidate pool (your umas + rentals)
2. Excludes invalid pairs (same character, target as parent)
3. For each pair:
   - Computes exact affinity: `CR(t,p1) + CR(t,p2) + RL2(p1,p2) + WSB(p1) + WSB(p2)`
   - Scores spark value: `score_for_want(p1) + score_for_want(p2)` with GP weights
   - Objective = `w_aff × affinity + w_spark × spark_value`
4. Returns top 15 OWN pairs + top 15 RENTAL pairs

### Phase 4: Evaluate Results

For each recommended pair, check:

| Metric | What to look for |
|--------|-----------------|
| **Affinity total** | Higher = more proc chances. ◎ (double circle) = 151+, ○ = 51-150, △ = <51 |
| **Spark value** | Higher = more matching sparks. Check which sparks actually match your wants |
| **Blue inheritance** | Per-stat strength. Own 3★ = 3.0, GP 3★ = 1.2 (0.4× weight) |

### Phase 5: Detailed Pair Analysis

Click a result combo card to open the **pair detail modal**:

1. **Spark Proc Odds** — Per-spark probability using the Hakuraku individual affinity model:
   - Each of the 6 tree entities (P1, P1-GP1, P1-GP2, P2, P2-GP1, P2-GP2) has its own individual affinity
   - Parent indiv affinity = CR(t,p) + WSB(p) + RL2(p1,p2)
   - GP indiv affinity = RL3(t, parent, gp) + |parent ∩ gp saddles|
   - Per-event rate = `base% × (1 + individual_affinity / 100)`, clamped to 100%
   - Over 2 inheritance events: `P(≥1) = 1 - (1-p)²`

2. **Blue Stats** — Now included with 70/80/90% base proc:
   - Speed 3★ with 75 individual affinity = `90% × 1.75 = 100%` (guaranteed)
   - Speed 1★ with 20 individual affinity = `70% × 1.20 = 84%`

3. **Affinity Breakdown** — CR per parent, cross-parent bonus, WSB

### Phase 6: Decision & Execution

Compare your top 3-5 pairs on:

1. **Proc coverage** — Does the pair cover all your wanted sparks with reasonable odds?
2. **Downside risk** — What's the worst case? (all sparks miss)
3. **Availability** — Is the rental parent actually available in your friend list?

Then go in-game and set up the inheritance.

---

## Formula Reference

### Affinity (CalcRelationPoint)
```
Total = CR(t,p1) + CR(t,p2) + RL2(p1,p2) + WSB(p1) + WSB(p2)

CR(t,p)  = RL2(t,p) + RL3(t,p,gp1) + RL3(t,p,gp2)
RL2(a,b) = sum(relation_point for shared relation_types between a and b)
RL3(a,b,c) = RL2(a,b) filtered to types that also include c
WSB(p)   = |parent_saddles ∩ GP1_saddles| + |parent_saddles ∩ GP2_saddles|
```

### Spark Base Proc Rates
| Category | 1★ | 2★ | 3★ |
|----------|:---:|:---:|:---:|
| Blue (Stats) | 70% | 80% | 90% |
| Pink (Aptitude) | 1% | 3% | 5% |
| Green (Unique) | 5% | 10% | 15% |
| Race | 1% | 2% | 3% |
| Skill | 3% | 6% | 9% |
| Scenario | 3% | 6% | 9% |

### Individual Affinity (Hakuraku model)
```
Parent:      CR(t, parent) + WSB(parent) + RL2(parent, otherParent)
Grandparent: RL3(t, parent, gp) + |parent_saddles ∩ gp_saddles|

Per-event proc = min(100%, base_rate × (1 + individual_affinity / 100))
Over 2 events:  P(≥1) = 1 - (1 - combined_rate)²
Combined rate:  1 - product(1 - rate_i) for all sources with this spark
```

### White Spark Generation (Hakuraku piecewise, 107K+ observations)
| Tier | Base | Lineage 1-2 boost | Lineage 3+ boost |
|------|:----:|:-----------------:|:-----------------:|
| White (plain) | 20% | +2.0% each | +2.75% each |
| Double Circle | 25% | +2.5% each | +3.4375% each |
| Gold | 40% | +4.0% each | +5.5% each |

Note: First 1-2 lineage copies give LESS bonus than previously thought.
Community formula (`+2.5%/+5%` linear) overestimates early copies.

### Star Count Bands
| Rank Score | 1★ | 2★ | 3★ |
|:----------:|:---:|:---:|:---:|
| < 6500 | 90% | 10% | 0% |
| 6500-17499 | 50% | 45% | 5% |
| ≥ 17500 | 20% | 70% | 10% |

---

## Sources

- **Affinity formula**: Confirmed via Frida hooks (261/261 match)
- **Spark base rates**: BourBon_Polaris testing + Cygames patent JP2022018121A
- **Individual inheritance theory**: Crazyfellow's Parenting & Gene Guide + BourBon_Polaris 100-trial validation
- **White spark generation**: Hakuraku.moe analysis (107K+ observations, chi² p≥0.79)
- **Star count bands**: Hakuraku.moe (22M Team Trial umas from Tunnelblick/uma.moe)
- **Unique factor stars**: Independent of unique skill level (Hakuraku 22M sample confirmation)
