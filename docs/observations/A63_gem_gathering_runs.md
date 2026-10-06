# A63 live gem-gathering runs with the planner on (redacted)

Scenario: gather N gems. `scripts/smoke_m8_olympuff.py --seconds 300` with the AI planner on, on a copy of `python/characters/olympuff_m8.toml` whose directives file holds one goal, `goals = ["gather_gems:20"]` (the directives shorthand, `plan.py:86`). The directive pins `gather_gems` on top of the stack; the planner plans below it. Character chosen at run time; no name or id is committed. Survival judged only (M8's milestone clauses need a full hour).

## Run 1 — no cuts in 300 s: Gather never finds ground it may cut

- **Code:** `main` at `0ea72cb` (after #116 gem yield by region and #117).
- **Verdict:** exit 0, `PASS` on survival after **301 s**. Character started at 10/10 health, 9 lives, 1 gem, on the overworld 213 blocks east of the town cell.
- **Gate summary:** deaths **0**; API errors **0**; fights below the health floor 0; gems earned **no**; armor, shop weapon, potion reserve, heal takes all **no**.

### Gems and gem yield

| | |
|---|---|
| Gems | **1 → 1** |
| Cuts (`Use` on grass or bush) | **0** |
| `Take` | 2, both a berry (not a gem) |
| `gem_yield` records | **none**: `kb.extra["gem_yield"]` was never written, so no region totals and no barren marks |

The map was not short of targets: the knowledge base ended with 31,120 known cells on map 76, grass and bushes among them from the first terrain read (e.g. grass at (585, 371), a bush at (588, 371)).

### Planner

| | |
|---|---|
| Calls | 20 (17 `applied`, 3 `unchanged`) |
| Plans accepted / errors | **20 / 0** |
| Tokens | input 10,606, output 3,880, cache write 98,054 (call 1), cache read 1,863,026 |
| Tokens per minute (budgeted) | **~101k in minute 1** (the one cache write), then **~2.7–3.3k/min** |

Two replies (calls 17 and 19) wrapped the JSON in prose, and call 19 in a stray `<invoke name="x">` tag; both still parsed and applied.

### Decision mix (76 decisions that sent intents; 176 more ticks held a queue)

| Op / state | Decisions |
|---|---|
| gather_gems (pinned): `look for gems: explore →` (Gather's fallback to the safe default) | 64 |
| flee | 8 |
| take berry (Loot) | 2 |
| heal_food | 1 |
| path stale, resend | 1 |
| cut grass, cut bush, take gem | **0** |

Intents: 531 `Step`, 1,391 `Wait`, 2 `Take`, 0 `Use`. Call mix: 252 `tick`, 212 `zone`, 40 `strategist`, 33 `terrain`, 21 `self`, 11 `position`, 4 `entities`.

### Outcomes

- **Deaths:** 0 (lives 9 → 9). Health 10 → 7: npc 213 hit for 1 and 2 at (424, 321) around 110 s, npc 220 for 1 at (341, 352) at 180 s. Flee got clear both times.
- **Stalls > 10 s at one cell:** none. The character was moving all run: (610, 396) → (424, 321) → (344, 362) → (265, 428), about 345 blocks west.
- **Left barren or low-yield areas on its own:** **not exercised.** With no cut filed there was no yield to act on. The long walk west is Gather's explore fallback, not a yield decision.
- **All 212 zone reads came back `safe=False`**, and the walk never came within 12 blocks of the town cell (397, 401).

### Top 3 defects

1. **Gather cuts only on known safe-zone cells or within 8 of town, so in the field it never cuts.** `is_safe_ish` requires the cell to touch a known safe zone or lie in the respawn probe ring; every zone read was unsafe and the character never reached town, so `cuttable` was false for every grass and bush in view. Gather then hands each decision to the safe default.

   ```
   t=3944278 @76:610,396 tick  queue 10×Step 27×Wait (look for gems: explore → (585, 385))
   t=3944491 @76:571,373 tick  queue 10×Step 28×Wait (look for gems: explore → (558, 362))
   t=3947121 @76:279,444 tick  queue 10×Step 27×Wait (look for gems: explore → (265, 428))
   gems: start 1, max 1, earned: False
   ```

   Suspect: `states/gather_safe.py:53` (`return touches_safe_zone(w, pos) or near_respawn_anchor(w, pos)`). It contradicts the planner's own prompt, `strategist.py:164` ("cut grass and bushes … field work makes about 3 gems a minute"), and starves A63, which can only learn yield from field cuts. A hostile-free, hazard-free cell would be a fair bar for grass.

2. **Gather's fallback explores away from the only ground it may cut.** With nothing cuttable in view, `GatherState.act` calls `safe_default`, which pushes to the nearest frontier outside safe ground. That led 345 blocks west, never toward the town cell the knowledge base already held. 64 of 76 decisions were this fallback.

   Suspect: `states/gather.py:51–53`. When Gather's bar is "near town or a safe zone", its fallback should head for town or the nearest known safe tile, not for the frontier.

3. **The planner spends every call retargeting `gather_gems` x, y to the region it stands in, and that op never runs.** 15 of 17 applied plans differ only in the x, y of the planner's own `gather_gems`, chasing `gem_yield.here` as the character walks ("The previous plan named region 544,352, but the character stands in region 512,352"). The op sits below the pinned directive `gather_gems`, so it never reaches the top; and x, y only lifts a barren mark (`plan.py:67`), it does not move the character. The planner cannot see why Gather is not cutting, so it guesses ("gem_yield has no measured regions yet, so gather_gems has nowhere to start").

   ```
   call 4  gather_gems x=544 y=352   "sample the current region; no measured yields yet, so name it explicitly"
   call 5  gather_gems x=512 y=352   "the character stands in region 512,352"
   call 6  gather_gems x=480 y=352   "the previous plan's 512,352 was wrong"
   …
   call 20 gather_gems x=272 y=432   "State gem_yield.here is (272,432), not (288,432)"
   ```

   Suspects: `gem_yield.py:251` puts `here` in the summary with no cuts behind it, which invites naming it, and `strategist.py:168` frames x, y as where Gather works. The State gives no reason for a gather that sends no cut (e.g. "nothing safe-ish to cut in view").

**Minor:** the spurious `idle` trigger from Walk run 3 is still there. Call 2 fired 4 s in with `{"trigger": "idle", "since_tick": 0, "tick": 3944265, "idle_ticks": 6000}`, and the planner wrote "Idle 6000 ticks: gather_gems on top appears stuck" into its notes. `runner.py:270` seeds `strategist_progress_tick` from `self.world.tick` before any tick is known (0); `strategist.py:708` then fires on the first real tick.
