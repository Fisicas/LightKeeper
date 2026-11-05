# LightKeeper manual

Time-resolved spectroscopy logger for Ocean Insight spectrometers driven through the OceanDirect SDK. It is a sibling of
[GammaKeeper](https://github.com/Fisicas/GammaKeeper): same look, same rules, with the spectra in front.

LightKeeper records raw spectra on a fixed time grid, with scheduled fast bursts. Every run is bracketed by dark sets
over the whole integration-time ladder it can use. Wavelength calibration from emission-line lamps against bundled NIST
ASD line lists is built in.


## Start
On opening, LightKeeper connects the first attached Ocean spectrometer and starts the live view at a fixed 100 ms (one
scan per update), as OceanView does. Spectrometers plugged in later are picked up within about 5 s. Press
**Disconnect** to release it (it is not reopened until the next start); `LIGHTKEEPER_AUTOCONNECT=0` turns this off.

- **Launch:** double-click `Launch_LightKeeper.cmd`. It uses the first `pythonw.exe` on PATH;
  set `LIGHTKEEPER_PYTHON` to override.
- **Close OceanView first.** Only one program can hold a USB spectrometer.
- **Requirements:** PySide6, numpy, scipy, matplotlib and h5py (all in the shared environment), plus the OceanDirect SDK
  at `C:\Program Files\Ocean Optics\OceanDirect SDK`, or wherever `OCEANDIRECT_HOME` points.

## Window
| Area | What it shows |
|---|---|
| Header | Run name (hover for the full folder path); the state indicator; the clock; **Hide setup / Show setup** (Ctrl+B); **More** (open run folder, resume an unfinished run, open log folder, about); **Full screen** (F11). |
| Splash | While the window loads, a row for each step: a spinning ring while it runs, then a check mark and its time. It closes once the main window is open. `--no-splash`, or `LIGHTKEEPER_SPLASH=0`, starts without it. |
| Metric strip | Slots recorded, integration time, scans per slot, peak counts, saturated slots, bursts, dark sets and data written. On the right: the next event (start countdown, next burst, stop) and the background recorder it is attached to. |
| Setup panel | Spectrometer; Live view (with **Take dark set…**); Run (label, geometry, notes, folder); Schedule; Acquisition (time slot, integration range, bursts, dark sets). **Show advanced settings** opens the ladder, dark time per integration time, SD storage, site and restart resume. |
| Time range (Waterfall, Time series) | **Usable daylight (auto)** draws only where the 420–700 nm dark-corrected signal stays above 1 % of the day's bright level for 5 min; **All data** or **Custom** from/to; **Reset** returns to auto. The range drawn is quoted next to it. **Open recorded run…** shows a finished run read-only, dark-corrected with its pre-run dark set. |
| Spectrum tab | **Save spectrum** (Ctrl+S) writes the scan on screen as a two-column `.dpt` (wavelength nm, signal; `#` header lines with the main details, which OPUS skips) plus a `.json` with everything, in `<Save in>\..\spectra\`, named `<label>_<date>_<time>` or just the time. **Live scan:** counts or counts/s, the matched dark and the saturation line. **Wavelength strip:** each wavelength in its colour, fading into the UV and the NIR. **Recent slots:** the last 24, newest brightest. The toolbar has navigation, Auto-scale, **Fit y** and **Display** (dark subtraction, counts/s, log y, hide hot/cold pixels, calibrated axis). |
| Waterfall | Recorded spectra over time: spectral shape normalised at 780–820 nm, log₁₀ counts/s, ratio to the first slot, or **Raw counts, as recorded** (mean counts per scan as stored: no dark subtraction, no scaling to a wavelength or to integration time, so a change of integration time shows as a step). **Cursor** reads the wavelength, time and value at a click. |
| Time series | Band-integrated signal, integration time and saturation over the run. |
| Schedule tab | The plan as a table, plus the sun times, beside the solar-elevation plot. |
| Calibration tab | Lamp wavelength calibration (see below). |
| Log tab | The run's event journal. |

The setup panel only shows the fields that apply:
- **Start:** "Now" hides both start fields; "At a set time" shows the time; "At a sun event" shows the event and offset.
- **Stop:** the same.

Whether the panel is shown is remembered between sessions. It steps aside while the window is attached to a running
recorder, and comes back when the run closes.

**Wheel guard.** Spin boxes and drop-downs react to the mouse wheel only after you click into them, so scrolling the
setup panel can never change a value.

### State indicator
| Indicator | Meaning |
|---|---|
| grey · No spectrometer / Connected / Waiting for the spectrometer | Nothing is running. |
| green · Live view, not recording | Live view; nothing is stored. |
| violet · Dark set, keep the input covered | A dark set is in progress. |
| amber · Uncover the input, then Continue | Pre-run darks are done. |
| blue · Armed | Waiting for the start time; live view only. |
| red · Recording | One row per time slot. |
| orange · Recording a fast burst | Every scan is stored. |
| amber · Run finished, post-run darks needed | Cover the input and record the post-run darks. |

### Look (theme.py)
- **Colour:** a dark graphite ground, kind to night vision at dawn and dusk. One accent, lamp amber, marks primary
  actions and the live trace. State colours appear only in the indicator.
- **Type and controls:** Segoe UI at one type scale, sentence-case labels, and Qt's Fusion style with a matching palette,
  so all controls share one compact size.
- **Plots:** matplotlib is styled from the same tokens, with comma thousands on counts axes.

## Run sequence
1. Connect, start the live view, aim the fiber, and check the exposure.
2. Fill in the run plan and check the Schedule tab.
3. Press **Start run…** and confirm the summary.
4. Cover the input. The **pre-run dark set** records every integration time the run can use: the fixed time (or the
   automatic ladder), the burst time and the **Long dark (fixed)** time (default 3000 ms). The Start dialog lists them
   with the pre-flight checks.
5. Uncover the input when asked, without moving the fiber, and press Continue.
   - **Start mode "Now":** recording starts at once.
   - **Start mode "At a set time":** the banner shows STANDBY until the start time.
6. **Recording.** Each slot (default 1 s) stores the per-pixel mean and SD of the n raw scans in that slot.
   - **Exposure:** fixed at the **Exposure** time (default 500 ms). With **Automatic**, the integration time steps along
     the ladder within the **Auto range**, never below 8 ms on a Maya2000 Pro.
   - **Frames:** at 8 or more frames per slot, frames are read without any idle time. A frame belongs to the slot its return
     time falls in, so a slot holds 99 or 100 frames at 10 ms, not exactly 100.
   - **Bursts:** at the scheduled times (default 60 s every 15 min, at minute 7), every scan is stored at the burst's own
     time, never longer than the slot time in use.
     - **At the slot time** (for example a 50 ms burst setting with 10 ms slots): the slots go on through the burst with no
       gap and no exposure command. Those slots summarize frames that are also in `burst`, so do not count them twice.
       Automatic exposure and back-off wait until the burst ends.
     - **At another time:** the slots pause for the burst, and two frames are discarded at each change of time.
     - **Burst minimum:** a burst whose predicted raw p99.5 is below this is skipped and logged. Raw counts sit below zero at
       night (about -100 at 10 ms), so the default of 0 is about 100 counts above dark.
   - **Save every frame:** keeps every individual spectrum of the whole run in `burst` (`burst_id` 0), with the slots as well.
     No scheduled bursts are needed. At 10 ms this is about 21 GB for a 12.6 h day.
   - **Temperature:** logged with every slot (`blocks/temperature_c`) when the spectrometer has a sensor the SDK can
     read (a cooled detector's thermistor). The Maya2000 Pro has none.
7. At the stop time, or when you press Stop: cover the input. The **post-run dark set** covers the whole ladder again.
   Then the files are closed.

**Why the dark sets are run-level:** darks are never taken during a run, because the input cannot be covered in the field.
The pre- and post-run sets cover every integration time the run may use. Dark drift during the day is interpolated in
analysis between the two sets, using the UV floor (205–290 nm) as a tracer.

## Files (`<Save to>\<date>_<label>_vNN\`, a new folder every time)
- **`run_metadata.json`:**
  - the plan;
  - device info (model, serial, SDK and actual minimum integration, electric-dark and nonlinearity flags, the SDK features
    the device reports, its nonlinearity coefficients and its temperature source);
  - the recording ladder, the dark times and the burst time;
  - the Windows time-sync status;
  - the auto-exposure rules;
  - final row counts.
- **`run_summary.json`** (written when the run closes): slots and saturated slots, each integration change with its time,
  bursts with their integration and realised scan rate, dark times and pre/post dark agreement, the usable-light window
  with the sun elevation at each end, the clock-offset range, the temperature range and the version.
- **`events.csv`:** a journal of every event, with UTC nanoseconds and local time.
- **`spectra_YYYYMMDD_HHMM.h5`:** files rotate hourly. Each holds `wavelength_nm_device` and three groups:
  - **`blocks`**: one row per slot. Fields: `t_wall_ns`, `t_start_wall_ns`, `t_end_wall_ns`, `slot_index`, `segment` (0.2.0), `int_us`,
    `n_scans`, `mean`, `sd`, `max_raw_window`, `p995_mean_window`, `n_saturated_scans`, `overrun_ms`. Take time from `t_wall_ns`,
    never from `slot_index`: the slot grid runs on the PC's performance counter, which drifted about 20 ppm from the clock on
    one PC. From 0.3.13, `overrun_ms` is how much later than expected the next frame came.
  - **`dark_scans`**: every dark scan, with `int_us`, `session` and `label` (`pre` or `post`).
  - **`burst`**: every burst scan, with `int_us` and `burst_id`.
- **Storage (new files from 0.3.13):** lossless, built-in HDF5 gzip, so any HDF5 reader opens them with no extra package.
  `blocks` arrays use 30-row chunks with shuffle. `burst` and `dark_scans` frames use chunks of 400 rows by 16 pixels without
  shuffle, because gzip then finds a pixel's repeating values. Reading one pixel through time is also much faster; reading
  one frame takes about 13 ms. The same 4 Nov 2026 run took 1.06 GB instead of 1.50 GB, with every value identical. Spectra
  are stored as float32, as they always have been.
- **`weather_vNN.json`** (optional): see below.
- **Raw data only.** No dark subtraction, smoothing or resampling is applied in the files. Electric-dark and nonlinearity
  corrections are the device's own and are recorded as flags.
- **Times:** all stored times are UTC. Daylight-saving changes affect only folder and file names; the repeated hour in
  autumn gets a `b` suffix (`spectra_20261101_0100b.h5`). The local time zone is `LIGHTKEEPER_TZ` (default
  `America/New_York`).
- **Setup and clock:** `run_metadata.json` and every HDF5 file carry the setup fields (fiber, diffuser, view
  azimuth/elevation, window, site altitude), `format_version` (2; files without it are format 1) and the SNTP clock
  checks (`clock_offset_s`, `clock_rtt_s`, `clock_checks`). An optional setup photo is copied to `setup_photo.<ext>`.
- **Hot and cold pixels:** after each dark set, LightKeeper looks at the individual scans at the longest integration time.
  A pixel is listed when it sits more than 5 robust sigma above (hot) or below (cold) a 15-pixel running median in at
  least 80 % of those scans. Noise rarely repeats from scan to scan, so the list holds pixels that belong to the detector.
  - The run keeps the union over its dark sets: `hot_pixels` in `run_metadata.json` (with each set's own list and the
    method) and `hot_pixels` / `cold_pixels` attributes (pixel indices) on every HDF5 file.
  - The latest list for each spectrometer is kept in `%LOCALAPPDATA%\LightKeeper\detector\<serial>.json`, so the live
    view hides those pixels outside a run as well.
  - The list is metadata only: raw spectra are never altered. **Display → Hide hot and cold pixels** (on by default)
    bridges them in the live scan, the recent slots and the waterfall.
  - **Take dark set…** (Live view) finds them without a run: cover the input, and LightKeeper takes 5 scans at the
    longest integration time of the Acquisition range (and 5 at the live view's current time). The list is saved for
    the spectrometer and the live view gets a matched dark. Nothing is written to a run folder, and a run always
    starts from its own dark sets.
  - The live-view readout says how many pixels are hidden, or "no hot-pixel list" when none is saved yet.
- **Window log:** the Log tab is also written to `%LOCALAPPDATA%\LightKeeper\logs\window_<date>.log`.
- **Writing:** a separate file thread writes the data, the journal and the run state, so a slow disk does not cost frames.
  If the drive stalls or disappears, rows wait in memory and writing keeps trying. At 512 MB behind, acquisition pauses and logs
  `writer_backlog`; rows are never dropped.
- **A sleeping drive:** the frame loop does not wait for the drive. After 2 minutes without a write, the file thread wakes the
  drive before it opens an HDF5 file, and logs `the data drive took N s to wake` when that takes a second or more. During
  recording, writes come every 30 s, so this does not run and the drive does not sleep. Before recording, darks and other steps
  outside the frame loop can wait for it, and the window can pause briefly when it first looks at **Save in**.
- **Crash safety:** a file is opened only while it is written. A crash loses what has not been stored: about 30 s of slots,
  and at most about 8 s of burst frames, because frames wait in memory to complete a chunk, even between bursts. Stop and
  Interrupt store everything.
- **Weather:** **More → Fetch weather for a run…**, or the tick box under **Show advanced settings** for every run that
  closes, or `LightKeeper.py --fetch-weather <run folder> ...`. It writes `weather_vNN.json` beside the run: airport
  observations (via the Iowa Environmental Mesonet) and model cloud, radiation and aerosol (Open-Meteo, CC BY 4.0, free for
  non-commercial use). It runs after recording and never touches the engine, the recorder or the HDF5 files. It sends the site,
  rounded to two decimals, and the dates. Use it to label a day's conditions: an airport and a model grid do not see this
  sensor's sky, so it cannot explain single events.
- **Sleep:** Windows sleep is blocked while a run is active.

## Calibration (`<Save to>\..\calibrations\<date>_<lamp>_vNN\`)
1. Choose the lamp. The bundled lamps are:
   - **Discharge and calibration lamps:** UVC germicidal (Hg + Ar fill), Hg-Ar, Hg, Ne, Ar, Ne+Ar, He, H (Balmer), Kr,
     Xe, Cd, Zn.
   - **Na:** check only. D1/D2 blend at this resolution, and Na has too few isolated lines to calibrate on its own.
2. **1. Acquire dark, lamp off:** darks at every integration time of the ladder plus the calibration time (the
   **Dark time each** setting per time, at least 5 scans). The longest one also finds the hot and cold pixels. Then
   **2. Acquire lamp**, with no saturation. The lamp is matched to the dark at its own integration time, so you can
   change the calibration time to any ladder time without new darks. The next step is highlighted in yellow
   throughout: 1, then 2, then 3, then Save.
3. Fit. The procedure:
   - **Hot and cold pixels:** bridged from their neighbours in the dark-subtracted lamp spectrum, so they cannot be
     taken for lines. The saved `calibration.json` lists them.
   - **Peaks:** found by prominence ≥ S/N × noise. Saturated peaks are rejected. Peak positions use a 3-point parabola.
   - **Reference lines:** NIST lines above the minimum relative intensity and isolated, with no other candidate line
     within 1 nm.
   - **Matching:** within 1.5, then 0.8, then 0.5 nm.
   - **Fit:** cubic in pixel, with 3σ clipping. The leave-one-out rms is reported.
   **Fit y**, the mouse wheel and a double-click scale the calibration plots as on the Spectrum tab.
4. **Save** writes `calibration.json`, `matched_lines.csv` and `lamp_spectrum.csv`. **Use for display axis** relabels the
   plots.

The device EEPROM is never changed. Recorded data always keep the device axis plus pixel index, and the calibration is
applied in analysis. OceanView-format coefficients are shown if you want to enter them there.

**NIST reference data:** `lightkeeper/data/nist_asd/*.tsv`, unmodified NIST ASD output retrieved 2026-10-31 (see
`PROVENANCE.json`). Observed air wavelengths are used where listed, otherwise Ritz. NIST relative intensities depend on
the source and are not comparable between species; they are used only to select candidate lines.

## Testing and validation
- **Simulator tests** (`python -X utf8 -m unittest discover -s tests -v`). They cover:
  - a full run;
  - resume after a kill and after an interruption;
  - burst skipping;
  - the reconnect race;
  - calibration recovery of a deliberate axis error to < 0.01 nm;
  - the plan estimate;
  - the SNTP clock check, with a mocked socket;
  - the 2026 daylight-saving change;
  - hot-pixel detection from the dark sets, and the saved list on the next connection.
- **GUI checks**, run off-screen with `QT_QPA_PLATFORM=offscreen`:
  - `tests/gui_recorder_smoke.py`: window close and re-attach, y-scaling and recorder kill.
  - `tests/gui_schedule_running.py`: the Schedule tab during a run.
  - `tests/gui_save_spectrum.py`: Save spectrum file names, the two-column `.dpt` and its `.json`.
  - `tests/gui_preflight.py`: the run's own exposure setting and the pre-flight checklist.
  - `tests/gui_watchdog.py`: the window and the watchdog sharing the spectrometer.
  - `tests/gui_time_range.py [run folder]`: the time range and opening a recorded run read-only (a synthetic day by
    default; give a real run folder to open that one, which is only read).
- **Hardware:** dawn-to-dusk field runs on a Maya2000 Pro. The scan period equals the integration time from 8 to 500 ms,
  with no dead time.
- **Tested only with the simulator so far:**
  - handing the device from the window to the recorder after a crash;
  - reopening by serial after a USB fault;
  - resuming after a Windows restart.
- **Known limits:**
  - Scan timestamps are host times at the end of each exposure.
  - The simulator is not a hardware validation.
  - Only spectrometers that the OceanDirect SDK can open over USB are supported. Network devices and spectrometers from
    other makers are not.

## Starting from the previous run

**What is copied:** when the window opens, and when a run closes, the setup panel is filled from the newest run in **Save in**: label, geometry, notes, diffuser, fiber, view, site, schedule choices, acquisition settings and switches. The panel says so, and its tooltip names the run. Everything can still be edited.

**What changes by itself:**
- **Dates and times:** sun events are worked out for the next day. A set time keeps its clock time and moves to its next occurrence.
- **Label:** one ending in `day N` counts up, so `canopy day 2` becomes `canopy day 3`. Any other label stays, and the run folder gets its own date and version.
- **Setup photo:** dropped if the file no longer exists.

**Exception:** a run recorded before 0.3.12 with the old default times (astronomical, ±20 min) does not pass them on, so the next run uses the nautical default.

**With no previous run:** the defaults below stand.

## Sun-event scheduling

**Setting it up:** Start and Stop can each be tied to a sun event, plus an offset in minutes. The events are:
- astronomical, nautical or civil dawn (sun at −18°, −12°, −6°);
- sunrise and sunset (−0.833°, which includes refraction and the sun's semidiameter);
- civil, nautical or astronomical dusk.

**Defaults:**
- **Start:** nautical dawn, no offset.
- **Stop:** nautical dusk, no offset.
- **Why these:** on 3 and 4 Nov 2026, at 50 and 10 ms, signal appeared 30–40 min after nautical dawn. On 3 Nov it was gone
  about 35 min before nautical dusk. Those minutes record the sky-dark baseline at the run's own exposure, which shows how
  far the dark level has drifted since the pre-run darks. Astronomical times added about 1 h 40 min of night a day.
- **Long exposures:** these detect light earlier. Move the start earlier if you want the first twilight, or building
  lights and sky glow through the night.

**When the times are fixed:** the times are computed for the site when you press Start and fixed then. They are stored in
`run_metadata.json` (`planned_start_local`, `planned_stop_local`, `sun_events_local`).

**Accuracy:** solar position uses the NOAA/Meeus algorithm. On 31 Oct 2026 it agreed with astropy to within 0.012° in
solar elevation at the event instants (about 3 s of time).

**Unattended overnight start:**
1. Cover the input and record the pre-run darks now.
2. Uncover the input and press Continue.
3. LightKeeper waits in STANDBY (live view, nothing stored, system sleep blocked) and starts recording at the dawn time.
4. At the stop time it waits in RUN DONE · POST-RUN DARKS until you cover the input and record the post-run set. All
   data are already flushed by then.

## Background recorder, tray icon and resume

A run is not executed inside the window. **Start run…** hands the spectrometer to a separate background
**recorder** process. A small **supervisor** process watches the recorder and restarts it if it crashes (the same idea
as GammaKeeper's independent recorder).

- **Tray icon.** The LightKeeper icon appears in the notification area (show hidden icons ^) while a run exists. Its
  badge colour shows the state:

  | Badge | State |
  |---|---|
  | red | recording |
  | orange | burst |
  | blue | armed, waiting for the start time |
  | purple | dark set |
  | amber | needs you: uncover and Continue, or post-run darks |
  | grey | waiting for the spectrometer |

  - **Tooltip:** the run, the stop time and the slot count.
  - **Menu:** Open LightKeeper window, Open run folder, Continue (input uncovered), Record / Skip post-run darks, Stop
    run…, and Interrupt, resume later…. A double-click opens the window.
  - **Balloons:** recording started, uncover the input, run finished with post-run darks needed, and problems
    (at most one every 5 min).
- **Closing the window never stops a run.** A new window attaches by itself. It replays the recent slots: every slot
  of the last ~15 min, plus a thinned history of the whole run. It then follows live. Several windows may attach at once.
- **Crash or kill.** If the recorder dies (native fault, Task Manager, an unhandled error), the supervisor restarts it
  after 5, 10, 20, 40 or 60 s and resumes the run. After more than 6 crashes in 10 min it stops trying, and the run
  stays open for a resume by hand.
- **Recorder and supervisor die together.** While a run is open, a hidden scheduled task, `LightKeeper Watchdog`, checks
  once a minute (about 0.1 s of work). If an open run has neither process, it resumes the run; recording is back within
  about a minute plus the time to open the spectrometer. It removes itself when no open run is left. It runs only while
  you are logged in. It leaves a run alone if:
  - the run was interrupted or closed;
  - the supervisor gave up after repeated crashes;
  - you answered **Not now** when the window offered to resume it;
  - the run is more than 1 h past its scheduled stop;
  - a window has the spectrometer open. A window that lost its recorder leaves the spectrometer free so the run can return.

  To end a run on purpose, use **Stop** or **Interrupt**: killing both processes in Task Manager brings the run back.
  What it did is in `logs\watchdog_<date>.log`.
- **USB fault.** After 3 and 5 failed scans the spectrometer is closed and reopened by serial number.
- **Windows restart.** While a run is open, a per-run entry `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\LightKeeperResume_<run>`
  restarts the supervisor with `--resume` at the next **logon**. Nothing runs before you log in. The entry is removed
  when the run closes or is interrupted on purpose. It can be switched off per run with "resume automatically after a
  Windows restart".
- **Interrupt, resume later** (tray or window). Acquisition stops and everything is flushed. The spectrometer is
  released, and `run_state.json` says `interrupted`. The window offers to resume unfinished runs when it opens, and
  **Resume an unfinished run…** does it by hand.
- **What a resume does:**
  - before the start time: back to STANDBY;
  - between start and stop: recording continues at once as a new **segment**;
  - if the run was waiting for Continue: asks for Continue again;
  - after the stop time, or if recording had already ended: straight to POST_RUN for the post-run darks.
  - **Darks:** the pre-run darks are reloaded from the HDF5 files for the matched-dark display.
  - **Record:** the gap is written to `events.csv` and `run_metadata.json` (`resumes`).
- **Lost data on a hard stop:** at most the unstored rows (about `flush_s` of slots, default 30 s, and about 8 s of burst
  frames). If a flush was cut off, resume
  trims that table's columns back to equal length and logs `storage_repair`. If an hourly file cannot be opened, new rows
  for that hour go to `spectra_..._partN.h5`.

New or changed files in a run folder:
- `run_state.json`: the run journal (status `open` / `interrupted` / `closed`, phase, segment, last slot, counts,
  dark sets, files), rewritten atomically at every state change and every flush.
- `supervisor.json`: present while a supervisor looks after the run.
- `watchdog_off.json`: the watchdog leaves this run alone, and why. The next resume by hand or at logon removes it.
- `blocks/segment` (int16): 0 for the first stretch of recording, +1 for each resume. `slot_index` restarts in each
  segment; `t_wall_ns` is the time base.

Process files: `%LOCALAPPDATA%\LightKeeper\`, holding:
- `recorders\<serial>.json`: how a window finds the recorder (pid, local port, key);
- `open_runs\`;
- `logs\`: supervisor, recorder and watchdog logs. A native crash leaves a Python traceback in the recorder log through
  `faulthandler`.

**Spectrum tab cursor and slider.**
- **Cursor:** tick it, then click or drag on either plot. It snaps to a pixel and shows the wavelength, the live value and the value in the newest slot shown. Arrow keys move it one pixel, Shift+arrow ten. The toolbar's pan and zoom tools take the click while they are on.
- **Slider, under the plots:** moves the lower plot through the run. It shows the 24 stored slots up to that time, newest brightest. Far left is the start of usable light, as defined for the time range above; far right is the present, or the end of the run, and stays on the newest slot as new ones arrive.
- **Spacing:** it steps through the stored slots, of which a window keeps up to 1,500 per run, about every 32 s over a full day. The recorded files keep every slot.
- **Comparing brightness:** untick **Auto-scale** to hold the y-scale while you slide.

**Spectrum tab y-axis.**
- **Auto-scale** fits the y-axis to the data, not to the saturation line.
- **Mouse wheel** stretches y about the cursor; **Shift+wheel** zooms x; **Ctrl+wheel** zooms both.
- **Fit y** fits both panels to the data in the visible wavelength range.
- **Double-click** resets.
- Any wheel or toolbar zoom unticks Auto-scale, so the view is held while the data update. The toolbar's pan tool with
  the right mouse button drag also stretches the axes.
