# kosim: the real KOReader against the real gateway, no device needed

Built 2026-09-26 to verify sync behaviour on screen before touching a Kobo. It
found three sync defects that no unit test had (see `tools/test_gateway_sync.py`).

    tools/kosim/setup.sh "Redeeming Love" LBR-dd157de7f7a1
    W=/tmp/kosim
    (cd $W && python3 mock_abs.py &)                           # fake ABS on :13399
    (cd $W/gw && ABS_URL=http://127.0.0.1:13399 ABS_API_TOKEN=x UPLOAD_TOKEN=t PORT=3099 \
       PYTHONPATH=$PWD/../.. python3 -u ../../librerian_gateway_v3.py &)   # adjust path to the repo
    echo '{"currentTime": 10881.0, "duration": 57449.88, "lastUpdate": 0, "patches": []}' > $W/abs_state.json
    docker run --rm --platform linux/arm64 --add-host=host.docker.internal:host-gateway \
      -v $W/lib/koreader:/ko -v $W/home:/home/tester -v $W/books:/books -v $W/out:/out \
      -v $W/run.sh:/run.sh -e KOSIM_BOOK="Redeeming Love" koemu \
      sh /run.sh 20 shot 'for i in 1 2 3 4 5 6 7 8 9 10 11; do xdotool key Right; sleep 1.2; done'

`run.sh <seconds> <name> [xdotool script]` opens the book, runs the script, and
saves `$W/out/<name>.png`. The fake ABS records every PATCH in `abs_state.json`.
Useful coordinates at 1072x1448:
- tap the top edge (536,40) for the menu
- Tools tab (466,47)
- next menu page (668,1052)
- a ConfirmBox's OK button is at about (738,797)
