# Game notes (M0)

What the agent needs to know to play Agent Realm. Every fact names its source:

| Tag | Source |
|---|---|
| **M §n** | [Manual](https://agentrealm.gg/docs/manual), section n; §11 names its sub-heading (M §11 Food and potions) |
| **API** | [API](https://agentrealm.gg/docs/api) |
| **Guide** | [Create a character agent](https://agentrealm.gg/docs/guides/create-a-character-agent) |
| **SM** | [State machine agent guide](https://agentrealm.gg/guides/state-machine) |
| **Tick** | The `tick` tool's description and intent schema on the Agent Realm MCP server (one call to `POST /characters/{id}/tick`) |
| **Obs** | Observed in play, on a point the published docs are silent on (the Manual, the API page, the guides, the Supplies reference). Unless it names a run, by the observer character (Olympuff) on 2026-10-04, ticks 1835171–1839533. An observation the docs now state is cited to them instead (A152) |
| **SR** | The Manual's Supplies reference (M §11 Supplies reference), also served as [`supplies.json`](https://agentrealm.gg/docs/supplies.json) and bundled as `python/agentrealm_adk/reference/supplies.json` |
| **CL** | [Changelog](https://agentrealm.gg/docs/changelog), and the server's change notes behind it (saims B136, B137, B139: the update live on 2026-10-10; B138 capabilities, live on 2026-10-11; the Manual's core play rules, 2026-10-12) |

Checked against the published docs on 2026-10-12 (A152): every fact the Manual states is cited to it, and what it contradicted is corrected. Where the agent's code still contradicts or works around a stated rule, [`observations/A152_manual_sync.md`](observations/A152_manual_sync.md) lists it.

## No spoilers in this repo

This repo is public. It records **rules and mechanics only**. It never records:
- a world's clues: sign, statue and scroll text, or helper lines;
- puzzle solutions or secret locations;
- level layouts, what a level's door needs, boss stats or drops;
- the dependency order between levels.

The agent finds those in play. It keeps them in its per-world knowledge base under `python/.state/`, which is gitignored, and hands them to the strategist from there. Test fixtures use invented worlds, not a real world's text.

## Answers in one screen

- **A level** is a set of maps behind an entrance door on the overworld, with one boss room. **Clearing it means killing its boss** in a timed one-on-one fight. Clear all `level_count` levels to transcend (M §3, §11; API Round Trip).
- **Levels can be cleared in any order.** Beating the world takes one clear of every level number, whichever came first; a second clear of a level counts once. Content gates the order instead: difficulty first, and some level doors are locked and need a key found elsewhere, often from another level's boss. The minimap marks every entrance, unnumbered. An entrance may be hidden behind a breakable block, locked, or across water (M §5.3, §9.2, §11 Bosses and levels). The agent takes them lowest number first by choice (A145, Evan), not by a game rule.
- **The world tells you how to progress through text:** signs, statues, scrolls and helper lines. Rumor helpers give rough directions toward doors, and waystation helpers hint at what locked doors need (M §16 Rumor helpers). Capturing and interpreting that text is the strategist's main job.
- **Compose** joins fragment supplies into the whole they belong to, once all pieces are held. There is no workbench and no recipe catalog (M §6; API Snapshots).
- **There is no "build".** No verb places blocks. The world changes only by destroying blocks, which grow back, and by composing supplies (M §6, §11).
- **Combat** uses published d20 rules. A new character has 10 health, permanent attack power 2 and defense 0, so gear decides fights (M §11 Bosses and levels, §16, CL). Health never comes back on its own (M §11 Food and potions). Two weak hostiles killed the observer character in about 6 s (Obs, before attack power 2).

## Loop, budget and timing

- **Clock and budget.** 10 ticks/s. One request per character per tick, burst 3, counted across every `/characters/{id}/…` route, reads included (M §7.4).
- **Queue.** Each POST carries up to `queue_horizon_seconds × tick_rate` intents, 40 at 10 Hz (M §7.6). Over that is `queue_too_long`.
- **Queue results and the end of a queue (B133).** An applied `Wait` has no entry in `intent_results`; a rejected `Wait` and every other intent's result still report, each with its `queue_id` and `index`, so a gap in `index` is a Wait that ran. Results never add up to the queue's length. While the queue is held the round trip carries `queue` (`queue_id`, `next_index`: every index below it has run). Once it runs to its end the round trip carries `finished_queue: {queue_id, length}` instead, `length` counting the Waits. A rejection ends the queue on that index and its result says so; the manual sends `finished_queue` only for a queue that ran out with no rejection, but the agent reads the rejection either way. A `finished_queue` naming a queue the agent already replaced says nothing about the current one (M §7.6; API Tick loop; Changelog B133).
- **Draining a long walk by hand.** Send at most 40 intents (each `Step` followed by its pacing `Wait`s), then `POST tick` with no `intents` field until the queue runs out. Replacing the queue on every poll before it drains left the character stuck; clearing with `[]` and re-queuing worked (Obs [`A47_live_play.md`](observations/A47_live_play.md)).
- **Step.** `{"verb": "Step", "direction": d}` with `d` one of `up`, `down`, `left`, `right`, `up_left`, `up_right`, `down_left`, `down_right`; `up` is toward row 0. It moves one block from wherever the character stands when it runs, so the rest of a queue stays valid across an idle tick. `{"verb": "Wait"}` takes no fields (Tick).
- **Movement pacing.** A new character moves 2.5 blocks per second, one move every 4 ticks at 10 Hz: `Step, Wait, Wait, Wait` (M §11 Movement). Three things wait on a cooldown, each its own, on the same accumulator mechanism (M §11 What waits on a cooldown; API Movement):
  - move (`SetPosition`, `Step`, a door warp included): one per 1000 / speed seconds;
  - attack (a weapon `Use` that rolls against a character or an NPC, hit or miss, or that breaks a block): one per weapon cooldown, 1 s by default (10 ticks); each weapon family sets its own, 0.8 s (the knife) to 1.5 s (bows) in Olympuff, every tier of a family alike: swords 1 s, mallets 1.3 s, whips 1.2 s (M §11, Supplies reference `attacks` `cooldown_ms`, saims B147). The agent paces each `Use` by the armed weapon's (`executor.pacing.attack_cooldown_ticks`, A123);
  - speech (`Say`, `Broadcast`): one per second;
  - each is an integer accumulator capped at one action, so nothing banks.
- **Everything else has no cooldown.** `Arm`, `Wear`, `Remove`, `Take`, `Drop`, `Disarm`, `Compose`, `DepositToChest`, `WithdrawFromChest`, `Read`, `Wait`, `Sleep`, and every `Use` of a consumable, a tool or a trap (a drink, a chest upgrade, a teleport, a bomb) run on the tick they are queued for. A rejected move or swing spends nothing, and neither does a weapon `Use` that changed nothing (M §11 What waits on a cooldown). Each still takes its tick: one intent per tick (M §3).
- **Breaking a block spends the attack cooldown**, as a swing does; a strike that breaks nothing spends nothing (M §11 Breaking blocks).
- **Changing weapons does not reset the attack cooldown.** A swing is judged against the cooldown of the weapon armed when it runs: a quicker weapon may swing at once, a slower one waits out its longer cooldown. `move_ready_at_tick` and `attack_ready_at_tick` in the observation say when the next may run (M §11 What waits on a cooldown, §7.2).
- **First rejection clears the rest of the queue** (M §7.6).
- **Snapshot deltas only arrive against the current or the immediately previous version.** Anything older gets a complete snapshot (M §7.1). The version advances whenever anything in view changes, so a client polling every ~40 ticks while moving got a complete snapshot nearly every time (Obs). The executor must treat complete snapshots as normal, or poll every tick.
- **Cache tiles.** Terrain and entity reads are versioned in 16×16-block cache tiles aligned to the map origin (API Reads).
- **Snapshot shape.** `inventory` carries `armed`, `gems`, `held`, `worn` by slot, and `chest`; each supply is in exactly one of them (Items, below; M §11 Where your supplies are; API Snapshots). `levels_cleared` is absent while it is empty (Obs).
- **Hand play through MCP is too slow for combat.** Each MCP round trip took about 4 s of wall time (40–80 ticks), so the observer character's retreat landed after the death. A real executor must poll about every tick while threatened, and queue a retreat with every attack (Obs).

## Movement and blocks

- **Block types.** Walkable: `grass`, `dirt`, `tile`, and `fire`/`lava`, which deal `occupy_damage` on the tick you enter and every second you stay, less your defense and the armor you wear. Blocked: `water`, `bush`, `tree`, `rock`, `mountain`, `wall`. Doors (warp): `framed_door`, `rock_entry`. Treat an unknown type as blocked (M §9.2).
- **Clouds.** In a terrain read, `?` is clouds: a cell never seen, or past the map's edge. Treat it as unknown, not as empty (M §9.2). Every block you see now or have seen has a symbol showing its current type (M §9.2, §3 Perception). Sight is perception range times the brightness of the zone you stand in (plus a light), so in a dark zone, a level say, most of the perception window reads `?` until walked near; a revealed cell stays readable. The map's size comes with the read (`map_width`, `map_height`, left out when the tile cache has none) and with each map on the minimap (API Tiles B102, Perception and sight, GetMinimap).
- **Door destinations are not served.** A terrain cell names a door's `block_type` (and `locked`), never where it leads. The agent learns each warp by observation: it steps on, then reads its position (M §9.2; A26).
- **Art.** A cell can carry art, but behaviour always comes from `block_type`. There are no looks-only types: a path is `dirt` with `art` `path`, a bridge is `tile` with `art` `bridge`, and a `house`, `hut`, `tent`, `building`, `fence` or `well` is `wall` with art of that name. A sign carries `readable: true` (M §9.2). Statues were readable signs too (Obs).
- **Breaking blocks.** `Use` with the right supply armed destroys a block; anything else is `applied_no_effect`. A block never says whether it breaks or what breaks it (M §11).
- **What breaks what** (M §11 Breaking blocks). Capabilities, never one named item: every sword cuts and chops, every mallet smashes, matches and torches burn, bombs blast. In the shipped worlds:

  | Capability | Carried by | Breaks | Tool used up | Becomes |
  |---|---|---|---|---|
  | `cuts` | Pocket knife, every sword | Grass, bushes | — | Dirt |
  | `chops` | Every sword | Trees | — | Dirt |
  | `smashes` | Every mallet | Rocks | — | Dirt |
  | `burns` | Matches, torch | Hedge trees, a few bushes | Yes | Fire, which burns whoever stands on it until it grows back |
  | `blasts` | Bomb, salvaged bomb | Rocks, mountain, a few walls | Yes | Dirt, or a door where a hidden entrance was |

  This is the rule set, not a map: many grass, bush, tree, rock, mountain and wall blocks name no capability and never break, and some trees and bushes fall only to fire. Dirt, tile, water, fire, lava and doors never break. Only reach-1 weapons break blocks: no whip or bow carries a block breaker. An odd block that wandered in from elsewhere may name any of the five, whatever its type (M §11 Breaking blocks). In the sandbox every breakable block names `blasts` (M §11). The agent tries a block with the capabilities known to open its type, or one that opened that cell before (`break_memory.BLOCK_OPENERS`, A103).
- **Town grass does not cut** (Obs, A63 run 2). Every `Use` with the pocket knife on town grass came back `applied_no_effect`. The Manual allows it: many grass blocks name no capability, and a weapon still breaks a block in a safe zone, so nothing about town says it is safe ground that blocks it (M §11 Breaking blocks, Combat). The agent learns uncuttable ground per region from its own no-effect cuts (A63; [`A63_gem_gathering_runs.md`](observations/A63_gem_gathering_runs.md) Run 2).
- **Which item works is a property of each block** (M §11 Breaking blocks: some bushes fall only to fire). One bush fell to a sword and became `dirt`; another ignored the sword and burned with matches into `fire` (Obs). The agent remembers, per block, what worked and what didn't.
- **Tools are used up when they break a block; weapons are not.** A tool that breaks nothing is not used up (M §11 Breaking blocks).
- **Destroyed blocks recover.** A destroyed block shows its destroyed type until it grows back, may reveal a door, and may drop a supply; striking it again meanwhile changes nothing. It grows back after its type's recovery time in that world, 60 s unless the world sets another, every block of a type alike; Olympuff's odd blocks take 10 minutes (M §11 Breaking blocks, Regrowth, drops, and returns).
- **Locked doors.** A locked door shows `locked: true` and never names its key. Stepping onto it with a matching key held uses up the key and warps, on every pass, so keep a spare to come back; without one the move is `door_locked` (M §9.2, §11 Movement, Bosses and levels).
- **Water** is walkable only while an armed or worn supply makes it so; a supply only carried does not, and a timed effect does not count. On water an `Arm`, `Wear`, `Remove` or `Drop` that would leave none is `would_strand` (M §11 Movement, CL B138).
- **The raft is armed, never worn.** It is a tool: `Wear` is `not_wearable`. Armed, it leaves no weapon in hand, so a `Use` on a character or NPC is `applied_no_effect` while hostiles still hit you; cross, step onto land, then `Arm` the weapon. The Supplies reference's raft row reads "armed or worn" only because `fords` allows both. A worn fording supply (armor that walks on water, such as the water-walking sandals) leaves the armed slot free for a weapon (M §11 Movement, SR).
- **Occupancy.** One character or NPC per block. Moving onto an NPC is `block_occupied`. Characters move before NPCs act (M §11).

## Zones

- **Zone fields.** `get_zone` returns `brightness`, `safe`, and `strength_ceiling` for a hunting ground. A cell in no zone reads brightness 1, not safe (M §5.3).
- **Zone reads need a revealed cell.** `get_zone` answers only for a cell the character has revealed (PLAN.md **What the API gives today**). The agent probes only revealed cells, and a cell whose read is refused with a 4xx is not probed again (A7). Unverified: the exact refusal code.
- **Safe zones.** No damage of any kind lands there (hits, damaging blocks, traps), and you can't attack or set a trap from inside one (`not_allowed_in_safe_zone`). Breaking blocks is allowed. Hostiles are not kept out: one may walk in and swing, but the swing deals nothing (M §11 Combat, §10.2).
- **Safe zones work as a refuge.** A chest outside the safe zone can still be emptied from a safe tile next to it (Obs 1837572).
- **Edges are sharp.** Safety is judged on the exact block: stand a block inside the edge, not on the line. `safe` on terrain reads and `get_zone` mark it (M §11 Combat).
- **Hunting grounds.** Your strength is permanent attack + permanent defense + armed weapon damage + worn armor defense. Over the ceiling you can't enter (`over_strength_ceiling`), and if you grow past it inside you are moved out (API Movement). Strength is readable only on the owner watch sheet (`/watch/characters/{id}/sheet`, viewing bucket), not on `get_self` (M §5.5); the sheet counts only an armed weapon's damage and worn armor's defense, never a held potion or worn non-armor (CL, B139). Neither serves attack power on its own.
- **Olympuff's hunting ground** has `strength_ceiling` 7: attack power 2 plus 5 of gear. The pocket knife or a bronze sword, with bronze mail or without, gets in; a bronze mallet or anything stronger does not (M §16, CL: it was 5 before attack power 2).
- **Light.** Sight range = perception × zone brightness, plus a light's radius, capped at perception (Guide, The world model). A torch or lantern lights for its lit time (two and five minutes) after `Use` on yourself, without being used up, and gives light only while armed; an always-on light such as the firefly jar lights armed or worn. Only the largest radius carried counts; lights do not add up (M §11, CL B138).
- **Where a supply works** (M §11 Armed, worn, or only carried). Carrying it does nothing unless this says so: boots in the chest give no speed.

  | Works | Capabilities |
  |---|---|
  | Armed, then `Use` | `attacks`, the block breakers, `heals` on a potion, `holds` (chest upgrade), `teleports`, `traps`, a torch or lantern lit with `Use` on yourself |
  | Armed or worn | `speeds`, `perceives`, `detects`, `fords`, `unmasks`, an always-on light |
  | Worn only | `protects` (armor in hand protects nothing), `disguises`, `maps` (accessory slot) |
  | Carried (held or armed) | A key: a locked door takes a matching key you hold |
  | On pickup | `heals` on food, gems, hearts |

  The armed slot is the weapon's, so the agent wears what works worn (A97, A147).

## Combat

- **No battle state.** Attacking is `Use` with a weapon armed: on a character, or on the block an NPC stands on (M §11; Guide). A `Use` target `{"kind": "npc", "npc_id": N}` resolves that block on the tick the `Use` runs; an NPC not in sight that tick, dead, or unknown is `target_out_of_range`, and reach, safe zones, cooldown and the hit roll are those of a `Use` on the block (API Use; server release 1.11, saims B126). `{"kind": "direction", "direction": d}` names the neighbouring block in that direction from wherever the character stands when the `Use` runs; a missing or unknown direction is `malformed_intent` (API Use).
- **Rolls.**
  - To hit: d20 + attack ≥ 10 + target defense + target armor defense. 1 always misses, 20 always hits.
  - Damage: uniform from 1 to max(1, attack + weapon damage), less defense and armor defense, never below 0. Weapon damage adds to attack power for this roll only; it does not change the roll to hit (M §11).
  - Armor counts only worn: its `defense` protects against weapon hits, hostile and boss hits, traps and, since B138, damaging blocks (`occupy_damage`, before cut by defense alone); armor in hand protects nothing (M §11, CL B138). The route planner prices a hazard step at the `occupy_damage` that gets through (A97).
  - Die size, hit target and minimum are per-world settings (API Use).
  - Every Olympuff character has permanent attack power 2, existing characters included; permanent defense stays 0 (M §16, CL). A d20 roll of 8+ hits a hostile, which has no defense: 65%. Damage is 1 up to 2 plus weapon damage: the pocket knife deals 1–4, a bronze sword 1–6. The world's hit target stays 10, so hostiles' swings at characters are unchanged.
  - The API serves no attack power (PLAN.md **Server gaps**); the agent's win estimate uses the base of 2 (`survival.BASE_ATTACK_POWER`).
  - `Damaged.amount` is what got through: a hit armor absorbs whole is `Damaged` with 0 (API Events, B131). The threat table adds back the defense of the armor worn when a hit lands, so it holds the hostile's own number, and the win estimate and Retreat's floor take the armor worn now off it once (`threat.absorb_damaged`, `survival.worn_defense`, A97). The armor added back is the loadout a response leaves, so a hit landed before an armor swap in the same response is filed off by the armor swapped; accepted as rare.
- **Hostiles.** A hostile or boss uses its attack power, with no weapon damage (no shipped NPC carries an `attacks` amount; saims B142). A trap strikes with attack power equal to its grade: grade 1 hits an unarmored character 60% of the time for 1, grade 9 95% for 1 to 9 (M §11, saims B146). Hostiles use the same move and attack accumulators as characters (API Use, Movement).
- **How hostiles move** (M §11 Hostiles). A hostile goes for the nearest living character on its map, with no sight or distance limit, and none waits to be hit first. It attacks whoever is in its attack range, no faster than its cooldown. A **chaser** steps toward the nearest character at its speed, taking the neighbouring block that brings it closest and standing still when none does, so a wall or a river stops it; it finds no path. A **patroller** walks a fixed loop and attacks only what comes into range. There is no leash: a hostile spawned across a zone never leaves that zone; a hand-placed one can follow you anywhere walkable on its map. A hostile never heals while it lives and never walks back to its spawn. Killed, it returns after its own respawn timer (30 s to 5 minutes for shipped hostiles) at full health, where it fell or, for a zone spawn, anywhere free in its zone. A kill may drop one supply on its block, often a gem, never onto a block that already shows a supply. The agent sees neither a hostile's kind (chaser or patroller) nor its zone, and no hostile's speed is published, so it prices every hostile as a chaser free to follow at the character's pace (`hostile_ground.chase`, A162).
- **What two weak hostiles did** (Obs 1836771–1836830):

  | Hostile | Hits | Interval |
  |---|---|---|
  | `snotling` | 1 | about 15 ticks |
  | `gristlewick` | 1–2 | about 14–15 ticks |

  - Stepping next to the pair drew a hit on that same tick.
  - Seven hits took 10 health in 59 ticks.
  - The observer character's bronze sword landed 2 of 3 swings, for 1 and 2 damage (at attack power 0).
- **Corrected (M §11 Hostiles).** These notes once said hostiles stay near their spawn and never follow into a safe zone, from a pair that stood just outside town for 30 minutes (Obs). The Manual says otherwise: a chaser goes for the nearest character on its map and may walk into a safe zone (its swing there deals nothing). That pair was zone-bound, or patrollers, or blocked by ground.
- **Lesson.** Several hostiles close together are one fight, not several. Never step next to a group with 10 health. Count every hostile within two blocks of the target before engaging (Obs).
- **Combat events.** A swing that rolls is `applied`, hit or miss, and spends the cooldown; its intent result carries `hit` and, on a hit, `damage`, what the target took. A swing at an empty block is `applied_no_effect` with neither (M §11 Combat, B131). A swing at an NPC shows to everyone in sight as `NPCAttacked`, plus `NPCDamaged` on a hit. `NPCDied` marks a kill (M §8, §11 Combat). These notes once said a miss emits nothing; it emits `NPCAttacked`.
- **Who hit us.** `Damaged` names its source in `source_kind` (`npc`, `character`, `trap`, `occupy`) and, except for `occupy`, `source_id` (API Events; `threat.py` keys the threat table on them). `Attacked` names its swinger in `actor_kind` and `actor_id`, on every swing at us, hit or miss; a hit adds a `Damaged` from the same attacker, so an `Attacked` with no such `Damaged` on its tick is a miss (API Events, B131; `threat.count_swings` counts each type's hits and misses from them, A85). Who is hitting us (A9) still follows only a `Damaged`'s source: an `Attacked` on a later tick than the last named hit clears it.
- **Bosses** are the only NPCs with `health`/`max_health` on entity reads: "A boss NPC also includes its current `health` and `max_health` … no character, hostile, or helper carries health" (M §9.3; API Reads). The agent identifies a boss by that field (A38).
- **Healing** (M §11 Food and potions). **Health never comes back on its own.** Only food, potions, a respawn (full health) and a boss clear (refilled to your maximum) raise it, so resting restores nothing and safe zones only stop damage. Food is eaten as you pick it up and heals at once, up to your maximum, even at full health, and never takes room in the chest: a berry 1, an apple 2, a mushroom 3, a golden cap 6. A potion is carried, armed, and drunk with `Use` on yourself, used up even at full health: a potion or a small potion 10, a large potion 30 (SR `heals`). These notes once listed food as `Use`d; food is never `Use`d. So the agent leaves food lying until it is hurt by at least the food's heal, gives food no pack room, counts no food among the heals it carries, and never sends a potion at full health (A155). Maximum health starts at 10 and only a level's first clear raises it; no supply does (M §11 Bosses and levels).
- **Never wake or idle next to a hostile.** An unattended character keeps taking hits (Guide).

## Items, slots and gear

- **Slots.** One armed slot; `Arm` costs the tick. Five worn slots: `head`, `body`, `legs`, `feet` for armor, `accessory` for one accessory (M §6, §11).
- **Where your supplies are** (M §11 Where your supplies are; API Snapshots). The snapshot's `inventory` is `gems`, `armed`, `worn` by slot, `held`, and `chest`, each supply an `id` and `supply_subtype_code`, and each supply is in exactly one place: the armed supply and the worn ones are not also in `held`, which is everything else the carried chest holds. `inventory.chest` stays empty today: no intent stows a supply in your own chest apart from holding it. `Arm` and `Wear` take a held supply (to arm something worn, `Remove` it first); `Compose` uses held fragments only, so an armed fragment is `not_held`.
- **Carry capacity is the chest** (M §11 How big your chest is). No read names the carried chest or its capacity. Every held supply, the armed one and each worn one count against it; the chest itself does not. You start with the world's starting chest, the blue chest (10 in every shipped world). A chest upgrade (middle 30, red 50) is armed and `Use`d on yourself: `applied` and used up when it made your chest bigger, `applied_no_effect` and still armed when not. **Every death resets it**: the upgrade is the carried chest, it drops with you, and you respawn with a new, empty starting chest; the upgraded chest on the ground keeps its size for whoever empties it (M §11 Death, lives, and ending). `carry_capacity_full` on a `Take`, a pickup or a withdrawal says you are at the limit. The agent derives capacity at every inventory sync from the chest it applied, kept per character (A115) with the lives it was saved at, and resets it on any death it sees or infers (A157).
- **Take and withdraw.** `Take` reaches the supply's block or a neighbour, corners included. A ground chest's `contents` are served only within that same reach. `WithdrawFromChest` with `supply_ids` is all or nothing, `carry_capacity_full` for one too many; without them it takes everything that fits, ascending by id, and a chest with no room for even one is `carry_capacity_full` (M §6, §11; API).
- **Drop** puts a carried supply on the ground under you, held, armed or worn; `DepositToChest` takes held ones only. Both refuse a gem, a life, or a non-transferable supply (the starting kit) with `not_transferable`, permanent (M §6, §10.2, §11 Where your supplies are, Death).
- **A price belongs to the supply** (M §11 A price belongs to the supply). A priced subtype costs its `gem_price` wherever a copy lies on the ground, a copy you dropped included: drop a bought sword and picking it up again costs the price again. Corrected: these notes once read a box of matches dropped on a shop tile and back for sale at 5 gems as "a drop on a shop cell becomes stock" (Obs, [`A102_pack_room.md`](observations/A102_pack_room.md)); the matches were priced because their subtype is, on any cell. The agent still guards only shop cells (`pack.drop_makes_stock`, A102; A152 findings).
- **`attack_range`** on `get_self` is the armed weapon's reach; 1 when the weapon authors none (M §5.3).
- **Supplies reference.** The Manual's Supplies reference lists every supply subtype: class, slot, its capabilities (each with its trigger, `used`, `equipped`, `worn` or `picked_up`, and its `amount`, `reach`, `cooldown_ms` and `duration_seconds` where it takes them: a weapon's damage, attack range and attack cooldown on `attacks`, a potion's or food's heal on `heals`, armor's defense on `protects`, counted only while worn, a chest's capacity on `holds`, light radius on `lights`), whether a break uses it up, whether it is eaten on pickup, and gem price per world. Before saims B138 each row had `use_effects` and a field per stat (`attack_range`, `damage`, `heal`, `teleport_range`, `chest_capacity`, `defense`, `light_radius`, `grants`) instead; `supplies.parse` reads both (A123). The `blue_chest` row (consumable, chest capacity 10) is the chest a character starts with (M §11). The site serves the same rows as JSON at `https://agentrealm.gg/docs/supplies.json`, with no key (API Supplies reference, saims B132). The agent reads it through `supplies.py` (A54).
- **Olympuff starting kit:** a non-transferable pocket knife (damage 2, range 1, cooldown 0.8 s, cuts grass and bushes; with attack power 2 it deals 1–4). It survives death, and the starting kit is armed again on respawn when nothing else is; a character that lacks a kit piece is given it again (M §11 Death, lives, and ending, §16).
- **Buying.** Walk onto, or `Take`, a supply with a `gem_price`. Without enough gems it is `not_enough_gems` and stays (M §11 Shops). The agent never steps onto one (`WorldModel.for_sale()` is impassable except as a search goal): Shop buys only by `Take` from a neighbouring cell, corners included (A21).
- **Shops** (M §11 Shops, Regrowth, drops, and returns). A shop is priced supplies on the ground, often beside a helper; the helper only says its line and knows nothing of stock. Each shop sells what was placed there, so stock differs by shop, and no read lists it: you see stock by sight, as supplies with a `gem_price` on entity reads. The Supplies reference gives each world's price for each subtype, never where it is sold or whether it is in stock. A bought item's stock returns on its home block 2 minutes later, staying hidden while another supply lies there.
- **Olympuff prices and where each tier is sold** are the Manual's (M §16 Gear and prices; SR `gem_prices`): bronze in town, iron at the waystations, adamant at the Last Camp.

- **Gems and lives are counters.** Gems and hearts (extra lives) are consumed on pickup into counters and leave nothing in the chest, so they take no slot; lives cap at 20 ever gained (M §11; API). A heart's subtype is `heart`, class `life`, eaten on pickup (SR). Manual §16 documents **gem** drops from cutting Olympuff grass (20% in ring 1, 25% farther) and from kills; it documents gems and hearts from **sandbox** field grass and bushes when broken.
- **Food and potions** (Healing, above). Potions are drunk with `Arm` + `Use` on self, target `{"kind": "self"}` (M §6 `Use` targets); a `character` target naming our own id drank nothing in free-play run 4 (Obs). The `Arm` takes the tick, with no cooldown (M §3, §11).
- **Gear tiers:** bronze in town, iron at waystations, adamant at the Last Camp and from bosses (M §16).
- **Gems come from** cutting grass with the pocket knife or a sword (20% in the ring-1 fields, 25% in the hunting ground and beyond ring 1), kills, felling wild trees with a sword (a gem at least as often as grass on the same ground, B148), gem piles that return on an interval (10 to 45 minutes by ring in Olympuff), and gem caches. Field work earns about 6 gems a minute (M §11 Regrowth, drops, and returns, §16, CL). Each break rolls at most one drop, on that block, and none while a supply already lies there (M §11). Gems are kept on death.
- **Oddities** (M §11 Regrowth, drops, and returns; §16 Gem caches, Design rules): a block that looks out of place, alone where it does not belong, may hide a gem cache. Each Olympuff god's region has one hand-placed oddity two steps off its road (one rock in a garden, one bush in a wheat field, one tree in a maze). Broken with the capability it needs, which may be any of the five whatever its look, it drops a gem cache worth 5, 7 or 10 gems by ring every time, unless the last cache still lies on its block; it grows back in 10 minutes and can be broken again. A block type alone where it does not belong is often destroyable and worth checking. The agent shows them to the planner (A134).
- **Bushes drop berries, not gems** (M §11, §16, CL). Cutting one is for food (a berry heals 1 on pickup), never for gems; the agent's Gather does not cut them (A81). The knife or any sword cuts one (M §16); Heal cuts a bush within 3 cells when hurt out of combat with no food lying near, and takes the berries, ahead of any potion (A135).
- **Kill drops.** A hostile killed may drop a gem: the zone's grass chance plus a bonus for its type that grows with the hostile's strength. A Snotling in the ring-1 fields drops one 30% of the time, a Chugbug in the hunting ground 40% (M §16, CL). Where grass drops no gem (roads, shores) the bonus alone is the chance.
- **Food and gems on the ground.** Apples, berries and gem piles lie around town, free to pick up (Obs).
- **Authored gem piles on the wire.** In Olympuff town, a pile ready to pick up is three adjacent ground supplies on one row, each with `supply_subtype_code` `gem` and no `gem_price` (Obs: fake fixture observer, map 76, supplies 266–268 at 377–379,377 and 2226–2228 at 359–361,360, ticks ~2578415). No separate `gem_pile` code appeared on entity reads. A grass drop is assumed to use the same code when a gem lands on the ground.

## Compose

- **What it does.** `Compose {supply_ids}` assembles a full set of fragments into their finished supply (M §6).
- **Fragments describe their whole.** A fragment carries `fragment: {composes_into, piece_count, slot, missing_slots}` on entity reads and in your inventory. Holding one piece shows the whole and which pieces are missing, never where they are (API Snapshots, Reads).
- **Rejections:** `not_composable` (permanent), `fragments_missing` (precondition) (M §10.2).
- **Recipes come from fragment catalog metadata** (M §14). The agent knows a recipe exists the moment it holds one piece, and needs no clue to learn its shape.

## NPCs, signs and scrolls

- **Helpers** stay put. `Say` to a helper within 25 blocks gets its one authored line back that tick as a `SpokenTo` with `speaker_kind: npc`; it is the same line every time. The 25-block reach is wider than perception, so the helper answering can be out of sight. Hostiles, bosses, and helpers with no line stay silent, but the `Say` still applies (M §6, §11).
- **A shop's helper knows nothing of stock** (M §11 Shops). Other helpers gave clues, some of them riddles (Obs).
- **Signs.** A sign is a block with `readable: true`. `Read {kind: block}` returns its text on the intent result while it is in sight (M §6). Statues were readable signs too (Obs).
- **Scrolls.** A scroll is a supply. `Read {kind: supply}` works on a carried scroll, or one on the ground in sight. A sign or scroll with no text is `nothing_to_read` (M §10.2); `Read` on a non-scroll supply drew the same (Obs).
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
  - the life is spent and the carried chest drops at the death, where you fell, with everything held, armed or worn, except gems and non-transferable items;
  - `Died` names the dropped chest (`chest_id`, its `map_id`, `x`, `y`) and every supply in it; a death with nothing to stow drops no chest;
  - you respawn at full health in the nearest respawn zone, outside any level, carrying a new, empty starting chest (10): a chest upgrade dies with you;
  - `WithdrawFromChest` with only the `chest_id` takes everything that fits; anyone who reaches the chest first can too;
  - this is all M §11 Death, lives, and ending.
- **Zero lives.** At zero lives the character is ended, permanently: every intent is `character_ended` (M §11).
- **Respawn zones:** Olympuff has five waystations plus a corner of the town plaza (M §11).
- **Dying in a boss room** puts the chest outside the level, within 20 blocks of its perimeter (M §11).
- **Sleep.** `Sleep` needs 10 s with no damage dealt or taken (`recent_damage`) and is refused inside a level (`sleep_not_allowed_in_level`). A character with no intent for 10 minutes falls asleep, except inside a level, where an idle character stands where it stopped (M §11 Sleep, Bosses and levels).
- **Sleeping in town worked** (Obs 1839492): the round trip then carries only `asleep: true` and results.
- **A wake does not refresh `placed`.** The round trip that wakes the character carries `asleep: false`, not `placed`, and a self read after the wake may still say `placed: false`. So the agent infers placement from the wake itself, whether a round-trip body or a self read shows `asleep` going from true to false, and keeps it over a `placed: false` self read until the next position read; otherwise the stale value would hold back that read (A5).
- **`GetSelf` `placed`.** False while the character is off the map, and `GET position` then answers 409 `not_on_map` (API GetPosition). Seen false while dead, with `alive: false`, from `Died` until `Respawned` (A58 run 9: four position reads in that window all drew 409). A sleeping character is off the map too (Waking, below); whether self then says `placed: false` is unmeasured, and the M7 acceptance fake (`FakeSleeper`) assumes it does. Unmeasured: what a new character reports before its first placement, and whether placement emits an event; the scheduler therefore re-reads self every 10 windows while self says alive but not placed (PLAN.md Scheduler).
- **Waking.** A sleeping character is off the map and perceives nothing; `GetSelf` reports whether it is asleep. Any intent from it wakes it on the block it slept on (or the nearest free one) on the tick that intent resolves, and the intent also acts; `Wait` is the one that does nothing else. A full alive cap is `alive_cap_full`, no free block is `block_occupied`, both transient, and the character stays asleep (API Wait and Sleep, GetSelf). The agent's **Sync** sends one `Wait` while `asleep`, then re-reads self.

## Levels and bosses

- **Finding entrances.** `get_minimap` returns `tick`, `map_worn` and `maps`; a map is listed only once one of its tiles is revealed. Each entry carries `map_id`, `map_width`, `map_height`, `entrances` (every level entrance door on that map as `{x, y}`, found or not, with no level number), `cells` (empty without a map worn) and, on a map that belongs to a level, `level`, that level's number (M §5.3 `GET minimap`; API Reads).
- **Which level you are in.** The position read and the snapshot's `position` carry no level: the minimap is the only read that numbers the map you stand on, by matching `position.map_id` to a `maps` entry (M §5.3, §11 Bosses and levels). Olympuff's overworld (800×800) showed 8 marks from town (Obs). The agent reads the current map's level from the minimap (A145): at startup, and after a door step onto a map whose level is unread, at most `MINIMAP_TRIES` (2) reads per map, since a map is listed only once one of its tiles is revealed; after that the map's level stays unknown and nothing more is spent on it.
- **Entering.** An entrance is a door. It may be hidden behind a breakable block (a blast leaves a door where a hidden entrance was), locked (`locked: true`, needing a key, used up on every pass), or out of reach across water (M §5.3, §9.2, §11 Breaking blocks, Bosses and levels).
- **Order.** Levels can be cleared in any order; beating the world takes one clear of every level number, and a second clear of a level counts once. Difficulty and locked doors gate the order (M §11 Bosses and levels). Taking the lowest uncleared number first is the agent's choice (A145), not a game rule.
- **Boss room door:**
  - it admits one character at a time;
  - it rejects `boss_room_occupied` while a fight is on, or while you are already in another boss fight;
  - there is no queue, only the same-block coin flip when it opens
  (M §11).
- **Boss fight** (M §11 Bosses and levels):
  - it is on a time limit set per level: 5 minutes when the level sets none, from 90 s to 10 minutes in Olympuff;
  - no read serves the limit or the time left: count from the tick your step through the door applied;
  - at the limit your health is set to 0, with no roll: an ordinary death that spends a life, drops your chest outside the level within 20 blocks of its perimeter, and respawns you in the nearest respawn zone;
  - before the limit you can walk out through the room's exit door, an ordinary door: the fight ends and the boss door reopens;
  - a boss keeps the damage it has taken until it dies, between challengers too; its entity entry carries `health` and `max_health`.
- **Clear:**
  - the round trip carries a one-shot `level_clear_ceremony {level_number, max_health_gain}`, `max_health_gain` left out when there is none;
  - every clear refills health to your maximum and drops what the boss holds;
  - your **first** clear of a level also raises your maximum health first, by the level's authored gain (0 to 15 per level in Olympuff); a repeat clear only refills;
  - you are moved outside the level that tick;
  - `levels_cleared` updates in the snapshot
  (M §7.2, §11 Bosses and levels; API Round Trip). The agent takes the ceremony as the boss's defeat (A38).
- **Inside a level** you can't `Sleep`, and auto-sleep never takes you (M §11 Bosses and levels).
- **Clearing all `level_count` levels** transcends the character, which ends it (M §11).

## Open questions

Each has a test the agent or a hand session can run. The Manual is silent on every one.

| Question | How to find out |
|---|---|
| Hostile health per type (no NPC's health is served but a boss's; a hostile never heals while it lives, M §11 Hostiles) | Sum the `damage` on our swings' intent results at one NPC until its `NPCDied` |
| Hostile stats (defense, range, speed, attack cooldown) per type | Our swings' `hit` against one type gives its defense; log `Damaged` and spacing for damage and cooldown; log first `Attacked` distance for range |
| What each level's entrance needs | **Investigate (A30)** walks next to each minimap mark (A27) on the current map and records `block_type` and `locked` on its `kb.entrances` row, keyed `"<map_id>:<x>,<y>"` (M §9.2). A look sets `needs` only to `key`, on a locked door (M §9.2, §11); a clue that ties a level to an item or capability is recorded by the planner in its own field, `clue_needs` (A147, `entrance_needs`); `break` and `cross_water` are not inferred, since a block never says whether it breaks (M §11) and an entrance across water shows its door, not the water. Open: what a non-door block on a mark means, and which key a lock takes. Stored in `.state/`, never committed |
| Hunting ground locations and ceilings | `get_zone` on cells around town |
| Each level's boss time limit (90 s to 10 minutes in Olympuff; no read serves it, M §11) | Count from the tick the step through the boss door applied to the death the limit causes, and keep it per level in the knowledge base |
| How to tell a scroll supply from others before reading it. Nothing sourced names scroll subtype codes, so `Investigate` learns them by probing (PLAN.md A56); none is confirmed on the wire yet | Log `supply_subtype_code` of every supply seen; `Read` one of each once and keep the codes that do not answer `nothing_to_read` (A56 does this; note confirmed codes in [`A30_live_play.md`](observations/A30_live_play.md)) |
| Where gems drop from cutting grass. The Manual gives only a rate by ring (Items, slots and gear); the agent measures yield per 16×16-block region from its own cuts, barren after 15 cuts with no gem and poor or good against the map's measured rate, with the Manual's 20% as the prior (PLAN.md A63, A81). No region yield is measured yet | Read `gem_yield` in the world knowledge base after field play |
| Do art or a statue's `facing` mark secrets? The Manual only says art is a picture, behaviour comes from `block_type`, and `facing` tells which way a statue looks (M §9.2) | Log art and facing next to every secret found, and compare |

### Answered by the Manual (A152)

| Question | Answer |
|---|---|
| Is the boss fight clock served, and under what field? | No read serves the limit or the time left; count from the tick the step through the door applied. 5 minutes when the level sets none (M §11 Bosses and levels) |
| Boss fight time limits | Set per level, 90 s to 10 minutes in Olympuff, not served (above; open per level) |
| Weapon cooldown per subtype | The Supplies reference's `attacks` `cooldown_ms`, every tier of a family alike (SR; M §16 Gear and prices; A123). A swing's intent result carries `hit` and `damage` (M §11 Combat) |
| How much damage each worn item saves | Its `protects` amount, worn only, off every hit's damage and onto the hit target (M §11 Combat, §16; SR) |
| How ground food other than the golden cap heals | Eaten on pickup: berry 1, apple 2, mushroom 3, golden cap 6 (M §11 Food and potions; SR) |
| Does any supply raise max health permanently, besides a level's first clear? | No: a first clear is the only thing that raises it (M §11 Bosses and levels) |
| Which `supply_subtype_code` a life (heart) has on the ground | `heart`, class `life`, eaten on pickup (SR) |
| Does `Drop` take a supply stowed in the carried chest (`inventory.chest`)? | Nothing is ever stowed there: `inventory.chest` stays empty today. `Drop` works on any held, armed or worn supply (M §11 Where your supplies are) |
| Is the armed supply also listed in `held`? | No: each supply is in exactly one of `armed`, `worn`, `held` (M §11 Where your supplies are; API Snapshots) |
| Carry capacity with a larger chest | Middle chest 30, red chest 50, once its `Use` on yourself is `applied`; every death resets it to the starting chest (M §11 How big your chest is, Death) |

### Assumed until measured

The win estimate (A9, gated on by **Fight** in A23) uses these until the questions above answer them.

| Assumption | Value | Basis |
|---|---|---|
| Hostile attack interval | 15 ticks | The two weak hostiles in Combat (Obs); the Manual gives each hostile a cooldown but no number |
| Our attack power | 2 | The world's base (M §16); the API serves none, so a permanent gain above it is not counted |
| A hostile's damage a swing | hit chance × (its largest measured hit, gross of armor, less the armor we wear, at least 1): 1.3 for one that hit for 2 with no armor worn, 0.6 with bronze mail. The hit chance is the published roll at that hit as attack power against our worn armor's defense, moved by the type's counted hits and misses (the roll counts as 10 swings) | The published roll (Combat: a hostile's damage number is its attack power). The largest hit seen is only a floor on that number, so a landed hit is priced at it less our armor, never as free, not at the mean below it. A type never measured swings at the world's base attack power, 2, on the same roll; the Manual publishes no hostile's own (A85). Two approximations: the counted hits and misses mix every loadout worn when they landed, so a type swung at often drifts off its rate against today's armor; and a hit is filed gross using the armor worn after the response, so one landed before a swap in that same response is filed off by the armor swapped |
| Our damage a swing | hit chance (65%) × the mean of 1 up to 2 plus weapon damage: 1.625 with the pocket knife, once per the armed weapon's cooldown | The published roll (Combat) and the weapon's damage and cooldown from the Supplies reference (`supplies.weapon_damage`, `supplies.weapon_cooldown_ms`, A123); an unlisted armed item swings as the knife |
| Health of a hostile never hit | 10, less every `NPCDamaged` amount it has taken since, kept until its `NPCDied` (`survival.health_left`, A162) | A new character's health (PLAYABLE_AGENT_PLAN Combat); a hostile never heals while it lives (M §11 Hostiles). A death out of sight is never seen, so the damage is kept for this run only |
| Our health when no observation has served it | 10 | A new character's health (M §11 Bosses and levels: maximum health starts at 10) |
