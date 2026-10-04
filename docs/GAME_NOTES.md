# Game notes (M0)

What the agent needs to know to play Agent Realm. Every fact names its source:

| Tag | Source |
|---|---|
| **M §n** | [Manual](https://agentrealm.gg/docs/manual), section n |
| **API** | [API](https://agentrealm.gg/docs/api) |
| **Guide** | [Create a character agent](https://agentrealm.gg/docs/guides/create-a-character-agent) |
| **SM** | [State machine agent guide](https://agentrealm.gg/guides/state-machine) |
| **Tick** | The `tick` tool's description and intent schema on the Agent Realm MCP server (one call to `POST /characters/{id}/tick`) |
| **Obs** | Observed in play with Pippin (character 1, Olympuff) on 2026-10-04, ticks 1835171–1839533 |

## No spoilers in this repo

This repo is public. It records **rules and mechanics only**. It never records:
- a world's clues: sign, statue and scroll text, or helper lines;
- puzzle solutions or secret locations;
- level layouts, what a level's door needs, boss stats or drops;
- the dependency order between levels.

The agent finds those in play. It keeps them in its per-world knowledge base under `python/.state/`, which is gitignored, and hands them to the strategist from there. Test fixtures use invented worlds, not a real world's text.

## Answers in one screen

- **A level** is a set of maps behind an entrance door on the overworld, with one boss room. **Clearing it means killing its boss** in a timed one-on-one fight. Clear all `level_count` levels to transcend (M §3, §11; API Round Trip).
- **Levels may depend on each other.** The minimap marks every entrance, unnumbered. An entrance may be hidden behind a breakable block, locked, or across water. A key may be dropped by a boss (M §5.3, §9.2, §11).
- **The world tells you how to progress through text:** signs, statues, scrolls and helper lines. Text read in town and at entrances describes what each level asks for (Obs). Capturing and interpreting that text is the strategist's main job.
- **Compose** joins fragment supplies into the whole they belong to, once all pieces are held. There is no workbench and no recipe catalog (M §6; API Snapshots).
- **There is no "build".** No verb places blocks. The world changes only by destroying blocks, which grow back, and by composing supplies (M §6, §11).
- **Combat** uses published d20 rules. A new character has 10 health and attack and defense of 0, so gear decides fights. Two weak hostiles killed Pippin in about 6 s (Obs).

## Loop, budget and timing

- **Clock and budget.** 10 ticks/s. One request per character per tick, burst 3, counted across every `/characters/{id}/…` route, reads included (M §7.4).
- **Queue.** Each POST carries up to `queue_horizon_seconds × tick_rate` intents, 40 at 10 Hz (M §7.6). Over that is `queue_too_long`.
- **Step.** `{"verb": "Step", "direction": d}` with `d` one of `up`, `down`, `left`, `right`, `up_left`, `up_right`, `down_left`, `down_right`; `up` is toward row 0. It moves one block from wherever the character stands when it runs, so the rest of a queue stays valid across an idle tick. `{"verb": "Wait"}` takes no fields (Tick).
- **Movement pacing.** At movement speed 2500 (2.5 blocks/s), a move is allowed every 4 ticks: `Step, Wait, Wait, Wait`. Three rates use the same accumulator mechanism (API Movement):
  - movement: one move per 1000 / speed seconds;
  - attacks: one per weapon cooldown, 1 s by default (10 ticks);
  - speech: one per second;
  - each is an integer accumulator capped at one action, so nothing banks.
- **Pacing worked in play.** About 60 queued steps ran with no `movement_cooldown` (Obs).
- **Breaking a block spends the attack accumulator.** `Use` on a bush, then `Use` again 2 ticks later, was rejected `attack_cooldown` (Obs 1835985–87).
- **Some intents don't wait on the move accumulator.** `Arm`, `Read`, `Take`, `WithdrawFromChest` and `Use` ran on the tick right after a `Step`. Nine `Read`s in a row on consecutive ticks all applied (Obs).
- **First rejection clears the rest of the queue** (M §7.6). One `not_traversable` step (a bush on the path) discarded 35 queued intents (Obs 1835532).
- **Snapshot deltas only arrive against the current or the immediately previous version.** Anything older gets a complete snapshot (M §7.1). The version advances whenever anything in view changes, so a client polling every ~40 ticks while moving got a complete snapshot nearly every time (Obs). The executor must treat complete snapshots as normal, or poll every tick.
- **Cache tiles.** Terrain and entity reads are versioned in 16×16-block cache tiles aligned to the map origin (API Reads).
- **Snapshot shape.** `inventory` carries `armed`, `gems`, `held`, `worn` by slot, and `chest`. `levels_cleared` is absent while it is empty (API Snapshots; Obs).
- **Hand play through MCP is too slow for combat.** Each MCP round trip took about 4 s of wall time (40–80 ticks), so Pippin's retreat landed after the death. A real executor must poll about every tick while threatened, and queue a retreat with every attack (Obs).

## Movement and blocks

- **Block types.** Walkable: `grass`, `dirt`, `tile`, and `fire`/`lava`, which deal `occupy_damage` on the tick you enter and every second you stay. Blocked: `water`, `bush`, `tree`, `rock`, `mountain`, `wall`. Doors (warp): `framed_door`, `rock_entry`. Treat an unknown type as blocked (M §9.2).
- **Door destinations are not served.** A terrain cell names a door's `block_type` (and `locked`), never where it leads. The agent learns each warp by observation: it steps on, then reads its position (M §9.2; A26).
- **Art.** A cell can carry art (path, fence, house, statue, sign, pond, bridge), but behaviour always comes from `block_type`. Statues and signs are `wall` cells with `readable: true` (M §9.2; Obs).
- **Breaking blocks.** `Use` with the right supply armed destroys a block; anything else is `applied_no_effect`. A block never says whether it breaks or what breaks it (M §11).
- **Capabilities, per the manual:**
  - every sword cuts and chops;
  - every mallet smashes;
  - matches and torches burn;
  - bombs blast
  (M §11, §16).
- **Which item works is a property of each block** (Obs). One bush fell to a sword and became `dirt`. Another ignored the sword (`applied_no_effect`) and burned with matches into `fire`. The agent must try the capabilities it carries and remember, per block, what worked and what didn't.
- **Tools are used up when they break a block; weapons are not** (M §11). Matches were consumed on use (Obs).
- **Destroyed blocks recover.** A destroyed block shows its destroyed type until it grows back. It may reveal a door, which warps only while open, and may drop a supply (M §11). A cut bush grew back in 60 s, 600 ticks (Obs 1835985→1836585).
- **Locked doors.** A locked door shows `locked: true` and never names its key. Stepping onto it with a matching key consumes the key and warps; without one the move is `door_locked` (M §9.2, §11).
- **Water** is walkable only while an armed, worn or timed supply makes it so. `would_strand` stops you unequipping it while on water (M §11).
- **Occupancy.** One character or NPC per block. Moving onto an NPC is `block_occupied`. Characters move before NPCs act (M §11).

## Zones

- **Zone fields.** `get_zone` returns `brightness`, `safe`, and `strength_ceiling` for a hunting ground. A cell in no zone reads brightness 1, not safe (M §5.3).
- **Zone reads need a revealed cell.** `get_zone` answers only for a cell the character has revealed (PLAN.md **What the API gives today**). The agent probes only revealed cells, and a cell whose read is refused with a 4xx is not probed again (A7). Unverified: the exact refusal code.
- **Safe zones.** No damage of any kind lands there, and you can't attack or set a trap from inside one. Breaking blocks is allowed (M §11). Attacking a hostile from a safe tile was `not_allowed_in_safe_zone` (Obs 1836584).
- **Safe zones work as a refuge.** A chest outside the safe zone can still be emptied from a safe tile next to it (Obs 1837572).
- **Edges are sharp.** Town's gate tiles were safe; the tile one step outside was not (Obs).
- **Hunting grounds.** Your strength is permanent attack + permanent defense + armed weapon damage + worn armor defense. Over the ceiling you can't enter (`over_strength_ceiling`), and if you grow past it inside you are moved out (API Movement). Strength is readable only on the owner watch sheet (`/watch/characters/{id}/sheet`, viewing bucket), not on `get_self` (M §5.5).
- **Light.** Sight range = perception × zone brightness, plus an armed torch or lantern (or a worn light), capped at perception (Guide, The world model).

## Combat

- **No battle state.** Attacking is `Use` with a weapon armed: on a character, or on the block an NPC stands on (M §11; Guide).
- **Rolls.**
  - To hit: d20 + attack ≥ 10 + target defense + target armor defense. 1 always misses, 20 always hits.
  - Damage: uniform from 1 to max(1, attack + weapon damage − defense − armor defense).
  - Die size, hit target and minimum are per-world settings (API Use).
  - Olympuff keeps permanent attack and defense at 0 (M §16). With attack 0, a d20 roll of 10+ hits a defense-0 target: 55%.
- **Hostiles.** A hostile, trap or boss uses its damage number as attack power, with no weapon damage. Hostiles use the same move and attack accumulators as characters (API Use, Movement).
- **What two weak hostiles did** (Obs 1836771–1836830):

  | Hostile | Hits | Interval |
  |---|---|---|
  | `snotling` | 1 | about 15 ticks |
  | `gristlewick` | 1–2 | about 14–15 ticks |

  - Stepping next to the pair drew a hit on that same tick.
  - Both attacked once Pippin was adjacent, though only one was attacked.
  - Seven hits took 10 health in 59 ticks.
  - Pippin's bronze sword landed 2 of 3 swings, for 1 and 2 damage.
- **Hostiles stay near their spawn.** The pair stood just outside town for over 30 minutes, moving at most one block, and never followed into the safe zone (Obs).
- **Lesson.** Several hostiles close together are one fight, not several. Never step next to a group with 10 health. Count every hostile within two blocks of the target before engaging (Obs).
- **Combat events.** `NPCDamaged` shows damage dealt; a miss emits nothing. `NPCDied` marks a kill (M §8).
- **Bosses** are the only NPCs with `health`/`max_health` on entity reads (M §9.3).
- **Healing.** Potions and food heal (M §16). A new character starts at 10 health. Regeneration out of combat has not been observed.
- **Never wake or idle next to a hostile.** An unattended character keeps taking hits (Guide).

## Items, slots and gear

- **Slots.** One armed slot; `Arm` costs the tick. Five worn slots: `head`, `body`, `legs`, `feet` for armor, `accessory` for one accessory (M §6, §11).
- **Carry capacity is the chest.** Everything held, worn and armed counts; `carry_capacity_full` when full. A new character's chest holds 10 (M §11).
- **`attack_range`** on `get_self` is the armed weapon's reach; 1 when the weapon authors none (M §5.3).
- **Olympuff starting kit:** a non-transferable pocket knife (damage 2, range 1, cuts grass and bushes). It survives death and is re-armed on respawn (M §5.3, §11; Obs: Died dropped everything but the knife).
- **Buying.** Walk onto, or `Take`, a supply with a `gem_price`. Without enough gems it is `not_enough_gems` (M §11).
- **Olympuff town shop prices seen** (Obs):

  | Item | Gems |
  |---|---|
  | `bronze_sword` | 15 |
  | `bronze_mail` | 20 |
  | `bronze_mallet` | 25 |
  | `middle_chest` | 50 |
  | `torch` | 10 |
  | `matches` | 5 |
  | `small_potion` | 10 |

- **Gems and lives are counters.** Gems and hearts (extra lives) are consumed on pickup into counters (M §11). In Olympuff, cut grass and bushes drop gems and hearts (M §16).
- **Food.** Olympuff's golden cap heals 6 and is eaten on pickup (M §16). Potions are drunk with `Arm` + `Use` on self; swapping what is armed costs a tick (API Use). Small potion +10, large +30 (M §16).
- **Gear tiers:** bronze in town, iron at waystations, adamant at the Last Camp and from bosses (M §16).
- **Gems come from** cutting grass and bushes (10% in ring 1, 15% farther), felling trees, gem piles that return on an interval, and gem caches. Field work earns about 3 gems a minute (M §16). Gems are kept on death.
- **Food and gems on the ground.** Apples, berries and gem piles lie around town, free to pick up (Obs).

## Compose

- **What it does.** `Compose {supply_ids}` assembles a full set of fragments into their finished supply (M §6).
- **Fragments describe their whole.** A fragment carries `fragment: {composes_into, piece_count, slot, missing_slots}` on entity reads and in your inventory. Holding one piece shows the whole and which pieces are missing, never where they are (API Snapshots, Reads).
- **Rejections:** `not_composable` (permanent), `fragments_missing` (precondition) (M §10.2).
- **Recipes come from fragment catalog metadata** (M §14). The agent knows a recipe exists the moment it holds one piece, and needs no clue to learn its shape.

## NPCs, signs and scrolls

- **Helpers** stay put. `Say` to a helper within 25 blocks gets its one authored line back that tick as a `SpokenTo` with `speaker_kind: npc`; it is the same line every time. Hostiles, bosses, and helpers with no line stay silent, but the `Say` still applies (M §6, §11).
- **Shopkeepers had nothing to say** (Obs). Other helpers gave clues, some of them riddles (Obs).
- **Signs.** A sign is a block with `readable: true`. `Read {kind: block}` returns its text on the intent result while it is in sight (M §6). Statues were readable signs too (Obs).
- **Scrolls.** A scroll is a supply. `Read {kind: supply}` works on a carried scroll, or one on the ground in sight. `Read` on a non-scroll supply is `nothing_to_read` (Obs).
- **Speech cost.** `Read` doesn't spend the speech accumulator; `Say` and `Broadcast` share 1/s (M §11).
- **Speech isn't kept.** Speech is an event: missed is gone (M §8). Store every line.
- **Clues point at secrets.** The manual says a helper near something hidden often hints at it, without coordinates, and that an "odd block out" is often breakable (M §16). In play, clue text did lead to a breakable block with a reward (Obs).

## Traps

- **Firing.** An armed trap fires on any character that steps onto it, outside safe zones, and stays armed. NPCs never trigger traps. A hit you could not see still arrives as `Damaged {source_kind: trap}` (M §11).
- **Seeing traps.** You see traps up to your detection grade; goggles raise it. A seen armed trap carries `trap_armed: true` on entity reads (M §9.3, §11).
- **Disarming.** `Disarm` succeeds when d20 + detection grade ≥ 10 + trap grade. Success turns the trap into a takeable supply; failure triggers it. `no_trap_here` looks the same for bare ground and an unseen trap (API Use; M §10.2).
- **Re-arming.** An authored trap re-arms the tick after its lifetime ends (M §11).

## Death, respawn, sleep

- **On death:**
  - you are downed for `respawn_delay_seconds` (5 s);
  - every intent is `character_dead`;
  - the carried chest drops with everything except gems and non-transferable items;
  - you respawn at full health in the nearest respawn zone, outside any level;
  - this is all M §11.
- **Pippin's death** (Obs 1836830–80):
  - lives went 6 → 5 on the death tick;
  - `Died` named `chest_id` and its landing block, and listed the 4 dropped supplies;
  - `Respawned` came exactly 50 ticks later, on the town plaza;
  - `WithdrawFromChest` with only the `chest_id` took everything back.
- **Zero lives.** At zero lives the character is ended, permanently: every intent is `character_ended` (M §11).
- **Respawn zones:** Olympuff has five waystations plus a corner of the town plaza (M §11).
- **Dying in a boss room** puts the chest outside the level, within 20 blocks of its perimeter (M §11).
- **Sleep.** `Sleep` needs 10 s with no damage dealt or taken (`recent_damage`) and is refused inside a level (`sleep_not_allowed_in_level`). A character with no intent for 10 minutes falls asleep (M §11).
- **Sleeping in town worked** (Obs 1839492): the round trip then carries only `asleep: true` and results.

## Levels and bosses

- **Finding entrances.** `get_minimap` lists every level entrance on revealed maps, found or not, with no level number. A map inside a level carries `level` (M §5.3). Olympuff's overworld (800×800) showed 8 marks from town (Obs).
- **Entering.** An entrance is a door. It may be hidden behind a breakable block, locked (`locked: true`, needing a key), or out of reach across water (M §5.3, §9.2, §11).
- **Boss room door:**
  - it admits one character at a time;
  - it rejects `boss_room_occupied` while a fight is on, or while you are already in another boss fight;
  - there is no queue, only the same-block coin flip when it opens
  (M §11).
- **Boss fight:**
  - it is on a time limit, and running out of time sets your health to 0;
  - a death in the boss room respawns you outside, with the chest outside the level
  (M §11; Guide).
- **Clear:**
  - the round trip carries a one-shot `level_clear_ceremony {level_number, max_health_gain}`;
  - health refills, and max health rises on the first clear;
  - you are moved outside the level that tick;
  - `levels_cleared` updates in the snapshot
  (M §7.2; API Round Trip).
- **Inside a level** you can't `Sleep` (M §11).
- **Clearing all `level_count` levels** transcends the character, which ends it (M §11).

## Open questions

Each has a test the agent or a hand session can run.

| Question | How to find out |
|---|---|
| Hostile health per type (no NPC's health is served but a boss's) | Sum `NPCDamaged` on one NPC until its `NPCDied` |
| Hostile stats (defense, range, speed) per type | Log `NPCDamaged` and our swings for the hit rate (gives defense); log `Damaged` and spacing for damage and cooldown; log first `Attacked` distance for range |
| Does health regenerate out of combat? | Take a hit, retreat to town, read `health` every 10 s for 2 minutes |
| Weapon damage and cooldown per subtype | Swing at a lone weak hostile from full health, with a retreat queued in the same request. `NPCDamaged` is copied to every character that sees the block (API Events), so count only one on the block and tick our `Use` resolved |
| How much damage each worn item saves | Take hits from one hostile type with and without the item worn and compare `Damaged` amounts; one hit can't be split between worn slots |
| What each level's entrance needs | Walk to each minimap mark; read terrain (`locked`, block type), signs and helpers nearby. Stored in `.state/`, never committed |
| Hunting ground locations and ceilings | `get_zone` on cells around town |
| Boss fight time limits | Read on entry, or learn from the first attempt |
| How ground food other than the golden cap heals (apples, berries): on pickup, or carried and `Use`d on self | `Take` one while hurt and read `health`; if unchanged, `Arm` + `Use` self |
| Does any supply raise max health permanently, besides a level's first clear? | Watch `max_health` in the snapshot after every pickup and `Use` |
| How to tell a scroll supply from others before reading it. Nothing sourced names scroll subtype codes, so `Investigate` reads no scrolls yet (PLAN.md A30) | Log `supply_subtype_code` of every supply seen; `Read` one of each once and keep the codes that do not answer `nothing_to_read` |
| Do art or a statue's `facing` mark secrets? The manual only says art is a picture and behaviour comes from `block_type` (M §9.2) | Log art and facing next to every secret found, and compare |
