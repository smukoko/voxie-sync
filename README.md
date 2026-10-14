<p align="center"><img src="assets/logo/voxie-mark.svg" width="96" alt=""></p>

# Voxie

**Read a book on your Kobo, keep going as an audiobook from the page you were
on, and come back to the page where the audio stopped.**

Free and open source. Your books, your server, no account with us.

## Hear it

The end of *The Yellow Wallpaper* (Charlotte Perkins Gilman, public domain),
under a minute, narrated by Voxie two ways:

| Free, on your own computer | The recommended setup |
| --- | --- |
| [Listen](assets/samples/the-yellow-wallpaper-kokoro.mp3): Kokoro, its `af_heart` voice | [Listen](assets/samples/the-yellow-wallpaper-recommended.mp3): CosyVoice on RunPod in a voice you choose (here, the maintainer's own), directed line by line |

## Install

Run this one line on your Mac (or Linux computer):

```bash
curl -fsSL https://raw.githubusercontent.com/smukoko/voxie-sync/main/install.sh | bash
```

It installs Voxie and opens **the setup page** in your browser. Everything
else happens on that page: your login, your server, your RunPod key if you
want fast narration, a voice, and last your Kobo, plugged in by cable. The
page runs on your own computer, never online, so your keys stay with you.
The Kobo restarts with a **Voxie** button, and you are done.

The Kobo is set up by cable, so it is updated by cable too: when a new
version of Voxie changes the Kobo's part (its plugin, or your server's
address), plug the Kobo in again and run `voxie setup-kobo` (or the setup
page's last step). Your books, reading positions and settings stay.

Closed the page too early? `voxie init` opens it again (`voxie init
--terminal` asks the same questions without a browser).

Now drop an EPUB into `~/Kobo_Dropzone`. Read it on the Kobo, listen to it in
any Audiobookshelf app on your phone: each one picks up where the other
stopped.

Just want the ebook on your Kobo? Drop it into `~/Kobo_Dropzone_NoAudio`: it
arrives on the Kobo over Wi-Fi in a few minutes, no cable, no narration.
Want its audiobook after all? Drop the same file into `~/Kobo_Dropzone` (it
waits in `~/Kobo_Archive`): it stays the same book, keeps your reading
position, and gets its audio.

Windows is not supported yet. The Kobo step needs a Mac; from Linux, or for
any other KOReader device, pairing takes a few settings
([docs/KOREADER_DEVICES.md](docs/KOREADER_DEVICES.md)).

## Without Wi-Fi

On a train or a plane, your Kobo still knows where you are in the audiobook.
It keeps a small map of each book (positions only, about 55 KB for a 16-hour
novel). With a book open, go to **Tools** (second page) → **Voxie**:

- **Your audiobook position: chapter 12, 14:32** is where to start the
  audiobook to carry on from the page you are reading.
- **I listened up to…**: type the chapter and time your phone shows, and the
  Kobo opens that page.

Download the audiobook in your Audiobookshelf app to listen offline. When
Wi-Fi comes back, the Kobo sends the page you read and when you read it, so
whichever you did last, reading or listening, wins on both sides.

## The recommended setup

Three things you rent, for a few euros:

- **A server at [Hetzner](https://www.hetzner.com/cloud)**,
  about €5 a month. Always on and reachable from anywhere, so your Kobo and your phone stay in sync away from home; `voxie init` installs everything on
  it for you ([DEPLOY.md](DEPLOY.md)).
- **Narration on [RunPod](https://runpod.io?ref=x88lhsfk)**, a GPU by
  the hour. **A 16-hour novel costs under $1 in all**, the voice director
  below included: Voxie rents only cards measured to keep it there, says the
  estimate before anything bills, and returns a pod that comes at a higher
  price (about $0.50 of GPU on one RTX A5000).
  It reads in a voice you choose, which sounds much closer to a real narrator
  than the free voices.
- **A voice director: JEV through [OpenRouter](https://openrouter.ai)**,
  about $0.17 a novel (measured: $0.14 for a 13.8-hour thriller), counted
  inside that dollar. The biggest difference to how the book sounds: JEV
  reads each scene and tells the narrator how to say every paragraph
  (hushed, frantic, tender, shouted, and twelve more), so the narrator acts
  the book instead of reading it out. Put your OpenRouter key in `.env` as
  `OPENROUTER_API_KEY`.

<sub>The RunPod link is a referral link: Voxie gets a small RunPod credit when
you sign up through it, and you pay nothing extra.</sub>

**Free instead:** narrate on your own computer (preset voices, a novel
overnight) and run the server on a machine you already have. Pick those in
`voxie init`; see [docs/GUIDE.md](docs/GUIDE.md).

## Before you use it

- **Voxie narrates English books only, for now.** No other language has been
  tested end to end. A book in another language still reaches your Kobo, as an
  ebook without an audiobook; French and Dutch narration are planned.
- Narrate only books you own, without DRM. Voxie does not remove DRM.
- Use only a voice whose speaker agreed. `voxie init` records that consent;
  no voice ships with Voxie.

## Learn more

- [How it works, privacy, limits, installing by hand](docs/GUIDE.md)
- [The server, in detail](docs/SERVER_REFERENCE.md)
- [Any other KOReader device](docs/KOREADER_DEVICES.md)
- [The sync format, for other apps](docs/SYNC_FORMAT.md)
- [Something sounded wrong? Tell us](https://github.com/smukoko/voxie-sync/issues/new?template=narration.yml), no programming needed
- [Contributing](.github/CONTRIBUTING.md) · [Security](.github/SECURITY.md)

## Support Voxie

Voxie is free, with no paid tier, and lives on donations and grants. If it
is useful to you: [ko-fi.com/voxiesync](https://ko-fi.com/voxiesync).

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/Y0O3288BGA)

## License

AGPL-3.0-or-later ([LICENSE](LICENSE)); the sync format spec is CC-BY-4.0.
Contributions are under a [Contributor License Agreement](.github/CLA.md). What Voxie
builds on: [docs/THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md).

Kobo is a trademark of Rakuten Kobo Inc.; KOReader and Audiobookshelf belong
to their projects. Voxie is an independent project.

