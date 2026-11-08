"""Helpers for tests that decode AAC-in-MP4 with the ffmpeg CLI."""
import subprocess


def declared_frames(path) -> int:
    """Samples the MP4 edit list leaves after trimming priming and padding (ffprobe duration_ts; the
    stream time base is 1/sample-rate)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "stream=duration_ts", "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return int(out.split()[0])


def apply_end_trim(path, n_decoded: int) -> int:
    """ffmpeg applies the edit list's start (priming) when decoding but, up to at least 7.1, does not
    cut the last packet's padding; ffmpeg 8 and the browsers' decoders do. This is the length a
    decoder honouring the whole edit list returns: the declared length, never more than decoded."""
    return min(n_decoded, declared_frames(path))
