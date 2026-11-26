#!/usr/bin/env bash
# The symbols the shell scripts show or strip, as named constants: the one
# shell file that knows the characters (the Python side has symbols.py).
# UTF-8 bytes as octal escapes, so this file holds no emoji and works in bash 3.2.
# shellcheck disable=SC2034  # sourced by mm-agent-lib.sh; the callers use them

CB_CROWN=$'\360\237\221\221'  # crown
CB_INBOX=$'\360\237\223\250'  # incoming envelope
CB_SIGNOFF=$'\342\234\213'  # raised hand
CB_ANSWER=$'\360\237\222\254'  # speech balloon
CB_LISTENING=$'\360\237\221\202'  # ear
CB_UPDATE=$'\342\254\206'  # up arrow
