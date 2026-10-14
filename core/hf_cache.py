"""A Hugging Face model's folder in the local cache, so loading it asks no one.

mlx_whisper, given a repo name, asks the hub about the model's files on every
call, even when all of them are cached. On 2026-10-12 one of those questions
hung on a dead connection (CLOSE_WAIT, no timeout) and a narration scan waited
25 minutes before its first chunk. The network is needed once, to fetch the
model; after that nothing depends on it (core/ai_brain.load_embedder learned
the same for the sentence-transformer, 2026-10-07).
"""
_found = {}


def model_path(repo, snapshot_download=None):
    """The cached snapshot's folder for `repo`, or `repo` itself when it is not
    cached yet: the caller's library then downloads it, the one time. Only a
    folder is remembered, so the next call after that download finds it."""
    if repo in _found:
        return _found[repo]
    try:
        if snapshot_download is None:
            from huggingface_hub import snapshot_download
        path = snapshot_download(repo, local_files_only=True)  # nosec B615: reads the cache, downloads nothing
    except Exception:
        # Not cached (LocalEntryNotFoundError), or no huggingface_hub: the old
        # way, by name, which is how the model gets cached in the first place.
        return repo
    _found[repo] = path
    return path
