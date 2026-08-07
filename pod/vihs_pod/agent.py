"""Pod agent entrypoint.

EP-001 skeleton: parses the mock-GPU flag and prints readiness. The real
pipeline (VAD/STT/LLM/TTS/lipsync/mux) lands from EP-005 onward.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vihs-pod")
    parser.add_argument(
        "--mock-gpu",
        action="store_true",
        help="run with mock stage implementations (CI-safe)",
    )
    args = parser.parse_args(argv)

    mode = "mock-gpu" if args.mock_gpu else "real"
    print(f"pod ready ({mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
