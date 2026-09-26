using System;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using System.Text;
using Pug.Platform;
using Pug.UnityExtensions;
using PugWorldGen;
using PugTilemap;
using Unity.Collections;
using Unity.Entities;
using Unity.Mathematics;
using Unity.NetCode;
using Unity.Transforms;
using UnityEngine;

namespace FullMapGen
{
    // Plain SystemBase with NO Entities.ForEach / SystemAPI / IJobEntity -> needs NO
    // source-generated .g.cs. Only EntityManager + manually built EntityQuery are used.
    // Auto-registered into the SERVER world by the default bootstrap (same as the shipped
    // InstantPortalCharge PortalChargeSystem, which also relies on attributes only).
    [WorldSystemFilter(WorldSystemFilterFlags.ServerSimulation)]
    [UpdateInGroup(typeof(SimulationSystemGroup))]
    public partial class FullMapGenSystem : SystemBase
    {
        // ---- tunables -------------------------------------------------------------
        const int CellSize = 256;             // spawn cell = 4x4 submaps (RespawnPartialSubMapsInitSystem.GetSpawnCellIndex)
        const int SubMapSize = 64;
        const int MaxInflightAreas = 2;       // game itself only runs ONE SpawnProceduralInArea at a time + 4 GPU requests
        const double EnqueueCooldownSec = 1.0;
        const double CellStallTimeoutSec = 120.0;
        const double IdleToFinishSec = 20.0;
        const double StartupDelaySec = 15.0;
        const double ProgressLogSec = 10.0;
        const int PartsRenderedPerFrame = 1;
        const double SaveTimeoutSec = 120.0;
        const int AutoRingStep = 256;         // auto extent: grow the plan by one spawn-cell ring per step
        const int AutoMaxRadius = 8192;       // hard cap for auto extent (safety)
        const double AutoContentMin = 0.0005; // band with < 0.05% real terrain = edge-only
        const int OreBucket = 16;             // ores aggregated into 16x16-tile buckets
        const int StatBand = 128;             // tile histogram radius band (tiles)
        const int BossMergeDist = 48;         // boss entities of the same name closer than this = one marker

        enum Phase { Inert, WaitReady, Generate, Settle, Render, WaitSave, Done, Aborted }
        Phase _phase = Phase.Inert;

        // ---- queries -------------------------------------------------------------
        EntityQuery _qPlayers, _qConnections, _qRegistry, _qTileUpdate, _qWorldGenType, _qReady1, _qReady2, _qReady3;
        EntityQuery _qInflight, _qDb;

        TileAccessor _tiles;

        // ---- plan / progress -----------------------------------------------------
        readonly List<int2> _cells = new List<int2>();   // spawn cell indices, sorted by distance (appended per auto ring)
        readonly HashSet<int2> _cellSet = new HashSet<int2>();
        int _nextCell;
        readonly Dictionary<int2, double> _cellEnqueuedAt = new Dictionary<int2, double>();
        int _cellsSkippedExisting, _cellsEnqueued, _cellsStalled;
        int _worldRadius, _boundRadius, _planRadius;
        bool _auto;
        readonly List<string> _autoLog = new List<string>();
        double _t0, _lastEnqueue, _lastProgress, _idleSince = -1, _saveStart;

        readonly List<int2> _parts = new List<int2>();   // 256x256 map part keys
        int _nextPart, _partsSubmitted;

        NativeParallelHashSet<TileCD> _drawable;
        ShareMapServerSystem _share;
        MethodInfo _receiveMapData;
        FieldInfo _lastMapChangeTime, _mapFileHandle, _mapFileField;

        // ---- stats / POIs ----------------------------------------------------------
        // tile histogram key: band<<40 | tileset<<20 | tileType<<1 | emptyStack
        readonly Dictionary<long, long> _tileHist = new Dictionary<long, long>();
        readonly Dictionary<long, uint> _kindColor = new Dictionary<long, uint>();
        readonly Dictionary<(int, int, int), int> _oreBuckets = new Dictionary<(int, int, int), int>();   // (tileset, bx, by) -> tiles
        readonly Dictionary<int, string> _oreKindByTileset = new Dictionary<int, string>();
        readonly List<string> _oreSamples = new List<string>();
        long _oreTiles, _oreTilesVisible;
        BlobAssetReference<PugDatabase.PugDatabaseBank> _db;

        protected override void OnCreate()
        {
            if (!FullMapGenGuard.Armed)
            {
                Enabled = false;   // inert on any normal server
                return;
            }
            var em = EntityManager;
            _qPlayers = em.CreateEntityQuery(ComponentType.ReadOnly<PlayerGhost>());
            _qConnections = em.CreateEntityQuery(ComponentType.ReadOnly<NetworkId>());
            _qRegistry = em.CreateEntityQuery(new EntityQueryDesc { All = new[] { ComponentType.ReadOnly<SubMapRegistry>() }, Options = EntityQueryOptions.IncludeSystems });
            _qTileUpdate = em.CreateEntityQuery(new EntityQueryDesc { All = new[] { ComponentType.ReadWrite<TileUpdateBuffer>() }, Options = EntityQueryOptions.IncludeSystems });
            _qWorldGenType = em.CreateEntityQuery(new EntityQueryDesc { All = new[] { ComponentType.ReadOnly<WorldGenerationTypeCD>() }, Options = EntityQueryOptions.IncludeSystems });
            _qReady1 = em.CreateEntityQuery(new EntityQueryDesc { All = new[] { ComponentType.ReadOnly<InitialLoadingDoneCD>() }, Options = EntityQueryOptions.IncludeSystems });
            _qReady2 = em.CreateEntityQuery(new EntityQueryDesc { All = new[] { ComponentType.ReadOnly<WorldHasBeenDeserializedCD>() }, Options = EntityQueryOptions.IncludeSystems });
            _qReady3 = em.CreateEntityQuery(new EntityQueryDesc { All = new[] { ComponentType.ReadOnly<UniqueDungeonInitDoneCD>() }, Options = EntityQueryOptions.IncludeSystems });
            _qDb = em.CreateEntityQuery(new EntityQueryDesc { All = new[] { ComponentType.ReadOnly<PugDatabase.DatabaseBankCD>() }, Options = EntityQueryOptions.IncludeSystems });

            // "generation pipeline busy" = any entity carrying one of the per-area pipeline tags.
            var any = new List<ComponentType>
            {
                ComponentType.ReadOnly<SpawnUniqueDungeonAtAreaCD>(),
                ComponentType.ReadOnly<SpawnDungeonsAndScenesInArea>(),
                ComponentType.ReadOnly<SpawnProceduralInAreaCD>(),
                ComponentType.ReadOnly<SpawnRootsInAreaCD>(),
            };
            // ProceduralTerrainDataCD is `internal` in WorldGen.dll -> resolve by reflection.
            Type ptd = FindType("ProceduralTerrainDataCD");
            if (ptd != null) any.Add(ComponentType.ReadOnly(ptd));
            _qInflight = em.CreateEntityQuery(new EntityQueryDesc { Any = any.ToArray(), Options = EntityQueryOptions.IncludeDisabledEntities });

            _phase = Phase.WaitReady;
            Log("armed; waiting for world ready");
        }

        protected override void OnDestroy()
        {
            if (_drawable.IsCreated) _drawable.Dispose();
        }

        protected override void OnUpdate()
        {
            double now = SystemAPIFreeTime();
            if (_phase == Phase.Inert || _phase == Phase.Done || _phase == Phase.Aborted) return;

            // Hard interlock: the instant a real player/connection appears, stop forever.
            if (!_qPlayers.IsEmptyIgnoreFilter || !_qConnections.IsEmptyIgnoreFilter)
            {
                Abort("player/connection detected - this mod must only run on a disposable, player-less server");
                return;
            }

            try
            {
                switch (_phase)
                {
                    case Phase.WaitReady: StepWaitReady(now); break;
                    case Phase.Generate: StepGenerate(now); break;
                    case Phase.Settle: StepSettle(now); break;
                    case Phase.Render: StepRender(now); break;
                    case Phase.WaitSave: StepWaitSave(now); break;
                }
            }
            catch (Exception e)
            {
                Abort("exception in phase " + _phase + ": " + e);
            }
        }

        // ----------------------------------------------------------------------------
        void StepWaitReady(double now)
        {
            if (_qRegistry.IsEmptyIgnoreFilter || _qTileUpdate.IsEmptyIgnoreFilter || _qWorldGenType.IsEmptyIgnoreFilter
                || _qReady1.IsEmptyIgnoreFilter || _qReady2.IsEmptyIgnoreFilter || _qReady3.IsEmptyIgnoreFilter)
            { _t0 = now; return; }
            if (_t0 == 0) _t0 = now;
            if (now - _t0 < StartupDelaySec) return;   // let RespawnPartialSubMapsInitSystem process loaded submaps first

            var wgt = _qWorldGenType.GetSingleton<WorldGenerationTypeCD>().Value;
            if (wgt != WorldGenerationType.FullRelease) { Abort("world generation type " + wgt + " not supported (FullRelease only)"); return; }

            _share = World.GetExistingSystemManaged<ShareMapServerSystem>();
            if (_share == null) { Abort("ShareMapServerSystem not found in server world"); return; }
            const BindingFlags F = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
            _receiveMapData = typeof(ShareMapServerSystem).GetMethod("ReceiveMapData", F);
            _lastMapChangeTime = typeof(ShareMapServerSystem).GetField("_lastMapChangeTime", F);
            _mapFileHandle = typeof(ShareMapServerSystem).GetField("_mapFileHandle", F);
            _mapFileField = typeof(ShareMapServerSystem).GetField("_mapFile", F);
            if (_receiveMapData == null || _lastMapChangeTime == null) { Abort("reflection targets missing (game updated?)"); return; }

            var wp = Manager.saves.GetWorldGenerationParametersReference();
            _boundRadius = Manager.worldGen.GetScaledWorldRadiusBound();   // ceil(max(ring4+ring4Chaos, ring4+3*northBlob) * worldScale) + 32
            Log($"worldgen params: worldScale={wp.worldScale} ring1={wp.ring1Size} ring2={wp.ring2Size} ring3={wp.ring3Size} ring4={wp.ring4Size} ring4Chaos={wp.ring4Chaos} northBlob={wp.northBlobRadius} -> bound={_boundRadius}");
            // radius=N may be smaller OR larger than the game's bound (larger = probe beyond the edge).
            // No override -> auto: start at the bound and keep growing while the outermost band has real terrain.
            _auto = FullMapGenGuard.RadiusOverride <= 0;
            _worldRadius = _auto ? _boundRadius : FullMapGenGuard.RadiusOverride;
            if (!_qDb.IsEmptyIgnoreFilter) _db = _qDb.GetSingleton<PugDatabase.DatabaseBankCD>().databaseBankBlob;
            BuildDrawable();
            AddCellsUpTo(_worldRadius);
            _tiles = new TileAccessor(this);
            FullMapGenGuard.WriteText(FullMapGenGuard.RunningFile, DateTime.UtcNow.ToString("o"));
            Log($"world radius bound={_boundRadius} plan radius={_worldRadius} auto={_auto}; spawn cells planned={_cells.Count}");
            _t0 = now;
            _phase = Phase.Generate;
        }

        static float CellNearestDist(int2 c)
        {
            // spawn cell c covers tiles [256c-128, 256c+127] (see RespawnPartialSubMapsInitSystem)
            float2 lo = new float2(c.x * CellSize - 128, c.y * CellSize - 128);
            float2 hi = lo + CellSize - 1;
            return math.length(math.clamp(float2.zero, lo, hi));
        }

        // Appends (sorted by distance) every not-yet-planned cell touching the disc of radius R.
        int AddCellsUpTo(int R)
        {
            int K = (R + CellSize / 2) / CellSize + 1;
            var add = new List<int2>();
            for (int cy = -K; cy <= K; cy++)
                for (int cx = -K; cx <= K; cx++)
                {
                    var c = new int2(cx, cy);
                    if (!_cellSet.Contains(c) && CellNearestDist(c) <= R) add.Add(c);
                }
            add.Sort((a, b) => math.lengthsq(a).CompareTo(math.lengthsq(b)));
            foreach (var c in add) { _cellSet.Add(c); _cells.Add(c); }
            _planRadius = R;
            return add.Count;
        }

        void BuildParts()
        {
            int R = _worldRadius;
            int P = R / 256 + 2;
            for (int py = -P; py < P; py++)
                for (int px = -P; px < P; px++)
                {
                    float2 lo = new float2(px * 256, py * 256), hi = lo + 255;
                    float2 nearest = math.clamp(float2.zero, lo, hi);
                    if (math.length(nearest) <= R) _parts.Add(new int2(px, py));
                }
        }

        void BuildDrawable()
        {
            _drawable = new NativeParallelHashSet<TileCD>(512, Allocator.Persistent);
            var table = Resources.Load<TileTypeColorTable>("TileTypeColorTable");   // same as MapUpdateSystem.OnCreate
            foreach (var set in table.tileSetColors)
                foreach (var c in set.tileColors)
                    _drawable.Add(new TileCD { tileset = (int)set.pugMapTileset, tileType = c.tileType });
        }

        // ----------------------------------------------------------------------------
        void StepGenerate(double now)
        {
            _tiles.Update(this);
            int inflight = _qInflight.CalculateEntityCountWithoutFiltering();

            // retire enqueued cells whose submaps now exist
            if (_cellEnqueuedAt.Count > 0)
            {
                var finished = new List<int2>();
                foreach (var kv in _cellEnqueuedAt)
                {
                    if (CellFullyInitialized(kv.Key)) finished.Add(kv.Key);
                    else if (now - kv.Value > CellStallTimeoutSec && inflight == 0) { finished.Add(kv.Key); _cellsStalled++; Log($"WARN cell {kv.Key} stalled, skipping"); }
                }
                foreach (var c in finished) _cellEnqueuedAt.Remove(c);
            }

            if (inflight + _cellEnqueuedAt.Count < MaxInflightAreas && now - _lastEnqueue >= EnqueueCooldownSec && _nextCell < _cells.Count)
            {
                var buf = EntityManager.GetBuffer<TileUpdateBuffer>(_qTileUpdate.GetSingletonEntity());
                while (_nextCell < _cells.Count && inflight + _cellEnqueuedAt.Count < MaxInflightAreas)
                {
                    int2 cell = _cells[_nextCell++];
                    if (CellAnyInitialized(cell))
                    {
                        // Already known to the save: loaded cells are (re)generated by
                        // RespawnPartialSubMapsInitSystem at load. Touching them would create
                        // EMPTY submaps in an already-triggered cell -> never do that.
                        _cellsSkippedExisting++;
                        continue;
                    }
                    // Same mechanism as SpawnBasedOnPlayersSystem: Add with tileType none just
                    // creates the submap entity (ApplyToNewSubmapsJob, wasCreatedThisSession=true),
                    // which triggers a NEW 256x256 spawn cell -> dungeons/scenes -> GPU terrain.
                    buf.Add(new TileUpdateBuffer { command = TileUpdateBuffer.Command.Add, position = cell * CellSize });
                    _cellEnqueuedAt[cell] = now;
                    _cellsEnqueued++;
                }
                _lastEnqueue = now;
            }

            if (now - _lastProgress >= ProgressLogSec)
            {
                _lastProgress = now;
                double el = now - _t0;
                double frac = _cells.Count == 0 ? 1 : (double)_nextCell / _cells.Count;
                double eta = frac > 0.01 ? el / frac - el : -1;
                string msg = $"GEN r={_planRadius} cells {_nextCell}/{_cells.Count} ({frac * 100:F1}%) enqueued={_cellsEnqueued} existing={_cellsSkippedExisting} stalled={_cellsStalled} inflight={inflight} pending={_cellEnqueuedAt.Count} elapsed={el:F0}s eta={eta:F0}s";
                Log(msg);
                WriteProgress("generate", frac, msg);
            }

            if (_nextCell >= _cells.Count && _cellEnqueuedAt.Count == 0)
            {
                if (inflight == 0)
                {
                    if (_idleSince < 0) _idleSince = now;
                    if (now - _idleSince >= IdleToFinishSec)
                    {
                        _idleSince = -1;
                        if (_auto && TryExtend()) return;
                        Log("generation idle -> settle"); _phase = Phase.Settle; _t0 = now;
                    }
                }
                else _idleSince = -1;
            }
        }

        // "World edge" tile: nothing stored at all (the accessor then reports its DefaultTile = Obsidian wall)
        // or an Obsidian wall (Tileset.Obsidian, obsolete as a biome in full release). Everything else is terrain.
        static bool IsEdgeTile(TileCD top, int stackCount)
            => stackCount == 0 || (top.tileset == (int)Tileset.Obsidian && top.tileType == TileType.wall);

        // Scans the band (rIn, rOut] (every 2nd tile per axis). Returns the number of non-edge tiles.
        long CountContentInBand(int rIn, int rOut, out long scanned)
        {
            scanned = 0; long content = 0;
            long in2 = (long)rIn * rIn, out2 = (long)rOut * rOut;
            for (int y = -rOut; y <= rOut; y += 2)
                for (int x = -rOut; x <= rOut; x += 2)
                {
                    long d2 = (long)x * x + (long)y * y;
                    if (d2 <= in2 || d2 > out2) continue;
                    var p = new int2(x, y);
                    if (!_tiles.IsInitialized(p)) continue;
                    scanned++;
                    int cnt = _tiles.Count(p);
                    TileCD top = cnt == 0 ? default : _tiles.GetTopFromSelection(p, _drawable);
                    if (!IsEdgeTile(top, cnt)) content++;
                }
            return content;
        }

        // Auto extent: if the outermost band still has real terrain, plan one more ring of cells.
        bool TryExtend()
        {
            EntityManager.CompleteAllTrackedJobs();
            _tiles.Update(this);
            int rOut = _planRadius, rIn = Math.Max(0, rOut - AutoRingStep);
            long content = CountContentInBand(rIn, rOut, out long scanned);
            double frac = scanned == 0 ? 0 : (double)content / scanned;
            string line = $"band ({rIn},{rOut}]: sampled={scanned} content={content} ({(frac * 100).ToString("F3", CultureInfo.InvariantCulture)}%)";
            _autoLog.Add(line);
            Log("auto " + line);
            if (frac >= AutoContentMin && rOut + AutoRingStep <= AutoMaxRadius)
            {
                int added = AddCellsUpTo(rOut + AutoRingStep);
                _worldRadius = _planRadius;
                Log($"auto: terrain continues -> radius {_worldRadius} (+{added} cells)");
                if (added > 0) return true;
            }
            // Edge reached: this band is edge-only, so render up to the last band that still had terrain
            // (the inner edge of this band = previous plan radius; never below the game's own bound).
            _worldRadius = Math.Max(_boundRadius, rIn);
            Log($"auto: edge reached, render radius={_worldRadius}");
            return false;
        }

        void StepSettle(double now)
        {
            // Environment objects / roots / water spreading may still be applying tiles. Give it time.
            if (_qInflight.CalculateEntityCountWithoutFiltering() > 0) { _t0 = now; return; }
            if (now - _t0 < 10) return;

            BuildParts();
            Log($"render start: {_parts.Count} map parts, radius={_worldRadius}, drawable tile kinds={_drawable.Count()}");
            _t0 = now;
            _phase = Phase.Render;
        }

        // ----------------------------------------------------------------------------
        void StepRender(double now)
        {
            EntityManager.CompleteAllTrackedJobs();
            _tiles.Update(this);
            var colors = World.GetExistingSystemManaged<TileTypeColorLookupSystem>().CreateLookupHelper();
            PugColorARGB32 stamp = MapUI.CurrentMapTimestampColor;   // (unix seconds >> 2) packed RGBA, same as clients

            for (int n = 0; n < PartsRenderedPerFrame && _nextPart < _parts.Count; n++)
            {
                int2 key = _parts[_nextPart++];
                var mapTex = new Texture2D(256, 256, TextureFormat.ARGB32, false, false);
                var tsTex = new Texture2D(256, 256, TextureFormat.ARGB32, false, true);
                try
                {
                    var px = mapTex.GetPixelData<PugColorARGB32>(0);
                    var ts = tsTex.GetPixelData<PugColorARGB32>(0);
                    var clear = new PugColorARGB32(0, 0, 0, 0);
                    int set = 0;
                    int2 origin = key * 256;
                    for (int y = 0; y < 256; y++)
                        for (int x = 0; x < 256; x++)
                        {
                            // texture row y = world origin.y + y. Unity textures are bottom-up and EncodeToPNG
                            // writes the top row first, so PNG row r <-> world y = origin.y + 255 - r (+y north).
                            int i = y * 256 + x;          // UpdateMapPartJob.SetColorAtPosInternal indexing
                            int2 wp = origin + new int2(x, y);
                            if (!_tiles.IsInitialized(wp)) { px[i] = clear; ts[i] = clear; continue; }
                            TileCD top = _tiles.GetTopFromSelection(wp, _drawable);
                            Color32 c = colors.GetColorByTileType(top.tileset, top.tileType);
                            px[i] = c;
                            ts[i] = stamp;
                            set++;
                            Accumulate(wp, top, c);
                        }
                    if (set == 0) continue;
                    mapTex.Apply(false);
                    tsTex.Apply(false);
                    var part = new MapPartSerialized(mapTex.EncodeToPNG(), tsTex.EncodeToPNG(), default);
                    part.RecomputeTimestampHash();
                    byte[] packed = MapPackingHelper.PackIntoBuffer(key, part);
                    // Exactly the path a client upload takes (side channel MapData -> ReceiveMapData):
                    // merges per-pixel by timestamp and arms the 0.5 s delayed save.
                    _receiveMapData.Invoke(_share, new object[] { packed });
                    _partsSubmitted++;
                }
                finally
                {
                    UnityEngine.Object.Destroy(mapTex);
                    UnityEngine.Object.Destroy(tsTex);
                }
            }

            if (now - _lastProgress >= ProgressLogSec)
            {
                _lastProgress = now;
                double frac = _parts.Count == 0 ? 1 : (double)_nextPart / _parts.Count;
                string msg = $"RENDER parts {_nextPart}/{_parts.Count} ({frac * 100:F1}%) submitted={_partsSubmitted}";
                Log(msg);
                WriteProgress("render", frac, msg);
            }

            if (_nextPart >= _parts.Count)
            {
                Log($"render done, submitted {_partsSubmitted} parts; writing POIs, then waiting for ShareMapServerSystem save");
                try { WritePoisAndStats(); }
                catch (Exception e) { Log("WARN pois/stats failed: " + e); }
                _saveStart = now;
                _phase = Phase.WaitSave;
            }
        }

        // per rendered tile: histogram by radius band + ore buckets
        void Accumulate(int2 wp, TileCD top, Color32 c)
        {
            int cnt = _tiles.Count(wp);
            int band = (int)(math.length(new float2(wp)) / StatBand);
            long kind = ((long)(top.tileset & 0xFFFF) << 20) | ((long)((int)top.tileType & 0x3FF) << 1);
            long hk = ((long)band << 40) | kind | (cnt == 0 ? 1L : 0L);
            _tileHist.TryGetValue(hk, out long hv); _tileHist[hk] = hv + 1;
            if (!_kindColor.ContainsKey(kind)) _kindColor[kind] = ((uint)c.r << 16) | ((uint)c.g << 8) | c.b;

            if (cnt == 0 || !_tiles.GetType(wp, TileType.ore, out TileCD ore)) return;
            _oreTiles++;
            bool visible = top.tileType == TileType.ore;
            if (visible) _oreTilesVisible++;
            var bk = (ore.tileset, FloorDiv(wp.x, OreBucket), FloorDiv(wp.y, OreBucket));
            _oreBuckets.TryGetValue(bk, out int oc); _oreBuckets[bk] = oc + 1;
            if (visible && _oreSamples.Count < 40 && (_oreTiles % 997) == 1)
                _oreSamples.Add("{\"x\":" + wp.x + ",\"y\":" + wp.y + ",\"kind\":\"" + OreKindOf(ore.tileset)
                    + "\",\"rgb\":\"" + c.r.ToString("x2") + c.g.ToString("x2") + c.b.ToString("x2") + "\"}");
        }

        void StepWaitSave(double now)
        {
            double lmc = (double)_lastMapChangeTime.GetValue(_share);
            bool saved = lmc < 0;
            if (!saved && now - _saveStart > SaveTimeoutSec && _mapFileHandle != null && _mapFileField != null)
            {
                // Fallback: do what ShareMapServerSystem.OnUpdate does itself.
                object handle = _mapFileHandle.GetValue(_share);
                object mapFile = _mapFileField.GetValue(_share);
                byte[] data = FilesystemManager.SerializeToBinary((MapFile)mapFile);
                var write = handle.GetType().GetMethod("Write", new[] { typeof(byte[]), typeof(bool), typeof(bool), typeof(bool) });
                write.Invoke(handle, new object[] { data, false, true, false });
                _lastMapChangeTime.SetValue(_share, -1.0);
                Log("WARN save fallback used");
                saved = true;
            }
            if (!saved) return;
            if (now - _saveStart < 5) return;   // small grace for the async file write

            string json = "{\"ok\":true,\"worldRadius\":" + _worldRadius + ",\"boundRadius\":" + _boundRadius
                + ",\"auto\":" + (_auto ? "true" : "false") + ",\"cells\":" + _cells.Count
                + ",\"cellsEnqueued\":" + _cellsEnqueued + ",\"cellsExisting\":" + _cellsSkippedExisting
                + ",\"cellsStalled\":" + _cellsStalled + ",\"partsSubmitted\":" + _partsSubmitted
                + ",\"utc\":\"" + DateTime.UtcNow.ToString("o") + "\"}";
            FullMapGenGuard.WriteText(FullMapGenGuard.DoneFile, json);
            Log("DONE " + json);
            _phase = Phase.Done;
        }

        // ----------------------------------------------------------------------------
        static int FloorDiv(int a, int b) => a >= 0 ? a / b : -((-a + b - 1) / b);

        string OreKindOf(int tileset)
        {
            if (_oreKindByTileset.TryGetValue(tileset, out string k)) return k;
            string name = null;
            if (_db.IsCreated)
            {
                var od = PugDatabase.GetObjectData(tileset, TileType.ore, _db);   // same lookup DropLootSystem uses for ore drops
                if (od.objectID != ObjectID.None) name = od.objectID.ToString();
            }
            k = name != null ? FullMapGenPoi.OreKind(name) : "Tileset" + ((Tileset)tileset).ToString();
            _oreKindByTileset[tileset] = k;
            return k;
        }

        void WritePoisAndStats()
        {
            // ---- objects: every server entity with an object id + position ----
            var q = EntityManager.CreateEntityQuery(new EntityQueryDesc
            {
                All = new[] { ComponentType.ReadOnly<ObjectDataCD>(), ComponentType.ReadOnly<LocalTransform>() },
                Options = EntityQueryOptions.IncludeDisabledEntities
            });
            var ods = q.ToComponentDataArray<ObjectDataCD>(Allocator.Temp);
            var lts = q.ToComponentDataArray<LocalTransform>(Allocator.Temp);
            var census = new Dictionary<string, int>();
            var seen = new HashSet<(string, int, int)>();
            var objects = new List<FullMapGenPoi.Obj>();
            var nameCache = new Dictionary<ObjectID, string>();
            for (int i = 0; i < ods.Length; i++)
            {
                ObjectID id = ods[i].objectID;
                if (!nameCache.TryGetValue(id, out string name)) { name = id.ToString(); nameCache[id] = name; }
                census.TryGetValue(name, out int cc); census[name] = cc + 1;
                string cat = FullMapGenPoi.Categorize(name);
                if (cat == null) continue;
                float3 pos = lts[i].Position;
                int x = (int)math.round(pos.x), y = (int)math.round(pos.z);
                // dedupe: same object id on the same or an adjacent tile = one multi-tile object / duplicate entity.
                // Bosses: one marker per name within BossMergeDist tiles (multi-entity bodies, e.g. WallBoss).
                bool dup = false;
                for (int dy = -1; dy <= 1 && !dup; dy++)
                    for (int dx = -1; dx <= 1 && !dup; dx++)
                        if (seen.Contains((name, x + dx, y + dy))) dup = true;
                if (!dup && cat == "boss")
                    foreach (var o in objects)
                        if (o.name == name && math.abs(o.x - x) <= BossMergeDist && math.abs(o.y - y) <= BossMergeDist) { dup = true; break; }
                if (dup) continue;
                seen.Add((name, x, y));
                objects.Add(new FullMapGenPoi.Obj { cat = cat, name = name, x = x, y = y });
            }
            int entityTotal = ods.Length;
            ods.Dispose(); lts.Dispose();
            objects.Sort((a, b) => a.cat != b.cat ? string.CompareOrdinal(a.cat, b.cat) : a.x != b.x ? a.x.CompareTo(b.x) : a.y.CompareTo(b.y));

            // ---- ores: one entry per (kind, 16x16 bucket), bucket-center coordinates ----
            var oreAgg = new Dictionary<(string, int, int), int>();
            foreach (var kv in _oreBuckets)
            {
                var key = (OreKindOf(kv.Key.Item1), kv.Key.Item2, kv.Key.Item3);
                oreAgg.TryGetValue(key, out int c0); oreAgg[key] = c0 + kv.Value;
            }
            var ores = new List<FullMapGenPoi.Ore>();
            var oreTotals = new Dictionary<string, long>();
            foreach (var kv in oreAgg)
            {
                ores.Add(new FullMapGenPoi.Ore { kind = kv.Key.Item1, x = kv.Key.Item2 * OreBucket + OreBucket / 2, y = kv.Key.Item3 * OreBucket + OreBucket / 2, count = kv.Value });
                oreTotals.TryGetValue(kv.Key.Item1, out long t0); oreTotals[kv.Key.Item1] = t0 + kv.Value;
            }
            ores.Sort((a, b) => a.kind != b.kind ? string.CompareOrdinal(a.kind, b.kind) : a.x != b.x ? a.x.CompareTo(b.x) : a.y.CompareTo(b.y));

            FullMapGenGuard.WriteText(FullMapGenGuard.PoisFile, FullMapGenPoi.ToJson(_worldRadius, objects, ores));
            var catCount = new Dictionary<string, int>();
            foreach (var o in objects) { catCount.TryGetValue(o.cat, out int c1); catCount[o.cat] = c1 + 1; }
            Log($"pois: entities={entityTotal} objects={objects.Count} ({Join(catCount)}) oreBuckets={ores.Count} oreTiles={_oreTiles} ({Join(oreTotals)})");

            // ---- stats (diagnostics for the world-extent question) ----
            var sb = new StringBuilder();
            sb.Append("{\"boundRadius\":").Append(_boundRadius).Append(",\"worldRadius\":").Append(_worldRadius)
              .Append(",\"auto\":").Append(_auto ? "true" : "false").Append(",\"statBand\":").Append(StatBand)
              .Append(",\"entities\":").Append(entityTotal).Append(",\"autoLog\":[");
            for (int i = 0; i < _autoLog.Count; i++) sb.Append(i > 0 ? "," : "").Append('"').Append(FullMapGenPoi.Esc(_autoLog[i])).Append('"');
            sb.Append("],\"oreTiles\":").Append(_oreTiles).Append(",\"oreTilesVisible\":").Append(_oreTilesVisible)
              .Append(",\"oreKindByTileset\":{");
            bool first = true;
            foreach (var kv in _oreKindByTileset) { sb.Append(first ? "" : ",").Append('"').Append(((Tileset)kv.Key).ToString()).Append("\":\"").Append(kv.Value).Append('"'); first = false; }
            sb.Append("},\"oreSamples\":[").Append(string.Join(",", _oreSamples)).Append("],\"tileHist\":[");
            first = true;
            foreach (var kv in _tileHist)
            {
                long k = kv.Key;
                int band = (int)(k >> 40), tsi = (int)((k >> 20) & 0xFFFF), tt = (int)((k >> 1) & 0x3FF);
                bool empty = (k & 1) != 0;
                _kindColor.TryGetValue(k & 0xFFFFFFFFFEL, out uint rgb);
                sb.Append(first ? "" : ",").Append("\n{\"band\":").Append(band).Append(",\"tileset\":\"").Append(((Tileset)tsi).ToString())
                  .Append("\",\"tileType\":\"").Append(((TileType)tt).ToString()).Append("\",\"empty\":").Append(empty ? "true" : "false")
                  .Append(",\"rgb\":\"").Append(rgb.ToString("x6", CultureInfo.InvariantCulture)).Append("\",\"n\":").Append(kv.Value).Append('}');
                first = false;
            }
            sb.Append("],\"objectCensus\":{");
            first = true;
            var names = new List<string>(census.Keys); names.Sort(string.CompareOrdinal);
            foreach (var n in names) { sb.Append(first ? "" : ",").Append("\n\"").Append(FullMapGenPoi.Esc(n)).Append("\":").Append(census[n]); first = false; }
            sb.Append("}}\n");
            FullMapGenGuard.WriteText(FullMapGenGuard.StatsFile, sb.ToString());
        }

        static string Join<T>(Dictionary<string, T> d)
        {
            var l = new List<string>();
            foreach (var kv in d) l.Add(kv.Key + "=" + kv.Value);
            l.Sort(string.CompareOrdinal);
            return string.Join(" ", l);
        }

        bool CellAnyInitialized(int2 cell)
        {
            int2 lo = cell * CellSize - 128;
            for (int sy = 0; sy < 4; sy++)
                for (int sx = 0; sx < 4; sx++)
                    if (_tiles.IsInitialized(lo + new int2(sx, sy) * SubMapSize)) return true;
            return false;
        }

        bool CellFullyInitialized(int2 cell)
        {
            int2 lo = cell * CellSize - 128;
            for (int sy = 0; sy < 4; sy++)
                for (int sx = 0; sx < 4; sx++)
                    if (!_tiles.IsInitialized(lo + new int2(sx, sy) * SubMapSize)) return false;
            return true;
        }

        void WriteProgress(string phase, double frac, string msg)
        {
            FullMapGenGuard.WriteText(FullMapGenGuard.ProgressFile,
                "{\"phase\":\"" + phase + "\",\"fraction\":" + frac.ToString("F4", CultureInfo.InvariantCulture)
                + ",\"msg\":\"" + msg.Replace("\"", "'") + "\",\"utc\":\"" + DateTime.UtcNow.ToString("o") + "\"}");
        }

        void Abort(string why)
        {
            _phase = Phase.Aborted;
            Debug.LogError("[FullMapGen] ABORT: " + why);
            FullMapGenGuard.WriteText(FullMapGenGuard.AbortFile, DateTime.UtcNow.ToString("o") + " " + why);
            Enabled = false;
        }

        double SystemAPIFreeTime() => World.Time.ElapsedTime;   // server sim time (monotonic)

        static void Log(string m) => Debug.Log("[FullMapGen] " + m);

        static Type FindType(string name)
        {
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                var t = asm.GetType(name, false);
                if (t != null) return t;
            }
            return null;
        }
    }
}
