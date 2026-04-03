#!/usr/bin/env python3
"""
CLI entry for the aiagent scaffold.
"""

from __future__ import annotations

import argparse
import ast
import json

from .agent import CustomAIAgent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the aiagent scaffold")
    parser.add_argument(
        "--prompt",
        help="Run a full model + tool loop for one prompt",
    )
    parser.add_argument(
        "--model",
        default="claude-3-5-sonnet-20241022",
        help="Model label recorded in agent status",
    )
    parser.add_argument(
        "--tool",
        help="Optional direct tool call name, for example: describe_tools, read, bash",
    )
    parser.add_argument(
        "--arguments",
        default="{}",
        help='JSON object for the direct tool call, for example: {"path":"README.md"}',
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Start the interactive s02-style agent loop",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print agent/tool status and exit",
    )
    return parser.parse_args()


def parse_tool_arguments(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = ast.literal_eval(raw)

    if not isinstance(value, dict):
        raise ValueError("tool arguments must decode to an object")
    return value


def main() -> None:
    args = parse_args()
    agent = CustomAIAgent(model_name=args.model)

    if args.status:
        print(json.dumps(agent.get_status(), ensure_ascii=False, indent=2))
        return

    if args.tool:
        tool_args = parse_tool_arguments(args.arguments)
        result = agent.call_tool(args.tool, **tool_args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.interactive:
        agent.interactive_mode()
        return

    if args.prompt:
        result = agent.run_prompt(args.prompt)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    payload = agent.run_once("Inspect this repository and suggest the next aiagent improvement.")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
