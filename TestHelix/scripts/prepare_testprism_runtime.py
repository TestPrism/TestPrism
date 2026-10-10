"""Create the agent-owned native runtime without modifying the external benchmark."""
from pathlib import Path
import argparse
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrations.testprism.bootstrap import prepare_runtime

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(prepare_runtime(args.bench, args.out))
