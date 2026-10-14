# Voxie narrator (CosyVoice 3)

The GPU narrator for [Voxie](https://github.com/smukoko/voxie-sync): it turns
your DRM-free EPUBs into audiobooks in a voice you choose, and the Voxie app on
your computer keeps your e-reader and your audiobook player at the same place.

**You do not start pods from this page.** Voxie does it for you: it rents a
pod from this template when you drop a book, narrates, and deletes the pod as
soon as the book is done. You pay only for those minutes.

## Use it

1. Install Voxie on your Mac or Linux computer and run `voxie init`
   ([the README](https://github.com/smukoko/voxie-sync#install)).
2. Choose **RunPod** when it asks how to narrate, and paste a RunPod API key
   (Settings → API Keys). Voxie fills in this template by itself.
3. Add a voice when it offers: an 8-15 second clip of someone who agreed to be
   cloned, you or someone who gave you permission. No voice is built in.
4. Recommended: an [OpenRouter](https://openrouter.ai) key, for the voice
   director. It tells the narrator how to say each paragraph (hushed,
   frantic, tender...), about $0.17 a novel.

## What it costs

Under $1 for a 16-hour novel, the director included: Voxie rents only cards
measured to keep it there and says the estimate before anything bills. One
RTX A5000 narrates a directed book about 9 times faster than real time.

A pod whose client goes quiet (a laptop asleep or out of battery, a lost
connection) terminates itself after 15 minutes without a request, so it never
bills for nothing. `POD_IDLE_MINUTES` in Voxie's `.env` changes that.

## Details

- Image: `ghcr.io/smukoko/cosyvoice-tts:4.3`, built from
  [voxie-sync](https://github.com/smukoko/voxie-sync) by its own workflow. It starts its own server and
  carries its own settings; there is no start command and no variable to set.
- Port **8000/tcp**: the narration API. Every request needs the
  `TTS_API_TOKEN` that Voxie generates for you and sends to each pod it
  starts. **Never put your token in a template**: a public template shows its
  settings to everyone.
- Port **8001/tcp**: the pod's boot log, for when something goes wrong.
- The model: [Fun-CosyVoice3-0.5B](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512)
  and the [CosyVoice](https://github.com/FunAudioLLM/CosyVoice) code, both
  Apache-2.0. Voxie itself is AGPL-3.0.

## Use it fairly

Narrate only books you own, without DRM. Clone only a voice whose speaker
agreed; Voxie records that consent with the voice.
