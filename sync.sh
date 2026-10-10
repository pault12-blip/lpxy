#!/bin/sh

cd /u2/LLL/lpxy

echo START
date

cp map.txt map.old
python3 map.py --refresh > map.txt
diff map.old map.txt > delta.txt
./dif.pl delta.txt >> feed.log
tail -200 feed.log | sponge feed.log

./costs.py > costs.tab

echo END
date
