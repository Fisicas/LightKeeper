# Changelog

## 0.3.15 (2026-11-05)
- **Fixed:** pressing Tab in the setup panel could shift its content sideways and cut off the labels, with no way back short of
  restarting. The panel now fits its content, and sideways scrolling is locked. Tab still scrolls it up and down.

## 0.3.14 (2026-11-05)
- **Splash screen.** The window now opens with a splash in the style of the project's social card. Each loading step shows a spinning
  ring while it runs, then a check mark and its time, and the splash closes once the main window is open. A failure while starting now
  shows a message instead of vanishing silently under `pythonw`. Nothing changes in recording, and the background recorder does not use it.

## 0.3.13 (2026-11-04)
Measured on a Maya2000 Pro at 10 ms, writing to a USB hard drive, and by replaying the whole 4 Nov 2026 run through the new writer.
- **Frames keep coming.** Bursts at 10 ms now run at 99.85 frames/s (was 97.1), against about 99.88 possible. The longest gap was
  26 ms (was 120 ms).
  - **Saving:** a file thread does the writing, so saves no longer stop the frame loop.
  - **Slot edges:** at 8 or more frames per slot the loop no longer idles until the next slot, which lost about one frame per
    slot during bursts. A frame belongs to the slot its return time falls in.
- **No gap in the 1 s record.** A burst at the slot's own integration time keeps the slots going and also saves each frame, with no
  exposure command and no discarded frames. A burst at another time still pauses the slots.
  - **Changed:** those slots summarize frames that are also in `burst`, so do not count them twice. `bursts_done` marks them
    `mode: inline`.
- **Smaller files, every value kept.** The 4 Nov run takes 1.06 GB instead of 1.50 GB (-30 %), with every stored value identical.
  - Holes left by appending fell from 13.7 % to 0.2 %: only whole chunks are written, except on a forced flush.
  - `blocks` arrays gain the shuffle filter. Frames use chunks of 400 rows by 16 pixels, which also makes one pixel through time
    much faster to read.
  - Built-in HDF5 filters only; the layout, `format_version` and older files are unchanged.
  - **Changed:** a crash can lose up to about 8 s of burst frames, held to complete a chunk, as well as about 30 s of slots.
- **Save every frame.** New option: every individual spectrum of the run. About 21 GB for a 12.6 h day at 10 ms.
- **A sleeping data drive.** After 2 minutes without a write, the file thread wakes the drive before it opens an HDF5 file, so the
  spin-up cannot hold up the frame loop. It adds no wait while recording.
- **Longer short darks.** The cap of 30 scans is now 12,000, so a dark gets the time it is set to. Long darks are unchanged.
- **New defaults in the window.** Bursts of 60 s every 15 min at minute 7, burst minimum 0 counts, dark time 30 s. A run from before
  0.3.13 that only holds the old defaults does not pass them on; a choice someone made does.
- **Burst minimum** is now a setting. Plans from older versions keep 600 counts, including on resume.
- **Weather.** Optional `weather_vNN.json` beside a run, from `LightKeeper.py --fetch-weather`, the More menu or a tick box. It runs after
  recording and never in the engine or recorder. The site is rounded to two decimals before it is sent.
- **Moving the Save in folder.** A `setup_fallback_dir` setting lets the first run in an empty folder copy its setup from the old one.
  The pre-flight check now fails if the drive for **Save in** is missing.
- **Fixed:** `burst_int_us` in the metadata reported the requested time, not the time used; the requested time is now
  `burst_requested_int_us`. A burst now stops at the planned stop time. A failed burst no longer leaves the engine in BURST at the
  burst's exposure. The data estimate no longer counts bursts in the dark or ignores compression.

## 0.3.12 (2026-11-04)
- **A new run starts from the previous run's setup.** When the window opens, and when a run closes, the setup panel is
  filled from the newest run in **Save in**, including the site, diffuser, fiber, schedule and acquisition settings.
  - **Changes by itself:** dates and times; a label ending in `day N` counts up; a setup photo that no longer exists is dropped.
  - **Exception:** a run from before 0.3.12 does not pass on the old default times, so the next run uses the new default.
- **Wavelength cursor.** A **Cursor** tick box on the Spectrum and Waterfall tabs: click or drag to place it, arrow keys move
  it one pixel. It shows the wavelength and the value there; on the Waterfall, the time as well.
- **Slider through the run.** Under the Spectrum plots, the slider moves "Recent slots, newest brightest" from the start of
  usable light (far left) to the present or the end of the run (far right). The Live scan plot is slightly shorter to fit it.
  Opening a recorded run now fills this plot too.
- **Raw counts on the Waterfall.** A new view, **Raw counts, as recorded**: mean counts per scan as stored, with no dark
  subtraction and no scaling. The window keeps the stored means to do this, which adds up to about 12 MB of memory per run.
- **New default schedule: nautical dawn to nautical dusk, no offsets.** It was astronomical dawn − 20 min to astronomical
  dusk + 20 min. This drops about 1 h 40 min of night a day at
  the field site in November. Existing runs keep their own plan, and any event and offset can still be chosen in the setup panel.
  - **Why:** on 3 and 4 Nov, at 50 and 10 ms, signal appeared 30–40 min after nautical dawn. On 3 Nov it was gone about
    35 min before nautical dusk. That margin still records the sky-dark baseline, which shows how far the dark level has
    drifted during the day.

## 0.3.11 (2026-11-03)
- **Watchdog.** A hidden scheduled task checks once a minute while a run is open, and resumes the run if its recorder and
  supervisor have died together. Until now only opening the window or logging on again could do that.
  - **Why:** on 3 Nov, a Claude desktop update closed a supervisor that had been started from inside Claude, mid-burst.
  - **It leaves a run alone** if it was interrupted or closed, if the supervisor gave up after repeated crashes, if you
    answered **Not now** in the window, if it is more than 1 h past its scheduled stop, or while a window has the
    spectrometer open. The task removes itself when no open run is left.
  - **Behaviour change:** killing both processes in Task Manager no longer ends a run. Use **Stop** or **Interrupt**.
- **One supervisor per spectrometer.** A named lock settles a window, the logon entry and the watchdog resuming at once.
- **A window that lost its recorder** no longer takes the spectrometer for live view while the run can still come back.
- **Fix: the Start dialog's default back-off at 500 ms.** Back off (0.3.9) defaults to 10 ms steps, which at the default
  500 ms means 50 integration times: each dark set takes about 4 min, not 26 s, and reaching 10 ms takes 49 s. The
  pre-flight list now warns about this. At 50 ms it is 5 times, as intended.
  - **Fix:** with back-off on, a burst at the fixed time was wrongly flagged as longer than the shortest slot time.
- **Logon entries clean themselves up.** A supervisor refusing a closed run now removes that run's logon entry; a run
  closed on 3 Nov left one behind, which only caused a harmless refusal at logon. The cause was not found.
- **Fix: the recorder log was always empty.** Windows reports the supervisor's NUL output as a terminal, so every message
  was discarded. Messages now reach `logs\recorder_*.log`, and a full disk cannot raise an error from a log message.

## 0.3.10 (2026-11-03)
- **Save spectrum asks where to save.** The button (and Ctrl+S) opens a Save As dialog. It starts in Documents, then in
  the folder you last saved to. The suggested name is the scan time followed by the run name shown in the header (its
  leading date dropped when it repeats the scan's), e.g. `2026-11-03_13-41-46_polystyrene_diffuser_upper_canopy_v01.dpt`;
  with no run, the label from the setup panel. Cancel saves nothing; a name typed without an extension gets `.dpt`.
  - **Fix:** spectra used to go, without asking, to a `spectra` folder beside the run output folder, which could be a
    hidden or temporary location.

## 0.3.9 (2026-11-03)
- **Back-off for fixed exposure.** New **Back off** setting (Start dialog, next to Exposure; default 10 ms steps down to
  10 ms, 0 = off). After any slot whose peak reaches 55,000 counts, the next slot runs one step shorter (50 → 40 → 30 →
  20 → 10 ms, one step per 1 s slot). After 30 slots in a row that would stay under 40,000 counts at the next longer
  time, it steps back up, never above the fixed time. Every step is in the dark ladder, so the post-run darks cover it.
  `run_metadata.json` records the settings under `backoff`; `run_summary.json` reports the mode as "fixed with back-off".
- **Tabs no longer move.** The Time range bar (Waterfall, Time series) now sits under the tab strip, inside the page,
  instead of above the tabs, so switching tabs never shifts them.
- **Resume picks up an amended plan.** If a run's plan in `run_metadata.json` was changed while it was interrupted (for
  example back-off added), resume widens the integration-time and dark ladders and logs `plan_amended`.

## 0.3.8 (2026-11-02)
- **Fixed exposure by default.** A run records at a fixed 500 ms unless **Exposure** is set to **Automatic** (which
  moves along the ladder within the **Auto range**, unchanged for now). The run's exposure is now its own setting:
  - **Fix:** since 0.3.5, a run took its exposure from the Live view controls, which open at Fixed 100 ms, so a run
    started from a fresh window would have recorded at 100 ms.
- **Fix: bursts at their own integration time (B1).** A 50 ms burst was snapped onto the slot ladder (500–3000 ms),
  so every burst on 1 Nov ran at 500 ms, 2 scans/s. Bursts now run at the **Burst … at** time (never longer than the
  slot time in use), and that time gets its own dark.
- **Darks at two times under fixed exposure (B5).** A fixed run takes darks at the fixed time, the burst time and
  **Long dark (fixed)** (default 3000 ms, advanced settings), so the dark-drift model has the dark current at two
  integration times. Automatic runs keep their whole ladder plus the burst time.
- **Temperature logging.** At connect, LightKeeper records which SDK features the spectrometer reports
  (`features_supported`), its nonlinearity coefficients and its temperature source in the device information,
  the Log and `run_metadata.json`.
  - **Devices with a readable sensor:** every slot gets `blocks/temperature_c`, shown as **Detector, °C**. The
    OceanDirect SDK reads temperature only through a thermo-electric cooler's thermistor.
  - **The Maya2000 Pro:** it is uncooled, and the SDK release notes say its temperature feature was removed, so no
    temperature is logged and `temperature_source` says so. The simulator drifts slowly, for testing.
- **Pre-flight checks at Start.** The Start dialog lists, as ✔ / ⚠ / ✖:
  - exposure mode;
  - burst time and its dark;
  - dark times (one only is flagged);
  - site and time zone;
  - empty setup fields and no photo;
  - clock offset (one SNTP query);
  - disk space against the estimate;
  - electric dark / nonlinearity changed since the last run;
  - a diffuser that differs from the last run with the same label;
  - temperature logging.

  The list is written to the Log.
- **Post-run dark reminder repeats.** The tray reminds you (with a beep) every 15 min until the post-run darks are
  taken or skipped, and `post_darks_delay_min` records how long it took.
- **`run_summary.json` at close.** It records:
  - slots and saturated slots;
  - each integration change with its time;
  - bursts with their integration and realised scan rate;
  - dark times and pre/post dark agreement;
  - usable light (the 1 % rule, and > 100 counts/s), with the sun elevation at each end;
  - the clock-offset range;
  - the temperature range;
  - the version.

## 0.3.7 (2026-11-01)
- **Time range for the Waterfall and Time series tabs.** A bar above these tabs chooses **Usable daylight (auto)**,
  **All data** or **Custom** (from/to), and quotes the range drawn. Usable daylight starts and ends where the
  420–700 nm dark-corrected signal stays above 1 % of the day's bright level for 5 minutes, so the pre-dawn and
  after-dusk dark noise no longer swamps the waterfall and the ratios. On the 1 Nov 2026 forest-edge run this is
  07:38–17:29. During a live run the newest slots stay in view. **Reset** returns to the automatic range.
- **Open a recorded run (read-only).** **Open recorded run…** (on the range bar, or More) reads a run's HDF5 files
  in the background, dark-corrects them with the run's pre-run dark set, and shows them in the Waterfall and Time
  series tabs from the usable part of the day. The files are opened read-only; nothing in the run folder changes.
  It is refused while a run is being recorded or watched, and a run that starts replaces it on screen. **More →
  Close the recorded run** clears it.
- **Calibration plot scaling.** **Fit y** stretches both calibration plots to the data in the visible wavelength
  range (ignoring the 60,000-count line). The mouse wheel zooms as on the Spectrum tab, and a double-click resets.
- **Site changes update the Schedule at once.** Changing the latitude or longitude used to wait for the 30 s
  refresh. The Schedule now says "Updating the schedule for the new site…" and redraws straight away.
- **Details inside the .dpt.** OPUS reads `#` header lines, so each saved spectrum now starts with the main details
  (scan time, integration, device, processing, label, geometry, notes). The data rows are unchanged, and the `.json`
  still holds everything, including the raw and dark counts.

## 0.3.6 (2026-11-01)
- **Save spectrum (Ctrl+S).** Saves the scan on screen, processed as displayed (dark subtraction, hidden pixels
  bridged, counts or counts/s, display axis), to `<Save in>\..\spectra\<label>_<YYYY-MM-DD_HH-MM-SS>.dpt`. With no
  label the name is the scan time. The `.dpt` holds only two comma-separated columns (wavelength in nm, signal), so
  OPUS and other programs open it as a data point table. A `.json` with the same name holds the details: time,
  integration, device and corrections, processing, setup fields, site, and the raw and dark counts.
- **Geometry starts empty.** The last development prefill is gone; it shows an example instead.

## 0.3.5 (2026-11-01)
- **Opens the spectrometer by itself.** Like OceanView, the window opens the first attached Ocean spectrometer and
  starts the live view at a fixed 100 ms, one scan per update, without pressing Connect. A spectrometer plugged in
  later is picked up within about 5 s. After you press Disconnect it is left alone until the next start, and a device
  that cannot be opened (for example while OceanView holds it) is tried once. The simulator is never opened
  automatically. `LIGHTKEEPER_AUTOCONNECT=0` turns this off.
- **Site checks.** The site is shown as N/S and E/W under the fields and in the Schedule panel.
  - **Unset site (0°, 0°):** flagged, and a run that starts or stops on a sun event is refused until it is set.
  - **Longitude that does not fit the time zone:** when solar noon falls more than 3 h from midday on the local
    clock, the window says so and suggests the sign change (west longitudes are negative); a sun-event run asks
    before it starts.
- **Calibration steps.**
  - **1. Acquire dark** now takes darks at every ladder time plus the calibration time, and finds the hot and cold
    pixels. The lamp is matched to the dark at its own integration time.
  - Hot and cold pixels are bridged before peak finding and listed in `calibration.json`.
  - The next step is highlighted in yellow: 1, then 2, then 3, then Save. Changing the lamp or a calibration time
    that the dark set does not cover moves the highlight back.
- **Fix: sun times on daylight-saving days.** The 1-minute grid stepped through the local clock, so in autumn it
  never sampled the repeated hour and in spring it stepped through the missing one. It now steps through real time
  from local midnight to the next local midnight. Sun events between 01:00 and 03:00 local time were the only ones
  affected.
- **Take dark set… without a run.** In Live view, cover the input and press **Take dark set…**: 5 scans at the
  longest integration time find the hot and cold pixels for this spectrometer, and 5 at the live view's time give it a
  matched dark. Nothing is written to a run folder; a run still starts from its own dark sets.
- **Hot-pixel status in the readout.** The live view says how many pixels are hidden, or "no hot-pixel list" when none
  is saved, and the Log tab says why a saved list was not used.
- **Window log file.** The Log tab is also written to `%LOCALAPPDATA%\LightKeeper\logs\window_<date>.log`.
- **Tests keep their settings apart.** With `LIGHTKEEPER_HOME` set, the window keeps its settings in
  `settings.ini` there. Before, the GUI checks could overwrite the user's saved run folder.

## 0.3.4 (2026-11-01)
- **Hot and cold pixels from the dark sets.**
  - After each dark set, the individual scans at the longest integration time are searched: a pixel is listed when it
    is more than 5 robust sigma above or below a 15-pixel running median in at least 80 % of those scans.
  - The run stores the list in `run_metadata.json` and as `hot_pixels` / `cold_pixels` attributes on its HDF5 files.
    Raw spectra are unchanged.
  - The latest list for each spectrometer is remembered, so **Hide hot and cold pixels** now works in the live view
    outside a run too. Before, it worked only once a run had darks.
  - The waterfall bridges hidden pixels instead of dropping them, so its wavelength axis stays exact.
  - The Display setting is remembered.

## 0.3.3 (2026-11-01): first public release
- **Portable defaults.**
  - The run folder is `Documents\LightKeeper Runs`, or `LIGHTKEEPER_DATA_ROOT`.
  - The site is `LIGHTKEEPER_SITE` (`lat,lon`), or 0, 0 until you set it.
  - The window remembers the last run folder and site.
- **Time zone.** `LIGHTKEEPER_TZ` (an IANA name) sets the local time used for folder names, file names and sun schedules.
  The default is `America/New_York`.
- **Launcher.** It uses `LIGHTKEEPER_PYTHON`, or else the first `pythonw.exe` on PATH.
- **Setup fields.** The setup fields and the run label start empty.
- **Unchanged.** Acquisition and file formats are the same as 0.3.2.

## 0.3.2 (2026-11-01)
- **Fix: an armed run could drop to IDLE after its pre-run darks and never start.**
  - **Cause:** a reconnect request queued behind Start reopened the spectrometer while the run was still open.
  - **Fix:** the engine now ignores connect requests while a run exists, and logs `connect_ignored`. The recorder
    stops reconnecting once Start or Resume has been sent.
  - **Test:** `TestReconnectRace` reproduces the failure. It fails on 0.3.1 and passes on 0.3.2.

## 0.3.1 (2026-10-31)
- **Exposure defaults.** Integration time and the minimum are now 500 ms; the maximum is still 3000 ms. Bursts use up
  to 50 ms.
- **Dim-burst skip.** A burst predicted to fill < 1 % of full scale is skipped and logged as `burst_skipped`, with the
  prediction.
- **Clock check.** One SNTP query (UDP 123, standard library only) runs at Start, hourly in a background thread, and at
  Stop.
  - The offset and round trip are saved in `run_metadata.json` and `events.csv`.
  - The status bar turns amber if the offset is over 1 s or the Windows time service is not running.
- **Setup metadata.** New fields: fiber core, diffuser, diffuser thickness, fiber–diffuser distance, view azimuth and
  elevation, window state, site altitude and setup photo.
  - They are stored in the plan and as HDF5 attributes.
  - The photo is copied into the run folder.
  - Old plans still load.
- **`format_version` = 2** on every HDF5 file and in `run_metadata.json`. Files without it are format 1.
- **Daylight saving.** Two bugs found by the new `tests/test_dst_2026_11_01.py` are fixed:
  - The estimated duration was 1 h short across the change.
  - Both passes through the repeated autumn hour got the same file name. The second is now `..._0100b.h5`.

## 0.3.0 (2026-10-31)
- **Redesigned window.**
  - A header with the run name, the state indicator and the More menu.
  - One metric strip.
  - Full screen (F11).
  - A setup panel you can hide (Ctrl+B) and that shows only the fields that apply.
  - Spin boxes ignore the mouse wheel until clicked.
- **Spectrum tab.**
  - One toolbar row.
  - A wavelength-colour strip.
  - Recent slots drawn in lamp-amber shades.
- **Schedule tab.** The plan and sun times are shown as a table.
- **Theme.** A single set of tokens drives the Qt style, the palette and the matplotlib plots.
- **Fix: lost time in the slot loop.** The slot timing loop could compute a negative sleep, which lost about 0.5 s each
  time. The sleep is now clamped at 0.

## 0.2.0 (2026-10-31)
- **Background recorder.** Runs execute in a background recorder process, kept alive by a supervisor, with a
  notification-area icon. Closing the window never stops a run.
- **Resume.**
  - `run_state.json` journals the run.
  - The run resumes automatically after a recorder crash, by hand after an interruption, and at logon after a Windows
    restart.
  - A new `blocks/segment` column marks each resumed stretch.
- **USB faults.** The spectrometer is reopened by serial number after repeated scan failures.
- **Fixed times.** Sun-event start and stop times are fixed at Start, and are never re-resolved later.
- **Spectrum y-axis.** Auto-scale fits the data. You can zoom with the mouse wheel, use Fit y, or double-click to reset.

## 0.1.1 (2026-10-31)
- **Fix:** while a run was armed or recording, the Schedule tab re-resolved a sun-event plan and could show the next
  day's times. It now shows the times fixed at Start. This was a display bug only: the recording itself was unaffected.

## 0.1.0 (2026-10-30)
- First version:
  - raw spectra on a fixed time grid, hourly HDF5;
  - pre- and post-run dark sets over the integration-time ladder;
  - auto-exposure;
  - fast bursts;
  - sun-event scheduling;
  - lamp wavelength calibration against NIST ASD lines;
  - a synthetic simulator.
