#!/bin/bash
sleep 60 & echo $! > "$2/pid"; wait
