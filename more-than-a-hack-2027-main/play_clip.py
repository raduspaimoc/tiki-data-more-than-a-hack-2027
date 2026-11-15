"""Open the tactical video between two frames. Local viewing only, not part of a submission.

    python play_clip.py 11326 11376
    python play_clip.py 11326 11376 11351

The start frame is included and the end frame is excluded, same span as
animate_frames(dataset, start, end). Frame f is second f/25 of the tactical file.
The window seeks to the start and plays the span in a loop. A third frame
pauses there first; space or the play button continues, and from then on the
span repeats. The left and right arrows move one frame. Ctrl+C closes the
window and the player. The first run compiles the player and can take a little while.
"""

import os
import signal
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from games import GAME, GAMES, video_path  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYER = os.path.join(HERE, "play_clip.swift")


def play_clip(start_frame, end_frame, pause_frame=None):
    """Open the tactical video from start_frame inclusive to end_frame exclusive."""
    if end_frame <= start_frame:
        raise ValueError("end_frame must be greater than start_frame")
    n_frames = GAMES[GAME]["n_video_frames"]
    if start_frame < 0 or end_frame > n_frames:
        raise ValueError("frames must sit inside 0..{}".format(n_frames))
    if pause_frame is not None and not (start_frame <= pause_frame < end_frame):
        raise ValueError("pause frame must sit inside the span")

    path = video_path(source="tactical")
    if not os.path.isfile(path):
        raise IOError("video not found: {}".format(path))

    fps = GAMES[GAME]["fps"]
    start_s = start_frame / fps
    end_s = end_frame / fps
    print("{}  frames {}-{}  ({:.2f} s)  {} -> {}".format(
        os.path.basename(path), start_frame, end_frame - 1, end_s - start_s,
        _clock(start_s), _clock(end_s)), flush=True)
    if pause_frame is not None:
        print("Pauses on frame {} ({:.2f} s). Play loops the span.".format(
            pause_frame, pause_frame / fps), flush=True)
    print("Opening a player at that second. The first launch compiles it.", flush=True)
    command = ["swift", PLAYER, path, "{:.3f}".format(start_s), "{:.3f}".format(end_s)]
    if pause_frame is not None:
        command.append("{:.3f}".format(pause_frame / fps))
    # Own process group, so Ctrl+C can stop swift and the player it compiles.
    proc = subprocess.Popen(command, start_new_session=True)
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        _stop(proc)
        raise
    if returncode:
        sys.exit(returncode)


def _stop(proc):
    """Close the player window and the executable swift left running."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        proc.wait()


def _clock(seconds):
    whole = int(seconds)
    return "{:d}:{:02d}.{:02d}".format(whole // 60, whole % 60, int(round((seconds - whole) * 100)))


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        print("usage: python play_clip.py START_FRAME END_FRAME [PAUSE_FRAME]", file=sys.stderr)
        sys.exit(2)
    pause = int(sys.argv[3]) if len(sys.argv) == 4 else None
    play_clip(int(sys.argv[1]), int(sys.argv[2]), pause)
