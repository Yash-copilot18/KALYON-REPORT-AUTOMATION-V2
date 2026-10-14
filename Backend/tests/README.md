# Backend test suite

Repeatable checks for the parts of Scheduled Reports that are easy to break and
expensive to get wrong: the mail quota, the Generated Time rule, validation, and
the date-range resolution.

## Running

    cd Backend
    venv\Scripts\python.exe tests\run_tests.py

The backend must be running on http://localhost:8000 (override with API_BASE).

## Safety

These tests are designed to run against the live installation without harming it:

* Every record they touch is one they created themselves. They never delete,
  update or read-modify a pre-existing schedule.
* Test schedules are named with a fixed `ZZTEST_` prefix and removed in a
  `finally` block, including after a failure.
* SMTP is stubbed in-process, so **no e-mail is ever sent** by the suite.
* The mail quota is left exactly as it was found: the runs the tests create are
  deleted with their schedule, which is what the quota counts.
* Nothing is written to `app_settings`, and the quota baseline is never moved.

If a run is interrupted, re-running the suite clears any `ZZTEST_` leftovers
before it starts.
