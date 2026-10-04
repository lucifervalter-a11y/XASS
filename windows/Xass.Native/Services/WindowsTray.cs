using System.Runtime.InteropServices;

namespace Xass.Native.Services;

// No WinForms/Tk dependency. Callback stays rooted for the HWND lifetime.
public sealed class WindowsTray : IDisposable
{
    private readonly nint hwnd;
    private readonly SubclassProc callback;
    private readonly Action restore, check, mute, quit;
    private const uint Message = 0x8000 + 73;
    private readonly uint taskbarCreated = RegisterWindowMessage("TaskbarCreated");
    private NotifyIconData icon;
    public bool Available { get; private set; }
    public WindowsTray(nint window, Action restore, Action check, Action mute, Action quit)
    {
        hwnd = window; this.restore = restore; this.check = check; this.mute = mute; this.quit = quit;
        callback = WindowMessage;
        icon = new NotifyIconData { Size = (uint)Marshal.SizeOf<NotifyIconData>(), Window = hwnd, Id = 1,
            Flags = 1 | 2 | 4, Callback = Message, Icon = LoadIcon(0, (nint)32512), Tip = "XASS · фоновый агент", Info = "", Title = "" };
        if (SetWindowSubclass(hwnd, callback, 73, 0)) Available = Shell_NotifyIcon(0, ref icon);
    }
    private nint WindowMessage(nint h, uint message, nuint w, nint l, nuint id, nuint data)
    {
        if (message == taskbarCreated) { Available = Shell_NotifyIcon(0, ref icon); return 0; }
        if (message == Message)
        {
            uint action = unchecked((uint)l.ToInt64()) & 0xffff;
            if (action == 0x0203) restore();
            if (action == 0x0205)
            {
                nint menu = CreatePopupMenu();
                try
                {
                    AppendMenu(menu, 0, 1, "Открыть XASS"); AppendMenu(menu, 0, 2, "Проверить соединение");
                    AppendMenu(menu, 0, 3, "Выключить микрофоны"); AppendMenu(menu, 0x800, 0, "");
                    AppendMenu(menu, 0, 4, "Выйти из XASS");
                    GetCursorPos(out Point point); SetForegroundWindow(hwnd);
                    uint selected = TrackPopupMenu(menu, 0x100 | 0x2, point.X, point.Y, 0, hwnd, 0);
                    if (selected == 1) restore(); else if (selected == 2) check();
                    else if (selected == 3) mute(); else if (selected == 4) quit();
                }
                finally { DestroyMenu(menu); }
            }
            return 0;
        }
        return DefSubclassProc(h, message, w, l);
    }
    public void Dispose()
    { if (Available) Shell_NotifyIcon(2, ref icon); RemoveWindowSubclass(hwnd, callback, 73); }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NotifyIconData
    {
        public uint Size; public nint Window; public uint Id, Flags, Callback; public nint Icon;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string Tip;
        public uint State, StateMask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Info;
        public uint Timeout;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string Title;
        public uint InfoFlags; public Guid Guid; public nint BalloonIcon;
    }
    [StructLayout(LayoutKind.Sequential)] private struct Point { public int X, Y; }
    private delegate nint SubclassProc(nint h, uint message, nuint w, nint l, nuint id, nuint data);
    [DllImport("comctl32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool SetWindowSubclass(nint h, SubclassProc proc, nuint id, nuint data);
    [DllImport("comctl32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool RemoveWindowSubclass(nint h, SubclassProc proc, nuint id);
    [DllImport("comctl32.dll")] private static extern nint DefSubclassProc(nint h, uint message, nuint w, nint l);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool Shell_NotifyIcon(uint message, ref NotifyIconData data);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern nint LoadIcon(nint instance, nint name);
    [DllImport("user32.dll")] private static extern nint CreatePopupMenu();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern uint RegisterWindowMessage(string name);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern bool AppendMenu(nint menu, uint flags, nuint id, string title);
    [DllImport("user32.dll")] private static extern uint TrackPopupMenu(nint menu, uint flags, int x, int y, int reserved, nint h, nint rectangle);
    [DllImport("user32.dll")] private static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(nint h);
    [DllImport("user32.dll")] private static extern bool DestroyMenu(nint menu);
}
