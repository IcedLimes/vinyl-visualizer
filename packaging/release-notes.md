**Experimental.** These are the first Windows and macOS builds of Vinyl. They
are built and self-tested automatically, but nobody has tried them on a real
Windows PC or Mac yet. Expect rough edges, and please
[open an issue](https://github.com/IcedLimes/vinyl-visualizer/issues) if
something doesn't work. Linux users don't need these: install from source
as the README describes.

The apps aren't signed, so Windows and macOS warn about them the first time
you open them. The steps below get past those warnings.

## Windows 10 / 11

1. Download **`Vinyl-…-windows-x64-setup.exe`** and run it. It installs for
   your account only and doesn't need administrator rights.
   (`…-portable.zip` is the same app without an installer: unzip it and run
   `Vinyl.exe`.)
2. If SmartScreen says *Windows protected your PC*, click **More info**, then
   **Run anyway**.
3. Start Spotify. The record appears while Spotify is open. Vinyl's icon is
   in the tray by the clock (Windows 11 may tuck it under the **^** arrow).
   Click it to show or hide the record, or right-click it for the menu.

## macOS 13 or newer

1. Download the disk image for your Mac: **`…-macos-arm64.dmg`** for Apple
   silicon (M1 and later), **`…-macos-x64.dmg`** for Intel. Open it and drag
   **Vinyl** into **Applications**.
2. Open Vinyl. macOS blocks it the first time. Go to **System Settings →
   Privacy & Security**, scroll down and click **Open Anyway**. If macOS says
   the app *is damaged*, run this in Terminal and open it again:
   `xattr -dr com.apple.quarantine /Applications/Vinyl.app`
3. Allow what Vinyl asks for:
   - **Control Spotify** (Automation), to read what's playing and play or pause it.
   - **Screen & System Audio Recording**, to hear Spotify so the record can
     move with the music. Only Spotify's sound is used. Nothing on your screen
     is recorded or saved. Quit and reopen Vinyl after allowing it.
4. Vinyl has no Dock icon. It lives in the menu bar.

## Differences from Linux

- **Windows hears everything you hear**, not just Spotify, so other sounds
  move the record too. On macOS it hears only Spotify.
- **Covers on Windows are 300 px** (all Windows gets from Spotify), so they look
  softer on a big record.
- **It doesn't correct for Bluetooth delay automatically.** If the pulse runs
  ahead of what you hear, raise **Sync offset** in Settings (Bluetooth
  headphones usually need 150 to 250 ms).
- **No separate `--with-spotify` mode.** Vinyl stays in the tray or menu bar and
  shows the record only while Spotify is open. Use **Start at login** in its
  menu to have it there from the start.

## If something goes wrong

Vinyl writes what went wrong to `vinyl.log`, next to its settings:
`%APPDATA%\vinyl-visualizer` on Windows,
`~/Library/Application Support/vinyl-visualizer` on macOS. Please attach that
file to an issue.
