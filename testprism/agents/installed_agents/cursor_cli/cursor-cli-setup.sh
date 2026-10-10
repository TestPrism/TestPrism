#!/bin/bash

if command -v cursor-agent >/dev/null 2>&1; then
  echo "Using preinstalled Cursor CLI runtime"
  cursor-agent --version
else
apt-get update
apt-get install -y curl

curl https://cursor.com/install -fsS | bash

echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
fi

echo "INSTALL SUCCESS!"
