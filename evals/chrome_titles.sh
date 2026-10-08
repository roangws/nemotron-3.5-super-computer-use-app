#!/bin/sh
# Front window title of every Google Chrome process (this Mac runs several Chrome profiles as separate processes,
# so `tell app "Google Chrome"` can reach one with no windows).
osascript -e 'set out to ""
tell application "System Events"
  repeat with p in (every process whose name is "Google Chrome")
    try
      set out to out & (name of window 1 of p) & linefeed
    end try
  end repeat
end tell
return out'
