using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading;
using PlayerState;
using PugMod;
using Unity.Collections;
using Unity.Entities;
using Unity.Transforms;
using Debug = UnityEngine.Debug;

namespace LivePlayers
{
    // Server-only, READ-ONLY exporter of connected player positions for the map viewer.
    //
    //  * never writes entity data, no Harmony (manifest disableHarmonyPatching=true), no RPCs,
    //    no ECS system of its own: the query runs from IMod.Update() (Loader.Update, once per frame)
    //    at most once per second of REAL time (Stopwatch; the server sets timeScale=0 and disables
    //    SimulationSystemGroup when nobody is connected - ECSManager.Pause - but IMod.Update keeps
    //    running, so an empty list with "paused":true is still written ~every 2 s).
    //  * disconnected players keep their entity with the `Disabled` tag (the game
    //    re-enables it on reconnect); default EntityQuery options exclude Disabled -> only online players.
    //  * map convention: map x = LocalTransform.Position.x, map y = Position.z
    //    (the in-game map uses the player's world position (x, z) the same way).
    //  * file write happens on a thread-pool thread (never blocks the game loop); temp + File.Replace.
    //    We deliberately do NOT use API.ConfigFilesystem.Write: it fsyncs and Thread.Sleep(1000)s up to
    //    10x when a reader holds the file - that would stall the server.
    //  * every failure is caught; each distinct error message is logged once.
    public class LivePlayersMod : IMod
    {
        public const string NAME = "LivePlayers";
        public const string VERSION = "1.0.0";
        const string RelPath = "LivePlayers/players.json";   // relative to <datapath>/mods
        const double IntervalSec = 1.0;
        const double PausedIntervalSec = 2.0;

        static readonly Stopwatch Clock = Stopwatch.StartNew();
        static readonly HashSet<string> LoggedErrors = new HashSet<string>();
        static int _writing;           // 1 while a background write is in flight
        static string _path;           // absolute path of players.json (resolved lazily)
        static bool _pathFailed;

        double _next;
        EntityQuery _q;
        World _qWorld;
        bool _inited;
        int _lastCount = -1;
        bool _lastPaused;

        public void EarlyInit()
        {
            Debug.Log($"[{NAME}] v{VERSION} loaded (read-only, server-only)");
        }

        public void Init()
        {
            if (_inited) return;
            _inited = true;
            Debug.Log($"[{NAME}] init; server api={(API.Server != null)} out={ResolvePath() ?? "(unresolved)"}");
            // IMod.Shutdown only runs on a mod reload, not on process exit -> plain Unity event (no Harmony)
            try { UnityEngine.Application.quitting += WriteStopped; } catch (Exception e) { LogOnce("quitting", e); }
        }

        public void Shutdown() { WriteStopped(); }

        static volatile bool _stopped;

        // tell the viewer immediately that the server went away
        static void WriteStopped()
        {
            try
            {
                if (_stopped) return;
                _stopped = true;
                var sw = Stopwatch.StartNew();
                while (Volatile.Read(ref _writing) != 0 && sw.ElapsedMilliseconds < 300) Thread.Sleep(5);
                WriteNow(BuildJson(new List<string>(), paused: false, stopped: true), sync: true);
                Debug.Log($"[{NAME}] wrote stopped marker");
            }
            catch (Exception e) { LogOnce("stopped", e); }
        }

        public void ModObjectLoaded(UnityEngine.Object obj) { }

        public void Update()
        {
            try
            {
                if (_stopped) return;
                double now = Clock.Elapsed.TotalSeconds;
                if (now < _next) return;
                _next = now + IntervalSec;

                if (API.Server == null) return;          // not a server process (or not ready)
                World w = API.Server.World;
                if (w == null || !w.IsCreated) return;

                bool paused = IsPaused(w);
                if (paused && _lastPaused) _next = now + PausedIntervalSec;

                var players = paused ? new List<string>() : ReadPlayers(w);
                if (players == null) return;
                if (players.Count != _lastCount || paused != _lastPaused)
                    Debug.Log($"[{NAME}] players={players.Count} paused={paused}");
                _lastCount = players.Count;
                _lastPaused = paused;
                WriteNow(BuildJson(players, paused, stopped: false), sync: false);
            }
            catch (Exception e) { LogOnce("update", e); }
        }

        // ECSManager.Pause disables the server SimulationSystemGroup while nobody is connected.
        static bool IsPaused(World w)
        {
            try
            {
                var g = w.GetExistingSystemManaged<SimulationSystemGroup>();
                return g != null && !g.Enabled;
            }
            catch { return false; }
        }

        List<string> ReadPlayers(World w)
        {
            var em = w.EntityManager;
            if (_qWorld != w)
            {
                _q = em.CreateEntityQuery(
                    ComponentType.ReadOnly<PlayerGhost>(),
                    ComponentType.ReadOnly<LocalTransform>());
                _qWorld = w;
            }
            var list = new List<string>();
            using (var ents = _q.ToEntityArray(Allocator.Temp))
            {
                for (int i = 0; i < ents.Length; i++)
                {
                    try { list.Add(PlayerJson(em, ents[i])); }
                    catch (Exception e) { LogOnce("player", e); }
                }
            }
            return list;
        }

        static string PlayerJson(EntityManager em, Entity e)
        {
            var ghost = em.GetComponentData<PlayerGhost>(e);
            var pos = em.GetComponentData<LocalTransform>(e).Position;

            string name = null;
            if (em.HasComponent<PlayerCustomizationCD>(e))
                name = em.GetComponentData<PlayerCustomizationCD>(e).customization.name.ToString();
            if (string.IsNullOrEmpty(name)) name = ghost.onlineName.ToString();
            if (string.IsNullOrEmpty(name)) name = "Player " + ghost.playerIndex;

            string hp = "null", maxHp = "null", dead = "null";
            if (em.HasComponent<HealthCD>(e))
            {
                var h = em.GetComponentData<HealthCD>(e);
                int max = h.maxHealth;
                try
                {
                    if (em.HasBuffer<SummarizedConditionEffectsBuffer>(e))
                    {
                        var buf = em.GetBuffer<SummarizedConditionEffectsBuffer>(e, true);
                        if (buf.Length > 68) max = h.GetMaxHealthWithConditions(buf);
                    }
                }
                catch (Exception ex) { LogOnce("maxhp", ex); }
                hp = h.health.ToString(CultureInfo.InvariantCulture);
                maxHp = max.ToString(CultureInfo.InvariantCulture);
            }
            if (em.HasComponent<PlayerStateCD>(e))
                dead = em.GetComponentData<PlayerStateCD>(e).HasAnyState(PlayerStateEnum.Death) ? "true" : "false";

            var sb = new StringBuilder(160);
            sb.Append("{\"id\":").Append(Str(ghost.playerGuid.ToString()))
              .Append(",\"name\":").Append(Str(name))
              .Append(",\"x\":").Append(F(pos.x))
              .Append(",\"y\":").Append(F(pos.z))
              .Append(",\"hp\":").Append(hp)
              .Append(",\"maxHp\":").Append(maxHp)
              .Append(",\"dead\":").Append(dead)
              .Append('}');
            return sb.ToString();
        }

        static string BuildJson(List<string> players, bool paused, bool stopped)
        {
            var sb = new StringBuilder(64 + players.Count * 160);
            sb.Append("{\"utc\":").Append(Str(DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ", CultureInfo.InvariantCulture)))
              .Append(",\"version\":").Append(Str(VERSION))
              .Append(",\"paused\":").Append(paused ? "true" : "false")
              .Append(",\"stopped\":").Append(stopped ? "true" : "false")
              .Append(",\"players\":[");
            for (int i = 0; i < players.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(players[i]);
            }
            sb.Append("]}");
            return sb.ToString();
        }

        static void WriteNow(string json, bool sync)
        {
            string path = ResolvePath();
            if (path == null) return;
            byte[] data = new UTF8Encoding(false).GetBytes(json);
            if (sync)
            {
                if (Interlocked.CompareExchange(ref _writing, 1, 0) != 0) return;
                WriteFile(path, data);
                return;
            }
            if (Interlocked.CompareExchange(ref _writing, 1, 0) != 0) return;   // previous write still busy: skip this tick
            if (!ThreadPool.QueueUserWorkItem(_ => WriteFile(path, data)))
                Interlocked.Exchange(ref _writing, 0);
        }

        static void WriteFile(string path, byte[] data)
        {
            string tmp = path + ".tmp";
            try
            {
                File.WriteAllBytes(tmp, data);
                if (File.Exists(path)) File.Replace(tmp, path, null, true);
                else File.Move(tmp, path);
            }
            catch (Exception e)
            {
                // typically a sharing violation while the viewer is reading: just skip this tick
                LogOnce("write", e);
                try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }
            }
            finally { Interlocked.Exchange(ref _writing, 0); }
        }

        static string ResolvePath()
        {
            if (_path != null || _pathFailed) return _path;
            try
            {
                string p = null;
                if (API.ConfigFilesystem is Pug.Platform.StandaloneFilesystem fs)
                    p = fs.Rel2Abs(RelPath);          // <datapath>/mods/LivePlayers/players.json
                if (p == null) { _pathFailed = true; LogOnce("path", new Exception("ConfigFilesystem is not StandaloneFilesystem")); return null; }
                Directory.CreateDirectory(Path.GetDirectoryName(p));
                _path = p;
            }
            catch (Exception e) { _pathFailed = true; LogOnce("path", e); }
            return _path;
        }

        static void LogOnce(string where, Exception e)
        {
            string key = where + ":" + e.GetType().Name + ":" + e.Message;
            lock (LoggedErrors)
            {
                if (LoggedErrors.Count > 200 || !LoggedErrors.Add(key)) return;
            }
            Debug.LogWarning($"[{NAME}] {where} failed (logged once): {e.GetType().Name}: {e.Message}");
        }

        static string F(float v) =>
            float.IsNaN(v) || float.IsInfinity(v) ? "null" : v.ToString("0.###", CultureInfo.InvariantCulture);

        static string Str(string s)
        {
            var sb = new StringBuilder(s.Length + 2);
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            return sb.Append('"').ToString();
        }
    }
}
