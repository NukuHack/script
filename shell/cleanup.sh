#!/bin/bash

# 1. Check if the hours argument is provided
if [ -z "$1" ]; then
  echo "Usage: $0 <hours> [target_directory]"
  echo "Example: $0 24 /path/to/cleanup"
  exit 1
fi

HOURS=$1
# Default to current directory (.) if no second argument is provided
TARGET_DIR=${2:-.} 

# 2. Validate that the hours argument is a valid integer
if ! [[ "$HOURS" =~ ^[0-9]+$ ]]; then
  echo "Error: Hours must be a positive integer."
  exit 1
fi

# 3. Convert hours to minutes (find's -mtime uses days, -mmin uses minutes)
MINUTES=$(( HOURS * 60 ))

echo "Scanning '$TARGET_DIR' for items not modified in the last $HOURS hours..."

# 4. Execute the find and delete command
# -mindepth 1: Prevents deleting the target directory itself
# -mmin +$MINUTES: Finds items older than X minutes
# -exec rm -rf {} +: Deletes the found items recursively
find "$TARGET_DIR" -mindepth 1 -mmin +"$MINUTES" -exec rm -rf {} +

echo "Cleanup complete."