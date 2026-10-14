# Pair any KOReader device

Voxie syncs through KOReader's own **Progress sync** and **OPDS catalog**,
so any device that runs [KOReader](https://koreader.rocks) can use it:
a Kobo set up from Windows or Linux, a PocketBook, a Kindle running
KOReader, an Android phone or tablet, or KOReader on a computer.

On a Kobo with a Mac at hand, `voxie setup-kobo` does all of this in one
click, also when KOReader is already installed. This page is for everything
else. Installing KOReader itself is covered by KOReader's own instructions
for your device.

## 1. The values

On the computer where you ran `voxie init`:

```bash
voxie device-settings
```

It prints your server, username, password, the catalogue address and the
plugin download. If someone else runs the server, ask them for an account.

## 2. Progress sync (positions both ways)

Open a book, tap the top of the screen, open the **Tools** tab (the wrench),
then **Progress sync**:

1. **Custom sync server**: the server address, e.g. `https://voxie.your-tailnet.ts.net`.
2. **Register / Login** → **Login** with your username and password.
3. **Automatically keep documents in sync**: on.
4. **Document matching method**: **Binary**. Voxie recognises a book by its
   contents; with *Filename* nothing is ever found.
5. Optional, as the Mac installer sets it: **Sync behavior** → **Sync to a
   newer state**: *Silently*; **Sync to an older state**: *Prompt*.

On an e-reader (Kobo, PocketBook, Kindle), automatic sync also needs Wi-Fi
to switch itself on: in the **Settings** tab (the gear) → **Network** →
**Action when Wi-Fi is off** → **turn on**.

## 3. The catalogue (your books)

From the file browser, tap the top of the screen, open the **Search** tab
(the magnifier) → **OPDS catalog** → **＋ Add catalog**:

- Catalog name: `Voxie`
- Catalog URL: the server address followed by `/opds`
- Username and password: your account

Download a book from it. Read it on the device and listen in any
Audiobookshelf app: the two stay in step.

## 4. The audiobook menu (optional)

The plugin adds a **Voxie** menu to the Tools tab: where to start the
audiobook for the page you are on and *I listened up to…* (chapter and time,
to the page), both without Wi-Fi, and *Where is my audiobook?* online. Sync
works without it.

1. Download the plugin address from `voxie device-settings` in a browser,
   and log in with your account. You get `librarian_sync.koplugin.zip`,
   already set to your server.
2. Unzip it and copy the `librarian_sync.koplugin` folder into KOReader's
   `plugins` folder:

   | Device | `plugins` folder |
   | --- | --- |
   | Kobo | `.adds/koreader/plugins/` (a hidden folder on the Kobo's drive) |
   | PocketBook | `applications/koreader/plugins/` |
   | Kindle | `koreader/plugins/` |
   | Android | `koreader/plugins/` in the device's internal storage |
   | Linux (AppImage, Flatpak) | `~/.config/koreader/plugins/` |
   | macOS | `~/Library/Application Support/koreader/plugins/` |

3. Restart KOReader.

If your server moves (a new address), download the plugin again and repeat
step 2 of this page with the new address.
