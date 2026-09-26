using HarmonyLib;

namespace FullMapGen
{
    // A dedicated server with no connections pauses itself every frame:
    // ECSManager.Update -> Pause() disables SimulationSystemGroup and sets timeScale = 0
    // (observed condition: loading done && no NetworkStreamConnection).
    // The disposable server never has a player, so while ARMED we keep it running.
    // Unarmed (any real server) this prefix returns true and the game behaves as usual.
    [HarmonyPatch(typeof(ECSManager), nameof(ECSManager.Pause))]
    public static class FullMapGenNoPausePatch
    {
        public static bool Prefix(ECSManager __instance)
        {
            if (!FullMapGenGuard.Armed) return true;
            __instance.Resume();
            return false;
        }
    }
}
