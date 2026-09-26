using System;
using System.IO;
using System.Text;
using PugMod;
using UnityEngine;

namespace FullMapGen
{
    // Triple safety interlock. ALL must hold, otherwise the mod stays inert:
    //  1) "-datapath" was passed explicitly on the command line
    //  2) that datapath is NOT the default <persistentDataPath>/DedicatedServer (the real server)
    //  3) <datapath>/mods/FullMapGen/ARMED exists and contains the exact token below
    // API.ConfigFilesystem is rooted at <datapath>/mods (the game's mod loader creates it that way).
    // Additionally the system aborts at runtime the moment any player/connection exists.
    public static class FullMapGenGuard
    {
        public const string Dir = "FullMapGen";
        public const string ArmedFile = Dir + "/ARMED";
        public const string RunningFile = Dir + "/RUNNING";
        public const string ProgressFile = Dir + "/PROGRESS.json";
        public const string DoneFile = Dir + "/DONE.json";
        public const string AbortFile = Dir + "/ABORTED.txt";
        public const string PoisFile = Dir + "/pois.json";
        public const string StatsFile = Dir + "/STATS.json";
        public const string Token = "DISPOSABLE-COPY-OK";

        public static bool Armed { get; private set; }
        public static int RadiusOverride { get; private set; }
        public static string Reason { get; private set; } = "not evaluated";

        public static void Evaluate()
        {
            Armed = false;
            try
            {
                string dp = GetArg("-datapath");
                if (string.IsNullOrEmpty(dp)) { Reason = "no -datapath argument"; return; }

                string full = Norm(Path.GetFullPath(dp));
                string def = Norm(Path.GetFullPath(Application.persistentDataPath + "/DedicatedServer"));
                if (string.Equals(full, def, StringComparison.OrdinalIgnoreCase)) { Reason = "datapath is the default (real) server path"; return; }

                var fs = API.ConfigFilesystem;
                if (fs == null) { Reason = "ConfigFilesystem null"; return; }
                if (!fs.FileExists(ArmedFile)) { Reason = "marker mods/" + ArmedFile + " missing"; return; }
                // line 1 = token, optional "radius=N" = plan radius in tiles from the core. It may be SMALLER
                // (quick test) or LARGER (probe beyond the game bound) than the bound. Absent = auto extent.
                string[] lines = Encoding.UTF8.GetString(fs.Read(ArmedFile)).Replace("\r", "").Split('\n');
                if (lines[0].Trim() != Token) { Reason = "marker token mismatch"; return; }
                RadiusOverride = 0;
                foreach (string l in lines)
                    if (l.Trim().StartsWith("radius=") && int.TryParse(l.Trim().Substring(7), out int r) && r > 0)
                        RadiusOverride = r;
                if (fs.FileExists(DoneFile)) { Reason = "DONE already present (delete it to rerun)"; return; }

                Armed = true;
                Reason = "armed (datapath=" + full + ")";
            }
            catch (Exception e)
            {
                Armed = false;
                Reason = "guard exception: " + e.Message;
            }
        }

        public static void WriteText(string rel, string text)
        {
            try { API.ConfigFilesystem.Write(rel, Encoding.UTF8.GetBytes(text)); }
            catch (Exception e) { Debug.LogWarning("[FullMapGen] write " + rel + " failed: " + e.Message); }
        }

        private static string GetArg(string name)
        {
            string[] args = Environment.GetCommandLineArgs();
            for (int i = 0; i < args.Length - 1; i++)
                if (string.Equals(args[i], name, StringComparison.OrdinalIgnoreCase)) return args[i + 1];
            return null;
        }

        private static string Norm(string p) => p.Replace('\\', '/').TrimEnd('/');
    }
}
