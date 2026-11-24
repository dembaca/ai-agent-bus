#!/bin/bash
env > "$2/env.txt"; pwd >> "$2/env.txt"; echo "$@" >> "$2/env.txt"
