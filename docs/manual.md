# LightKeeper manual

Time-resolved spectroscopy logger for Ocean Insight spectrometers driven through the OceanDirect SDK. It is a sibling of
[GammaKeeper](https://github.com/Fisicas/GammaKeeper): same look, same rules, with the spectra in front.

LightKeeper records raw spectra on a fixed time grid, with scheduled fast bursts. Every run is bracketed by dark sets
over the whole integration-time ladder it can use. Wavelength calibration from emission-line lamps against bundled NIST
ASD line lists is built in.


## Start
- **Launch:** double-click `Launch_LightKeeper.cmd`. It uses the first `pythonw.exe` on PATH;
  set `LIGHTKEEPER_PYTHON` to override.
- **Close OceanView first.** Only one program can hold a USB spectrometer.
- **Requirements:** PySide6, numpy, scipy, matplotlib and h5py (all in the shared environment), plus the OceanDirect SDK
  at `C:\Program Files\Ocean Optics\OceanDirect SDK`, or wherever `OCEANDIRECT_HOME` points.

## Window
| Area | What it shows |
|---|---|
| Header | Run name (hover for the full folder path); the state indicator; the clock; **Hide setup / Show setup** (Ctrl+B); **More** (open run folder, resume an unfinished run, open log folder, about); **Full screen** (F11). |
| Metric strip | Slots recorded, integration time, scans per slot, peak counts, saturated slots, bursts, dark sets and data written. On the right: the next event (start countdown, next burst, stop) and the background recorder it is attached to. |
| Setup panel | Spectrometer; Live view; Run (label, geometry, notes, folder); Schedule; Acquisition (time slot, integration range, bursts, dark sets). **Show advanced settings** opens the ladder, dark time per integration time, SD storage, site and restart resume. |
| Spectrum tab | **Live scan:** counts or counts/s, the matched dark and the saturation line. **Wavelength strip:** each wavelength in its colour, fading into the UV and the NIR. **Recent slots:** the last 24, newest brightest. The toolbar has navigation, Auto-scale, **Fit y** and **Display** (dark subtraction, counts/s, log y, hide hot/cold pixels, calibrated axis). |
| Waterfall | Recorded spectra over time, as raw counts or as spectral shape normalised in a band you choose. |
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
4. Cover the input. The **pre-run dark set** records every ladder integration time (about a minute for each set with the default ladder).
5. Uncover the input when asked, without moving the fiber, and press Continue.
   - **Start mode "Now":** recording starts at once.
   - **Start mode "At a set time":** the banner shows STANDBY until the start time.
6. **Recording.** Each slot (default 1 s) stores the per-pixel mean and SD of the n raw scans in that slot.
   - **Exposure:** integration time steps along the ladder automatically, never below the minimum you set (default 500 ms)
     and never below 8 ms on a Maya2000 Pro.
   - **Bursts:** at the scheduled times, every scan is stored at ≤ 50 ms by default. A burst predicted to fill < 1 % of
     full scale is skipped and logged.
7. At the stop time, or when you press Stop: cover the input. The **post-run dark set** covers the whole ladder again.
   Then the files are closed.

**Why the dark sets are run-level:** darks are never taken during a run, because the input cannot be covered in the field.
The pre- and post-run sets cover every integration time the run may use. Dark drift during the day is interpolated in
analysis between the two sets, using the UV floor (205–290 nm) as a tracer.

## Files (`<Save to>\<date>_<label>_vNN\`, a new folder every time)
- **`run_metadata.json`:**
  - the plan;
  - device info (model, serial, SDK and actual minimum integration, electric-dark and nonlinearity flags);
  - the integration-time ladder;
  - the Windows time-sync status;
  - the auto-exposure rules;
  - final row counts.
- **`events.csv`:** a journal of every event, with UTC nanoseconds and local time.
- **`spectra_YYYYMMDD_HHMM.h5`:** files rotate hourly. Each holds `wavelength_nm_device` and three groups:
  - **`blocks`**: one row per slot. Fields: `t_wall_ns`, `t_start_wall_ns`, `t_end_wall_ns`, `slot_index`, `segment` (0.2.0), `int_us`,
    `n_scans`, `mean`, `sd`, `max_raw_window`, `p995_mean_window`, `n_saturated_scans`, `overrun_ms`.
  - **`dark_scans`**: every dark scan, with `int_us`, `session` and `label` (`pre` or `post`).
  - **`burst`**: every burst scan, with `int_us` and `burst_id`.
- **Raw data only.** No dark subtraction, smoothing or resampling is applied in the files. Electric-dark and nonlinearity
  corrections are the device's own and are recorded as flags.
- **Times:** all stored times are UTC. Daylight-saving changes affect only folder and file names; the repeated hour in
  autumn gets a `b` suffix (`spectra_20261101_0100b.h5`). The local time zone is `LIGHTKEEPER_TZ` (default
  `America/New_York`).
- **Setup and clock:** `run_metadata.json` and every HDF5 file carry the setup fields (fiber, diffuser, view
  azimuth/elevation, window, site altitude), `format_version` (2; files without it are format 1) and the SNTP clock
  checks (`clock_offset_s`, `clock_rtt_s`, `clock_checks`). An optional setup photo is copied to `setup_photo.<ext>`.
- **Crash safety:** a file is opened only during its 30 s flush, so a crash loses at most that buffer.
- **Sleep:** Windows sleep is blocked while a run is active.

## Calibration (`<Save to>\..\calibrations\<date>_<lamp>_vNN\`)
1. Choose the lamp. The bundled lamps are:
   - **Discharge and calibration lamps:** UVC germicidal (Hg + Ar fill), Hg-Ar, Hg, Ne, Ar, Ne+Ar, He, H (Balmer), Kr,
     Xe, Cd, Zn.
   - **Na:** check only. D1/D2 blend at this resolution, and Na has too few isolated lines to calibrate on its own.
2. Acquire a dark (lamp off) and the lamp spectrum, at the same integration time and with no saturation.
3. Fit. The procedure:
   - **Peaks:** found by prominence ≥ S/N × noise. Saturated peaks are rejected. Peak positions use a 3-point parabola.
   - **Reference lines:** NIST lines above the minimum relative intensity and isolated, with no other candidate line
     within 1 nm.
   - **Matching:** within 1.5, then 0.8, then 0.5 nm.
   - **Fit:** cubic in pixel, with 3σ clipping. The leave-one-out rms is reported.
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
  - the 2026 daylight-saving change.
- **GUI checks**, run off-screen with `QT_QPA_PLATFORM=offscreen`:
  - `tests/gui_recorder_smoke.py`: window close and re-attach, y-scaling and recorder kill.
  - `tests/gui_schedule_running.py`: the Schedule tab during a run.
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

## Sun-event scheduling

**Setting it up:** Start and Stop can each be tied to a sun event, plus an offset in minutes. The events are:
- astronomical, nautical or civil dawn (sun at −18°, −12°, −6°);
- sunrise and sunset (−0.833°, which includes refraction and the sun's semidiameter);
- civil, nautical or astronomical dusk.

**Defaults:**
- **Start:** astronomical dawn − 20 min.
- **Stop:** astronomical dusk + 20 min.
- **Why the margins:** the pre-dawn and post-dusk minutes record the night baseline at that window: building lights,
  headlights, sky glow.

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
- **Lost data on a hard stop:** at most the unflushed buffer (≤ `flush_s`, default 30 s). If a flush was cut off, resume
  trims that table's columns back to equal length and logs `storage_repair`. If an hourly file cannot be opened, new rows
  for that hour go to `spectra_..._partN.h5`.

New or changed files in a run folder:
- `run_state.json`: the run journal (status `open` / `interrupted` / `closed`, phase, segment, last slot, counts,
  dark sets, files), rewritten atomically at every state change and every flush.
- `supervisor.json`: present while a supervisor looks after the run.
- `blocks/segment` (int16): 0 for the first stretch of recording, +1 for each resume. `slot_index` restarts in each
  segment; `t_wall_ns` is the time base.

Process files: `%LOCALAPPDATA%\LightKeeper\`, holding:
- `recorders\<serial>.json`: how a window finds the recorder (pid, local port, key);
- `open_runs\`;
- `logs\`: supervisor and recorder logs. A native crash leaves a Python traceback in the recorder log through
  `faulthandler`.

**Spectrum tab y-axis.**
- **Auto-scale** fits the y-axis to the data, not to the saturation line.
- **Mouse wheel** stretches y about the cursor; **Shift+wheel** zooms x; **Ctrl+wheel** zooms both.
- **Fit y** fits both panels to the data in the visible wavelength range.
- **Double-click** resets.
- Any wheel or toolbar zoom unticks Auto-scale, so the view is held while the data update. The toolbar's pan tool with
  the right mouse button drag also stretches the axes.
