"""Deploys in a separate process and kills itself for real at a named step."""
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from livingcode.common import config  # noqa: E402
from livingcode.guard.deploy import Deployer  # noqa: E402


def main():
    config_path, tree, step = sys.argv[1:4]

    def hook(name):
        if name == step:
            os.kill(os.getpid(), signal.SIGKILL)

    Deployer(config.load(config_path), hook=hook).deploy_ref(tree)


main()
