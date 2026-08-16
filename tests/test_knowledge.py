from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from meridian_bot.broker import BrokerError, Tool
from meridian_bot.criteria import CriteriaEvaluator
from meridian_bot.controller import BotController
from meridian_bot.knowledge import KnowledgeBase
from meridian_bot.knowledge_mcp import TOOLS as KNOWLEDGE_TOOLS
from meridian_bot.simulator import SimulatedBroker
from meridian_bot.storage import Storage

from .helpers import config, goal_payload


def make_compendium(root: Path) -> Path:
    harness = root / "harness"
    compendium = harness / "compendium"
    (compendium / "data").mkdir(parents=True)
    (harness / "substrate").mkdir()
    (harness / "substrate" / "m59-safespots.json").write_text(
        json.dumps(
            {
                "rooms": {
                    "575": {
                        "13,45": {
                            "col": 13,
                            "row": 45,
                            "held": 2,
                            "failed": 0,
                            "held_seconds": 32,
                            "most_attackers": 1,
                        },
                        "23,3": {"col": 23, "row": 3, "held": 1, "failed": 1},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (harness / "substrate" / "m59-merchants.json").write_text(
        json.dumps(
            {
                "merchants": [
                    {
                        "seen": True,
                        "id": 421,
                        "cls": "CorNothSergeant",
                        "name": "Lieutenant Vale",
                        "room": 50,
                        "markup": None,
                        "sells": [{"id": 425, "cls": "LeatherArmor", "quantity": None}],
                        "teaches": [
                            {
                                "kind": "skill",
                                "skill": "slash",
                                "price": 500,
                                "level": 1,
                                "num": 421,
                                "constant": "SKID_SLASH",
                            }
                        ],
                        "buying_rule": {
                            "source": "kod/cornoth/sergeant.kod",
                            "kod": "if Send(Self,@IsObjectWearable,#what=what) OR Send(Self,@IsObjectWeapon,#what=what) { return True; }",
                        },
                        "buys_anything": False,
                    },
                    {
                        "seen": False,
                        "id": None,
                        "cls": "TosBlacksmith",
                        "room": None,
                        "markup": None,
                        "sells": [{"id": None, "cls": "ChainArmor", "quantity": None}],
                        "teaches": [],
                        "note": "defined in source but no instance was standing in the world",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (compendium / "spells").mkdir()
    (compendium / "skills").mkdir()
    (compendium / "items").mkdir()
    zones = {
        "builtAt": None,
        "rooms": {
            "Tos": {
                "slug": "tos",
                "name": "The Streets of Tos",
                "disp": "The Streets of Tos",
                "rid": "RID_TOS",
                "ridValue": 50,
                "region": "Tos",
                "file": "kod/tos.kod",
                "line": 11,
                "terrain": ["TERRAIN_CITY"],
                "flags": [],
                "dims": {"rows": 69, "cols": 42},
                "teleport": {"row": 60, "col": 18},
                "monsters": [],
                "exits": [{"kind": "square", "to": "TosInn", "toRid": "RID_TOS_INN"}],
            },
            "TosInn": {
                "slug": "tosinn",
                "name": "Familiars",
                "disp": "Familiars",
                "rid": "RID_TOS_INN",
                "ridValue": 52,
                "region": "Set pieces",
                "file": "kod/tosinn.kod",
                "line": 11,
                "terrain": ["TERRAIN_CITY", "TERRAIN_SHOP"],
                "flags": [],
                "dims": {"rows": 11, "cols": 11},
                "teleport": {"row": 7, "col": 8},
                "monsters": [],
                "exits": [{"kind": "square", "to": "Tos", "toRid": "RID_TOS"}],
            },
            "OutdoorsG5": {
                "slug": "outdoors-g5",
                "name": "The King's Way",
                "disp": "The King's Way",
                "rid": "RID_G5",
                "ridValue": 575,
                "region": "The countryside",
                "file": "kod/g5.kod",
                "line": 57,
                "terrain": ["TERRAIN_GRASS"],
                "flags": [],
                "dims": {"rows": 20, "cols": 20},
                "teleport": {"row": 10, "col": 10},
                "monsters": [],
                "exits": [],
            },
            "OutdoorsJ3": {
                "slug": "outdoors-j3",
                "name": "The Queen's Way",
                "disp": "The Queen's Way",
                "rid": "RID_J3",
                "ridValue": 603,
                "region": "The countryside",
                "file": "kod/j3.kod",
                "line": 61,
                "terrain": ["TERRAIN_GRASS"],
                "flags": [],
                "dims": {"rows": 20, "cols": 20},
                "teleport": {"row": 10, "col": 10},
                "monsters": [],
                "exits": [],
            },
            "RazaInn": {
                "slug": "raza-inn",
                "name": "Raza Inn",
                "disp": "Raza Inn",
                "rid": "RID_RAZA_INN",
                "ridValue": 1011,
                "region": "Raza",
                "file": "kod/razainn.kod",
                "line": 11,
                "terrain": ["TERRAIN_CITY", "TERRAIN_SHOP"],
                "flags": ["ROOM_NO_COMBAT", "ROOM_SANCTUARY"],
                "dims": {"rows": 10, "cols": 10},
                "teleport": {},
                "monsters": [],
                "exits": [],
            },
            "RazaMausoleum": {
                "slug": "raza-mausoleum",
                "name": "Mausoleum (Raza)",
                "disp": "Mausoleum (Raza)",
                "rid": "RID_RAZA_MAUSOLEUM",
                "ridValue": 1016,
                "region": "Raza",
                "file": "kod/razamausoleum.kod",
                "line": 11,
                "terrain": [],
                "flags": [],
                "dims": {"rows": 10, "cols": 10},
                "teleport": {},
                "monsters": [],
                "exits": [],
            },
        },
    }
    (compendium / "data" / "zones.json").write_text(json.dumps(zones), encoding="utf-8")
    (compendium / "data" / "koddb.json").write_text(
        json.dumps(
            {
                "classes": {
                    "room": {
                        "name": "Room",
                        "file": "kod/room.kod",
                        "chain": ["Room"],
                        "classvars": {
                            "viPermanent_Flags": {"expr": "0", "line": 177, "value": 0},
                            "viTerrain_type": {"expr": "0", "line": 180, "value": 0},
                        },
                    },
                    "tos": {
                        "name": "Tos",
                        "file": "kod/tos.kod",
                        "chain": ["Tos", "Room"],
                        "properties": {
                            "viPermanent_flags": {
                                "expr": "ROOM_GUILD_PK_ONLY | ROOM_LAMPS",
                                "line": 31,
                                "value": 0,
                            }
                        },
                    },
                    "tosinn": {
                        "name": "TosInn",
                        "file": "kod/tosinn.kod",
                        "chain": ["TosInn", "Room"],
                        "properties": {
                            "viPermanent_flags": {
                                "expr": "ROOM_NO_COMBAT | ROOM_SANCTUARY | ROOM_HOMETOWN",
                                "line": 43,
                                "value": 0,
                            }
                        },
                    },
                    "outdoorsg5": {
                        "name": "OutdoorsG5",
                        "file": "kod/g5.kod",
                        "chain": ["OutdoorsG5", "Room"],
                    },
                    "outdoorsj3": {
                        "name": "OutdoorsJ3",
                        "file": "kod/j3.kod",
                        "chain": ["OutdoorsJ3", "Room"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    creatures = {
        "beasts": [
            {
                "slug": "mummy",
                "name": "mummy",
                "level": 30,
                "difficulty": 4,
                "karma": -20,
                "role": "monster",
                "where": ["Mausoleum"],
            },
            {
                "slug": "barkeep",
                "name": "barkeep",
                "level": 26,
                "difficulty": 0,
                "karma": 0,
                "role": "merchant",
                "where": ["Familiars"],
            },
            {
                "slug": "giantrat",
                "koc": "napyijoa",
                "name": "giant rat",
                "level": 30,
                "difficulty": 8,
                "karma": -10,
                "role": "monster",
                "where": ["The King's Way", "The Queen's Way"],
            },
            {
                "slug": "spiderbaby",
                "koc": "imixkinich",
                "name": "baby spider",
                "level": 25,
                "difficulty": 2,
                "karma": -5,
                "role": "monster",
                "where": ["The King's Way", "The Queen's Way"],
            },
        ],
        "weapons": [],
        "armour": {
            "body": [
                {
                    "slug": "leather-armor",
                    "name": "leather armor",
                    "cls": "LeatherArmor",
                    "value": 100,
                }
            ]
        },
    }
    (compendium / "creatures.json").write_text(json.dumps(creatures), encoding="utf-8")
    spawns = {
        "rooms": {
            "OutdoorsG5": {"name": "The King's Way", "rid": "RID_G5"},
            "OutdoorsJ3": {"name": "The Queen's Way", "rid": "RID_J3"},
        },
        "byMonster": {
            "GiantRat": [
                {"room": "OutdoorsG5", "name": "The King's Way", "how": "generator", "chance": 50, "cap": 7, "cite": "kod/g5.kod:57"},
                {"room": "OutdoorsJ3", "name": "The Queen's Way", "how": "generator", "chance": 60, "cap": 5, "cite": "kod/j3.kod:61"},
            ],
            "SpiderBaby": [
                {"room": "OutdoorsG5", "name": "The King's Way", "how": "generator", "chance": 50, "cap": 7, "cite": "kod/g5.kod:57"},
                {"room": "OutdoorsJ3", "name": "The Queen's Way", "how": "generator", "chance": 40, "cap": 5, "cite": "kod/j3.kod:61"},
            ],
        },
    }
    (compendium / "data" / "spawns.json").write_text(json.dumps(spawns), encoding="utf-8")
    (compendium / "spells" / "blink.html").write_text(
        '<html><head><meta name="description" content="Teleport a short distance."></head>'
        '<body><main><h1>blink</h1><p>Riija spell.</p><p class="cite">kod/blink.kod:11</p></main></body></html>',
        encoding="utf-8",
    )
    (compendium / "skills" / "dodge.html").write_text(
        '<html><head><meta name="description" content="Avoid blows."></head>'
        '<body><main><h1>dodge</h1><p>Automatic skill.</p><p class="cite">kod/dodge.kod:11</p></main></body></html>',
        encoding="utf-8",
    )
    (compendium / "skills" / "slash.html").write_text(
        '<html><head><meta name="description" content="Fight effectively with slashing weapons."></head>'
        '<body><main><h1>slash</h1><p>Weapon skill.</p><p class="cite">kod/slash.kod:11</p></main></body></html>',
        encoding="utf-8",
    )
    (compendium / "items" / "leatherarmor.html").write_text(
        '<html><head><meta name="description" content="Generic item page."></head>'
        '<body><main><h1>leather armor</h1></main></body></html>',
        encoding="utf-8",
    )
    (compendium / "items" / "sapphire.html").write_text(
        '<html><head><meta name="description" content="A valuable gem."></head>'
        '<body><main><h1>sapphire</h1><div class="facts">'
        '<div class="fact"><div class="lbl">Weight</div><div class="val">1</div></div>'
        '<div class="fact"><div class="lbl">Value</div><div class="val">60 sh</div></div>'
        '</div></main></body></html>',
        encoding="utf-8",
    )
    (compendium / "items" / "storytrinket.html").write_text(
        '<html><head><meta name="description" content="An unpriced trinket."></head>'
        '<body><main><h1>story trinket</h1><p>A rumor claims its value is 999 sh.</p>'
        '</main></body></html>',
        encoding="utf-8",
    )
    return harness


class KnowledgeTests(unittest.TestCase):
    def knowledge(self, root: Path) -> KnowledgeBase:
        value = config(root)
        harness = make_compendium(root)
        value = replace(value, harness=replace(value.harness, root=harness, expected_revision="fixture-revision"))
        return KnowledgeBase(value)

    def test_resolves_canonical_entities_and_rejects_invention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            knowledge = self.knowledge(Path(temporary))
            self.assertEqual("found", knowledge.resolve("Tos Inn", kinds=["location"])["status"])
            self.assertEqual(52, knowledge.resolve("Tos Inn", kinds=["location"])["entity"]["facts"]["room_id"])
            self.assertEqual("found", knowledge.resolve("52", kinds=["location"])["status"])
            self.assertEqual("found", knowledge.resolve("blink", kinds=["spell"])["status"])
            self.assertEqual("not_found", knowledge.resolve("Silverfall", kinds=["location"])["status"])
            self.assertGreaterEqual(knowledge.metadata()["entity_count"], 7)

    def test_source_item_valuation_prefers_typed_equipment_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            knowledge = self.knowledge(Path(temporary))

            valuation = knowledge.item_valuation("leather armor")

            self.assertEqual("valued", valuation["status"])
            self.assertEqual(100, valuation["unit_value"])
            self.assertIn("base item value", valuation["basis"])

    def test_source_item_valuation_imports_only_structured_value_fact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            knowledge = self.knowledge(Path(temporary))

            sapphire = knowledge.item_valuation("sapphire")
            story = knowledge.item_valuation("story trinket")

            self.assertEqual("valued", sapphire["status"])
            self.assertEqual(60, sapphire["unit_value"])
            self.assertIn("compendium Value fact", sapphire["basis"])
            self.assertEqual("value_unknown", story["status"])

    def test_financial_context_totals_shillings_and_known_item_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = config(root)
            harness = make_compendium(root)
            value = replace(
                value,
                harness=replace(
                    value.harness,
                    root=harness,
                    expected_revision="fixture-revision",
                ),
            )
            controller = BotController(value)
            try:
                finances = controller._financial_context(
                    {
                        "inventory": {
                            "items": [
                                {"name": "shillings", "amount": 278},
                                {"name": "leather armor", "amount": 2},
                                {"name": "mysterious pebble", "amount": 1},
                            ]
                        }
                    }
                )

                self.assertEqual(278, finances["carried_shillings"])
                self.assertEqual(200, finances["known_inventory_item_value"])
                self.assertEqual(478, finances["known_total_carried_value"])
                self.assertFalse(finances["valuation_complete"])
                self.assertEqual("leather armor", finances["valued_items"][0]["name"])
                self.assertEqual("mysterious pebble", finances["unknown_value_items"][0]["name"])
                leather_buyers = finances["buyer_candidates"][0]
                self.assertEqual("Wearable", leather_buyers["inferred_source_category"])
                self.assertEqual(
                    "CorNothSergeant", leather_buyers["candidates"][0]["merchant"]
                )
                self.assertEqual(
                    {
                        "seller_id_at_build": 421,
                        "name": "Lieutenant Vale",
                        "room_id": 50,
                    },
                    leather_buyers["candidates"][0]["instances"][0],
                )
                self.assertIn("confirm=false", leather_buyers["next_evidence"])
                self.assertTrue(finances["banking_policy"]["never_blocks_travel_or_combat"])
            finally:
                controller.storage.close()

    def test_financial_context_separates_source_estimate_from_live_quote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = config(root)
            harness = make_compendium(root)
            value = replace(
                value,
                harness=replace(
                    value.harness,
                    root=harness,
                    expected_revision="fixture-revision",
                ),
            )
            controller = BotController(value)
            try:
                goal = {"id": "quote-goal"}
                phase = {"id": "quote-phase", "kind": "liquidate_inventory"}
                observation = {
                    "look": {"room": {"num": 50, "name": "The Streets of Tos"}},
                    "inventory": {
                        "items": [
                            {"id": 77, "name": "leather armor", "amount": 2}
                        ]
                    },
                }

                unquoted = controller._financial_context(observation)
                self.assertEqual(
                    200, unquoted["source_estimated_liquidatable_inventory_value"]
                )
                self.assertEqual(
                    200, unquoted["known_liquidatable_inventory_value"]
                )
                self.assertEqual(
                    0, unquoted["confirmed_live_quote_liquidatable_value"]
                )
                self.assertEqual(
                    "quote_required", unquoted["liquidation_status"]["state"]
                )
                self.assertEqual(
                    [77],
                    [
                        item["id"]
                        for item in unquoted["unquoted_liquidatable_items"]
                    ],
                )
                manager_context = controller._campaign_manager_financial_context(
                    unquoted
                )
                self.assertEqual(
                    200,
                    manager_context[
                        "source_estimated_liquidatable_inventory_value"
                    ],
                )
                self.assertEqual(
                    "quote_required",
                    manager_context["liquidation_status"]["state"],
                )

                controller._record_prepare_combat_sell_quote(
                    goal,
                    phase,
                    {"to": 421, "items": [77], "confirm": False},
                    observation,
                    {"sold": False, "offered_price": 75},
                )
                quoted = controller._financial_context(observation)
                self.assertEqual(
                    75, quoted["confirmed_live_quote_liquidatable_value"]
                )
                self.assertEqual(
                    "live_quotes_available", quoted["liquidation_status"]["state"]
                )
                self.assertEqual([], quoted["unquoted_liquidatable_items"])
                self.assertEqual(
                    77,
                    quoted["valid_live_sell_quotes"][0]["items"][0]["id"],
                )

                changed_inventory = {
                    **observation,
                    "inventory": {
                        "items": [
                            {"id": 77, "name": "leather armor", "amount": 3}
                        ]
                    },
                }
                invalidated = controller._financial_context(changed_inventory)
                self.assertEqual(
                    0, invalidated["confirmed_live_quote_liquidatable_value"]
                )
                self.assertEqual([], invalidated["valid_live_sell_quotes"])

                controller._record_prepare_combat_sell_quote(
                    goal,
                    phase,
                    {
                        "to": 421,
                        "items": [{"id": 77, "amount": 1}],
                        "confirm": False,
                    },
                    changed_inventory,
                    {"sold": False, "offered_price": 25},
                )
                partial_quote = controller._financial_context(changed_inventory)
                self.assertEqual(
                    25, partial_quote["confirmed_live_quote_liquidatable_value"]
                )
                self.assertEqual(
                    1,
                    partial_quote["valid_live_sell_quotes"][0]["items"][0][
                        "quantity"
                    ],
                )
                self.assertEqual(
                    3,
                    partial_quote["valid_live_sell_quotes"][0]["items"][0][
                        "inventory_quantity"
                    ],
                )
                self.assertEqual(
                    [77],
                    [
                        item["id"]
                        for item in partial_quote["unquoted_liquidatable_items"]
                    ],
                )
            finally:
                controller.storage.close()

    def test_financial_context_excludes_intrinsically_unsellable_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = config(root)
            harness = make_compendium(root)
            value = replace(
                value,
                harness=replace(
                    value.harness,
                    root=harness,
                    expected_revision="fixture-revision",
                ),
            )
            controller = BotController(value)
            try:
                goal = controller.storage.submit_goal(
                    goal_payload(request_id="restricted-financial-context")
                )["goal"]
                controller.storage.promote()
                controller._record_blocked_action(
                    goal,
                    {"look": {"room": {"num": 103}}, "inventory": {"items": []}},
                    "sell",
                    {"to": 674, "items": [77], "confirm": False},
                    (
                        'Meidei tells you, "I cannot see how you could bear to part with '
                        'leather armor! I certainly couldn\'t be the one to take it off '
                        'your hands."'
                    ),
                )

                finances = controller._financial_context(
                    {
                        "inventory": {
                            "items": [
                                {"name": "shillings", "amount": 20},
                                {"id": 77, "name": "leather armor", "amount": 1},
                                {"id": 78, "name": "leather armor", "amount": 1},
                            ]
                        }
                    }
                )

                self.assertEqual(200, finances["known_inventory_item_value"])
                self.assertEqual(100, finances["known_liquidatable_inventory_value"])
                self.assertEqual(
                    [77],
                    [item["id"] for item in finances["npc_transfer_restricted_items"]],
                )
                transferability = {
                    item["id"]: item["npc_transferable"]
                    for item in finances["valued_items"]
                }
                self.assertEqual({77: False, 78: True}, transferability)
                self.assertEqual(1, len(finances["buyer_candidates"]))
            finally:
                controller.storage.close()

    def test_financial_context_excludes_equipped_gear_from_liquidatable_wealth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = config(root)
            harness = make_compendium(root)
            value = replace(
                value,
                harness=replace(
                    value.harness,
                    root=harness,
                    expected_revision="fixture-revision",
                ),
            )
            controller = BotController(value)
            try:
                finances = controller._financial_context(
                    {
                        "inventory": {
                            "items": [
                                {"name": "shillings", "amount": 12},
                                {"id": 77, "name": "leather armor", "amount": 1},
                            ]
                        },
                        "equipment": {
                            "equipped": [
                                {"id": 77, "name": "leather armor"}
                            ]
                        },
                    }
                )

                self.assertEqual(100, finances["known_inventory_item_value"])
                self.assertEqual(0, finances["known_liquidatable_inventory_value"])
                self.assertEqual(
                    [{
                        "id": 77,
                        "name": "leather armor",
                        "reason": "equipped or in-use active loadout",
                    }],
                    finances["protected_sale_items"],
                )
                self.assertTrue(finances["valued_items"][0]["sale_protected"])
                self.assertFalse(finances["valued_items"][0]["liquidatable"])
                self.assertEqual([], finances["buyer_candidates"])
            finally:
                controller.storage.close()

    def test_location_rules_merge_property_defined_kod_class_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            knowledge = self.knowledge(Path(temporary))
            street = knowledge.get("location:50")["entity"]
            inn = knowledge.get("location:52")["entity"]

            self.assertIn("ROOM_GUILD_PK_ONLY", street["facts"]["flags"])
            self.assertEqual(
                {"ROOM_HOMETOWN", "ROOM_NO_COMBAT", "ROOM_SANCTUARY"},
                set(inn["facts"]["effective_permanent_flags"]),
            )
            self.assertEqual("TosInn", inn["facts"]["flag_evidence"]["declaring_class"])
            self.assertIn("kod/tosinn.kod:43", inn["evidence"]["source_ref"])

    def test_nearest_safe_location_comes_from_source_graph_and_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            knowledge = self.knowledge(Path(temporary))

            options = knowledge.safe_location_candidates(50, limit=8)
            self.assertEqual("found", options["status"])
            self.assertGreaterEqual(len(options["candidates"]), 1)
            self.assertEqual(52, options["candidates"][0]["room_id"])
            self.assertTrue(
                all(
                    {"ROOM_SANCTUARY", "ROOM_NO_COMBAT"}.intersection(
                        candidate["flags"]
                    )
                    for candidate in options["candidates"]
                )
            )

            staging = knowledge.nearest_safe_location(50)

            self.assertEqual("found", staging["status"])
            self.assertEqual(52, staging["room_id"])
            self.assertEqual("source_connection_graph", staging["basis"])
            self.assertEqual(1, staging["distance"])
            self.assertIn("ROOM_SANCTUARY", staging["flags"])

            regional = knowledge.nearest_safe_location(1016)
            self.assertEqual(1011, regional["room_id"])
            self.assertEqual("source_region", regional["basis"])
            self.assertIsNone(regional["distance"])

    def test_goal_validation_canonicalizes_rooms_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            knowledge = self.knowledge(Path(temporary))
            valid = knowledge.validate_goal(
                {
                    "objective": "Raise health and return to the Tos Inn.",
                    "success_criteria": [
                        {"kind": "numeric_threshold", "metric": "vitals.health.max", "value": 30},
                        {"kind": "location_reached", "location": "Tos Inn"},
                    ],
                }
            )
            self.assertTrue(valid["valid"])
            self.assertEqual("status.vitals.health.max", valid["canonical_goal"]["success_criteria"][0]["metric"])
            self.assertEqual(52, valid["canonical_goal"]["success_criteria"][1]["room_id"])
            self.assertEqual("Familiars", valid["canonical_goal"]["success_criteria"][1]["location"])

            invalid = knowledge.validate_goal(
                {
                    "objective": "Explore Silverfall.",
                    "success_criteria": [{"kind": "location_reached", "location": "Silverfall"}],
                }
            )
            self.assertFalse(invalid["valid"])
            self.assertEqual("UNKNOWN_LOCATION", invalid["errors"][0]["code"])

            malformed = knowledge.validate_goal({"objective": "Wait.", "success_criteria": [], "mystery": True})
            self.assertFalse(malformed["valid"])
            self.assertEqual(
                {"UNKNOWN_GOAL_FIELDS", "INVALID_GOAL_SCHEMA"},
                {error["code"] for error in malformed["errors"]},
            )

            malformed_farm = knowledge.validate_goal(
                {
                    "objective": "Raise max HP.",
                    "success_criteria": [
                        {
                            "kind": "numeric_threshold",
                            "metric": "status.vitals.health.max",
                            "value": 31,
                        }
                    ],
                    "constraints": {
                        "operator_notes": "Use room 567 with an assigned_room and safe spots."
                    },
                }
            )
            self.assertFalse(malformed_farm["valid"])
            farm_error = next(
                error
                for error in malformed_farm["errors"]
                if error["code"] == "INVALID_FARM_OPERATOR_NOTES"
            )
            self.assertEqual(["assigned_room"], farm_error["fields"])

            structured_farm = knowledge.validate_goal(
                {
                    "objective": "Raise max HP.",
                    "success_criteria": [
                        {
                            "kind": "numeric_threshold",
                            "metric": "status.vitals.health.max",
                            "value": 31,
                        }
                    ],
                    "constraints": {
                        "operator_notes": (
                            "hunt=groundworm larva; assigned_room=567; "
                            "use_safe_spots=true"
                        )
                    },
                }
            )
            self.assertTrue(structured_farm["valid"])

            ability = knowledge.validate_goal(
                {
                    "objective": "Improve Blink through ordinary spell use.",
                    "success_criteria": [
                        {
                            "kind": "numeric_threshold",
                            "metric": "ability.spell.BLINK",
                            "value": 10,
                        }
                    ],
                }
            )
            self.assertTrue(ability["valid"])
            self.assertEqual(
                "ability.spell.blink",
                ability["canonical_goal"]["success_criteria"][0]["metric"],
            )
            unknown_ability = knowledge.validate_goal(
                {
                    "objective": "Improve an invented skill.",
                    "success_criteria": [
                        {
                            "kind": "numeric_threshold",
                            "metric": "ability.skill.space piracy",
                            "value": 10,
                        }
                    ],
                }
            )
            self.assertFalse(unknown_ability["valid"])
            self.assertIn(
                "UNKNOWN_ABILITY",
                {error["code"] for error in unknown_ability["errors"]},
            )

    def test_purchase_goals_require_verified_merchant_stock_and_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            knowledge = self.knowledge(Path(temporary))
            buyer = knowledge.resolve("CorNothSergeant", kinds=["merchant"])
            self.assertEqual(["Weapon", "Wearable"], buyer["entity"]["facts"]["buying_categories"])
            self.assertFalse(buyer["entity"]["facts"]["buys_anything"])
            self.assertEqual(
                "Lieutenant Vale",
                buyer["entity"]["facts"]["instances"][0]["name"],
            )
            self.assertEqual(
                {
                    "merchant_class": "CorNothSergeant",
                    "entity_id": "merchant:cor-noth-sergeant",
                    "instance": {
                        "seller_id_at_build": 421,
                        "name": "Lieutenant Vale",
                        "room_id": 50,
                    },
                    "matched_by": "object_id",
                },
                knowledge.merchant_identity(
                    object_id=421, name="a stale display name"
                ),
            )
            self.assertEqual(
                "CorNothSergeant",
                knowledge.merchant_identity(name="Lieutenant Vale")[
                    "merchant_class"
                ],
            )
            self.assertIn("fresh in-room sell quote", buyer["entity"]["facts"]["sale_verification"])
            unplaced = knowledge.resolve("TosBlacksmith", kinds=["merchant"])
            self.assertEqual("found", unplaced["status"])
            self.assertFalse(unplaced["entity"]["facts"]["available"])
            self.assertIn("UNAVAILABLE", unplaced["entity"]["summary"])

            missing_plan = knowledge.validate_goal(
                {
                    "objective": "Buy leather armor and return home.",
                    "success_criteria": [
                        {"kind": "inventory_contains", "item": "leather armor"}
                    ],
                }
            )
            self.assertFalse(missing_plan["valid"])
            self.assertIn(
                "PURCHASE_PLAN_REQUIRED",
                {error["code"] for error in missing_plan["errors"]},
            )

            unavailable = knowledge.validate_goal(
                {
                    "objective": "Buy leather armor in Tos.",
                    "success_criteria": [
                        {"kind": "inventory_contains", "item": "leather armor"}
                    ],
                    "constraints": {
                        "purchase_plan": {
                            "item": "leather armor",
                            "merchant_class": "TosBlacksmith",
                            "room_id": 50,
                        }
                    },
                }
            )
            self.assertFalse(unavailable["valid"])
            self.assertIn(
                "MERCHANT_UNAVAILABLE",
                {error["code"] for error in unavailable["errors"]},
            )

            valid = knowledge.validate_goal(
                {
                    "objective": "Buy leather armor in the verified shop.",
                    "success_criteria": [
                        {"kind": "inventory_contains", "item": "leather armor"}
                    ],
                    "constraints": {
                        "purchase_plan": {
                            "item": "LeatherArmor",
                            "merchant_class": "CorNothSergeant",
                            "room_id": 50,
                            "maximum_price": 250,
                        }
                    },
                }
            )
            self.assertTrue(valid["valid"], valid["errors"])
            self.assertTrue(valid["purchase_verification"]["static_verified"])
            self.assertIn(
                "PURCHASE_ITEM_CANONICALIZED",
                {warning["code"] for warning in valid["warnings"]},
            )
            self.assertEqual(
                "leather armor",
                valid["canonical_goal"]["constraints"]["purchase_plan"]["item"],
            )

            training = knowledge.training_candidates("skill", "slash")
            self.assertEqual("found", training["status"])
            self.assertEqual(1, len(training["candidates"]))
            self.assertEqual(
                {
                    "offering_kind": "skill",
                    "item": "slash",
                    "merchant_class": "CorNothSergeant",
                    "room_id": 50,
                    "maximum_price": 500,
                },
                {
                    key: training["candidates"][0][key]
                    for key in (
                        "offering_kind",
                        "item",
                        "merchant_class",
                        "room_id",
                        "maximum_price",
                    )
                },
            )
            merchant = knowledge.get("merchant:cor-noth-sergeant")["entity"]
            self.assertEqual(500, merchant["facts"]["teaching_offers"][0]["price"])
            self.assertIn(
                ("teaches", "skill:slash"),
                {
                    (relation["predicate"], relation["entity"]["id"])
                    for relation in merchant["relations"]
                },
            )

            strategic = knowledge.validate_goal(
                {
                    "title": "Raise TestHero to 45 maximum HP",
                    "objective": (
                        "Raise maximum HP through ordinary gameplay. Autonomously manage "
                        "farming, recovery, equipment, inventory, selling, buying, and optional "
                        "banking as intermediate tactics."
                    ),
                    "success_criteria": [
                        {
                            "kind": "numeric_threshold",
                            "metric": "status.vitals.health.max",
                            "value": 45,
                        }
                    ],
                }
            )
            self.assertTrue(strategic["valid"], strategic["errors"])
            self.assertIsNone(strategic["purchase_verification"])

    def test_paid_training_requires_budget_and_exact_ability_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            knowledge = self.knowledge(Path(temporary))
            conversational_only = knowledge.validate_goal(
                {
                    "objective": "Learn the Dodge skill by talking to the teacher.",
                    "success_criteria": [
                        {"kind": "location_reached", "room_id": 50}
                    ],
                }
            )
            self.assertFalse(conversational_only["valid"])
            self.assertTrue(
                {"PURCHASE_PLAN_REQUIRED", "ABILITY_RESULT_CRITERION_REQUIRED"}
                .issubset({error["code"] for error in conversational_only["errors"]})
            )

            valid = knowledge.validate_goal(
                {
                    "objective": "Learn the Slash skill from the verified teacher.",
                    "success_criteria": [
                        {
                            "kind": "numeric_threshold",
                            "metric": "ability.skill.SLASH",
                            "operator": ">=",
                            "value": 1,
                        }
                    ],
                    "constraints": {
                        "purchase_plan": {
                            "offering_kind": "skill",
                            "item": "SLASH",
                            "merchant_class": "CorNothSergeant",
                            "room_id": 50,
                            "maximum_price": 500,
                        }
                    },
                }
            )
            self.assertTrue(valid["valid"], valid["errors"])
            self.assertTrue(valid["purchase_verification"]["static_verified"])
            self.assertEqual(
                "ability.skill.slash",
                valid["canonical_goal"]["success_criteria"][0]["metric"],
            )
            self.assertEqual(
                "slash",
                valid["canonical_goal"]["constraints"]["purchase_plan"]["item"],
            )
            self.assertNotIn(
                "TEACHER_STOCK_LIVE_VERIFICATION_REQUIRED",
                {warning["code"] for warning in valid["warnings"]},
            )

            invented_teacher = knowledge.validate_goal(
                {
                    "objective": "Learn the Slash skill.",
                    "success_criteria": [
                        {
                            "kind": "numeric_threshold",
                            "metric": "ability.skill.slash",
                            "value": 1,
                        }
                    ],
                    "constraints": {
                        "purchase_plan": {
                            "offering_kind": "skill",
                            "item": "slash",
                            "merchant_class": "Ye Olde Slasher Salesman",
                            "room_id": 201,
                            "maximum_price": 15,
                        }
                    },
                }
            )
            teacher_error = next(
                error
                for error in invented_teacher["errors"]
                if error["code"] == "UNKNOWN_MERCHANT_CLASS"
            )
            self.assertEqual(
                "CorNothSergeant",
                teacher_error["purchase_plan_candidates"][0]["merchant_class"],
            )
            self.assertEqual(
                500,
                teacher_error["purchase_plan_candidates"][0]["maximum_price"],
            )

            wrong_result = knowledge.validate_goal(
                {
                    "objective": "Learn the Dodge skill from the verified teacher.",
                    "success_criteria": [
                        {
                            "kind": "numeric_threshold",
                            "metric": "ability.spell.blink",
                            "value": 1,
                        }
                    ],
                    "constraints": valid["canonical_goal"]["constraints"],
                }
            )
            self.assertFalse(wrong_result["valid"])
            self.assertIn(
                "ABILITY_RESULT_CRITERION_REQUIRED",
                {error["code"] for error in wrong_result["errors"]},
            )

    def test_goal_drafter_replaces_invented_teacher_with_unique_catalogue_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = config(root)
            harness = make_compendium(root)
            value = replace(
                value,
                harness=replace(
                    value.harness,
                    root=harness,
                    expected_revision="fixture-revision",
                ),
            )
            controller = BotController(value)

            class InventedTeacherModel:
                def __init__(self) -> None:
                    self.calls: list[dict[str, object]] = []

                def draft_goal(self, **kwargs: object) -> dict[str, object]:
                    self.calls.append(kwargs)
                    return {
                        "title": "Acquire the slash skill",
                        "objective": "Ensure the character has the slash skill.",
                        "success_criteria": [
                            {
                                "id": "slash_at_least_1",
                                "kind": "numeric_threshold",
                                "metric": "ability.skill.slash",
                                "operator": ">=",
                                "value": 1,
                            }
                        ],
                        "constraints": {
                            "purchase_plan": {
                                "offering_kind": "skill",
                                "item": "slash",
                                "merchant_class": "Ye Olde Slasher Salesman",
                                "room_id": 201,
                                "maximum_price": 15,
                            }
                        },
                        "priority": 50,
                        "activation": "queue",
                    }

            model = InventedTeacherModel()
            controller.model = model  # type: ignore[assignment]
            try:
                result = controller.draft_goal(
                    {"prompt": "Acquire the slash skill at priority 50."}
                )

                plan = result["goal"]["constraints"]["purchase_plan"]
                self.assertEqual("CorNothSergeant", plan["merchant_class"])
                self.assertEqual(50, plan["room_id"])
                self.assertEqual(500, plan["maximum_price"])
                self.assertEqual(1, len(model.calls))
                self.assertIn(
                    "TRAINING_PLAN_GROUNDED",
                    {
                        warning["code"]
                        for warning in result["validation"]["warnings"]
                    },
                )
                training_hint = next(
                    hint
                    for hint in model.calls[0]["grounding_hints"]
                    if hint.get("kind") == "training_options"
                )
                self.assertEqual(
                    "CorNothSergeant",
                    training_hint["purchase_plan_candidates"][0]["merchant_class"],
                )
            finally:
                controller.storage.close()

    def test_progression_and_planner_context_are_grounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            knowledge = self.knowledge(Path(temporary))
            progression = knowledge.progression_context({"max_health": 25})
            self.assertEqual("mummy", progression["candidates"][0]["name"])
            self.assertNotIn("barkeep", {item["name"] for item in progression["candidates"]})
            self.assertIn("candidate_warning", progression["new_player_doctrine"]["progression"])
            self.assertEqual(30, progression["new_player_doctrine"]["pvp"]["guide_protection_until_max_hp"])
            context = knowledge.context_for(
                {
                    "objective": "Return to Tos Inn.",
                    "success_criteria": [{"kind": "location_reached", "location": "Tos Inn"}],
                },
                {"look": {"room": {"num": 50, "name": "The Streets of Tos"}}},
            )
            self.assertTrue(context["goal_validation"]["valid"])
            self.assertIn("Familiars", {item["canonical_name"] for item in context["relevant_entities"]})
            self.assertTrue(context["new_player_doctrine"]["progression"]["max_hp_is_level"])

            farm_context = knowledge.context_for(
                {
                    "objective": "Raise max HP with giant rats, then return to Tos Inn.",
                    "success_criteria": [
                        {"kind": "numeric_threshold", "metric": "vitals.health.max", "value": 28},
                        {"kind": "location_reached", "location": "Tos Inn"},
                    ],
                    "constraints": {
                        "operator_notes": "hunt=giant rat assigned_room=575 use_safe_spots=false"
                    },
                },
                {
                    "look": {"room": {"num": 52, "name": "Familiars"}},
                    "status": {"vitals": {"health": {"max": 27}}},
                },
            )
            options = farm_context["hunt_room_options"]
            self.assertEqual("found", options["status"])
            self.assertEqual(575, options["rooms"][0]["room"]["room_id"])
            self.assertTrue(options["rooms"][0]["preferred"])
            self.assertEqual(50, options["rooms"][0]["target_chance"])
            self.assertEqual(7, options["rooms"][0]["population_cap"])
            self.assertEqual(
                {"giant rat", "baby spider"},
                {item["creature"] for item in options["rooms"][0]["spawns"]},
            )
            self.assertEqual(
                100, options["rooms"][0]["generator_chance_total"]
            )
            safe_spots = options["rooms"][0]["safe_spot_evidence"]
            self.assertEqual(2, safe_spots["tested_squares"])
            self.assertEqual(1, safe_spots["proven_clean_squares"])
            self.assertEqual(1, safe_spots["discredited_squares"])
            self.assertEqual(
                {"col": 13, "row": 45},
                {
                    "col": safe_spots["best_clean_spots"][0]["col"],
                    "row": safe_spots["best_clean_spots"][0]["row"],
                },
            )
            room = knowledge.get("location:575")["entity"]
            self.assertEqual(2, len(room["spawn_table"]["spawns"]))
            self.assertTrue(progression["room_options_by_candidate"])

    def test_source_content_change_rebuilds_with_a_new_corpus_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            knowledge = self.knowledge(root)
            original = knowledge.corpus_version
            page = knowledge.root / "spells" / "blink.html"
            page.write_text(page.read_text(encoding="utf-8").replace("short distance", "brief distance"), encoding="utf-8")

            rebuilt = knowledge.ensure_ready()

            self.assertNotEqual(original, rebuilt["corpus_version"])
            self.assertEqual(rebuilt["corpus_version"], knowledge.metadata()["corpus_version"])

    def test_startup_emits_one_interesting_event_per_corpus_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = config(root)
            harness = make_compendium(root)
            value = replace(value, harness=replace(value.harness, root=harness, expected_revision="fixture-revision"))
            first = BotController(value)
            try:
                first.startup(connect_game=False)
                self.assertEqual(1, len(first.storage.events(kinds=["knowledge.corpus.updated"])["events"]))
            finally:
                first.storage.close()

            second = BotController(value)
            try:
                second.startup(connect_game=False)
                self.assertEqual(1, len(second.storage.events(kinds=["knowledge.corpus.updated"])["events"]))
            finally:
                second.storage.close()

    def test_live_progression_uses_agent_only_for_character_scoped_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = config(root)
            harness = make_compendium(root)
            value = replace(value, harness=replace(value.harness, root=harness, expected_revision="fixture-revision"))
            controller = BotController(value)
            try:
                class DevelopmentBroker(SimulatedBroker):
                    def __init__(self) -> None:
                        super().__init__()
                        agent_schema = {
                            "type": "object",
                            "properties": {"agent": {"type": "string"}},
                            "required": ["agent"],
                        }
                        self.tools["abilities"] = Tool(
                            "abilities", "live abilities", agent_schema
                        )
                        self.tools["spells"] = Tool(
                            "spells", "live spell readiness", agent_schema
                        )

                    def call_tool(self, name, arguments, *, timeout=180, mutation=False):
                        if name == "abilities":
                            self.calls.append((name, dict(arguments)))
                            return {
                                "skills": [{"name": "Dodge", "ability": 12}],
                                "spells": [{"name": "Blink", "ability": 5}],
                                "freshness": {
                                    "known": {"skills": True, "spells": True}
                                },
                                "advancement": {
                                    "changes_on_record": 1,
                                    "recent": [],
                                    "atrophied": [],
                                },
                            }
                        if name == "spells":
                            self.calls.append((name, dict(arguments)))
                            return {
                                "known_spells": 1,
                                "castable_now": 1,
                                "spells": [{"name": "Blink", "castable": True}],
                            }
                        return super().call_tool(
                            name, arguments, timeout=timeout, mutation=mutation
                        )

                broker = DevelopmentBroker()
                controller.broker = broker
                controller.dependencies["broker"] = "healthy"

                result = controller.progression_context({"character_state": {"max_health": 21}})

                advancement = next(arguments for name, arguments in broker.calls if name == "progress")
                grounds = next(arguments for name, arguments in broker.calls if name == "hunting_grounds")
                prey = next(arguments for name, arguments in broker.calls if name == "prey")
                self.assertEqual("primary", advancement["agent"])
                self.assertNotIn("agent", grounds)
                self.assertEqual(21, grounds["for_level"])
                self.assertEqual("primary", prey["agent"])
                self.assertEqual("advance", prey["purpose"])
                self.assertEqual([{"kind": "hp"}], prey["goals"])
                self.assertIn("live_advancement", result)
                self.assertIn("live_hunting_grounds", result)
                self.assertIn("live_prey", result)
                self.assertEqual(
                    12, result["live_development"]["skills"][0]["ability"]
                )
                self.assertTrue(
                    result["live_development"]["spell_readiness"]["spells"][0][
                        "castable"
                    ]
                )
                self.assertEqual("compact", result["detail"])
                self.assertLess(len(json.dumps(result)), 30_000)
                self.assertNotIn("source_ref", json.dumps(result))
                full = controller.progression_context(
                    {"character_state": {"max_health": 21}, "detail": "full"}
                )
                self.assertNotIn("detail", full)
                self.assertIn("source_ref", json.dumps(full))
            finally:
                controller.storage.close()

    def test_live_progression_advisories_fail_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = config(root)
            harness = make_compendium(root)
            value = replace(value, harness=replace(value.harness, root=harness, expected_revision="fixture-revision"))
            controller = BotController(value)
            try:
                class ProgressFailureBroker(SimulatedBroker):
                    def call_tool(self, name, arguments, *, timeout=180, mutation=False):
                        if name == "progress":
                            raise BrokerError("progress unavailable")
                        return super().call_tool(name, arguments, timeout=timeout, mutation=mutation)

                broker = ProgressFailureBroker()
                controller.broker = broker
                controller.dependencies["broker"] = "healthy"

                result = controller.progression_context({"character_state": {"max_health": 21}})

                self.assertIn("progress: progress unavailable", result["live_warning"])
                self.assertIn("live_hunting_grounds", result)
                self.assertIn("live_prey", result)
            finally:
                controller.storage.close()

    def test_room_id_only_criterion_does_not_match_every_room(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage = Storage(Path(temporary) / "controller.sqlite3")
            evaluator = CriteriaEvaluator(storage)
            result = evaluator.evaluate(
                {"success_criteria": [{"kind": "location_reached", "room_id": 52}]},
                {"look": {"room": {"num": 50, "name": "The Streets of Tos"}}},
            )
            self.assertFalse(result["all_met"])
            storage.close()

    def test_named_ability_metric_uses_live_server_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage = Storage(Path(temporary) / "controller.sqlite3")
            evaluator = CriteriaEvaluator(storage)
            goal = {
                "success_criteria": [
                    {
                        "kind": "numeric_threshold",
                        "metric": "ability.spell.bLiNk",
                        "operator": ">=",
                        "value": 10,
                    }
                ]
            }
            observation = {
                "abilities": {
                    "skills": [],
                    "spells": [{"name": "Blink", "ability": 9}],
                    "freshness": {"known": {"skills": True, "spells": True}},
                }
            }
            self.assertFalse(evaluator.evaluate(goal, observation)["all_met"])
            observation["abilities"]["spells"][0]["ability"] = 10
            self.assertTrue(evaluator.evaluate(goal, observation)["all_met"])
            observation["abilities"]["freshness"]["known"]["spells"] = False
            self.assertFalse(evaluator.evaluate(goal, observation)["all_met"])
            storage.close()

    def test_legacy_active_goal_with_unknown_location_is_paused_before_planning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = config(root)
            harness = make_compendium(root)
            value = replace(value, harness=replace(value.harness, root=harness, expected_revision="fixture-revision"))
            controller = BotController(value)
            try:
                controller.broker = SimulatedBroker()
                goal = controller.storage.submit_goal(
                    {
                        "request_id": "legacy-silverfall",
                        "objective": "Explore Silverfall.",
                        "success_criteria": [{"kind": "location_reached", "location": "Silverfall"}],
                    }
                )["goal"]

                result = controller.turn()

                self.assertTrue(result["paused"])
                self.assertTrue(result["strategic_goal_preserved"])
                self.assertEqual("paused", controller.storage.goal(goal["id"])["status"])
                self.assertIsNone(controller.storage.goal(goal["id"])["blocked_reason"])
                self.assertEqual([], controller.storage.events(kinds=["action.attempted"])["events"])
            finally:
                controller.storage.close()

    def test_knowledge_mcp_has_five_fully_documented_closed_schemas(self) -> None:
        self.assertEqual(
            {"search", "resolve", "get", "validate_goal", "progression_context"},
            {tool["name"] for tool in KNOWLEDGE_TOOLS},
        )
        for tool in KNOWLEDGE_TOOLS:
            self.assertGreater(len(tool["description"]), 40, tool["name"])
            self._assert_documented_schema(tool["inputSchema"], tool["name"])

    def _assert_documented_schema(self, schema: dict[str, object], path: str) -> None:
        schema_type = schema.get("type")
        if schema_type == "object":
            properties = schema.get("properties")
            self.assertIsInstance(properties, dict, path)
            self.assertTrue(properties, path)
            self.assertFalse(schema.get("additionalProperties", True), path)
            for name, child in properties.items():
                self.assertIsInstance(child, dict, f"{path}.{name}")
                self.assertTrue(child.get("description"), f"{path}.{name}")
                self._assert_documented_schema(child, f"{path}.{name}")
        if schema_type == "array":
            items = schema.get("items")
            self.assertIsInstance(items, dict, f"{path}[]")
            self.assertTrue(items, f"{path}[]")
            self._assert_documented_schema(items, f"{path}[]")


if __name__ == "__main__":
    unittest.main()
