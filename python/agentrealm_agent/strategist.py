"""Strategist: the AI planner that owns the goal stack (A35).

On in every live run. ``run --no-planner`` (or ``AGENTREALM_NO_PLANNER=1``)
is a test mode: triggers are drained and logged, and the built-in plan from
``policy.goals`` plus the no-LLM clue rules (A32) stay in charge; it answers
an op's ``no_way_on`` itself by exploring (``plan.builtin_way_on``, A120). With the
planner on and no key, :meth:`Strategist.from_env` raises
``llm.PlannerConfigError`` with one line, and the CLI stops before it plays.

Two threads, one rule: everything that reads or writes the world, the plan,
``Memory`` or the trace runs on the tick thread, in :meth:`Strategist.on_window`.
The background thread only sends a prompt and waits for the reply. The two
threads talk through two queues, a request and an answer, so a slow model
never costs a tick and nothing needs a lock.

One window, on the tick thread:

1. Move new triggers into the inbox: clue, stuck, death, goal done or
   dropped, no way on (A114), idle (from ``Memory``), and map change, hurt, discovery
   (:mod:`.discovery`: a new NPC, entrance, affordable item, hostile pack or
   unread sign, A71, A72) and timer (raised here).
2. If an answer came back, reconcile it with what changed since its prompt
   (:class:`Asked`): an op that finished or was dropped while the call was
   out is not put back, and a ``buy`` counts what was held when it was
   asked for (free-play run 7). Then apply it (see
   :meth:`Strategist._settle`). A new head waits for the next action boundary: it is applied once no queue is
   held, so it never cuts a walk short (A71); the survival reflexes act on
   the world, not the plan, so they still win at once.
3. If nothing is in flight, the inbox has triggers and the budget allows it,
   build the prompt, log it to the trace, and hand it to the background
   thread. A discovery alone waits ``DISCOVERY_GAP_S`` after the last reply.

The timer is a gap, not a rate (A83): ``timer`` is raised once
``replan_s`` has passed since the last call ended (its reply applied,
kept, dropped or failed), so a slow reply never makes the next call follow
it at once. An event still starts a call as soon as none is in flight,
within the budget and any backoff.

The op table in ``plan.py`` (``OP_FIELDS``) is the contract with the model.
When the planner needs something the states cannot do, add an op there and
the state that runs it; never a state that starts itself.

The providers, the call budget, backoff and reply parsing are the ADK's
(``agentrealm_adk.llm``, A140), shared with Lorraine; the prompt, the
triggers and what a reply does to the stack are Gary's, here.

Settings (environment variables, defaults in brackets):

- ``AGENTREALM_PLANNER_PROVIDER`` [anthropic, or openai when only an
  OpenAI key is set]: ``anthropic`` or ``openai``.
- ``AGENTREALM_PLANNER_MODEL`` [claude-sonnet-5-5 for anthropic; required for openai].
- The provider's key: ``AGENTREALM_PLANNER_ANTHROPIC_KEY``, else
  ``ANTHROPIC_API_KEY``; ``AGENTREALM_PLANNER_OPENAI_KEY``, else
  ``OPENAI_API_KEY`` (``llm.KEY_ENV``). The planner's own names come first
  because some hosts strip the standard ones from the environment.
- ``AGENTREALM_PLANNER_EFFORT`` [low]: Anthropic effort level.
- ``AGENTREALM_PLANNER_REPLAN_S`` [15]: with no event, replan this long
  after the last call ended (A83).
- ``AGENTREALM_PLANNER_CALLS_PER_MIN`` [6] and
  ``AGENTREALM_PLANNER_TOKENS_PER_MIN`` [the larger of 40000 and 1.5 times
  the cached prefix]: the budget, over the last 60 seconds of play, every
  attempt counted. Uncached input (cache writes included) plus answer tokens
  as the API reports them, cache reads left out (see below); a call that
  reports none is charged its uncached prompt at 4 characters a token. A
  rolling minute, so a long session never runs dry.
- ``AGENTREALM_PLANNER_HURT_FRACTION`` [0.5]: dropping below this share of
  max health raises ``hurt``.
- ``AGENTREALM_PLANNER_IDLE_MINUTES`` [10]: no applied Step for this long
  raises ``idle``.

- ``AGENTREALM_PLANNER_REFERENCE_SECTIONS`` [core, then the rest under
  about 60k tokens]: which parts of the game reference go in the prompt;
  ``all``, ``core``, or a comma list (see :mod:`.planner_reference`).

The system prompt is the game reference, then the measured facts in
``docs/GAME_NOTES.md``, then the progression stages (:data:`PROGRESSION`),
then the op contract below (:func:`system_prompt`).
It is the same on every call, so it is cached: ``cache_control`` on
Anthropic, a fixed ``prompt_cache_key`` on OpenAI (which caches long
prefixes on its own). Only the user message (triggers, state, stack) changes.
``tokens_per_min`` counts uncached input (cache writes included: Anthropic
``cache_creation_input_tokens``, OpenAI prompt tokens not reported cached)
plus output. Cache reads are not counted: they cost a tenth of input and
``calls_per_min`` bounds them. Unset, the budget is the larger of 40000 and
1.5 times the prefix, so the first call and a cache miss never silence the
planner (:func:`default_tokens_per_min`).

Failures: :meth:`Strategist.check` makes one call before play, and a key
the provider refuses (401 or 403) stops the run with one line
(``llm.PlannerAuthError``). A call that fails mid-run backs off: the next
waits :data:`llm.BACKOFF_BASE_S`, doubling with each failure in a row up to
:data:`llm.BACKOFF_CAP_S`. Acceptance runs count every failure and every
accepted plan (``acceptance.PlannerHealth``).

Another provider is a class with the same ``complete(messages)`` and
``check()`` methods (``llm.LLMClient``), picked in ``llm.make_client``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Mapping

# The planner call plumbing, shared with Lorraine (A140).
from agentrealm_adk.llm import (
    AUTH_STATUSES,
    DEFAULT_CALLS_PER_MIN,
    DEFAULT_EFFORT,
    Backoff,
    CallBudget,
    LLMClient,
    ProviderConfig,
    check_client,
    connect,
    env_number,
    error_status,
    planner_disabled_by_env,
    trace_messages,
)
from agentrealm_adk.llm import default_tokens_per_min as llm_default_tokens_per_min
from agentrealm_adk.llm import parse_reply as llm_parse_reply

from agentrealm_adk.directives import Directives
from agentrealm_adk.travel.resolve import given_up_travel, travel_dest, travel_given_up
from agentrealm_adk.travel.strength import StrengthBracket
from agentrealm_adk.discovery import DISCOVERY_GAP_S, Discoveries, seen_prices
from agentrealm_adk.sellers import codes_with_sellers, refuse_unsold, sellers_line
from agentrealm_adk.level_order import CLEARED, TARGET, UNKNOWN, entrance_level, entrance_rank, level_pending, order_line, refuse_barred
from agentrealm_adk.entrance_needs import CLUE_NEEDS, CLUE_WHY, NEEDS, record_needs, refuse_unmet, unmet_need
from agentrealm_adk.knowledge_base import KnowledgeBase
from agentrealm_adk.memory import Memory
from agentrealm_adk.navigation import entrance_routes
from agentrealm_adk.gem_yield import RATE_MIN_SECONDS, keep_gather_region, summary as gem_yield_summary
from agentrealm_adk.healing import (
    FOOD_REACH,
    POTION_RULE,
    food_near,
    food_to_take,
    held_heal,
    low_health_line,
    potion_count,
    supply_matches,
    walk_seconds,
)
from agentrealm_adk.equip import CUT, damage_per_second, does_job, hunting_ceiling, loadout_strength, weapon_rank
from agentrealm_adk.supplies import (
    BLOCK_FOOD,
    armor_defense,
    chest_applied,
    chest_capacity,
    eaten_on_pickup,
    heal_of,
    is_potion,
    is_weapon,
    row as supply_row,
    sold_codes,
    weapon_damage,
    worn_slot,
    what_it_does,
)
from agentrealm_adk.planner_reference import game_notes_text, reference_text
from agentrealm_adk.loot import carry_slots_used
from agentrealm_adk.oddities import oddities_line
from agentrealm_adk.break_memory import capabilities_for_code
from agentrealm_adk.pack import reserved_supplies
from agentrealm_adk.investigation import HELPER_STILL_TICKS, cell_was_read, greeted_npc_ids, in_sight, spoken_npc_ids
from agentrealm_adk.stalled import StalledOp, below_progress, find as find_stalled, target_of
from agentrealm_adk.survival import (
    HOSTILE_ATTACK_INTERVAL_TICKS,
    STARTING_WEAPON,
    UNKILLED_HOSTILE_HEALTH,
    attack_interval_ticks,
    health_left,
    hostile_swing_damage,
    known_hostile,
    retreat_goal,
    swing_damage,
    worn_defense,
)
from agentrealm_adk.threat import type_key_for_entity
from agentrealm_adk.travel.knowledge import iter_entrances, iter_shop_cells, town_from_kb
from agentrealm_adk.travel.ops import travel_op_from_plan_goal
from agentrealm_adk.world import Pos, WorldModel, chebyshev
from agentrealm_adk.zone_discovery import known_safe

from .plan import (
    OP_FIELDS,
    MAX_WAIT_SECONDS,
    PARAM_MEANINGS,
    PLAN_STALL_SECONDS,
    Plan,
    accepts_plan,
    builtin_way_on,
    collect_rejections,
    parse_plan_payload,
)
from .cells import doors_here, refuse_bad_cells, step_from

log = logging.getLogger(__name__)

INBOX_KEPT = 48  # newest triggers kept while waiting for a call
FINDS_KEPT = 16  # newest finds one discovery trigger carries (A71)
DEFER_MAX_S = 10.0  # a new head waits at most this long for an action boundary (A71)
DEFAULT_REPLAN_S = 15.0
DEFAULT_HURT_FRACTION = 0.5
DEFAULT_IDLE_MINUTES = 10
STALL_SECONDS = 30  # State shows a stall once nothing has changed for this long
MAP_RADIUS = 15  # State map: the cells this far round the character each way (A114)
RULER_STEP = 5  # State map: every this-many-th column's whole x is on the ruler (A148)
# State on_the_ground: the things on the ground listed, nearest first (A148).
# Doors are few, so doors_seen lists every door seen on the map.
GROUND_SHOWN = 8


def _op_table() -> str:
    return "\n".join(f"- {name}: {fields}" for name, fields in OP_FIELDS.items())


def _param_table() -> str:
    return "\n".join(f"- {name}: {meaning}" for name, meaning in PARAM_MEANINGS.items())


SYSTEM_PROMPT = f"""You are the planner for an Agent Realm character. Plan from the game reference above: it is the game's own documentation of its rules, intents, combat, survival, items and maps. Where it and the measured facts disagree, trust the measured facts. You own its goal stack: the states work on the op on top. With an empty stack you are not steering: the dispatcher's safe default runs (exploring in safe ground).

Reply with one JSON object only, no markdown, with these keys:
- "goals": the whole new goal stack, top first. Omit the key to keep the current stack. An empty or invalid list clears it.
- "params": survival params to change, starting from the current values under State; omit it to keep them
- "notes": optional string for the trace
- "entrance_needs": optional list of what a level entrance needs before it is entered, each {{"x", "y", "map_id", "item", "why"}}: a known entrance from level_entrances, the item code or capability (cut, chop, smash, burn, blast, fords) it needs, and the clue that says so; "item": null clears it. When a clue ties a level or its door to an item or a capability ("Sandals get you through" for the lake door), record it here: level_entrances shows it as clue_needs, with clue_needs_why, and need_met (whether what is carried meets it and any needs a look filed, such as a locked door's key)

Each goal is an object with "op" and that op's fields; every op may also carry "why". These are the only ops (anything else is dropped):
{_op_table()}

State lists the current stack, each op marked "pinned" or "planner". Pinned ops come from the directives file (the user's manual steering, or the run's own target). You cannot remove, reorder or replace them: whatever you send, they stay on top, until they are done or stuck detection gives up on their target. Plan around them. Your "goals" are only your own part of the stack, the ops below the pinned ones; leave pinned ops out of it. Never send a travel, of any kind, whose destination is a cell listed under given_up_travel: stuck detection gave up on it. Each give-up lapses at its retry_at_tick, when the cell leaves the list; whether to travel there again is then your call, and a give-up there again backs off twice as long. A stuck trigger says which walk was given up and its retry_at_tick: drop or replace the goal behind it if it keeps getting stuck.

The survival params ("params" under State, set by "params" or a set_param op). Survival params may only tighten past params_floor; a change that loosens one is ignored:
{_param_table()}

Safe ground: hostiles cannot hurt the character only while it stands on safe ground. State safe_ground says whether it does now, and nearest_safe and town say how far away (Chebyshev cells) and which way those are. Away from safe ground, a wait or any op that stays put leaves a hurt character exposed: travel to town or let Retreat walk to the nearest safe tile first. Safe ground only stops damage. Health rises only from potions and food: a potion drunk, or food, eaten on pickup once the character is hurt by at least its heal (food lying near and the berries of a bush near it cuts: State food_near), plus a respawn, which is at full health, and a boss clear, which refills it to the maximum. There is no natural regeneration, on safe ground or anywhere, so resting or waiting restores nothing. An op that leaves health below a bar leaves it there until a potion or food heals it: readiness to_close health_at_bar shows the ways that close it (eat, buy). A potion is for an actual health need: health at the survival floor (low_health), or a fight ahead that needs the health (a boss, a hostile the character means to take on). Never drink one only to meet a readiness bar: health_at_bar already counts the potions held, so a drink leaves it where it was and takes one off potions_at_bar. For a fight ahead, put a use_item with its code directly above the fight, fight_boss or enter_level op it is for (or a gather_gems with fight true); out of combat a use_item of a potion is refused while food near can be had or health is above low_health, and the why names the food (raising low_health makes a potion due sooner, still only once no food near can be had); to eat food lying in sight, fetch_item it; to buy a potion, buy it at a shop.

NPCs: State nearby_npcs lists the nearest NPCs in sight (id, type, cells and dir from here, spoken, greeted, hostile, stays_put); npcs_spoken_to counts the NPCs a say op of yours has spoken to so far, and npcs_greeted those Greet has said hello to. The game does not say which NPCs are helpers and which are monsters. "hostile" true means known hostile: a boss, the last thing that hit the character, or a type that has swung at it, hit it or died in view, in this run or an earlier one; false only means none of its type has done so yet, so it may still be a monster. "stays_put" true means it has stood on one cell for a while, as helpers do, or as a hostile does where the ground stops it reaching the character; judge the rest from its type and the clues. A hostile's "health_left", once it has been hit, is the health it has left: a hostile never heals while it lives, so a hurt one is a shorter fight. On safe ground (anywhere, if policy.hostile does not name NPCs), Greet says hello once to an NPC in sight that stays put and is not hostile, and marks it greeted; a helper answers any words with the same line, which lands in Clues. A greeting never counts as spoken: a say op to a greeted NPC still says your text. To talk to one further away, or with your own words, use a say op with its npc_id (from nearby_npcs) or its npc_type (any NPC of that type, the nearest first); the character walks within speech range and says your text. A helper's reply is added to Clues as a row of kind "npc".

Map: State map draws the ground round the character, {MAP_RADIUS} cells each way and never past the map's edge (its head gives the whole map's x and y range once known), one character per cell: rows by y (the whole number at the left), x by the ruler above (every {RULER_STEP}th column carries its whole x, starting at the | under its first digit), and a legend of the marks used. It shows what the character's reads have shown on this map (? where none has) and what is in view now: use it to see what lies where and between (walls, water, the way round something). Take every x, y you target from a State list that gives it outright, never by counting columns in the drawing: doors_seen (every door on this map, the one under the character included, each with step_from, the ground beside it), level_entrances, nearby_npcs (hostiles among them), signs_seen, on_the_ground (the nearest {GROUND_SHOWN} items, gems, items for sale and chests in view, of= counting all: walk nearer to list one further off), sellers, food_near, oddities, death_chests. A cell that is in none of them (a block to break, ground to walk to) you count from the nearest listed cell or ruler label. An op whose cell is off the current map, or a wall to stand on (a travel or fetch_item), is left out with why (last_reply_rejected). That check covers only the ops that run on the current map: those before the first op that may leave it (enter_level, leave_level, fight_boss, buy, a travel to another map, a town, a hunting ground or a door; inside a level above level_target, where leave_level goes on top by itself, none is checked), judged after every other refusal. Ops after that are not checked; their walk finds out on the map they run on. A block is destroyed only by a Use with an item whose capability opens its type, or, for an oddity, any of the five (held lists each item's use: cut, chop, smash, burn, blast); a break_block names the cell (x, y) and the capability, and is dropped if that cell holds no block, or a block whose type's known openers exclude the capability unless State oddities lists it; a type with no known opener is still tried, so its opener is learned. A destroyed block may grow back. Water (~) is walkable while an item that fords (held shows fords true: a raft) is armed or worn: with one held, every walk plans across water and puts it on before the first water step: one that is worn (water-walking sandals) is worn, so the weapon stays armed and fights on water go as on land; one that goes in the armed slot (a raft) is armed only when no wearable one is held, so get the wearable one where a level is water. On water nothing is armed, worn, removed or dropped that would leave no fording item armed or worn (the server refuses it): no weapon is armed and no potion drunk there unless the fording item is worn, so fight and drink from land; once ashore the best weapon is armed again. No attack can be made from a safe zone, and nothing can hurt the character there. When the ground the character can walk to is all seen and it has nothing to do, it sends a no_way_on trigger and does nothing until you give it a way on. Any op that finds its way shut sends no_way_on too, with why, the op, blocked_by (the hostiles and blocks on the way, each block with whether a held item opens it) and tried (only what this op actually did since it reached the top: how its walk was given up, the search for a way round, then each Use it sent and break recorded on a block on the way, with its result; never an older break), and holds where it is (it never wanders off to explore); so does an op with nothing to work on (no such NPC or item in sight, nothing to gather, a missing item or fragment, a boss op whose conditions are not met): give it another way (a break_block, a fight op on the hostile, an explore_area, a travel elsewhere first) or another goal. A hostile in blocked_by holds the only way: a hostile chases (it steps toward the nearest character and stands still only where no step closes in), so the walk passes its ground by itself only when, chased that way, it never comes within hostile_range and health and the heals carried cover the hits expected while it is near, and lands to fight it where it would first come within range, when that cell is land and the fight from there would be won; it asks only when neither holds: heal, fight it, or route another way. A remembered hostile out of view in blocked_by is ground it held when last seen, not one in sight: a fight op on it is dropped, so route elsewhere, walk near enough to see it again (then fight it if worth it), or explore_area. To fight an NPC (a hostile that blocks the way, one worth its gems), put a fight op on it: the character goes for it whatever its own fight-or-flee judgement says, stepping off safe ground to swing (never at the health floor: the op is dropped then), so weigh nearby_npcs (each one's cell and swing at us, measured or not) against our_swing, health and potions first. A cornered trigger means fleeing found no step that opens distance from the hostiles in range, no heal was due and no hostile was in weapon reach: the character holds where it is, taking any hits that land. Answer it with a way out: a fight op on the hostile, a use_item potion, a travel or explore_area another way, or a wait only on safe ground. A pacing trigger means two of the character's own states (states, such as Fight and Flee) kept taking turns between two cells with nothing coming of it, and benching the losing side (benched) did not settle it, or no side could be benched (benched is null: only the side carrying out the fight-or-flee decision, or Retreat, paced): decide for it, with a fight op on the hostile, a travel or explore_area away, or another goal.

Oddities: State oddities lists the nearest blocks that look out of place, found on the map and remembered: a breakable block alone among another type (one rock in a garden, one bush in a wheat field, one tree in a maze), each with its cell, cells away, block, may_break_with (the held capabilities that may break it, or none held), failed (capabilities that already failed there: never try one again) and last_try with its result and tick. Each god's region hides one oddity two steps off its road; broken with the capability it needs (any of the five, whatever its block, so may_break_with lists every capability held that has not failed there) it drops a gem cache worth 5, 7 or 10 gems by ring every time, unless its last cache still lies on it, far more than grass or trees pay, and it returns 10 minutes later (back_at_tick on one opened); any other oddity may hold something or nothing. Weigh the walk and the tool against that, and break one with a break_block.

Signs: State signs_seen lists the signs and statues seen on this map, the unread ones first and then the nearest, each with its map_id, x, y, block, cells (Chebyshev) and dir from here, and "read" true once its text has been read; unread counts the unread ones on this map. The character does not read a sign on its own. To read one, put a read op with its x, y on the stack: the character walks until the sign is in sight and reads it, and the text lands in Clues as a row of kind "sign". Signs often say where to go or what a place holds, so one near the way is usually worth the few steps.

A travel with no x, y (town, hunting_ground, a nearest shop or entrance) shows in the stack without them, with "goes_to": the cell it walks to now, which the agent works out itself. It is already on the stack: re-sending it changes nothing.

State stall shows how long the character has neither moved, gained or spent gems, gained or lost an item, nor cleared a level, once that passes {STALL_SECONDS} s, and the decision it last made: the stack is not working, so change it. An op on top that makes no progress for {PLAN_STALL_SECONDS} s is moved just below the next op on the stack that can make progress, or dropped when none can (a goal_stalled trigger with the op and the op it is now_below, or goal_failed), and State stalled_ops lists it with why. It never comes back on top when the ops above it end, either: it is moved below again, or dropped. It stays there until a new item is held, a new clue comes in or a new way is known (a door's warp learned, a level entrance): until then a reply that puts it back on top has it moved below again, or left out when nothing in the reply can make progress, and last_reply_rejected says so. So put work that can make progress (gather_gems, say) above it, or give it a reason to work: get the item that opens it, or find another way to it. level_entrances lists the known level entrances nearest first (travel to one with to "entrance", its x, y and map_id), each on this map with its route by what is known of the map and steps, the cells of the route found (an estimate that can run up to 3 times the shortest walk, so compare steps loosely): path_known (known ground reaches it), frontier_only (known ground does not reach it; the way runs through unexplored ground, unexplored of those cells not seen yet, so it may be walled off), known_blocked (what is known walls it off from here: it takes a break or another way in) or not_searched (not checked within this look's search budget; steps is the route through unexplored ground when one was found, else the straight line); other_map for one on another map. shop_prices lists the gem price of every item seen for sale, and which ones the gems held can buy; a buy op takes only the item it names. A buy needs a seller: no_seller_known lists the items the Supplies reference prices for sale (in some world) that no seller is known for, that are not owned, and that are not in sight now (lying, or in a ground chest within reach): one seen earlier and out of view is listed, so go back for it if you know where it lies. A gem price in the Supplies reference is not a seller, nor is a price seen with no place. sellers lists, for each item a seller is known for and each item the stack buys, where it is sold, nearest first with roughly how far (steps, a straight-line estimate; other_map for one on another map): by shop (a shop cell last seen selling it, with its price), clue (a sign, scroll or NPC line with a phrase naming it and a gem price, found at that cell: the seller is searched for within search_radius of it), plan (the x, y and map_id a buy op gives) or unchecked (a shop cell recorded before what it sells was kept: it may sell any item a price was seen for, and is tried only after every other seller). A shop's stock returns two minutes after it is bought, hidden while another item lies on its cell: a shop seen without the item is sold out (sold_out_until_tick, the tick it is back) and stays its seller, walked to only once it is back, and forgotten as its seller only once it was found without the item at several visits in a row, each after its stock was due back; a buy whose every seller is sold out is left out of your reply with the tick it is back. A clue's or a buy's place searched round without it is no seller of it until a clue offering it is found after that search, or a shop is seen selling it. A buy walks only toward a seller of its item, the cheapest walk first, never to a shop that does not sell it. A buy with no seller known is left out of your reply (last_reply_rejected says so). When a clue says where a seller is without being found there (a town, say), give the buy that place as x, y and map_id. A buy of an item with no seller is dropped, and gems saved toward one buy nothing until a seller or the item itself is found. When a goal needs one (a tool to reach a door, say), go and find it: travel to a place a clue or an NPC names, explore_area new ground, ask NPCs with say, read signs; or work on a goal that is reachable now. Do not gather gems toward it.

Held items and purchases: State held lists each item the character carries, with count (held) and stowed (in a carried chest), and what the Supplies reference says it does: its use effects, heal, damage, defense (armor protects only while worn, never in hand; worn, it lowers both how often and how hard a hostile hits), and used_up_on_break (true means each block broken with it uses one up, so one item opens one block; false means it is kept). upgrades_for_sale lists the gear seen for sale that beats the best weapon or armor owned for its slot, cheapest first, with its price, what it gains, gems_short (the gems still needed) and minutes (how long gems_short takes at gather_run gems_per_min, the same as readiness to_close; none before a rate is measured); none seen means no better gear is known for sale anywhere, so there is no upgrade to save gems toward until a shop shows one. Gems are spent once: every buy delays the next upgrade by its price. Before you put a buy on the stack, weigh what it is for against what is already held and what the gems are saving toward. Buy another of an item only when the ones held will not cover what you mean to use them for before the next chance to restock, and the use is worth more than getting the upgrade sooner. A rumor or a clue that an item might be useful is reason to keep one, not to buy more when one is already held. A chest shows as class chest with carry_slots, the pack size it gives, and applied true once the pack has those slots (State pack slots_total). Holding a chest gives nothing: it is applied by a Use on yourself, like a potion, which uses it up. The character applies a bigger chest by itself as soon as one is held; a use_item op names a held potion or chest to use now. An equip arms or wears only (a weapon, armor), so an equip of a potion or chest is dropped. Buying a chest no bigger than the pack has adds nothing. State ops_ended_recently lists the ops that finished (outcome done) or were dropped (outcome dropped, with why) in the last few minutes, newest first, with seconds_ago, even after their goal_done or goal_failed trigger has gone out with an earlier call. A done op is done: leave it off the stack unless you mean to do it again (a second buy buys a second one). A buy is done once its gems are spent and the item is held, or for a chest once the pack has its slots, or for an item eaten on pickup once it is taken. for_sale_eaten_on_pickup lists the items seen for sale that are eaten on pickup, with price and what they do: one applies the moment it is taken and is never held, so a heal eaten on pickup heals at once, and bought at full health it heals nothing and its gems are wasted.

Pack room: State pack gives the carry slots used and the total (held, stowed, worn and armed each take one; gems and lives take none) and reserved, the carried items the stack's ops will use (a use_block's, equip's or use_item's code, a fight_boss's armed and worn, a compose's fragments, a break_block's tool). Making room never drops a reserved item. A dropped item the world prices costs its price again to pick up, wherever it lies, a shop cell no different, so dropping a bought item forfeits what it cost: the choices name what each drop forfeits. The character never picks what to drop on its own: when a buy or fetch_item needs a slot and the pack is full, it drops only what the op names in drop, and an op that names nothing it may drop comes back to you as dropped, with the items it could drop. A bigger carried chest is the other way to make room: upgrades_for_sale lists any chest seen for sale with more slots than the pack has (carry slots now -> with it), so weigh its price against giving an item up. So before a buy into a full pack, weigh what each held item is good for (held: its use, such as attack, cut, smash or burn, and its damage, defense and heal) against the purchase, and name what to give up in drop. An item not used yet is not worthless: a mallet smashes rocks and fights, matches burn.

Death chests: a death drops what the character carried into a chest where it fell, which anyone may empty and which otherwise stays; the starting kit is kept, and the character respawns with the starting chest (10 slots): a bigger chest applied before is lost with the death and must be applied again. State death_chests lists every one of the character's death chests still remembered, nearest first: chest_id, cell with distance and direction (or another map), contents (the item codes last seen in it, unknown until the character has stood next to it) and in_view. To take one back, travel to "point" at its x, y (with its map_id when on another map): once it is in reach the character withdraws all that fits by itself, best items first with a full pack. Weigh the walk and the ground on the way against what is in it.

Weapons and the hunting ground: which weapon is armed follows one rule, by the job at hand. For fighting (an equip with no code), the best weapon held: one that keeps strength within the highest hunting-ground ceiling known beats one that would put it over; among those alike, more damage a second wins (a swing's damage over the weapon's attack cooldown, cooldown_ms under held: a mallet hits harder than a sword of its tier but swings 1.3 s apart, not 1 s); with no ceiling known, damage a second alone decides. For cutting grass (gather_gems), the best held item that cuts, by the same order. So a weapon that would put strength over the ceiling is not armed while a held weapon keeps it within, one armed over it is swapped back, and a weapon that cannot cut (a mallet) is put away while gathering and armed again once the gather work ends. State hunting_strength gives the strength as armed and worn now (attack power 2, plus the armed weapon's damage, plus worn armor's defense) and that ceiling; upgrades_for_sale lists weapons by the fighting rule, with cuts (whether it can cut grass). Weigh a weapon by the work it will do: one that cannot cut adds nothing to gem gathering. To arm or wear an item the rule does not pick (a stronger weapon for a level boss, say), name it: an equip with a code arms or wears exactly that held item, and a later equip with no code applies the rule again. A gather_gems arms by the cutting rule, so it can set aside a named weapon that cuts but would put strength over the ceiling, and does not arm it again afterwards; name it again once the gather work ends.

Potions: {POTION_RULE} State potions counts the potions held and stowed. Plan a reserve (potion_reserve) by that rule: the character will not drink one to top up a scratch. A heal_supplies trigger (hurt, with no food and no potion due) also counts potions: any held are that reserve, kept back until health is low, so weigh food or drinking one now before buying more.

When State shows last_reply_rejected, those parts of your previous reply were dropped or ignored, for the reasons given; the rest of it was applied. Do not repeat them unchanged.

"wait" needs a "why" and at most {MAX_WAIT_SECONDS} seconds. You are asked again on every event and, with none, a few seconds after each reply, so plan the next few steps, not the whole game. A "discovery" trigger lists what the character has just found: an NPC not seen before (kind "npc"), a level entrance (kind "entrance"), an item for sale the gems held can now buy (kind "affordable"), a pack of known-hostile NPCs in a part of the map where none was seen (kind "hostile_pack"), or a sign or statue not read yet (kind "sign"); replan if it changes what is worth doing. A new head op takes over once the action under way ends (within a few seconds), never in the middle of a walk; the survival states still act at once. The agent keeps each target it picks (a grass cell, a frontier, a safe tile, a shop cell, a door) until it gets there or finds it out of reach, and takes a gem, life or (when hurt) food in view before walking on when it is worth the extra steps (more for a cluster of gem piles or a life).

Examples:
{{"goals":[{{"op":"buy","code":"torch","why":"clue mentions darkness"}}],"params":{{"curiosity":0.3}},"notes":"try the cave entrance"}}
{{"goals":[{{"op":"buy","code":"small_potion","why":"potions before the boss"}},{{"op":"equip","why":"wear the new armor"}}]}}
"""


# The stage 2 readiness bar (PROGRESSION, stage 2).
READY_HEALTH_FRACTION = 0.8
READY_POTIONS = 3
# A check's minutes while the gather's rate shows 0 gems a minute (A121).
NO_GEMS_EARNED = "never at gems_per_min 0"

PROGRESSION = f"""# Progression

The character's arc, in order. Judge the stage from State (health, gems, armed, worn, held, lives, map_level, levels_cleared, level_count), the plan and the clues, then pick ops that advance that stage. State readiness shows each stage's readiness check by check, and whether it is met. A stage whose readiness is met is done: move on to the next stage. Stay only for a concrete, reachable reason, and name it in the why: an upgrade listed under upgrades_for_sale whose gems_short the gems will cover soon (its minutes say how soon), say. Saving for gear that is not listed there is not a reason: an item not known to be for sale cannot be bought, however much it would help, so with upgrades_for_sale none seen there is nothing to save toward and a met stage moves on. The same holds for anything a goal needs, at any stage: an item under no_seller_known is no reason to gather gems. A goal that waits on one finds a seller or the item first (travel to a named place, explore, ask NPCs, read signs), or the stage works on a goal reachable now (another level entrance, say). Drop back a stage when its readiness no longer is met (after a death, say), except for a check skipped for its cost (below). The readiness bars are guidance you weigh, not a gate: readiness stage_2 to_close shows, for each unmet check a purchase closes, the gems_short and the minutes it takes at gather_run gems_per_min, this gather's measured rate ("{NO_GEMS_EARNED}" when that rate is 0; no minutes before a rate is measured). health_at_bar counts held heals: it is met when health plus the heal of the potions held reaches the bar (food is eaten on pickup, never held), and readiness stage_2 health_with_held_heals shows that sum. So the potion bar and the health bar trade against each other through held heals: a potion drunk to meet the health bar takes one off potions_at_bar, so drinking to meet one bar breaks the other, and a heal held already counts toward health. For health_at_bar it shows health_short and the ways it closes: eat (the nearest food lying in sight, its heal, where it is and the walk_seconds there) and buy (the cheapest potion seen for sale: its gems_short and minutes, and at, the nearest known shop, with the walk_seconds there; nothing records which shop stocks it, so that shop may not); health_short is what health and held heals together still lack, which no drink closes. There is no rest: health never comes back by itself. With none of these listed, nothing the character has or can see for sale closes it: health stays where it is, so find food or a potion seller, or move on and name it in the why. A bar whose cost in time outweighs what it is worth may be skipped: move on, and name the trade in the why (the minutes a check costs against what it buys, say). A check whose minutes still outweigh what it buys stays skipped: by itself it sends the character back to no stage and calls for no restock; weigh it again when its minutes fall (gems gained, a cheaper seller, a better region). Applying this is yours: the code does not switch stages.

1. Survive and learn. Explore safe ground, read signs, talk to NPCs, map the town (explore_area, travel, read, say). Ready when the town's shop and at least one level entrance are known. To find an entrance when none is known: a travel to "entrance" with no x, y walks to the nearest door seen on this map known to be level_target's entrance, else the nearest whose warp is not known yet, else the nearest door seen on this map, never one known to be a level above level_target, and does nothing while no door has been seen here; so explore_area out past the town toward ground not yet seen, read the signs and ask the NPCs where the levels are, and travel to a door once one shows. A later stage that needs an entrance and knows none comes back here to find one.
2. Build up loot, gear and supplies. Gem hunting is the main work here: cut grass (a gem drops 20% of the time in the ring-1 fields, 25% farther out; field work makes about 6 gems a minute; bushes drop berries, not gems), fell trees, take gem piles and break gem caches. Kills pay gems too: a slain hostile drops one more often than a grass cut on the same ground, more for stronger types, so a weak hostile the character can beat on safe-enough terms (full health, potions held, no pack nearby, safe ground close) is worth fighting rather than walking round, and the hunting ground is a place to do it. Every character swings with attack power 2, a hit 65% of the time, so even the starting knife takes one weak hostile alone (a type's damage and hit rate are measured as it fights the character and kept across runs; a type never measured is priced at attack power 2); still, do not hunt on low health, few lives or against types that have hurt the character badly, and leave the survival states to retreat when a fight turns. To hunt, put a gather_gems with fight true on ground weak hostiles hold, or travel to the hunting ground; its strength ceiling counts attack power 2 plus the armed weapon's damage and worn armor's defense, so the knife or a bronze sword gets in, with bronze mail or without, and a bronze mallet does not. Food is eaten the moment it is taken, even at full health, and never carried: leave it lying until the character is hurt by at least its heal (Heal takes food near by itself then; a fetch_item of food further away is done once it is taken). Then buy potions and gear and equip them (gather_gems, break_block, fetch_item, buy, equip; equip arms the best weapon by the rule under Weapons and the hunting ground, so a weapon that would shut the hunting ground stays unarmed unless named). Ready when health is at least {round(READY_HEALTH_FRACTION * 100)}% of max, at least {READY_POTIONS} potions are held, a weapon better than the starting weapon by the rule under Weapons and the hunting ground is armed, and armor is worn (State readiness stage_2), each weighed against its minutes under to_close. Better gear helps in levels, but once these are met, more gems are worth gathering only toward an upgrade listed under upgrades_for_sale.
3. Beat levels. When geared, enter a level door, solve it, fight its boss (travel, enter_level, break_block, use_block, compose, fight_boss). Levels are taken in order: level_target is the lowest level not cleared, and an entrance's level is learned only by going in, so take an entrance whose order under level_entrances is target first, then unknown ones nearest first, and never a barred one (a level above level_target; an op aimed at one is refused); inside a level above level_target the character leaves at once by itself (leave_level). Pick the entrance by its route and cost under level_entrances, not by distance alone: a path_known entrance beats a nearer one whose way is frontier_only through many unexplored cells, unless its steps are far more, and a known_blocked one needs a break or another way first. A level's needs come from its clues: record each with entrance_needs. An entrance whose need is not met (need_met false) is not entered: an enter_level or a travel to it is left out (last_reply_rejected says why), and one already on the stack is dropped before it steps onto the door; only an enter_level whose item lies in sight is kept, and Level takes the item on the way. Get the item first: buy it from a seller (State sellers), follow a clue to a seller or to where it is made (a clue that it is made somewhere hot points to hot ground: lava or fire on the map, a region the reference or a sign names for fire or a volcano; travel or explore_area there, read the signs and ask the NPCs), or explore for one. Never enter on a weaker substitute because it gets you to the door: a raft that reaches a water level does not meet a need for sandals there. When the best option is an entrance no known path reaches yet, explore toward it (explore_area around the way there) rather than send one long travel into unexplored ground. A boss has no defense, so swings hit it 65% of the time for up to the weapon's damage plus 2; bosses were tuned for a tier's gear before that base, so the stage 2 bar leaves a margin. Restock (stage 2) between levels and whenever health or potions fall below the stage 2 bar, unless the check is one skipped for its cost.
4. Beat the world. Clear every level to transcend: done when levels_cleared holds level_count levels.

Gems by area (stage 2). Gem drops from grass vary by area, and some areas drop none. State gem_yield is measured from the character's own cuts: expected_yield, the gems a cut drops on this map (the manual's rate until enough cuts measure it), the region it stands in (here, once cut there), the best regions at any distance with their yield (gems per cut) and distance in blocks, and the barren ones nearby. A good region far behind is still worth a travel back. Hunt gems where the yield is good, leave a region that shows no gems after a fair sample, and explore regions not yet sampled to sample them.

How gather_gems works. Gather goes for three kinds of place: grass to cut, gem piles to take, and the edge of unexplored ground, where more grass may lie. Each is priced the same way, and the cheapest wins: the walk there (its steps, plus extra steps for ground near a known hostile), then the actions there that get the gems still needed (count less the gems held) at the yield there (a region's measured yield, expected_yield on ground not sampled yet): a cut for each gem a cut is expected to drop; a gem pile is one take for a sure gem, then cuts for the rest; an edge also pays the steps it takes to find grass past it. So near grass is cut first, a good region's grass beats closer grass in a poor one only when its yield outweighs the longer walk, and Gather explores nearby rather than walk far for grass of the same yield. What is in reach is worked first. It works only ground clear of known hostiles: a hostile in view bars cells within weapon reach or hostile_range plus a step (the one that hit us, 6 blocks), and it remembers hostiles out of view at the cell it last saw each on (one that was moving for a minute, one that was blocked for 5 minutes, either forgotten once that cell is seen empty); ground they hold is not gathered and no walk crosses a known hostile's reach, and a hit on the way to a pile shows that pile's ground held. A hostile keeps no post: it goes for the nearest character on its map, however far, stepping toward it, so one that stands still away from the character with no step that would close in is blocked by the ground between (a wall, water), not guarding; one standing still beside the character is swinging at it; it bars only the cells near it, so Gather works further off. A safe-zone block is no hostile's ground: nothing hurts there. Whether to work near one is yours: set fight true on a gather_gems to gather on ground a hostile holds and walk through its reach, taking the fight. It skips barren regions (15 cuts with no gem) and learns ground where cuts have no effect (town grass, say) and leaves it; the survival states keep the character alive while it works. Gather keeps the place it picked until it is reached or gone; it trades it for another only when the walk there grows (a wall found ahead, a long way round) and another is cheaper by a clear margin. Which region is yours too: with no x, y Gather works the ground where the character is, priced as above. A gather_gems x, y names a target region (any block of it, such as a gem_yield corner): Gather goes only for places there (grass, piles and unexplored edges in that region; a pile beside it outside the region is left to pickup), walks toward the region while it knows no cell of it, and works as if no region were named once it knows nothing there to go for or no way reaches it (known hostile ground in between, say: then name another region, or set fight); it also lifts that region's barren mark. Name one to send Gather to a good region (State gem_yield lists the best sampled regions at any distance), and move it once the region turns poor (20 cuts with under half of expected_yield) or barren; never re-send an otherwise unchanged gather_gems just to change x, y. A gather_gems of yours under a pinned gather_gems with no higher count is a repeat of it and is dropped. State gather_status, shown while a gather_gems is on top, is Gather's last decision: "cutting" (a cut sent now), "taking a gem", "walking to grass", "walking to a gem pile", "walking to unexplored ground", "walking to a target region" (it heads for a named region it has not seen), "blocked by hostile" (known grass is barred by a hostile near it and there is nowhere else to go), "cuts have no effect here" (cuts here changed nothing, so it moves on to other cells), "no cuttable cell in view" (it knows nowhere to go, so it explores), or "region barren" (the same, standing in a barren region). While it works only in a target region, the status ends in " (region x,y)", that region's corner. Any of these but "cutting" ends in ", no cut for N s" once no cut has taken effect for 30 s: that is a stall, not progress. State gather_run counts this run's cuts that took effect (cuts), cuts that did nothing (no_effect_cuts) and gems the counter gained (gems_gained): cuts rising with gems_gained flat for long is a stall, not progress. Once the current gather_gems has been on top for {RATE_MIN_SECONDS} s, gather_run also shows its measured rate: gems_per_min, every gem gained while it was on top, over gather_minutes; any other op on top ends that gather, and the rate stays shown until the next gather_gems on top starts measuring afresh."""


def system_prompt(reference_sections: str = "") -> str:
    """The stable prefix: the game reference, the measured facts, then the op contract."""
    parts = []
    reference = reference_text(reference_sections)
    if reference:
        parts.append(reference.strip())
    notes = game_notes_text()
    if notes:
        parts.append(
            "# Measured facts (docs/GAME_NOTES.md)\n\n"
            "What we measured in play. Prefer these over the reference when they disagree.\n\n" + notes.strip()
        )
    parts.append(PROGRESSION)
    parts.append("# Planner contract\n\n" + SYSTEM_PROMPT.strip())
    return "\n\n".join(parts) + "\n"


@dataclass
class StrategistConfig:
    provider: str = ""  # "" means off (the --no-planner test mode)
    model: str = ""
    api_key: str = ""
    effort: str = DEFAULT_EFFORT
    replan_s: float = DEFAULT_REPLAN_S
    calls_per_min: int = DEFAULT_CALLS_PER_MIN
    tokens_per_min: int = 0  # 0: default_tokens_per_min(reference_sections)
    hurt_fraction: float = DEFAULT_HURT_FRACTION
    idle_minutes: float = DEFAULT_IDLE_MINUTES
    reference_sections: str = ""  # "" core then the rest under budget, "all", "core", or a comma list

    @property
    def enabled(self) -> bool:
        return bool(self.provider and self.model and self.api_key)

    @property
    def provider_config(self) -> ProviderConfig:
        return ProviderConfig(self.provider, self.model, self.api_key, self.effort)

    @classmethod
    def from_env(cls) -> StrategistConfig:
        """Read the planner settings. Raises ``llm.PlannerConfigError`` when one is missing."""
        p = ProviderConfig.from_env()
        return cls(
            provider=p.provider,
            model=p.model,
            api_key=p.api_key,
            effort=p.effort,
            replan_s=env_number("AGENTREALM_PLANNER_REPLAN_S", DEFAULT_REPLAN_S),
            calls_per_min=int(env_number("AGENTREALM_PLANNER_CALLS_PER_MIN", DEFAULT_CALLS_PER_MIN)),
            tokens_per_min=int(env_number("AGENTREALM_PLANNER_TOKENS_PER_MIN", 0)),
            hurt_fraction=env_number("AGENTREALM_PLANNER_HURT_FRACTION", DEFAULT_HURT_FRACTION),
            idle_minutes=env_number("AGENTREALM_PLANNER_IDLE_MINUTES", DEFAULT_IDLE_MINUTES),
            reference_sections=os.environ.get("AGENTREALM_PLANNER_REFERENCE_SECTIONS", "").strip(),
        )


def default_tokens_per_min(reference_sections: str = "") -> int:
    """The per-minute token budget when none is set: room for one full prefix write."""
    return llm_default_tokens_per_min(system_prompt(reference_sections))


def drain_triggers(m: Memory) -> list[dict[str, Any]]:
    """Take every queued trigger (clue, stuck, and strategist signals). Tick thread only."""
    out = m.clue_signals + m.nav_stuck.stuck_signals + m.strategist_signals
    m.clue_signals, m.nav_stuck.stuck_signals, m.strategist_signals = [], [], []
    return out


def parse_reply(raw: str) -> Any:
    """The JSON object in a reply (``llm.parse_reply``): of several, the last
    one :func:`parse_plan_payload` accepts (:func:`accepts_plan`)."""
    return llm_parse_reply(raw, accepts_plan)


def same_ops(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> bool:
    """Whether two stacks are the same plan: equal ops, ignoring each op's
    free-text ``why``, so a reworded reason does not restart a ``wait``."""

    return len(a) == len(b) and all(target_of(x) == target_of(y) for x, y in zip(a, b))


def stalled_lines(records: Collection[StalledOp]) -> str:
    """The planner's ``stalled_ops`` (A104): each op that stalled, why, and when."""
    if not records:
        return ""
    rows = [{"op": r.target, "why": r.why, "at_tick": r.tick} for r in records]
    return (
        f"stalled_ops={json.dumps(rows, sort_keys=True)} (ops that made no progress: each stays below an op "
        "that can make progress until a new item is held, a new clue comes in or a new way is known)"
    )


def build_prompt(
    *,
    triggers: list[dict[str, Any]],
    w: WorldModel,
    plan: Plan,
    directives: Directives,
    knowledge: KnowledgeBase | None,
    reference_sections: str = "",
    given_up_travel: Mapping[tuple[int, tuple[int, int]], int] | None = None,
    gather_status: str = "",
    gather_run: dict[str, float | int] | None = None,
    rejected: Collection[str] = (),
    stall: str = "",
    strength: StrengthBracket | None = None,
    stalled_ops: Collection[StalledOp] = (),
    ended_ops: Collection[dict[str, Any]] = (),
    memory: Memory | None = None,
) -> list[dict[str, Any]]:
    """The model's input: the cached system prefix (:func:`system_prompt`), then
    one user message with triggers, state, the remaining plan, every clue, and instructions."""
    pos = f"{w.map_id}:{w.pos[0]},{w.pos[1]}" if w.pos and w.map_id is not None else "unknown"
    state_lines = [
        f"tick={w.tick} pos={pos} alive={w.alive} health={w.health}/{w.max_health} gems={w.gems}",
        f"map_level={w.map_level} armed={w.armed_code} lives={w.lives}",
        f"worn={json.dumps(w.worn_codes, sort_keys=True)}",
        held_line(w, knowledge),
        pack_line(w, plan, knowledge),
        potion_line(w, plan.params),
        *([food_line(w, knowledge, memory)] if memory is not None else []),
        f"levels_cleared={w.levels_cleared} level_count={w.level_count}",
        order_line(w),
        *safety_lines(w, knowledge),
        *npc_lines(w, knowledge),
        *sign_lines(w, knowledge),
        *door_lines(w),
        *ground_lines(w),
        *map_lines(w),
        oddities_line(w, knowledge, plan.tick_hz),
        f"gem_yield={json.dumps(gem_yield_summary(w, knowledge), sort_keys=True)}",
        *_gather_line(plan, gather_status),
        f"gather_run={json.dumps(gather_run or {}, sort_keys=True)}",
        f"stall={stall or 'none'}",
        f"ops_ended_recently={json.dumps(list(ended_ops), sort_keys=True) if ended_ops else 'none'}",
        *entrance_lines(w, knowledge),
        death_chests_line(w),
        shop_price_line(w, knowledge),
        eaten_on_pickup_line(w, knowledge),
        no_seller_line(w, knowledge, plan.ops_left()),
        sellers_line(w, knowledge, plan.ops_left()),
        upgrade_line(w, knowledge, (gather_run or {}).get("gems_per_min")),
        hunting_strength_line(w, knowledge),
        readiness_line(w, knowledge, (gather_run or {}).get("gems_per_min"), memory),
        f"params={json.dumps(plan.params, sort_keys=True)}",
        f"params_floor={json.dumps(directives.params, sort_keys=True)} (survival params may only tighten past these)",
    ]
    if plan.notes:
        state_lines.append(f"plan_notes={plan.notes!r}")
    bracket = strength or StrengthBracket()
    lines = stack_lines(plan, lambda op: travel_dest(op, w, knowledge, bracket, given_up_travel or ()))
    state_lines.append("stack (top first):" + "".join(f"\n  {line}" for line in lines) if lines else "stack: (empty)")
    if given_up_travel:
        rows = [{"cell": f"{mid}:{x},{y}", "retry_at_tick": until} for (mid, (x, y)), until in sorted(given_up_travel.items(), key=str)]
        state_lines.append(f"given_up_travel={json.dumps(rows, sort_keys=True)} (cells stuck detection gave up on: never travel to them before retry_at_tick)")
    stalled = stalled_lines(stalled_ops)
    if stalled:
        state_lines.append(stalled)
    if rejected:
        state_lines.append(f"last_reply_rejected={json.dumps(list(rejected))} (parts of your previous reply that were not applied, and why)")
    clues: list[dict[str, Any]] = []
    if knowledge is not None:
        with knowledge.lock:
            clues = list(knowledge.clues)
    user_parts = [
        "Triggers:\n" + json.dumps(triggers, sort_keys=True),
        "State:\n" + "\n".join(state_lines),
        "Clues (oldest first):\n" + json.dumps(clues, sort_keys=True),
        "Directives instructions:\n" + (directives.instructions or "(none)"),
    ]
    return [
        {"role": "system", "content": system_prompt(reference_sections), "cache": True},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def potion_line(w: WorldModel, params: dict[str, float | int]) -> str:
    """Potions held and stowed, and the health at or below which one is
    drunk out of a fight (``healing.spend_potion``, A96): the ``low_health``
    param's line."""
    line = low_health_line(w, params)
    at = f"health<={line}" if line is not None else "unknown"
    return f"potions={potion_count(w)} potion_drunk_at={at}"


def food_line(w: WorldModel, knowledge: KnowledgeBase | None, memory: Memory) -> str:
    """The food candidates a short walk away (``healing.food_near``, A135):
    food lying near and blocks that drop food (a bush, for berries), only
    food the character is hurt by at least the heal of (A155), each
    with its heal and walk (straight, a rough estimate, as ``to_close``
    gives it). Out of combat Heal goes for the cheapest by its walk, ahead of
    any potion. It reads the run's memory, so a refused take or a given-up
    block is left out as Heal leaves it out; with no memory the line is not
    shown."""
    foods, blocks = food_near(w, memory, knowledge)
    rows = [{"kind": "food", "code": e.code, "pos": e.pos} for e in foods]
    rows += [{"kind": w.view.tiles.get(p), "code": BLOCK_FOOD.get(w.view.tiles.get(p) or ""), "pos": p} for p in blocks]
    shown = [
        {
            "kind": r["kind"],
            "cell": f"{w.map_id}:{r['pos'][0]},{r['pos'][1]}",
            "code": r["code"],
            "heal": heal_of(r["code"]),
            "walk_seconds": walk_seconds(w, r["pos"]),
        }
        for r in rows
    ]
    return (
        f"food_near={json.dumps(shown, sort_keys=True) if shown else 'none'} (within {FOOD_REACH} cells, only food "
        "the character is hurt by at least the heal of: food is eaten on pickup, so it is left lying until then. Out "
        "of combat Heal takes food lying here or cuts a bush for its berries before any potion, so a potion is not "
        "drunk out of combat while food here can be had)"
    )


def safety_lines(w: WorldModel, knowledge: KnowledgeBase | None) -> list[str]:
    """Whether the character stands on safe ground, and how far and which
    way the nearest known safe tile and the town are. ``yes`` once a zone or
    terrain read shows the cell safe; ``unknown`` until a zone read covers a
    cell no terrain read marked safe."""
    if w.pos is None or w.map_id is None:
        return []
    fact = w.zones.get(w.map_id, {}).get(w.pos)
    if known_safe(w, w.map_id, w.pos):
        here = "yes"
    else:
        here = "unknown (zone not read here)" if fact is None else "no"
    safe = retreat_goal(w, None)  # safe tiles only; the town line follows
    town = town_from_kb(knowledge) or (w.respawn_anchors[0] if w.respawn_anchors else None)
    return [
        f"safe_ground={here}",
        f"nearest_safe={_bearing(w, (w.map_id, safe)) if safe is not None else 'none known on this map'}",
        f"town={_bearing(w, town) if town is not None else 'unknown'}",
    ]


# The nearest NPCs in sight that State lists (A65).
NEARBY_NPCS_SHOWN = 5


def npc_lines(w: WorldModel, knowledge: KnowledgeBase | None) -> list[str]:
    """The nearest NPCs in sight, and how many were spoken to and greeted so far (A65).

    Each NPC: ``id``, ``type``, ``cells`` (Chebyshev) and ``dir`` from here,
    ``spoken`` (a ``say`` op's text was said to it, ``spoken_npcs``),
    ``greeted`` (Greet's hello was, ``greeted_npcs``), ``hostile`` (``survival.known_hostile``:
    a boss, our last hitter, or a type that has swung at us, hit us or died in view) and ``stays_put`` (on
    one cell for ``HELPER_STILL_TICKS`` in view, as helpers do). The API names
    no helpers, so these are the only hints. Each also gives its cell (``x``,
    ``y``) and its swing at us as the engagement prices it (A114:
    ``swing``, expected damage per swing through our armor, ``measured``
    when its type has hit us before, and ``health`` the API serves only for
    a boss, else ``health_left`` once it has been hit: a hostile never heals,
    ``survival.health_left``), next to ours (``our_swing``), so the planner
    can judge a fight.
    """
    spoken, greeted = spoken_npc_ids(knowledge), greeted_npc_ids(knowledge)
    ours = {
        "expected_damage": round(swing_damage(w.armed_code), 2),
        "every_ticks": attack_interval_ticks(w.armed_code),
        "hostile_health_assumed": UNKILLED_HOSTILE_HEALTH,
    }
    lines = [f"npcs_spoken_to={len(spoken)} npcs_greeted={len(greeted)} our_swing={json.dumps(ours, sort_keys=True)}"]
    armor = worn_defense(w)
    if w.pos is None or w.map_id is None:
        return lines
    here, map_id = w.pos, w.map_id
    npcs = sorted(
        (e for e in w.entities if e.kind == "npc" and in_sight(w, map_id, here, e.pos)),
        key=lambda e: (chebyshev(e.pos, here), e.id),
    )[:NEARBY_NPCS_SHOWN]
    rows = [
        {
            "id": e.id,
            "type": e.code or None,
            "cells": chebyshev(e.pos, here),
            "dir": compass(here, e.pos) or "here",
            "spoken": e.id in spoken,
            "greeted": e.id in greeted,
            "hostile": known_hostile(w, e),
            "stays_put": w.npc_still_ticks(e) >= HELPER_STILL_TICKS,
            "x": e.pos[0],
            "y": e.pos[1],
            **_their_swing(w, e, armor),
        }
        for e in npcs
    ]
    lines.append(f"nearby_npcs={json.dumps(rows, sort_keys=True)}")
    return lines


def _their_swing(w: WorldModel, e: Any, armor: int) -> dict[str, Any]:
    """An NPC's swing at us as the engagement prices it (``survival.hostile_swing_damage``)."""
    key = type_key_for_entity(e)
    swing = hostile_swing_damage(w.threat.damage_per_hit(key), w.threat, key, armor)
    row: dict[str, Any] = {
        "swing": {"expected_damage": round(swing, 2), "every_ticks": HOSTILE_ATTACK_INTERVAL_TICKS, "measured": w.threat.measured(key)}
    }
    if e.health is not None:
        row["health"] = e.health
    elif e.id in w.npc_damage:
        row["health_left"] = health_left(e, w.npc_damage)
    return row


# The signs and statues State lists.
SIGNS_SHOWN = 5


def sign_lines(w: WorldModel, knowledge: KnowledgeBase | None) -> list[str]:
    """The signs and statues seen on this map (``MapView.readable``), unread first, then nearest.

    Each: ``map_id``, ``x``, ``y``, ``block``, ``cells`` (Chebyshev) and ``dir``
    from here, and ``read`` (the knowledge base records its ``Read``,
    ``cell_was_read``). ``unread`` counts the unread ones on this map, listed or not.
    """
    if w.pos is None or w.map_id is None:
        return []
    here, map_id = w.pos, w.map_id
    cells = [(cell_was_read(knowledge, map_id, p), chebyshev(p, here), p) for p in w.view.readable]
    if not cells:
        return ["signs_seen=none on this map"]
    cells.sort()
    rows = [
        {
            "map_id": map_id,
            "x": p[0],
            "y": p[1],
            "block": w.view.tiles.get(p) or None,
            "cells": dist,
            "dir": compass(here, p) or "here",
            "read": read,
        }
        for read, dist, p in cells[:SIGNS_SHOWN]
    ]
    unread = sum(1 for read, _, _ in cells if not read)
    return [f"signs_seen={json.dumps(rows, sort_keys=True)} unread={unread}"]


# What each block type shows as on the State map; a type not listed shows as OTHER_BLOCK.
MAP_BLOCKS = {
    "dirt": ".",
    "tile": ".",
    "grass": ",",
    "fire": "!",
    "lava": "!",
    "water": "~",
    "bush": "b",
    "tree": "T",
    "rock": "o",
    "mountain": "M",
    "wall": "#",
    "framed_door": "D",
    "rock_entry": "D",
}
OTHER_BLOCK = "X"
# The legend, in the order it is printed; only the marks on the map are listed.
MAP_LEGEND = {
    "@": "you",
    "H": "known hostile",
    "B": "boss",
    "N": "other NPC",
    "C": "other character",
    "$": "gem",
    "i": "item on the ground",
    "+": "item for sale",
    "c": "chest",
    "S": "sign or statue (readable)",
    "L": "locked door",
    "D": "door",
    ".": "ground",
    ",": "grass",
    "!": "fire or lava (hurts)",
    "~": "water",
    "b": "bush",
    "T": "tree (hedges are trees)",
    "o": "rock",
    "M": "mountain",
    "#": "wall",
    OTHER_BLOCK: "other block",
    "_": "no ground",
    "?": "not seen",
}


def _entity_mark(w: WorldModel, e: Any) -> str:
    if e.kind == "npc":
        return "B" if e.is_boss else "H" if known_hostile(w, e) else "N"
    if e.kind == "character":
        return "C"
    if e.kind == "chest":
        return "c"
    if e.gem_price is not None:
        return "+"
    return "$" if e.code == "gem" else "i"


def ruler_lines(xs: range) -> list[str]:
    """The State map's x ruler (A148): every ``RULER_STEP``-th column carries
    its whole x, written from that column rightward, with a ``|`` under its
    first digit, so a column's x is read off whole, never pieced together.
    A label that would run into the one before is left out, never cut short:
    a partial number is the very misreading the ruler is there to stop. The
    last label may run past the right edge."""
    labels = [" "] * len(xs)
    ticks = [" "] * len(xs)
    for i, x in enumerate(xs):
        text = str(x)
        if x % RULER_STEP or (i and labels[i - 1] != " "):
            continue
        labels[i : i + len(text)] = text  # the last may run past the edge
        ticks[i] = "|"
    return ["x".rjust(5) + " " + "".join(labels).rstrip(), " " * 6 + "".join(ticks).rstrip()]


def map_lines(w: WorldModel) -> list[str]:
    """The ground round the character as one character per cell (A114):
    ``MAP_RADIUS`` cells each way, cut at the map's edge once a terrain read
    has given its size, rows by y top to bottom with each row's whole y at
    its left, a ruler of x above (:func:`ruler_lines`), and a legend of the
    marks used. Terrain is what this map's reads have shown, ``?`` where
    none has; entities in view are drawn over it, and the character over
    everything. So the planner sees what lies where and between; the cells
    it targets it takes from State's lists, which give x, y outright (A148)."""
    if w.pos is None or w.map_id is None:
        return []
    cx, cy = w.pos
    size = w.map_size
    x_lo, x_hi = cx - MAP_RADIUS, cx + MAP_RADIUS
    y_lo, y_hi = cy - MAP_RADIUS, cy + MAP_RADIUS
    if size is not None:
        x_lo, x_hi = max(x_lo, 0), min(x_hi, size[0] - 1)
        y_lo, y_hi = max(y_lo, 0), min(y_hi, size[1] - 1)
    xs, ys = range(x_lo, x_hi + 1), range(y_lo, y_hi + 1)
    marks: dict[Pos, str] = {}
    for e in w.entities:
        if e.pos not in marks or e.kind == "npc":  # an NPC shows over an item on its cell
            marks[e.pos] = _entity_mark(w, e)
    marks[w.pos] = "@"
    used: set[str] = set()

    def cell(p: Pos) -> str:
        if p in marks:
            return marks[p]
        block = w.view.tiles.get(p)
        if block is None:
            return "?"
        if w.view.readable.get(p):
            return "S"
        if not block:
            return "_"
        mark = MAP_BLOCKS.get(block, OTHER_BLOCK)
        return "L" if mark == "D" and w.view.locked.get(p) else mark

    rows = []
    for y in ys:
        row = "".join(cell((x, y)) for x in xs)
        used.update(row)
        rows.append(f"{y:>5} {row}")
    legend = ", ".join(f"{mark} {what}" for mark, what in MAP_LEGEND.items() if mark in used)
    whole = f", the whole map is x 0..{size[0] - 1}, y 0..{size[1] - 1}" if size is not None else ""
    head = (
        f"map (map {w.map_id}, x {xs[0]}..{xs[-1]} left to right, y {ys[0]}..{ys[-1]} top to bottom{whole}, "
        f"one character per cell; legend: {legend}):"
    )
    return [head, *ruler_lines(xs), *rows]


def door_lines(w: WorldModel) -> list[str]:
    """Every door seen on this map, nearest first (A148): each one's ``x``,
    ``y``, ``block``, ``locked``, ``here`` (the character stands on it, as
    on a level's door on arrival), ``cells`` and ``dir`` from here, and
    ``step_from``, the ground beside it a walk steps onto it from. A level
    entrance on this map is a door here too; ``level_entrances`` names
    which."""
    if w.pos is None or w.map_id is None:
        return []
    here = w.pos
    doors = sorted(doors_here(w), key=lambda p: (chebyshev(p, here), p))
    if not doors:
        return ["doors_seen=none on this map"]
    rows = [
        {
            "x": p[0],
            "y": p[1],
            "block": w.view.tiles.get(p),
            "locked": bool(w.view.locked.get(p)),
            "here": p == here,
            "cells": chebyshev(p, here),
            "dir": compass(here, p) or "here",
            "step_from": [{"x": s[0], "y": s[1]} for s in step_from(w, p)],
        }
        for p in doors
    ]
    return [f"doors_seen={json.dumps(rows, sort_keys=True)}"]


def ground_lines(w: WorldModel) -> list[str]:
    """What lies on the ground in view, the nearest ``GROUND_SHOWN`` (A148):
    items, gems, items for sale (``gem_price``) and chests, each with its
    ``x``, ``y``, ``cells`` and ``dir`` from here; ``of`` counts all in view."""
    if w.pos is None or w.map_id is None:
        return []
    here = w.pos
    things = sorted((e for e in w.entities if e.kind in ("supply", "chest")), key=lambda e: (chebyshev(e.pos, here), e.id))
    if not things:
        return ["on_the_ground=none in view"]
    rows = []
    for e in things[:GROUND_SHOWN]:
        row: dict[str, Any] = {
            "kind": e.kind,
            "id": e.id,
            "x": e.pos[0],
            "y": e.pos[1],
            "cells": chebyshev(e.pos, here),
            "dir": compass(here, e.pos) or "here",
        }
        if e.code:
            row["code"] = e.code
        if e.gem_price is not None:
            row["gem_price"] = e.gem_price
        rows.append(row)
    return [f"on_the_ground={json.dumps(rows, sort_keys=True)} of={len(things)}"]


def _bearing(w: WorldModel, where: tuple[int, Pos]) -> str:
    """``map:x,y (N cells <direction>)``, or ``here``; another map gives no distance."""
    map_id, pos = where
    cell = f"{map_id}:{pos[0]},{pos[1]}"
    if map_id != w.map_id or w.pos is None:
        return f"{cell} (another map)"
    if pos == w.pos:
        return f"{cell} (here)"
    return f"{cell} ({chebyshev(w.pos, pos)} cells {compass(w.pos, pos)})"


def compass(a: Pos, b: Pos) -> str:
    """Which way ``b`` lies from ``a``, eight-way; y grows southward."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    # A component under half the other counts as none: (10, 3) is east.
    ns = "north" if dy < 0 else "south" if dy > 0 else ""
    ew = "west" if dx < 0 else "east" if dx > 0 else ""
    if abs(dy) * 2 < abs(dx):
        ns = ""
    if abs(dx) * 2 < abs(dy):
        ew = ""
    return f"{ns}-{ew}" if ns and ew else ns or ew


def _gather_line(plan: Plan, gather_status: str) -> list[str]:
    """``gather_status``, while a ``gather_gems`` is on top and Gather has decided once."""
    head = plan.current()
    if head is None or head["op"] != "gather_gems" or not gather_status:
        return []
    return [f"gather_status={json.dumps(gather_status)}"]


def repeats_pinned(op: dict[str, Any], pinned: list[dict[str, Any]]) -> bool:
    """``op`` adds nothing below the pinned ops: it is one of them (``why``
    aside), or a ``gather_gems`` with no higher count than a pinned one. That
    one is done on reaching the top (gems at its count), so its x, y never act."""
    for p in pinned:
        if same_ops([op], [p]):
            return True
        if op["op"] == p["op"] == "gather_gems" and op["count"] <= p["count"]:
            return True
    return False


def stack_lines(plan: Plan, goes_to: Callable[[dict[str, Any]], tuple[int, Pos] | None] | None = None) -> list[str]:
    """The ops left on the stack, top first, each marked ``pinned`` (a
    directives op, which the planner cannot remove) or ``planner`` (A35).
    Each op as :func:`shown_op` shows it, with ``goes_to`` for a symbolic travel."""
    return [
        f"{'pinned' if i < plan.directive_end else 'planner'} {json.dumps(shown_op(op, goes_to), sort_keys=True)}"
        for i, op in enumerate(plan.goals[plan.index :], plan.index)
    ]


def shown_op(op: dict[str, Any], goes_to: Callable[[dict[str, Any]], tuple[int, Pos] | None] | None = None) -> dict[str, Any]:
    """``op`` as State shows it. A travel the agent finds the cell for
    (``travel_op_from_plan_goal`` reads no x, y) drops the ``0, 0`` placeholder
    validation gave it, which reads like the map origin and invited re-sends
    (free-play run 1), and says where it ``goes_to`` now instead."""
    if op.get("op") != "travel" or travel_op_from_plan_goal(op).x is not None:
        return op
    shown = {k: v for k, v in op.items() if k not in ("x", "y")}
    dest = goes_to(op) if goes_to is not None else None
    if dest is not None:
        shown["goes_to"] = f"{dest[0]}:{dest[1][0]},{dest[1][1]}"
    else:
        shown["goes_to"] = "nearest unexplored door" if op["to"] == "entrance" else "not known yet"
    return shown


# Each entrance's place in the order levels are taken (A145).
ENTRANCE_ORDER = {TARGET: "target", UNKNOWN: "unknown", CLEARED: "cleared"}
# Level entrances State lists, nearest first.
ENTRANCES_SHOWN = 8


def entrance_lines(w: WorldModel, knowledge: KnowledgeBase | None) -> list[str]:
    """The known level entrances (``kb.entrances``), nearest first: cell,
    bearing, what a look at it filed (``block_type``, ``locked``, ``needs``),
    a need a clue tied to it (``clue_needs`` with the planner's
    ``clue_needs_why``, A147) and whether what is carried meets every need
    (``need_met``),
    and for one on this map whether a walk reaches it by what is known of
    the map (A107, ``navigation.entrance_routes``): ``route`` path_known,
    frontier_only (``unexplored`` cells of it not seen yet), known_blocked
    or not_searched, with ``steps``. One on another map shows ``route``
    other_map. So the planner picks by reachability and cost, not distance alone."""
    rows = []
    for map_id, pos, row in iter_entrances(knowledge):
        entry: dict[str, Any] = {"cell": _bearing(w, (map_id, pos))}
        for key in ("block_type", "locked", NEEDS, CLUE_NEEDS, CLUE_WHY):
            if row.get(key) is not None:
                entry[key] = row[key]
        if (level := entrance_level(row)) is not None:
            entry["level"] = level
        rank = entrance_rank(row, w)
        entry["order"] = ENTRANCE_ORDER[rank] if rank is not None else "barred"
        if row.get(NEEDS) is not None or row.get(CLUE_NEEDS) is not None:
            entry["need_met"] = unmet_need(w, knowledge, map_id, pos) is None
        here = w.map_id == map_id and w.pos is not None
        rows.append(((0 if here else 1, chebyshev(w.pos, pos) if here else 0, map_id, pos), entry))
    if not rows:
        return ["level_entrances=none known"]
    rows.sort(key=lambda t: t[0])
    count, rows = len(rows), rows[:ENTRANCES_SHOWN]
    on_map = [key[3] for key, _ in rows if key[0] == 0]
    routes = entrance_routes(w, on_map) if on_map else {}
    for key, entry in rows:
        route = routes.get(key[3]) if key[0] == 0 else None
        if route is None:
            entry["route"] = "other_map"
            continue
        entry.update({"route": route.kind, "steps": route.steps})
        if route.unexplored:
            entry["unexplored"] = route.unexplored
    shown = [entry for _, entry in rows]
    more = f" (+{count - len(shown)} more)" if count > len(shown) else ""
    return [f"level_entrances={json.dumps(shown, sort_keys=True)}{more}"]


# Death chests State lists, nearest first.
DEATH_CHESTS_SHOWN = 8


def death_chests_line(w: WorldModel) -> str:
    """Our remembered death chests (A122), nearest first, at most
    ``DEATH_CHESTS_SHOWN`` and a count of the rest: cell and bearing,
    the item codes last seen in it (``unknown`` until seen in reach), and
    ``in_view`` while its cell is in sight. So the planner weighs going back
    for one (a ``travel`` to its cell; Recover takes back what fits)."""
    rows = []
    for chest_id, c in w.death_chests.items():
        here = w.map_id == c.map_id and w.pos is not None
        d = chebyshev(w.pos, c.pos) if here else 0
        entry = {
            "chest_id": chest_id,
            "cell": _bearing(w, (c.map_id, c.pos)),
            "contents": c.contents if c.contents is not None else "unknown",
            "in_view": here and d <= w.perception,
        }
        rows.append(((0 if here else 1, d, c.map_id, chest_id), entry))
    if not rows:
        return "death_chests=none known"
    shown = [e for _, e in sorted(rows, key=lambda t: t[0])[:DEATH_CHESTS_SHOWN]]
    more = f" (+{len(rows) - len(shown)} more)" if len(rows) > len(shown) else ""
    return f"death_chests={json.dumps(shown, sort_keys=True)}{more}"


def shop_price_line(w: WorldModel, knowledge: KnowledgeBase | None) -> str:
    """Every item seen for sale (``items`` rows with a ``gem_price``, and
    priced supplies in sight), cheapest first, against the gems held."""
    prices = seen_prices(w, knowledge)
    if not prices:
        return "shop_prices=none seen"
    ordered = dict(sorted(prices.items(), key=lambda t: (t[1], t[0])))
    gems = w.gems or 0
    affordable = [code for code, price in ordered.items() if price <= gems]
    return f"shop_prices={json.dumps(ordered)} gems={gems} can_buy_now={json.dumps(affordable)}"


def eaten_on_pickup_line(w: WorldModel, knowledge: KnowledgeBase | None) -> str:
    """The items seen for sale that are eaten on pickup (A112), with their
    price and what the Supplies reference says they do (a heal): bought, one
    applies at once and is never held, so it is worth only what it does now."""
    rows = {
        code: {"price": price, **what_it_does(code)}
        for code, price in sorted(seen_prices(w, knowledge).items())
        if eaten_on_pickup(code)
    }
    return f"for_sale_eaten_on_pickup={json.dumps(rows, sort_keys=True)}" if rows else "for_sale_eaten_on_pickup=none"


def no_seller_line(w: WorldModel, knowledge: KnowledgeBase | None, stack: Collection[dict[str, Any]] = ()) -> str:
    """The items a shop sells in some world (``supplies.sold_codes``) that
    cannot be bought from what is known (A110): no seller of one is known
    (``sellers.sellers_of``, A131: a price seen with no place is no seller),
    none lies in sight or in a ground chest in reach, and none is owned. A goal that needs one has to find a seller or the item
    first: gems saved for it buy nothing. Items never sold (gems, lives, the
    starting kit) are left out: there is no seller to find."""
    owned = {s.code for s in w.held_supplies + w.chest_supplies} | {w.armed_code, *w.worn_codes.values()}
    found = {e.code for e in w.entities if e.kind == "supply"}
    found |= {s.code for contents in w.chest_contents.values() for s in contents}
    unknown = [c for c in sold_codes() if c not in owned | found]
    sold = codes_with_sellers(w, knowledge, unknown, stack)
    unknown = [c for c in unknown if c not in sold]
    return f"no_seller_known={json.dumps(unknown)}" if unknown else "no_seller_known=none"


def held_line(w: WorldModel, knowledge: KnowledgeBase | None = None) -> str:
    """Each item carried (A92): how many are held, how many stowed in a
    carried chest, what it does by the Supplies reference
    (:func:`supplies.what_it_does`: ``use`` lists attack, cut, smash, burn…),
    plus ``breaks`` for the capabilities learned in play that the reference
    does not list (A102), and a chest as a chest (A106): the ``carry_slots``
    it gives and whether the pack has them (``applied``), so a purchase, or a
    drop to make room for one, can be weighed against them."""
    held = Counter(s.code for s in w.held_supplies if s.code)
    stowed = Counter(s.code for s in w.chest_supplies if s.code)
    rows: dict[str, dict[str, Any]] = {}
    for code in sorted(held.keys() | stowed.keys()):
        entry: dict[str, Any] = {"count": held[code]}
        if stowed[code]:
            entry["stowed"] = stowed[code]
        entry.update(what_it_does(code))
        slots = entry.pop("chest_capacity", None)
        if slots is not None:
            # A chest shows the pack's size (A106): carry_slots it gives, applied once
            # its Use on yourself raised the pack to them (A108, State pack slots_total).
            entry.update({"class": "chest", "carry_slots": slots, "applied": chest_applied(code, w.carry_capacity)})
        breaks = capabilities_for_code(code, knowledge) - set(entry.get("use", []))
        if breaks:
            entry["breaks"] = sorted(breaks)
        rows[code] = entry
    return f"held={json.dumps(rows, sort_keys=True)}"


def pack_line(w: WorldModel, plan: Plan, knowledge: KnowledgeBase | None) -> str:
    """Carry slots used and the total (A102), and the carried items the stack's
    ops will use, by code (``pack.reserved_supplies``): making room never drops
    those, so a pickup into a full pack is weighed against the rest."""
    reserved = Counter(s.code for s in reserved_supplies(w, plan.ops_left(), knowledge) if s.code)
    row = {"slots_used": carry_slots_used(w), "slots_total": w.carry_capacity, "reserved": dict(sorted(reserved.items()))}
    return f"pack={json.dumps(row, sort_keys=True)}"


# The gear upgrades for sale State lists, cheapest first.
UPGRADES_SHOWN = 3


def upgrade_line(w: WorldModel, knowledge: KnowledgeBase | None, gems_per_min: float | None = None) -> str:
    """The gear seen for sale that beats the best owned for its slot, cheapest
    first, with its price, the gems still short (A92) and the minutes those
    take at the current gather's gems a minute (A124, :func:`minutes_at`). Owned is armed, worn,
    held or stowed; a weapon must rank above every owned one by Equip's own
    best-weapon rule (``equip.weapon_rank``, A99: keeping within the highest
    hunting-ground ceiling known first, then damage a second,
    ``equip.damage_per_second``) and says whether it
    cuts grass (``cuts``, Gather's job), armor have more
    ``defense`` than any owned for its slot, and a chest a larger
    ``chest_capacity`` than the slots carried now (A102: room for a full
    pack), all by the Supplies reference. An item
    already owned is not listed. The first is what gems save toward."""
    carried = [s.code for s in w.held_supplies + w.chest_supplies]
    owned = {c for c in [*carried, w.armed_code, *w.worn_codes.values()] if c}
    ceiling = hunting_ceiling(w, knowledge)
    best_weapon = max((c for c in owned if is_weapon(c)), key=lambda c: weapon_rank(c, w, ceiling), default=None)
    best_rank = weapon_rank(best_weapon, w, ceiling)  # no weapon owned: bare hands, as Equip counts them
    best_damage = weapon_damage(best_weapon) or 0
    best_dps = damage_per_second(best_weapon)

    def best_defense(slot: str) -> int:
        return max((armor_defense(c) or 0 for c in owned if worn_slot(c) == slot), default=0)

    gems = w.gems or 0
    rows = []
    for code, price in seen_prices(w, knowledge).items():
        if code in owned:
            continue
        damage, defense, slot = weapon_damage(code), armor_defense(code), worn_slot(code)
        rank = weapon_rank(code, w, ceiling)
        extra: dict[str, Any] = {}
        if damage is not None and is_weapon(code) and rank > best_rank:
            gain = f"damage {best_damage} -> {damage}, a second {best_dps:.2f} -> {damage_per_second(code):.2f}"
            if rank[0] and not best_rank[0]:
                gain += ", back within the hunting ceiling"
            extra = {"cuts": does_job(code, CUT, knowledge)}
        elif defense is not None and slot and defense > best_defense(slot):
            gain = f"{slot} defense {best_defense(slot)} -> {defense}"
        elif (capacity := chest_capacity(code)) is not None and capacity > w.carry_capacity:
            gain = f"carry slots {w.carry_capacity} -> {capacity}"
        else:
            continue
        short = max(0, price - gems)
        minutes = minutes_at(short, gems_per_min)
        if minutes is not None:
            extra["minutes"] = minutes
        rows.append({"code": code, "price": price, "gains": gain, "gems_short": short, **extra})
    if not rows:
        return "upgrades_for_sale=none seen"
    rows.sort(key=lambda e: (e["price"], e["code"]))
    return f"upgrades_for_sale={json.dumps(rows[:UPGRADES_SHOWN], sort_keys=True)}"


def hunting_strength_line(w: WorldModel, knowledge: KnowledgeBase | None) -> str:
    """Strength as armed and worn now against the highest hunting-ground
    ceiling known (A99), the numbers Equip's best-weapon rule weighs."""
    ceiling = hunting_ceiling(w, knowledge)
    row = {"strength": loadout_strength(w, w.armed_code if is_weapon(w.armed_code) else None), "ceiling": ceiling}
    return f"hunting_strength={json.dumps(row, sort_keys=True)}"


def readiness(
    w: WorldModel, knowledge: KnowledgeBase | None, gems_per_min: float | None = None, memory: Memory | None = None
) -> dict[str, dict[str, Any]]:
    """Each stage's readiness check by check, and whether it is met
    (PROGRESSION). Stage 2's bar is ``READY_HEALTH_FRACTION`` of max health,
    ``READY_POTIONS`` potions, a weapon better than the starting one by the
    one weapon rule (``equip.weapon_rank``, A99) armed, and armor worn. The
    planner reads it to judge when a stage is done; the code switches no
    stage itself. Stage 2's ``to_close`` prices each unmet check (A121, A124,
    :func:`cost_to_close`). Health counts the heals carried (A132): health
    plus the heal of the potions held (food is never held, A155) must reach
    the bar, and
    ``health_with_held_heals`` shows that sum, so no potion is drunk only to
    meet the health bar, which would break the potion bar."""
    shop_known = bool(iter_shop_cells(knowledge) or seen_prices(w, knowledge))
    entrance_known = bool(iter_entrances(knowledge))
    stage_1 = {"shop_known": shop_known, "entrance_known": entrance_known}
    health_with_heals = (w.health or 0) + held_heal(w)
    health_ok = bool(w.max_health) and health_with_heals >= READY_HEALTH_FRACTION * w.max_health
    ceiling = hunting_ceiling(w, knowledge)
    armed = w.armed_code if is_weapon(w.armed_code) else None
    better_weapon = armed is not None and weapon_rank(armed, w, ceiling) > weapon_rank(STARTING_WEAPON, w, ceiling)
    armor_worn = any((armor_defense(c) or 0) > 0 for c in w.worn_codes.values())
    stage_2 = {
        "health_at_bar": health_ok,
        "potions_at_bar": potion_count(w) >= READY_POTIONS,
        "better_weapon_armed": better_weapon,
        "armor_worn": armor_worn,
    }
    row_2: dict[str, Any] = {**stage_2, "met": all(stage_2.values()), "health_with_held_heals": health_with_heals}
    unmet = [check for check, ok in stage_2.items() if not ok]
    to_close = cost_to_close(w, knowledge, unmet, gems_per_min, memory)
    if to_close:
        row_2["to_close"] = to_close
    return {
        "stage_1": {**stage_1, "met": all(stage_1.values())},
        "stage_2": row_2,
    }


def cost_to_close(
    w: WorldModel,
    knowledge: KnowledgeBase | None,
    unmet: Collection[str],
    gems_per_min: float | None,
    memory: Memory | None = None,
) -> dict[str, dict[str, Any]]:
    """What each unmet stage 2 check costs (A121): for a check a purchase
    closes, the gems still short of the cheapest seen for sale that closes it
    (none for gear carried, which an equip closes), each alone, and the
    minutes that takes (:func:`minutes_at`). A check with nothing seen for
    sale that closes it is left out. Health is always shown, with the ways
    it closes (A124, :func:`health_to_close`)."""
    prices = seen_prices(w, knowledge)
    # Gear already carried closes its check with an equip: no gems.
    carried = {s.code for s in w.held_supplies + w.chest_supplies if s.code}
    gear = {**prices, **{code: 0 for code in carried}}
    ceiling = hunting_ceiling(w, knowledge)
    starting = weapon_rank(STARTING_WEAPON, w, ceiling)
    closes: dict[str, tuple[int, int]] = {}  # check: (cheapest price, how many)
    potion = min((p for c, p in prices.items() if is_potion(c)), default=None)
    if "potions_at_bar" in unmet and potion is not None:
        closes["potions_at_bar"] = (potion, READY_POTIONS - potion_count(w))
    weapon = min((p for c, p in gear.items() if is_weapon(c) and weapon_rank(c, w, ceiling) > starting), default=None)
    if "better_weapon_armed" in unmet and weapon is not None:
        closes["better_weapon_armed"] = (weapon, 1)
    armor = min((p for c, p in gear.items() if (armor_defense(c) or 0) > 0), default=None)
    if "armor_worn" in unmet and armor is not None:
        closes["armor_worn"] = (armor, 1)
    rows: dict[str, dict[str, Any]] = {}
    if "health_at_bar" in unmet and w.max_health:
        rows["health_at_bar"] = health_to_close(w, knowledge, prices, gems_per_min, memory)
    for check, (price, count) in closes.items():
        short = max(0, price * count - (w.gems or 0))
        row: dict[str, Any] = {"gems_short": short}
        minutes = minutes_at(short, gems_per_min)
        if minutes is not None:
            row["minutes"] = minutes
        rows[check] = row
    return rows


def minutes_at(gems_short: int, gems_per_min: float | None) -> float | str | None:
    """The minutes ``gems_short`` takes at the current gather's gems a minute
    (A121, A124): None with no rate measured, and ``NO_GEMS_EARNED`` while the
    rate shown is 0 and gems are still short."""
    if gems_per_min is None:
        return None
    if gems_per_min > 0:
        return round(gems_short / gems_per_min, 1)
    return NO_GEMS_EARNED if gems_short else 0.0


def health_to_close(
    w: WorldModel,
    knowledge: KnowledgeBase | None,
    prices: Mapping[str, int],
    gems_per_min: float | None,
    memory: Memory | None = None,
) -> dict[str, Any]:
    """How an unmet ``health_at_bar`` closes, and roughly what that costs
    (A124). Health rises only from potions and food (A130: there is no
    natural regeneration, so no rest is ever shown). The bar counts the heals
    carried (A132), so ``health_short`` is what health and held heals
    together still lack, and a drink closes none of it: ``eat``, the nearest
    free food in sight that Heal's own rule lets it take
    (``healing.food_to_take`` on ``memory``), its heal and the walk
    there; and ``buy``, the cheapest potion seen for sale, its
    ``gems_short`` and minutes, and the nearest known shop (``at``: a shop,
    not one known to stock the potion; nothing records what each shop sells)
    with the walk there."""
    short = math.ceil(READY_HEALTH_FRACTION * (w.max_health or 0)) - (w.health or 0) - held_heal(w)
    row: dict[str, Any] = {"health_short": short}
    here = w.pos
    food = [e for e in food_to_take(w, memory or Memory()) if not e.gem_price]
    if food and here is not None:
        f = min(food, key=lambda e: (chebyshev(here, e.pos), e.pos))
        row["eat"] = {"code": f.code, "heals": supply_row(f.code).heal or 0, **_walk_to(w, w.map_id, f.pos)}
    if sale := sorted((p, c) for c, p in prices.items() if is_potion(c)):
        price, code = sale[0]
        buy: dict[str, Any] = {"code": code, "gems_short": max(0, price - (w.gems or 0))}
        minutes = minutes_at(buy["gems_short"], gems_per_min)
        if minutes is not None:
            buy["minutes"] = minutes
        shops = iter_shop_cells(knowledge)
        if shops and here is not None:
            map_id, cell = min(shops, key=lambda s: (s[0] != w.map_id, chebyshev(here, s[1]), s))
            buy.update(_walk_to(w, map_id, cell))
        row["buy"] = buy
    return row


def _walk_to(w: WorldModel, map_id: int | None, cell: Pos) -> dict[str, Any]:
    """Where ``cell`` is, and on this map the walk there in seconds (a
    straight line at the character's speed, a rough estimate)."""
    out: dict[str, Any] = {"at": f"{map_id}:{cell[0]},{cell[1]}"}
    if map_id == w.map_id and w.pos is not None:
        out["walk_seconds"] = walk_seconds(w, cell)
    return out


def readiness_line(
    w: WorldModel, knowledge: KnowledgeBase | None, gems_per_min: float | None = None, memory: Memory | None = None
) -> str:
    return f"readiness={json.dumps(readiness(w, knowledge, gems_per_min, memory), sort_keys=True)}"


@dataclass
class StallClock:
    """When the character last moved, gained or spent gems, gained or lost
    an item or cleared a level: progress the planner can see (free-play run 1
    stood still for six minutes with the stack unchanged)."""

    key: tuple | None = None
    since: int = 0

    def note(self, w: WorldModel) -> None:
        # What is owned, not what is armed: an arm flip-flop is no progress.
        owned = sorted([s.code for s in w.held_supplies] + [w.armed_code or ""] + list(w.worn_codes.values()))
        key = (w.map_id, w.pos, w.gems, tuple(c for c in owned if c), tuple(w.levels_cleared or ()))
        if key != self.key:
            self.key, self.since = key, w.tick

    def line(self, w: WorldModel, tick_hz: int, reason: str) -> str:
        """``""`` until ``STALL_SECONDS`` pass with no change, then how long and the last decision."""
        seconds = (w.tick - self.since) // max(1, tick_hz)
        if self.key is None or seconds < STALL_SECONDS:
            return ""
        return json.dumps({"seconds": seconds, "last_decision": reason or "unknown"}, sort_keys=True)


# Ops State lists as ended recently: the newest this many, none older than ENDED_SHOWN_S.
ENDED_SHOWN = 8
ENDED_SHOWN_S = 300


@dataclass
class EndedOps:
    """The ops that finished or were dropped lately (A106), from their
    ``goal_done`` and ``goal_failed`` triggers, each with when it ended. A
    trigger is shown once, with the call it goes out on; this list outlives
    the reply, so a later prompt still says a buy is done and is not
    planned again."""

    seen: list[tuple[int, dict[str, Any]]] = field(default_factory=list)  # (tick it ended, row)

    def note(self, trigger: dict[str, Any], tick: int) -> None:
        kind, op = trigger.get("trigger"), trigger.get("op")
        if kind not in ("goal_done", "goal_failed") or not isinstance(op, dict):
            return
        row: dict[str, Any] = {"op": op, "outcome": "done" if kind == "goal_done" else "dropped"}
        if kind == "goal_failed" and trigger.get("reason"):
            row["why"] = trigger["reason"]
        self.seen = [*self.seen, (tick, row)][-ENDED_SHOWN:]

    def rows(self, tick: int, tick_hz: int) -> list[dict[str, Any]]:
        """Newest first, each with ``seconds_ago``; older than ``ENDED_SHOWN_S`` left out."""
        hz = max(1, tick_hz)
        out = []
        for ended, row in reversed(self.seen):
            ago = max(0, tick - ended) // hz
            if ago <= ENDED_SHOWN_S:
                out.append({**row, "seconds_ago": ago})
        return out


@dataclass
class Asked:
    """What the prompt of the call in flight showed, and what has changed
    since, so its reply is applied to the stack as it is now (free-play run
    7: a reply asked before ``buy matches`` finished put it back, and a
    second pair took the last gems).

    ``stack`` is the ops left on the stack when the prompt was built,
    ``held`` the supply codes held or stowed then; ``done`` and ``failed``
    the ops that finished or were dropped since (their ``goal_done`` and
    ``goal_failed`` triggers).
    """

    stack: list[dict[str, Any]]
    held: list[str]
    done: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)

    def note(self, trigger: dict[str, Any]) -> None:
        """Keep the op of a ``goal_done`` or ``goal_failed`` trigger raised while the call is out."""
        op = trigger.get("op")
        if not isinstance(op, dict):
            return
        if trigger.get("trigger") == "goal_done":
            self.done.append(op)
        elif trigger.get("trigger") == "goal_failed":
            self.failed.append(op)

    def drop_ended(self, goals: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """``goals`` less one copy of each op that ended since the prompt,
        and the copies taken out.

        An op the prompt showed on the stack is carried over by a reply
        that sends it again, so the reply's first copy of it is the one
        that has ended. A further copy is the planner asking for another
        (two ``buy`` ops buy two), and stays. An op the prompt never showed
        is the planner's own and stays.
        """
        goals, shown, dropped = list(goals), list(self.stack), []
        for op in self.done + self.failed:
            if (i := _index_of(shown, op)) is None:
                continue
            del shown[i]
            if (j := _index_of(goals, op)) is not None:
                dropped.append(goals.pop(j))
        return goals, dropped

    def held_before(self, code: str) -> int:
        """The fewest of ``code`` a ``buy`` adds one to: what was held when
        the planner asked for it, plus one for each buy of it that has
        finished since, which the planner counted on."""
        bought = sum(1 for op in self.done if op.get("op") == "buy" and op.get("code") == code)
        return sum(1 for c in self.held if supply_matches(code, c)) + bought


def _index_of(ops: list[dict[str, Any]], op: dict[str, Any]) -> int | None:
    """Where ``op`` first stands in ``ops`` (``same_ops``: its ``why`` ignored), or None."""
    return next((i for i, o in enumerate(ops) if same_ops([o], [op])), None)


@dataclass
class Answer:
    """What the background thread hands back for one call."""

    raw: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""  # set when the call failed
    status: int | None = None  # the provider's HTTP status on a failed call, when it gave one


@dataclass
class Strategist:
    """Asks the model for a new goal stack on each event and timer tick (see module docstring)."""

    config: StrategistConfig
    client: LLMClient | None = None
    clock: Callable[[], float] = time.monotonic
    inbox: list[dict[str, Any]] = field(default_factory=list)  # triggers not yet sent
    in_flight: list[dict[str, Any]] | None = None  # triggers of the call being answered
    calls: int = 0  # calls this run, for the trace
    last_call_at: float | None = None  # when the last call was sent
    last_reply_at: float | None = None  # when the last call ended; the timer and discovery gaps count from it (A83)
    backoff: Backoff = field(default_factory=Backoff)  # after a failed call, how long the next waits
    budget: CallBudget = field(default_factory=CallBudget)  # calls and tokens in the budget window
    _last_map: tuple[int, int | None] | None = None  # (map_id, level) at the last window
    _hurt: bool = False
    _token_budget: int = 0  # resolved tokens_per_min (token_budget)
    _idle_sent_for_tick: int = -1
    rejected: list[str] = field(default_factory=list)  # what the last reply had dropped or ignored, for the next State
    stall: StallClock = field(default_factory=StallClock)
    discoveries: Discoveries = field(default_factory=Discoveries)  # what this run has seen, for discovery triggers (A71)
    deferred: tuple[Plan, dict[str, Any], int, float, dict | None] | None = None  # (plan, record, pinned, since, head it replaces) waiting for an action boundary (A71)
    asked: Asked | None = None  # what the call in flight was asked from, and what changed since
    ended: EndedOps = field(default_factory=EndedOps)  # ops finished or dropped lately, for State (A106)
    _requests: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1), repr=False)
    _answers: queue.Queue = field(default_factory=queue.Queue, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    @classmethod
    def off(cls) -> Strategist:
        """The ``--no-planner`` test mode: triggers are drained and logged, nothing is sent."""
        return cls(config=StrategistConfig())

    @classmethod
    def from_env(cls) -> Strategist:
        """The planner for a live run; off only with ``AGENTREALM_NO_PLANNER``.

        Raises ``llm.PlannerConfigError`` when it is on and cannot run.
        """
        if planner_disabled_by_env():
            return cls.off()
        cfg = StrategistConfig.from_env()
        return cls(config=cfg, client=connect(cfg.provider_config))

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self.client is not None

    @property
    def failures_in_a_row(self) -> int:
        return self.backoff.failures_in_a_row

    @property
    def retry_at(self) -> float:
        return self.backoff.retry_at

    @property
    def spent(self):
        """``[sent_at, tokens]`` per call in the budget window."""
        return self.budget.spent

    def check(self) -> None:
        """One call before play. Raises ``llm.PlannerAuthError`` when the
        provider refuses the key (401 or 403), and ``llm.PlannerConfigError``
        on any other 4xx but 429 (a typo'd model is a 404). Rate limits,
        server errors and the network are left to the run, which counts them
        and backs off."""
        if not self.enabled:
            return
        assert self.client is not None
        failed = check_client(self.client, self.config.provider_config)
        if failed is not None:
            log.warning("strategist: startup check failed: %s", failed)

    # --- background thread: send the prompt, wait for the reply ---

    def start(self) -> None:
        """Start the background thread; in test mode there is nothing to start."""
        if not self.enabled:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="strategist", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the thread. A call still running is abandoned; its answer is never applied."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.serve_one(timeout=1.0)

    def serve_one(self, timeout: float | None = None) -> None:
        """Answer one waiting request, if any. Touches nothing but the two queues."""
        try:
            messages = self._requests.get(timeout=timeout)
        except queue.Empty:
            return
        self._answers.put(self._ask(messages))

    def _ask(self, messages: list[dict[str, Any]]) -> Answer:
        assert self.client is not None
        answer = Answer()
        try:
            answer.raw, answer.usage = self.client.complete(messages)
        except Exception as e:  # network, HTTP status, refusal
            answer.error = str(e) or type(e).__name__
            answer.status = error_status(e)
        return answer

    # --- tick thread: everything else ---

    def on_window(self, runner: Any) -> None:
        """Collect triggers, apply an answer that came back, maybe send the next call."""
        self._collect(runner)
        if self.deferred is not None and (
            runner.mem.held_queue is None or self.clock() - self.deferred[3] >= DEFER_MAX_S
        ):
            plan, record, pinned, _, head = self.deferred
            self.deferred = None
            # Built against a stack that has since changed (its head finished
            # or was dropped, a directives op ended): stale, so it is dropped;
            # the goal_done or goal_failed trigger asks again.
            if runner.plan.current() is head and same_ops(plan.goals[:pinned], runner.plan.directive_ops()):
                self._apply(runner, plan, record, pinned)
            else:
                runner.log("strategist", "deferred plan dropped: the stack changed while it waited", {"strategist": {"event": "deferred_dropped", **record}})
        try:
            answer = self._answers.get_nowait()
        except queue.Empty:
            answer = None
        if answer is not None:
            self._settle(runner, answer)
        if not self.enabled:
            for t in self.inbox:
                if t.get("trigger") == "no_way_on":
                    builtin_way_on(runner.plan, t, runner.world)
            if self.inbox:
                runner.log(
                    "strategist",
                    f"drained {len(self.inbox)} trigger(s), no planner",
                    {"strategist": {"event": "drain", "triggers": self.inbox}},
                )
                self.inbox = []
            return
        if self.in_flight is not None or runner.world.pos is None or not runner.world.synced:
            # One call at a time, and nothing to plan from before position, self
            # and inventory have each been read: an unread loadout reads as empty.
            return
        if self.last_reply_at is None or self.clock() - self.last_reply_at >= self.config.replan_s:
            if not any(t["trigger"] == "timer" for t in self.inbox):
                self.inbox.append({"trigger": "timer", "tick": runner.world.tick})
        if self.backoff.waiting(self.clock()):
            return  # backing off after a failed call
        if self.inbox and not self.limit_reached() and not self._discovery_waits():
            self._send(runner)

    def _discovery_waits(self) -> bool:
        """The inbox holds only a discovery, and the last call ended under ``DISCOVERY_GAP_S`` ago (A71, A83)."""
        if any(t["trigger"] != "discovery" for t in self.inbox):
            return False
        return self.last_reply_at is not None and self.clock() - self.last_reply_at < DISCOVERY_GAP_S

    def token_budget(self) -> int:
        """``tokens_per_min``, or :func:`default_tokens_per_min` when it is unset."""
        if not self._token_budget:
            self._token_budget = self.config.tokens_per_min or default_tokens_per_min(self.config.reference_sections)
        return self._token_budget

    def limit_reached(self) -> str:
        """Which per-minute budget stops a call now, or "" when one may start."""
        return self.budget.limit_reached(self.clock(), self.config.calls_per_min, self.token_budget())

    def _collect(self, runner: Any) -> None:
        """Move queued signals into the inbox and raise the map, hurt, idle and discovery triggers."""
        w, m = runner.world, runner.mem
        self.stall.note(w)
        # A landing map's level is read from the minimap just after (A145): the trigger waits for it.
        where = (w.map_id, w.map_level) if w.map_id is not None and not level_pending(w, m) else None
        if where is not None and where != self._last_map:
            # Every change of map or level, re-entering one included (after a death, a retry).
            self._last_map = where
            self.inbox.append({"trigger": "map", "map_id": w.map_id, "level": w.map_level, "tick": w.tick})
        if w.alive and w.health is not None and w.max_health:
            hurt = w.health < self.config.hurt_fraction * w.max_health
            if hurt and not self._hurt:
                self.inbox.append({"trigger": "hurt", "health": w.health, "max_health": w.max_health, "tick": w.tick})
            self._hurt = hurt
        since = m.strategist_progress_tick
        if since < 0 and runner.server_tick is not None:
            # The idle clock starts at the first tick the server reported, not at 0.
            since = m.strategist_progress_tick = w.tick
        idle_ticks = max(1, int(self.config.idle_minutes * 60 * runner.tick_hz))
        if since >= 0 and w.tick - since >= idle_ticks and self._idle_sent_for_tick != since:
            self._idle_sent_for_tick = since
            self.inbox.append({"trigger": "idle", "since_tick": since, "tick": w.tick, "idle_ticks": idle_ticks})
        finds = self.discoveries.scan(w, runner.knowledge)
        if finds:
            # One trigger holds every find not yet sent, so a run of them is one replan.
            pending = next((t for t in self.inbox if t["trigger"] == "discovery"), None)
            if pending is None:
                self.inbox.append({"trigger": "discovery", "finds": finds[-FINDS_KEPT:], "tick": w.tick})
            else:
                pending["finds"].extend(finds)
                del pending["finds"][:-FINDS_KEPT]  # a crowded view or a new map never floods the prompt
        drained = drain_triggers(m)
        for trigger in drained:
            self.ended.note(trigger, w.tick)
        if self.asked is not None:
            for trigger in drained:
                self.asked.note(trigger)
        if runner.acceptance is not None:
            for trigger in drained:
                runner.acceptance.on_strategist_trigger(trigger)
        self.inbox.extend(drained)
        del self.inbox[:-INBOX_KEPT]

    @staticmethod
    def _report(acceptance: Any, reply: dict[str, Any], goals: list[dict[str, Any]], record: dict[str, Any]) -> None:
        """Tell the acceptance hooks what the reply was: a plan accepted (at
        least one valid op, or an explicit empty stack), an error (ops all
        invalid), or neither (no ``goals`` key: notes or params only, the
        stack kept). A reply that is not a JSON object is ``_fail``'s."""
        if "goals" not in reply:
            return
        sent = reply["goals"]
        if goals or sent == []:
            acceptance.on_strategist_reply()
        else:
            acceptance.on_strategist_error("reply has no valid goal op")

    def _send(self, runner: Any) -> None:
        messages = build_prompt(
            triggers=self.inbox,
            w=runner.world,
            plan=runner.plan,
            directives=runner.directives.directives,
            knowledge=runner.knowledge,
            reference_sections=self.config.reference_sections,
            given_up_travel=given_up_travel(runner.mem.nav_stuck, runner.world.tick),
            gather_status=runner.mem.gather_status,
            gather_run=runner.gem_cuts.run_counts(runner.world.tick, runner.tick_hz),
            rejected=self.rejected,  # replaced when this call's reply is read; kept if the call fails
            stall=self.stall.line(runner.world, runner.tick_hz, runner.mem.last_decision),
            strength=runner.mem.strength,
            stalled_ops=runner.mem.stalled_ops,
            ended_ops=self.ended.rows(runner.world.tick, runner.tick_hz),
            memory=runner.mem,
        )
        # Charge the attempt now, so a call that fails still uses up the budget.
        self.calls += 1
        self.last_call_at = self.clock()
        self.budget.charge(self.last_call_at, messages)
        self.in_flight, self.inbox = self.inbox, []
        w, plan = runner.world, runner.plan
        self.asked = Asked(
            stack=[dict(op) for op in plan.goals[plan.index :]],
            held=[supply.code for supply in w.held_supplies + w.chest_supplies],
        )
        runner.log(
            "strategist",
            f"ask (call {self.calls}, {len(self.in_flight)} trigger(s))",
            {"strategist": {"event": "ask", "call": self.calls, "triggers": self.in_flight, "messages": trace_messages(messages)}},
        )
        self._requests.put(messages)

    def _settle(self, runner: Any, answer: Answer) -> None:
        """Charge the real token count, then apply the answer.

        A failed call (network, HTTP, refusal), or a reply that is not a
        JSON object, keeps the stack, puts its triggers back for the next
        call and backs off (``_fail``). Otherwise ``params`` merge onto the
        current ones at once, bounded by the directives floor as it is now,
        and ``goals``, when the reply has the key, become the stack, first
        reconciled with what changed since the prompt (:class:`Asked`): an op
        that finished or was dropped while the call was out is taken out
        once, and a new ``buy`` head counts from what was held when the
        planner asked. A stack
        equal to the one left keeps its progress, and the same op on top
        keeps its own (stall clock, wait start, block snapshot, path). No
        valid goal (an empty list, or only invalid ops) clears it: the
        planner layer emits nothing and the dispatcher's safe default runs.
        Directives ``goals`` override the planner: the directives ops still
        left stay on top, and the planner's goals go below them (A35), less any
        that only repeats one (:func:`repeats_pinned`).
        """
        triggers, self.in_flight = self.in_flight or [], None
        asked, self.asked = self.asked, None
        self.last_reply_at = self.clock()
        self.budget.settle(answer.usage)
        record: dict[str, Any] = {
            "call": self.calls,
            "raw": answer.raw,
            "usage": answer.usage,
            "tokens_last_minute": self.budget.tokens(),
        }
        if answer.error:
            self._fail(runner, triggers, answer.error, record, auth=answer.status in AUTH_STATUSES)
            return
        try:
            reply, unreadable = parse_reply(answer.raw), "reply is not a JSON object"
        except ValueError as e:
            reply, unreadable = None, f"reply is not JSON: {e}"
        if not isinstance(reply, dict):
            # No plan to apply: the stack stays as it is and the call is
            # retried like a failed one (A82, free-play run 8: a reply that
            # was not JSON cleared gather_gems and buy for the safe default).
            self._fail(runner, triggers, unreadable, record)
            return
        self.backoff.ok()
        d = runner.directives.directives
        (goals, params, notes), self.rejected = collect_rejections(
            lambda: parse_plan_payload(reply, floor_params=dict(d.params), current_params=runner.plan.params)
        )
        # A need a clue ties to a level entrance (A147): recorded before the
        # goals, so this reply's own enter_level meets it.
        unrecorded = record_needs(reply.get("entrance_needs"), runner.knowledge)
        if unrecorded:
            self.rejected = [*self.rejected, *unrecorded]
        if self.rejected:
            record["rejected"] = self.rejected
        if runner.acceptance is not None:
            self._report(runner.acceptance, reply, goals, record)
        runner.plan.params = params
        record.update(goals=goals, params=runner.plan.params, notes=notes)
        if "goals" not in reply:
            runner.log("strategist", "no goals in reply; stack kept", {"strategist": {"event": "kept", **record}})
            return
        if asked is not None:
            # Asked before these ended: the reply carries them over from the
            # stack it was shown, so they are not put back (free-play run 7).
            goals, stale = asked.drop_ended(goals)
            if stale:
                record["ended_since_ask"] = stale
        given_up = given_up_travel(runner.mem.nav_stuck, runner.world.tick)
        if given_up:
            # A travel to a cell stuck detection gave up on never reaches the
            # stack (A16): filtered here, so a re-send raises no goal_failed
            # and cannot set off another call.
            def gave_up(g):
                return travel_given_up(g, runner.world, runner.knowledge, runner.mem.strength, given_up)

            kept = [g for g in goals if not gave_up(g)]
            if len(kept) < len(goals):
                record["given_up_filtered"] = [g for g in goals if g not in kept]
                goals = kept
        goals, barred = refuse_barred(goals, runner.world, runner.knowledge)
        if barred:
            # Levels are taken in order (A145): never an entrance known to be a higher, uncleared level.
            record["barred_level_refused"] = barred
            self.rejected = [*self.rejected, *barred]
        goals, unsold = refuse_unsold(goals, runner.world, runner.knowledge)
        if unsold:
            # A buy needs a known seller (A110, A131): never a walk to shops that do not sell it.
            record["no_seller_refused"] = unsold
            self.rejected = [*self.rejected, *unsold]
        goals, unmet = refuse_unmet(goals, runner.world, runner.knowledge)
        if unmet:
            # An entrance whose need is unmet is not entered: the item comes first (A147).
            record["unmet_need_refused"] = unmet
            self.rejected = [*self.rejected, *unmet]
        goals = self._keep_stalled_below(runner.mem.stalled_ops, goals, record, runner.world.map_id)
        # Last, on the stack as it will run: what map each op runs on depends on the ops before it.
        goals, off_map = refuse_bad_cells(goals, runner.world, runner.plan.directive_ops(), runner.mem.stalled_ops)
        if off_map:
            # A cell off the map's edge, or a wall to stand on, is no target (A148).
            record["bad_cell_refused"] = off_map
            self.rejected = [*self.rejected, *off_map]
        self.deferred = None  # a newer reply decides
        old = runner.plan
        pinned = old.directive_ops()  # directives ops left: they stay on top
        # A reply that repeats a directives op does not stack it twice, so a
        # reply that differs only in such a repeat leaves the stack unchanged.
        repeats = [g for g in goals if repeats_pinned(g, pinned)]
        if repeats:
            record["pinned_repeats"] = repeats
        goals = pinned + [g for g in goals if g not in repeats]
        head = old.current()
        if goals and head is not None:
            kept = keep_gather_region(
                head, goals[0], runner.world, runner.mem.gather_in_region, runner.knowledge,
                runner.gem_cuts, triggers, runner.tick_hz,
            )
            if kept is not None:
                # The region is the head's committed target (A71). Another cell
                # of it is the same target; a move with no reason is refused,
                # and said through last_reply_rejected on purpose: that is how
                # the planner learns a part of its reply was not applied, and why.
                goals = [kept[0], *goals[1:]]
                if kept[1]:
                    record["region_kept"] = goals[0]
                    self.rejected = [
                        *self.rejected,
                        f"gather_gems kept its region at {head['x']}, {head['y']}: it is not exhausted, "
                        "not yet shown impossible, and nothing changed",
                    ]
        if same_ops(goals, old.goals[old.index :]):
            # A timer reply that re-sends the stack (or leaves an empty one
            # empty): keep its progress (stall clock, wait start, block
            # snapshot) and the path being walked.
            old.notes = notes or old.notes
            runner.log("strategist", "same stack; progress kept", {"strategist": {"event": "unchanged", **record}})
            return
        new = Plan(
            list(goals),
            dict(old.params),
            notes=notes,
            floor_params=dict(d.params),
            tick_hz=runner.tick_hz,
            directive_end=len(pinned),
        )
        new_head = not (goals and head is not None and same_ops(goals[:1], [head]))
        if new_head and asked is not None and goals and goals[0]["op"] == "buy":
            # One more than the planner saw held when it asked (``Plan.advance``
            # lowers it further on a drink or a drop).
            new.held_before = asked.held_before(goals[0]["code"])
        if new_head and head is not None and runner.mem.held_queue is not None:
            # Mid-action: the new head waits for the queue to end (A71).
            self.deferred = (new, record, len(pinned), self.clock(), head)
            runner.log("strategist", "new head deferred to the next action boundary", {"strategist": {"event": "deferred", **record}})
            return
        self._apply(runner, new, record, len(pinned))

    def _keep_stalled_below(
        self, records: list[StalledOp], goals: list[dict[str, Any]], record: dict[str, Any], map_id: int | None = None
    ) -> list[dict[str, Any]]:
        """A reply that puts a stalled op back on top (A104) has it moved just
        below its first op that can make progress, or left out when it has
        none, until a reason to retry comes (``stalled.lapse``). Said through
        last_reply_rejected, so the planner learns why."""
        order, moved, above = below_progress(goals, lambda g: find_stalled(records, g, map_id) is not None)
        if not moved:
            return goals
        record["stalled_kept_below"] = moved
        for op in moved:
            stall = find_stalled(records, op, map_id)
            where = f"moved below {json.dumps(above, sort_keys=True)}" if above is not None else "left out: no op in the reply can make progress"
            self.rejected = [
                *self.rejected,
                f"{json.dumps(op, sort_keys=True)} {where}: it stalled ({stall.why if stall else 'no progress'}) "
                "and nothing has changed since (no new item held, clue or way known)",
            ]
        return order

    def _fail(self, runner: Any, triggers: list[dict[str, Any]], error: str, record: dict[str, Any], auth: bool = False) -> None:
        """A call that gave no plan to apply (it failed, or its reply cannot be
        read as a JSON object): keep the stack, put its triggers back, and
        back off before the next call."""
        self.inbox = (triggers + self.inbox)[-INBOX_KEPT:]
        delay = self.backoff.fail(self.clock())
        log.warning("strategist: call failed: %s", error)
        runner.log(
            "strategist",
            f"failed: {error}; stack kept; next call in {delay:.0f}s",
            {"strategist": {"event": "error", "error": error, "backoff_s": delay, **record}},
        )
        if runner.acceptance is not None:
            runner.acceptance.on_strategist_error(error, auth=auth)

    def _apply(self, runner: Any, new: Plan, record: dict[str, Any], pinned: int) -> None:
        """Make ``new`` the stack. The same op on top carries on where it was;
        a new head drops the old head's path and walks (its targets were
        committed for the old op, so they no longer hold, ``targets``)."""
        old, goals = runner.plan, new.goals
        runner.plan = new
        head = old.current()
        if goals and head is not None and same_ops(goals[:1], [head]):
            # Same op on top: it carries on where it was; only the ops below it changed.
            # Keep the old op itself (its `why` too), so the path set for it stays owned.
            runner.plan.goals[0] = head
            runner.plan.carry_head_progress(old)
        else:
            # The safe default's walk serves no op, so its queue is cut at its
            # next step, not run out (free-play run 6: the walk queued before
            # the first reply took the character into a hostile).
            if head is None and goals:
                runner.mem.cut_safe_default_walk()
            runner.mem.path, runner.mem.goal, runner.mem.goal_op = [], "", None
            runner.mem.walks.clear()  # the new head walks a path of its own (A15)
            runner.mem.gather_status = ""  # it was the old head's
        if not goals:
            runner.log("strategist", "no valid goals; stack cleared (dispatcher safe default)", {"strategist": {"event": "cleared", **record}})
            return
        below = f", under {pinned} directives op(s)" if pinned else ""
        runner.log("strategist", f"plan replaced ({len(goals) - pinned} goals{below})", {"strategist": {"event": "applied", **record}})
        if runner.acceptance is not None:
            runner.acceptance.on_strategist_applied(goals)
