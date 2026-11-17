# Frame-level possession for BAR-ATH

**More than a Hack 2027 - Phase 1**  
Tiki Data, FC Barcelona vs Athletic Club, tactical video, 25 fps

We are a **data science** team, Tiki Data. The work was to implement a frame-level possession algorithm and then **review it on video** with the `play_clip` script: watch a spell, find the corner case, change a generic rule, watch again. We did not label frames by hand. The strategy was to **find dead time (`D`)** first (out of play, restarts, half-time, cooling break, VAR-like huddles). Once those intervals are right, live frames are a zone-and-touch problem (`H` / `A`). Implementation was done via analysis notebooks and complementing our knowledge with **Cursor**.

This note describes that **ball-first** pipeline. The ranking metric is **spellF1** (possession intervals), not event names. We used the public 5-minute ground truth (frames 11185-18684) only as a **sanity check**, not as a training set: constants were left at literature values so a rule that fitted those 5 minutes would not damage the other 85.

---

## 1. Related work

We did not invent new possession physics. The implementation follows a small set of published ideas and the Laws of the Game.

- **Vidal-Codina et al.** - possession as a **1.5 m zone** around the ball. A team keeps the ball while a player is inside that zone; a change of team needs a **touch** (speed or direction change). Two opponents in the zone is a **duel**: we keep the last team. Dead-ball events (DBE) are a boolean on the interval, not a copy of an event log. Corners are a taker near the flag during a dead spell (Law 17). The main corner/throw-in error is mixing the **touchline** with the **goal line**. A penalty is the only restart with a unique spatial trigger (Law 14). VAR is a referee interruption, not a set piece.
- **Link et al.** - nearest player **below a distance**, and an action ends if that player cannot reach the ball in about **1 s** at the current speed. We keep vanished player tracks for **2 s** (ghosting) so a 1 s dropout does not look like a new action.
- **FIFA / IFAB Laws 8, 10, 14, 16, 17** - kick-off after a stoppage; ball in the net between the posts; penalty shape; goal kick vs corner by the posts; throw-in vs corner by which line the ball crossed.
- **Metrica EPTS / FIFA tracking standard** - the ATD files shipped with the challenge (`*_atd.xml` / `*_atd.txt`) are that export. We read them with **kloppy** (`metrica.load_tracking_epts`) so positions, missing ball and slot indices stay on the **tactical video clock** (row *i* of the submission is frame *i*). Smart Tagging is the same SportsCode XML: useful as a **search window** around kick-offs, never as frame labels.

The 1.5 m radius, **5 m/s** speed jump, **10 degree** turn and **3-frame** control streak are those papers, not numbers fit on the 5-minute slice.

---

## 2. Design: start from the ball

Live play always has a team. Dead play is `D` (out, missing long enough to be a restart, or a match stoppage). `X` is not used on graded frames.

**Inter-frame ball motion.** From consecutive samples we get speed (m/s), turning angle and speed change. A **touch** is: speed below 5 m/s, speed change above 5 m/s, or a turn above 10 degrees. Possession can change only if someone is in the 1.5 m zone **and** there is a touch, for **3 frames**.

**Out of play.** The ball is dead when it is tracked outside the pitch or missing for >= 1 s after leaving a line. Touchline -> throw-in; goal line outside the posts -> corner; between the posts -> goal kick. A keeper catch (last on-pitch sample inside 1.5 m, then the ball vanishes) is a **save**, not a restart.

**Ghosts.** Player slots drop out for 1-2 s in crowds. We hold the last position for 2 s: if another body is within 2 m we follow that track (a crossing); otherwise we extrapolate velocity and stop at the pitch edge. Separately, **ball bridge** fills gaps of at most 1 s with the last velocity while the estimate stays near the pitch.

**Set pieces stay dead until the restart is taken** (Laws 16-17): not until the tracker finds someone at 1.5 m. A short corner can reappear already at ~8 m/s at the flag; we treat that as the kick and release when the pass slows below 5 m/s. Walking to the flag is still `D`.

---

## 3. Pipeline (six stacked steps)

Each step writes a submission CSV. We kept the stack so a regression on the 5 minutes is visible.

| Step | Name | What it adds | Why |
|---|---|---|---|
| 1 | `order1_out` | Geometric out of play -> `D` | Without this, live nearest-player labels run through throw-ins and corners. |
| 2 | `order2_events` | Kick-off XML **windows** (start / after goal) plus hold until the ball is kicked at the centre | Kick-off is a stoppage restart. The XML is ~5 s around the kick, not the whole warm-up. |
| 3 | `order3_zone` | 1.5 m zone + touch + 3-frame streak | Replaces "nearest player" on live frames (the baseline). |
| 4 | `order4_duel` | Two teams in the zone -> keep last team | Avoids flickering H/A in challenges. |
| 5 | `order5_bridge` | 1 s ball interpolation | Short occlusions should not start a new spell. |
| 6 | `order6_ghost` | 2 s player ghosts + diagnostics | **Deliverable.** Same labels as 5, more stable nearest/zone when a slot blinks. |

Same drawing as the notebook visualizer (`starter/utils.py`): grass pitch, **red = home**, **blue = away**, **white = ball**. The yellow dashed ring is the 1.5 m zone drawn larger so it is readable on a full pitch.

### Live frames (`H` / `A`) — steps 3-6

![Live possession cases](../figures/live.png)

- **Zone + touch, 3 frames:** a player inside 1.5 m and a touch (speed / turn) can take the label. Nearest player alone is not enough.
- **Duel:** both teams in the zone -> keep the last team (no H/A flicker).
- **Ghost 2 s:** a slot that blinks is held; a 1 s dropout is not a new action.
- **Ball bridge <= 1 s:** fill a short missing-ball hole with the last velocity while the estimate stays near the pitch.

### Restarts (`D` until the kick) — steps 1-2 plus hold

![Restart cases](../figures/restarts.png)

- **Throw-in / corner / goal kick** from geometry: which line, and whether the ball is outside the posts. Stay `D` until the restart is taken, not until someone is 1.5 m from the ball. Walking to the flag is still `D`. Holes < 5 s without a >= 5 m/s kick stay `D` (placing the ball).
- **Short corner:** the ball can reappear already at ~8 m/s at the flag. That is the kick; we release when the pass slows below 5 m/s.
- **Kick-off:** XML window (~5 s) plus hold until the ball is struck at the centre. The XML is not the warm-up.
- **Penalty:** Law 14 shape on the pitch (the only restart with a unique spatial trigger).

### Other stoppages (`D`)

![Stoppage cases](../figures/stoppages.png)

- **Keeper catch:** last on-pitch sample inside 1.5 m, then the ball vanishes -> **save**, live possession, not a restart.
- **Freeze >= 5 s**, ball quiet -> `D` (large foul / offside). A 3 s freeze split true possessions in the 5-minute file.
- **VAR huddle:** ball missing >= 20 s, players packed in midfield.
- **Match calendar** (cooling, half-time warm-up, after the final whistle): not a ball-out DBE. The ATD has no whistle; jogging looks live. We take half spans from `games.half_video_blocks` (tracking IdFrame blocks). Outside those spans we write `D` and skip live possession. On BAR-ATH the second graded block starts at **110808** but kick-off is **113742** (2934 frames, all `D`, FAQ). Cooling is a long `D` until the tagged restart (~163083). After **190198** the tactical file continues and is ungraded. Another EPTS match needs its own half blocks; the possession rules stay the same.

Smart Tagging is used **only** for kick-off (start / after goal) windows and the published kick-off frames. Throw-in, corner, free-kick and goal-kick tags are 35 s search spans. Copying them as `D` **hurts** spellF1 (we tried). Official corner counts (e.g. 11) were **not** a fitting target.

---

## 4. Calibration on 5 minutes, rules for 90

The public GT is 7500 frames, 17 possessions. We used it to **reject** rules, not to tune 1.5 m / 5 m/s / 10 degrees / 3 frames.

Examples of checks against the clip and the rest of the match:

- Goal kick ~4:32 (frames 18025-18388): `D` until the throw, then `H` - matches GT. Reclassifying the keeper walking behind the goal as a corner was a **type** error, not a label error.
- Short corner ~4:24 (GT `A` at 17838): the ball was not missing; it reappeared at the flag already moving. Releasing on that kick (not on 1.5 m of the taker) put `A` from **17843**. Five frames late, inside the +/-25-frame transition window. A 15 s look-ahead to a later corner had previously glued a wing dropout at 17047 onto that corner - **reverted**.
- Freeze at 3 s would split true possessions in the 5 minutes; we kept **5 s**.
- Treating open-field missing ball as offside produced tens of thousands of `D` frames and collapsed spellF1 (~0.47 to ~0.23) - **reverted**.
- Live H vs A disagreements (e.g. 17293, a recovery that the GT calls `H` while we still hold `A` through a duel) were left: they need a different possession model, not a one-clip exception.

Public score of the deliverable (`order6_ghost`): **spellF1 ~ 0.52**, macro-F1 ~ 0.83, accuracy ~ 0.83. The ranking metric did not move when we fixed half-time warm-up (that slice is not in the 5 minutes). The private file grades the full 158857 in-play frames; that is where the 2934 warm-up `D` frames and the cooling-break `D` matter.

The point of the challenge, for us: **one 5-minute label file to constrain a 90-minute rule set**, not to overfit 17 possessions.

---

## 5. Data we used (FIFA-standard files)

Only the **tactical** package:

- `BAR-ATH_tactical_atd.xml` + `.txt` - EPTS tracking (kloppy).
- `BAR-ATH_tactical_smart_tagging.xml` - kick-off windows and time base (kick-offs sit on 11185 and 113742).
- Tactical video - local `play_clip` review, not an input to the scorer.
- Homography / broadcast were **not** required for this submission.

We stayed on that standard so the same code could run on another EPTS match **if that match has half spans in the same table**. Next steps would be more restarts (whistle is not in the file), fewer false freeze/VAR spells, and **several matches** instead of one. The interesting constraint was the opposite: one public slice.

---

## 6. Notebooks (how we worked)

All under `starter/`. The algorithm itself lives in `tiki_data_algorithm.py`; the notebooks are the audit trail.

| Notebook | Role |
|---|---|
| `01_smart_tagging.ipynb` | XML codes, set-piece types, time base vs real kick-offs. |
| `02_tracking_atd.ipynb` | EPTS load, pitch, missing ball, slot layout (home 0-23, away 24-45). |
| `03_first_submission.ipynb` | Baseline nearest-player CSV and evaluation entry point. |
| `04_eda.ipynb` | EDA only (archived solution notebook not used for the deliverable). |
| `05_algoritmo.ipynb` | Runs steps 1-6 and writes `evaluation/results/` + `frame_df.csv`. |
| `06_revision.ipynb` | `look(a, b)` vs public GT, event catalogue, `play_clip.py` for corners, throw-ins, cooling, half-time. |

`frame_id` in the tables is the **video** index (deliverable `frame`). Tracking's `frame_id` is stored as `tracking_frame_id` (= video + 1).

---

## 7. Deliverable

- **File:** `evaluation/results/order6_ghost.csv`
- **Format:** `frame,label` - 199319 rows - alphabet `H`/`A`/`D`
- **Code:** `starter/tiki_data_algorithm.py` (step 6)

We do not submit event types (corner, throw-in, etc.). They were a debug layer. The scorer only sees possession.
