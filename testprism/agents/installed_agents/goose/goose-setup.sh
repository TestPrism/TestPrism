#!/bin/bash

export GOOSE_DISABLE_KEYRING=true
export CONFIGURE=false

mkdir -p ~/.config/goose
cat > ~/.config/goose/config.yaml << EOF
GOOSE_MODEL: ${GOOSE_MODEL}
GOOSE_PROVIDER: ${GOOSE_PROVIDER}
${OPENAI_HOST:+OPENAI_HOST: ${OPENAI_HOST}}
${AZURE_OPENAI_ENDPOINT:+AZURE_OPENAI_ENDPOINT: ${AZURE_OPENAI_ENDPOINT}}
${AZURE_OPENAI_DEPLOYMENT_NAME:+AZURE_OPENAI_DEPLOYMENT_NAME: ${AZURE_OPENAI_DEPLOYMENT_NAME}}
${AZURE_OPENAI_API_VERSION:+AZURE_OPENAI_API_VERSION: ${AZURE_OPENAI_API_VERSION}}
extensions:
  developer:
    bundled: true
    display_name: Developer
    enabled: true
    name: developer
    timeout: 300
    type: builtin
EOF

if command -v goose >/dev/null 2>&1; then
  echo "Using preinstalled Goose runtime"
  goose --version
else
apt-get update
apt-get install -y curl bzip2 libxcb1 libgomp1

curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh | bash

export PATH="/root/.local/bin:$PATH"
fi
