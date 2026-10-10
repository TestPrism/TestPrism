"""Use native label-filtered cleanup; never prune shared images or caches."""
import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrations.testprism.bootstrap import verify_runtime
from integrations.testprism.cleanup import cleanup_native

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--bench", type=Path, required=True)
parser.add_argument("--runtime-copy", type=Path)
parser.add_argument("--journal", type=Path, required=True)
args = parser.parse_args()
if args.runtime_copy:
    verify_runtime(args.bench.resolve(), args.runtime_copy.resolve())
sys.path.insert(0, str((args.runtime_copy or args.bench).resolve()))
print(cleanup_native(args.journal))
