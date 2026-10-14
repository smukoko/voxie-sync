# Third-party notices

Voxie's own code is AGPL-3.0-or-later ([LICENSE](../LICENSE)); the map and sync spec is
CC-BY-4.0. It is built on, installs or downloads the work of others, listed
here with their licences. Licences were checked at the source (the project's
repository, or the model card on Hugging Face) on 2026-10-03.

Nothing below is copied into this repository except where it says so. Most
of it is downloaded on your own machine, from its own publisher, when you
first use the part that needs it.

## In this repository

| What | Licence | Note |
| --- | --- | --- |
| The junk-filter model (`safe_models/`: an XGBoost classifier and its scaler) | AGPL-3.0, as the rest of Voxie | Trained for this project. It holds weights, not book text. |
| The Voxie logo (`assets/logo/`) | AGPL-3.0, as the rest of Voxie | Made for this project. |
| The Kobo look's icons (`assets/kobo_look/`) and the setup page's look previews | AGPL-3.0, as the rest of Voxie | Drawn for this project; the previews are KOReader screenshots of public-domain books (Project Gutenberg covers). |
| `assets/samples/*.mp3` | Public-domain text; audio by Voxie | The README's samples: the end of *The Yellow Wallpaper* (Charlotte Perkins Gilman, Project Gutenberg #1952, public domain in the EU and the US), narrated with Kokoro-82M (Apache-2.0) and with CosyVoice 3 in the maintainer's own voice (`SAMPLES.json` says which voice made each). |
| [IBM Plex Sans and Plex Mono](https://github.com/IBM/plex) (`libra_cli/setup_web/static/fonts/`) | SIL Open Font License 1.1 | The setup page's type, served from your own computer; the licence is beside the fonts (`OFL.txt`). |

## Installed on your e-reader by `voxie setup-kobo`

| What | Licence | Source |
| --- | --- | --- |
| [KOReader](https://koreader.rocks) | AGPL-3.0 | The official release, pinned by sha256. Voxie on the Kobo is KOReader plus a plugin; it is not affiliated with the KOReader project. |
| [NickelMenu](https://pgaskin.net/NickelMenu/) | MIT | The official release, pinned by sha256. |
| [Simple UI](https://github.com/doctorhetfield-cmd/simpleui.koplugin) 2.7.5 (only with the Kobo look) | MIT | The official release, pinned by sha256. Its release carries no licence file, so the installer writes its LICENSE (`assets/simpleui/LICENSE`) beside it. |
| Literata, Crimson Pro, EB Garamond | SIL Open Font License 1.1 | From the fonts' own repositories, pinned; each font's OFL file is installed beside it. |
| Dictionaries: en-en, fr-fr, nl-nl, es-en | CC-BY-SA-4.0 | [koreader-dicts](https://github.com/DanielGregorini/koreader-dicts) v1.2.0, with their own attribution. |
| Dictionaries: en-fr, es-fr | CeCILL-C | Same source, with their own attribution. |
| Dictionaries: fr-nl, nl-fr, de-nl | CC-BY-SA-4.0 | [WikDict](https://www.wikdict.com/) (FreeDict; data from Wiktionary via DBnary). The archives carry no licence file, so the installer writes ATTRIBUTION and LICENSE beside them. |

Voxie changes only the dictionaries' display name, and says so in a
CHANGES.txt beside each.

## Downloaded on your computer when first used

| What | Licence | Used for |
| --- | --- | --- |
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) weights | Apache-2.0 | Narration on your CPU (the default). |
| [kokoro](https://github.com/hexgrad/kokoro), [misaki](https://github.com/hexgrad/misaki) | Apache-2.0 | Kokoro's Python package and its text-to-phoneme library. |
| [Whisper large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo) weights ([MLX conversion](https://huggingface.co/mlx-community/whisper-large-v3-turbo)) | MIT | Optional: checks a voice clip (word times, transcript) when it is added. Downloaded on first use. |
| [mlx-whisper](https://github.com/ml-explore/mlx-examples), [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | Optional: what runs Whisper for the clip check, on a Mac and elsewhere. |
| [eSpeak NG](https://github.com/espeak-ng/espeak-ng) | GPL-3.0 | Pronunciation of words Kokoro's lexicon lacks. A separate library, loaded at run time; GPL-3.0 and AGPL-3.0 may be combined (section 13 of each). |
| [paraphrase-multilingual-mpnet-base-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-mpnet-base-v2) and [sentence-transformers](https://github.com/UKPLab/sentence-transformers) | Apache-2.0 | The local junk filter's text embeddings. |
| [XGBoost](https://github.com/dmlc/xgboost) | Apache-2.0 | Runs the junk-filter model. |
| [Montreal Forced Aligner](https://github.com/MontrealCorpusTools/Montreal-Forced-Aligner) (Docker image, pinned by digest) | MIT | Word-level alignment of the narration. |
| [MFA pretrained models](https://github.com/MontrealCorpusTools/mfa-models) (english_mfa) | CC-BY-4.0 | Its acoustic models and dictionaries, by Michael McAuliffe and the MFA contributors. |

## In the GPU image this project publishes (`cosyvoice-tts`)

| What | Licence |
| --- | --- |
| [CosyVoice](https://github.com/FunAudioLLM/CosyVoice) code | Apache-2.0 |
| [Fun-CosyVoice3-0.5B-2512](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512) weights | Apache-2.0 |
| [CosyVoice-ttsfrd](https://huggingface.co/FunAudioLLM/CosyVoice-ttsfrd) | Apache-2.0 |
| [vLLM](https://github.com/vllm-project/vllm) | Apache-2.0 |
| Montreal Forced Aligner and its models | MIT, CC-BY-4.0 (as above) |

The licences travel with what the image installs: the CosyVoice checkout's
LICENSE, and each Python package's licence in its installed metadata. This
file is the summary.

## Run beside Voxie, unchanged

| What | Licence |
| --- | --- |
| [Audiobookshelf](https://github.com/advplyr/audiobookshelf) (its official Docker image) | GPL-3.0 |

## Tools you install yourself

| What | Licence |
| --- | --- |
| [kepubify](https://github.com/pgaskin/kepubify) | MIT |
| [FFmpeg](https://ffmpeg.org) | LGPL-2.1 or GPL, depending on the build |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) (only for `voxie voice-add --url`) | Unlicense |
| Docker | per its own terms |

Python packages are installed from PyPI by `pip install -r requirements.txt`
under their own licences; none is copied into this repository.

## Names

KOReader, Kobo, Audiobookshelf and the other names above belong to their
owners. Voxie uses them only to say what it works with.
