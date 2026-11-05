"""Open the tactical video between two frames. Local viewing only, not part of a submission.

    python play_clip.py 11435 12000

The start frame is included and the end frame is excluded, same span as
animate_frames(dataset, start, end). Frame f is second f/25 of the tactical file.
A small player window seeks to that second and pauses when the span ends.
The first run compiles the player and can take a little while.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from games import GAME, GAMES, video_path  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYER = os.path.join(HERE, "play_clip.swift")


def play_clip(start_frame, end_frame):
    """Open the tactical video from start_frame inclusive to end_frame exclusive."""
    if end_frame <= start_frame:
        raise ValueError("end_frame must be greater than start_frame")
    n_frames = GAMES[GAME]["n_video_frames"]
    if start_frame < 0 or end_frame > n_frames:
        raise ValueError("frames must sit inside 0..{}".format(n_frames))

    path = video_path(source="tactical")
    if not os.path.isfile(path):
        raise IOError("video not found: {}".format(path))

    fps = GAMES[GAME]["fps"]
    start_s = start_frame / fps
    end_s = end_frame / fps
    print("{}  frames {}-{}  ({:.2f} s)  {} -> {}".format(
        os.path.basename(path), start_frame, end_frame - 1, end_s - start_s,
        _clock(start_s), _clock(end_s)), flush=True)
    print("Opening a player at that second. The first launch compiles it.", flush=True)
    subprocess.Popen(["swift", PLAYER, path, "{:.3f}".format(start_s), "{:.3f}".format(end_s)])


def _clock(seconds):
    whole = int(seconds)
    return "{:d}:{:02d}.{:02d}".format(whole // 60, whole % 60, int(round((seconds - whole) * 100)))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python play_clip.py START_FRAME END_FRAME", file=sys.stderr)
        sys.exit(2)
    play_clip(int(sys.argv[1]), int(sys.argv[2]))
