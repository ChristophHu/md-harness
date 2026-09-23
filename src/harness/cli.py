import argparse

from harness.application import build_orchestrator
from harness.config import ConfigError


def main() -> None:
    parser = argparse.ArgumentParser(description="MD Harness")
    parser.add_argument("--version", action="version", version="0.1.0")
    parser.add_argument("--config", default="config/config.yaml")
    subparsers = parser.add_subparsers(dest="command")
    for command in ("run", "resume", "cancel"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("task_id", type=int)
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    try:
        orchestrator, connection = build_orchestrator(args.config)
        try:
            if args.command == "run":
                result = orchestrator.run(args.task_id)
            elif args.command == "resume":
                result = orchestrator.resume(args.task_id)
            else:
                result = orchestrator.cancel(args.task_id)
        finally:
            connection.close()
    except ConfigError as error:
        parser.error(str(error))
    print(result.message or result.status.value)
    if result.status.value == "failed":
        raise SystemExit(1)
