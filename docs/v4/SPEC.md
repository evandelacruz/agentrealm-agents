# v4 agent: Modal Mona

Status: agreed with Evan, 2026-10-26; Mona is built to it. Mona replaces Lou's reaction layer; she keeps Lou's body, memory, router and game binding (`python/adk3/realm/`).

## Why v4

Lou (v3) priced every choice in gems with a Monte Carlo play-out, then grew commitment flags, decline timers and dominance filters to stop the choices flipping. Each fix was sound alone; together they made her walk on with a snotling hitting her, refuse a potion at 5/10, flee away from town and die on lava. The strategist reasoned well; the layer under it did not do what was agreed.

Mona is built the other way round: a rigid rule system in which flipping cannot happen, because every choice has a defined moment it is made and a defined moment it ends, and nothing in between reopens it. No guards against oscillation; no dice; no pricing.

## Layers

```
 strategist (AI)      the kit for level N; picks from a mode's how-list
 goal decider         one of four modes, always (deterministic)
 subgoal              get health, only (deterministic trigger and exit)
 destination          one at a time, from the mode's (or subgoal's) fixed list
 reflexes             a static priority list, every tick
 router               every move and every attack goes through it
```

## The goal decider

Next level N is the lowest level not yet cleared. The mode is, in order:

1. **Learn what level N needs**, when the kit for N is unknown or not trusted.
2. **Get money**, when her gems do not cover what the kit still costs.
3. **Get gear**, when something on the kit is missing.
4. **Beat level N**, otherwise.

She is in exactly one mode at all times. The decider runs only when the current mode is met, after a death, or when the strategist changes the kit. Between those moments the mode is fixed.

## The kit

The gear the strategist believes level N needs: weapon, armor, the key, bomb or raft its entrance needs, and potions. The strategist owns it and may change it as it learns (a clue, a failed attempt, a death); the goal never changes because of it, only whether it is met. A kit is **trusted** when the strategist says so.

Where a mode is blocked, the block is always about gear (every prerequisite is gear), so the way out is a kit revision:
- **Get gear:** the item is sold nowhere she can find, or costs more than the kit allowed.
- **Beat level N:** a door she cannot open, water she cannot cross, a boss she cannot beat.

Learn and get money cannot block: learn's steps each run once and end with a kit; grass regrows and enemies respawn, so money is always there.

## The modes

**1. Learn** (the agent's task, not the character's: the AI writes the kit). Steps in fixed order, each once: walk town (inside it only, where its signs and helpers are; read and talk come by reflex), scout level N's entrance (open, locked, hidden, across water), attempt level N (go in, see the boss). After each step the strategist updates the kit and says whether it is trusted; a trusted kit ends the mode. The attempt comes only after the first three.

**2. Get money.** Target: the kit's remaining cost, in gems (deterministic). How: the fields her gear can handle (inner band in bronze, outer band in iron and up), known gem piles and caches; kills come along the way. The strategist picks one at each destination moment; she works it until the target is met or it fails (cut out, no route, or poor: under one gem in ten cuts after forty, half what ring 1 pays), and a failed choice is crossed off for this mode. When all are crossed off, the list resets.

**3. Get gear.** Order: weapon, armor, then tools and potions. Each piece from the death chest if it holds it, else a known seller; with none known, search: shops not visited, roads out toward waystations, each once. Buy, then arm or wear at once. Met when everything on the kit is held, worn or armed.

**4. Beat level N.** Route to the entrance; open it with what the kit carries; in, find the boss through doors not yet taken, fight to the death. A death, or the boss clock running out, is a loss: the decider re-runs and the strategist revises the kit; N stays.

## The subgoal: get health

The only subgoal. One at a time; it cannot nest.

- **Enters** when health is 3 or below.
- **Exits** when health is 10 or above. Nothing else ends it.
- While it runs she does not fight (it is the one thing that turns her from a fight), and it owns the destination: at 3 or below, drink a carried potion, else buy one at a known shop if she can pay; otherwise, and always above 3, the nearest food it knows (town's berries, bushes seen), else unexplored ground nearby. A potion heals 10, so above 3 most of it would be wasted. Reflexes keep running.
- When it exits, the goal's destination resumes as it was (she goes back to finish the half-dead enemy: a hostile never heals while it lives).

Both marks are fixed whatever her max health. A safe zone heals nothing (Manual §11 Food and potions); it is never a health destination.

## Destinations

One at a time, from the active mode's (or the subgoal's) fixed list: level N's entrance, town or a shop, a field (inner or outer band), the death chest, a known seller, unexplored ground. Chosen only when the mode starts, when she arrives and its work is done, when it fails (no route at all, the item not there), or after a death. Never mid-trip: a reflex is a detour, and she resumes the same destination after it. A failed destination is crossed off for the rest of the mode.

## Reflexes

Every tick, the first that applies acts:

1. **On fire or lava:** step off.
   - **Always armed** (a standing rule): her best weapon armed and her armor worn. A potion or tool is armed only for its use, then the weapon goes straight back.
2. **A hostile within 10 blocks:** fight it until it is dead. She fights everything, hurt or not; country too rough for her gear teaches the strategist through deaths. Only get health (3 and below) turns her from a fight.
3. **Something to take within 25 blocks, the nearest first:** gems, a heart, free gear, a chest not yet looked into, and, when hurt, food or a berry bush to cut. Food is eaten on pickup, so at full health it is left lying.
4. **A sign or a person within 25 blocks:** read it or talk.
5. **Otherwise:** go to the destination or do its work.

A reflex runs to its end (the thing taken, the enemy dead) and then gives the tick back; get health takes over between ticks whenever its mark is crossed.

## The router

Every move and every attack goes through one router. Travelling, it replans each step on what is known (breaking bushes and trees a carried weapon can, avoiding known fire and lava, learning from refused steps) and reroutes around a fence or a closed gap like a map app; a reroute never changes the destination, which fails only when no route exists. Attacking, it picks the block to strike from: in reach, off safe ground, not across fire or lava.

## The strategist

Asked only at decision points: each learn step, a kit revision (a blocked gear or level mode, a death), and a destination choice from a how-list. Never tick by tick; its answer stands until the next decision point. It reads the Manual (§3, §11, §16) and GAME_NOTES once (cached), and each time her state, every clue, sellers seen, entrances, level attempts and how they ended, deaths, the kit and why it is what it is. It answers only the kit (and trusted or not) or a pick from the list it was given.

## Testing

Each rule has an offline test on a made-up map, and the decision loop runs over scripted scenes (a chaser, low health far from food, a fence on the route, a kit that cannot be bought) before any live run. Live runs are short, to keep model calls down.

## Carried over from Lou

Body (perception and acting), memory, the router (`adk3.realm.travel`), state, actions, the loop's plumbing. Not carried: Lou's engines, opportunities pricing, the danger play-out and the executive.
