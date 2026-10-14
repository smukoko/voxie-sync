#!/bin/sh
# usage: run.sh <seconds-before-screenshot> <shot-name> [xdotool script]
Xvfb :9 -screen 0 1072x1448x24 >/dev/null 2>&1 &
export DISPLAY=:9 HOME=/home/tester EMULATE_READER_W=1072 EMULATE_READER_H=1448 KO_MULTIUSER=1 SDL_AUDIODRIVER=dummy
sleep 1
cd /ko && ./reader.lua "/books/${KOSIM_BOOK:-Redeeming Love}.epub" > /out/ko.log 2>&1 &
sleep "$1"
[ -n "$3" ] && sh -c "$3"
import -window root /out/$2.png
sleep 1; pkill -f reader.lua; sleep 2
