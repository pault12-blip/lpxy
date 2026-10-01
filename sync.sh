#!/bin/sh

cd /u2/LLL/lpxy

cp map.txt map.old
python3 map.py > map.txt
diff map.old map.txt > delta.txt
./dif.pl delta.txt >> feed.log

./costs.py > costs.txb

