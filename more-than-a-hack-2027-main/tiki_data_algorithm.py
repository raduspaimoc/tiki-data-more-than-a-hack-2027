from starter.baseline import write_submission
from starter.load_smart_tagging import load as load_events
import os, sys
sys.path.insert(0, os.path.abspath(''))

import numpy as np
import matplotlib.pyplot as plt
from kloppy import metrica
import games
import time
import pandas as pd

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


def ball_out(ball_x, ball_y, players_x, players_y):
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

    def edge_kind(x, y):
        if not np.isfinite(x) or not np.isfinite(y):
            return None, False
        goal = min(x, 1 - x) * L
        side = min(y, 1 - y) * W
        left = bool(x <= 0.5)
        if goal <= OUT_CORNER_M and side <= OUT_CORNER_M:
            return "corner", left
        if goal <= OUT_EDGE_M:
            return "goal", left
        if side <= OUT_EDGE_M:
            return "side", left
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

    goals = sides = corners = 0
    i = 0
    while i < n:
        if here[i]:
            i += 1
            continue
        j = i
        while j < n and not here[j]:
            j += 1
        span = j - i
        i_next = j
        if span < OUT_MIN_FRAMES or i == 0 or not in_play[i:j].any():
            i = i_next
            continue
        left_kind = edge_kind(ball_x[i - 1], ball_y[i - 1]) if here[i - 1] else (None, False)
        back_kind = edge_kind(ball_x[j], ball_y[j]) if j < n and here[j] else (None, False)
        which = left_kind[0] or back_kind[0]
        if which is None:
            i = i_next
            continue
        left = left_kind[1] if left_kind[0] else back_kind[1]
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
    path = os.path.join("..", "evaluation", "results", name + ".csv")
    write_submission(path, labels)
    counts = {label: int((labels == label).sum()) for label in "HAD"}
    print(name, counts, "possessions", spell_count(labels))
    return labels



def catalog_events(out_dead, out_team, out_kind, kickoff, kick_at, labels):
    """One row of event columns per frame.

    A restart runs from the frame it is detected until the ball is in motion.
    event_ball is out_of_play while the ball is gone or outside, and in_play
    once it is back on the pitch, up to and including the frame it reaches 5 m/s.
    """
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
    names = {"side": "throw_in", "goal": "goal_kick", "corner": "corner"}
    spells = []

    def edge_at(frame, kind):
        if frame < 0 or not tracked[frame]:
            return "", np.nan
        goal_m = min(bx[frame], 1 - bx[frame]) * L
        side_m = min(by[frame], 1 - by[frame]) * W
        if kind == "throw_in":
            return ("near" if by[frame] <= 0.5 else "far"), float(side_m)
        return ("left" if bx[frame] <= 0.5 else "right"), float(goal_m)

    def live_end(stop):
        """Last frame of the restart: the first one at 5 m/s, at most 4 s later."""
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

    i = 0
    while i < n:
        kind = out_kind[i]
        if not out_dead[i] or kind not in names:
            i += 1
            continue
        j = i + 1
        while j < n and out_dead[j] and out_kind[j] == kind:
            j += 1
        ref = i - 1
        side, edge = edge_at(ref, names[kind])
        spells.append({
            "start": i,
            "dead_end": j,
            "end": live_end(j),
            "type": names[kind],
            "team": out_team[i] if out_team[i] in ("H", "A") else "",
            "side": side,
            "edge": edge,
        })
        i = j

    kick_names = {
        "Kick Off (Start)": "kick_off_start",
        "Kick Off (After Goal)": "kick_off_after_goal",
    }
    for frame in np.flatnonzero(kick_at.astype(str) != ""):
        raw = str(kick_at[frame])
        if raw not in kick_names:
            continue
        lo = int(frame)
        while lo > 0 and kickoff[lo - 1] == kickoff[frame]:
            lo -= 1
        start = int(frame)
        while start > lo:
            prev = start - 1
            at_centre = tracked[prev] and 0.40 <= bx[prev] <= 0.60
            if (not tracked[prev]) or at_centre:
                start -= 1
            else:
                break
        team = labels[frame] if labels[frame] in ("H", "A") else ""
        spells.append({
            "start": start,
            "dead_end": int(frame),
            "end": int(frame),
            "type": kick_names[raw],
            "team": team,
            "side": "",
            "edge": np.nan,
        })

    spells.sort(key=lambda spell: (spell["start"], spell["end"]))
    seen = {name: 0 for name in ("throw_in", "corner", "goal_kick", "kick_off_start", "kick_off_after_goal")}
    kept = 0
    for spell in spells:
        free = [frame for frame in range(spell["start"], spell["end"] + 1) if event_id[frame] == 0]
        if not free:
            continue
        seen[spell["type"]] += 1
        kept += 1
        number = seen[spell["type"]]
        for frame in free:
            event_id[frame] = kept
            event_number[frame] = number
            event_type[frame] = spell["type"]
            event_team[frame] = spell["team"]
            event_side[frame] = spell["side"]
            event_edge_m[frame] = spell["edge"]
            event_ball[frame] = "out_of_play" if frame < spell["dead_end"] else "in_play"
    frame_df["event_id"] = event_id
    frame_df["event_number"] = event_number
    frame_df["event_type"] = event_type
    frame_df["event_team"] = event_team
    frame_df["event_side"] = event_side
    frame_df["event_ball"] = event_ball
    frame_df["event_edge_m"] = event_edge_m
    print("events", {name: count for name, count in seen.items() if count})


def run_possession(name, use_zone=False, use_duel=False, use_bridge=False, use_pieces=False, trace=False, use_kickoff=False, use_missing_touch=False, use_ghost=False, use_out=False):
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
    if use_out:
        out_dead, out_team, out_kind, out_counts = ball_out(bx, by, px, py)
        print("ball out frames", int(out_dead.sum()), "goal", out_counts[0], "throw-in", out_counts[1], "corner", out_counts[2])
        frame_df["out_kind"] = out_kind
    gain, touch_mps, touch_turn, touch_dspeed = gain_parts(ball_x, ball_y)
    nearest = np.argmin(dist, axis=1)
    nearest_dist = dist[np.arange(n), nearest]

    piece = np.zeros(n, dtype=np.int32)
    piece_team = np.full(n, "", dtype="<U1")
    kickoff = np.full(n, "", dtype=object)
    event_type = np.full(n, "", dtype=object)
    if use_pieces:
        piece_id = 1
        for event in load_events():
            if event["code"] != "SET PIECES":
                continue
            start = max(0, event["start_frame"])
            end = min(n, event["end_frame"])
            team = {"Barcelona": "H", "Atletic Club": "A"}.get(event["tags"].get("Team"), "?")
            piece[start:end] = piece_id
            piece_team[start:end] = team
            kind = (event["tags"].get("Type") or "").strip()
            if kind.lower() in ("kick off (start)", "kick off (after goal)"):
                kickoff[start:end] = kind
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
        # Priority on this frame: a ball out of play is D, and nothing below can change it.
        if use_out and out_dead[i]:
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
            streak = 0
            streak_team = None
        if use_pieces and active_piece and not released:
            team = team_in_zone() if here[i] else None
            at_centre = here[i] and 0.40 <= ball_x[i] <= 0.60
            if use_kickoff and kickoff_open and at_centre:
                formed, home_side, away_side = formation()
                if formed and team in ("H", "A"):
                    released = True
                    kickoff_open = False
                    last = team
                    labels[i] = team
                    event_type[i] = kickoff[i]
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
                        event_type[i] = kickoff[i]
                        note(i, "kickoff", True)
                        continue
            if team in ("H", "A"):
                streak = streak + 1 if team == streak_team else 1
                streak_team = team
            else:
                streak = 0
                streak_team = None
            wanted = piece_team[i]
            if team in ("H", "A") and streak >= MIN_FRAMES and gain[i] and wanted in ("?", team):
                released = True
                last = team
                labels[i] = team
                note(i, "restart_end", True)
            else:
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
    if use_out or use_kickoff:
        catalog_events(out_dead, out_team, out_kind if use_out else np.full(n, "", dtype="<U8"), kickoff, event_type, labels)
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

    video = frame_df["frame_id"].to_numpy()
    n = len(video)
    in_play = ((video >= 11185) & (video <= 90651)) | ((video >= 110808) & (video <= 190197))
    half_start = (video == 11185) | (video == 110808)

    bx = frame_df["ball_x"].to_numpy(dtype=float)
    by = frame_df["ball_y"].to_numpy(dtype=float)
    px = frame_df[[f"{i}_x" for i in range(1, 47)]].to_numpy(dtype=float)
    py = frame_df[[f"{i}_y" for i in range(1, 47)]].to_numpy(dtype=float)

    frame_df["tracking_frame_id"] = frame_df["frame_id"].astype(int)
    frame_df["frame_id"] = frame_df["tracking_frame_id"] - 1
    frame_df = frame_df.reset_index(drop=True)
    return frame_df


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




if __name__ == '__main__':

    frame_df = read_main_data()
    frame_df = compute_meta_columns(frame_df, fps=FPS)

    labels_order1 = run_possession("order1_out", use_out=True)
    labels_order2 = run_possession("order2_events", use_out=True, use_pieces=True, use_kickoff=True)
    labels_order3 = run_possession("order3_zone", use_out=True, use_pieces=True, use_kickoff=True, use_zone=True,)
    labels_order4 = run_possession("order4_duel", use_out=True, use_pieces=True, use_kickoff=True, use_zone=True, use_duel=True)
    labels_order5 = run_possession("order5_bridge",use_out=True, use_pieces=True, use_kickoff=True, use_zone=True, use_duel=True, use_bridge=True,)
    labels_order6 = run_possession("order6_ghost", use_out=True, use_pieces=True, use_kickoff=True, use_zone=True, use_duel=True, use_bridge=True, use_ghost=True)

    # Ref publica
    gt = pd.read_csv(os.path.join("..", "evaluation", "gt", "BAR-ATH_possession_gt_public.csv"))
    gt = gt.rename(columns={"frame": "frame_id", "label": "gt_label"})
    frame_df = frame_df.drop(columns=["gt_label"], errors="ignore")
    frame_df = frame_df.merge(gt, on="frame_id", how="left")
    # Tracking positions plus the columns this notebook adds, including event_id and event_type.
    # Working copy for another notebook. Not a submission: evaluation/results is scored as one.
    path = os.path.join("..", "frame_df.csv")
    frame_df.to_csv(path, index=False)
    print(path, frame_df.shape)
