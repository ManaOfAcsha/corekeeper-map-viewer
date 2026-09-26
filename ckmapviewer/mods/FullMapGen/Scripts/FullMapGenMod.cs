using PugMod;
using UnityEngine;

namespace FullMapGen
{
    // Entry point. Does NOTHING unless FullMapGenGuard.Evaluate() says the process is a
    // disposable server armed with the marker file. The ECS system below checks the same
    // flag in OnCreate and disables itself otherwise.
    public class FullMapGenMod : IMod
    {
        public const string NAME = "FullMapGen";
        public const string VERSION = "1.0.0";

        public void EarlyInit()
        {
            FullMapGenGuard.Evaluate();
            Debug.Log($"[{NAME}] v{VERSION} loaded. armed={FullMapGenGuard.Armed} reason={FullMapGenGuard.Reason}");
        }

        public void Init()
        {
            if (!FullMapGenGuard.Armed || API.Server == null) return;
            // Fallback in case the bootstrap did not auto-create the system in the server world.
            API.Server.OnWorldCreated += () =>
            {
                var w = API.Server.World;
                if (w == null || w.GetExistingSystemManaged<FullMapGenSystem>() != null) return;
                var s = w.CreateSystemManaged<FullMapGenSystem>();
                API.Server.AddScheduledSystem(s);   // -> SimulationSystemGroup
                Debug.Log($"[{NAME}] system added via OnWorldCreated fallback");
            };
        }
        public void Shutdown() { }
        public void ModObjectLoaded(Object obj) { }
        public void Update() { }
    }
}
