# `scanimage -A` fixtures

One file per backend family, feeding the parser and fuzzy-mapper tests in `tests/unit/test_options.py`. The fixtures are modeled on the option formats the backends document (fujitsu, brscan4, sane-airscan/eSCL, the SANE `test` backend); they are not yet verbatim captures. When touching a fleet device, capture the real listing with `scanimage -d <dev> -A > tests/fixtures/scanimage-A/<name>.txt` and replace the modeled file, keeping the test expectations honest.
