using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace FullMapGen
{
    // POI categorisation + JSON writing (no ECS here, pure data).
    // Coordinates are world tiles, same convention as the rendered map pixels:
    //   pixel (col,row) of map part (X,Y) <-> world x = X*256+col, y = Y*256+(255-row)   (+y = north)
    // Entity position -> tile: x = round(LocalTransform.Position.x), y = round(LocalTransform.Position.z)
    public static class FullMapGenPoi
    {
        // Returns the viewer category for an ObjectID name, or null to skip it.
        // Order matters: the first matching rule wins.
        public static string Categorize(string n)
        {
            if (string.IsNullOrEmpty(n) || char.IsDigit(n[0]) || n[0] == '-') return null;   // modded / unnamed ids

            // never POIs: gear, projectiles, loot items, summon items, scanners, trophies, map echoes ...
            if (Has(n, "Projectile", "Trophy", "Scanner", "SummoningItem", "Bait", "SoulOrb", "CraftingItem",
                    "Chestplate", "BreastArmor", "PantsArmor", "Helm", "EchoMap", "Pet", "Beam", "Attack",
                    "Pushback", "Mortar", "Shard", "Stalactite", "Segment", "Bulb", "Leg", "Eye", "Orb",
                    "ImmuneZone", "VoidChaser", "Neuron", "SpawnItem", "Egg", "MapMarker", "MusicSheet",
                    "OracleCard", "Pouch", "Protocol", "Tutorial"))
                return null;

            if (n == "TheCore") return "core";
            if (n.EndsWith("OreBoulder", StringComparison.Ordinal) || n == "AmberBoulder" || n == "CrystalMeteorBoulder") return "oreBoulder";
            if (n.EndsWith("BossStatue", StringComparison.Ordinal)) return "statue";
            if (n.EndsWith("Statue", StringComparison.Ordinal) || n.EndsWith("StatueRaided", StringComparison.Ordinal)) return "statue";
            if (n.EndsWith("Merchant", StringComparison.Ordinal)) return "merchant";
            if (n == "WayPoint") return "waypoint";
            if (n == "Portal" || n.EndsWith("Portal", StringComparison.Ordinal)) return "portal";
            if (n.Contains("Chest")) return "chest";
            if (n.EndsWith("SpawnLocation", StringComparison.Ordinal) || n.EndsWith("TeleportLocation", StringComparison.Ordinal)) return "boss";
            if (IsBoss(n) || n == "CicadaBurrow") return "boss";           // CicadaBurrow: Giant Cicada lair
            if (n == "SummonArea") return "altar";                            // boss summoning arenas
            if (n.StartsWith("Excavation", StringComparison.Ordinal) && n.EndsWith("Door", StringComparison.Ordinal)) return "dungeon";
            if (n.Contains("Altar") || n == "RobotBossActivatorConsole") return "altar";
            if (n.Contains("Shrine") || n.Contains("Idol") || n.Contains("Obelisk") || n.Contains("Relief")
                || n == "BrokenCore" || n.StartsWith("BrokenCoreSentry", StringComparison.Ordinal)) return "shrine";
            return null;
        }

        // Actual boss creatures (not their parts, see the exclusions above).
        static readonly HashSet<string> Bosses = new HashSet<string>
        {
            "BossLarva", "LarvaHiveBoss", "LarvaHiveHalloweenBoss", "SlimeBoss", "PoisonSlimeBoss", "SlipperySlimeBoss",
            "LavaSlimeBoss", "ShamanBoss", "BirdBoss", "OctopusBoss", "ScarabBoss", "HydraBossNature", "HydraBossSea",
            "HydraBossDesert", "HydraBossVoid", "CoreBoss", "WallBoss", "GiantCicadaBoss", "RobotBoss", "KingSlime",
        };

        static bool IsBoss(string n) => Bosses.Contains(n);

        static bool Has(string n, params string[] parts)
        {
            foreach (var p in parts) if (n.Contains(p)) return true;
            return false;
        }

        // "CopperOre" -> "Copper"
        public static string OreKind(string dropName)
        {
            if (string.IsNullOrEmpty(dropName)) return "Unknown";
            return dropName.EndsWith("Ore", StringComparison.Ordinal) && dropName.Length > 3
                ? dropName.Substring(0, dropName.Length - 3) : dropName;
        }

        public struct Obj { public string cat, name; public int x, y; }
        public struct Ore { public string kind; public int x, y, count; }

        public static string ToJson(int worldRadius, List<Obj> objects, List<Ore> ores)
        {
            var sb = new StringBuilder(1 << 20);
            sb.Append("{\"generatedAt\":\"").Append(DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture))
              .Append("\",\"worldRadius\":").Append(worldRadius.ToString(CultureInfo.InvariantCulture))
              .Append(",\"objects\":[");
            for (int i = 0; i < objects.Count; i++)
            {
                var o = objects[i];
                if (i > 0) sb.Append(',');
                sb.Append("\n{\"cat\":\"").Append(Esc(o.cat)).Append("\",\"name\":\"").Append(Esc(o.name))
                  .Append("\",\"x\":").Append(o.x.ToString(CultureInfo.InvariantCulture))
                  .Append(",\"y\":").Append(o.y.ToString(CultureInfo.InvariantCulture)).Append('}');
            }
            sb.Append("],\"ores\":[");
            for (int i = 0; i < ores.Count; i++)
            {
                var o = ores[i];
                if (i > 0) sb.Append(',');
                sb.Append("\n{\"kind\":\"").Append(Esc(o.kind))
                  .Append("\",\"x\":").Append(o.x.ToString(CultureInfo.InvariantCulture))
                  .Append(",\"y\":").Append(o.y.ToString(CultureInfo.InvariantCulture))
                  .Append(",\"count\":").Append(o.count.ToString(CultureInfo.InvariantCulture)).Append('}');
            }
            sb.Append("]}\n");
            return sb.ToString();
        }

        public static string Esc(string s)
        {
            if (s == null) return "";
            var sb = new StringBuilder(s.Length);
            foreach (char c in s)
            {
                if (c == '"' || c == '\\') sb.Append('\\').Append(c);
                else if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                else sb.Append(c);
            }
            return sb.ToString();
        }
    }
}
