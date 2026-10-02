#!/bin/sh

cd /u2/LLL/lpxy
echo "START"
date

./sync.sh

./start.sh kill
./start.sh

echo "DONE"
date

