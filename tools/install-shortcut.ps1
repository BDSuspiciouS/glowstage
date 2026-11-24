# Adds "Glowstage" to the Start menu of a source checkout, so it shows up in Windows Search and
# can be pinned. (The installer does this itself for the installed app.) The shortcut carries the
# app's AppUserModelID (Glowstage.App, set by the app at start), so the taskbar button and pins
# use this shortcut's name and icon instead of python's.
# Run again any time to repair it; pass -Remove to take it out.

param([switch]$Remove, [string]$Exe)   # -Exe: the installed Glowstage.exe instead of a source checkout

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$programs = [Environment]::GetFolderPath('Programs')
$lnk = Join-Path $programs 'Glowstage.lnk'

if ($Remove) {
    Remove-Item $lnk -ErrorAction SilentlyContinue
    "Removed $lnk"
    return
}

if ($Exe) {
    $target, $arguments, $workdir, $icon = $Exe, '', (Split-Path $Exe), $Exe
} else {
    # The python that runs this checkout.
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    $target = Join-Path (Split-Path $python) 'pythonw.exe'
    $arguments = '"' + (Join-Path $project 'Glowstage.pyw') + '"'
    $workdir = $project
    $icon = Join-Path $env:APPDATA 'Glowstage\icon.ico'
    if (-not (Test-Path $icon)) {
        # The app writes its icon on first start; make it now if it isn't there yet.
        & $python -c "import sys; sys.path.insert(0, r'$project'); from glowstage.icon import icon_file; from glowstage.paths import APP_DIR; icon_file(APP_DIR)"
    }
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

[ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder f, int cch, IntPtr fd, uint flags);
    void GetIDList(out IntPtr ppidl);
    void SetIDList(IntPtr pidl);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder name, int cch);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder dir, int cch);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string dir);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int cch);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
    void GetHotkey(out short key);
    void SetHotkey(short key);
    void GetShowCmd(out int cmd);
    void SetShowCmd(int cmd);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int cch, out int index);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
    void Resolve(IntPtr hwnd, uint flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string file);
}

[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyStore {
    void GetCount(out uint count);
    void GetAt(uint index, out PropertyKey key);
    void GetValue(ref PropertyKey key, out PropVariant value);
    void SetValue(ref PropertyKey key, ref PropVariant value);
    void Commit();
}

[StructLayout(LayoutKind.Sequential, Pack = 4)]
public struct PropertyKey { public Guid fmtid; public uint pid; }

[StructLayout(LayoutKind.Explicit)]
public struct PropVariant { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; }

[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
class CShellLink { }

public static class GlowstageShortcut {
    static PropertyKey AppIdKey = new PropertyKey { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };

    public static void Create(string lnk, string target, string args, string dir, string icon, string desc, string appId) {
        var link = (IShellLinkW)new CShellLink();
        link.SetPath(target);
        link.SetArguments(args);
        link.SetWorkingDirectory(dir);
        link.SetIconLocation(icon, 0);
        link.SetDescription(desc);
        var store = (IPropertyStore)link;
        var value = new PropVariant { vt = 31, p = Marshal.StringToCoTaskMemUni(appId) };  // VT_LPWSTR
        store.SetValue(ref AppIdKey, ref value);
        store.Commit();
        Marshal.FreeCoTaskMem(value.p);
        ((IPersistFile)link).Save(lnk, true);
    }

    public static string ReadAppId(string lnk) {
        var link = (IShellLinkW)new CShellLink();
        ((IPersistFile)link).Load(lnk, 0);
        PropVariant value;
        ((IPropertyStore)link).GetValue(ref AppIdKey, out value);
        return value.vt == 31 ? Marshal.PtrToStringUni(value.p) : null;
    }
}
'@

[GlowstageShortcut]::Create($lnk, $target, $arguments, $workdir, $icon,
    'Light shows for your PC and room lights', 'Glowstage.App')
"Created $lnk"
"  runs:   $target $arguments"
"  icon:   $icon"
"  app id: $([GlowstageShortcut]::ReadAppId($lnk))"
