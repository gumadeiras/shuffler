# Make shuffler shortcuts that Windows can pin to the taskbar.
#
# Windows does not pin a shortcut to a .bat file. These shortcuts start
# .venv\Scripts\shufflerw.exe (the window, no console) in the repository folder, use
# shuffler.ico, and carry the app ID that the window sets (ClarkLab.shuffler), so the
# pinned icon and the open window are one taskbar button.
#
# Run: uv run shuffler shortcut   (or this script directly, with -Repo <folder>)

param(
    [Parameter(Mandatory = $true)][string]$Repo,
    [string]$AppId = "ClarkLab.shuffler"
)
$ErrorActionPreference = "Stop"

$target = Join-Path $Repo ".venv\Scripts\shufflerw.exe"
$icon = Join-Path $Repo "shuffler.ico"
foreach ($file in $target, $icon) {
    if (-not (Test-Path $file)) { throw "$file is missing. Run 'uv sync' and 'uv run shuffler icon' first." }
}

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace ShufflerShortcut {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    class CShellLink {}

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("000214F9-0000-0000-C000-000000000046")]
    interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file, int size, IntPtr data, uint flags);
        void GetIDList(out IntPtr list);
        void SetIDList(IntPtr list);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int size);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder dir, int size);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string dir);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int size);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
        void GetHotkey(out short key);
        void SetHotkey(short key);
        void GetShowCmd(out int command);
        void SetShowCmd(int command);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string file);
    }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    interface IPropertyStore {
        void GetCount(out uint count);
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey { public Guid FormatId; public uint PropertyId; }

    [StructLayout(LayoutKind.Explicit, Size = 24)]
    public struct PropVariant { [FieldOffset(0)] public ushort Type; [FieldOffset(8)] public IntPtr Value; }

    public static class Link {
        static PropertyKey AppIdKey() {
            // PKEY_AppUserModel_ID
            return new PropertyKey { FormatId = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), PropertyId = 5 };
        }

        public static void Create(string path, string target, string dir, string icon, string appId, string description) {
            var link = (IShellLinkW)new CShellLink();
            link.SetPath(target);
            link.SetWorkingDirectory(dir);
            link.SetIconLocation(icon, 0);
            link.SetDescription(description);
            var key = AppIdKey();
            var value = new PropVariant { Type = 31, Value = Marshal.StringToCoTaskMemUni(appId) };  // VT_LPWSTR
            try {
                var store = (IPropertyStore)link;
                store.SetValue(ref key, ref value);
                store.Commit();
            } finally {
                Marshal.FreeCoTaskMem(value.Value);
            }
            ((IPersistFile)link).Save(path, true);
        }

        public static string ReadAppId(string path) {
            var link = (IShellLinkW)new CShellLink();
            ((IPersistFile)link).Load(path, 0);
            var key = AppIdKey();
            PropVariant value;
            ((IPropertyStore)link).GetValue(ref key, out value);
            return value.Type == 31 ? Marshal.PtrToStringUni(value.Value) : "";
        }
    }
}
"@

$places = @(
    (Join-Path ([Environment]::GetFolderPath("Programs")) "shuffler.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "shuffler.lnk")
)
foreach ($place in $places) {
    [ShufflerShortcut.Link]::Create($place, $target, $Repo, $icon, $AppId, "Motorized stage control")
    $written = [ShufflerShortcut.Link]::ReadAppId($place)
    if ($written -ne $AppId) { throw "The app ID was not saved in $place." }
    Write-Output "Wrote $place"
}
Write-Output "To pin: open Start, find shuffler, right-click it, and choose Pin to taskbar."
