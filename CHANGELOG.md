# Changelog

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
