# Flow board generator (hand-off from the coordinator)

This is the generator for the May flow board, copied as it runs today. Today it runs by hand in the coordinator's session.

- `build.py` reads `data.json`, may-app's `docs/evidence/flows.json`, a thumbnails file and may-renders `device-tests/runs.json`, then writes `board.html` from `template.html`.
- `sample-data.json` is the hand-curated input for run 40055826521. It still has the coordinator's local paths in `thumbs` and `flows`.

To do: run this from `device-tests.yml` after publish, write `board.html` next to the run's results in may-renders, and take verdicts from each `report.json` rather than hand-curated data.
