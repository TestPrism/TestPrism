#!/bin/bash

if command -v aider >/dev/null 2>&1; then
  echo "Using preinstalled Aider runtime"
  aider --version
else
apt-get update
apt-get install -y curl

curl -LsSf https://aider.chat/install.sh | sh
source $HOME/.local/bin/env
fi
