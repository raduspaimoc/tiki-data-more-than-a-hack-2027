import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
from kloppy import metrica

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (HERE, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import games  # noqa: E402
from baseline import write_submission  # noqa: E402
from load_smart_tagging import KICKOFF_VIDEO_FRAMES, load as load_events  # noqa: E402

# Module state filled by prepare_match().
frame_df = None
video = n = None
in_play = half_start = None
bx = by = px = py = None

paths = games.asset_paths(source='tactical')

L, W, FPS = 105.3, 68.0, 25
PZ_RADIUS = 1.5
MIN_FRAMES = 3
ANGLE_DEG = 10.0
SPEED_DELTA = 5.0
SHORT_GAP = 25  # 1 second
GHOST_SECONDS = 2.0  # lost track kept this long; Link predicts 1 s, this pass needs 2
GHOST_PAIR_M = 2.0
OUT_EDGE_M = 2.0
OUT_CORNER_M = 3.0
OUT_MIN_FRAMES = 25  # a ball gone for under 1 s stays in play
EVENT_LIVE_FRAMES = 100  # after the ball reappears, the restart ends once it reaches 5 m/s
GOAL_LOOKBACK = 30 * FPS
GOAL_MOUTH_PAD_M = 1.0  # metres beyond each post; Law 10 is 7.32 m wide
MAX_CELEBRATION_FRAMES = 360 * FPS
HYDRATION_SIDELINE_M = 5.0
# Vidal-Codina / Law 17: a taker near the corner mark, teammates packed in the box.
CORNER_TAKER_M = 5.0
BOX_DEPTH_M = 16.5
BOX_HALF_W_M = 20.16
BOX_MIN_PLAYERS = 6
SIX_DEPTH_M = 5.5   # Law 1: six-yard box
SIX_HALF_W_M = 9.16  # 5.5 m outside each post; 7.32/2 + 5.5
CORNER_SETUP_FRAMES = 25
PENALTY_SPOT_M = 11.0  # Law 14
PENALTY_ARC_M = 9.15
PENALTY_TAKER_M = 3.0
PENALTY_LINE_M = 1.5
VAR_MIN_FRAMES = 20 * 25  # VAR review is tens of seconds, not a 1 s gap
FREEZE_MIN_FRAMES = 5 * 25  # large foul/offside: the whole game stops ≥ 5 s
FREEZE_FAST_MAX = 3


def load_ball_speed(raw_path, n_frames):
    """Ball speed from the raw ATD, in the unit the XML declares (kph).

    The ball is the third ':' field of each line, and speed is its third comma
    value. Kloppy drops that value, so this reads the text directly. Index i is
    video frame i. NaN where the file has no speed.
    """
    speed = np.full(n_frames, np.nan)
    with open(raw_path) as handle:
        for line in handle:
            frame, _, ball = line.split(":", 2)
            value = ball.rstrip("\n").split(",")[2]
            if value != "NaN":
                speed[int(frame) - 1] = float(value)
    return speed

def distances(ball_x, ball_y, players_x, players_y):
    dist = np.hypot(players_x * L - ball_x[:, None] * L, players_y * W - ball_y[:, None] * W)
    dist[~np.isfinite(players_x)] = np.inf
    return dist

def ghost_players(players_x, players_y):
    """Keep a vanished slot for GHOST_SECONDS.

    Within 2 m of someone on the last frame, follow that player: they are
    crossing. Otherwise continue at the last velocity, and stop at the pitch edge.
    """
    out_x = players_x.copy()
    out_y = players_y.copy()
    n_frames, n_slots = players_x.shape
    limit = int(GHOST_SECONDS * FPS)
    filled = 0
    for slot in range(n_slots):
        col_x = players_x[:, slot]
        col_y = players_y[:, slot]
        i = 1
        while i < n_frames:
            if np.isfinite(col_x[i]) or not np.isfinite(col_x[i - 1]):
                i += 1
                continue
            j = i
            while j < n_frames and not np.isfinite(col_x[j]):
                j += 1
            span = min(j - i, limit)
            partner = None
            best = GHOST_PAIR_M
            for other in range(n_slots):
                if other == slot or not np.isfinite(players_x[i - 1, other]):
                    continue
                gap = np.hypot(
                    (players_x[i - 1, other] - col_x[i - 1]) * L,
                    (players_y[i - 1, other] - col_y[i - 1]) * W,
                )
                if gap < best:
                    best = gap
                    partner = other
            if partner is not None:
                for t in range(span):
                    frame = i + t
                    if not np.isfinite(players_x[frame, partner]):
                        break
                    out_x[frame, slot] = players_x[frame, partner]
                    out_y[frame, slot] = players_y[frame, partner]
                    filled += 1
            elif i >= 2 and np.isfinite(col_x[i - 2]):
                step_x = col_x[i - 1] - col_x[i - 2]
                step_y = col_y[i - 1] - col_y[i - 2]
                for t in range(span):
                    nx = col_x[i - 1] + (t + 1) * step_x
                    ny = col_y[i - 1] + (t + 1) * step_y
                    if not (-0.05 <= nx <= 1.05 and -0.05 <= ny <= 1.05):
                        break
                    out_x[i + t, slot] = nx
                    out_y[i + t, slot] = ny
                    filled += 1
            i = j
    return out_x, out_y, filled


def ball_out(ball_x, ball_y, players_x, players_y, kickoff_mask=None):
    """Dead frames for a throw-in, a corner or a goal kick.

    A gap longer than a second counts when the ball leaves within 2 m of a
    line, or comes back from outside that band. A goal kick keeps the team
    defending that goal when one of their players is in the six-yard box.
    A throw-in or a corner keeps nobody: the next touch decides.
    """
    dead = np.zeros(n, dtype=bool)
    team = np.full(n, "", dtype="<U1")
    kind = np.full(n, "", dtype="<U8")
    here = np.isfinite(ball_x) & np.isfinite(ball_y)
    outside = here & ((ball_x < 0) | (ball_x > 1) | (ball_y < 0) | (ball_y > 1))

    def off_pitch(x, y):
        return x < 0 or x > 1 or y < 0 or y > 1

    def edge_kind(x, y):
        if not np.isfinite(x) or not np.isfinite(y):
            return None, False
        left = bool(x <= 0.5)
        if not off_pitch(x, y):
            goal_m = min(x, 1 - x) * L
            side_m = min(y, 1 - y) * W
            if goal_m <= OUT_CORNER_M and side_m <= OUT_CORNER_M:
                return "corner", left
            return None, False
        if x < 0:
            goal_m = abs(x) * L
        elif x > 1:
            goal_m = (x - 1) * L
        else:
            goal_m = min(x, 1 - x) * L
        if y < 0:
            side_m = abs(y) * W
        elif y > 1:
            side_m = (y - 1) * W
        else:
            side_m = min(y, 1 - y) * W
        # Touchline out is a throw-in even next to the corner flag (Vidal: that
        # mix-up is the main corner/throw-in error). Goal-line out is corner or GK.
        if y < 0 or y > 1:
            return "side", left
        if goal_m <= OUT_CORNER_M and side_m <= OUT_CORNER_M:
            return "corner", left
        if x < 0 or x > 1:
            if goal_m <= OUT_EDGE_M:
                return "goal", left
        return None, left

    def defending(frame, left):
        home_left = video[frame] <= 90651
        if left:
            return "H" if home_left else "A"
        return "A" if home_left else "H"

    def keeper_there(start, stop, left, who):
        depth, half_w = 7.5, 11.16
        slots = range(0, 24) if who == "H" else range(24, 46)
        step = max(1, (stop - start) // 20)
        for frame in range(start, stop, step):
            xs, ys = players_x[frame], players_y[frame]
            for slot in slots:
                if not np.isfinite(xs[slot]):
                    continue
                along = xs[slot] * L if left else (1 - xs[slot]) * L
                across = abs(ys[slot] - 0.5) * W
                if along <= depth and across <= half_w:
                    return True
        return False

    POST_HALF_W = 3.66  # Law 1: goal 7.32 m. Outside the posts + goal line = corner or goal kick.

    def posts_kind(x, y, left):
        if not np.isfinite(x) or not np.isfinite(y):
            return None
        if 0 <= x <= 1:
            return None
        across = abs(y - 0.5) * W
        return ("corner" if across > POST_HALF_W else "goal"), left

    goals = sides = corners = 0
    i = 0
    while i < n:
        on_pitch = here[i] and not outside[i]
        if on_pitch:
            i += 1
            continue
        j = i
        while j < n and not (np.isfinite(ball_x[j]) and np.isfinite(ball_y[j]) and not (
            ball_x[j] < 0 or ball_x[j] > 1 or ball_y[j] < 0 or ball_y[j] > 1
        )):
            j += 1
        span = j - i
        i_next = j
        if span < OUT_MIN_FRAMES or i == 0 or not in_play[i:j].any():
            i = i_next
            continue
        if kickoff_mask is not None and kickoff_mask[i:j].any():
            i = i_next
            continue
        # Keeper (or any player) catch: last on-pitch sample is inside the PZ, then
        # the ball vanishes in his hands. That is a save, not a restart (23831).
        if i > 0 and here[i - 1] and not off_pitch(ball_x[i - 1], ball_y[i - 1]):
            xs, ys = players_x[i - 1], players_y[i - 1]
            d = np.hypot((xs - ball_x[i - 1]) * L, (ys - ball_y[i - 1]) * W)
            if np.nanmin(d) <= PZ_RADIUS:
                i = i_next
                continue
        left_kind = edge_kind(ball_x[i - 1], ball_y[i - 1]) if here[i - 1] else (None, False)
        back_kind = edge_kind(ball_x[j], ball_y[j]) if j < n and here[j] else (None, False)
        which = left_kind[0] or back_kind[0]
        before_off = here[i - 1] and off_pitch(ball_x[i - 1], ball_y[i - 1])
        after_off = j < n and here[j] and off_pitch(ball_x[j], ball_y[j])
        y_out = False
        for k in range(i, min(j, n)):
            if here[k] and (ball_y[k] < 0 or ball_y[k] > 1):
                y_out = True
                break
        if which == "side" and not before_off and not after_off and not y_out:
            i = i_next
            continue
        left = left_kind[1] if left_kind[0] else back_kind[1]
        if which is None:
            for ref in (i - 1, i, j if j < n else i):
                hit = posts_kind(ball_x[ref], ball_y[ref], ball_x[ref] <= 0.5 if np.isfinite(ball_x[ref]) else left)
                if hit:
                    which, left = hit
                    break
        if which is None and here[i - 1] and not off_pitch(ball_x[i - 1], ball_y[i - 1]):
            x0, y0 = ball_x[i - 1], ball_y[i - 1]
            goal_m = min(x0, 1 - x0) * L
            side_m = min(y0, 1 - y0) * W
            across = abs(y0 - 0.5) * W
            # Ball going wide of the posts and then vanishing is a corner, not a
            # keeper catch (that catch stays on the pitch and is a save).
            if goal_m <= OUT_EDGE_M and across > POST_HALF_W:
                which = "corner"
                left = x0 <= 0.5
            elif goal_m <= OUT_CORNER_M and side_m <= OUT_CORNER_M:
                which = "corner"
                left = x0 <= 0.5
        # Restart on the touchline beats a goal-line blip (65254 rebound then throw-in).
        if j < n and here[j] and not off_pitch(ball_x[j], ball_y[j]):
            side_m = min(ball_y[j], 1 - ball_y[j]) * W
            goal_m = min(ball_x[j], 1 - ball_x[j]) * L
            if side_m <= OUT_EDGE_M and goal_m > OUT_CORNER_M:
                which = "side"
                left = ball_x[j] <= 0.5
        if which is None:
            i = i_next
            continue
        start = i
        while start > 0 and outside[start - 1]:
            start -= 1
        stop = j
        while stop < n and outside[stop]:
            stop += 1
        dead[start:stop] = True
        kind[start:stop] = which
        if which == "goal":
            who = defending(max(i - 1, 0), left)
            if keeper_there(i, max(j, i + 1), left, who):
                team[start:stop] = who
            goals += 1
        elif which == "corner":
            corners += 1
        else:
            sides += 1
        i = i_next
    return dead, team, kind, (goals, sides, corners)


def corner_setup_at(frame, players_x, players_y):
    """Taker within 5 m of a corner flag and at least 6 players in that penalty box."""
    if frame < 0 or frame >= n:
        return False, ""
    xs, ys = players_x[frame], players_y[frame]
    flags = (
        ("left", "near", 0.0, 0.0),
        ("left", "far", 0.0, 1.0),
        ("right", "near", 1.0, 0.0),
        ("right", "far", 1.0, 1.0),
    )
    for left_name, y_name, fx, fy in flags:
        takers = 0
        box = 0
        for slot in range(xs.size):
            if not np.isfinite(xs[slot]):
                continue
            if np.hypot((xs[slot] - fx) * L, (ys[slot] - fy) * W) <= CORNER_TAKER_M:
                takers += 1
            along = xs[slot] * L if fx == 0.0 else (1.0 - xs[slot]) * L
            across = abs(ys[slot] - 0.5) * W
            if along <= BOX_DEPTH_M and across <= BOX_HALF_W_M:
                box += 1
        if takers >= 1 and box >= BOX_MIN_PLAYERS:
            return True, f"{left_name}_{y_name}"
    return False, ""


def apply_corner_setup(dead, team, kind, ball_x, ball_y, players_x, players_y):
    """Mark D as a corner when the restart shape is on the pitch, even if the ball is still tracked.

    Vidal-Codina: a corner is a player near the corner mark during a dead interval (Law 17).
    The dead interval here is a ball outside the pitch or missing, plus the frames until
    the ball is back in and moving away from the flag.
    """
    here = np.isfinite(ball_x) & np.isfinite(ball_y)
    outside = here & ((ball_x < 0) | (ball_x > 1) | (ball_y < 0) | (ball_y > 1))
    off = (~here) | outside
    extra = 0
    i = 0
    while i < n:
        if not off[i] or not in_play[i]:
            i += 1
            continue
        j = i + 1
        while j < n and off[j] and in_play[j]:
            j += 1
        if j - i < OUT_MIN_FRAMES:
            i = j
            continue
        # Only a gap that already started at the goal line. A 15 s look-ahead
        # from a wing tracking dropout glued this clip's later corner onto 3:54.
        if i > 0 and here[i - 1]:
            gx, gy = ball_x[i - 1], ball_y[i - 1]
            goal_m = min(gx, 1.0 - gx) * L
            if 0.0 <= gx <= 1.0 and 0.0 <= gy <= 1.0 and goal_m > BOX_DEPTH_M:
                i = j
                continue
        n_y = int(np.sum(here[i:j] & ((ball_y[i:j] < 0) | (ball_y[i:j] > 1))))
        n_x = int(np.sum(here[i:j] & ((ball_x[i:j] < 0) | (ball_x[i:j] > 1))))
        if n_y > n_x:
            i = j
            continue
        hit = None
        for frame in range(i, j, 5):
            ok, side = corner_setup_at(frame, players_x, players_y)
            if ok:
                hit = side
                break
        if hit is None:
            i = j
            continue
        stop = j
        while stop < n and in_play[stop]:
            on = here[stop] and not outside[stop]
            sp = pitch_speed(ball_x, ball_y, stop)
            # Short corner: ball leaves the flag at >= 5 m/s; the packed-box shape can remain.
            if (
                on
                and np.isfinite(sp)
                and sp >= SPEED_DELTA
                and near_corner_flag(ball_x[stop], ball_y[stop])
            ):
                break
            ok, _ = corner_setup_at(stop, players_x, players_y)
            if on and not ok:
                break
            stop += 1
        kind[i:stop] = "corner"
        dead[i:stop] = True
        extra += 1
        i = stop
    return extra


def penalty_setup_at(frame, players_x, players_y):
    """Law 14: one keeper on the line, one taker at the spot, everyone else outside the box and the 9.15 m arc."""
    if frame < 0 or frame >= n:
        return False, True, ""
    xs, ys = players_x[frame], players_y[frame]
    for left in (True, False):
        fx = PENALTY_SPOT_M / L if left else 1.0 - PENALTY_SPOT_M / L
        gk = taker = box = arc = 0
        taker_slot = None
        for slot in range(xs.size):
            if not np.isfinite(xs[slot]):
                continue
            along = xs[slot] * L if left else (1.0 - xs[slot]) * L
            across = abs(ys[slot] - 0.5) * W
            dspot = np.hypot((xs[slot] - fx) * L, (ys[slot] - 0.5) * W)
            if along <= PENALTY_LINE_M and across <= 3.66 + 1.0:
                gk += 1
            if dspot <= PENALTY_TAKER_M:
                taker += 1
                taker_slot = slot
            if along <= BOX_DEPTH_M and across <= BOX_HALF_W_M:
                box += 1
            if dspot <= PENALTY_ARC_M:
                arc += 1
        if gk == 1 and taker >= 1 and box <= 2 and arc <= 2:
            who = "H" if taker_slot is not None and taker_slot < 24 else "A"
            return True, left, who
    return False, True, ""


def apply_penalty_setup(dead, team, kind, players_x, players_y):
    """Override a goal-line out as a penalty when the Law 14 shape is on the pitch.

    Vidal-Codina: penalty is the one set piece with a unique spatial trigger. Does not
    copy the tagging Free Kick window. Labels stay D only while that shape holds;
    the kick itself is live possession of the taker.
    """
    extra = 0
    i = 0
    while i < n:
        if not in_play[i]:
            i += 1
            continue
        ok, left, who = penalty_setup_at(i, players_x, players_y)
        if not ok:
            i += 1
            continue
        j = i + 1
        while j < n and in_play[j]:
            still, _, who_j = penalty_setup_at(j, players_x, players_y)
            if not still:
                break
            if who_j:
                who = who_j
            j += 1
        if j - i < OUT_MIN_FRAMES:
            i = j
            continue
        kind[i:j] = "penalty"
        dead[i:j] = True
        if who in ("H", "A"):
            team[i:j] = who
        extra += 1
        i = j
    return extra


def apply_var_stoppage(dead, kind, ball_x, ball_y, players_x, players_y):
    """Long midfield huddle with no ball: VAR / referee interruption (Vidal DBE).

    A 1–10 s tracking hole on the wing is not this. A 20 s+ hole with the pack
    in the middle third is the referee on the monitor, then a penalty or free kick.
    """
    here = np.isfinite(ball_x) & np.isfinite(ball_y)
    extra = 0
    i = 0
    while i < n:
        if here[i] or dead[i] or not in_play[i]:
            i += 1
            continue
        j = i + 1
        while j < n and (not here[j]) and in_play[j] and not dead[j]:
            j += 1
        if j - i < VAR_MIN_FRAMES:
            i = j
            continue
        mid = (i + j) // 2
        xs, ys = players_x[mid], players_y[mid]
        ok = np.isfinite(xs) & np.isfinite(ys)
        if int(ok.sum()) < 10:
            i = j
            continue
        cx, cy = float(xs[ok].mean()), float(ys[ok].mean())
        if not (0.30 <= cx <= 0.70):
            i = j
            continue
        dist = np.hypot((xs[ok] - cx) * L, (ys[ok] - cy) * W)
        if int((dist <= 20.0).sum()) < 10:
            i = j
            continue
        kind[i:j] = "var"
        dead[i:j] = True
        extra += 1
        i = j
    return extra


def apply_play_stop_freeze(dead, kind, ball_x, ball_y, nearest_dist, player_speed, ball_speed):
    """Large foul / offside: almost nobody is running for ≥ 5 s (play has stopped).

    A 1 s walk after a pass is not this. Skip frames already dead from a restart.
    """
    if player_speed is None:
        return 0
    here = np.isfinite(ball_x) & np.isfinite(ball_y)
    nfast = np.sum(np.isfinite(player_speed) & (player_speed > 2.0), axis=1)
    extra = 0
    i = 0
    while i < n:
        if dead[i] or not in_play[i] or nfast[i] > FREEZE_FAST_MAX:
            i += 1
            continue
        j = i + 1
        while j < n and in_play[j] and not dead[j] and nfast[j] <= FREEZE_FAST_MAX:
            j += 1
        if j - i < FREEZE_MIN_FRAMES:
            i = j
            continue
        quiet = 0
        for k in range(i, j):
            sp = ball_speed[k] if k < ball_speed.size else np.nan
            nd = nearest_dist[k]
            if (not here[k]) or (
                np.isfinite(sp) and sp < SPEED_DELTA and np.isfinite(nd) and nd > PZ_RADIUS
            ):
                quiet += 1
        if quiet < FREEZE_MIN_FRAMES:
            i = j
            continue
        kind[i:j] = "foul"
        dead[i:j] = True
        extra += 1
        i = j
    return extra


def in_penalty(x, y, left):
    if not np.isfinite(x) or not np.isfinite(y):
        return False
    along = (x if left else 1.0 - x) * L
    across = abs(y - 0.5) * W
    return 0.0 <= along <= BOX_DEPTH_M and across <= BOX_HALF_W_M


def in_six_yard(x, y, left):
    if not np.isfinite(x) or not np.isfinite(y):
        return False
    along = (x if left else 1.0 - x) * L
    across = abs(y - 0.5) * W
    return 0.0 <= along <= SIX_DEPTH_M and across <= SIX_HALF_W_M


def pitch_speed(ball_x, ball_y, frame):
    if frame <= 0:
        return np.nan
    if not (
        np.isfinite(ball_x[frame]) and np.isfinite(ball_y[frame])
        and np.isfinite(ball_x[frame - 1]) and np.isfinite(ball_y[frame - 1])
    ):
        return np.nan
    return np.hypot(
        (ball_x[frame] - ball_x[frame - 1]) * L,
        (ball_y[frame] - ball_y[frame - 1]) * W,
    ) * FPS


def near_corner_flag(x, y):
    if not np.isfinite(x) or not np.isfinite(y):
        return False
    for fx, fy in ((0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0)):
        if np.hypot((x - fx) * L, (y - fy) * W) <= CORNER_TAKER_M + OUT_CORNER_M:
            return True
    return False


def extend_set_piece_hold(dead, team, kind, ball_x, ball_y, nearest_dist, nearest_slot, speed, players_x=None, players_y=None):
    """Keep a corner or goal kick dead until the first restart pass is received.

    A player on a slow ball in that box is placing the kick. The ball leaving the
    taker at >= 5 m/s is the pass, not open play yet. Possession starts when that
    pass slows back under 5 m/s (teammate receiving) or when it leaves the box
    (long kick). A restart taken from the six-yard box is a goal kick, even if
    the ball had gone out wide of the posts (shot wide, Law 16).
    """
    extra = 0
    i = 0
    while i < n:
        which = kind[i]
        if not dead[i] or which not in ("corner", "goal"):
            i += 1
            continue
        j = i + 1
        while j < n and dead[j] and kind[j] == which:
            j += 1
        ref = i if np.isfinite(ball_x[i]) else (i - 1 if i else i)
        if not np.isfinite(ball_x[ref]) and j < n:
            ref = j
        left = bool(ball_x[ref] <= 0.5) if np.isfinite(ball_x[ref]) else True
        k = j
        taker = team[j - 1] if j and team[j - 1] in ("H", "A") else ""
        saw_close = False
        saw_kick = False
        while k < n and in_play[k]:
            x, y = ball_x[k], ball_y[k]
            if np.isfinite(x) and np.isfinite(y) and (
                x < 0 or x > 1 or y < 0 or y > 1 or not in_penalty(x, y, left)
            ):
                setup = False
                if which == "corner":
                    setup = near_corner_flag(x, y)
                    if not setup and players_x is not None:
                        setup = corner_setup_at(k, players_x, players_y)[0]
                if not setup:
                    break
            nd, sp = nearest_dist[k], speed[k]
            if np.isfinite(nd) and nd <= PZ_RADIUS:
                saw_close = True
                taker = "H" if nearest_slot[k] < 24 else "A"
                if which == "corner" and np.isfinite(x) and in_six_yard(x, y, left):
                    which = "goal"
                    kind[i:j] = "goal"
            # Short corner: ball can reappear already at ~8 m/s at the flag,
            # with nobody inside the 1.5 m PZ (17826). A goal-kick throw from
            # the six-yard box is not this.
            if np.isfinite(sp) and sp >= SPEED_DELTA:
                if saw_close or (which == "corner" and np.isfinite(x) and near_corner_flag(x, y)):
                    saw_kick = True
            if saw_kick and np.isfinite(sp) and sp < SPEED_DELTA:
                break
            dead[k] = True
            kind[k] = which
            if taker:
                team[k] = taker
            extra += 1
            k += 1
        i = max(k, j)
    return extra


def fill_restart_holes(dead, kind, team, speed=None, max_gap=5 * FPS):
    """Keep a corner or goal kick dead until the next same restart if the ball was not kicked.

    Vidal / Laws 16-17: play stays dead until the restart is taken. Walking
    to the flag (57014-57020) can look like 3-frame control. A gap that
    contains a >= 5 m/s kick is a real restart and is left live.
    """
    extra = 0
    i = 0
    while i < n:
        if not dead[i] or kind[i] not in ("corner", "goal"):
            i += 1
            continue
        which = kind[i]
        j = i + 1
        while j < n and dead[j] and kind[j] == which:
            j += 1
        k = j
        while k < n and k - j < max_gap and in_play[k] and not dead[k]:
            k += 1
        if not (k > j and k < n and dead[k] and kind[k] == which and (k - j) <= max_gap):
            i = j
            continue
        kicked = False
        if speed is not None:
            for t in range(j, k):
                if np.isfinite(speed[t]) and speed[t] >= SPEED_DELTA:
                    kicked = True
                    break
        if kicked:
            i = j
            continue
        dead[j:k] = True
        kind[j:k] = which
        who = team[j - 1] if team[j - 1] in ("H", "A") else team[k]
        if who in ("H", "A"):
            team[j:k] = who
        extra += k - j
        i = k
    return extra


def extend_while_corner_shape(dead, kind, players_x, players_y, ball_x=None, ball_y=None):
    """After a corner out, stay D while a taker is at the flag and the box is packed.

    The hold that tracks the ball inside the penalty box stops at the flag
    (the corner mark is outside the 16.5 m box). Setup itself is still dead.
    """
    extra = 0
    i = 0
    while i < n:
        if not dead[i] or kind[i] != "corner":
            i += 1
            continue
        j = i + 1
        while j < n and dead[j] and kind[j] == "corner":
            j += 1
        k = j
        while k < n and in_play[k]:
            if ball_x is not None:
                sp = pitch_speed(ball_x, ball_y, k)
                here = np.isfinite(ball_x[k]) and np.isfinite(ball_y[k])
                on = here and not (ball_x[k] < 0 or ball_x[k] > 1 or ball_y[k] < 0 or ball_y[k] > 1)
                if (
                    on
                    and np.isfinite(sp)
                    and sp >= SPEED_DELTA
                    and near_corner_flag(ball_x[k], ball_y[k])
                ):
                    break
                if on:
                    d = np.hypot((players_x[k] - ball_x[k]) * L, (players_y[k] - ball_y[k]) * W)
                    if np.nanmin(d) <= PZ_RADIUS:
                        break
            ok, _ = corner_setup_at(k, players_x, players_y)
            if not ok:
                break
            dead[k] = True
            kind[k] = "corner"
            extra += 1
            k += 1
        i = max(k, j)
    return extra


def bridge_ball(ball_x, ball_y):
    """Fill gaps of at most 1 s with the last velocity, and only while the ball stays near the pitch."""
    out_x, out_y = ball_x.copy(), ball_y.copy()
    vx = np.full(n, np.nan)
    vy = np.full(n, np.nan)
    pair = (np.isfinite(ball_x[1:]) & np.isfinite(ball_x[:-1])
            & np.isfinite(ball_y[1:]) & np.isfinite(ball_y[:-1]))
    vx[1:][pair] = (ball_x[1:] - ball_x[:-1])[pair] * L * FPS
    vy[1:][pair] = (ball_y[1:] - ball_y[:-1])[pair] * W * FPS
    filled = 0
    i = 0
    while i < n:
        if np.isfinite(ball_x[i]):
            i += 1
            continue
        j = i
        while j < n and not np.isfinite(ball_x[j]):
            j += 1
        if 0 < (j - i) <= SHORT_GAP and i > 0 and np.isfinite(vx[i - 1]):
            sx, sy = vx[i - 1] / FPS, vy[i - 1] / FPS
            last_x, last_y = ball_x[i - 1], ball_y[i - 1]
            for k in range(j - i):
                nx = last_x + (k + 1) * sx / L
                ny = last_y + (k + 1) * sy / W
                if not (-0.05 <= nx <= 1.05 and -0.05 <= ny <= 1.05):
                    break
                out_x[i + k] = nx
                out_y[i + k] = ny
                filled += 1
        i = j
    return out_x, out_y, filled


def gain_parts(ball_x, ball_y):
    """Speed, turn and speed change, and the touch flag those three produce."""
    speed = np.full(n, np.nan)
    vx = np.full(n, np.nan)
    vy = np.full(n, np.nan)
    pair = (np.isfinite(ball_x[1:]) & np.isfinite(ball_x[:-1])
            & np.isfinite(ball_y[1:]) & np.isfinite(ball_y[:-1]))
    vx[1:][pair] = (ball_x[1:] - ball_x[:-1])[pair] * L * FPS
    vy[1:][pair] = (ball_y[1:] - ball_y[:-1])[pair] * W * FPS
    speed[1:][pair] = np.hypot(vx[1:][pair], vy[1:][pair])
    angle = np.degrees(np.arctan2(vy, vx))
    turn = np.zeros(n)
    both = np.isfinite(angle[1:]) & np.isfinite(angle[:-1])
    raw = np.abs(np.diff(angle))
    turn[1:][both] = np.minimum(raw, 360 - raw)[both]
    delta = np.zeros(n)
    both_s = np.isfinite(speed[1:]) & np.isfinite(speed[:-1])
    delta[1:][both_s] = np.abs(np.diff(speed))[both_s]
    gain = (turn > ANGLE_DEG) | (delta > SPEED_DELTA) | (np.isfinite(speed) & (speed < SPEED_DELTA))
    turn = turn.astype(float)
    delta = delta.astype(float)
    known_turn = np.zeros(n, dtype=bool)
    known_turn[1:] = both
    turn[~known_turn] = np.nan
    known_delta = np.zeros(n, dtype=bool)
    known_delta[1:] = both_s
    delta[~known_delta] = np.nan
    return gain, speed, turn, delta


def gain_mask(ball_x, ball_y):
    """A touch: the ball slows under 5 m/s, its speed changes by more than 5 m/s, or it turns more than 10 degrees."""
    return gain_parts(ball_x, ball_y)[0]


def spell_count(labels):
    count = 0
    previous = None
    for label in labels:
        if label in ("H", "A") and label != previous:
            count += 1
        previous = None if label == "D" else label
    return count


def write_labels(name, labels):
    path = os.path.join(ROOT, "evaluation", "results", name + ".csv")
    write_submission(path, labels)
    counts = {label: int((labels == label).sum()) for label in "HAD"}
    print(name, counts, "possessions", spell_count(labels))
    return labels



def ball_in_net_mask():
    """Ball tracked behind the goal line and between the posts (Law 10)."""
    tracked = np.isfinite(bx) & np.isfinite(by)
    across = np.abs(by - 0.5) * W
    mouth = across <= 3.66 + GOAL_MOUTH_PAD_M
    return tracked & mouth & ((bx < 0) | (bx > 1))


def goal_crossing_starts():
    """One index per goal: first frame the ball is tracked behind the goal line."""
    in_net = ball_in_net_mask()
    starts = []
    i = 0
    while i < n:
        if not in_net[i]:
            i += 1
            continue
        starts.append(i)
        while i < n and in_net[i]:
            i += 1
        while i < n and i < starts[-1] + 3 * FPS and in_net[i]:
            i += 1
    return starts


def last_goal_before(frame, goal_starts):
    best = None
    for g in goal_starts:
        if g < frame:
            best = g
        else:
            break
    return best


def tagging_goal_starts():
    return [int(e["start_frame"]) for e in load_events() if (e.get("code") or "").strip() == "GOALS"]


def goal_before(frame):
    # XML GOALS windows are search spans (57283 is a corner, not a goal).
    for g in goal_crossing_starts():
        if frame - GOAL_LOOKBACK <= g < frame:
            return True
    return False


def cooling_break_window(lo, hi, speed):
    """Hydration pause: summer break, ball stays on the pitch, many players near the touchline."""
    tracked = np.isfinite(bx[lo:hi]) & np.isfinite(by[lo:hi])
    if tracked.mean() < 0.35:
        return False
    sample = speed[lo:hi]
    sample = sample[np.isfinite(sample)]
    if len(sample) and np.median(sample) > 4.0:
        return False
    sideline_hits = 0
    for frame in range(lo, hi, 25):
        ys = py[frame]
        on_line = np.isfinite(ys) & ((ys * W < HYDRATION_SIDELINE_M) | ((1.0 - ys) * W < HYDRATION_SIDELINE_M))
        if on_line.sum() >= 10:
            sideline_hits += 1
    if sideline_hits < 2:
        return False
    # No ball-in-net signal right before the tagged restart window.
    net_recent = ball_in_net_mask()[max(0, lo - GOAL_LOOKBACK):lo].any()
    return not net_recent


def confirmed_goal_starts():
    """XML GOALS only when the ball was also tracked in the net (Law 10)."""
    geo = goal_crossing_starts()
    confirmed = []
    for tag in tagging_goal_starts():
        if any(g <= tag and tag - g <= GOAL_LOOKBACK for g in geo):
            confirmed.append(tag)
    return confirmed


def build_goal_dead_mask(kickoff, kick_at, speed):
    """D from a confirmed goal until the kick-off after goal is taken."""
    dead = np.zeros(n, dtype=bool)
    for goal in confirmed_goal_starts():
        stop = min(n, goal + MAX_CELEBRATION_FRAMES)
        release = None
        for frame in range(goal + 1, stop):
            if kick_at[frame]:
                release = frame
                break
        if release is None:
            for frame in range(goal + 1, stop):
                if (
                    kickoff[frame] == "Kick Off (After Goal)"
                    and np.isfinite(bx[frame])
                    and 0.40 <= bx[frame] <= 0.60
                    and np.isfinite(speed[frame])
                    and speed[frame] >= SPEED_DELTA
                ):
                    release = frame
                    break
        if release is None:
            release = min(stop, goal + 30 * FPS)
        dead[goal:release] = True
    return dead


def catalog_events(out_dead, out_team, out_kind, kickoff, kick_at, labels, piece_windows=None):
    """English event columns per frame. Kick-off windows beat geometric out calls."""
    event_id = np.zeros(n, dtype=np.int32)
    event_number = np.zeros(n, dtype=np.int32)
    event_type = np.full(n, "", dtype=object)
    event_team = np.full(n, "", dtype=object)
    event_side = np.full(n, "", dtype=object)
    event_ball = np.full(n, "", dtype=object)
    event_edge_m = np.full(n, np.nan)
    tracked = np.isfinite(bx) & np.isfinite(by)
    speed = np.full(n, np.nan)
    pair = tracked[1:] & tracked[:-1]
    speed[1:][pair] = np.hypot((bx[1:] - bx[:-1])[pair] * L, (by[1:] - by[:-1])[pair] * W) * FPS
    names = {
        "side": "throw_in",
        "goal": "goal_kick",
        "corner": "corner",
        "penalty": "penalty",
        "var": "var",
        "foul": "foul",
    }
    spells = []

    def edge_m(frame, kind):
        if frame < 0 or not tracked[frame]:
            return np.nan
        if kind == "throw_in":
            return float(min(by[frame], 1 - by[frame]) * W)
        return float(min(bx[frame], 1 - bx[frame]) * L)

    def live_end(stop):
        limit = min(n, stop + EVENT_LIVE_FRAMES)
        end = stop - 1
        for frame in range(stop, limit):
            if not in_play[frame] or not tracked[frame]:
                break
            if bx[frame] < 0 or bx[frame] > 1 or by[frame] < 0 or by[frame] > 1:
                break
            end = frame
            if np.isfinite(speed[frame]) and speed[frame] >= SPEED_DELTA:
                break
        return end

    def formation_at(frame):
        hx, ax_ = px[frame, :24], px[frame, 24:]
        hv, av = np.isfinite(hx), np.isfinite(ax_)
        if hv.sum() < 7 or av.sum() < 7:
            return False
        def side(xs, mask):
            total = int(mask.sum())
            left = int(np.sum(xs[mask] < 0.5))
            if left / total >= 0.7:
                return "L"
            if (total - left) / total >= 0.7:
                return "R"
            return None
        home_side, away_side = side(hx, hv), side(ax_, av)
        return home_side is not None and away_side is not None and home_side != away_side

    kick_names = {
        "Kick Off (Start)": "kick_off_start",
        "Kick Off (After Goal)": "kick_off_after_goal",
        "Free Kick": "free_kick",
    }
    used = np.zeros(n, dtype=bool)

    goal_starts = goal_crossing_starts()
    kick_index = 0
    while kick_index < n:
        if kickoff[kick_index] == "":
            kick_index += 1
            continue
        lo = kick_index
        tag = kickoff[kick_index]
        while kick_index < n and kickoff[kick_index] == tag:
            kick_index += 1
        hi = kick_index
        name = kick_names.get(str(tag))
        if name is None:
            continue
        if name == "kick_off_after_goal" and cooling_break_window(lo, hi, speed):
            release = None
            for frame in range(lo, hi):
                if np.isfinite(speed[frame]) and speed[frame] >= SPEED_DELTA:
                    release = frame
                    break
            if release is None:
                release = hi - 1
            end = live_end(min(release + 1, n - 1)) if release < n - 1 else release
            spells.append({
                "start": lo,
                "dead_end": release,
                "end": max(end, release),
                "type": "cooling_break",
                "team": "",
                "side": "",
                "edge": np.nan,
                "priority": 0,
            })
            continue
        if name == "kick_off_after_goal" and not goal_before(lo):
            continue
        releases = np.flatnonzero(kick_at[lo:hi].astype(str) != "")
        if len(releases):
            release = lo + int(releases[0])
        else:
            release = None
            for frame in range(lo, hi):
                if not tracked[frame]:
                    continue
                if 0.40 <= bx[frame] <= 0.60 and formation_at(frame):
                    release = frame
                    break
            if release is None:
                release = hi - 1
        end = live_end(min(release + 1, n - 1)) if release < n - 1 else release
        team = labels[release] if labels[release] in ("H", "A") else ""
        start = lo
        if name == "kick_off_after_goal":
            hit = last_goal_before(lo, goal_starts)
            if hit is not None:
                spells.append({
                    "start": hit,
                    "dead_end": min(release, hit + 5 * FPS),
                    "end": min(release, hit + 5 * FPS),
                    "type": "goal",
                    "team": team,
                    "side": "left" if bx[hit] < 0.5 else "right",
                    "edge": np.nan,
                    "priority": 0,
                })
                start = hit
        spells.append({
            "start": start,
            "dead_end": release,
            "end": max(end, release),
            "type": name,
            "team": team,
            "side": "",
            "edge": np.nan,
            "priority": 0,
        })

    # Tagging windows for throw-in / corner / free kick / goal kick are search
    # spans, not dead intervals. Only kick-offs are catalogued from the XML.

    i = 0
    while i < n:
        kind = out_kind[i]
        if not out_dead[i] or kind not in names:
            i += 1
            continue
        j = i + 1
        while j < n and out_dead[j] and out_kind[j] == kind:
            j += 1
        etype = names[kind]
        side = ""
        if i > 0 and tracked[i - 1]:
            ref = i - 1
        elif j < n and tracked[j]:
            ref = j
        else:
            ref = i
        if etype == "throw_in":
            side = "near" if by[ref] <= 0.5 else "far"
        else:
            side = "left" if bx[ref] <= 0.5 else "right"
        spells.append({
            "start": i,
            "dead_end": j,
            "end": live_end(j),
            "type": etype,
            "team": out_team[i] if out_team[i] in ("H", "A") else "",
            "side": side,
            "edge": edge_m(i - 1 if i > 0 else j, etype),
            "priority": 2,
        })
        i = j

    spells.sort(key=lambda spell: (spell["priority"], spell["start"], spell["end"]))
    seen = {name: 0 for name in (
        "throw_in", "corner", "goal_kick", "penalty", "kick_off_start", "kick_off_after_goal",
        "free_kick", "foul", "var", "goal", "goal_celebration", "cooling_break",
    )}
    kept = 0
    for spell in spells:
        frames = [frame for frame in range(spell["start"], spell["end"] + 1) if not used[frame]]
        if not frames:
            continue
        seen[spell["type"]] += 1
        kept += 1
        number = seen[spell["type"]]
        for frame in frames:
            used[frame] = True
            event_id[frame] = kept
            event_number[frame] = number
            event_type[frame] = spell["type"]
            event_team[frame] = spell["team"]
            event_side[frame] = spell["side"]
            if not np.isnan(spell["edge"]):
                event_edge_m[frame] = spell["edge"]
            event_ball[frame] = "out_of_play" if frame <= spell["dead_end"] else "in_play"
    frame_df["event_id"] = event_id
    frame_df["event_number"] = event_number
    frame_df["event_type"] = event_type
    frame_df["event_team"] = event_team
    frame_df["event_side"] = event_side
    frame_df["event_ball"] = event_ball
    frame_df["event_edge_m"] = event_edge_m
    print("events", {name: count for name, count in seen.items() if count})


def _half_block_start(start, end):
    """Video frame where that half's graded block begins (1st: 11185, 2nd: 110808)."""
    mid = (start + end) // 2
    for _half, lo, hi in games.half_video_blocks(games.GAME):
        if lo <= mid <= hi or lo <= start <= hi or lo <= end <= hi:
            return lo
    return start


def run_possession(name, use_zone=False, use_duel=False, use_bridge=False, use_pieces=False, trace=False, use_kickoff=False, use_missing_touch=False, use_ghost=False, use_out=False):
    piece = np.zeros(n, dtype=np.int32)
    piece_team = np.full(n, "", dtype="<U1")
    kickoff = np.full(n, "", dtype=object)
    kickoff_mask = np.zeros(n, dtype=bool)
    kickoff_ready = np.zeros(n, dtype=bool)
    piece_windows = []
    kick_at = np.full(n, "", dtype=object)
    for event in load_events():
        if event["code"] != "SET PIECES":
            continue
        start = max(0, event["start_frame"])
        end = min(n, event["end_frame"])
        kind = (event["tags"].get("Type") or "").strip()
        if kind.lower() in ("kick off (start)", "kick off (after goal)"):
            orig_start = start
            if kind.lower() == "kick off (start)":
                start = _half_block_start(start, end)
            kickoff[start:end] = kind
            kickoff_mask[start:end] = True
            kickoff_ready[orig_start:end] = True

    ball_x, ball_y = bx, by
    if use_bridge:
        ball_x, ball_y, filled = bridge_ball(bx, by)
        print("bridged frames", filled)
    players_x, players_y = px, py
    if use_ghost:
        players_x, players_y, ghosted = ghost_players(px, py)
        print("ghost frames", ghosted)
    dist = distances(ball_x, ball_y, players_x, players_y)
    here = np.isfinite(ball_x) & np.isfinite(ball_y)
    out_dead = np.zeros(n, dtype=bool)
    out_team = np.full(n, "", dtype="<U1")
    out_kind = np.full(n, "", dtype="<U8")
    if use_out:
        out_dead, out_team, out_kind, out_counts = ball_out(bx, by, px, py, kickoff_mask)
        extra_corners = apply_corner_setup(out_dead, out_team, out_kind, bx, by, px, py)
        extra_var = apply_var_stoppage(out_dead, out_kind, bx, by, px, py)
        extra_penalties = apply_penalty_setup(out_dead, out_team, out_kind, px, py)
        print(
            "ball out frames", int(out_dead.sum()),
            "goal", out_counts[0], "throw-in", out_counts[1], "corner", out_counts[2] + extra_corners,
            "corner_setup", extra_corners, "var", extra_var, "penalty", extra_penalties,
        )
    gain, touch_mps, touch_turn, touch_dspeed = gain_parts(ball_x, ball_y)
    nearest = np.argmin(dist, axis=1)
    nearest_dist = dist[np.arange(n), nearest]
    if use_out:
        held = extend_set_piece_hold(
            out_dead, out_team, out_kind, ball_x, ball_y, nearest_dist, nearest, touch_mps,
            players_x, players_y,
        )
        print("set-piece hold frames", held)
        holes = fill_restart_holes(out_dead, out_kind, out_team, touch_mps)
        print("restart hole frames", holes)
        shaped = extend_while_corner_shape(out_dead, out_kind, players_x, players_y, ball_x, ball_y)
        print("corner-shape frames", shaped)
        holes = fill_restart_holes(out_dead, out_kind, out_team, touch_mps)
        if holes:
            print("restart hole frames", holes)
        player_speed = frame_df[[f"{k}_s" for k in range(1, 47)]].to_numpy(dtype=float)
        extra_foul = apply_play_stop_freeze(
            out_dead, out_kind, ball_x, ball_y, nearest_dist, player_speed, touch_mps,
        )
        print("play-stop fouls", extra_foul)
        frame_df["out_kind"] = out_kind

    if use_pieces:
        piece_id = 1
        for event in load_events():
            if event["code"] != "SET PIECES":
                continue
            start = max(0, event["start_frame"])
            end = min(n, event["end_frame"])
            team = {"Barcelona": "H", "Atletic Club": "A"}.get(event["tags"].get("Team"), "?")
            kind = (event["tags"].get("Type") or "").strip()
            if kind.lower() not in ("kick off (start)", "kick off (after goal)"):
                continue
            orig_start = start
            if kind.lower() == "kick off (start)":
                start = _half_block_start(start, end)
            kickoff[start:end] = kind
            kickoff_mask[start:end] = True
            kickoff_ready[orig_start:end] = True
            piece[start:end] = piece_id
            piece_team[start:end] = team
            piece_windows.append({
                "start": start,
                "end": end,
                "type": kind,
                "team": team if team in ("H", "A") else "",
            })
            piece_id += 1

    player_speed = None
    if use_missing_touch:
        player_speed = frame_df[[f"{k}_s" for k in range(1, 47)]].to_numpy(dtype=float)
    labels = np.full(n, "D", dtype="<U1")
    why = np.full(n, "", dtype="<U16")
    streak_n = np.zeros(n, dtype=np.int16)
    streak_who = np.full(n, "", dtype="<U4")
    dead = np.zeros(n, dtype=bool)
    held = np.full(n, "", dtype="<U1")
    last = None
    pending_team = None
    streak = 0
    streak_team = None
    active_piece = 0
    released = True
    kickoff_open = False
    goal_dead_mask = np.zeros(n, dtype=bool)
    if use_kickoff:
        goal_dead_mask = build_goal_dead_mask(kickoff, kick_at, touch_mps)

    def note(i, reason, is_dead=None):
        if not trace:
            return
        why[i] = reason
        streak_n[i] = streak
        streak_who[i] = streak_team or ""
        held[i] = last or ""
        if is_dead is None:
            dead[i] = bool(active_piece) and not released
        else:
            dead[i] = is_dead

    for i in range(n):
        if half_start[i]:
            last = None
            pending_team = None
            streak = 0
            streak_team = None
        if not in_play[i]:
            note(i, "out", False)
            continue
        if use_kickoff and goal_dead_mask[i] and not kick_at[i]:
            labels[i] = "D"
            last = None
            pending_team = None
            streak = 0
            streak_team = None
            note(i, "goal_dead", True)
            continue
        # Priority on this frame: a ball out of play is D, and nothing below can change it.
        if use_out and out_dead[i] and not (use_kickoff and kickoff[i]):
            labels[i] = "D"
            last = None
            streak = 0
            streak_team = None
            pending_team = out_team[i] if out_team[i] in ("H", "A") else None
            note(i, "ball_out", True)
            continue
        if use_out and pending_team and i > 0 and out_dead[i - 1] and not out_dead[i]:
            last = pending_team
            pending_team = None

        def team_in_zone():
            home = np.any(dist[i, :24] <= PZ_RADIUS)
            away = np.any(dist[i, 24:] <= PZ_RADIUS)
            if home and away:
                if use_duel:
                    return "duel"
                inside = np.flatnonzero(dist[i] <= PZ_RADIUS)
                slot = inside[np.argmin(dist[i, inside])]
                return "H" if slot < 24 else "A"
            if home or away:
                return "H" if home else "A"
            return None

        def formation():
            """Each team at least 70% on one side of halfway, and the two sides opposite."""
            hx, ax_ = px[i, :24], px[i, 24:]
            hv, av = np.isfinite(hx), np.isfinite(ax_)
            if hv.sum() < 7 or av.sum() < 7:
                return False, None, None
            def side(xs, mask):
                total = int(mask.sum())
                left = int(np.sum(xs[mask] < 0.5))
                if left / total >= 0.7:
                    return "L"
                if (total - left) / total >= 0.7:
                    return "R"
                return None
            home_side, away_side = side(hx, hv), side(ax_, av)
            formed = home_side is not None and away_side is not None and home_side != away_side
            return formed, home_side, away_side

        if use_pieces and piece[i] != active_piece:
            active_piece = int(piece[i])
            released = active_piece == 0
            kickoff_open = bool(kickoff[i])
            last = None
            pending_team = None
            streak = 0
            streak_team = None
        if use_pieces and active_piece and not released:
            team = team_in_zone() if here[i] else None
            at_centre = here[i] and 0.40 <= ball_x[i] <= 0.60
            ko_ok = True
            if kickoff_open:
                for half, lo, hi in games.half_video_blocks(games.GAME):
                    if lo <= i <= hi:
                        key = "1st Half" if half == 1 else "2nd Half"
                        ko_ok = i >= KICKOFF_VIDEO_FRAMES.get(key, lo)
                        break
            if use_kickoff and kickoff_open and at_centre and kickoff_ready[i] and ko_ok:
                formed, home_side, away_side = formation()
                if formed and team in ("H", "A"):
                    released = True
                    kickoff_open = False
                    last = team
                    labels[i] = team
                    kick_at[i] = kickoff[i]
                    note(i, "kickoff", True)
                    continue
                moved = i > 0 and np.isfinite(ball_x[i - 1]) and abs(ball_x[i] - ball_x[i - 1]) > 0.002
                if formed and team is None and moved:
                    going = "R" if ball_x[i] > ball_x[i - 1] else "L"
                    if home_side == going:
                        last = "H"
                    elif away_side == going:
                        last = "A"
                    if last:
                        released = True
                        kickoff_open = False
                        labels[i] = last
                        kick_at[i] = kickoff[i]
                        note(i, "kickoff", True)
                        continue
            if team in ("H", "A"):
                streak = streak + 1 if team == streak_team else 1
                streak_team = team
            else:
                streak = 0
                streak_team = None
            wanted = piece_team[i]
            # Kick Off (Start) stays dead through warm-up (FAQ: 110808-113741 is D).
            # Only the centre-spot formation above may release it.
            if (
                not kickoff_open
                and team in ("H", "A")
                and streak >= MIN_FRAMES
                and gain[i]
                and wanted in ("?", team)
            ):
                released = True
                last = team
                labels[i] = team
                note(i, "restart_end", True)
            else:
                labels[i] = "D"
                last = None
                note(i, "restart", True)
            continue

        if not here[i] or not np.isfinite(nearest_dist[i]):
            if last:
                labels[i] = last
            streak = 0
            streak_team = None
            note(i, "no_ball")
            continue

        if not use_zone:
            last = "H" if nearest[i] < 24 else "A"
            labels[i] = last
            note(i, "nearest")
            continue

        team = team_in_zone()
        if use_missing_touch and here[i] and gain[i] and team is None:
            speeds = player_speed[i]
            home_move = int(np.sum(np.isfinite(speeds[:24]) & (speeds[:24] > 1.0)))
            away_move = int(np.sum(np.isfinite(speeds[24:]) & (speeds[24:] > 1.0)))
            if abs(home_move - away_move) >= 2:
                last = "H" if home_move < away_move else "A"
                labels[i] = last
                streak = 0
                streak_team = None
                note(i, "missing")
                continue
        if team == "duel" or team is None:
            if last:
                labels[i] = last
            streak = 0
            streak_team = None
            note(i, "duel" if team == "duel" else "open")
            continue
        streak = streak + 1 if team == streak_team else 1
        streak_team = team
        if streak >= MIN_FRAMES and gain[i]:
            last = team
            note(i, "control")
        else:
            note(i, "wait")
        labels[i] = last if last else "D"

    frame_df[name] = labels
    if use_out or use_pieces:
        frame_df["kickoff_tag"] = kickoff
        catalog_events(
            out_dead,
            out_team,
            out_kind if use_out else np.full(n, "", dtype="<U8"),
            kickoff,
            kick_at,
            labels,
            piece_windows if use_pieces else None,
        )
    if trace:
        seen = np.isfinite(bx) & np.isfinite(by)
        finite = np.isfinite(nearest_dist)
        home_n = np.sum(dist[:, :24] <= PZ_RADIUS, axis=1)
        away_n = np.sum(dist[:, 24:] <= PZ_RADIUS, axis=1)
        zone = np.full(n, "", dtype="<U4")
        zone[(home_n > 0) & (away_n > 0)] = "duel" if use_duel else "both"
        zone[(home_n > 0) & (away_n == 0)] = "H"
        zone[(away_n > 0) & (home_n == 0)] = "A"
        frame_df["in_play"] = in_play
        frame_df["seen_ball"] = seen
        frame_df["bridged_ball"] = here & ~seen
        frame_df["ball_x_used"] = ball_x
        frame_df["ball_y_used"] = ball_y
        frame_df["nearest_slot"] = np.where(finite, nearest + 1, 0)
        frame_df["nearest_team"] = np.where(finite, np.where(nearest < 24, "H", "A"), "")
        frame_df["nearest_m"] = np.where(finite, nearest_dist, np.nan)
        frame_df["home_in_zone"] = home_n
        frame_df["away_in_zone"] = away_n
        frame_df["zone_team"] = zone
        frame_df["touch_mps"] = touch_mps
        frame_df["touch_turn"] = touch_turn
        frame_df["touch_dspeed"] = touch_dspeed
        frame_df["touch"] = gain
        frame_df["set_piece"] = piece
        frame_df["set_piece_team"] = piece_team
        frame_df["dead_ball"] = dead
        frame_df["zone_streak"] = streak_n
        frame_df["zone_streak_team"] = streak_who
        frame_df["last_team"] = held
        frame_df[name + "_why"] = why
        if name == "step5_set_pieces":
            frame_df["step5_why"] = why
    return write_labels(name, labels)



def prepare_match(df: pd.DataFrame) -> pd.DataFrame:
    """Attach arrays the step functions read from module scope."""
    global frame_df, video, n, in_play, half_start, bx, by, px, py
    frame_df = df
    video = frame_df["frame_id"].to_numpy()
    n = len(video)
    in_play = ((video >= 11185) & (video <= 90651)) | ((video >= 110808) & (video <= 190197))
    half_start = (video == 11185) | (video == 110808)
    bx = frame_df["ball_x"].to_numpy(dtype=float)
    by = frame_df["ball_y"].to_numpy(dtype=float)
    px = frame_df[[f"{i}_x" for i in range(1, 47)]].to_numpy(dtype=float)
    py = frame_df[[f"{i}_y" for i in range(1, 47)]].to_numpy(dtype=float)
    return frame_df


def read_main_data() -> pd.DataFrame:
    t0 = time.perf_counter()
    dataset = metrica.load_tracking_epts(
        meta_data=paths["atd_meta"],
        raw_data=paths["atd_raw"],
    )
    elapsed = time.perf_counter() - t0
    print("%d frames in %.1f s" % (len(dataset.records), elapsed))

    t1 = time.perf_counter()
    frame_df = dataset.to_df()
    elapsed = time.perf_counter() - t1
    print("%d frames in %s s" % (frame_df.shape[0], elapsed))

    frame_df["tracking_frame_id"] = frame_df["frame_id"].astype(int)
    frame_df["frame_id"] = frame_df["tracking_frame_id"] - 1
    frame_df = frame_df.reset_index(drop=True)
    return prepare_match(frame_df)


def compute_meta_columns(frame_df: pd.DataFrame, fps: int = FPS) -> pd.DataFrame:

    # Ball speed
    ball_speed = load_ball_speed(paths["atd_raw"], games.GAMES[games.GAME]["n_video_frames"])
    print("numeric speeds: %d of %d" % (np.isfinite(ball_speed).sum(), len(ball_speed)))

    # frame_id is the video frame. ball_speed[i] is the same frame.
    frame_df["ball_speed"] = ball_speed[frame_df["frame_id"].to_numpy()]
    frame_df["ball_speed"].value_counts(dropna=False)

    dx = frame_df["ball_x"].diff() * L
    dy = frame_df["ball_y"].diff() * W
    seen = frame_df["ball_x"].notna() & frame_df["ball_y"].notna()

    frame_df["ball_speed_mps"] = np.hypot(dx, dy) * fps
    frame_df.loc[~(seen & seen.shift(fill_value=False)), "ball_speed_mps"] = np.nan

    # Ball tracked indicator
    frame_df["is_ball_tracked"] = frame_df["ball_x"].notna() & frame_df["ball_y"].notna()

    # Nearest player to ball
    Lx, Wy = 105.3, 68.0
    bx = frame_df["ball_x"].to_numpy(dtype=float)
    by = frame_df["ball_y"].to_numpy(dtype=float)
    px = frame_df[[f"{i}_x" for i in range(1, 47)]].to_numpy(dtype=float)
    py = frame_df[[f"{i}_y" for i in range(1, 47)]].to_numpy(dtype=float)
    dist = np.hypot((px - bx[:, None]) * Lx, (py - by[:, None]) * Wy)
    dist[~np.isfinite(px)] = np.inf
    nearest = dist.min(axis=1)
    nearest[~frame_df["is_ball_tracked"].to_numpy() | ~np.isfinite(nearest)] = np.nan
    frame_df["nearest_tracked_m"] = nearest
    return frame_df




STEPS = {
    1: ("order1_out", dict(use_out=True)),
    2: ("order2_events", dict(use_out=True, use_pieces=True, use_kickoff=True)),
    3: ("order3_zone", dict(use_out=True, use_pieces=True, use_kickoff=True, use_zone=True)),
    4: ("order4_duel", dict(use_out=True, use_pieces=True, use_kickoff=True, use_zone=True, use_duel=True)),
    5: ("order5_bridge", dict(use_out=True, use_pieces=True, use_kickoff=True, use_zone=True, use_duel=True, use_bridge=True)),
    6: ("order6_ghost", dict(use_out=True, use_pieces=True, use_kickoff=True, use_zone=True, use_duel=True, use_bridge=True, use_ghost=True, trace=True)),
}


def run_steps(which):
    for num in which:
        name, flags = STEPS[num]
        run_possession(name, **flags)


def save_frame_df():
    global frame_df
    gt = pd.read_csv(os.path.join(ROOT, "evaluation", "gt", "BAR-ATH_possession_gt_public.csv"))
    gt = gt.rename(columns={"frame": "frame_id", "label": "gt_label"})
    frame_df = frame_df.drop(columns=["gt_label"], errors="ignore")
    frame_df = frame_df.merge(gt, on="frame_id", how="left")
    path = os.path.join(ROOT, "frame_df.csv")
    frame_df.to_csv(path, index=False)
    print(path, frame_df.shape)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run possession steps and write frame_df.csv")
    parser.add_argument(
        "steps",
        nargs="*",
        type=int,
        help="step numbers 1-6 (default: all)",
    )
    parser.add_argument("--save-only", action="store_true", help="only merge gt and write frame_df.csv")
    args = parser.parse_args()
    which = args.steps or list(STEPS)
    os.chdir(HERE)
    if not args.save_only:
        df = read_main_data()
        compute_meta_columns(df, fps=FPS)
        run_steps(which)
    save_frame_df()

