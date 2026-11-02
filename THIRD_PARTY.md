# Third-party notices

## OceanDirect SDK (Ocean Insight)
LightKeeper loads the OceanDirect SDK's Python bindings and native library at run time from `OCEANDIRECT_HOME`
(default `C:\Program Files\Ocean Optics\OceanDirect SDK`). The SDK is **not** included in this repository; obtain and
install it from Ocean Insight under their license terms.

## NIST Atomic Spectra Database line lists
`lightkeeper/data/nist_asd/*.tsv` are unmodified output of the NIST Atomic Spectra Database (ASD) lines query,
retrieved 2026-10-31; the query parameters are in `PROVENANCE.json`. Cite as: Kramida, A., Ralchenko, Yu., Reader, J.,
and NIST ASD Team, NIST Atomic Spectra Database (version 5.x), National Institute of Standards and Technology,
Gaithersburg, MD, https://physics.nist.gov/asd.

## Python packages
PySide6 (LGPLv3), NumPy, SciPy, Matplotlib and h5py (BSD-style licenses) are installed separately and not redistributed.
